"""Unit tests for the Milestone B SelectionClaim state machine."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from eggpool.request.selection_claim import (
    SelectionClaim,
    SelectionClaimError,
    SelectionClaimState,
    SelectionClaimTracker,
)


def _make_claim(**overrides: Any) -> SelectionClaim:
    base: dict[str, Any] = {
        "proxy_request_id": "req-1",
        "attempt_number": 1,
        "account_name": "acct-1",
        "account_id": 42,
        "provider_id": "openai",
        "model_id": "gpt-4",
        "protocol": "openai",
        "estimated_tokens": 100,
        "estimated_microdollars": 1_000,
        "selected_score": 0.5,
        "selected_tier": 1,
        "token": "token-abc",
    }
    base.update(overrides)
    return SelectionClaim(**base)


def test_state_machine_happy_path() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    assert t.state is SelectionClaimState.PLAN
    t.transition(SelectionClaimState.CLAIM)
    t.transition(SelectionClaimState.PERSIST)
    t.mark_persisted(db_request_id="dbreq", attempt_id=7, reservation_id="res")
    assert t.state is SelectionClaimState.COMMITTED
    assert t.committed_db_request_id == "dbreq"
    assert t.committed_attempt_id == 7
    assert t.committed_reservation_id == "res"
    t.mark_published(active_count_increased=True, quota_reservation_added=True)
    assert t.state is SelectionClaimState.PUBLISHED
    assert t.active_count_published is True
    assert t.quota_reservation_published is True


def test_transition_is_idempotent_on_same_target() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.transition(SelectionClaimState.CLAIM)
    t.transition(SelectionClaimState.CLAIM)  # second call: no-op
    assert t.state is SelectionClaimState.CLAIM


def test_transition_skip_raises() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    with pytest.raises(SelectionClaimError):
        t.transition(SelectionClaimState.PERSIST)  # PLAN -> PERSIST is two-step


def test_rolled_back_blocks_further_transition() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.roll_back()
    assert t.state is SelectionClaimState.ROLLED_BACK
    with pytest.raises(SelectionClaimError):
        t.transition(SelectionClaimState.CLAIM)


def test_roll_back_idempotent() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.roll_back()
    t.roll_back()  # no-op
    assert t.state is SelectionClaimState.ROLLED_BACK


def test_roll_back_after_published_uses_compensate() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.transition(SelectionClaimState.CLAIM)
    t.transition(SelectionClaimState.PERSIST)
    t.mark_persisted(db_request_id="d", attempt_id=1, reservation_id="r")
    t.mark_published(active_count_increased=True, quota_reservation_added=True)
    with pytest.raises(SelectionClaimError):
        t.roll_back()  # must use compensate_post_commit
    t.compensate_post_commit()
    assert t.state is SelectionClaimState.ROLLED_BACK


def test_compensate_post_commit_idempotent() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.transition(SelectionClaimState.CLAIM)
    t.transition(SelectionClaimState.PERSIST)
    t.mark_persisted(db_request_id="d", attempt_id=1, reservation_id="r")
    t.compensate_post_commit()
    t.compensate_post_commit()  # no-op
    assert t.state is SelectionClaimState.ROLLED_BACK


def test_mark_persisted_twice_with_same_ids_is_no_op() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.transition(SelectionClaimState.CLAIM)
    t.transition(SelectionClaimState.PERSIST)
    t.mark_persisted(db_request_id="d", attempt_id=1, reservation_id="r")
    t.mark_persisted(db_request_id="d", attempt_id=1, reservation_id="r")
    assert t.committed_db_request_id == "d"


def test_mark_persisted_with_conflicting_ids_raises() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.transition(SelectionClaimState.CLAIM)
    t.transition(SelectionClaimState.PERSIST)
    t.mark_persisted(db_request_id="d1", attempt_id=1, reservation_id="r1")
    with pytest.raises(SelectionClaimError):
        t.mark_persisted(db_request_id="d2", attempt_id=1, reservation_id="r1")


def test_release_health_slot_only_once() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    health = MagicMock()
    t.mark_health_slot_acquired()
    assert t.health_slot_acquired is True
    t.release_health_slot_if_held(health)
    assert t.health_slot_acquired is False
    assert health.release_request.call_count == 1
    t.release_health_slot_if_held(health)
    assert health.release_request.call_count == 1


def test_release_health_slot_with_no_holder_is_no_op() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    health = MagicMock()
    t.release_health_slot_if_held(health)
    assert health.release_request.call_count == 0


def test_release_health_slot_with_none_health_manager_is_safe() -> None:
    t = SelectionClaimTracker(claim=_make_claim())
    t.mark_health_slot_acquired()
    t.release_health_slot_if_held(None)
    assert t.health_slot_acquired is False


def test_add_exclusion_preserves_order() -> None:
    from eggpool.routing.router import RoutingExclusion

    t = SelectionClaimTracker(claim=_make_claim())
    t.add_exclusion(RoutingExclusion(account_name="a", reason="circuit_breaker"))
    t.add_exclusion(RoutingExclusion(account_name="b", reason="auth"))
    assert [e.account_name for e in t.exclusions] == ["a", "b"]


def test_concurrent_claim_history() -> None:
    """Many concurrent tokens serialize through tracker instances."""
    seen: set[int] = set()
    seen_lock = threading.Lock()

    def _worker(token_id: int) -> None:
        c = _make_claim(token=f"tok-{token_id}")
        t = SelectionClaimTracker(claim=c)
        assert t.state is SelectionClaimState.PLAN
        with seen_lock:
            seen.add(token_id)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(64)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(seen) == 64


def test_invalid_claim_construction_raises() -> None:
    with pytest.raises(ValueError):
        _make_claim(account_name="")


def test_invalid_token_raises() -> None:
    with pytest.raises(ValueError):
        _make_claim(token="")
