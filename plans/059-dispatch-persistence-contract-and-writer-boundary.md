# Plan 059 — Dispatch Persistence Contract and Writer Boundary

Date: 2026-07-31
Status: implemented and verified
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Planning baseline: `1a997cf862c04542a6d18e1e2873bc154ebe1fa1`

## Purpose

Prevent a failed dispatch-persistence transaction from being represented as success and make the dispatch writer's event-loop ownership match EggPool's actual single-loop runtime.

The current repository layer can catch a batch transaction failure and return one nominal `PersistedDispatchResult` per input with empty identifiers and attempt ID zero. The writer then increments persisted counters and resolves caller futures successfully. The coordinator checks only for `None`, so empty strings and zero pass through to runtime publication and potentially to upstream dispatch.

This is a narrow contract correction. It does not require a new durable queue, distributed transaction model, savepoint framework, generalized batch retry engine, or multi-loop writer.

## Confirmed defect chain

1. `persist_dispatch_bundles()` catches broad exceptions around the transaction.
2. It manufactures result rows with:
   - `db_request_id=""`;
   - `reservation_id=""`;
   - `attempt_id=0`.
3. `DispatchPersistenceWriter._persist_batch()` treats the returned list as successful persistence.
4. Waiting futures receive values rather than exceptions.
5. `RequestCoordinator._persist_dispatch_bundle_via_writer()` forwards the invalid identifiers.
6. `_select_and_persist_attempt()` only rejects `None`, not empty/zero values.
7. Runtime quota/active ownership can be published and an upstream request can be sent even though the database transaction rolled back.
8. Later code may attempt operations such as integer conversion on an empty request ID, obscuring the original failure.

## Scope

Primary files:

- `src/eggpool/db/dispatch_repository.py`
- `src/eggpool/request/dispatch_writer.py`
- `src/eggpool/request/coordinator.py`
- the existing persisted-dispatch result definition
- existing dispatch repository/writer/coordinator test files

Optional small supporting files:

- `src/eggpool/errors.py` only if an existing database exception cannot express the failure cleanly
- existing metrics/diagnostics definitions if a failure counter needs renaming

## Explicitly out of scope

- persistent dispatch queues;
- cross-process batching;
- per-row database savepoints;
- binary splitting of failed batches;
- automatic replay after unknown commit state;
- a second SQLite connection for dispatch writes;
- generalized event-loop bridging;
- changing routing, quota, retry count, or provider behavior;
- new CI jobs, test markers, benchmark gates, or soak tests.

## Design decisions

1. A rolled-back transaction raises; it never returns placeholder success objects.
2. `PersistedDispatchResult` is a valid durable identity, not a partially populated transport object.
3. Batch atomicity remains simple: all intents in one transaction succeed or all callers receive failure.
4. Input validation happens before opening the batch transaction so malformed local input does not cause avoidable database work.
5. Unexpected database failures are visible to the coordinator and existing database-recovery machinery.
6. EggPool's canonical runtime uses one event-loop thread. The writer should assert that contract instead of pretending to support arbitrary caller loops.
7. No retries are added inside the writer. The existing request/recovery owner decides what to do after persistence failure.

## Phase A — Make repository failure exceptional

### Required changes

1. Remove the broad exception-to-placeholder conversion in `persist_dispatch_bundles()`.
2. On transaction failure:
   - preserve the original exception chain;
   - raise the existing database transaction exception if one is already produced;
   - otherwise wrap it once in a narrow internal dispatch-persistence error.
3. Do not return any `PersistedDispatchResult` values after rollback.
4. Validate every input bundle before entering the transaction:
   - required request identity inputs are non-empty;
   - account/provider/model identifiers required by the schema are present;
   - requested token/cost/request reservations are non-negative;
   - enum/status inputs are valid at their construction boundary.
5. Keep the transaction all-or-nothing. Do not add per-intent savepoints merely to preserve unrelated rows in a small local microbatch.
6. Preserve unknown-commit handling from `Database.transaction()`; do not catch and reinterpret a commit ambiguity as an ordinary rollback.

### Result invariant

A repository call has exactly two outcomes:

- a result list with the same length/order as the validated input list and every result fully valid; or
- an exception.

There is no third placeholder state.

### Acceptance criteria

- A statement failure rolls back the batch and raises to the writer.
- No result object with empty request/reservation IDs or attempt ID zero is produced.
- A validation failure occurs before any transaction begins.
- A commit-ambiguity exception remains distinguishable from an ordinary statement/rollback failure.
- Successful batches preserve existing row order and identity mapping.

## Phase B — Enforce persisted-result validity

### Required changes

1. Add one private or dataclass-level validation path for `PersistedDispatchResult`.
2. Require:
   - `db_request_id` is a non-empty string;
   - `reservation_id` is a non-empty string;
   - `attempt_id` is a positive integer;
   - any account/provider identity required by current callers is present.
3. Prefer validation at construction time if the type is internal and all construction sites are controlled.
4. If construction-time validation would create broad churn, add one `validate()` or `is_valid()` helper and call it at both repository return and coordinator receipt.
5. Do not introduce a general validation library or public schema model.
6. Keep the coordinator's defensive check even after repository validation. Persistence identities are correctness-critical and cheap to verify.

### Acceptance criteria

