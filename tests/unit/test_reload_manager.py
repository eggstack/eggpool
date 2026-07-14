"""Tests for the ReloadManager transaction flow."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.config_reload_policy import (
    ConfigChange,
    ConfigDiff,
    ReloadDisposition,
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
from eggpool.models.config import AppConfig, ServerConfig
from eggpool.runtime_manager import RuntimeGeneration, RuntimeManager

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
    """Concurrency-guard tests for ReloadManager.

    ``test_rejects_concurrent`` uses a ``patch.object`` side-effect on
    ``_build_candidate_generation`` to block reload A while reload B
    attempts to enter.  The alternative ``preparation_event`` hook on the
    manager (see ``TestDeterministicConcurrency``) is a lighter-weight
    approach that avoids patching internals.
    """

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


# ---------------------------------------------------------------------------
# Helpers for critical reload-manager tests
# ---------------------------------------------------------------------------


def _make_real_config(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> AppConfig:
    """Build a real AppConfig with the given server settings."""
    return AppConfig(server=ServerConfig(host=host, port=port))


def _make_real_generation(
    *,
    generation_id: int = 0,
    config: AppConfig | None = None,
    config_digest: str = "a" * 64,
) -> RuntimeGeneration:
    """Build a real RuntimeGeneration with mock services but real config."""
    if config is None:
        config = _make_real_config()
    return RuntimeGeneration(
        generation_id=generation_id,
        config=config,
        config_digest=config_digest,
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        dns_backend=None,
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        compression_policy=MagicMock(),
        cache_config=MagicMock(),
        compression_tuning_registry=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        finalization_retry_queue=MagicMock(),
        routing_trace_guard=MagicMock(),
        created_at_monotonic=time.monotonic(),
        created_at_epoch=time.time(),
    )


def _make_real_validation(config: AppConfig) -> MagicMock:
    """Build a mock ConfigValidationResult wrapping a real config."""
    v = MagicMock()
    v.content_digest = "b" * 64
    v.warnings = ()
    v.config = config
    return v


# ---------------------------------------------------------------------------
# Critical tests: restart-required field rejection through ReloadManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_required_host_change_rejects_entire_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host change (RESTART_REQUIRED) must reject the entire reload,
    not apply a subset of changes."""
    rm = RuntimeManager()
    proc = _make_process()
    mgr = ReloadManager(rm, proc)

    baseline = _make_real_config(host="0.0.0.0", port=8080)
    gen = _make_real_generation(generation_id=0, config=baseline)
    await rm.install_initial(gen)

    candidate_config = _make_real_config(host="127.0.0.1", port=8080)
    validation = _make_real_validation(candidate_config)

    monkeypatch.setattr(mgr, "_record_event", AsyncMock())

    result = await mgr.reload(validation)

    assert result.ok is False
    assert len(result.restart_required) > 0
    assert rm.active_snapshot().generation_id == 0


# ---------------------------------------------------------------------------
# Critical tests: ignored-only changes return success without candidate build
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignored_only_changes_return_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all changes are IGNORED (no LIVE, no RESTART_REQUIRED),
    the reload returns success without building a candidate."""
    rm = RuntimeManager()
    proc = _make_process()
    mgr = ReloadManager(rm, proc)

    baseline = _make_real_config()
    gen = _make_real_generation(generation_id=0, config=baseline)
    await rm.install_initial(gen)

    candidate_config = _make_real_config()
    validation = _make_real_validation(candidate_config)

    ignored_change = ConfigChange(
        path="models.refresh_interval_s",
        disposition=ReloadDisposition.IGNORED,
        old_display="300",
        new_display="600",
        section="models",
    )
    mock_diff = ConfigDiff(changes=(ignored_change,))

    monkeypatch.setattr(
        "eggpool.control.reload_manager.compute_diff",
        lambda _active, _candidate: mock_diff,
    )
    monkeypatch.setattr(mgr, "_record_event", AsyncMock())

    result = await mgr.reload(validation)

    assert result.ok is True
    assert rm.active_snapshot().generation_id == 0


# ---------------------------------------------------------------------------
# Critical tests: publication guard rejects concurrent installs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publication_guard_rejects_generation_mismatch() -> None:
    """install_candidate rejects when active generation changed during prep."""
    rm = RuntimeManager()

    gen1 = _make_real_generation(generation_id=0)
    await rm.install_initial(gen1)

    gen2 = _make_real_generation(generation_id=1)
    await rm.install_candidate(gen2)

    gen3 = _make_real_generation(generation_id=2)
    with pytest.raises(RuntimeError, match="Active generation changed"):
        await rm.install_candidate(gen3, expected_active_generation_id=0)


# ---------------------------------------------------------------------------
# Critical tests: diagnostic snapshot includes restart_required and warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_includes_restart_required_and_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reload manager snapshot includes restart_required and warnings."""
    rm = RuntimeManager()
    proc = _make_process()
    mgr = ReloadManager(rm, proc)

    snapshot = mgr.snapshot()
    assert snapshot["last_reload_result"] is None

    baseline = _make_real_config(host="0.0.0.0")
    gen = _make_real_generation(generation_id=0, config=baseline)
    await rm.install_initial(gen)

    candidate_config = _make_real_config(host="127.0.0.1")
    validation = _make_real_validation(candidate_config)

    monkeypatch.setattr(mgr, "_record_event", AsyncMock())

    await mgr.reload(validation)

    snapshot = mgr.snapshot()
    result = snapshot["last_reload_result"]
    assert result is not None
    assert result["ok"] is False
    assert len(result["restart_required"]) > 0


