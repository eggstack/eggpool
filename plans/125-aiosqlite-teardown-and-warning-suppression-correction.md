# Plan 125 — aiosqlite Teardown and Warning-Suppression Correction

Date: 2026-08-14
Status: completed
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Priority: P1 lifecycle/test correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Resolve the repository's globally suppressed Pytest warning involving the
`aiosqlite` connection worker thread attempting to publish a result after the
owning event loop has closed.

`pyproject.toml` currently suppresses a `PytestUnhandledThreadExceptionWarning`
for `aiosqlite` worker-thread/closed-loop behavior and points maintainers to
`bugs.md item #7`. `bugs.md` is not present at current HEAD. A global warning
suppression is therefore masking a lifecycle condition whose current ownership
is unclear and references documentation that no longer exists.

The objective is not to replace aiosqlite or redesign database concurrency. The
objective is to reproduce the race deterministically enough to identify whether
EggPool fixtures/lifespan teardown leave SQL work in flight, fix that ownership
ordering where possible, and remove or narrow the suppression.

## Governing constraints

1. Keep aiosqlite and the existing single-primary-connection architecture.
2. Do not add a DB connection pool, executor framework, custom SQLite worker,
   AnyIO abstraction, or alternative async driver.
3. Preserve task-owned transaction semantics and fail-closed commit/rollback
   ambiguity behavior.
4. Preserve one canonical event loop per `Database` instance.
5. Do not weaken teardown assertions merely to silence warnings.
6. Prefer fixing fixture/lifespan ownership over adding sleeps or retries.
7. Deterministic concurrency tests must use explicit events/barriers/tasks, not
   timing-sensitive sleeps.
8. Do not globally suppress a broader warning category.
9. If the warning proves to be an upstream aiosqlite/Python interaction that
   occurs only after all EggPool resources are correctly awaited and closed,
   retain the narrowest possible suppression with a current inline explanation
   and upstream issue/reference if available.
10. No runtime dependency, migration, CI expansion, or production telemetry.

## Workstream A — Inventory database/task ownership during tests

Inspect:

- `Database.connect()` / `disconnect()` / invalidation paths;
- aiosqlite connection worker lifetime semantics;
- app lifespan startup/teardown;
- RuntimeManager/generation retirement;
- finalization supervisor shutdown;
- metrics/coalescer/background task supervisor shutdown;
- test fixtures that create `Database` directly;
- test fixtures that run FastAPI/Granian/ASGI lifespan;
- tests that spawn child tasks inside or around transactions;
- fixture scopes and event-loop scopes under `pytest-asyncio` strict mode;
- any `asyncio.create_task()` callers whose task is not explicitly awaited or
  supervisor-owned.

Record which component is supposed to close first:

```text
request/worker tasks
 -> generation-owned finalization/background tasks
 -> process-owned background tasks
 -> DB users
 -> Database.disconnect()/aiosqlite close
 -> event loop teardown
```

Adjust this ordering only if source inspection proves it is currently wrong.

## Workstream B — Reproduce the warning without the global filter

Temporarily remove/override the filter locally and identify the smallest
repeatable reproducer.

Use targeted candidates first:

- lifespan startup then immediate teardown;
- database fixture create/use/close;
- cancellation during aiosqlite operation;
- finalization task cancellation near fixture exit;
- nested/child transaction ownership tests;
- rehash generation retirement teardown;
- tests that intentionally invalidate/fail a DB connection.

Useful diagnostic techniques are test-local only:

- `asyncio.all_tasks()` inventory before fixture exit;
- explicit Events to hold/release a DB operation;
- monkeypatch a private operation boundary to prove close ordering;
- thread enumeration around connection close;
- enabling the warning as error in the focused reproducer.

Do not add production task/thread logging solely for this issue.

## Workstream C — Classify root cause

Choose exactly one primary class based on evidence.

### Class 1 — EggPool task outlives DB/loop ownership

Examples:

- detached finalizer still awaiting SQL;
- fixture exits before supervised background task is drained;
- app lifespan closes DB before generation-owned work is joined;
- direct-test helper opens a DB but does not await disconnect.

