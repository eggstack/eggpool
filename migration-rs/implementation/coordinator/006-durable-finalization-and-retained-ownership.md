# C006 — Durable Finalization and Retained Terminal Ownership

Status: planned; blocked on C005 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant

Hard dependency: C005.

## Objective

Port the attempt/request terminal machinery that makes cleanup independent of the client request task. Implement idempotent durable finalization, claim/runtime compensation, failure-effect convergence, and a bounded retained terminal-job supervisor interface.

## Python oracle

Use C001 plus `attempt_finalizer.py`, `finalizer.py`, `finalization_job.py`, `claim_lifecycle.py`, effects applier, quota/router/health release paths, terminal-status definitions, and repository transactions.

## Terminal command model

Use immutable terminal identity/submission data plus explicit resumable progress. At minimum support:

- retryable failed-attempt cleanup;
- post-commit claim compensation;
- selected request finalization for completed, client error, upstream error, midstream error, client cancelled, timeout, and interrupted outcomes.

Separate durable convergence from runtime release and non-authoritative analytics. Durable request/attempt/reservation truth and required runtime resources must converge before a command is complete; logging/analytics failure cannot retain correctness ownership forever.

## Idempotency and conflict

DB transitions must use conditional terminal updates and re-read durable state when a transition did not occur. Repeated/concurrent compatible submissions share/observe the same terminal result. A request already terminal with an incompatible outcome is a typed conflict, not silently overwritten.

A failed attempt is independently finalized and its reservation converged before C005 authorizes replacement ownership. Request finalization may race attempt cleanup without double-releasing a reservation or applying health effects twice.

## Runtime lease/compensation

Track active count, quota reservation/pending load, and health/circuit probe ownership independently. Each component releases at most once. Partial release failures remain observable/retryable rather than marking the whole command complete.

Post-commit interruption uses the C002 receipt to compensate every acquired component and terminalize any durable attempt/reservation. Pre-publication release remains synchronous/local where possible.

## Retained supervisor

Implement a small bounded process-local supervisor/registry that registers terminal ownership before cancellation-sensitive awaits, retains one canonical job per attempt/request identity, lets multiple waiters share the same job, observes completion independently of request waiters, and exposes explicit `drain`, `snapshot`, and `reconcile_once` style interfaces.

Do not implement M8 generation publication, rehash, signal handlers, or a perpetual background scheduler. The supervisor must be movable/ownable by an M8 generation later.

## Backoff/capacity

Supervisor capacity and retry queues are bounded. Capacity exhaustion must fail before ownership is transferred unless a synchronous safe compensation path is guaranteed. Durable-finalization retry delay/backoff must be bounded and injectable for tests.

## Cost/usage/effects

Preserve Python finalization precedence for provider-reported cost, trusted local derived/partial/exact cost, bounded estimated fallback, reservation estimate, normalized usage/cache counters, request/attempt bytes/timing, upstream request ID, release reason, and redacted error detail policy. Reuse M6 normalized usage and M5 effects rather than recomputing provider semantics.

## Tests

Fault-inject each durable write and each runtime component release. Cover duplicate/concurrent terminal submissions, request-vs-attempt finalizer race, cancellation of all external waiters, supervisor capacity, retryable DB failure, partial runtime release, terminal conflict, post-commit compensation, and eventual convergence after dependencies recover. Assert jobs/resources return to baseline.

## Dependencies

Tokio task primitives and existing SQLite/M5 types only. No task queue, actor system, executor framework, or new database dependency.

## Acceptance criteria

C006 closes when terminal correctness no longer depends on the client task reaching a `finally` block, durable and runtime ownership converge idempotently under races/faults, supervisor state is bounded/redacted, and M8 can later own the supervisor without changing terminal semantics.

## Closure

Create `migration-rs/closure/coordinator/006-status.md`. Accepted closure promotes C007.