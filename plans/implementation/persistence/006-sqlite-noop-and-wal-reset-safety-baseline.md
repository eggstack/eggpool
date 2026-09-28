# Persistence Milestone 006 — SQLite NOOP and WAL-Reset Safety Baseline

Status: ready

Repository baseline: `2fd4bce10822cc66f576a0f5054fe4dcab495403`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-006--sqlite-noop-and-wal-reset-safety-baseline`

Long-term requirements:

- `plans/000-long-term-specification.md` — persistence ownership and fail-closed durability invariants
- `plans/002-long-term-roadmap.md` — Phase 3 persistence bounds
- `plans/003-planning-process.md` — dependency and closure evidence rules

Related persistence evidence:

- `plans/closure/persistence/004-status.md`
- `plans/closure/persistence/005-status.md`
- `tests/tooling/test_persistence_m004_evidence.py`

External research references:

- SQLite 3.51.0 release notes — `https://sqlite.org/releaselog/3_51_0.html`
- SQLite WAL documentation / WAL-reset bug — `https://sqlite.org/wal.html`
- SQLite 3.51.3 release notes — `https://sqlite.org/releaselog/3_51_3.html`
- SQLite WAL hook / auto-checkpoint contract — `https://sqlite.org/c3ref/wal_hook.html`, `https://sqlite.org/c3ref/wal_autocheckpoint.html`
- tokio-rusqlite 0.8.0 — `https://docs.rs/crate/tokio-rusqlite/0.8.0`
- rusqlite/libsqlite3-sys 0.40.2/0.38.2 bundled-engine notes — `https://docs.rs/crate/libsqlite3-sys/0.38.2`

Applicable ADRs:

- None required. This is an in-family dependency/engine safety upgrade that preserves the existing SQLite authority, schema, durability policy, and public surface.
- Stop for architecture review if implementation would require replacing tokio-rusqlite/rusqlite, changing WAL/NORMAL, adding a connection, or changing public configuration.

Primary class: invariant

## 1. Objective

Establish a SQLite baseline on which EggPool's checkpoint observation primitive is truly non-mutating and the 2026 WAL-reset corruption bug is fixed before persistence M003 introduces event-driven checkpoint scheduling.

The target resolved graph is:

- tokio-rusqlite 0.8.0;
- rusqlite 0.40.2 (or the compatible 0.40.x version selected by the reviewed lock update);
- libsqlite3-sys 0.38.2;
- bundled SQLite 3.53.2 or a later reviewed fixed release on the same compatible line.

The milestone must preserve:

- one SQLite connection;
- one EggPool database gate;
- one tokio-rusqlite worker;
- WAL + `synchronous=NORMAL`;
- ordinary `wal_autocheckpoint=1000`;
- schema 54 and all durable row semantics;
- the existing process-owned checkpoint task;
- no new public API/config/CLI/HTTP behavior.

## 2. Why this milestone is ready

M005 is closed and the prerequisite defect is repository-verifiable.

Current `rust/Cargo.toml` selects `tokio-rusqlite = 0.7.0` with `bundled,backup`. The current lock resolves:

- tokio-rusqlite 0.7.0;
- rusqlite 0.37.0;
- libsqlite3-sys 0.35.0;
- bundled SQLite 3.50.2.

SQLite added `PRAGMA wal_checkpoint(NOOP)` / `SQLITE_CHECKPOINT_NOOP` in 3.51.0. In the pre-NOOP pragma implementation, checkpoint mode defaults to PASSIVE and only FULL/RESTART/TRUNCATE override it. Therefore EggPool's current `query_wal_progress()` call to `PRAGMA wal_checkpoint(NOOP)` is not a pure observation on the locked 3.50.2 engine: it resolves through the older PASSIVE behavior.

This matters because M001's intended algorithm is:

1. observe WAL frames without checkpointing;
2. compare against the 256-frame soft threshold;
3. run PASSIVE only when due.

M003 must not be built on an engine where step 1 can itself checkpoint.

SQLite also documents the WAL-reset corruption bug as likely present through 3.51.2 and fixed in 3.51.3+. EggPool's one-connection/gate design avoids the documented internal two-connection race shape, but the bundled 3.50.2 engine remains on an affected line and M003 will increase application-initiated checkpoint activity. Upgrade before that change.

