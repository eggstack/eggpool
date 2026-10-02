# Deep Dive: SQLite and Repositories

Back to [Architecture](README.md). See also [overview.md §9](overview.md), [Runtime](deep-dive-runtime.md), and [Background](deep-dive-background.md).

## Ownership

`rust/src/db/` is the only persistence boundary: `connection.rs` (serialized `Database` gate, caller-owned transactions), `migrations.rs` (checksum-validated runner), `repositories.rs` (typed account/catalog/model/request/ping/dashboard/usage access), `mod.rs` (facade), and feature-gated `qualification.rs` (tooling-only in-memory collector, compiled only with `qualification-db-diagnostics`). `db/` owns only the `backup_to` snapshot primitive; orchestration lives in `operations/backup.rs`, and crash repair lives in `runtime_lifecycle/recovery.rs` plus coordinator reconciliation. `Database::open` is pinned to `tokio-rusqlite 0.8.0` (`bundled`, `backup` features) over `rusqlite 0.40.2` / `libsqlite3-sys 0.38.2`.

## Connection and gate

One `Database` holds one async connection behind one semaphore permit (`gate`) plus one worker. `DatabaseConfig::default` is WAL with `synchronous = "NORMAL"`; `configure` enforces `foreign_keys = ON`, the busy timeout, WAL mode (verified on file databases), synchronous level, and the optional journal-size limit. `call` runs one fenced closure; `with_transaction` runs `BEGIN IMMEDIATE` / body / `COMMIT`-or-`ROLLBACK` as one unit; `begin_transaction` returns a caller-owned `DatabaseTransaction` (`call`, `commit`, `rollback`) whose gate permit is held until finished — the primitive behind the reload acceptance window. Ordinary repository writes use `with_transaction`, never the long-lived handle.

## Failure and integrity contract

Commit/rollback ambiguity fails closed: body failure maps to `Transaction`/`Sqlite`, rollback failure to `RollbackFailed` (marks closed, closes the connection), commit failure to `CommitFailed` (closes only when the safety rollback also failed). `MigrationChecksumMismatch`, `UnknownMigration`, `MigrationNameMismatch`, and `ReadOnlyMigration` refuse startup before use. `quick_check` (`PRAGMA quick_check = ok`), `vacuum`, `checkpoint` (passive), `checkpoint_maintenance` (opportunistic, below), and `cleanup_retention` (bounded per-tick batches under `RetentionCleanupPolicy`, never selecting pending requests or active reservations) complete the surface. Repositories never store credentials, prompts, raw bodies, or cache keys, and accept no unbounded diagnostic content. Compatibility fixtures under `tests/fixtures/` are test-only.

## Migrations

Migrations are embedded from `rust/assets/db/migrations/` with `checksums.json`: v1–v54, 54 files, validated by `MigrationRunner::validate_embedded_checksums` before any database use (`canonical_inventory_is_complete_and_immutable` guards first = 1, last = 54, len = 54). `MigrationRunner::run` keeps the `_migrations` ledger, rejects unknown versions and ledger-name mismatches, refuses read-only migration, and applies all pending migrations in one atomic transaction. History is never rewritten or renumbered; restore validation additionally checks the staged ledger against this inventory.

## Publication and finalization transactions

Durable publication prepares its deterministic routing-decision row before acquiring the gate (persistence M002): `PreparedRoutingDecisionRow` in `coordinator/publication.rs` serializes exclusion/selected-score facts from the borrowed selection snapshot, and only those minimal owned facts enter the worker transaction. Publication and durable finalization use named `TransactionKind::{Publication, Finalization, Other}` entry points; the public `with_transaction` API and the single gate are unchanged.

## Passive checkpoint maintenance (M001)

`Database::checkpoint_maintenance` runs on the same gate/worker with a crate-private `CheckpointMaintenancePolicy` (soft WAL-frame threshold, default 256) and returns `CheckpointMaintenanceOutcome` (`not_due` / `gate_busy` / `below_threshold` / `checkpointed` plus scalar frame counts): idle ticks perform no SQLite work (in-memory durable-transaction watermark), busy-gate ticks defer via `try_acquire`, and `PRAGMA wal_checkpoint(PASSIVE)` runs only when the soft threshold is due (`NOOP` observation otherwise). The 1000-page `wal_autocheckpoint` ceiling stays the hard fallback; no checkpoint work runs inside publication/finalization code, and no public config/CLI/HTTP surface is added. The task polls every 60s (`CHECKPOINT_POLL_INTERVAL_S`); feature-only overrides follow Plan 239 rules (validated at startup, absent from ordinary builds).

## Qualification diagnostics (Plans 238/239, tooling only)

Plan 238's `scripts/qualification_sbc.py --diagnose-publication-storage` waits for task quiescence, runs bounded sequential native finite requests, and retains only scalar page-size/checkpoint-sequence facts. Plan 239's `qualification-db-diagnostics` feature adds a bounded 256-entry in-memory collector (`RECORD_CAPACITY`, `sqlite-db-phase.v1`) keyed by transaction kind with monotonic sequence numbers; `EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES` (`0..=100000`) applies once at startup and never enters `Config`, reload policy, or production defaults. The authenticated `/api/stats/runtime` projection exposes the snapshot only in feature builds. Plan 240 authorizes no runtime change: single connection/gate, WAL/NORMAL, existing ownership, and the passive maintenance boundary stay as-is pending a reviewed design.

## M004 checkpoint context

Persistence M004 (`plans/closure/persistence/004-status.md`) collected 14 accepted Pi 5 / ext4 / MMC artifacts and rejected the periodic 60s/256 strategy on the target class: three 60-request phase runs showed maxima of 1709 ms, 561 ms, and 10 943 ms with foreground publication `COMMIT` owning 522 ms–10.9 s of automatic-checkpoint work, and ordinary 30-sample benchmarks cannot complete against the runner's 5 s per-request timeout. The M001 mechanism is retained as additive-safe with no retune; persistence M003's event-assisted wake was separately rejected and reverted after transferring 1.88–3.30 s stalls into finalization gate wait. Persistence M007 is registered as a qualification-only experiment of a dedicated checkpoint connection/worker; the production invariant below remains one connection/gate/worker until target evidence and a separate architecture decision justify otherwise.

## Invariants

- One connection, one serialized gate, one worker; WAL/NORMAL; caller-owned transactions.
- Migrations immutable and checksum-pinned; unknown or mismatched ledgers fail closed.
- `backup_to` is a primitive; orchestration, validation, and restore stay in `operations/backup.rs`.
- Startup reconciliation repairs only covered states; ambiguity never resolves silently.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
```