# ---------------------------------------------------------------------------
# Deterministic concurrency tests (preparation_event hook)
# ---------------------------------------------------------------------------


class TestDeterministicConcurrency:
    """Proves concurrent reload B is rejected while reload A holds inside
    candidate preparation, using the ``preparation_event`` test hook."""

    @pytest.mark.asyncio
    async def test_deterministic_busy_when_reload_holds_preparation(
        self,
    ) -> None:
        """Reload A holds inside _build_candidate_generation via
        preparation_event; reload B gets ReloadInProgressError."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        block_event = asyncio.Event()
        mgr.preparation_event = block_event

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        async def _build_with_hook(
            *args: object, **kwargs: object
        ) -> CandidateGeneration:
            if mgr.preparation_event is not None:
                await mgr.preparation_event.wait()
            return candidate

        validation_a = _make_validation()
        validation_b = _make_validation(content_digest="c" * 64)

        event_calls: list[tuple[str, dict[str, object]]] = []

        async def _capture_event(event_type: str, **kwargs: object) -> None:
            event_calls.append((event_type, kwargs))

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
                side_effect=_build_with_hook,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_record_event", side_effect=_capture_event),
        ):
            task_a = asyncio.create_task(mgr.reload(validation_a))
            await asyncio.sleep(0.05)

            with pytest.raises(ReloadInProgressError):
                await mgr.reload(validation_b)

            block_event.set()
            result_a = await task_a

        assert result_a.ok is True
        assert result_a.generation == 5
        assert rm.install_candidate.await_count == 1

        conflict_events = [
            (et, kw) for et, kw in event_calls if et == "reload_publication_conflict"
        ]
        assert len(conflict_events) == 1

    @pytest.mark.asyncio
    async def test_recorded_event_does_not_leak_secrets(self) -> None:
        """Operational events must not contain secret-shaped values."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        block_event = asyncio.Event()
        mgr.preparation_event = block_event

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        async def _build_with_hook(
            *args: object, **kwargs: object
        ) -> CandidateGeneration:
            if mgr.preparation_event is not None:
                await mgr.preparation_event.wait()
            return candidate

        validation_a = _make_validation()
        validation_b = _make_validation(content_digest="c" * 64)

        event_calls: list[tuple[str, dict[str, object]]] = []

        async def _capture_event(event_type: str, **kwargs: object) -> None:
            event_calls.append((event_type, kwargs))

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
                side_effect=_build_with_hook,
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
            patch.object(mgr, "_record_event", side_effect=_capture_event),
        ):
            task_a = asyncio.create_task(mgr.reload(validation_a))
            await asyncio.sleep(0.05)

            with pytest.raises(ReloadInProgressError):
                await mgr.reload(validation_b)

            block_event.set()
            await task_a

        valid_event_types = {
            "reload_publication_conflict",
            "reload_requested",
            "reload_activated",
            "reload_preparation_failure",
            "reload_restart_required_rejected",
            "reload_digest_mismatch",
            "reload_reconciliation_failure",
        }

        for event_type, kwargs in event_calls:
            assert event_type in valid_event_types, (
                f"unexpected event type {event_type!r}"
            )
            for key, value in kwargs.items():
                if not isinstance(value, str):
                    continue
                lower = value.lower()
                assert not lower.startswith("sk-"), (
                    f"event {event_type!r} kwarg {key!r} leaks a secret (sk- prefix)"
                )
                assert "api_key" not in key.lower(), (
                    f"event {event_type!r} kwarg {key!r} may carry a secret"
                )
                assert not (
                    len(value) >= 20
                    and any(c.isdigit() for c in value)
                    and any(c.isalpha() for c in value)
                ), (
                    f"event {event_type!r} kwarg {key!r} looks like a token: "
                    f"{value[:8]}…"
                )


# ---------------------------------------------------------------------------
# Stale expected-generation guard (independent of the reload lock)
# ---------------------------------------------------------------------------


class TestStaleExpectedGenerationGuard:
    """The expected-generation guard runs inside RuntimeManager.install_candidate
    and is independent of the ReloadManager's reload lock."""

    @pytest.mark.asyncio
    async def test_stale_expected_generation_guard(self) -> None:
        """Install gen 0, then gen 1; building gen 2 with expected=0 raises."""
        rm = RuntimeManager()

        gen0 = _make_real_generation(generation_id=0)
        await rm.install_initial(gen0)

        gen1 = _make_real_generation(generation_id=1)
        await rm.install_candidate(gen1)

        gen2 = _make_real_generation(generation_id=2)
        with pytest.raises(RuntimeError, match="Active generation changed"):
            await rm.install_candidate(gen2, expected_active_generation_id=0)

    @pytest.mark.asyncio
    async def test_lock_check_fires_before_digest_check(self) -> None:
        """When the lock is held, ReloadInProgressError is raised
        regardless of digest validity, proving the lock guard runs first."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        await mgr._reload_lock.acquire()

        validation_matching = _make_validation(content_digest="a" * 64)
        with pytest.raises(ReloadInProgressError):
            await mgr.reload(validation_matching, expected_digest="a" * 64)

        mgr._reload_lock.release()

        validation_mismatch = _make_validation(content_digest="a" * 64)
        result = await mgr.reload(validation_mismatch, expected_digest="f" * 64)
        assert result.ok is False
        assert (
            "digest" in result.message.lower() or "mismatch" in result.message.lower()
        )
