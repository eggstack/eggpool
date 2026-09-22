# Deep Dive: SQLite and Repositories

Back to [Architecture](README.md)

`rust/src/db/` owns the SQLite connection, migration runner, repositories,
backup state, and recovery boundaries. Migrations and their checksums are
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

See `rust/src/db/connection.rs`, `migrations.rs`, and `repositories.rs`.

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

Plan 239 classified the MMC tail as I1 (foreground SQLite automatic-checkpoint
work). Plan 240 is the separate production design handoff and authorizes no
runtime change: the single connection/gate, WAL mode, `synchronous = "NORMAL"`,
and the existing maintenance-task `PRAGMA wal_checkpoint(PASSIVE)` boundary
remain the production policy. Any future passive-checkpoint scheduling must
stay on the existing DB worker (no second writer), bound WAL growth across
restart/reload/backup/restore/recovery, and keep qualification diagnostics out
of ordinary release builds.

## Passive-checkpoint production follow-up (Plan 240, design only)

Plan 239 classified the Pi MMC tail as I1 (foreground SQLite automatic
checkpoint work). Plan 240 is the separate production design handoff and
authorizes no runtime change: the single connection/gate, WAL with
`synchronous = "NORMAL"`, existing publication/finalization ownership, and
the pre-existing passive-checkpoint maintenance task remain the production
policy. Any bounded passive-checkpoint scheduling proposal must preserve
durability, bound WAL growth across restart/reload/backup/restore/recovery,
keep diagnostics out of ordinary builds, and arrive with loopback evidence
before a default change is considered.

## Passive-checkpoint production follow-up (Plan 240, design only)

Plan 239 classified the MMC tail as I1 (foreground SQLite automatic-checkpoint
work). Plan 240 is the separate production design handoff and authorizes no
runtime change: the single connection/gate, WAL mode, `synchronous = "NORMAL"`,
and the existing maintenance-task `PRAGMA wal_checkpoint(PASSIVE)` boundary
remain the production policy. Any future passive-checkpoint scheduling must
stay on the existing DB worker (no second writer), bound WAL growth across
restart/reload/backup/restore/recovery, and keep qualification diagnostics out
of ordinary release builds.
