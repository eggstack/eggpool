# Plan 019 Exact-Head Evidence

Date: 2026-07-24
Commit: c06f01997f838db69fb82d165b9972dc55da82dd (HEAD)
Python: 3.11, 3.12 (CI matrix)

## Focused lifecycle suite (Plan 019 tests)

Command: `uv run pytest tests/unit/test_accepted_finalization_state_machine.py tests/integration/reload/test_plan_019_*.py -q --tb=short`
Result: 50 passed, 0 failed
Runs: 3/3 consistent

## CI suite (Plan 018/019 combined)

Command: `uv run pytest tests/unit/test_runtime_manager.py tests/unit/test_process_transition_plan.py tests/unit/test_reload_manager.py tests/unit/test_reload_diagnostics_matrix.py tests/unit/test_accepted_finalization_state_machine.py tests/integration/reload/test_plan_017_lease_condition.py tests/integration/reload/test_plan_018_*.py tests/integration/reload/test_plan_019_*.py tests/integration/reload/test_pending_swap_visibility.py tests/integration/reload/test_diagnostics_matrix.py -q --tb=short`
Result: 298 passed, 0 failed

## Lint

- `ruff format --check`: 652 files clean
- `ruff check`: 0 violations
- `pyright src/ scripts/`: 0 errors, 0 warnings

## Skip/xfail audit

- `python scripts/audit_xfail_skips.py`: OK

## New test files created

1. `tests/unit/test_accepted_finalization_state_machine.py` — 23 tests
2. `tests/integration/reload/test_plan_019_finalization_retry.py` — 3 tests
3. `tests/integration/reload/test_plan_019_finalization_retention.py` — 4 tests
4. `tests/integration/reload/test_plan_019_shutdown_drain.py` — 4 tests
5. `tests/integration/reload/test_plan_019_acceptance_boundary.py` — 4 tests
6. `tests/integration/reload/test_plan_019_database_invalidation.py` — 7 tests
7. `tests/integration/reload/test_plan_019_transition_prefix.py` — 3 tests

## Plan 019 workstream coverage

| Workstream | Code | Tests | Closure gate |
|---|---|---|---|
| A (state machine) | Done | 23 unit tests | All invariants evidenced |
| B (transition finalization) | Done | 3 integration tests | Fail-once production test passes |
| C (registry/references) | Done | 4 integration tests | 100-reload retention bounded |
| D (retirement) | Done | 2 existing tests | Retry and exact-gen proven |
| E (shutdown) | Done | 4 integration tests | Drain and timeout tested |
| F (acceptance boundary) | Done | 4 integration tests | Guard and post-accept proven |
| G (diagnostics) | Done | existing tests | Counters and status proven |
| H (deterministic tests) | Done | 10 tests | Indeterminate, ownership, A/B/C |
| I (CI/evidence) | Done | this file | CI updated, evidence archived |
