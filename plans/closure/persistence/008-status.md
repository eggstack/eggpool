# Persistence Milestone 008 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/persistence/008-persist-journal-mode-qualification-and-write-amplification.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-008--rollback-journal-persist-qualification-and-write-amplification-attribution`

Repository baseline reviewed: `fac930b9e7be1dd5d05519e4bc14e922df335869`

Implementation commits or pull requests:

- `fac930b9` — feature-only PERSIST/EXTRA qualification, write-I/O attribution, isolated SBC runner modes, reporting, integrity checks, documentation, and report path redaction.

## 1. Executive finding

The bounded qualification is complete and its adoption decision is **rejected**. On the physical Pi 5/MMC target, PERSIST/EXTRA eliminated candidate WAL/SHM files and avoided WAL checkpoint work, but increased commit and request latency: all three 60-request candidate runs failed the plan's request p95 `<100 ms` gate (155–160 ms), and all five windows of the 300-request candidate run failed that p95 gate (166–181 ms). The measured write-I/O proxy was about 2.04× control in the separate attribution cohort. Production remains WAL/NORMAL with the existing single connection, gate, and worker. The persistence roadmap remains active because the storage latency tail remains unresolved.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Same feature-built binary and attested physical target | `artifacts/qualification/m008-pi5-2026-10-03/binary.sha256`, `physical-attestation.md`, eight JSON reports | pass | Raspberry Pi 5 / aarch64 / ext4 on MMC; all reports record SHA-256 `8a5c44f0…e8a3d6cf`. |
| Three 60-request WAL/NORMAL controls | `m008-control-1.json` through `m008-control-3.json` | pass as controls | Request p95 4–5 ms; maximum 299–325 ms. WAL grew 1,792,200 bytes across each measurement. |
| Three 60-request PERSIST/EXTRA candidates | `m008-candidate-1.json` through `m008-candidate-3.json` | fail adoption gates | Request p95 155–160 ms; maxima 157–190 ms. Commit maxima stayed below 500 ms, gate waits stayed at or below 12 μs, and no WAL/SHM appeared. The request-p95 gate failed in every run. |
| Five-window 300-request steady-state candidate | `m008-candidate-300.json` | fail adoption gates | Every window completed; maxima 178–348 ms; request/reservation state converged to zero; WAL/SHM remained absent. Each window failed request p95 `<100 ms` (166–181 ms). The rollback journal grew early and stabilized at 275,480 bytes in windows 4–5. |
| Backup/recovery/restart/rehash/vacuum/graceful shutdown | candidate reports | pass | Lifecycle checks completed on isolated per-invocation databases. |
| Database integrity and foreign keys | `database_integrity` in all eight reports | pass | Every report records `quick_check: ok` and zero foreign-key violations after shutdown. |
| Separate worker-I/O attribution cohort | `m008-attribution-control.json`, `m008-attribution-candidate.json` | pass as bounded measurement | 60 publication and 60 finalization transactions per mode; no `Other` transactions. Candidate/control aggregate write-byte proxy ratio 2.038×. This is a Linux worker-I/O proxy, not physical NAND write amplification and not a primary latency gate. |
| Feature/default isolation and no production adoption | implementation diff, default/no-default tests and builds | pass | Feature is opt-in, configuration remains unchanged, and PERSIST/EXTRA applies only to the isolated qualification database. |

## 3. Production implementation evidence

