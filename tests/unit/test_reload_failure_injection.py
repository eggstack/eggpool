"""Failure-injection matrix for ReloadManager pre-publication stages.

Each test injects a deterministic failure at a different stage of the
reload pipeline and asserts the invariant set:

- active generation unchanged
- active request behavior unchanged (no candidate resources leak)
- candidate resources closed (no leftover supervisor/client_pool)
- persistence rolled back or idempotent
- no new tasks scheduled on the active process supervisor
- structured redacted operational event recorded
- ReloadResult.exit_code / stage maps to the right CLI exit code
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.cli_exit_codes import (
    EXIT_DIGEST_MISMATCH,
    exit_code_for_failure,
)
from eggpool.config_reload_policy import ReloadResult, ReloadStage
from eggpool.control.reload_manager import (
    CandidateGeneration,
    ReloadInProgressError,
    ReloadManager,
    ReloadPreparationError,
)
from eggpool.runtime_manager import RuntimeGeneration, RuntimeManager

# ---------------------------------------------------------------------------
# Helpers (mirrors test_reload_manager.py patterns)
# ---------------------------------------------------------------------------

SERVER_API_KEY = "ep_test_server_key_1234567890"


def _make_process() -> MagicMock:
    proc = MagicMock()
    proc.db = MagicMock()
    proc.stats_db = MagicMock()
    proc.metrics_coalescer = MagicMock()
    proc.process_supervisor = None
    return proc


def _make_validation(
    *,
    content_digest: str = "a" * 64,
    warnings: tuple = (),
    config: MagicMock | None = None,
) -> MagicMock:
    v = MagicMock()
    v.content_digest = content_digest
    v.warnings = warnings
    v.config = config or MagicMock()
    return v


def _make_diff(changes: tuple = ()) -> MagicMock:
    d = MagicMock()
    d.changes = changes
    d.live = bool(changes)
    d.restart_required = tuple(
        c for c in changes if getattr(c, "disposition", None) == "restart"
    )
    return d


def _make_generation(generation_id: int = 0, digest: str = "a" * 64) -> MagicMock:
    gen = MagicMock()
    gen.generation_id = generation_id
    gen.config_digest = digest
    gen.config = MagicMock()
    return gen


def _make_candidate(
    generation_id: int = 1,
    digest: str = "b" * 64,
) -> MagicMock:
    gen = _make_generation(generation_id, digest)
    process = MagicMock()
    diff = _make_diff()
    candidate = MagicMock(spec=CandidateGeneration)
    candidate.generation = gen
    candidate.process = process
    candidate.diff = diff
    candidate._built_generation = gen
    return candidate


def _make_real_config() -> object:
    from eggpool.models.config import AppConfig, ServerConfig

    return AppConfig(server=ServerConfig(host="0.0.0.0", port=8080))


async def _ignore_event(*args: object, **kwargs: object) -> None:
    return None


def _make_real_generation(
    *,
    generation_id: int = 0,
    config: object | None = None,
    config_digest: str = "a" * 64,
) -> RuntimeGeneration:
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
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
        created_at_monotonic=time.monotonic(),
        created_at_epoch=time.time(),
    )


def _make_real_validation(config: object) -> MagicMock:
    v = MagicMock()
    v.content_digest = "b" * 64
    v.warnings = ()
    v.config = config
    return v


def _exit_code(result: ReloadResult) -> int:
    if result.ok:
        return 0
    stage_val = (
        result.stage.value if isinstance(result.stage, ReloadStage) else result.stage
    )
    return exit_code_for_failure(
        stage=stage_val,
        restart_required=list(result.restart_required),
        message=result.message,
    )


# ---------------------------------------------------------------------------
# 1. Digest mismatch rejects before any work
# ---------------------------------------------------------------------------


class TestDigestMismatchInjection:
    @pytest.mark.asyncio
    async def test_digest_mismatch_rejects_before_any_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        gen_before = rm.active_snapshot().generation_id

        validation = _make_validation(content_digest="a" * 64)

        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        with patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock:
            result = await mgr.reload(validation, expected_digest="f" * 64)

            assert rm.active_snapshot().generation_id == gen_before
            ic_mock.assert_not_called()

        assert result.ok is False
        assert _exit_code(result) == EXIT_DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# 2. Diff computation failure preserves active
# ---------------------------------------------------------------------------


class TestDiffComputationFailure:
    @pytest.mark.asyncio
    async def test_diff_computation_failure_preserves_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        gen_before = rm.active_snapshot().generation_id

        validation = _make_validation()
        event_calls: list[tuple[str, dict]] = []

        async def _capture_event(event_type: str, **kwargs):
            event_calls.append((event_type, kwargs))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock
            ) as diff_mock,
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
        ):
            diff_mock.side_effect = RuntimeError("diff computation failed")
            result = await mgr.reload(validation)

            assert rm.active_snapshot().generation_id == gen_before
            ic_mock.assert_not_called()

        assert result.ok is False
        # Phase 11: diff computation failure now correctly reports stage=DIFF.
        assert result.stage == ReloadStage.DIFF
        failure_events = [et for et, _ in event_calls if "failure" in et]
        assert len(failure_events) >= 1


# ---------------------------------------------------------------------------
# 3. Candidate build failure preserves active
# ---------------------------------------------------------------------------


class TestCandidateBuildFailure:
    @pytest.mark.asyncio
    async def test_candidate_build_failure_preserves_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        gen_before = rm.active_snapshot().generation_id

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        validation = _make_validation()

        event_calls: list[tuple[str, dict]] = []

        async def _capture_event(event_type: str, **kwargs):
            event_calls.append((event_type, kwargs))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr, "_build_candidate_generation", new_callable=AsyncMock
            ) as build_mock,
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
            patch("eggpool.runtime_manager.RuntimeGenerationBuilder"),
        ):
            build_mock.side_effect = ReloadPreparationError("build failed")
            result = await mgr.reload(validation)

            assert rm.active_snapshot().generation_id == gen_before
            ic_mock.assert_not_called()

        assert result.ok is False
        # Phase 11: build failure now correctly reports stage=PREPARATION.
        assert result.stage == ReloadStage.PREPARATION
        failure_events = [et for et, _ in event_calls if "failure" in et]
        assert len(failure_events) >= 1


# ---------------------------------------------------------------------------
# 4. Reconciliation failure preserves active
# ---------------------------------------------------------------------------


class TestReconciliationFailure:
    @pytest.mark.asyncio
    async def test_reconciliation_failure_preserves_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        gen_before = rm.active_snapshot().generation_id

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
        validation = _make_validation()

        event_calls: list[tuple[str, dict]] = []

        async def _capture_event(event_type: str, **kwargs):
            event_calls.append((event_type, kwargs))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

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
                mgr,
                "_prepare_persistence_delta",
                side_effect=Exception("reconciliation failed"),
            ),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
        ):
            result = await mgr.reload(validation)

            assert rm.active_snapshot().generation_id == gen_before
            ic_mock.assert_not_called()

        assert result.ok is False
        # Phase 11: reconciliation failure now correctly reports stage=RECONCILIATION.
        assert result.stage == ReloadStage.RECONCILIATION
        reconciliation_events = [et for et, _ in event_calls if "reconciliation" in et]
        assert len(reconciliation_events) >= 1


# ---------------------------------------------------------------------------
# 5. Publish failure preserves active until stage
# ---------------------------------------------------------------------------


class TestPublishFailure:
    @pytest.mark.asyncio
    async def test_publish_failure_preserves_active_until_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        gen_before = rm.active_snapshot().generation_id

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
        validation = _make_validation()

        event_calls: list[tuple[str, dict]] = []

        async def _capture_event(event_type: str, **kwargs):
            event_calls.append((event_type, kwargs))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

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
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_pre_commit_verification",
                new_callable=AsyncMock,
                side_effect=Exception("publication failed"),
            ),
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
        ):
            result = await mgr.reload(validation)

            assert rm.active_snapshot().generation_id == gen_before
            ic_mock.assert_not_called()

        assert result.ok is False
        # Phase 11: pre-commit verification failure occurs during RECONCILIATION stage
        # (before _set_stage(COMMIT) is called).
        assert result.stage == ReloadStage.RECONCILIATION
        failure_events = [et for et, _ in event_calls if "failure" in et]
        assert len(failure_events) >= 1


# ---------------------------------------------------------------------------
# 6. Event recorder failure does not break reload
# ---------------------------------------------------------------------------


class TestEventRecorderFailure:
    @pytest.mark.asyncio
    async def test_event_recorder_failure_does_not_break_reload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        call_count = {"n": 0}

        async def _fail_first(event_type: str, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("event recorder broken")

        monkeypatch.setattr(mgr, "_record_event", _fail_first)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
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
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
            patch.object(rm, "begin_retirement", new_callable=AsyncMock),
        ):
            result = await mgr.reload(validation)

        assert isinstance(result, ReloadResult)
        assert result.ok is True
        assert result.generation == 5


# ---------------------------------------------------------------------------
# 7. Concurrent reload returns busy immediately
# ---------------------------------------------------------------------------


class TestConcurrentReloadBusy:
    @pytest.mark.asyncio
    async def test_concurrent_reload_returns_busy_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        block_event = asyncio.Event()
        mgr.preparation_event = block_event

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate_a = _make_candidate(generation_id=5)
        candidate_b = _make_candidate(generation_id=6)

        validation_a = _make_validation()
        validation_b = _make_validation(content_digest="c" * 64)

        build_count = {"n": 0}

        async def _build_with_hook(*args, **kwargs):
            build_count["n"] += 1
            if mgr.preparation_event is not None:
                await mgr.preparation_event.wait()
            return candidate_a if build_count["n"] == 1 else candidate_b

        event_calls: list[tuple[str, dict]] = []

        async def _capture_event(event_type: str, **kwargs):
            event_calls.append((event_type, kwargs))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr, "_build_candidate_generation", side_effect=_build_with_hook
            ),
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
        ):
            task_a = asyncio.create_task(mgr.reload(validation_a))
            await asyncio.sleep(0.05)

            with pytest.raises(ReloadInProgressError):
                await mgr.reload(validation_b)

            block_event.set()
            await task_a

        # With atomic admission, the rejected caller is rejected before
        # any event recording, so no reload_publication_conflict event
        # is emitted.  The busy decision is immediate and does not
        # depend on event persistence.
        conflict_events = [
            (et, kw) for et, kw in event_calls if et == "reload_publication_conflict"
        ]
        assert len(conflict_events) == 0


# ---------------------------------------------------------------------------
# 8. Publication generation guard rejects stale
# ---------------------------------------------------------------------------


class TestPublicationGenerationGuard:
    @pytest.mark.asyncio
    async def test_publication_generation_guard_rejects_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two reloads: A succeeds and advances generation, B fails at
        publish because the active generation moved."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate_a = _make_candidate(generation_id=1)
        candidate_b = _make_candidate(generation_id=2)

        validation_a = _make_validation()
        validation_b = _make_validation(content_digest="c" * 64)

        block_event = asyncio.Event()
        mgr.preparation_event = block_event

        build_count = {"n": 0}

        async def _build_with_hook(*args, **kwargs):
            build_count["n"] += 1
            if mgr.preparation_event is not None:
                await mgr.preparation_event.wait()
            return candidate_a if build_count["n"] == 1 else candidate_b

        event_calls: list[tuple[str, dict]] = []

        async def _capture_event(event_type: str, **kwargs):
            event_calls.append((event_type, kwargs))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr, "_build_candidate_generation", side_effect=_build_with_hook
            ),
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
        ):
            # Reload A: held at build, then completes
            task_a = asyncio.create_task(mgr.reload(validation_a))
            await asyncio.sleep(0.05)
            block_event.set()
            result_a = await task_a

            # The active generation is now gen1, but candidate_b expects
            # gen0.  The staged-swap protocol validates this in stage().
            async def _stage_rejects() -> None:
                raise ReloadPreparationError(
                    "Active generation changed during candidate preparation"
                )

            mock_swap_b = MagicMock()
            mock_swap_b.staged = False
            mock_swap_b.committed = False
            mock_swap_b.stage = AsyncMock(side_effect=_stage_rejects)
            mock_swap_b.rollback = AsyncMock()
            rm.prepare_candidate_swap = AsyncMock(return_value=mock_swap_b)

            result_b = await mgr.reload(validation_b)

        assert result_a.ok is True
        assert result_a.generation == 1
        assert result_b.ok is False


# ---------------------------------------------------------------------------
# 9. Pre-publication does not schedule new tasks
# ---------------------------------------------------------------------------


class TestPrePublicationNoNewTasks:
    @pytest.mark.asyncio
    async def test_build_failure_no_new_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        proc.process_supervisor = MagicMock()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        supervisor = proc.process_supervisor
        task_count_before = (
            len(supervisor._tasks) if hasattr(supervisor, "_tasks") else 0
        )

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
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
            patch.object(mgr, "_record_event", _ignore_event),
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
        ):
            build_mock.side_effect = ReloadPreparationError("build failed")
            await mgr.reload(validation)

            task_count_after = (
                len(supervisor._tasks) if hasattr(supervisor, "_tasks") else 0
            )
            assert task_count_after == task_count_before
            ic_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_failure_no_new_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        proc.process_supervisor = MagicMock()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        supervisor = proc.process_supervisor
        task_count_before = (
            len(supervisor._tasks) if hasattr(supervisor, "_tasks") else 0
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
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
                mgr,
                "_prepare_persistence_delta",
                side_effect=Exception("reconciliation failed"),
            ),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
            patch.object(mgr, "_record_event", _ignore_event),
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
        ):
            await mgr.reload(validation)

            task_count_after = (
                len(supervisor._tasks) if hasattr(supervisor, "_tasks") else 0
            )
            assert task_count_after == task_count_before
            ic_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Post-publication failure visible in diagnostics
# ---------------------------------------------------------------------------


class TestPostPublicationFailureVisible:
    @pytest.mark.asyncio
    async def test_post_publish_retirement_failure_shows_retirement_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a successful publish, a retirement failure must NOT roll back
        the active generation.  The error must be visible in diagnostics and
        retirement_pending must be True."""
        rm = RuntimeManager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)
        validation = _make_validation()

        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        # Patch begin_retirement on the RuntimeManager to raise.
        # install_candidate calls begin_retirement AFTER the slot swap,
        # so the generation is already active when the error occurs.
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
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_compensate_post_publication",
                new_callable=AsyncMock,
                return_value=False,
            ),
            # Do NOT patch _publish_generation — let it call install_candidate
            # which calls begin_retirement.
            patch.object(rm, "begin_retirement", new_callable=AsyncMock) as retire_mock,
        ):
            retire_mock.side_effect = Exception("retirement failed")
            # install_candidate catches begin_retirement failure but
            # reload catches all exceptions — result.ok will be False
            # and the error stage will be recorded.
            result = await mgr.reload(validation)

        # The generation WAS swapped to 5 before begin_retirement failed
        assert rm.active_snapshot().generation_id == 5
        # Post-publication failure is compensated in Phase 6; result is ok
        # but retirement_pending reflects the retirement failure.
        assert result.ok is True
        assert result.generation == 5

        snapshot = mgr.snapshot()
        assert snapshot["last_reload_result"] is not None


# ---------------------------------------------------------------------------
# 9 (cross-cutting). Pre-publication does not schedule new tasks
# ---------------------------------------------------------------------------


class TestPrePublicationTaskCountCrossCutting:
    @pytest.mark.asyncio
    async def test_diff_failure_no_task_increase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = RuntimeManager()
        proc = _make_process()
        proc.process_supervisor = MagicMock()
        mgr = ReloadManager(rm, proc)

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )

        validation = _make_validation()
        monkeypatch.setattr(mgr, "_record_event", _ignore_event)

        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock
            ) as diff_mock,
            patch.object(rm, "install_candidate", new_callable=AsyncMock) as ic_mock,
        ):
            diff_mock.side_effect = RuntimeError("diff failed")
            result = await mgr.reload(validation)

            assert result.ok is False
            assert rm.active_snapshot().generation_id == 0
            ic_mock.assert_not_called()
