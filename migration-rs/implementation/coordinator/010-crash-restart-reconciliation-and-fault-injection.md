# C010 — Crash/Restart Reconciliation and Fault Injection

Status: planned; blocked on C009 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant

Hard dependency: C009.

## Objective

Prove that M7 durable ownership can recover from process interruption without a database reset or manual state repair. Implement deterministic reconciliation primitives for incomplete request/attempt/reservation state and exhaustive fault injection around publication, retry, handoff, and finalization.

This plan does not add M8's recurring background scheduler or generation lifecycle. Reconciliation is invoked explicitly by tests/callers.

## Python oracle

Use C001 restart snapshots plus request/attempt/reservation repositories, finalization job recovery/convergence behavior, application startup reconciliation if present, claim/failure/finalizer tests, and existing migration DB rollback/upgrade fixtures.

## Durable reconciliation

Define a bounded `reconcile_once`/equivalent that scans only the relevant nonterminal durable rows using indexed/bounded queries and classifies each case without raw body access. Required states include:

- request nonterminal with no attempt;
- attempt nonterminal with active reservation;
- attempt terminal with active reservation;
- request terminal with nonterminal attempt/reservation;
- interrupted post-commit publication;
- failed-attempt cleanup pending while request remains retryable/nonterminal;
- terminal request with already-released/expired reservation;
- stale duplicate terminal command evidence.

For states whose process-local M5 counts/probes vanished on process death, reconcile durable truth without pretending those ephemeral resources still exist. M8 startup/generation wiring later decides when/how often reconciliation runs.

## Recovery policy

Freeze exact Python behavior where it exists. Where process-death semantics are inherently implicit, choose the narrowest safe convergence consistent with existing rows: fail closed, terminalize interrupted work with explicit release reason, release active durable reservation where safe, never resurrect/provider-replay an unknown in-flight attempt, and never double-charge usage/cost.

Any new policy that changes durable user-visible history requires an ADR or explicit C001 contract amendment.

## Fault injection

Build deterministic hooks/barriers for failure/cancellation immediately before/after:

- local claim acquisition;
- each durable publication write and commit;
- runtime publication component conversion;
- wire negotiation gate acquisition/finish;
- provider send start/header receipt;
- retry decision and failed-attempt terminalization;
- response-start handoff;
- stream first byte/terminal event/EOF;
- terminal job registration;
- each durable finalizer write;
- each runtime component release;
- terminal job completion bookkeeping.

Restart a fresh Rust state over the same DB after simulated crash points and run reconciliation explicitly. Validate Python can still open/read the DB.

## Idempotency/resource tests

Run reconciliation repeatedly and concurrently. Results must converge without new attempts/reservations on each pass. Bounded scans/job queues must not grow with repeated invocation. No stale active count/quota/probe may be reconstructed from durable rows unless the M5 contract explicitly owns such hydration.

## Dependencies

No crash-recovery framework, WAL parser, or task scheduler. Use existing SQLite/repositories and deterministic test hooks.

## Acceptance criteria

C010 closes when every frozen crash point converges to a valid durable state after restart, repeated reconciliation is idempotent/bounded, no unknown in-flight request is replayed, Python rollback readability remains intact, and M8 can later schedule these primitives without changing their semantics.

## Closure

Create `migration-rs/closure/coordinator/010-status.md`. Accepted closure promotes C011.