The feature `qualification-persist-journal` and startup-only `EGGPOOL_QUALIFICATION_PERSIST_JOURNAL` toggle are confined to qualification builds. Candidate startup validates the writable file-backed database, expected WAL/NORMAL baseline, toggle value, and incompatibility with the dedicated-checkpointer experiment; it applies and verifies PERSIST/EXTRA before database operations. PERSIST-aware maintenance avoids WAL pragmas. Linux-only worker I/O attribution is separate, aggregate-only, bounded by transaction kind, and absent from ordinary builds. The SBC runner gives every invocation a fresh child database, strips the candidate toggle for read-only backup access, omits WAL observation for PERSIST, and records post-shutdown integrity checks. No schema migration, new dependency, production journal change, or additional database worker was introduced.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-persist-journal -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib --features qualification-persist-journal -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-persist-journal
uv run --frozen ruff format --check scripts/ tests/tooling/
uv run --frozen ruff check scripts/ tests/tooling/
uv run --frozen pyright scripts/
uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
```

### Results

All listed local Rust format, strict Clippy, checks, release builds, and serial test suites passed. The qualification-feature library suite passed 150 tests. The final full tooling suite passed 161 tests with 3 skipped; Ruff formatting/lint and Pyright passed with zero errors/warnings. One full-suite attempt under the shell's group-writable umask caused two existing updater safety tests to reject a temporary executable under `/tmp`; rerunning with `umask 022` and a private `TMPDIR` passed. The exact Pi corpus is local physical-target evidence; hosted CI has not run and is not claimed.

## 5. Invariant review

- The ordinary runtime remains one SQLite connection, one gate, and one worker on WAL/NORMAL; no performance boundary or public request type changed.
- PERSIST/EXTRA is limited to opt-in qualification builds and runner-owned isolated files.
- Request, credential, and database-path data are not added to persistence or retained diagnostic reports; retained absolute diagnostic database paths are redacted.
- No persistent configuration semantics, migration version, or production lifecycle authority changed.
- Candidate requests converged to zero pending/reservation state and integrity checks passed after shutdown.

## 6. Failure and recovery review

All completed candidate corpora passed backup, recovery, restart/reconciliation, rehash, vacuum, and graceful shutdown. The runner uses distinct temporary database children and validates database integrity and foreign keys after shutdown. An early runner iteration passed the candidate toggle to a read-only backup connection; that validation failure caused no production mutation, the environment scoping was corrected, and all retained final lifecycle reports pass. No unresolved recovery or correctness failure remains in this experiment.

## 7. Migration and compatibility review

No schema migration was needed. Existing database compatibility tests passed with default and qualification feature builds. WAL/NORMAL configuration and reopen behavior are unchanged in normal builds. PERSIST is reapplied only to the fresh candidate database at qualification startup; production databases and rollback behavior are outside the candidate path.

## 8. Security review

The runner accepts only its isolated diagnostic database directory for these modes. Attribution stores bounded aggregate byte counts and transaction-kind buckets, not paths, identities, SQL, prompts, or request bodies. Report diagnostics bound text and redact credential-shaped values and the diagnostic database path. The feature does not add network exposure or alter authorization.

## 9. Documentation and operations

`architecture/deep-dive-database.md` documents the qualification-only boundary and attribution limits. The implementation plan and subsystem roadmap now record the rejected result and production decision. Physical attestation, binary checksum, and sanitized raw reports are retained under `artifacts/qualification/m008-pi5-2026-10-03/`.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | WAL/NORMAL still has a material foreground storage tail; PERSIST/EXTRA misses request-p95 acceptance and has higher measured commit cost. | The tested journal change cannot be adopted under the current latency target; persistence performance remains unresolved. | Keep production WAL/NORMAL. Any control/outbox/analytics split or alternate production storage design requires a new bounded plan and architecture decision; none is registered or authorized by this closure. |
| low | Linux `/proc/thread-self/io` attribution is a worker-level write-byte proxy, not device/NAND write amplification. | Attribution cannot establish physical flash wear. | Preserve this caveat in future use; a device-level wear claim would need separate instrumentation and plan scope. |

No critical or high-severity defect remains in the implemented qualification path.

## 11. Roadmap disposition

Milestone M008 is closed with a rejected adoption outcome. The persistence roadmap remains active because the checkpoint/storage latency problem is unresolved. Audit of registered future work found no M008-dependent or blocked Persistence successor to promote; the dependency-ready table is empty. No follow-up plan is created implicitly. Research into a control/outbox/analytics split remains a possible separately authorized direction, not an unblocked plan.

## 12. Registry updates

In the closure commit, mark M008 implemented/closed in its source plan and roadmap, remove it from dependency-ready work, add this record to recently closed, and record the successor audit. Keep the Persistence roadmap active and production WAL/NORMAL unchanged.
