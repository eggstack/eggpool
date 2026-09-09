# Q003 — Database Upgrade, Rollback, Backup, and Recovery Compatibility

Status: queued behind Q002

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q002.

Primary class: invariant

Hard dependency: accepted Q002.

## Objective

Qualify the completed Rust database/repository/operations stack as a migration boundary, not only as isolated SQL tests. Q003 must prove supported Python-to-Rust upgrade and Rust-to-Python rollback readability, ordinary Rust writes, backup/recovery, and faulted recovery without reset or data loss.

## Authoritative sources

Inspect current:

- numbered SQL migrations/checksums and F004 closure;
- Python DB/migration/repository modules and tests;
- Rust `db/`, `MigrationRunner`, request/finalization repositories;
- O006 backup/recovery implementation and closure;
- O007 maintenance/stat repair operations;
- C010 restart reconciliation;
- Q001 DB transition cells and Q002 aggregate lifecycle observations.

## Fixture set

Create or reuse compact deterministic DB fixtures representing:

- empty/pre-initialized database;
- one or more historically relevant migration snapshots still supported by the Python upgrader;
- current Python-created latest schema with representative accounts/catalog/requests/attempts/reservations/routing/metrics/model-info rows;
- current Rust-created latest schema with equivalent representative rows;
- WAL-active database with recent committed writes;
- backup archive produced by Python where current contract supports Rust recovery;
- backup archive produced by Rust for Python inspection/recovery where supported.

Do not commit sensitive real databases.

## Required transitions

### Python -> Rust

For every supported source snapshot:

1. copy fixture to an isolated root;
2. record schema/migration/checksum/table/index facts and selected row hashes;
3. run Rust migration/startup/reconciliation;
4. execute representative Rust writes: finite + streaming request accounting, routing decision, metrics, catalog/model-info, config-independent operator maintenance;
5. checkpoint/close cleanly;
6. verify schema/checksums and semantic row projections.

### Rust -> final Python reference

Where rollback is expected to remain supported:

1. open the Rust-mutated database with Python without reset;
2. run Python schema/checksum validation;
3. read representative rows through Python repositories/API projections;
4. confirm no Rust-only enum/value/schema assumption prevents use;
5. run a bounded ordinary Python write/read cycle if safe.

If a transition is intentionally one-way, it must already be authorized by canonical migration policy or an ADR; M10 may not invent a one-way boundary for convenience.

## Backup/recovery matrix

Qualify:

- Rust backup from a live WAL database via SQLite online backup;
- archive manifest/member validation;
- config/env inclusion policy;
- archive collision handling;
- restore into a fresh root;
- restore over an existing valid root using the reviewed atomic replacement path;
- Python inspection of Rust backup contents;
- Rust recovery of supported Python backup archives if format parity claims it;
- retention/prune behavior after successful publication only;
- backup/recover followed by server startup and real deterministic inference.

## Fault matrix

Inject failures at bounded test hooks or filesystem fixtures around:

- migration begin/apply/commit;
- backup snapshot timeout/failure;
- archive write/final rename;
- malformed/traversal/duplicate/oversized archive members;
- invalid staged config;
- invalid/corrupt staged SQLite database;
- restore target validation;
- config/env/database replacement order;
- replacement/rollback failure paths;
- process restart after interrupted-looking but converged state.

For each fault, assert that the only previously usable config/DB copy is not destroyed and that the next startup has a deterministic recovery/error path.

## Durable compatibility observations

Compare bounded semantic facts, including:

- migration ids/checksums;
- `PRAGMA user_version` or authoritative migration ledger facts;
- required tables/indexes/triggers;
- request/attempt/reservation terminal state;
- cost/token/cache/reasoning counters;
- routing decisions/effects;
- account/catalog/model-info rows;
- metrics/rollups needed by dashboard/operator paths;
- backup metadata version/member set.

Avoid byte-for-byte SQLite file comparison.

## Resource/size observations

Record, but do not invent hard thresholds for:

- DB size before/after migration;
- WAL size after bounded workload/checkpoint;
- backup archive size;
- backup and restore elapsed time on the local qualification host.

Unexpected unbounded growth or failure to checkpoint is a finding.

## Required tests

Add focused Q003 tests for each supported transition and every fault class. At least one test must prove the complete sequence:

`Python DB -> Rust migrate/write/backup -> Rust restore -> Python reopen/read`.

Also prove a failed restore leaves the original database/config usable.

## Verification

Run at minimum:

```text
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# focused Python migration/backup/database suites named by implementation
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

## Non-goals

Q003 does not add migrations for qualification convenience, optimize SQLite pragmas without evidence, change backup format casually, or perform M11 cutover.

## Closure evidence

Write `migration-rs/closure/qualification/003-status.md` with:

- fixture/source versions and hashes;
- transition matrix results;
- Python->Rust and Rust->Python supported rollback statement;
- backup/recovery cross-implementation matrix;
- fault-injection results;
- size/time characterization;
- schema/dependency changes (expected none);
- unresolved findings and registry transition.

## Acceptance criteria

Q003 closes only when all Q001 mandatory DB transitions succeed, faulted backup/recovery preserves a usable prior state, no reset/schema fork is needed, and no unresolved high/medium data-loss or compatibility finding remains.

Accepted Q003 promotes only Q004.