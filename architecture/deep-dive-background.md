# Deep Dive: Background Tasks

Back to [Architecture](README.md). See also [overview.md §10](overview.md) and [Runtime](deep-dive-runtime.md).

## Ownership

`rust/src/task_supervisor.rs` is the sole owner of supervised task handles, callback registration, task-spec diffs, and bounded task shutdown. `ProcessRuntime::task_supervisor` holds the single process-owned `RuntimeTaskSupervisor`; `ReloadService` stages `PreparedTaskDiff` values against it, and `close_runtime_resources_until` shuts it down inside the foreground deadline. Business logic lives in callbacks (`TaskCallbackRegistry`); the supervisor owns only scheduling and lifecycle state.

## Task description and ownership

`RuntimeTaskSpec { name, interval_s, initial_delay_s, run_immediately, timeout_s, ownership, enabled, description, reloadable_fields, generation_dependencies, process_dependencies, callback_kind }` is the immutable, secret-free task description. `TaskOwnership::{Process, ActiveGenerationLeased}` (plus reserved `Unsupported`, rejected before commit) decides what one tick receives: `TaskTickContext::Process` runs with a process marker and survives generation swaps, while `TaskTickContext::Generation(lease)` acquires a fresh active-generation lease per tick and drops it before the next sleep. No long-lived loop ever captures a generation.

## Six-row inventory

`RUNTIME_TASK_NAMES` is frozen at six rows; `runtime_task_inventory()` sets defaults and `runtime_task_specs_for_config()` applies config gates (disabled rows stay visible for deterministic diffs but own no loop):

| Name | Ownership | Default cadence | Gate |
|---|---|---|---|
| `catalog_refresh` | generation-leased | 300s | `models.refresh_interval_s` (0 disables) |
| `retention_cleanup` | generation-leased | 86400s | `metrics.cleanup_interval_s` |
| `checkpoint` | process | 60s (`CHECKPOINT_POLL_INTERVAL_S`) | always on |
| `metrics_flush` | process | 30s, 5s delay | skipped when `metrics.write_mode = "immediate"` |
| `update_checker` | process | 86400s, immediate | `update_checker.enabled` |
| `automatic_backup` | process | 86400s, 300s delay | `[backup].enabled`/`interval_s`/`startup_delay_s` |

`catalog_refresh` is also the bounded model-info enrichment opportunity; there is no separate model-info scheduler.

## Scheduling and supervision

Scheduling is fixed-delay: the next interval starts after the previous tick completes (`run_task` records `TaskOutcome`: `Success`/`Error`/`TimedOut`/`Panicked`/`GenerationUnavailable`/`Cancelled`, then waits). Generation-leased ticks acquire the manager per tick and exit quietly on `ShuttingDown`; admission closure during a staged swap surfaces as `GenerationUnavailable`, never as fabricated success. `prepare_diff` validates specs (names, intervals, callback capabilities, ownership) and pre-allocates callback/channel state; `commit` applies only added/removed/rescheduled rows; `rollback` discards a staged diff synchronously, while `rollback_committed` restores the pre-commit set after a late SQLite compensation. `TaskTransition` and `TaskShutdownReport` are the bounded, secret-free evidence.

## Callbacks

`TaskCallbackRegistry::with_checkpoint` registers the opportunistic WAL-maintenance tick (idle ticks skip via one in-memory transaction counter; busy-gate ticks defer via `try_acquire` rather than queueing). `with_generation_maintenance` adds catalog refresh and retention cleanup. `register_metrics_flush` (via `operations/metrics.rs::MetricsWriteCoalescer`), `register_update_checker` (via `operations/update.rs::UpdateCheckerState`, check-only probes), and `register_automatic_backup` (re-resolves config per tick so reloads change directory/retention without capturing a retiring generation) complete the set. `capability_inventory` keeps deferred rows explicit instead of silently installing no-ops.

## Checkpoint qualification context

Persistence M004 (`plans/closure/persistence/004-status.md`, closed against HEAD `8113d264`) collected 14 accepted Pi 5 / ext4 / MMC physical artifacts and rejected the periodic 60s/256-frame checkpoint strategy on the target class: maintenance ticks do not fire inside short finite bursts, so the foreground publication `COMMIT` still owns the 1000-page automatic-checkpoint ceiling. The landed mechanism is retained as additive-safe per Plan 240 §9 with no constant retune authorized. M003 then tested commit-driven wakeups on that same gate/worker; it removed publication-COMMIT tails but transferred 1.88–3.30 second stalls into finalization gate wait and was reverted. M007 is the registered qualification-only experiment for a dedicated checkpointer worker; ordinary builds retain the current timer-only single-connection topology.

## Invariants

- One supervisor, fixed-delay loops, per-tick leases; generations are never captured.
- Task inventory and operational profiles are bounded diagnostics, not a performance benchmark or runtime authority.
- Backup and metrics tasks use bounded queues and report failures without losing the active generation.
- Shutdown joins supervised work before generation close and database close.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
```
