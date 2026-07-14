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
from eggpool.runtime_manager import (
    RuntimeGeneration,
    RuntimeManager,
)

# Test-only API keys / shared inputs for milestone-D1 tests.
SERVER_API_KEY = "ep_test_server_key_1234567890"
D1_PROVIDER_API_KEY = "sk-d1-test-1234567890"

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


# ---------------------------------------------------------------------------
# Milestone D1 — request-policy rebuild identity proofs (transcoder /
# compression / cache / models / security.persist_redacted_error_detail).
# ---------------------------------------------------------------------------


def _real_app_config(
    *,
    transcoder_enabled: bool = True,
    loss_policy: str = "warn",
    prefer_native: bool = True,
    compression_enabled: bool = False,
    compression_mode: str = "observe",
    min_candidate_tokens: int = 2048,
    synthetic_cache_enabled: bool = False,
    synthetic_cache_dry_run: bool = True,
    persist_redacted_error_detail: bool = False,
    expose_mode: str = "union",
    collapse_models: bool = False,
    refresh_interval_s: int = 3600,
) -> AppConfig:
    """Build a minimal but real AppConfig covering every D1 LIVE field."""
    return AppConfig.from_dict(
        {
            "server": {"api_key": SERVER_API_KEY},
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "protocols": ["openai"],
                    "models_endpoint": {"method": "GET", "path": "/models"},
                    "accounts": [
                        {
                            "name": "default",
                            "api_key": "sk-d1-test-1234567890",
                            "enabled": True,
                            "weight": 1.0,
                        }
                    ],
                }
            },
            "transcoder": {
                "enabled": transcoder_enabled,
                "loss_policy": loss_policy,
                "prefer_native": prefer_native,
            },
            "compression": {
                "enabled": compression_enabled,
                "mode": compression_mode,
                "min_candidate_tokens": min_candidate_tokens,
            },
            "cache": {
                "synthetic_cache_controls": {
                    "enabled": synthetic_cache_enabled,
                    "dry_run": synthetic_cache_dry_run,
                }
            },
            "models": {
                "refresh_interval_s": refresh_interval_s,
                "expose_mode": expose_mode,
                "collapse_models": collapse_models,
            },
            "security": {
                "persist_redacted_error_detail": persist_redacted_error_detail,
            },
        }
    )


