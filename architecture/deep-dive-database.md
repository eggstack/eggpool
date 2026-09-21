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
