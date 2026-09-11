# Deep Dive: Background Tasks

Back to [Architecture](README.md)

`rust/src/task_supervisor.rs` manages startup and periodic work. Tasks are
either process-owned, surviving safe generation swaps, or generation-leased,
retiring with the generation that created them.

## Task classes

Process-owned tasks cover WAL checkpointing, metrics flushing, optional update
checking, and optional automatic backups. Generation-leased tasks cover catalog
refresh and retention/reconciliation. Catalog refresh is also the opportunity
for bounded model-info enrichment; there is no separate model-info scheduler.

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

`rust/src/operations/update.rs` separates conservative background freshness
probes from explicit exact-version resolution. Latest/default resolution is
Rust-only; a historical Python release is considered only when an operator
requests an exact catalogued compatible target.
