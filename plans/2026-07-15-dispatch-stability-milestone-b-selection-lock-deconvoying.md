# Dispatch Stability Milestone B — Selection Lock Deconvoying

Date: 2026-07-15
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: B of G
Depends on: Milestone A

## Objective

Remove the coordinator-wide head-of-line blocking pattern in which a request holds `RequestCoordinator._select_lock` while waiting for the primary SQLite connection. Preserve durability-before-upstream-dispatch, circuit-breaker correctness, routing fairness, retry behavior, quota reservation accuracy, and cancellation cleanup.

This milestone should improve latency stability even before the dedicated dispatch writer in milestone C. It creates the state machine and lock ordering that milestone C will later use.

## Problem statement

`RequestCoordinator._select_and_persist_attempt()` currently computes a routing plan outside `_select_lock`, then enters `_select_lock` and performs all of the following before releasing it:

- live circuit-breaker probing;
- account credential/account ID resolution;
- request, reservation, and attempt persistence inside `db.transaction()`;
- commit;
- active-request count publication;
- quota reservation publication;
- attempted-account publication on the request context.

The database itself serializes operations through a process-owned connection lock. If any other task owns the database lock, the selector waits for SQLite while still holding `_select_lock`. All subsequent selectors then wait for `_select_lock`, even if their own routing plans and target accounts are unrelated.

The goal is not to remove serialization where correctness requires it. The goal is to ensure the coordinator's global selection lock is never held across database queueing or transaction I/O.

## Architectural decision

Introduce an explicit selection claim state machine with these phases:

```text
PLAN
  routing plan computed without coordinator lock

CLAIM
  narrow in-memory critical section
  revalidate live breaker/candidate
  acquire health slot
  reserve a claim token
  optionally publish provisional active/quota state

PERSIST
  outside coordinator lock
  durably create request/reservation/attempt bundle

COMMIT/PUBLISH
  mark claim committed
  publish request-local attempted-account metadata
  retain active/quota/health ownership for upstream execution

ROLLBACK
  on any pre-commit failure or cancellation
  release health slot
  undo provisional active/quota state
  invalidate claim token
```

The implementation must explicitly choose whether active-request and quota reservation state are published provisionally during CLAIM or only after durable persistence. Either approach can work, but it must not recreate the same global lock convoy.

Recommended approach for milestone B:

- claim the circuit/health slot under a narrow lock;
- persist outside `_select_lock`;
- after commit, publish active count and quota reservation through their own narrow synchronization;
- if post-commit publication fails, finalize/compensate durably and release the health slot using the existing post-commit interruption path;
- use a per-request claim token to prevent duplicate publication or rollback.

Milestone C may later move persistence behind a process-owned queue, but the claim/rollback contract should remain stable.

## Scope

### In scope

- Define and implement the selection claim state machine.
- Establish and document lock ordering.
- Remove all database awaits from inside `_select_lock`.
- Minimize other awaited operations inside `_select_lock`.
- Make claim rollback deterministic under exceptions and cancellation.
- Preserve retry and attempted-account semantics.
- Add detailed span timing for claim wait/held, persistence wait/execute, and post-commit publication.
- Add concurrency and fault-injection tests.

### Out of scope

- Dedicated dispatch writer and microbatching.
- Lossy trace queue implementation.
- Retention batching.
- Broad runtime-thread changes.
- Replacing SQLite or changing durability mode.

## Target files and modules

Primary:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/health/health_manager.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py` or equivalent attempt cleanup module
- `src/eggpool/runtime_dispatch.py`
- `src/eggpool/db/repositories.py`

Potential new module:

- `src/eggpool/request/selection_claim.py`

Tests:

- coordinator unit tests;
- router/quota/health concurrency tests;
- proxy integration tests;
- cancellation/failure-injection tests;
- performance tests created in milestone A.

Documentation:

- `architecture/README.md`

## Workstream B1 — Inventory current mutable state and lock ownership

Before changing code, produce a precise inventory of every mutable field touched by selection:

- coordinator `_select_lock`;
- request context `attempted_accounts` and `client_metadata`;
- health manager request slots and half-open probe slots;
- registry/account health state;
- router active-request count;
- quota estimator reservation state;
- account ID cache;
- fairness rotor state;
- routing recovery state;
- durable request/reservation/attempt rows.

For each field, record:

- current lock or serialization mechanism;
- whether access awaits;
- whether it is process-, generation-, account-, model-, or request-scoped;
- rollback operation;
- idempotence behavior;
- whether it must be synchronized with durable commit.

Do not begin the refactor until the exact invariant between health slot, active count, quota reservation, and durable reservation is documented.

## Workstream B2 — Define lock ordering and prohibited patterns

Add an architecture comment and tests enforcing the intended order.

Recommended ordering:

```text
request-local state
  -> selection claim lock
  -> account/health narrow lock if separate
  -> release all selection locks
  -> database transaction/dispatch writer
  -> router active-count lock
  -> quota reservation lock
