# Plan 097 — Request Persistence Round-Trip Reduction

Date: 2026-08-10
Status: complete
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

## Purpose

Remove avoidable SQLite/aiosqlite round trips from the normal request lifecycle by folding observability bookkeeping into already-required request mutations, while preserving the durable request/attempt/reservation convergence established by Roadmaps 024/086.

The target is not fewer logical facts. The target is fewer SQL statements and worker-thread crossings for facts that can safely ride an existing mutation.

## Relevant code

Primary file:

- `src/eggpool/db/repositories.py`
  - `RequestRepository.create_pending()`
  - `RequestRepository.finalize_if_pending_returning()`
  - `AttemptRepository.create()`
  - `AttemptRepository.update()`
  - `AttemptRepository.finalize_if_incomplete_returning()`
  - request/attempt helpers used by retry/finalization paths
- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py`
- migrations introducing `first_attempt_at` / `last_attempt_id`, especially `0026_attempt_observability.sql`
- related stats/dashboard queries and focused lifecycle tests.

## Confirmed current costs

### `first_attempt_at`

Attempt-1 creation inserts a `request_attempts` row and then executes a separate request-row UPDATE:

```sql
UPDATE requests
SET first_attempt_at = CURRENT_TIMESTAMP
WHERE id = ? AND first_attempt_at IS NULL
```

The field is observability data used to estimate coordinator overhead. It should not require a standalone hot-path mutation if the equivalent timestamp can be stored during an already-required request mutation without changing semantics.

### `last_attempt_id`

Attempt completion/finalization updates the attempt row and separately writes `requests.last_attempt_id`. The field is an observability/backlink optimization for trace resolution. During retry sequences this can rewrite the parent request multiple times before the final winning/terminal attempt is known.

## Goals

1. Remove the standalone attempt-1 request UPDATE if equivalent `first_attempt_at` semantics can be carried by existing request/attempt persistence.
2. Avoid rewriting `requests.last_attempt_id` for intermediate attempts when only the terminal/latest durable trace relation requires it.
3. Fold `last_attempt_id` into an already-required terminal request mutation where semantics allow.
4. Preserve durable IDs and startup reconciliation.
5. Preserve the Plan 090 `RETURNING` fast path: do not reintroduce read-after-write convergence SELECTs.
6. Keep all correctness DML inside the existing transaction boundaries.

## Non-goals

- removing `request_attempts` or `reservations` tables;
- changing retry semantics or max attempts;
- changing routing-decision persistence;
- changing request schema for aesthetics;
- denormalizing more analytics fields;
- introducing triggers solely to avoid Python calls;
- adding a write queue/batcher;
- changing aiosqlite connection count;
- changing finalization supervisor ownership.

## Workstream A — Define timestamp semantics precisely

Before editing, determine what `first_attempt_at` is intended to represent:

- request-row creation time;
- routing claim completion time;
- attempt-row creation time;
- immediate pre-dispatch time.

Use current docs/query consumers/tests to establish the contract.

If the field is specifically intended to mark **attempt creation**, do not simply set it in the initial request INSERT before routing persistence and claim publication. Instead capture one timestamp once at the attempt-creation boundary and pass it into both required mutations, or use a SQL statement arrangement that records the same timestamp without an extra worker round trip.

Preferred options, in order:

1. extend the already-required request creation/update statement with a supplied first-attempt timestamp when request creation and attempt-1 creation are guaranteed in the same transaction/boundary;
2. capture a Python/SQLite-compatible timestamp once and set it through an existing request mutation in the same transaction;
3. retain the extra UPDATE only if no equivalent semantics can be proven.

Do not trade semantic truthfulness for one fewer statement.

## Workstream B — Remove intermediate `last_attempt_id` writes

Trace all consumers of `requests.last_attempt_id`.

Classify whether they require:

- the most recently *started* attempt;
- the most recently *completed* attempt;
- the final/winning terminal attempt only.

The migration comment describes the field as a backlink to the final attempt that fulfilled the request. If current consumers match that contract, stop updating it for intermediate retryable attempts.

Preferred implementation:

- attempt mutation finalizes the attempt row only;
- terminal request finalization accepts the terminal attempt ID and writes `last_attempt_id` in the same `UPDATE ... RETURNING` statement that marks the request terminal;
- duplicate/idempotent request finalization falls back to existing durable reads only when the conditional terminal mutation did not transition;
- failed retryable attempts remain discoverable through `request_attempts(request_id, attempt_number)` and do not need to rewrite the parent request backlink.

If a dashboard API truly requires the current latest attempt while a request is still pending, preserve that behavior and document why the write cannot be removed; do not invent another cache/table.

## Workstream C — Repository contract changes

Keep repository APIs explicit and local. Likely changes may include:

- add optional/canonical timestamp input to `create_pending()` or a sibling request mutation;
- remove the internal request UPDATE from `AttemptRepository.create()`;
- add `last_attempt_id` to `RequestRepository.finalize_if_pending_returning()` arguments/update statement;
- remove parent request backlink writes from `AttemptRepository.update()` / `finalize_if_incomplete_returning()` where no longer required.

Avoid generic mutation objects or ORM-style repository abstraction. The current direct SQL style is appropriate.

## Workstream D — Preserve atomic convergence

Verify these paths explicitly:

1. normal non-stream success;
2. normal stream success;
3. retryable first attempt then successful second attempt;
4. exhausted retries;
5. client cancellation after durable selection;
6. provider failure requiring retained failed-attempt cleanup;
7. duplicate finalization;
8. process crash / startup reconciliation of pending rows.

Required invariant:

- request terminal state, terminal attempt, and reservation release still converge atomically where they do today;
- `last_attempt_id`, if set, identifies the terminal attempt associated with that terminal request state;
- no stale parent backlink can make a trace claim an intermediate failed attempt was the winner.

## Workstream E — Count SQL statements at application boundary

Use existing database operation counters/fakes or a narrow test spy to compare common paths before/after.

Expected outcomes:

- attempt-1 dispatch persistence: one fewer standalone request UPDATE when Workstream A succeeds;
- retryable intermediate attempt finalization: one fewer parent-request backlink UPDATE when Workstream B succeeds;
- terminal request finalization: no additional statement beyond the existing request terminal UPDATE for `last_attempt_id`;
- Plan 090 first-finalization convergence SELECT count remains zero for transitioned rows.

Do not create a persistent SQL tracing framework.

## Tests

Update focused repository/finalization/coordinator tests for:

- exact `first_attempt_at` semantics;
- no duplicate/redundant first-attempt timestamp mutation;
- retry path with at least two attempts and final backlink pointing to the terminal attempt;
- intermediate failed attempt still queryable from `request_attempts`;
- duplicate finalization preserves existing terminal backlink;
- no-transition fallback behavior remains correct;
- crash repair remains able to identify/reconcile incomplete requests;
- common first-finalization still performs no redundant convergence SELECTs.

## Verification

Run focused repository/request-finalizer/coordinator tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Implementation notes:

- `first_attempt_at` is diagnostic evidence of the first durable attempt
  boundary. The coordinator captures it once immediately before the request
  INSERT; request arrival remains `started_at`.
- `last_attempt_id` is a terminal/winning backlink. Intermediate failed
  attempts remain in `request_attempts` and do not mutate the parent request.
- Focused tests in the repository, finalizer, and metrics lifecycle suites
  passed before the full CI-equivalent gate.

Verification results:

- `uv run pytest tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_finalizer_transaction_scope.py tests/unit/test_attempt_stats.py tests/integration/test_coordinator_lifecycle.py tests/integration/test_failover_matrix.py tests/integration/test_health_idempotency.py tests/integration/test_migration_compatibility.py tests/integration/test_database_transaction_contract.py -q --tb=short --maxfail=1` — 107 passed.
- `uv run pytest tests/unit/test_attempt_stats.py tests/unit/test_finalizer_transaction_scope.py tests/unit/test_metrics_lifecycle.py -q --tb=short --maxfail=1` — 33 passed.
- `uv run ruff format --check src/ tests/ scripts/` — 717 files already formatted.
- `uv run ruff check src/ tests/ scripts/` — passed.
- `uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed.
- `uv run eggpool --config config.example.toml check-config` and the equivalent
  `config.sbc.example.toml` command — both passed.
