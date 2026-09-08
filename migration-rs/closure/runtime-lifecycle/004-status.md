# R004 Closure — Generation Retirement, Retained Finalization Drain, and Resource Close

Status: closed

Recommendation: closed

Implementation commit: [`fa3ab9d`](https://github.com/eggstack/eggpool/commit/fa3ab9dcc54cf0607a6388ddbfa83df8a69e742f)

Plan: [R004 — generation retirement, retained finalization drain, and resource close](../../implementation/runtime-lifecycle/004-generation-retirement-finalization-drain-and-close.md)

## Outcome

R004 replaces the R003 retiring-slot placeholder with manager-owned retirement.
Published old generations now move through `Retiring` → `DrainingFinalization`
→ `Closing` → `Closed`, or remain resident as `FailedClose`. Retirement waits
for explicit request/stream leases and retained terminal references, then drains
the generation-owned finalization supervisor before closing provider clients.

The manager owns one tracked Tokio retirement task per slot, deduplicates
retirement requests, reaps completed tasks and closed slots, and retains only a
bounded diagnostic summary. Four unresolved generations remain the local
backlog cap; staging rejects before candidate ownership transfers when that cap
is reached. Close reports expose deterministic, secret-free order and typed
finalization timeout/worker-failure evidence. Failed old-generation closure
does not close process-owned database, affinity, or wire-resolver state, and
the active generation remains admissible.

The finite and streaming drop paths now register retained finalization work
synchronously before spawning the waiter, closing the lease-release/registration
race that R004 must protect.

## Requirement-to-evidence matrix

| R004 requirement | Evidence | Result |
|---|---|---|
| Lease drain | `lease_drain_precedes_close_and_reaps_the_slot`; R003 finite/stream lease pinning tests | Pass |
| Retained finalization drain | `retained_finalization_reference_blocks_provider_close`; supervisor bounded drain API | Pass |
| Close order/idempotency | `close_order_is_deterministic_and_duplicate_retirement_is_idempotent`; provider close count remains one | Pass |
| Failed finalization isolation | `failed_finalization_keeps_old_transport_open_and_isolated`; typed diagnostic and active generation lease | Pass |
| Backlog bound/reaping | Updated R003 bounded-backlog test holds four old-generation leases, rejects the fifth publication before transfer, then drains to zero | Pass |
| Task ownership | `RuntimeManager` stores each retirement `JoinHandle`, deduplicates by slot, and reaps finished handles | Pass |
| Bounded diagnostics | `VecDeque` is capped at 16 summaries; unresolved failed slots remain resident | Pass |
| Secret-free evidence | Slot, close, retirement, and finalization diagnostics carry IDs/counts/states/bounded sanitized details only | Pass |
| Process isolation | Failed old-generation closure leaves the new active generation usable and does not touch process-owned state | Pass |
| Dependency scope | No Cargo dependency or database schema change; existing `arc-swap` remains the only lifecycle dependency | Pass |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r003 --test runtime_lifecycle_r004 --test coordinator_c007 --test coordinator_c008 --test coordinator_c014  # 56 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets  # 322 passed, 33 suites
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 89 passed, 3 skipped
rtk uv run pytest tests/migration_rs/test_r001_runtime_lifecycle.py -q --tb=short --maxfail=1  # 6 passed
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

The implementation was committed as `fa3ab9d`; closure and dependency-state
updates are recorded in the follow-up documentation commit.

## Supported structural difference

Rust uses one tracked Tokio retirement task per accepted old slot and a bounded
`VecDeque` diagnostic summary, while the Python oracle uses its own lifecycle
supervisor/condition structure. This is an internal normalization: accepted
work remains generation-pinned, the old slot is not force-closed, finalization
drains before provider transport close, and the active generation is isolated
from old-generation close failures.

R004 does not add config parsing, reload policy, background task inventory,
signal handling, process shutdown semantics, control CLI, or database schema.

## Unresolved findings

No unresolved R004 correctness, security, resource, or compatibility finding
remains. R009 will consume the reusable retirement/drain primitives for process
shutdown; R008 will supply generation-local task ownership beyond the current
no-op generation-task close hook. No live provider or paid network fixture was
required.

## Future-plan audit and registry transition

R004 is removed from the dependency-ready table and recorded in the completed
implementation table with commit `fa3ab9d` and this accepted closure record.
R005 is promoted to the sole dependency-ready M8 plan because its hard
dependency, accepted R004 closure, is satisfied. R006-R011 remain serially
blocked behind their immediate predecessors. M9 remains blocked on accepted
R011 M8 closure and its separate planning/implementation review.

No other future plan can be safely unblocked by R004 alone. In particular,
R006-R011 depend on the config/reload, task, transactional reload, maintenance,
shutdown, authority, and aggregate qualification slices that follow R005. No
M8 roadmap closure, M9 eligibility, operational CLI, signal, or process-shutdown
status is promoted by this record.