tokio-rusqlite 0.8.0 uses rusqlite ^0.40.1. rusqlite 0.40.2 / libsqlite3-sys 0.38.2 bundles SQLite 3.53.2 and is compatible with EggPool's Rust 1.89 MSRV (rusqlite 0.40.2 lowered its MSRV to 1.88).

## 3. Current implementation evidence

Authority paths:

- `rust/Cargo.toml` — tokio-rusqlite feature owner.
- `rust/Cargo.lock` — resolved rusqlite/libsqlite3-sys/SQLite dependency evidence.
- `rust/src/db/connection.rs`:
  - `query_wal_progress()` issues `PRAGMA wal_checkpoint(NOOP)`;
  - `run_passive_checkpoint()` issues `PRAGMA wal_checkpoint(PASSIVE)`;
  - `checkpoint_maintenance()` intends observation-before-threshold-before-PASSIVE;
  - production auto-checkpoint remains 1000 pages.
- `rust/tests/database_compatibility.rs` — WAL/NORMAL/autocheckpoint/backup/close compatibility.
- `rust/tests/runtime_lifecycle_r008.rs` — single process-owned checkpoint-task invariant.
- `rust/src/operations/backup.rs` — same-connection SQLite online backup owner.
- `rust/assets/db/migrations/` — schema ledger authority.

Compatibility hazard in the 0.38+ rusqlite line:

- rusqlite 0.38 disabled `u64`/`usize` ToSql/FromSql implementations by default;
- `fallible_uint` restores checked unsigned conversions;
- tokio-rusqlite 0.8.0 does not forward a `fallible_uint` feature.

Implementation must use compile/search evidence to determine whether EggPool has a live unsigned SQL boundary. Do not add `bundled-full` to paper over errors.

## 4. Invariants that must not regress

- Exactly one SQLite connection/gate/worker.
- No custom `sqlite3_wal_hook`.
- No change to `wal_autocheckpoint=1000`.
- No FULL/RESTART/TRUNCATE checkpoint in ordinary runtime.
- No schema or migration change.
- No durable row/value semantic change.
- No public config, CLI, HTTP, or Rust API change.
- No system-SQLite dependency: retain the bundled engine.
- `backup_to`, restore validation, startup recovery, reload, and shutdown remain compatible.
- Default and `--no-default-features` builds retain parity.
- Rust 1.89 remains supported.
- Qualification-only database instrumentation remains non-default and dependency-free.

## 5. Scope

### In scope

- Upgrade tokio-rusqlite to 0.8.0.
- Resolve and review the new rusqlite/libsqlite3-sys graph and bundled SQLite version.
- Handle any rusqlite 0.38+ unsigned-integer compatibility issue narrowly.
- Add a regression test proving NOOP does not advance checkpoint progress.
- Add a test/assertion proving the bundled runtime SQLite is on a NOOP-capable, WAL-reset-fixed baseline.
- Requalify database, backup, recovery, lifecycle, and full workspace behavior.
- Update dependency/current-architecture documentation.

### Explicitly out of scope

- Event-driven checkpoint wakeups.
- New Tokio tasks or Notify objects.
- Checkpoint cadence/threshold changes.
- M003 commit-sequence semantics.
- New SQLite hooks/features.
- Public tuning controls.
- A second connection or worker.
- Schema changes.
- Performance acceptance claims for the Pi/MMC foreground tail.

## 6. Required production changes

### 6.1 Dependency graph

Update `rust/Cargo.toml` from tokio-rusqlite 0.7.0 to 0.8.0 while keeping only the features EggPool owns today:

- `bundled`;
- `backup`.

Do not enable:

- `hooks`;
- `bundled-full`;
- `modern-full`;
- unrelated virtual-table/load-extension/session features.

Regenerate `rust/Cargo.lock` through Cargo. Closure must record the actual resolved versions and bundled SQLite version.

### 6.2 rusqlite unsigned-value compatibility

Run the migration against the existing code before adding compatibility features.

If the workspace compiles without `fallible_uint`, add no direct rusqlite dependency.

