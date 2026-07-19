# Phase 6 — Transactional Rehash and Compensatable Commit

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–5.

## Objective

Redesign live rehash as an explicit transaction that cannot leave EggPool in a mixed state. A failed reload must preserve the complete old runtime, persisted provider/account state, process task specifications, shared-writer configuration, and observable effective configuration. A successful reload must expose the complete new state.

This is the central correctness phase. Live rehash should not be considered production-safe until its acceptance criteria are met.

## Problem statement

The current reload path can perform externally visible work before publication, including persistence reconciliation, process-supervisor changes, and shared-writer reconfiguration. Publication can then fail or candidate construction can return through a path that lacks compensation. Conversely, publication may succeed without all observable state being synchronized.

A database transaction alone is insufficient because the operation spans:

- SQLite;
- in-memory active-generation state;
- process-supervisor task specs;
- process-owned writers and monitors;
- compatibility configuration state;
- candidate resource ownership;
- asynchronous old-generation retirement.

The solution is an application-level transaction with prepared deltas, a narrow commit point, and defined rollback or completion behavior.

## Non-goals

- Do not make every config field live-reloadable.
- Do not preserve side effects that cannot be prepared or compensated; classify those fields as restart-required.
- Do not hold the primary SQLite lock while performing network calls or candidate construction.
- Do not make old-generation drainage part of the synchronous commit.
- Do not hide rollback failure; it is an operator-visible critical condition.

## Transaction state model

Introduce a typed state machine, for example:

- `created`
- `validated`
- `diffed`
- `candidate_prepared`
- `persistence_prepared`
- `process_transitions_prepared`
- `commit_started`
- `runtime_published`
- `process_transitions_applied`
- `persistence_committed`
- `observable_state_updated`
- `retirement_scheduled`
- `completed`
- `aborting`
- `aborted`
- `compensation_failed`

Transitions must be monotonic and asserted in code. Diagnostics in Phase 11 should consume this state directly.

## Reload transaction object

Create a transaction object owned by `ReloadManager`, containing:

- request ID and timestamps;
- old generation ID/digest;
- candidate generation ID/digest;
- validated config and semantic diff;
- `RuntimeGenerationCandidate` from Phase 4;
- prepared provider/account persistence delta;
- prepared process transition plan;
- prepared shared-writer transitions;
- old-state snapshots needed for compensation;
- current transaction state;
- commit/abort/compensation diagnostics.

The object should expose narrow methods such as:

- `prepare()`;
- `commit()`;
- `abort()`;
- `compensate()` only if post-publication failure remains possible.

## Prepare stage

All potentially failing and expensive work should happen here without visible mutation.

### Validate and diff

- parse and validate the new config;
- run `check-config` semantics;
- classify changed, ignored, no-op, live, and restart-required fields;
- reject unsupported changes before creating resources;
- capture expected active generation ID and digest.

### Build candidate generation

Use Phase 5’s shared factory under Phase 4 ownership. Complete all mandatory hydration and preflight checks.

### Prepare persistence delta

Calculate, but do not commit:

- providers to insert/update/deactivate;
- accounts to insert/update/deactivate;
- relationships and canonical IDs;
- any config-derived rows;
- expected row/version preconditions where supported.

Prefer immutable typed delta objects. Avoid applying reconciliation to the live database during preparation unless it occurs inside a transaction held only for the bounded commit window.

### Prepare process transitions

Represent process-owned changes as typed reversible transitions, for example:

- `TaskSpecTransition`;
- `RoutingTraceWriterTransition`;
- `DispatchWriterTransition`;
- `MetricsTransition`;
- future monitor transitions.

Each transition must support:

- `preflight()` without mutation;
- `apply()`;
- `rollback()`;
- `finalize()` if old resources close only after commit.

A transition that cannot be safely preflighted and rolled back should be classified restart-required.

### Pre-commit verification

Immediately before commit, verify:

- active generation still matches the expected ID/digest;
- process is not shutting down;
- candidate remains prepared and open;
- database is available;
- all transition preconditions remain valid;
- no restart-required change slipped through.

## Commit protocol

Choose and document one precise ordering. Recommended shape:

1. Enter a narrow commit guard while retaining the reload claim.
2. Revalidate active generation and shutdown state.
3. Open a SQLite transaction and apply the prepared persistence delta without committing.
4. Pre-apply only process operations proven reversible and required before publication.
5. Publish the candidate generation atomically through `RuntimeManager`.
6. Transfer candidate ownership to the runtime manager.
7. Apply remaining bounded process-owned transitions.
8. Update effective configuration and compatibility state through Phase 7’s mechanism.
9. Commit SQLite.
10. Finalize process transitions and schedule old-generation retirement.
11. Mark the transaction completed.

