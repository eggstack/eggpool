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