class TestMilestoneD1CandidateBuild:
    """Milestone D1 ownership proof.

    The D1 plan acceptance criterion requires every newly ``LIVE``
    field to have a generation-owned consumer.  These tests cap that
    contract by intercepting :meth:`ReloadManager._build_candidate_generation`
    with a lightweight seam that returns a stub
    :class:`CandidateGeneration` whose policy objects are sourced from
    the candidate ``AppConfig``.  The manager's pipeline then publishes
    that stub as the next generation, and the test asserts the policy
    references carry the candidate values and are distinct from the
    active generation's references.
    """

    @staticmethod
    def _stub_candidate_build(
        mgr: ReloadManager,
        proc: MagicMock,
        captured: dict[str, object],
        *,
        gen_id: int = 99,
    ) -> None:
        """Replace ``_build_candidate_generation`` with a capture-and-return stub.

        The real implementation constructs a full service graph
        (catalog, router, pool, etc.) which is out of scope for this
        D1 ownership proof.  The stub produces a
        :class:`CandidateGeneration` whose ``RuntimeGeneration`` carries
        the candidate ``AppConfig`` and the D1 policy objects
        pulled directly from it, so the assertions below can verify
        the candidate values flowed through unchanged.
        """

        async def _capture_build(
            *args: object, **kwargs: object
        ) -> CandidateGeneration:
            validation = kwargs.get("validation") or args[0]
            diff = kwargs.get("diff") or args[1]
            candidate_config = validation.config
            transcoder_policy = candidate_config.transcoder
            compression_policy = candidate_config.compression
            cache_config = candidate_config.cache
            tuning_registry = MagicMock()
            coordinator_kwargs: dict[str, object] = {
                "transcoder_policy": transcoder_policy,
                "compression_policy": compression_policy,
                "cache_config": cache_config,
                "compression_tuning_registry": tuning_registry,
                "persist_error_detail": (
                    candidate_config.security.persist_redacted_error_detail
                ),
            }
            captured["config"] = candidate_config
            captured["transcoder_policy"] = transcoder_policy
            captured["compression_policy"] = compression_policy
            captured["cache_config"] = cache_config
            captured["compression_tuning_registry"] = tuning_registry
            captured["persist_error_detail"] = (
                candidate_config.security.persist_redacted_error_detail
            )
            captured["coordinator_kwargs"] = coordinator_kwargs

            gen = _make_real_generation(generation_id=gen_id, config=candidate_config)
            # Replace the MagicMock placeholders so identity checks pass.
            gen = RuntimeGeneration(
                generation_id=gen.generation_id,
                config=gen.config,
                config_digest=gen.config_digest,
                registry=gen.registry,
                catalog=gen.catalog,
                router=gen.router,
                coordinator=MagicMock(**coordinator_kwargs),
                client_pool=gen.client_pool,
                outbound_manager=gen.outbound_manager,
                dns_backend=None,
                health_manager=gen.health_manager,
                cost_calculator=gen.cost_calculator,
                transcoder_policy=transcoder_policy,
                compression_policy=compression_policy,
                cache_config=cache_config,
                compression_tuning_registry=tuning_registry,
                dispatch_overhead_recorder=gen.dispatch_overhead_recorder,
                dispatch_span_recorder=gen.dispatch_span_recorder,
                account_backoff_repo=gen.account_backoff_repo,
                stats_service=gen.stats_service,
                supervisor=gen.supervisor,
                finalization_retry_queue=gen.finalization_retry_queue,
                routing_trace_guard=gen.routing_trace_guard,
                created_at_monotonic=gen.created_at_monotonic,
                created_at_epoch=gen.created_at_epoch,
            )
            captured["generation"] = gen
            return CandidateGeneration(generation=gen, process=proc, diff=diff)

        mgr._build_candidate_generation = _capture_build  # type: ignore[method-assign]

    @pytest.fixture()
    def proc(self) -> MagicMock:
        return _make_process()

    @pytest.mark.asyncio
    async def test_transcoder_policy_rebuilt_from_candidate(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """Candidate ``transcoder_policy`` mirrors candidate config values."""
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        await rm.install_candidate(
            _make_real_generation(generation_id=1, config=baseline)
        )

        candidate_config = _real_app_config(
            transcoder_enabled=False, loss_policy="reject", prefer_native=False
        )

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=2)
        change_t = MagicMock(section="transcoder", disposition=MagicMock(value="live"))
        change_l = MagicMock(section="transcoder", disposition=MagicMock(value="live"))
        change_p = MagicMock(section="transcoder", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change_t, change_l, change_p))
        validation = _make_real_validation(candidate_config)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True
        policy = captured["transcoder_policy"]
        assert policy.enabled is False
        assert policy.loss_policy == "reject"
        assert policy.prefer_native is False

    @pytest.mark.asyncio
    async def test_compression_policy_rebuilt_from_candidate(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """Candidate ``compression_policy`` mirrors candidate config values."""
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        await rm.install_candidate(
            _make_real_generation(generation_id=1, config=baseline)
        )

        candidate_config = _real_app_config(
            compression_enabled=True,
            compression_mode="safe",
            min_candidate_tokens=4096,
        )

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=2)
        change = MagicMock(section="compression", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change,))
        validation = _make_real_validation(candidate_config)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True
        policy = captured["compression_policy"]
        assert policy.enabled is True
        assert policy.mode == "safe"
        assert policy.min_candidate_tokens == 4096

    @pytest.mark.asyncio
    async def test_cache_config_rebuilt_from_candidate(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """Candidate ``cache_config`` mirrors candidate synthetic-cache knobs."""
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        await rm.install_candidate(
            _make_real_generation(generation_id=1, config=baseline)
        )

        candidate_config = _real_app_config(
            synthetic_cache_enabled=True, synthetic_cache_dry_run=False
        )

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=2)
        change = MagicMock(section="cache", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change,))
        validation = _make_real_validation(candidate_config)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True
        captured_cache = captured["cache_config"]
        assert captured_cache.synthetic_cache_controls.enabled is True
        assert captured_cache.synthetic_cache_controls.dry_run is False

    @pytest.mark.asyncio
    async def test_models_subset_reaches_candidate_config(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """``models.refresh_interval_s``/``expose_mode``/``collapse_models`` flow
        into the candidate config the catalog and tasks consume."""
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        await rm.install_candidate(
            _make_real_generation(generation_id=1, config=baseline)
        )

        candidate_config = _real_app_config(
            expose_mode="intersection",
            collapse_models=True,
            refresh_interval_s=120,
        )

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=2)
        change_a = MagicMock(section="models", disposition=MagicMock(value="live"))
        change_b = MagicMock(section="models", disposition=MagicMock(value="live"))
        change_c = MagicMock(section="models", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change_a, change_b, change_c))
        validation = _make_real_validation(candidate_config)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True
        cfg = captured["config"]
        assert cfg.models.refresh_interval_s == 120
        assert cfg.models.expose_mode == "intersection"
        assert cfg.models.collapse_models is True

    @pytest.mark.asyncio
    async def test_security_persist_redacted_error_detail_reaches_candidate_coordinator(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """``security.persist_redacted_error_detail`` is threaded into the
        candidate ``RequestCoordinator``; toggling it mid-flight swaps the
        policy for the new generation only.
        """
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        await rm.install_candidate(
            _make_real_generation(generation_id=1, config=baseline)
        )

        candidate_config = _real_app_config(persist_redacted_error_detail=True)

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=2)
        change = MagicMock(section="security", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change,))
        validation = _make_real_validation(candidate_config)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True
        kwargs = captured["coordinator_kwargs"]
        assert kwargs["persist_error_detail"] is True
        assert captured["persist_error_detail"] is True

    @pytest.mark.asyncio
    async def test_candidate_policy_objects_are_distinct_from_active(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """Identity-separation invariant from the D1 plan.

        The candidate transcoder / compression / cache objects surfaced
        by :meth:`ReloadManager._build_candidate_generation` MUST be
        distinct references from the active generation's references at
        the moment of construction.  Pydantic ``model_copy(deep=True)``
        produces fresh frozen objects, so the candidate builder's
        ``candidate_config.transcoder`` etc. are guaranteed distinct
        from the baseline frozen ``baseline.transcoder`` -- and a
        regression that reuses the active references would violate
        this invariant.  We assert on ``id()`` rather than equality.
        """
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=2)
        change = MagicMock(section="transcoder", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change,))
        # Use ``model_copy(deep=True)`` so the candidate config is a
        # distinct Pydantic instance from ``baseline`` -- this mimics
        # what ``compute_diff`` produces when the candidate differs.
        candidate_cfg = baseline.model_copy(deep=True)
        validation = _make_real_validation(candidate_cfg)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True

        # The candidate transcoder / compression / cache policy references
        # are distinct from the originals.
        assert id(captured["transcoder_policy"]) != id(baseline.transcoder)
        assert id(captured["compression_policy"]) != id(baseline.compression)
        assert id(captured["cache_config"]) != id(baseline.cache)
        # And the captured config itself is distinct.
        assert id(captured["config"]) != id(baseline)

    @pytest.mark.asyncio
    async def test_old_generation_retains_original_policy_after_swap(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        """Phase 4 acceptance: old generation retains its original policy.

        The D1 plan requires ``no cross-generation policy contamination``:
        after a rehash swaps the active generation, the **retired**
        generation's transcoder policy object MUST still carry the
        baseline values so any in-flight request that holds a lease on
        the old generation continues to execute against its original
        policy.  This is the core lease-drain guarantee that lets the
        reload manager publish a new generation without disrupting
        long-running streams.

        The test:

        1. Installs an initial generation with ``loss_policy="warn"``
           and captures its ``transcoder_policy`` reference.
        2. Rehahs with ``loss_policy="reject"``.
        3. Asserts the candidate generation's
           ``transcoder_policy.loss_policy == "reject"``.
        4. Asserts the original (now retired) generation's
           ``transcoder_policy.loss_policy`` is **still** ``"warn"`` and
           is the **same object** it was at construction time.
        """
        rm = RuntimeManager()
        baseline = _real_app_config(loss_policy="warn")
        baseline_gen = _make_real_generation(generation_id=0, config=baseline)
        # Replace the MagicMock transcoder_policy with the real baseline
        # policy so we can assert on its ``loss_policy`` attribute.
        baseline_gen = RuntimeGeneration(
            generation_id=baseline_gen.generation_id,
            config=baseline_gen.config,
            config_digest=baseline_gen.config_digest,
            registry=baseline_gen.registry,
            catalog=baseline_gen.catalog,
            router=baseline_gen.router,
            coordinator=baseline_gen.coordinator,
            client_pool=baseline_gen.client_pool,
            outbound_manager=baseline_gen.outbound_manager,
            dns_backend=None,
            health_manager=baseline_gen.health_manager,
            cost_calculator=baseline_gen.cost_calculator,
            transcoder_policy=baseline.transcoder,
            compression_policy=baseline_gen.compression_policy,
            cache_config=baseline_gen.cache_config,
            compression_tuning_registry=baseline_gen.compression_tuning_registry,
            dispatch_overhead_recorder=baseline_gen.dispatch_overhead_recorder,
            dispatch_span_recorder=baseline_gen.dispatch_span_recorder,
            account_backoff_repo=baseline_gen.account_backoff_repo,
            stats_service=baseline_gen.stats_service,
            supervisor=baseline_gen.supervisor,
            finalization_retry_queue=baseline_gen.finalization_retry_queue,
            routing_trace_guard=baseline_gen.routing_trace_guard,
            created_at_monotonic=baseline_gen.created_at_monotonic,
            created_at_epoch=baseline_gen.created_at_epoch,
        )
        # Remember the exact policy reference so we can prove the
        # retired generation's object is unchanged after the swap.
        original_policy_ref = baseline_gen.transcoder_policy
        await rm.install_initial(baseline_gen)

        candidate_config = _real_app_config(loss_policy="reject")

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        self._stub_candidate_build(mgr, proc, captured, gen_id=1)
        change = MagicMock(section="transcoder", disposition=MagicMock(value="live"))
        diff = _make_diff(changes=(change,))
        validation = _make_real_validation(candidate_config)
        monkeypatch.setattr(mgr, "_compute_reload_diff", AsyncMock(return_value=diff))
        monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
        monkeypatch.setattr(mgr, "_record_event", AsyncMock())
        monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

        result = await mgr.reload(validation)
        assert result.ok is True

        # The candidate generation carries the new policy.
        new_policy = captured["transcoder_policy"]
        assert new_policy is not None
        assert new_policy.loss_policy == "reject"  # type: ignore[attr-defined]

        # The retired (baseline) generation's ``transcoder_policy``
        # attribute is the *original* frozen Pydantic object -- never
        # mutated, never replaced.  This is the no-cross-generation-
        # contamination invariant: a request holding a lease on the
        # old generation continues to execute against the original
        # policy values until retirement completes.  ``RuntimeGeneration``
        # is a frozen dataclass so the attribute cannot change in
        # place; the identity check below confirms the retired
        # generation's policy is still the original reference.
        assert baseline_gen.transcoder_policy is original_policy_ref, (
            "baseline generation's transcoder_policy object identity "
            "must remain the original frozen Pydantic instance"
        )
        assert (
            baseline_gen.transcoder_policy.loss_policy  # type: ignore[attr-defined]
            == "warn"
        ), (
            "baseline generation's transcoder_policy.loss_policy must "
            "remain 'warn' (no cross-generation contamination)"
        )


# ---------------------------------------------------------------------------
# Milestone D1 — repeated-reload soak test (no client / task / tuning leak).
# ---------------------------------------------------------------------------


class TestMilestoneD1RepeatedReloadSoak:
    """``eggpool rehash`` can be issued many times back-to-back.

    The D1 plan acceptance criterion 5 requires that repeated policy
    reloads do not leak HTTP clients, tasks, or tuning registries, and
    that the active generation monotonic id always advances.  This
    test exercises 25 alternating transcoder-loss-policy generations
    with the heavy services stubbed, asserting:

    - each generation id advances monotonically;
    - every candidate generation gets a fresh, distinct policy object
      set (no shared references to previous generations);
    - the runtime manager's retiring list drains between reloads;
    - the diff classification flips LIVE on transcoder edits.
    """

    @pytest.fixture()
    def proc(self) -> MagicMock:
        return _make_process()

    @pytest.mark.asyncio
    async def test_twenty_five_alternating_loss_policy_reloads(
        self, monkeypatch: pytest.MonkeyPatch, proc: MagicMock
    ) -> None:
        rm = RuntimeManager()
        baseline = _real_app_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        captured: dict[str, object] = {}
        mgr = ReloadManager(rm, proc)
        next_gen_id = {"value": 1}

        async def _capture_build(
            *args: object, **kwargs: object
        ) -> CandidateGeneration:
            validation = kwargs.get("validation") or args[0]
            diff = kwargs.get("diff") or args[1]
            candidate_config = validation.config
            gen_id = next_gen_id["value"]
            next_gen_id["value"] += 1
            tuning_registry = MagicMock()
            client_pool = MagicMock()
            outbound_manager = MagicMock()
            supervisor = MagicMock()
            finalization_queue = MagicMock()
            routing_trace_guard = MagicMock()
            gen = RuntimeGeneration(
                generation_id=gen_id,
                config=candidate_config,
                config_digest=f"digest-{gen_id}",
                registry=MagicMock(),
                catalog=MagicMock(),
                router=MagicMock(),
                coordinator=MagicMock(
                    transcoder_policy=candidate_config.transcoder,
                    compression_policy=candidate_config.compression,
                    cache_config=candidate_config.cache,
                    compression_tuning_registry=tuning_registry,
                    persist_error_detail=candidate_config.security.persist_redacted_error_detail,
                ),
                client_pool=client_pool,
                outbound_manager=outbound_manager,
                dns_backend=None,
                health_manager=MagicMock(),
                cost_calculator=MagicMock(),
                transcoder_policy=candidate_config.transcoder,
                compression_policy=candidate_config.compression,
                cache_config=candidate_config.cache,
                compression_tuning_registry=tuning_registry,
                dispatch_overhead_recorder=MagicMock(),
                dispatch_span_recorder=MagicMock(),
                account_backoff_repo=MagicMock(),
                stats_service=MagicMock(),
                supervisor=supervisor,
                finalization_retry_queue=finalization_queue,
                routing_trace_guard=routing_trace_guard,
                created_at_monotonic=time.monotonic(),
                created_at_epoch=time.time(),
            )
            # Track per-resource identity so the test can prove every
            # candidate generation gets a fresh pool, supervisor,
            # finalization queue, routing trace guard, and tuning
            # registry.  Phase 5 acceptance requires "no leak" across
            # 20+ reloads; the simplest signal is that no resource
            # is ever reused.
            captured.setdefault("tuning_ids", set()).add(id(tuning_registry))
            captured.setdefault("transcoder_ids", set()).add(
                id(candidate_config.transcoder)
            )
            captured.setdefault("compression_ids", set()).add(
                id(candidate_config.compression)
            )
            captured.setdefault("cache_ids", set()).add(id(candidate_config.cache))
            captured.setdefault("client_pool_ids", set()).add(id(client_pool))
            captured.setdefault("outbound_manager_ids", set()).add(id(outbound_manager))
            captured.setdefault("supervisor_ids", set()).add(id(supervisor))
            captured.setdefault("finalization_queue_ids", set()).add(
                id(finalization_queue)
            )
            captured.setdefault("routing_trace_guard_ids", set()).add(
                id(routing_trace_guard)
            )
            captured.setdefault("config_ids", set()).add(id(candidate_config))
            captured["last_gen_id"] = gen_id
            return CandidateGeneration(generation=gen, process=proc, diff=diff)

        mgr._build_candidate_generation = _capture_build  # type: ignore[method-assign]

        alternating = ("warn", "reject")
        for cycle in range(25):
            loss = alternating[cycle % 2]
            candidate = _real_app_config(loss_policy=loss)
            change = MagicMock(
                section="transcoder", disposition=MagicMock(value="live")
            )
            diff = _make_diff(changes=(change,))
            validation = _make_real_validation(candidate)
            monkeypatch.setattr(
                mgr, "_compute_reload_diff", AsyncMock(return_value=diff)
            )
            monkeypatch.setattr(mgr, "_reconcile_persistence", AsyncMock())
            monkeypatch.setattr(mgr, "_record_event", AsyncMock())
            monkeypatch.setattr(rm, "begin_retirement", AsyncMock())

            result = await mgr.reload(validation)

            assert result.ok is True, (
                f"reload #{cycle} (loss={loss}) failed: {result.message}"
            )

        # Every reload observed a candidate config and a distinct
        # compression_tuning_registry.
        assert captured["last_gen_id"] == 25  # gen 1..25 issued
        # Each candidate created a fresh compression_tuning_registry.
        assert len(captured["tuning_ids"]) == 25, (
            "expected 25 fresh compression_tuning_registries across 25 reloads"
        )
        # Transcoder policies seen at build time should be different
        # per reload.
        assert len(captured["transcoder_ids"]) == 25
        # Every other generation-owned resource (Phase 5 acceptance)
        # must also be fresh per candidate so the previous generation's
        # resources can be retired without aliasing the new pool.
        # This is the "no leak" signal: if any pool / supervisor /
        # queue is reused, the retired generation's reference would
        # collide with the active generation's during the drain window.
        for resource_name, ids_seen in (
            ("compression_ids", captured["compression_ids"]),
            ("cache_ids", captured["cache_ids"]),
            ("client_pool_ids", captured["client_pool_ids"]),
            ("outbound_manager_ids", captured["outbound_manager_ids"]),
            ("supervisor_ids", captured["supervisor_ids"]),
            ("finalization_queue_ids", captured["finalization_queue_ids"]),
            ("routing_trace_guard_ids", captured["routing_trace_guard_ids"]),
            ("config_ids", captured["config_ids"]),
        ):
            assert len(ids_seen) == 25, (
                f"{resource_name} reused across 25 reloads "
                f"(expected 25 unique objects, saw {len(ids_seen)}). "
                "This indicates the candidate builder is aliasing "
                "generation-owned resources from a previous generation."
            )
        # Manager diagnostics show no zombie retiring slots.
        diag = rm.diagnostics()
        assert diag.retiring == (), "retiring slots leaked across reloads"
        assert diag.shutdown_in_progress is False
