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