However, implementation should minimize operations after publication that can fail. Where feasible, restructure so post-publication work is limited to in-memory pointer/config swaps and operations already guaranteed by preflight.

SQLite commit ordering requires careful treatment. If SQLite commit can fail after runtime publication, either:

- retain the old generation and implement a tested runtime rollback before it begins teardown; or
- move commit before publication and maintain a compensating inverse persistence delta if publication fails; or
- use a durable pending/committed generation record that lets recovery complete or roll back after failure.

The implementing agent must choose one design and document why it has the smallest irrecoverable window. Do not leave the ordering implicit.

## Recommended durable intent option

A robust option is to add a small reload-intent record in SQLite:

- transaction/request ID;
- old and candidate digests;
- state `prepared`, `committing`, `committed`, `aborted`;
- bounded delta metadata;
- timestamps.

This can support startup recovery if the process dies during commit. It is optional only if the chosen ordering proves process-crash consistency without it.

## Compensation and rollback

Before publication, rollback is straightforward: abort the candidate, roll back SQLite, and leave process state untouched.

After publication, every remaining fallible operation must have one of:

- a tested inverse transition;
- a completion guarantee established during preflight;
- a durable recovery record allowing startup repair.

If runtime rollback is supported:

- retain the old slot without starting retirement until commit succeeds;
- prevent new leases from observing an intermediate generation if rollback is possible, or define a strict visibility point;
- atomically restore the old slot;
- close/abort the candidate exactly once;
- restore process transitions and persistence;
- record compensation outcome.

Avoid allowing requests to execute on a candidate that may later be rolled back unless the semantics are explicitly acceptable and tested.

## Cancellation semantics

Define a commit point:

- before commit point: cancellation aborts candidate and leaves old state;
- during/after commit point: shield the bounded commit, complete or compensate, then propagate or convert cancellation according to control protocol policy.

Do not allow arbitrary task cancellation to interrupt state between publication and persistence/process completion.

## Shutdown race

When shutdown starts:

- reject new reload claims;
- a transaction before commit aborts;
- a transaction in commit completes or compensates under a bounded shield;
- shutdown waits for transaction finalization before closing process-owned dependencies;
- diagnostics identify shutdown-triggered aborts.

## Fault-injection matrix

Using Phase 1 hooks, inject failure at every step:

- validation and diff;
- candidate resource creation;
- backoff hydration;
- persistence delta preparation;
- process transition preflight;
- SQLite begin/apply/commit;
- pre-publication validation;
- publication;
- ownership transfer;
- each process transition apply;
- observable-state update;
- retirement scheduling;
- transition finalize;
- rollback/compensation itself.

For every case, assert either complete old state or complete new state. Mixed state is a test failure.

## State domains to compare

Use Phase 1 snapshots to verify:

- active generation and digest;
- provider/account persistence;
- process task specs and running tasks;
- routing-trace/dispatch writer settings;
- effective config and compatibility mirrors;
- in-memory health/backoffs;
- candidate/open resource counts;
- retirement tasks;
- operational transaction record.

## Implementation sequence

1. Define transaction states and typed deltas/transitions.
2. Refactor reload into prepare and commit methods without changing ordering initially.
3. Remove visible mutations from candidate preparation.
4. Add process transition preflight/apply/rollback contracts.
5. Choose and document SQLite/publication ordering and crash-consistency model.
6. Implement narrow commit guard and active-generation revalidation.
7. Implement cancellation and shutdown shielding.
8. Add compensation or durable intent recovery as required.
9. Route every failure through candidate abort and transaction finalization.
10. Complete the fault-injection matrix.

## Acceptance criteria

- No candidate preparation step mutates active runtime, process tasks, shared writers, or persisted config state.
- Every pre-publication failure leaves all state domains equal to the pre-reload snapshot.
- No ordinary recoverable exception after publication lacks a tested completion or compensation path.
- Cancellation cannot interrupt the commit into a mixed state.
- Shutdown cannot close dependencies underneath an active commit.
- Fault injection at every stage yields complete old state or complete new state.
- Candidate ownership transfers exactly once on successful publication.
- Old-generation retirement starts only after the transaction reaches its defined visibility/commit point.
- Process-crash consistency is documented and tested or supported by a durable intent record.
- Rehash success is not reported until the transaction is committed and observable state is coherent.

## Handoff evidence

Provide a transaction-state diagram, chosen commit ordering, rationale for SQLite/publication ordering, fault-matrix results, cancellation/shutdown tests, and snapshots proving no mixed state at each injected failure.