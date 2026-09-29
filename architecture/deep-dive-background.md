# Deep Dive: Background Tasks

Back to [Architecture](README.md)

`rust/src/task_supervisor.rs` manages startup and periodic work.
`RuntimeTaskSpec { interval_s, initial_delay_s, run_immediately, timeout_s,
ownership, enabled, ... }` is the immutable, secret-free task description;
`TaskOwnership::{Process, ActiveGenerationLeased}` decides whether a tick
runs with a process marker or leases the active generation. Tasks are
either process-owned, surviving safe generation swaps, or
generation-leased,
retiring with the generation that created them. The frozen
`RUNTIME_TASK_NAMES` inventory has six rows; `runtime_task_inventory()` sets
defaults and `runtime_task_specs_for_config()` applies config gates.

## Task classes

Process-owned tasks cover opportunistic SQLite WAL checkpointing
(`checkpoint`, every 60 seconds; the tick skips cheaply when no durable
transaction completed since the previous tick and defers rather than
queueing when the single database gate is busy, per persistence M001 —
M004 (`plans/closure/persistence/004-status.md`, closed against HEAD
`8113d264`) collected 14 accepted Pi 5 / ext4 / MMC physical artifacts and
**rejected the periodic strategy** on the target class, so the 60s/256-frame
default is now conservative and unproven-on-target rather than target-
accepted; the landed mechanism is retained as additive-safe per Plan 240 §9
and M004 §6.2. Persistence M003 subsequently tested an event-assisted wake in this same process-owned task, but paired Pi/MMC qualification showed 1.88–3.30 second foreground finalization gate waits; the implementation was rejected and reverted, so the shipped checkpoint task remains timer-only),
metrics flushing (`metrics_flush` via `operations/metrics.rs::
MetricsWriteCoalescer`, skipped when `metrics.write_mode = "immediate"`),optional update checking (`update_checker` via
`operations/update.rs::UpdateCheckerState` + `register_update_checker`),
and optional automatic backups (`automatic_backup`, gated by
`[backup].enabled`/`interval_s`/`startup_delay_s`). Generation-leased tasks cover catalog
refresh (`catalog_refresh`, also the opportunity
for bounded model-info enrichment; there is no separate model-info scheduler)
and retention/reconciliation (`retention_cleanup`).

The supervisor uses fixed-delay scheduling: the next interval starts after the
previous tick completes. Task inventory and operational profiles are bounded
diagnostics, not a performance benchmark or a runtime authority.

## Recovery and shutdown

Startup reconciliation repairs interrupted requests and expired reservations
under the database transaction contract. Backup and metrics tasks preserve
bounded queues and report failures without losing the active generation.
Shutdown joins request, finalization, and generation-owned work before closing
the process-owned database.

## Update authority

`rust/src/operations/update.rs` (separate Hyper/Rustls owner: public release
metadata over its own client, never provider routing, proxy, or credentials;
SHA-256 verified artifacts, staged executable self-check, atomic rename with
rollback) separates conservative background freshness
probes from explicit exact-version resolution against
`operations/catalog.rs::ReleaseCatalog`. Latest/default resolution is
Rust-only; a historical Python release is considered only when an operator
requests an exact catalogued compatible target.
