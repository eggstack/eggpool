"""Process-local selection-claim diagnostics.

Milestone B instruments the new ``SelectionClaimTracker`` state machine
with bounded, lock-free counters that surface under
``/api/stats/runtime`` so operators can prove the selection critical
section no longer wraps database I/O.

Counters (all in-memory, reset on process restart):

* ``claims_created`` -- number of ``SelectionClaim`` objects the
  coordinator has minted.
* ``claims_persisted`` -- claims that entered the ``PERSIST`` state.
* ``claims_committed`` -- claims whose durable rows successfully
  committed.
* ``claims_published`` -- claims whose runtime state (active count,
  quota reservation) was successfully published.
* ``claims_rolled_back_before_persistence`` -- claims that were rolled
  back without ever entering the ``PERSIST`` state (circuit exhausted
  mid-claim, cancellation, persistence failure).
* ``ambiguous_commit_reconciliations`` -- claims whose commit outcome
  was ambiguous (exception after the SQL ``COMMIT`` boundary but
  before the row was known durable); the coordinator falls back to a
  reconciliation query.
* ``post_commit_publication_failures`` -- claims that successfully
  committed but failed during runtime publication (active count or
  quota reservation).
* ``compensation_successes`` / ``compensation_failures`` -- counters
  for the deterministic post-commit compensation path that releases
  the health slot, active count, and quota reservation when
  publication fails.
* ``claim_lock_wait_overflow`` -- number of times the narrow
  selection-claim lock wait exceeded ``claim_lock_overflow_threshold_ms``
  (default 50ms); useful as a coarse "this lock is too contended"
  signal without forcing operators to read every p95.

The class is intentionally tiny and lock-free on the hot path.  The
counters are emitted under ``selection_claims`` in
``/api/stats/runtime``.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

_HISTORY_MAX = 64
_default_diagnostics: SelectionClaimDiagnostics | None = None
_default_diagnostics_lock = threading.Lock()


class SelectionClaimDiagnostics:
    """Bounded counter package for selection-claim lifecycle events."""

    def __init__(self, *, lock_overflow_threshold_ms: float = 50.0) -> None:
        self._lock_overflow_threshold_ms = float(lock_overflow_threshold_ms)
        self._lock = threading.Lock()
        self.claims_created = 0
        self.claims_rolled_back_before_persistence = 0
        self.claims_persisted = 0
        self.claims_committed = 0
        self.claims_published = 0
        self.ambiguous_commit_reconciliations = 0
        self.post_commit_publication_failures = 0
        self.compensation_successes = 0
        self.compensation_failures = 0
        self._max_concurrent_claims = 0
        self._active_claims = 0
        self._claim_lock_wait_overflows = 0
        self._recent_lock_wait_ms: deque[float] = deque(maxlen=_HISTORY_MAX)

    def record_claim_created(self) -> None:
        with self._lock:
            self.claims_created += 1
            self._active_claims += 1
            if self._active_claims > self._max_concurrent_claims:
                self._max_concurrent_claims = self._active_claims

    def record_claim_persisted(self) -> None:
        with self._lock:
            self.claims_persisted += 1

    def record_claim_committed(self) -> None:
        with self._lock:
            self.claims_committed += 1

    def record_claim_published(self) -> None:
        with self._lock:
            self.claims_published += 1
            self._active_claims = max(0, self._active_claims - 1)

    def record_claim_rolled_back(self) -> None:
        with self._lock:
            self.claims_rolled_back_before_persistence += 1
            self._active_claims = max(0, self._active_claims - 1)

    def record_ambiguous_commit(self) -> None:
        with self._lock:
            self.ambiguous_commit_reconciliations += 1

    def record_post_commit_publication_failure(self) -> None:
        with self._lock:
            self.post_commit_publication_failures += 1

    def record_compensation(self, *, success: bool) -> None:
        with self._lock:
            if success:
                self.compensation_successes += 1
            else:
                self.compensation_failures += 1

    def record_claim_lock_wait(self, wait_ms: float) -> None:
        if wait_ms < 0:
            return
        with self._lock:
            self._recent_lock_wait_ms.append(float(wait_ms))
            if wait_ms > self._lock_overflow_threshold_ms:
                self._claim_lock_wait_overflows += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            recent = list(self._recent_lock_wait_ms)
        recent.sort()
        count = len(recent)
        if count == 0:
            p50 = p95 = p99 = max_ms = None
        else:

            def pct(p: float) -> float | None:
                idx = min(count - 1, max(0, int(round((count - 1) * p))))
                return recent[idx]

            p50 = pct(0.50)
            p95 = pct(0.95)
            p99 = pct(0.99)
            max_ms = recent[-1]
        return {
            "claims_created": self.claims_created,
            "claims_rolled_back_before_persistence": (
                self.claims_rolled_back_before_persistence
            ),
            "claims_persisted": self.claims_persisted,
            "claims_committed": self.claims_committed,
            "claims_published": self.claims_published,
            "ambiguous_commit_reconciliations": (self.ambiguous_commit_reconciliations),
            "post_commit_publication_failures": (self.post_commit_publication_failures),
            "compensation_successes": self.compensation_successes,
            "compensation_failures": self.compensation_failures,
            "max_concurrent_claims": self._max_concurrent_claims,
            "claim_lock_wait_overflows": self._claim_lock_wait_overflows,
            "claim_lock_wait_overflow_threshold_ms": (self._lock_overflow_threshold_ms),
            "claim_lock_wait_recent": {
                "sample_count": count,
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
                "max_ms": max_ms,
            },
        }


def get_selection_claim_diagnostics() -> SelectionClaimDiagnostics:
    """Return the process-wide :class:`SelectionClaimDiagnostics` instance."""
    global _default_diagnostics
    with _default_diagnostics_lock:
        if _default_diagnostics is None:
            _default_diagnostics = SelectionClaimDiagnostics()
        return _default_diagnostics


def reset_selection_claim_diagnostics_for_tests() -> SelectionClaimDiagnostics:
    """Reset the module-level singleton; for tests only."""
    global _default_diagnostics
    with _default_diagnostics_lock:
        _default_diagnostics = SelectionClaimDiagnostics()
        return _default_diagnostics


def set_selection_claim_diagnostics(
    instance: SelectionClaimDiagnostics | None,
) -> SelectionClaimDiagnostics | None:
    """Replace the process-wide singleton; primarily for tests.

    Returns the previous instance so tests can restore the default
    fixture in teardown.
    """
    global _default_diagnostics
    with _default_diagnostics_lock:
        previous = _default_diagnostics
        _default_diagnostics = instance
        return previous
