# C006 Closure — Durable Finalization and Retained Terminal Ownership

Status: closed

Recommendation: closed; C007 is dependency-ready.

Implementation commit: [`97a4846`](https://github.com/eggstack/eggpool/commit/97a48464b775514f90d36d021607c091881a36d3)

Plan: [C006 — durable finalization and retained terminal ownership](../../implementation/coordinator/006-durable-finalization-and-retained-ownership.md)

Repository baseline: `9b730c59`

## Outcome

C006 adds `DurableFinalizer` for idempotent request/attempt/reservation
convergence and `FinalizationSupervisor` for bounded process-local retained
terminal jobs. Request outcomes are conditionally terminalized and re-read by
status; incompatible duplicate outcomes fail closed as a typed conflict.
Failed attempts converge independently while the parent request stays pending
for replacement ownership. Converted M5 claims now release active count,
health probe, and quota reservation as separate idempotent operations. Jobs
share one result for duplicate submissions, retry transient finalization
failures a bounded number of times, and expose `snapshot`, `drain`, and
`reconcile_once` interfaces.

## Requirement-to-evidence matrix

| C006 requirement | Evidence | Result |
|---|---|---|
| Idempotent durable request finalization | `DurableFinalizer::finalize_request`; completion test | Pass |
| Attempt cleanup before replacement ownership | `finalize_failed_attempt` leaves parent pending and releases reservation | Pass |
| Terminal conflict detection | Conditional status transition and `TerminalConflict`; completion test | Pass |
| Separate runtime release ownership | `SelectionClaim::release_quota_reservation` plus active/probe release | Pass |
| Post-commit compensation | `compensate_post_commit` and C002 compensation wiring | Pass |
| Retained cancellation-independent supervisor | `FinalizationSupervisor`, shared watch result, duplicate-job test | Pass |
| Bounded capacity/retry/state | capacity cap, three attempts, bounded structural snapshot | Pass |
| Redacted bounded error detail | control-character filtering and 512-character cap; failure cleanup test | Pass |
| No schema fork or M8 lifecycle pull-forward | Existing schema-54 SQL only; no generation/scheduler code | Pass |

## Compatibility evidence

The durable SQL targets the already-qualified Python schema and preserves the
Python terminal vocabulary (`completed`, `client_error`, `cancelled`, and
`error`). The ownership decomposition follows the Python finalizer and
finalization-job oracles while keeping M8 generation ownership out of scope.
Rust tests exercise durable rows, runtime counters, duplicate convergence,
conflicts, and redaction. Public finite/streaming response differential
qualification remains a C007-C011 responsibility and is not claimed here.

## Verification commands actually run

```text
rtk cargo fmt --all
rtk cargo test --test coordinator_finalization -- --nocapture  # 4 passed
rtk cargo test --test coordinator_boundaries -- --nocapture   # 3 passed
rtk cargo clippy --all-targets -- -D warnings                  # clean
rtk cargo test --all-targets                                   # passed
rtk git diff --check                                            # passed
```

No database migration, dependency, provider credential, external network,
M8 generation, signal handler, or perpetual scheduler was added.

## Future-plan audit and registry transition

C006 is removed from the dependency-ready/queued implementation tables and
recorded as completed in the registry, coordinator README, handoff sequence,
roadmap, and this accepted closure record. C007 is promoted to the sole
dependency-ready plan. C008-C011 remain blocked behind their explicit serial
predecessors. M8 runtime-generation/background planning remains blocked on
accepted C011 M7 closure and its separate planning review.

Unresolved mandatory findings: none.