- Empty string IDs are rejected.
- Attempt ID zero and negative values are rejected.
- The coordinator cannot publish runtime ownership after an invalid result is injected by a test double.
- Successful existing call sites require no semantic changes beyond constructing valid results.

## Phase C — Propagate batch failure to every waiter

### Required changes

1. In `DispatchPersistenceWriter._persist_batch()`:
   - catch only to fan the same failure out to each pending future;
   - do not increment `_persisted_total` for failed intents;
   - increment an existing failed counter, or one narrowly named counter if none exists;
   - preserve cancellation semantics for callers already cancelled.
2. Resolve no future with a placeholder value.
3. Ensure each unresolved future receives an exception exactly once.
4. Keep the writer task alive after an ordinary batch failure so a later request can persist successfully, unless the database layer explicitly reports a failed-closed/recovery condition that existing lifecycle code handles.
5. Do not automatically replay the failed batch. The transaction owner cannot assume whether an ambiguous commit occurred.
6. Ensure a failed batch does not block queue progress for later independent submissions after the database is available.

### Acceptance criteria

- Every non-cancelled waiter in a failed batch receives an exception.
- Failed intents are not counted as persisted.
- A later valid batch can succeed after a deterministic rollback failure.
- No upstream dispatch is attempted for any failed intent.
- Writer shutdown/drain still resolves or cancels all owned futures.

## Phase D — Fail closed in the coordinator

### Required changes

1. Replace `is not None` assertions with explicit durable-identity validation.
2. Perform validation before:
   - publishing quota reservation state;
   - incrementing active request ownership;
   - recording post-commit selection metadata;
   - sending an upstream request.
3. If validation fails:
   - classify it as an internal persistence invariant failure;
   - do not treat it as an upstream/provider failure;
   - do not penalize or suppress the selected provider/account;
   - enter the existing retained compensation path only if a valid durable component was actually created.
4. Avoid attempts to parse or convert invalid IDs for diagnostics.
5. Preserve request-local failure: one malformed test double or database error must not poison subsequent unrelated routing.

### Acceptance criteria

- Invalid persistence output cannot reach runtime publication.
- Provider health and model quarantine are unchanged by local persistence failure.
- No cleanup path attempts to release a reservation whose identity is empty.
- The next unrelated request can proceed once database health is restored.

## Phase E — Make event-loop ownership explicit

### Current mismatch

The writer creates its queue and task on the loop used by `start()`, but submission obtains the caller's running loop and schedules against that loop. This implies cross-loop support that is not valid for an `asyncio.Queue` and locks owned by another loop.

### Preferred correction

Enforce the canonical one-loop contract:

1. Capture `self._loop = asyncio.get_running_loop()` in `start()`.
2. In `submit_intent()`, require the current loop to be `self._loop`.
3. Raise a clear internal error on cross-loop submission.
4. Update comments/docstrings that claim or imply cross-loop support.
5. Keep `runtime_threads=1` as the canonical deployment setting documented in `AGENTS.md`.

### Rejected alternative

Do not add `call_soon_threadsafe`, thread-safe proxy futures, or a cross-loop adapter unless the runtime architecture is intentionally changed in a separate roadmap. That complexity is not justified for the current SBC process model.

### Acceptance criteria

- Same-loop submission behavior is unchanged.
- Cross-loop submission fails immediately with a clear invariant error rather than touching the foreign queue.
- No thread-safe bridging code or additional worker thread is introduced.

## Focused verification

Test budget: normally no more than five focused cases, placed in existing capability-based files.

Required coverage:

1. Repository statement failure rolls back and raises; no placeholder results exist.
2. Writer fans one failure to every waiter and does not increment persisted count.
3. Coordinator rejects an injected empty-ID/zero-attempt result before runtime publication or upstream send.
4. A valid request succeeds after a prior deterministic batch rollback.
5. Cross-loop submission is rejected, or one direct invariant test proves the writer is bound to its owner loop.

Use one real in-memory or temporary SQLite transaction for the rollback case. Mocking alone is insufficient for the repository contract. Do not add repeated fault campaigns or a concurrency stress harness.

## Implementation sequence

Recommended commits:

1. repository exception contract and result validation;
2. writer propagation and loop ownership;
3. coordinator fail-closed checks and focused regressions;
4. plan status/documentation closure after verification.

Fewer commits are acceptable if reviewability remains good.

## Plan acceptance criteria

- [x] `persist_dispatch_bundles()` never encodes failure as a result object.
- [x] All successful result objects contain non-empty request/reservation IDs and a positive attempt ID.
- [x] Batch failure reaches all waiting callers as an exception.
- [x] Failed intents do not increment persisted metrics.
- [x] Runtime ownership and upstream dispatch occur only after validated durable identity exists.
- [x] Persistence failures do not penalize providers or models.
- [x] A later request can succeed after a prior deterministic rollback.
- [x] Dispatch writer loop ownership is explicit and same-loop only.
- [x] No durable queue, savepoint framework, batch-replay engine, cross-loop bridge, CI job, or soak test is added.

## Definition of done

The plan is complete when dispatch persistence has a binary success/exception contract, invalid durable identities cannot escape the repository or coordinator, every failed batch caller is resolved correctly, the writer matches the single-loop runtime, and focused regressions plus the existing smoke suite pass.