Fix ownership at the component that creates the task/resource. Do not paper over
it in `Database.disconnect()` with arbitrary sleeps.

### Class 2 — Cancelled DB operation not fully converged before close

Ensure cancellation paths wait for connection operation/close semantics that
aiosqlite supports. Preserve cancellation propagation and transaction ambiguity
rules.

Do not swallow `CancelledError` across correctness boundaries.

### Class 3 — Test fixture/event-loop ordering defect

If production lifespan ordering is correct but a fixture tears down resources in
an impossible production order, correct the fixture and add a small assertion
that owned DB resources close before loop teardown.

### Class 4 — Confirmed upstream-only post-close publication race

Use this only after proving:

- EggPool has no unfinished DB-using tasks;
- `await connection.close()` completes according to aiosqlite API contract;
- the warning can still occur from a worker publication after loop shutdown;
- there is no practical EggPool ordering change that eliminates it without
  breaking cancellation/shutdown semantics.

Then retain a narrow suppression specific to the exact upstream warning and add a
current explanatory comment/reference. Remove the stale `bugs.md` pointer.

## Workstream D — Harden teardown contract without overbuilding

If an EggPool defect exists, the desired outcome is explicit ownership:

- every task that can use DB is supervisor/fixture owned;
- teardown stops admission where needed;
- task owners cancel/await their tasks;
- no new DB work is submitted after shutdown boundary;
- then Database disconnects;
- then event loop may close.

Do not add a general "drain all asyncio tasks" helper. Await only tasks owned by
the relevant component.

Do not turn app shutdown into an unbounded wait. Existing bounded supervisor
shutdown behavior should remain authoritative unless the reproducer proves a
specific bound is wrong.

## Workstream E — Remove stale warning/documentation debt

If the root cause is fixed:

- remove the `filterwarnings` entry from `pyproject.toml`;
- remove the stale `bugs.md item #7` comment/reference;
- remove any test-local suppressions for the same warning that are no longer
  necessary.

If a narrowly justified upstream suppression remains:

- keep only the exact warning regex/category;
- explain the verified ownership facts inline;
- reference an upstream issue/version condition if one can be established;
- do not reference a missing local file;
- add one focused test that fails on actual unfinished EggPool task ownership,
  rather than relying on the warning filter as the test.

## Workstream F — Focused regression tests

Required coverage depends on root cause, but should include:

1. a minimal reproducer run with the warning promoted to error or without the
   repository filter;
2. clean direct `Database` connect/use/disconnect teardown;
3. app lifespan startup/shutdown with no unfinished DB-using task;
4. cancellation/transaction case if it caused the warning;
5. rehash/finalization teardown only if implicated;
6. existing commit/rollback failure tests to ensure fail-closed semantics remain.

Do not create a broad task-leak framework or assert exact global task counts for
all tests.

## Production smoke

If production code changes, run a short foreground lifecycle check:

1. start EggPool with a temporary/local config;
2. verify health/config startup;
3. perform one deterministic local/mock or configured provider request if
   practical;
4. terminate normally;
5. confirm clean shutdown without DB/thread traceback or warning.

This is a one-off manual confidence check, not CI infrastructure.

## Verification

Run the focused reproducer repeatedly enough to establish deterministic closure,
then affected DB/lifecycle/rehash/finalization tests.

Then ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

If the warning filter is removed, run at least the focused affected union once
with no substitute suppression.

## Explicit acceptance criteria

- [x] The current global aiosqlite warning suppression is disabled during root
  cause investigation and a smallest practical reproducer is identified, or a
  bounded search establishes it is no longer reproducible.
- [x] Database-using task/resource ownership at teardown is documented in this
  plan's closure record.
- [x] Any EggPool-owned task/fixture/lifespan ordering defect is corrected at its
  owner without sleeps/random retries.
- [x] Database transaction ownership, cancellation, commit/rollback ambiguity,
  and fail-closed behavior remain intact.
- [x] `bugs.md item #7` stale reference is removed.
- [x] The global filter is removed when EggPool ordering fixes eliminate the
  warning.
- [x] No upstream-only suppression was required; the global filter was removed
  after the fixture ownership defect was corrected.
