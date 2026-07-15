"""Unit tests for ``SelectionClaimDiagnostics``."""

from __future__ import annotations

import threading

from eggpool.request.selection_claim_diagnostics import (
    SelectionClaimDiagnostics,
    get_selection_claim_diagnostics,
    reset_selection_claim_diagnostics_for_tests,
    set_selection_claim_diagnostics,
)


def test_basic_counters() -> None:
    d = SelectionClaimDiagnostics()
    d.record_claim_created()
    d.record_claim_persisted()
    d.record_claim_committed()
    d.record_claim_published()
    snap = d.snapshot()
    assert snap["claims_created"] == 1
    assert snap["claims_persisted"] == 1
    assert snap["claims_committed"] == 1
    assert snap["claims_published"] == 1
    assert snap["max_concurrent_claims"] == 1


def test_rolled_back_decrements_active() -> None:
    d = SelectionClaimDiagnostics()
    d.record_claim_created()
    d.record_claim_rolled_back()
    snap = d.snapshot()
    assert snap["claims_rolled_back_before_persistence"] == 1
    assert snap["claims_published"] == 0
    assert snap["max_concurrent_claims"] == 1


def test_max_concurrent_claims_tracks_high_water() -> None:
    d = SelectionClaimDiagnostics()
    for _ in range(5):
        d.record_claim_created()
    for _ in range(5):
        d.record_claim_published()
    d.record_claim_created()
    d.record_claim_published()
    snap = d.snapshot()
    assert snap["max_concurrent_claims"] == 5


def test_lock_wait_histogram_percentiles() -> None:
    d = SelectionClaimDiagnostics(lock_overflow_threshold_ms=50.0)
    for v in [10.0, 20.0, 30.0, 40.0, 100.0]:
        d.record_claim_lock_wait(v)
    snap = d.snapshot()
    assert snap["claim_lock_wait_overflows"] == 1
    sample = snap["claim_lock_wait_recent"]
    assert sample["sample_count"] == 5
    assert sample["max_ms"] == 100.0
    assert sample["p50_ms"] is not None


def test_negative_wait_is_ignored() -> None:
    d = SelectionClaimDiagnostics()
    d.record_claim_lock_wait(-1.0)
    snap = d.snapshot()
    assert snap["claim_lock_wait_recent"]["sample_count"] == 0


def test_compensation_counters_split() -> None:
    d = SelectionClaimDiagnostics()
    d.record_compensation(success=True)
    d.record_compensation(success=False)
    d.record_compensation(success=True)
    snap = d.snapshot()
    assert snap["compensation_successes"] == 2
    assert snap["compensation_failures"] == 1


def test_ambiguous_commit_and_post_commit_publication_failure() -> None:
    d = SelectionClaimDiagnostics()
    d.record_ambiguous_commit()
    d.record_post_commit_publication_failure()
    snap = d.snapshot()
    assert snap["ambiguous_commit_reconciliations"] == 1
    assert snap["post_commit_publication_failures"] == 1


def test_singleton_round_trip() -> None:
    custom = SelectionClaimDiagnostics()
    previous = set_selection_claim_diagnostics(custom)
    try:
        assert get_selection_claim_diagnostics() is custom
    finally:
        set_selection_claim_diagnostics(previous)
    # After restoring, next get returns the singleton (None-triggered).
    singleton_a = get_selection_claim_diagnostics()
    singleton_b = get_selection_claim_diagnostics()
    assert singleton_a is singleton_b
    set_selection_claim_diagnostics(singleton_a)


def test_reset_returns_fresh_instance() -> None:
    original = set_selection_claim_diagnostics(None)
    try:
        first = reset_selection_claim_diagnostics_for_tests()
        second = reset_selection_claim_diagnostics_for_tests()
        assert first is not second
    finally:
        set_selection_claim_diagnostics(original)


def test_concurrent_record_is_thread_safe() -> None:
    d = SelectionClaimDiagnostics()

    def _worker() -> None:
        for _ in range(100):
            d.record_claim_created()
            d.record_claim_committed()
            d.record_claim_published()

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = d.snapshot()
    assert snap["claims_created"] == 800
    assert snap["claims_committed"] == 800
    assert snap["claims_published"] == 800
