"""Tests for the prepared-swap publication protocol (C1).

The publication pipeline is split into three phases
(``_prepare_swap`` / ``_commit_publication`` / ``_finalize_retirement_handling``)
so each transition can be reasoned about and tested in isolation.
These tests verify the boundaries between the phases:

* ``_prepare_swap`` returns a ``_PreparedSwap`` record without
  mutating runtime state.
* ``_commit_publication`` raises ``ReloadCommitError`` when the
  underlying ``install_candidate`` raises.
* ``_finalize_retirement_handling`` transfers ownership and mirrors
  the new generation onto ``app.state``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.control.reload_manager import (
    CandidateGeneration,
    ReloadCommitError,
    ReloadManager,
    _PreparedSwap,
)
from eggpool.runtime_manager import RuntimeGenerationCandidate


def _make_manager() -> ReloadManager:
    return ReloadManager(
        runtime_manager=MagicMock(),
        process=MagicMock(),
    )


class TestPrepareSwap:
    def test_prepare_swap_returns_record_without_state_change(self) -> None:
        """_prepare_swap captures inputs but does NOT mutate state."""
        mgr = _make_manager()
        candidate = MagicMock()
        candidate.generation = MagicMock()
        active = MagicMock()
        active.generation_id = 7
        mgr._runtime_manager.active_snapshot = MagicMock(return_value=active)
        mgr._drain_timeout_s = 3.5

        before = vars(mgr)
        swap = mgr._prepare_swap(candidate)
        after = vars(mgr)

        assert isinstance(swap, _PreparedSwap)
        assert swap.candidate is candidate
        assert swap.generation is candidate.generation
        assert swap.active_generation_id == 7
        assert swap.drain_timeout_s == 3.5
        assert before == after, "prepare_swap must not mutate manager state"

    def test_prepare_swap_raises_when_generation_absent(self) -> None:
        """Candidate without ``.generation`` is rejected as ReloadCommitError."""
        mgr = _make_manager()
        candidate = MagicMock(spec=CandidateGeneration)
        candidate.generation = None

        with pytest.raises(ReloadCommitError, match="no generation"):
            mgr._prepare_swap(candidate)

    def test_prepare_swap_uses_private_built_generation(
        self,
    ) -> None:
        """The new RuntimeGenerationCandidate uses ``_built_generation``."""
        mgr = _make_manager()
        candidate = MagicMock()
        candidate.generation = None
        candidate._built_generation = MagicMock()
        active = MagicMock()
        active.generation_id = 11
        mgr._runtime_manager.active_snapshot = MagicMock(return_value=active)

        swap = mgr._prepare_swap(candidate)
        assert swap.generation is candidate._built_generation
        assert swap.active_generation_id == 11


class TestCommitPublication:
    @pytest.mark.asyncio
    async def test_commit_publication_calls_install_candidate(self) -> None:
        """_commit_publication invokes ``install_candidate`` with swap inputs."""
        mgr = _make_manager()
        candidate = MagicMock()
        generation = MagicMock()
        swap = _PreparedSwap(
            candidate=candidate,
            generation=generation,
            active_generation_id=42,
            drain_timeout_s=2.0,
        )

        async def _install_candidate(
            gen: object,
            *,
            drain_timeout_s: float,
            expected_active_generation_id: int,
        ) -> None:
            assert gen is generation
            assert drain_timeout_s == 2.0
            assert expected_active_generation_id == 42

        mgr._runtime_manager = MagicMock()
        mgr._runtime_manager.install_candidate = AsyncMock(
            side_effect=_install_candidate
        )

        await mgr._commit_publication(swap)
        mgr._runtime_manager.install_candidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_commit_publication_wraps_unexpected_errors(self) -> None:
        """An exception from install_candidate becomes ReloadCommitError."""
        mgr = _make_manager()
        swap = _PreparedSwap(
            candidate=MagicMock(),
            generation=MagicMock(),
            active_generation_id=0,
            drain_timeout_s=1.0,
        )
        mgr._runtime_manager = MagicMock()
        mgr._runtime_manager.install_candidate = AsyncMock(
            side_effect=RuntimeError("kaboom")
        )

        with pytest.raises(ReloadCommitError, match="Failed to publish"):
            await mgr._commit_publication(swap)


class TestFinalizeRetirementHandling:
    def test_finalize_transfers_candidate_ownership(self) -> None:
        """After publication the candidate's ``transfer_to_runtime_manager``
        method is invoked so its registered closeables are not re-closed
        by the candidate.
        """
        mgr = _make_manager()
        candidate = MagicMock(spec=RuntimeGenerationCandidate)
        candidate.transfer_to_runtime_manager = MagicMock()
        swap = _PreparedSwap(
            candidate=candidate,
            generation=MagicMock(),
            active_generation_id=0,
            drain_timeout_s=1.0,
        )
        mgr._app = None

        mgr._finalize_retirement_handling(swap)
        candidate.transfer_to_runtime_manager.assert_called_once()

    def test_finalize_silent_when_no_transfer_attr(self) -> None:
        """Old CandidateGeneration lacks transfer_to_runtime_manager; no-op."""
        mgr = _make_manager()
        candidate = MagicMock(spec=CandidateGeneration)
        if hasattr(candidate, "transfer_to_runtime_manager"):
            del candidate.transfer_to_runtime_manager
        swap = _PreparedSwap(
            candidate=candidate,
            generation=MagicMock(),
            active_generation_id=0,
            drain_timeout_s=1.0,
        )
        mgr._app = None

        # Must not raise.
        mgr._finalize_retirement_handling(swap)

    def test_finalize_mirrors_when_app_present(self) -> None:
        """When ``_app`` is set, mirror_generation_on_app_state is invoked.

        The mirror call uses a deferred import (``from eggpool.app
        import mirror_generation_on_app_state``), so we patch the
        target module's attribute rather than the function reference
        inside ``reload_manager``.
        """
        mgr = _make_manager()
        candidate = MagicMock()
        candidate.transfer_to_runtime_manager = MagicMock()
        generation = MagicMock()
        swap = _PreparedSwap(
            candidate=candidate,
            generation=generation,
            active_generation_id=0,
            drain_timeout_s=1.0,
        )
        app = MagicMock()
        mgr._app = app

        sentinel = MagicMock()
        sentinel.mirror_generation_on_app_state = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "eggpool.app.mirror_generation_on_app_state",
                sentinel.mirror_generation_on_app_state,
            )
            mgr._finalize_retirement_handling(swap)

        sentinel.mirror_generation_on_app_state.assert_called_once_with(app, generation)


class TestPublishGenerationEndToEnd:
    @pytest.mark.asyncio
    async def test_publish_generation_invokes_all_three_phases(
        self,
    ) -> None:
        """``_publish_generation`` invokes prepare / commit / finalize."""
        mgr = _make_manager()
        candidate = MagicMock()
        candidate.generation = MagicMock()
        active = MagicMock()
        active.generation_id = 1
        mgr._runtime_manager.active_snapshot = MagicMock(return_value=active)
        mgr._runtime_manager.install_candidate = AsyncMock()
        mgr._app = None

        # Spy on the three phases.
        prepare_calls: list[Any] = []
        commit_calls: list[Any] = []
        finalize_calls: list[Any] = []

        original_prepare = mgr._prepare_swap
        original_commit = mgr._commit_publication
        original_finalize = mgr._finalize_retirement_handling

        def _spy_prepare(c: object) -> Any:
            prepare_calls.append(c)
            return original_prepare(c)

        async def _spy_commit(s: Any) -> None:
            commit_calls.append(s)
            await original_commit(s)

        def _spy_finalize(s: Any) -> None:
            finalize_calls.append(s)
            original_finalize(s)

        mgr._prepare_swap = _spy_prepare  # type: ignore[method-assign]
        mgr._commit_publication = _spy_commit  # type: ignore[method-assign]
        mgr._finalize_retirement_handling = _spy_finalize  # type: ignore[method-assign]

        await mgr._publish_generation(candidate, MagicMock())

        assert prepare_calls == [candidate]
        assert len(commit_calls) == 1
        assert len(finalize_calls) == 1
        mgr._runtime_manager.install_candidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publish_failure_skips_finalize(self) -> None:
        """A failure during commit does not run finalize or mirror."""
        mgr = _make_manager()
        candidate = MagicMock()
        candidate.generation = MagicMock()
        active = MagicMock()
        active.generation_id = 1
        mgr._runtime_manager.active_snapshot = MagicMock(return_value=active)
        mgr._runtime_manager.install_candidate = AsyncMock(
            side_effect=RuntimeError("simulated commit failure")
        )
        mgr._app = None

        finalize_called = False

        def _spy_finalize(s: Any) -> None:
            nonlocal finalize_called
            finalize_called = True

        mgr._finalize_retirement_handling = _spy_finalize  # type: ignore[method-assign]

        with pytest.raises(ReloadCommitError):
            await mgr._publish_generation(candidate, MagicMock())

        assert finalize_called is False, (
            "finalize_retirement_handling must not run when commit fails"
        )
