# Deep Dive: SQLite and Repositories

Back to [Architecture](README.md)

`rust/src/db/` owns the SQLite connection, migration runner, repositories,
and the consistent-snapshot `backup_to` primitive. Backup orchestration lives
in `rust/src/operations/backup.rs` and crash-repair hooks live in
`runtime_lifecycle/recovery.rs` plus coordinator reconciliation; the database
contributes the shared transaction/recovery contract. Migrations and their checksums are
embedded from `rust/assets/db/migrations/`; the runtime preserves the
historical schema ledger and schema 54 contract.

The database uses WAL and one serialized primary connection. Durable request,
attempt, reservation, usage, catalog, health, backup, and finalization writes
run inside explicit caller-owned transactions. Commit/rollback ambiguity fails
closed and startup reconciliation repairs only states covered by the recovery
contract.

Repositories are the only persistence boundary for runtime modules. They do
not open independent writer pools or accept raw unbounded diagnostic content.
Compatibility fixtures under `tests/fixtures/` are test-only and are never
loaded by the production executable.

See `rust/src/db/connection.rs`, `migrations.rs`, `repositories.rs`, `mod.rs`,
and feature-gated `qualification.rs` (only compiled with
`qualification-db-diagnostics`).

## Publication/storage qualification diagnostic

The tooling-only Plan 238 mode in `scripts/qualification_sbc.py` is not a
database runtime authority. On a physical Linux/aarch64 SBC it waits for the
fixed checkpoint, metrics-flush, catalog-refresh, retention-cleanup, and
automatic-backup task names to be quiescent, captures baseline/final tick
counts, and runs bounded sequential native finite requests. It reads file
sizes and at most the first 32 bytes of the WAL file after each request; the
report retains only scalar page-size/checkpoint-sequence facts and no database
path or raw header.

An optional diagnostic database directory puts only `usage.sqlite3` and its
WAL/SHM siblings on that filesystem while config, logs, runtime files, and
backup/recovery roots remain in the qualification root. This is a diagnostic
comparison, not a production placement recommendation or a durability
change. The mode uses the benchmark fixture directly and does not run the
ordinary benchmark corpus.

## Publication commit/checkpoint phase diagnostic

Plan 239's `qualification-db-diagnostics` Cargo feature is a non-default,
dependency-free qualification boundary. It adds a single bounded in-memory
collector to the existing `DatabaseInner`; records are limited to 256 fixed
scalar entries with monotonic sequence numbers and the fixed transaction kinds
`publication`, `finalization`, and `other`. The public
`Database::with_transaction` API and the single semaphore/connection remain
unchanged. Publication and durable finalization use named internal entry
points solely to label their records.

In a feature build, `Database::configure` queries effective `journal_mode`,
`synchronous`, `page_size`, and `wal_autocheckpoint` on that same connection.
The feature-only `EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES` startup
override accepts `0..=100000`; it is applied once during configuration and is
never part of `Config`, reload policy, CLI help, or production defaults. The
authenticated `/api/stats/runtime` projection exposes the bounded snapshot
only in this qualification build. Ordinary builds neither collect records nor
consult the environment variable.

`scripts/qualification_sbc.py --diagnose-publication-phases` waits for fixed
database-task quiescence, captures a record-sequence baseline, runs exactly 60
sequential native finite requests, and requires one successful publication and
finalization record per request. It retains phase summaries and correlated
scalars for only the five slowest requests; it rejects missing/duplicate
foreground records, failed requests, background task ticks, and ownership
non-convergence. H0 uses the effective default, H1 uses `0`, and H2 uses `256`
only when H0/H1 satisfy Plan 239's predicate. Neither override is a production
recommendation, and no explicit checkpoint is run inside the measured batch.

## Passive-checkpoint production follow-up (Plan 240, design only)

Plan 239 classified the Pi MMC tail as I1 (foreground SQLite automatic-
checkpoint work). Plan 240 is the separate production-design handoff and
authorizes no runtime change: the single connection/gate and DB worker, WAL
mode with `synchronous = "NORMAL"`, existing publication/finalization
ownership, and the pre-existing maintenance-task
`PRAGMA wal_checkpoint(PASSIVE)` boundary remain the production policy. Any
future scheduling proposal must preserve durability, stay on the existing DB
worker unless separately evidenced, bound WAL growth across
restart/reload/backup/restore/recovery, keep qualification diagnostics out of
ordinary release builds, and arrive with the focused loopback evidence required
by Plan 240 before a default change is considered.

## Bounded passive checkpoint scheduling (persistence M001)

The accepted M001 candidate keeps every Plan 240 invariant and makes the
existing process-owned `checkpoint` task opportunistic instead of periodic-
unconditional. `Database::checkpoint_maintenance` (in
`rust/src/db/connection.rs`) runs on the same gate/worker with a
crate-private `CheckpointMaintenancePolicy` (soft WAL-frame threshold, default
256) and returns a bounded `CheckpointMaintenanceOutcome`
(`not_due`/`gate_busy`/`below_threshold`/`checkpointed` plus scalar frame
counts):

- the tick first compares the in-memory durable-transaction counter against
  its last observed watermark, so an idle process performs no SQLite work;
- optional work uses `try_acquire` on the existing gate and defers when the
  gate is already owned instead of queueing behind foreground work;
- once the gate is owned, WAL frames are observed through SQLite itself
  (`PRAGMA wal_checkpoint(NOOP)`, no checkpoint work) and
  `PRAGMA wal_checkpoint(PASSIVE)` runs only when the soft threshold is due;
- the production `wal_autocheckpoint` safety ceiling (1000 pages) is
  unchanged and remains the hard fallback under bursts or repeated deferrals;
- no checkpoint work runs inside coordinator publication/finalization code,
  and no public config/CLI/HTTP/Rust surface is added.

The task polls every 60 seconds (`CHECKPOINT_POLL_INTERVAL_S` in
`rust/src/task_supervisor.rs`; wakeups are atomic-counter checks when idle).
Feature-only qualification overrides
(`EGGPOOL_QUALIFICATION_CHECKPOINT_INTERVAL_S` in 1..=3600 seconds,
`EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES` in 1..=1000 frames) follow the
Plan 239 rules: validated at database startup, absent from ordinary builds,
and recorded in the sanitized artifact. Maintenance counters and the
effective soft threshold ride the existing authenticated
`database_qualification.checkpoint_maintenance` projection; ordinary runtime
JSON is unchanged. `scripts/qualification_sbc.py
--diagnose-checkpoint-maintenance` (requires `--diagnose-publication-phases`)
records baseline/final projections with bounded tick deltas and exempts
checkpoint ticks from the Plan 239 contamination rule, since the maintenance
tick is the measured subject and per-record gate-wait phases already capture
any foreground wait behind PASSIVE work.
