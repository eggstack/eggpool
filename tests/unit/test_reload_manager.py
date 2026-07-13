"""Tests for the ReloadManager transaction flow."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.config_reload_policy import (
    ReloadResult,
    ReloadStage,
)
from eggpool.control.reload_manager import (
    CandidateGeneration,
    ReloadInProgressError,
    ReloadManager,
    ReloadOperationStage,
    ReloadOperationState,
    ReloadPreparationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validation(
    *,
    content_digest: str = "a" * 64,
    warnings: tuple = (),
    config: MagicMock | None = None,
) -> MagicMock:
    """Build a mock ConfigValidationResult."""
    v = MagicMock()
    v.content_digest = content_digest
    v.warnings = warnings
    v.config = config or MagicMock()
    return v


def _make_diff(
    changes: tuple = (),
) -> MagicMock:
    """Build a mock ConfigDiff with optional changes."""
    d = MagicMock()
    d.changes = changes
    d.restart_required = tuple(
        c for c in changes if getattr(c, "disposition", None) == "restart"
    )
    return d


def _make_generation(generation_id: int = 1, digest: str = "b" * 64) -> MagicMock:
    """Build a mock RuntimeGeneration."""
    gen = MagicMock()
    gen.generation_id = generation_id
    gen.config_digest = digest
    gen.config = MagicMock()
    return gen


def _make_candidate(
    generation_id: int = 1,
    digest: str = "b" * 64,
) -> CandidateGeneration:
    """Build a CandidateGeneration with mock internals."""
    gen = _make_generation(generation_id, digest)
    process = MagicMock()
    diff = _make_diff()
    return CandidateGeneration(generation=gen, process=process, diff=diff)


def _make_runtime_manager(active_generation: MagicMock | None = None) -> MagicMock:
    """Build a mock RuntimeManager."""
    rm = MagicMock()
    if active_generation is None:
        active_generation = _make_generation(0)
    rm.active_snapshot.return_value = active_generation
    rm.reserve_next_generation_id.return_value = 1
    rm.install_candidate = AsyncMock()
    return rm


def _make_process() -> MagicMock:
    """Build a mock ProcessRuntime."""
    proc = MagicMock()
    proc.db = MagicMock()
    proc.stats_db = MagicMock()
    proc.metrics_coalescer = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# ReloadOperationStage constants
# ---------------------------------------------------------------------------


class TestReloadOperationStage:
    def test_stage_constants(self) -> None:
        assert ReloadOperationStage.IDLE == "idle"
        assert ReloadOperationStage.VALIDATION == "validation"
        assert ReloadOperationStage.DIFF == "diff"
        assert ReloadOperationStage.PREPARATION == "preparation"
        assert ReloadOperationStage.RECONCILIATION == "reconciliation"
        assert ReloadOperationStage.COMMIT == "commit"
        assert ReloadOperationStage.ACTIVATION == "activation"
        assert ReloadOperationStage.RETIREMENT == "retirement"

    def test_all_stages_are_strings(self) -> None:
        for attr in dir(ReloadOperationStage):
            if attr.startswith("_"):
                continue
            val = getattr(ReloadOperationStage, attr)
            assert isinstance(val, str)


# ---------------------------------------------------------------------------
# ReloadManager initialization
# ---------------------------------------------------------------------------


class TestReloadManagerInit:
    def test_manager_init(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)
        assert mgr._runtime_manager is rm
        assert mgr._process is proc

    def test_operation_state_initially_none(self) -> None:
        mgr = ReloadManager(_make_runtime_manager(), _make_process())
        assert mgr.operation_state is None

    def test_custom_drain_timeout(self) -> None:
        mgr = ReloadManager(
            _make_runtime_manager(), _make_process(), drain_timeout_s=60.0
        )
        assert mgr._drain_timeout_s == 60.0


# ---------------------------------------------------------------------------
# Digest validation
# ---------------------------------------------------------------------------


class TestDigestValidation:
    @pytest.mark.asyncio
    async def test_validate_digest_matching(self) -> None:
        mgr = ReloadManager(_make_runtime_manager(), _make_process())
        validation = _make_validation(content_digest="a" * 64)
        await mgr._validate_digest(validation, "a" * 64)

    @pytest.mark.asyncio
    async def test_validate_digest_mismatch(self) -> None:
        mgr = ReloadManager(_make_runtime_manager(), _make_process())
        validation = _make_validation(content_digest="a" * 64)
        with pytest.raises(ReloadPreparationError, match="Content digest mismatch"):
            await mgr._validate_digest(validation, "b" * 64)

    @pytest.mark.asyncio
    async def test_validate_digest_none_expected(self) -> None:
        mgr = ReloadManager(_make_runtime_manager(), _make_process())
        validation = _make_validation(content_digest="a" * 64)
        await mgr._validate_digest(validation, None)


# ---------------------------------------------------------------------------
# Reload transaction: concurrency guard
# ---------------------------------------------------------------------------


class TestReloadConcurrency:
    @pytest.mark.asyncio
    async def test_rejects_concurrent(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        # Patch internals so the first reload blocks on _build_candidate_generation
        block_event = asyncio.Event()

        async def _blocking_build(
            *args: object, **kwargs: object
        ) -> CandidateGeneration:
            await block_event.wait()
            return _make_candidate()

        validation = _make_validation()
        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock
            ) as diff_mock,
            patch.object(
                mgr, "_build_candidate_generation", side_effect=_blocking_build
            ),
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
        ):
            diff_mock.return_value = _make_diff(changes=(MagicMock(section="routing"),))

            # Start first reload, let it block
            async def _run_first() -> ReloadResult:
                return await mgr.reload(validation)

            task = asyncio.create_task(_run_first())
            # Give the first reload time to acquire the lock
            await asyncio.sleep(0.05)

            # Second reload should fail immediately
            with pytest.raises(ReloadInProgressError):
                await mgr.reload(validation)

            # Unblock the first
            block_event.set()
            await task


# ---------------------------------------------------------------------------
# Reload transaction: semantic no-op
# ---------------------------------------------------------------------------


class TestReloadSemanticNoop:
    @pytest.mark.asyncio
    async def test_semantic_noop(self) -> None:
        active_gen = _make_generation(7, "c" * 64)
        rm = _make_runtime_manager(active_gen)
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        validation = _make_validation()
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = _make_diff(changes=())
            result = await mgr.reload(validation)

        assert result.ok is True
        assert result.generation == 7
        assert result.stage == ReloadStage.COMMIT
        assert result.changed_sections == ()
        assert "No configuration changes" in result.message


# ---------------------------------------------------------------------------
# Reload transaction: restart-required rejection
# ---------------------------------------------------------------------------


class TestReloadRestartRequired:
    @pytest.mark.asyncio
    async def test_rejects_restart_required(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="server", disposition="restart")
        restart_changes = (change,)

        validation = _make_validation()
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            fake_diff = _make_diff(changes=restart_changes)
            fake_diff.restart_required = restart_changes
            diff_mock.return_value = fake_diff
            result = await mgr.reload(validation)

        assert result.ok is False
        assert result.stage == ReloadStage.DIFF
        assert result.generation is None
        assert "restart-required" in result.message


# ---------------------------------------------------------------------------
# Reload transaction: digest mismatch
# ---------------------------------------------------------------------------


class TestReloadDigestMismatch:
    @pytest.mark.asyncio
    async def test_digest_mismatch_rejects(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        validation = _make_validation(content_digest="a" * 64)
        result = await mgr.reload(validation, expected_digest="f" * 64)

        assert result.ok is False
        assert (
            "digest" in result.message.lower() or "mismatch" in result.message.lower()
        )


# ---------------------------------------------------------------------------
# Reload transaction: full success flow
# ---------------------------------------------------------------------------


class TestReloadSuccess:
    @pytest.mark.asyncio
    async def test_success_flow(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ) as publish_mock,
        ):
            result = await mgr.reload(validation)

            assert result.ok is True
            assert result.generation == 5
            assert result.stage == ReloadStage.RETIREMENT
            assert "routing" in result.changed_sections
            publish_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_sections(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        changes = (
            MagicMock(section="routing"),
            MagicMock(section="accounts"),
        )
        diff = _make_diff(changes=changes)
        candidate = _make_candidate(generation_id=3)

        validation = _make_validation()
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.reload(validation)

            assert result.ok is True
            assert set(result.changed_sections) == {"accounts", "routing"}


# ---------------------------------------------------------------------------
# Reload transaction: failure paths
# ---------------------------------------------------------------------------


class TestReloadFailures:
    @pytest.mark.asyncio
    async def test_build_failure_rollback(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        validation = _make_validation()
        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr, "_build_candidate_generation", new_callable=AsyncMock
            ) as build_mock,
            patch.object(
                mgr, "_reconcile_persistence", new_callable=AsyncMock
            ) as reconcile_mock,
            patch.object(
                mgr, "_publish_generation", new_callable=AsyncMock
            ) as publish_mock,
        ):
            build_mock.side_effect = ReloadPreparationError("build failed")
            result = await mgr.reload(validation)

        assert result.ok is False
        assert "Reload failed" in result.message
        reconcile_mock.assert_not_called()
        publish_mock.assert_not_called()
        rm.install_candidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconciliation_failure_rollback(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate()

        validation = _make_validation()
        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr, "_reconcile_persistence", new_callable=AsyncMock
            ) as reconcile_mock,
            patch.object(
                mgr, "_publish_generation", new_callable=AsyncMock
            ) as publish_mock,
        ):
            reconcile_mock.side_effect = Exception("reconciliation failed")
            result = await mgr.reload(validation)

        assert result.ok is False
        publish_mock.assert_not_called()
        rm.install_candidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_failure_logged(self) -> None:
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate()

        validation = _make_validation()
        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(
                mgr, "_publish_generation", new_callable=AsyncMock
            ) as publish_mock,
        ):
            publish_mock.side_effect = Exception("commit failed")
            result = await mgr.reload(validation)

        assert result.ok is False
        assert "Reload failed" in result.message


# ---------------------------------------------------------------------------
# ReloadOperationState
# ---------------------------------------------------------------------------


class TestReloadOperationState:
    def test_frozen(self) -> None:
        state = ReloadOperationState(
            stage="idle",
            started_at=time.monotonic(),
            generation_id=None,
            digest_prefix="a" * 12,
        )
        with pytest.raises(AttributeError):
            state.stage = "validation"  # type: ignore[misc]

    def test_error_field(self) -> None:
        state = ReloadOperationState(
            stage="validation",
            started_at=0.0,
            generation_id=None,
            digest_prefix="x" * 12,
            error="something went wrong",
        )
        assert state.error == "something went wrong"


# ---------------------------------------------------------------------------
# ReloadOperationResult
# ---------------------------------------------------------------------------


class TestReloadOperationResult:
    def test_frozen(self) -> None:
        result = ReloadResult(
            ok=True,
            stage=ReloadStage.COMMIT,
            generation=1,
            changed_sections=(),
            warnings=(),
            restart_required=(),
            message="ok",
        )
        with pytest.raises(AttributeError):
            result.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CandidateGeneration
# ---------------------------------------------------------------------------


class TestCandidateGeneration:
    def test_fields(self) -> None:
        gen = MagicMock()
        proc = MagicMock()
        diff = MagicMock()
        candidate = CandidateGeneration(generation=gen, process=proc, diff=diff)
        assert candidate.generation is gen
        assert candidate.process is proc
        assert candidate.diff is diff