If an existing legitimate `u64`/`usize` SQL boundary fails because the impl moved behind `fallible_uint`:

1. inventory the exact call sites;
2. preserve the old checked semantics;
3. prefer the narrowest reviewed fix:
   - explicit checked conversion at the boundary when the DB schema is already signed `INTEGER`; or
   - direct feature unification on the exact compatible rusqlite line if multiple genuine unsigned SQL boundaries make that clearer;
4. do not use unchecked `as i64` casts for values that can exceed `i64::MAX`;
5. do not enable `bundled-full`.

Any direct rusqlite dependency must have a documented live owner and resolve to the same package version as tokio-rusqlite.

### 6.3 NOOP semantic guard

Add a file-backed WAL test under the existing database test boundary that:

1. opens the normal EggPool database with WAL/NORMAL and the 1000-page auto ceiling;
2. creates a bounded amount of WAL work well below 1000 pages;
3. records NOOP progress;
4. runs NOOP again and proves the observation did not advance the checkpointed-frame state;
5. demonstrates that an explicit PASSIVE checkpoint can advance/converge the same WAL afterward;
6. leaves backup and close green.

Do not disable automatic checkpointing in the production-path test to manufacture the result.

A qualification-only test may use a lower soft threshold when proving `checkpoint_maintenance()` selects PASSIVE only after its observational step.

### 6.4 Engine safety guard

Add a focused test that queries `sqlite_version()` from the bundled runtime and rejects an engine older than 3.51.3.

The closure must additionally record the exact engine selected by the lock/build. For the planned dependency graph that should be SQLite 3.53.2.

The test should express the minimum semantic/safety floor (NOOP support + WAL-reset fix), not unnecessarily pin a patch forever.

## 7. Ordered work packages

### Work package A — Dependency migration and feature audit

Intent:

Move the async SQLite stack to a current fixed engine with the same ownership/features.

Required changes:

- update tokio-rusqlite;
- regenerate lock;
- inspect `cargo tree -e features`;
- resolve any `fallible_uint` break narrowly;
- confirm no hooks/full feature creep.

Acceptance evidence:

- one tokio-rusqlite line;
- one rusqlite line;
- one libsqlite3-sys line;
- bundled SQLite >= 3.51.3;
- Rust 1.89 check green.

### Work package B — Prove observational NOOP

Intent:

Make M001/M003's observation-before-checkpoint assumption executable.

Required changes:

- add file-backed regression test;
- prove NOOP itself does not advance checkpoint state;
- prove PASSIVE remains the only explicit maintenance work primitive.

Acceptance evidence:

- test would fail against the old 3.50.2 behavior;
- effective WAL/NORMAL/1000 policy unchanged.

### Work package C — Persistence/lifecycle requalification

Intent:

Ensure the engine upgrade changes no durable behavior.

Required checks:

- database compatibility;
- coordinator publication/finalization;
- metrics persistence;
- backup/recovery;
- runtime lifecycle R006/R008;
- full default and no-default workspace;
- release build.

Acceptance evidence:

- no schema/config/API changes;
- recovery and online backup green;
- exactly one checkpoint process task remains.

### Work package D — Documentation and M003 unblock audit

Intent:

Make the dependency baseline explicit and prepare a clean M003 handoff.

Required changes:

- update `architecture/deep-dive-database.md` with the resolved engine and true-NOOP guarantee;
- update persistence roadmap current state;
- create `plans/closure/persistence/006-status.md`;
- audit M003 readiness.

M003 may move from `blocked` to `ready` only in a separate registry status-change commit after M006 closure is accepted.

## 8. Failure, cancellation, restart, contention semantics

No new asynchronous owner is introduced.

The dependency upgrade must preserve existing semantics:

- a transaction COMMIT/ROLLBACK error keeps the same fail-closed handling;
- database worker close remains terminal;
- checkpoint PASSIVE remains serialized through the same EggPool gate;
- backup remains on the same source connection/gate;
- shutdown still joins the existing process task before database close;
- reload keeps the same explicit caller-owned `DatabaseTransaction` boundary.

The NOOP regression test must not rely on timing races or external connections.

## 9. Compatibility and migration

No database migration.

SQLite database/WAL format remains compatible.