- Remote GitHub Actions run [31431226706](https://github.com/eggstack/eggpool/actions/runs/31431226706)
  for commit `71c32bf` — `check` passed.

## Acceptance criteria

- [x] The intended meaning of `first_attempt_at` is documented from current consumers/tests before optimization.
- [x] Attempt-1 persistence no longer executes a standalone parent-request UPDATE.
- [x] Timestamp folding preserves the first durable attempt boundary.
- [x] Intermediate retryable attempts no longer rewrite `requests.last_attempt_id`.
- [x] The terminal request UPDATE records the final attempt ID without an extra SQL statement.
- [x] Terminal request finalization supplies the winning attempt backlink.
- [x] Failed intermediate attempts remain fully queryable from `request_attempts`.
- [x] Request/attempt/reservation atomic convergence remains unchanged.
- [x] Duplicate/idempotent finalization retains focused fallback reads on no-transition paths.
- [x] Common first-finalization retains the Plan 090 no-convergence-SELECT fast path.
- [x] Application-level persistence round trips are reduced on the intended paths.
- [x] No trigger, background writer, ORM, schema migration, second connection, or new persistence subsystem was introduced.
- [x] Focused and smoke gates pass.

## Rejection conditions

Reject the implementation if:

- `first_attempt_at` becomes request-start time merely because it is convenient;
- parent request bookkeeping is removed while a supported API requires live latest-attempt state;
- terminal backlink can point to an intermediate failed attempt;
- optimization adds a new read to compensate for a removed write on the common path;
- idempotent/duplicate finalization assumes convergence without checking durable state when the conditional mutation did not transition;
- request/attempt/reservation transaction boundaries are weakened;
- implementation introduces triggers or a new persistence subsystem.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, this plan, repositories/finalizers/coordinator, migration 0026, and consumers of both fields.
2. Document exact field semantics and all read/write call sites.
3. Implement `first_attempt_at` folding only if the same logical timestamp can be retained.
4. Move terminal `last_attempt_id` bookkeeping into the existing request terminal mutation where supported.
5. Remove intermediate parent backlink writes proven unnecessary.
6. Add/update multi-attempt and idempotency regression tests.
7. Compare application-level SQL operation counts with a narrow test spy/counter.
8. Run focused and ordinary smoke/lint/type gates.
9. Record implementation commit and exact verification in this plan.
10. Stop; do not redesign the request schema or durable lifecycle.
