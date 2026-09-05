# C002 — Durable Dispatch Publication and Lifecycle Identity

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant/capability

Hard dependency: C001.

## Objective

Port the transaction boundary that turns an M5 local selection claim into durable request/attempt/reservation/routing-decision ownership. Establish typed Rust lifecycle identities and compensation semantics before any provider send is allowed.

## Python oracle

Use C001 plus `request/coordinator.py`, `attempt_finalizer.py`, `claim_lifecycle.py`, `finalization_job.py`, request/attempt/reservation/routing-decision repositories, and existing SQLite migrations.

## Required implementation

Introduce small Rust types equivalent in role to `FinalizationIdentity`, `RuntimePublicationReceipt`, attempt identity, and publication result. Keep request IDs, DB request IDs, attempt IDs, reservation IDs, account/provider/model/protocol/wire identity, attempt number, estimates, and frozen M5 selection trace explicit.

Implement one transaction/service that, after M5 claim acquisition:

1. revalidates required selected identity;
2. creates or observes the request row according to Python idempotency semantics;
3. creates the attempt row;
4. creates the reservation row;
5. persists the frozen D009 routing decision/trace when required;
6. commits as one durable publication boundary where the Python contract requires atomicity;
7. converts/publishes the process-local claim components exactly once;
8. returns an immutable terminal identity plus a receipt describing every acquired component.

Pre-commit failure releases only unpublished/provisional M5 ownership. Post-commit interruption must produce enough identity for retained compensation; it must never require a database reset or daemon restart.

## Invariants

- No provider HTTP request in C002.
- No attempt exists without a traceable request identity.
- A reservation cannot be released twice.
- Pending quota load is either converted or released, never both.
- Circuit probe/active count/quota reservation ownership is explicit, not inferred from later state.
- Duplicate publication observes the frozen contract rather than creating an unbounded row fan-out.
- DB writes use the existing schema/migrations; no Rust-only schema.
- Error detail/request bodies/auth values are not persisted.

## Tests

Use Python-created DB fixtures and Rust-created writes. Cover success, failure at each write/commit/publication boundary, cancellation barriers, duplicate invocation, transaction rollback, post-commit interruption, missing account identity, and compensation retries. Verify Python can read Rust state and that active/pending/quota/probe counts return to baseline after failed publication.

## Dependencies

No ORM or transaction framework. Use the existing F004 SQLite layer and M5 state interfaces. No new Cargo dependency expected.

## Acceptance criteria

C002 closes when durable publication has one explicit atomic boundary, every local component has an auditable receipt, failure/cancellation cannot strand claim ownership, rollback compatibility is proven, and no network dispatch is possible from this slice.

## Closure

Create `migration-rs/closure/coordinator/002-status.md`. Accepted closure promotes C003.
