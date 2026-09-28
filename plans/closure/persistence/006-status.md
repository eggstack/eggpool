# Persistence Milestone 006 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/persistence/006-sqlite-noop-and-wal-reset-safety-baseline.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-006--sqlite-noop-and-wal-reset-safety-baseline`

Repository baseline reviewed: `9c2eac2c`

Implementation commits:

- `974a5ece` — mark M006 active in the registry.
- `c3a72720` — upgrade the async SQLite stack, add checked unsigned-to-signed conversion, add engine/NOOP guards, and update the database architecture baseline.
- This closure commit — close M006 and audit the M003 dependency; M003 promotion follows in a separate registry status-change commit.

## 1. Executive finding

M006 is complete. EggPool now builds the bundled tokio-rusqlite 0.8.0 / rusqlite 0.40.2 / libsqlite3-sys 0.38.2 stack, which embeds SQLite 3.53.2. A file-backed compatibility test guards the SQLite 3.51.3 minimum and proves repeated `wal_checkpoint(NOOP)` calls do not advance checkpoint progress before an explicit PASSIVE checkpoint. The existing WAL/NORMAL policy, 1000-page automatic fallback, single connection/gate/worker, schema 54, backup boundary, and public surfaces remain unchanged.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Upgrade async SQLite stack without feature creep | `rust/Cargo.toml`, `rust/Cargo.lock`, Cargo feature trees | pass | One resolved tokio-rusqlite 0.8.0, rusqlite 0.40.2, and libsqlite3-sys 0.38.2 line; bundled + backup selected; no hooks, bundled-full, or modern-full. |
| Bundle a NOOP-capable, WAL-reset-fixed engine | `rust/target/debug/build/libsqlite3-sys-*/out/bindgen.rs`; runtime `SELECT sqlite_version()` guard in `database_compatibility` | pass | Bundled version is 3.53.2; regression floor is >= 3.51.3. |
| Prove NOOP is observational and PASSIVE advances progress | `database_compatibility::bundled_sqlite_supports_observational_noop_and_fixed_wal_reset_baseline` | pass | File-backed WAL work remains below autocheckpoint; two NOOP reads match; PASSIVE converges; backup and close succeed. |
| Preserve checked unsigned SQL semantics | `rust/src/db/connection.rs::Database::configure`; migration compile; `journal_size_limit_rejects_values_outside_sqlite_integer_range` | pass | The only live unsigned SQL boundary found is `journal_size_limit`; it now uses checked `i64::try_from`, with overflow rejected. No direct rusqlite dependency or `fallible_uint` feature was added. |
| Preserve WAL/NORMAL/1000 and schema 54 | `database_compatibility` and migration tests | pass | Existing pragma and migration assertions remain green; no migration was added. |
| Preserve backup, recovery, lifecycle, publication, and finalization | default/no-default full workspace suites; O006, R006/R008, coordinator publication/finalization targets | pass | O006 backup/recovery 4/4; R006 10/10; R008 4/4; coordinator publication 7/7; finalization 10/10. |
| Preserve Rust 1.89 compatibility | `cargo +1.89.0 check --manifest-path rust/Cargo.toml --workspace --all-targets` | pass | Local Rust 1.89.0 toolchain. |
| Keep public/runtime policy unchanged | source diff and Cargo/config/route review | pass | No config, CLI, HTTP, Rust public API, migration, checkpoint cadence, threshold, hook, or connection change. |
| Qualify default and reduced feature surfaces | full serial workspace suites | pass | Both default and `--no-default-features` passed on the physical Pi host. |

## 3. Production implementation evidence