No operator migration.

No config or CLI migration.

The release artifact continues to bundle SQLite rather than depending on the host library.

Rust MSRV remains 1.89.

## 10. Required tests

Focused:

- `rust/src/db/connection.rs` unit tests;
- `rust/tests/database_compatibility.rs`;
- `rust/tests/coordinator_publication.rs`;
- `rust/tests/coordinator_finalization.rs`;
- `rust/tests/operations_o006.rs`;
- `rust/tests/operations_o007.rs`;
- `rust/tests/runtime_lifecycle_r006.rs`;
- `rust/tests/runtime_lifecycle_r008.rs`.

Broad:

- full workspace default features;
- full workspace `--no-default-features`;
- release build;
- Rust 1.89 check.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo tree --manifest-path rust/Cargo.toml -i tokio-rusqlite -e features
cargo tree --manifest-path rust/Cargo.toml -i rusqlite -e features
cargo tree --manifest-path rust/Cargo.toml -i libsqlite3-sys -e features
cargo test --manifest-path rust/Cargo.toml --lib db:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo +1.89.0 check --manifest-path rust/Cargo.toml --workspace --all-targets
git diff --check
~~~

If Rust 1.89 is not installed on the execution host, record that truthfully and do not claim the MSRV check ran; closure still requires equivalent CI or another environment before accepting an MSRV-sensitive dependency change.

## 12. Documentation updates

- `architecture/deep-dive-database.md`
- `plans/subsystems/persistence-roadmap.md`
- `plans/registry.md`
- dependency comments near `rust/Cargo.toml` only if needed to explain non-obvious feature ownership.

Do not rewrite M004/M005 historical closure records.

## 13. Acceptance criteria

M006 closes only when:

- tokio-rusqlite 0.8.0 is the resolved async wrapper;
- resolved bundled SQLite is >= 3.51.3 and closure records the exact version;
- the intended current target is rusqlite 0.40.2 / libsqlite3-sys 0.38.2 / SQLite 3.53.2 unless the lock resolves a later reviewed compatible patch;
- no custom WAL hook or hooks feature is introduced;
- NOOP is proven observational by regression test;
- explicit PASSIVE remains the maintenance checkpoint primitive;
- WAL/NORMAL/1000-page fallback unchanged;
- schema 54 unchanged;
- backup/recovery/reload/shutdown green;
- default and no-default workspace tests green;
- no public surface changes;
- Rust 1.89 compatibility is demonstrated or closure remains conditional/blocked on that evidence;
- no medium-or-higher unresolved finding remains.

## 14. Stop conditions

Stop and re-plan if:

- tokio-rusqlite 0.8 requires a second connection/worker or material API redesign;
- the resolved bundled engine is older than 3.51.3;
- true NOOP cannot be demonstrated;
- preserving unsigned SQL semantics requires broad feature activation or unchecked conversions;
- Rust 1.89 cannot support the chosen dependency line;
- backup/recovery/schema behavior changes;
- a custom SQLite hook appears necessary;
- scope expands into M003 scheduling.

## 15. Closure evidence required

`plans/closure/persistence/006-status.md` must include:

- implementation commit(s);
- exact Cargo.toml and Cargo.lock SQLite-stack versions;
- `SELECT sqlite_version()` / equivalent exact bundled engine evidence;
- feature-tree evidence showing `bundled,backup` ownership and no hooks/full creep;
- unsigned SQL boundary audit and disposition;
- NOOP non-mutation test result;
- PASSIVE checkpoint test result;
- WAL/NORMAL/1000-page policy evidence;
- schema/migration review;
- focused and full test results, distinguishing local from hosted CI;
- Rust 1.89 evidence;
- backup/recovery/reload/shutdown review;
- dependency-ready audit for M003;
- severity-tagged residual findings.

## 16. Handoff notes

This is a prerequisite, not the event-driven implementation.

Do not use a SQLite WAL hook as an alternative. SQLite's automatic checkpoint mechanism itself owns the WAL-hook slot; registering a custom WAL hook replaces/disables that safety mechanism.

Do not infer M003 success from this upgrade. M006 establishes trustworthy checkpoint semantics and a fixed SQLite safety baseline only.
