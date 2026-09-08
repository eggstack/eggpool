# R003 Closure — Active Generation Manager, Atomic Publication, and Request Leases

Status: closed

Recommendation: closed

Implementation commit: [`af794eaa92eb767c837b10c9f01dc3f962181c3e`](https://github.com/eggstack/eggpool/commit/af794eaa92eb767c837b10c9f01dc3f962181c3e)

Plan: [R003 — active generation manager, atomic publication, and request leases](../../implementation/runtime-lifecycle/003-active-generation-manager-publication-and-leases.md)

Repository baseline: `93428b0b6f960fb5344b1e344eff405c77c47327`

## Outcome

R003 adds the process-owned `RuntimeManager` and publishes immutable
`GenerationSlot` values through one `ArcSwap`. A short synchronous manager lock
covers admission-gate recheck, active-slot load, lease increment, and staged
pointer transitions; no async lock is held across request work.

Finite handlers acquire one `GenerationLease` before invoking the M7 endpoint
path and retain it through terminal finalization. Streaming handlers move the
same lease into the spawned Axum body task, so a stream remains pinned through
clean terminal, failed terminal, disconnect, or retained terminal registration.
`AppState` no longer stores an inference graph as request authority.

## Requirement-to-evidence matrix

| R003 requirement | Evidence | Result |
|---|---|---|
| Atomic active publication | `RuntimeManager` uses `ArcSwap<GenerationSlot>`; `commit_pointer()` marks the old slot non-accepting and swaps the pointer while the gate remains closed | Pass |
| Linearizable acquisition | `acquire()` registers its `Notify` waiter before the gate/active/claim critical section and increments the slot count under that section | Pass |
| Staged protocol | `stage`, `commit_pointer`, `rollback_pointer`, `rollback`, and `accept` implement gated preparation, pointer commit/restore, epoch handling, and candidate transfer | Pass |
| No old lease after commit | R003 test commits B while admission is gated, verifies the active pointer is B and non-accepting until acceptance, then observes only B for the awakened waiter | Pass |
| Rollback semantics | Focused test restores A, keeps pointer rollback gated for compensation, then completes rollback without incrementing the publication epoch | Pass |
| Shutdown/gate behavior | Shutdown rejects acquisition/staging; cancelled acquisition waiters leave zero lease count; accept/rollback notification wakes waiters | Pass |
| Finite generation pinning | Focused lease test holds a finite lease across B publication and confirms it remains on A | Pass |
| Streaming generation pinning | Focused lease test holds a stream lease across B publication and confirms it remains on A; server body task owns the lease through completion/disconnect | Pass |
| M7 safety | Existing finite/stream execution and retained-finalization paths are unchanged; server integration continues to use the same coordinator entry points | Pass |
| Secret-free lifecycle state | Slot, lease, manager, and staged-swap debug/snapshot surfaces contain IDs, digest prefixes, states, counts, and elapsed classes only | Pass |
| Dependency scope | Added only direct `arc-swap` dependency; no schema, task loop, reload parser, signal, or control surface was added | Pass |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r003  # 6 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets                # 318 passed, 32 suites
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1              # 89 passed, 3 skipped
rtk uv run pytest tests/migration_rs/test_r001_runtime_lifecycle.py -q --tb=short --maxfail=1  # 6 passed
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1                    # 14 passed
rtk git diff --check
```

The first focused run exposed only a test assertion that treated the
intermediate `rollback_pointer()` operation as the final rollback. The test
was corrected to assert the specified gated-compensation behavior, and the
final all-target run passed.

## Supported structural difference

Rust uses `ArcSwap` plus a narrow synchronous claim/publication section rather
than the Python condition/slot implementation. This is the R003 plan's
explicit permitted normalization; the observable gate, lease, publication,
rollback, shutdown, and request-pinning behavior is covered by deterministic
Rust tests. R004 owns retirement drain/close policy; R003 exposes the old slot
through a bounded-purpose placeholder collection without closing it.

## Unresolved findings

No unresolved R003 correctness, security, resource, or compatibility finding
remains. The manager keeps at most four unresolved retiring-slot placeholders
until R004 supplies reaping; staging rejects further publications at that
bound. No live provider or network fixture was required. The only
intentional follow-up is R004's retirement/finalization drain and resource
close implementation.

## Future-plan audit and registry transition

R003 is removed from the dependency-ready table and recorded in the completed
implementation table with the implementation commit above and this accepted
closure record. R004 is promoted to the sole dependency-ready M8 plan because
its hard dependency, accepted R003 closure, is now satisfied. R005-R011 remain
serially blocked behind their immediate predecessors. M9 remains blocked on
accepted R011 M8 closure and its own planning/implementation review.

No other future plan can be safely unblocked by R003 alone. No M8 roadmap,
M9 boundary, database schema, or operational CLI status is promoted by this
closure.
