# Plan 021 Exact-Head Evidence

Date: 2026-07-24
Evidence timestamp: 2026-07-24T23:42:58Z
Implementation commit: `13f9a5f4a4314fd8f15a355c1478b0c19b716879`
Implementation tree: `195fb2e75717d1a305a0974f345e98d4bbd2136c`
Local Python: 3.11.9 and 3.12.13
CI matrix: Python 3.11 and 3.12 (`.github/workflows/ci.yml`)

The implementation tree used for the full standard verification was committed
unchanged as the implementation commit above. No source or test files changed
between that verification and the commit; subsequent changes are documentation
only.

## Repeated lifecycle suite

Command:

```bash
uv run pytest \
  tests/unit/test_accepted_finalization_state_machine.py \
  tests/integration/reload/test_plan_019_*.py \
  tests/integration/reload/test_plan_020_*.py \
  tests/integration/reload/test_plan_021_*.py \
  -q --tb=short
```

Results on the implementation tree:

- Run 1: 114 passed, 0 failed, 12.55s
- Run 2: 114 passed, 0 failed, 12.54s
- Run 3: 114 passed, 0 failed, 12.59s

Python 3.12.13 focused matrix result: 114 passed, 0 failed, 12.12s.

## Full verification

- Reload-control suite: `uv run pytest tests/integration/reload/ -q --tb=short` — **257 passed**, 0 failed, 38.19s.
- Standard suite: `uv run pytest tests/ -m "not slow and not performance and not soak and not extended_soak and not live" -q --tb=short` — **7917 passed**, 20 skipped, 119 deselected, 0 failed, 621.69s.
- `uv run ruff format --check src/ tests/ scripts/` — clean; 667 files formatted.
- `uv run ruff check src/ tests/ scripts/` — clean.
- `uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations.
- `uv run python scripts/audit_xfail_skips.py` — OK; no non-strict xfails or unconditional skips.

The standard suite had one nondeterministic subprocess busy-test observation
on an earlier attempt (7916 passed); the isolated test passed immediately on
rerun, and the subsequent complete standard run passed with 7917 tests. The
clean result above is the closure result.

## Closure coverage

- Acceptance is structurally separated from rollback-capable cleanup and
  acceptance accounting occurs before postacceptance awaits.
- Retirement scheduling has an explicit progress cursor with distinct failure
  status and retry accounting.
- Retained finalization attempts have process-owned completion reconciliation,
  bounded scalar history, delta accounting, and collectible references.
- Shutdown preparation handles transaction timeout, adoption, release, and
  exact per-generation close counts.
- Production-boundary A/B/C transition rollback, deterministic database
  outcomes, weak-reference collection, and 100-iteration cancellation stress
  are covered by Plan 021 tests.

No code or test changes occurred after implementation commit
`13f9a5f4a4314fd8f15a355c1478b0c19b716879`; only this evidence file and the
Plan 021 status record are added afterward.