- [x] No detached DB work remains solely because a fixture/event loop is closing.
- [x] No new DB driver, worker framework, connection pool, runtime dependency,
  migration, telemetry, or CI job is added.
- [x] Focused tests and ordinary gate pass.
- [x] Implementation SHA, reproducer/root-cause classification, final warning
  disposition, and exact verification are appended to this plan.

## Rejection conditions

Reject implementation if it:

- replaces aiosqlite or SQLite;
- adds `sleep()` to make teardown races less visible;
- swallows cancellation or DB errors to silence warnings;
- adds a global task-drain loop unrelated to ownership;
- broadens warning suppression;
- closes the DB before owned users are joined;
- weakens transaction/fail-closed tests;
- adds permanent task/thread telemetry or a new CI matrix.

## Handoff sequence

1. Read Roadmap 122, this plan, `AGENTS.md`, `pyproject.toml`, DB connection code,
   app/runtime shutdown paths, and relevant fixtures.
2. Disable the filter locally and reproduce before changing production code.
3. Classify root cause using explicit ownership evidence.
4. Fix the narrow owner/fixture/lifecycle boundary.
5. Remove or narrowly justify the filter and stale reference.
6. Run focused lifecycle/DB tests and ordinary gate.
7. Perform one short clean startup/shutdown smoke if production code changed.
8. Append closure evidence to this file and stop.

## Implementation closure

Implementation commit: `84ffa60cf8b3177ed4c2f270bacfc7a1ab9bccc7`

Root-cause classification: **Class 3 — test fixture/event-loop ordering
defect**.

The production lifespan already followed the required ownership sequence:

```text
request/worker tasks
 -> generation-owned finalization/background tasks
 -> process-owned probes/writers
 -> statistics and primary database users
 -> Database.disconnect()/aiosqlite close
 -> event loop teardown
```

The direct database lifecycle fixture in
`tests/unit/test_database_lifecycle.py` connected an in-memory aiosqlite
connection, returned it from an async fixture, and never disconnected it.
That left the worker resource owned by the closing pytest event loop. The
fixture now yields from `try/finally` and always awaits `Database.disconnect()`.
The shared real-runtime fixture now uses the same `try/finally` ownership
boundary, joins `RuntimeManager` retirement first, asserts that no retirement
slot remains, then asserts that the database connection is detached after
disconnect. No sleeps, retries, global task drain, cancellation swallowing, or
production database changes were added.

Reproducer and warning disposition:

- The smallest ownership defect was the unclosed `test_db` fixture; it was
  identified by inventorying direct database fixtures and comparing them with
  the production lifespan ordering.
- The global `PytestUnhandledThreadExceptionWarning` filter and its stale
  `bugs.md item #7` reference were removed from `pyproject.toml`. The orphaned
  `bugs.md` exclude glob was removed as well.
- The affected DB/lifecycle/fail-closed union was run with
  `-W error::pytest.PytestUnhandledThreadExceptionWarning`: **80 passed** with
  no aiosqlite worker-thread warning. The application startup/lifespan and
  direct database teardown cases are included in that union.
- A repository-wide exploratory run reached **1075 passed, 3 skipped** before
  the unrelated manual performance fixture
  `tests/perf/test_comprehensive_baseline.py::TestComprehensiveBaseline::test_all_metrics_baseline`
  failed because its coordinator has no generation finalization supervisor.
  That fixture is outside the ordinary CI gate; its captured warning was an
  unrelated Starlette deprecation warning, not an aiosqlite warning.

Verification:

- `uv sync --frozen --extra ci`: passed.
- `uv run ruff format --check src/ tests/ scripts/`: **701 files already
  formatted**.
- `uv run ruff check src/ tests/ scripts/`: passed.
- `uv run pyright src/ scripts/`: **0 errors, 0 warnings, 0 informations**.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1`:
  **14 passed**.
- `uv run eggpool --config config.example.toml check-config`: passed.
- `uv run eggpool --config config.sbc.example.toml check-config`: passed.

No production source changed, so the plan's one-off production lifecycle smoke
was not applicable. Transaction ownership, cancellation, commit/rollback
ambiguity, and fail-closed behavior remain covered by the focused regression
union.
