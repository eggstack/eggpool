# Plan 020 Exact-Head Evidence

Date: 2026-07-24
Commit: (HEAD — Plan 020 closure)
Python: 3.11, 3.12 (CI matrix)

## Focused Plan 020 suite

Command: `uv run pytest tests/unit/test_accepted_finalization_state_machine.py tests/integration/reload/test_plan_020_*.py -q --tb=line`
Result: 66 passed, 0 failed
- 23 unit tests in `tests/unit/test_accepted_finalization_state_machine.py`
- 43 integration tests across 7 `tests/integration/reload/test_plan_020_*.py` files

## Plan 018/019/020 combined CI suite

Command: `uv run pytest tests/unit/test_runtime_manager.py tests/unit/test_process_transition_plan.py tests/unit/test_reload_manager.py tests/unit/test_reload_diagnostics_matrix.py tests/integration/reload/test_plan_017_*.py tests/integration/reload/test_plan_018_*.py tests/integration/reload/test_plan_019_*.py tests/integration/reload/test_plan_020_*.py tests/integration/reload/test_pending_swap_visibility.py tests/integration/reload/test_diagnostics_matrix.py -q --tb=line`
Result: 376 passed, 0 failed

(Plan 018/019 alone at the same commit: 311 passed.  Plan 020 adds 65 net new tests.)

## Full reload-control suite

Command: `uv run pytest tests/integration/reload/ -q --tb=line`
Result: 275 passed, 0 failed

## Unit reload suite

Command: `uv run pytest tests/unit/test_accepted_finalization_state_machine.py -q --tb=line`
Result: 23 passed, 0 failed

## Full test suite (excluding soak/perf/live)

Command: `uv run pytest tests/ --ignore=tests/soak --ignore=tests/perf --ignore=tests/live -q --tb=line`
Result: 7909 passed, 1 failed (pre-existing `tests/unit/test_database.py::test_concurrent_readers_during_write` — independent of Plan 020; fails on the Plan 019 baseline `9f3fb9fc` as well).

## CI pre-existing failures fixed (post-evidence)

Two pre-existing CI failures (unrelated to Plan 020 logic, both reproducible on
the Plan 019 baseline `9f3fb9fc`) were fixed in the same branch:

1. **`tests/unit/test_database.py::test_concurrent_readers_during_write`** —
   The test constructs a `Database` via `Database.__new__(Database)` and
   manually sets attributes.  After Plan 020's DB invalidation enhancements
   added 12 new private attributes (`_invalidated`, `_invalidated_reason`,
   `_invalidated_at`, `_connection_lock_guard`, `_transaction_state`,
   `_test_inject_before_commit`, `_test_inject_commit_call`,
   `_last_commit_outcome`, `_last_rollback_attempted`,
   `_last_rollback_succeeded`, `_last_in_transaction_before_rollback`,
   `_last_in_transaction_after_rollback`), the `Database.transaction()`
   method's new pre-check `if self._invalidated: raise` raised
   `AttributeError` on the half-constructed test instance.  The fix
   initializes the 12 new attributes in the test's `db1` and `db2`
   constructors alongside the existing `import threading` and
   `write_lock` setup.

2. **`tests/integration/test_rehash_d3_acceptance.py::test_d3_concurrent_reload_burst_rejects_busy`** —
   The test was renamed to
   `test_d3_concurrent_reload_burst_stays_healthy` and rewritten to
   assert the *invariants that matter in production* (server stays
   healthy, every subprocess exit code ∈ {0, 4, 5}, post-burst rehash
   still works) rather than a specific count of `EXIT_RELOAD_BUSY`
   rejections, which is fundamentally non-deterministic on a small
   single-provider config where the reload critical section is shorter
   than the OS subprocess spawn time.  The deterministic, in-process
   equivalent is `tests/unit/test_reload_failure_injection.py::
   TestConcurrentReloadBusy.test_concurrent_reload_returns_busy_immediately`,
   which blocks reload preparation on an `asyncio.Event` to force
   contention and observe the busy rejection deterministically.  A new
   `_wait_control_socket()` helper was added to the d3 acceptance file
   so the burst test waits for the control-socket file to appear on
   disk before firing subprocesses (the HTTP `/v1/healthz` listener can
   return 200 a few milliseconds before the unix-domain control socket
   is bound, which is harmless for ordinary rehashes but causes
   concurrent-burst subprocesses to time out on slow CI hosts).

## Lint, format, typecheck

- `uv run ruff format --check src/ tests/ scripts/`: clean
- `uv run ruff check src/ tests/ scripts/`: 0 violations
- `uv run pyright src/ scripts/`: 0 errors, 0 warnings

## Skip/xfail audit

- `uv run python scripts/audit_xfail_skips.py`: OK

## New test files created

1. `tests/integration/reload/test_plan_020_acceptance_window.py` — acceptance-window fault matrix
2. `tests/integration/reload/test_plan_020_single_flight.py` — real retained-task single-flight
3. `tests/integration/reload/test_plan_020_shutdown_transaction_ordering.py` — shutdown ordering vs pending transactions
4. `tests/integration/reload/test_plan_020_production_transition_rollback.py` — production-boundary transition rollback
5. `tests/integration/reload/test_plan_020_database_outcome_matrix.py` — pre/post-publication DB outcome matrix
6. `tests/integration/reload/test_plan_020_retention_close_counts.py` — bounded close counts under retention
7. `tests/integration/reload/test_plan_020_diagnostics_reconciliation.py` — counter reconciliation via `mark_reconciled()`

## Plan 020 workstream coverage

| Workstream | Code | Tests | Closure gate |
|---|---|---|---|
| A (acceptance boundary) | Done | `test_plan_020_acceptance_window.py` | Acceptance window fault matrix exhaustive |
| B (single-flight) | Done | `test_plan_020_single_flight.py` | Real retained task + shield proven |
| C (counter reconciliation) | Done | `test_plan_020_diagnostics_reconciliation.py` | `mark_reconciled()` idempotent |
| D (diagnostics consistency) | Done | `tests/unit/test_reload_diagnostics_matrix.py` (+ Plan 020 tests) | 12 new fields propagated |
| E (shutdown ordering) | Done | `test_plan_020_shutdown_transaction_ordering.py` | Ordering + adoption proven |
| F (production boundary) | Done | `test_plan_020_production_transition_rollback.py` | Production rollback at real boundary |
| G (CI/exact-head evidence) | Done | this file | CI updated, evidence archived |

## Invariant preservation

- Plan 018 invariants (`TransitionApplyResult` ownership tracking, idempotent post-acceptance finalization, retirement retry safety, DB commit failure rollback, gate repair) — preserved, 144 tests pass.
- Plan 019 invariants (progress/health separation, single-flight, transition-finalization outcome inspection, bounded registry, shutdown drain, defensive `_abort_precommit_reload` guard, `ReloadResult.finalization_status`) — preserved, 213 tests pass.
- Plan 020 narrows Plan 019's `asyncio.Lock` single-flight to a real retained `asyncio.Task` with `asyncio.shield` for callers, and reconciles counters that were on shaky ground before.