```

Prohibited patterns:

- `_select_lock` held while awaiting `db.transaction()`, `fetch_*`, or repository writes;
- `_select_lock` held while waiting for routing trace persistence;
- `_select_lock` held across upstream I/O;
- database transaction held while awaiting a global coordinator lock;
- quota/router locks held while entering SQLite;
- cleanup path acquiring locks in the reverse order of the success path.

If the design cannot avoid a nested lock, document the exact order and add a deadlock regression test.

## Workstream B3 — Introduce `SelectionClaim`

Add a small explicit object representing one selected account before durable persistence. Suggested immutable identity fields:

- request ID;
- attempt number;
- account name;
- account ID;
- provider ID;
- model ID;
- protocol;
- selected score/tier;
- estimated tokens/microdollars;
- health-slot acquisition state;
- claim token/generation;
- creation timestamp.

Mutable lifecycle state should be narrow and explicit:

- `claimed`;
- `persisted`;
- `published`;
- `rolled_back`;
- durable IDs after persistence.

Prefer a state enum and idempotent methods over a collection of booleans. Invalid transitions should raise in tests and fail closed in production cleanup.

The claim object must not contain secrets beyond what the existing `SelectedAttempt` already requires. Avoid logging API keys or raw headers.

## Workstream B4 — Narrow candidate revalidation and health-slot claim

Continue computing the expensive routing plan outside the lock. Inside the narrow claim section:

1. Revalidate the ranked candidates against the live circuit breaker.
2. Select the first candidate whose slot can be acquired.
3. Resolve only in-memory fields needed to form the claim.
4. Do not perform database account lookup under the lock.
5. Mark the account claimed for this request/attempt.
6. Exit immediately.

Account ID lookup should be warmed at generation construction or resolved before entering the claim section. Preferred options, in order:

1. Populate an immutable account-name-to-ID mapping during account sync/runtime generation build.
2. Use the existing account ID cache and ensure it is complete before serving readiness.
3. As a temporary fallback, perform lookup before claim and revalidate afterward.

Do not retain a database lookup inside `_select_lock`.

If credentials can change only through generation swap, resolve the API key from the immutable generation registry outside the lock. Fail closed if credentials are absent.

## Workstream B5 — Persist outside the selection lock

After claim creation, create the durable request/reservation/attempt rows outside `_select_lock`.

For the first attempt:

- create pending request;
- create reservation;
- create request attempt.

For retries:

- create new reservation and attempt;
- update request selection/reservation fields as currently required.

Keep these operations in one SQLite transaction for atomicity. Milestone C will replace the direct call with a queued writer, so define a repository-facing method now:

```python
persist_dispatch_bundle(bundle: DispatchPersistenceBundle) -> PersistedDispatchBundle
```

The method should return all generated IDs and must not mutate router/quota/health state.

On persistence exception or cancellation before commit:

- call idempotent claim rollback;
- release health/circuit slot;
- ensure no active/quota state remains;
- do not add the account to `context.attempted_accounts` unless retry semantics intentionally require excluding a candidate whose local persistence failed. Recommended: do not mark attempted because no upstream attempt occurred.

On ambiguous commit outcome, fail closed and use an idempotent reconciliation query keyed by request ID/attempt number before deciding whether to rollback or publish. Do not assume an exception means no commit occurred.

## Workstream B6 — Post-commit publication and compensation

After durable commit:

- construct `SelectedAttempt`;
- increment router active-request count;
- add quota reservation;
- publish `context.attempted_accounts` and request metadata;
- retain health slot for upstream execution.

If router/quota publication can be combined under a narrower account-scoped lock, do so. Do not use the global coordinator selection lock.

If post-commit publication fails or is cancelled:

- finalize the durable attempt as `post_commit_interrupted` or an equivalent stable reason;
- release durable reservation;
- undo any partial router/quota publication idempotently;
- release health slot;
- record a bounded diagnostic event;
- do not dispatch upstream.

Create a single compensation helper rather than duplicating cleanup across branches.

## Workstream B7 — Retry and fairness semantics

Verify the refactor does not change:

- `context.attempted_accounts` ordering/contents;
- candidate exclusion on retry;
- distinction between pre-dispatch model unavailability and post-attempt exhaustion;
- half-open circuit slot semantics;
- fairness rotor advancement;
- account active-request penalties;
- reservation estimates;
- provider/account client selection.

Decide when fairness state advances. It should not permanently advance for a claim that fails local persistence unless that is already intended. If the fairness rotor currently advances during plan construction, consider returning a tentative decision that is committed only with a successful claim, or document the limited fairness skew. Do not broaden milestone B into a complete fairness redesign unless tests expose a correctness issue.

## Workstream B8 — Instrumentation

Add named spans/counters:

- `selection_claim_wait`;
- `selection_claim_held`;
- `selection_revalidation`;
- `dispatch_persistence_wait`;
- `dispatch_persistence_transaction`;
- `dispatch_persistence_commit` if separable;
- `post_commit_publication`;
- `claim_rollback`;
- `post_commit_compensation`.

Add counters:

- claims created;
- claims rolled back before persistence;
- ambiguous commit reconciliations;
- post-commit publication failures;
- compensation successes/failures;
- maximum concurrent claims.

The runtime snapshot should make it possible to prove `_select_lock` is no longer waiting on SQLite. Consider a debug-only assertion or context marker that raises if database access occurs while the claim lock is held.

## Test plan

### Unit tests

- successful first-attempt state transition;
- successful retry state transition;
- no eligible candidate;
- circuit slot rejected, next candidate selected;
- missing credentials/account ID;
- request insert failure;
- reservation insert failure;
- attempt insert failure;
- commit failure before commit;
- ambiguous commit outcome with row present;
- cancellation before claim;
- cancellation while waiting for claim;
- cancellation after claim but before persistence;
- cancellation during persistence;
- cancellation after commit but before publication;
- router publication failure;
- quota publication failure;
- idempotent rollback and compensation;
- attempted-account set unchanged on local persistence failure;
- health slot released exactly once.

### Concurrency tests

- Hold the primary DB lock with a test transaction, start selector A, then selector B. Assert B can enter/complete claim work and neither holds the global claim lock while waiting for DB.
- Dispatch to different accounts under a blocked DB and verify selection claim held time remains bounded.
- Concurrent half-open circuit probes permit only the configured slot count.
- Concurrent retries do not select an already-attempted account for the same request.
- Burst load returns claim lock queue to zero after release.

### Integration tests

- Native and transcoded request success.
- Streaming success and cancellation.
- Retry across two accounts.
- Rehash during requests; each request keeps its generation lease.
- Forced DB contention from finalization/maintenance.
- Compare milestone A baseline against the refactor.

## Acceptance criteria

1. No database operation occurs while `_select_lock` or its replacement global claim lock is held.
2. Claim lock-held p95 is below 5 ms on the CI host under the standard concurrent benchmark, excluding fault injection.
3. A blocked primary DB transaction increases persistence wait but does not cause selection claim-held duration to rise with the DB wait.
4. Durability-before-upstream-dispatch remains intact.
5. Request/reservation/attempt rows remain atomic per attempt.
6. Health, active-count, and quota state are released exactly once on every failure/cancellation path.
7. Post-commit publication failure is durably compensated before the request can dispatch upstream.
8. Retry, fairness, provider selection, and attempted-account semantics match the pre-refactor behavior.
9. Runtime diagnostics expose claim, persistence, rollback, and compensation timing/counters.
10. Existing finalization retry and stale-request safety nets remain functional but are not required for ordinary local persistence failures.
11. Full tests, ruff, format check, and pyright pass.
12. Milestone A soak workload shows a material reduction in selection lock wait p95/p99 and no time-dependent regression.

## Rollout and rollback

Land the state-machine types and instrumentation first if useful, followed by the lock-boundary refactor. Keep a temporary internal compatibility path only long enough to compare behavior in tests. Do not expose a user-facing configuration switch that leaves two long-term correctness paths.

Rollback criterion: any evidence of double reservation, leaked health slots, upstream dispatch without committed rows, retry-account reuse, or generation-crossing state. In that case revert to the prior direct path while retaining milestone A diagnostics.

## Handoff evidence

The implementing agent should provide:

- lock-order diagram;
- before/after span snapshots under blocked DB;
- state transition table;
- failure-injection test list;
- proof that no DB await occurs under the claim lock;
- benchmark comparison against milestone A;
- residual risks for milestone C.

## Exit condition

Milestone B is complete when selection has a narrow explicit claim phase, all database work occurs outside the global coordinator lock, rollback/compensation is deterministic, and sustained concurrency no longer converts one SQLite waiter into a global selection convoy.