- `tokio-rusqlite` moves from 0.7.0 to 0.8.0 with the existing explicit `bundled` and `backup` features.
- Cargo resolves rusqlite 0.40.2 and libsqlite3-sys 0.38.2; bundled bindings identify release tag SQLite 3.53.2.
- The migration exposed one removed implicit `u64: ToSql` conversion: the existing `journal_size_limit` pragma. EggPool now converts only after `i64::try_from`, preserving rejection for values above `i64::MAX`.
- The new file-backed integration guard queries the runtime engine, rejects versions below 3.51.3, proves repeated NOOP reads do not checkpoint frames, then proves explicit PASSIVE work advances/converges them and online backup still works.
- No runtime checkpoint scheduling behavior was changed; this is the engine-safety baseline only.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo +1.89.0 check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo test --manifest-path rust/Cargo.toml --test database_compatibility bundled_sqlite_supports_observational_noop_and_fixed_wal_reset_baseline -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib db:: -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -i tokio-rusqlite -e features
cargo tree --manifest-path rust/Cargo.toml -i rusqlite -e features
cargo tree --manifest-path rust/Cargo.toml -i libsqlite3-sys -e features
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo deny --manifest-path rust/Cargo.toml check
git diff --check
```

### Results

- Formatting, strict default Clippy, Rust 1.89 all-target check, strict no-default Clippy, and locked release build passed locally.
- The full default `--workspace --all-targets` serial suite passed locally. Its first attempt hit a Pi memory limit during parallel linking; the serialized run initially hit the pre-existing fixed-wait O002 test while compile I/O was high. That O002 test passed when isolated and in the successful full rerun.
- The full `--no-default-features --workspace --all-targets` serial suite passed locally.
- `database_compatibility` passed 9/9 under both feature configurations. The database unit target passed 7/7. O006 passed 4/4; R006 passed 10/10; R008 passed 4/4; publication passed 7/7; finalization passed 10/10.
- Cargo feature trees show one version of each SQLite crate and no hooks/full feature. `cargo deny` reports advisories, bans, licenses, and sources clear; it prints the repository's existing duplicate-crate warnings outside this SQLite stack.
- Target was a Raspberry Pi 5 Model B, Linux aarch64, ext4 root on non-rotational `/dev/mmcblk0p2` (MMC). M006 does not require physical performance qualification.
- Verification is local evidence; no hosted CI result is claimed.

## 5. Invariant review

- One tokio-rusqlite connection, one EggPool semaphore gate, and one worker remain.
- WAL and `synchronous=NORMAL` remain enabled; `wal_autocheckpoint` remains 1000 pages.
- Ordinary explicit maintenance remains PASSIVE only; NOOP is now truly observational on the bundled engine.
- Schema 54, migration checksums, durable row semantics, and online backup/recovery APIs are unchanged.
- No SQLite hook, second connection, second worker, public setting, task, or checkpoint policy change was introduced.
- Qualification-only diagnostics remain non-default and dependency-free.

## 6. Failure and recovery review

The tokio-rusqlite API migration preserves the existing transaction, rollback, and commit error mapping. Checked conversion rejects an unrepresentable journal-size limit before SQLite use. Database compatibility, Python historical fixture round-trip, second-connection contention, backup/recovery, startup reconciliation, reload lifecycle, and shutdown tests passed in both full feature suites. There is no new asynchronous owner or cancellation path.

## 7. Migration and compatibility review

No schema migration or file-format conversion is needed. The SQLite stack remains bundled, database files remain readable, and schema 54 is unchanged. Rust 1.89 compatibility passed. There is no config, CLI, HTTP, wire, or public Rust API change.

## 8. Security review

No new persisted or diagnostic data was added. The NOOP guard uses a test-only table and bounded synthetic rows. The unsigned conversion rejects overflow instead of truncating. No credentials, request bodies, SQL text, or database paths are exposed.

## 9. Documentation and operations

`architecture/deep-dive-database.md` records the exact bundled stack, true-NOOP guarantee, and unchanged ownership/durability policy. The persistence roadmap and registry identify M006 as closed and M003 as awaiting its separate readiness promotion.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M006 correctness or compatibility finding remains. | None. | None. |

`cargo deny` continues to print pre-existing duplicate-version warnings for unrelated crates; no SQLite stack duplicate is present and the check passes.

## 11. Roadmap disposition

M006 is closed. Its hard dependency on persistence M003 is satisfied. M003 remains blocked only until the separate registry status-change commit records the accepted M006 closure SHA and promotes M003 to ready. M003's paired physical Pi/MMC qualification remains its own closure requirement.

## 12. Registry updates

The persistence roadmap marks M006 closed and links this record. The active registry removes M006 from dependency-ready work and records M003 as awaiting a separate `blocked -> ready` promotion. Historical M004/M005 records are unchanged.
