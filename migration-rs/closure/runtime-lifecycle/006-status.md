# R006 Closure — Process Task Supervisor and Authoritative Task-Spec Staging

Status: closed

Recommendation: closed

Implementation commit: [`bc1220f`](https://github.com/eggstack/eggpool/commit/bc1220f)

Plan: [R006 — process task supervisor and authoritative task-spec staging](../../implementation/runtime-lifecycle/006-process-task-supervisor-and-task-spec-staging.md)

## Outcome

R006 adds one process-owned `RuntimeTaskSupervisor` and makes it part of
`ProcessRuntime`, so cloned process handles address one bounded task map. The
supervisor owns one loop and one bounded cancellation channel per enabled task;
ticks are serialized, scheduled from completion/failure, and isolated from
callback errors, timeout, panic/join failure, cancellation, and shutdown.

`RuntimeTaskSpec` and the Rust inventory mirror the six R001 task names,
ownership classes, dependency metadata, callback kinds, and schedule defaults.
Config resolution preserves canonical order and disabled rows without creating
loops. The callback registry makes capability availability explicit: R006
provides the process-owned SQLite checkpoint callback, while generation
maintenance, update-check, and backup callbacks remain typed missing
capabilities for R008/M9 rather than silently becoming no-ops.

`prepare_diff_with_callbacks` validates duplicate names, schedule bounds,
first-run conflicts, callback capability, and ownership before allocating
prepared task state. `PreparedTaskDiff` exposes deterministic added/removed/
rescheduled/unchanged sets, has no running side effects before commit, and
supports idempotent rollback/discard. Commit stops and replaces only affected
loops. Generation-leased loops resolve the current `RuntimeManager` handle at
each tick, acquire a `GenerationLease`, release it before the next sleep, and
respond to admission-gate cancellation without leaking leases.

## Requirement-to-evidence matrix

| R006 requirement | Evidence | Result |
|---|---|---|
| R001 inventory names/order/metadata and resolved defaults | `inventory_and_resolved_defaults_match_r001` compares the Rust inventory and default resolved rows to `r001-python-observations.json` | Pass |
| Singleton, non-overlapping process loops | `slow_process_callback_never_overlaps_itself`; one task name maps to one `TaskState`/join handle | Pass |
| Disabled tasks own no loop | `disabled_specs_own_no_loop_and_repeated_changes_stay_bounded` | Pass |
| Initial delay and immediate scheduling | inventory fixture comparison plus staged/immediate callback tests | Pass |
| Timeout/error isolation and continued scheduling | `timeout_and_callback_error_do_not_stop_future_ticks`; typed `TaskOutcome` diagnostics | Pass |
| Panic/join failure observation | callback invocation runs under a child join handle and maps join failure to `Panicked` | Pass |
| Side-effect-free staged diff | `staged_diff_is_idle_until_commit_and_rollback_is_side_effect_free` | Pass |
| Deterministic affected sets and bounded replacement | `TaskSpecDiff`, transition assertions, and `disabled_specs_own_no_loop_and_repeated_changes_stay_bounded` | Pass |
| Generation lease per tick and no stale capture | `generation_task_leases_the_current_generation_each_tick` | Pass |
| Gate wait/cancellation without lease leak | `cancellation_while_generation_admission_is_closed_leaks_no_lease` | Pass |
| Bounded diagnostics and shutdown | `RuntimeTaskSnapshot`, bounded task map, `TaskShutdownReport`, and shutdown assertions | Pass |
| Explicit deferred M9 callbacks | `deferred_callbacks_are_explicit_missing_capabilities` | Pass |
| Process-owned SQLite callback | `Database::checkpoint` and `TaskCallbackRegistry::with_checkpoint` | Pass |
| Dependency scope/no scheduler framework | existing Tokio/standard-library primitives only; no Cargo dependency or schema change | Pass |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check       # passed
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings  # passed
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1  # 10 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets          # passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1       # 89 passed, 3 skipped
rtk git diff --check                                                   # passed
```

## Supported structural differences

Rust uses a small callback registry plus an explicit `Notify` wake path around
Tokio task joins, while the Python oracle uses `SupervisedTask` objects and
`asyncio` task cancellation. This is an internal normalization: the
observable inventory, singleton/non-overlap scheduling, timeout/error
continuation, generation lease boundary, staged side effects, and bounded
diagnostics remain equivalent. Tokio paused-time support was not required by
the implementation; the focused tests use bounded barriers and short local
durations.

R006 intentionally does not register deferred catalog/retention/update/backup
business callbacks. R008 owns generation-dependent maintenance integration;
M9 owns update/backup operational capabilities. No placeholder callback is
used, and no second scheduler is introduced.

## Unresolved findings

No unresolved R006 correctness, resource, security, compatibility, or
dependency finding remains. R007 can consume the staged task-diff API during
its short acceptance window. R008 remains responsible for plugging in the
real generation-leased maintenance callbacks.

## Future-plan audit and registry transition

R006 is removed from the dependency-ready table and recorded in the completed
implementation table with implementation commit `bc1220f` and this accepted
closure record. R007 is promoted to the sole dependency-ready M8 plan because
its hard dependency, accepted R006 closure, is satisfied.

R008–R011 remain queued behind their immediate predecessors: R007 must first
establish transactional rehash, then R008 integrates maintenance/recovery,
R009 owns process signals and shutdown, R010 audits authority/diagnostics, and
R011 performs aggregate M8 qualification. M9 remains blocked on accepted R011
M8 closure and its separate planning/implementation review. No other future
plan can be safely unblocked by R006 alone.
