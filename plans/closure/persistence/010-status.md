# Persistence Milestone 010 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/persistence/010-control-outbox-analytics-storage-architecture-investigation.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-010--controlprojection-storage-boundary-architecture-investigation`

Repository baseline reviewed: `2ac02329`

Implementation commits:

- `2ac02329` — activate M010 after M009 closed.
- `dfa2a2ab` — complete the ownership investigation, isolated prototype, decision record, and test-fixture correction.
- This closure commit — formally close M010 and record the successor audit.

## 1. Executive finding

M010 is closed with the control/outbox/analytics split rejected under the current history and outage contract. The schema-54 ownership/index census, minimum control-state derivation, failure/backup/read-consistency analysis, and test-only transaction-shape prototype are complete. The synthetic candidate showed fewer modeled explicit secondary indexes in control storage (2 versus 12) but more foreground row/SQL mutations (800 versus 700 per 100 request lifecycles) and no safe finite backlog policy that preserves current history visibility while allowing control requests to proceed through an arbitrarily long analytics outage. No ADR-0002 is proposed, no production storage change is authorized, and production remains one SQLite database/connection/gate/worker on WAL/NORMAL.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Exhaustive schema-54 table/field/index/consumer ownership inventory | `architecture/persistence-control-projection-investigation.md` §§3 and complete column/index censuses; generated from migrations using SQLite metadata pragmas | pass | All 29 application tables are classified; mixed `requests`/`request_attempts` ownership is field-grouped. |
| Minimum synchronous request/attempt/reservation correctness state | Investigation §3, including publication, retry, finalization, compensation, recovery, quota and routing consumers | pass | Reservations remain fully control-authoritative. |
| Compare monolith, same-file logical split, and control/outbox/analytics | Investigation §4 and §10 | pass | Split has structural potential but fails current outage/history constraints. |
| Durable outbox ordering, idempotency, replay, cursor and reclamation contract | Investigation §5 | pass as rejected candidate model | Monotonic event ids, bounded batches, transactional projection/cursor, at-least-once delivery and exactly-once effect are specified; none is implemented in production. |
| Bounded backlog and analytics outage policy | Investigation §5 decision table | pass — candidate rejected | No lossless finite-cap action satisfies current history parity, bounded disk, and continued control admission simultaneously. |
| Two-file backup/restore and dashboard/read consistency contracts | Investigation §6 | pass as rejected candidate model | Backup v1 remains production authority; current history reads have no approved lag/degraded semantics. |
| Migration, rollback, deterministic failure, security/privacy analysis | Investigation §§7–8 and §5 | pass | No production migration or new retention of sensitive content. |
| Isolated current-vs-candidate transaction-shape prototype | `rust/tests/persistence_projection_architecture.rs` | pass | File-backed temporary DBs only; replay, cancellation/crash rollback, restart, poison-event, cursor, absent-analytics, and reclamation cases covered. |
| Quantify modeled statement/index and file/WAL deltas | Prototype `transaction_shape_compares_index_fanout_and_file_growth`; captured output in §9 | pass, synthetic only | 12 versus 2 explicit secondary indexes; 700 versus 800 statements/row mutations; local bytes are not target performance or physical wear evidence. |
| ADR disposition and production boundary | No ADR-0002; implementation diff contains no production source, Cargo, migration, API/config, or backup changes | pass | Split rejected; no automatic successor authorization. |
| Registry successor audit | `plans/registry.md` current dependency-ready/active/blocked tables and M010 closure audit below | pass | No registered Persistence successor or blocked row exists; nothing is promoted. |

## 3. Production implementation evidence

No production runtime, Cargo dependency, migration/schema, configuration, API/dashboard, backup format, qualification feature, or release workflow changed. The durable investigation is in `architecture/persistence-control-projection-investigation.md`, linked from `architecture/deep-dive-database.md`. The prototype is a standalone integration test and has no production entry point.

The only other code-path change is in `rust/tests/operations_o008.rs`: its two old-executable fixtures now use owner-only mode on Unix. The local `umask 0002` created them as mode `0664`, which production correctly rejects as group-writable; the fixture now matches the existing updater safety contract. Updater runtime validation is unchanged.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test persistence_projection_architecture -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test persistence_projection_architecture -- --test-threads=1 --nocapture
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo build --manifest-path rust/Cargo.toml --locked --release
uv run --frozen python scripts/validate_release_docs.py
uv run --frozen python scripts/validate_runtime_package_boundary.py
git diff --check
```

### Results

- Formatting check: pass.
- M010 prototype: 3 passed; neighboring publication 7 passed, finalization 10 passed, backup/recovery 4 passed, lifecycle R008 4 passed, updater O008 10 passed.
- Strict workspace Clippy: pass with `-D warnings`.
- Full default workspace/all-target serial suite: pass. One existing manual dashboard TTFT qualification is ignored by design.
- Full no-default workspace/all-target serial suite: pass, including SSH-config rejection and M010 prototype.
- Workspace no-default check: pass.
- Locked release build: pass.
- Release-doc validator: pass; 7 docs checked, release 0.8.1 and all 3 documented targets validated.
- Runtime-package-boundary validator: pass; current runtime Rust, historical Python 0.7.4, native release 0.8.1.
- `git diff --check`: pass.
- The first default full-suite attempt exposed the updater fixture's mode-0664 mismatch under this host's umask. The fixtures were corrected without changing production code; the focused updater target and the subsequent complete default and no-default suites pass.

The M010 `--nocapture` prototype run reported, for its deterministic 100-request local model: baseline main-file growth 77,824 bytes and WAL 4,161,232 bytes; candidate control main-file growth 49,152 bytes and WAL 4,140,632 bytes; 200 outbox events; analytics apply separate. This synthetic subset excludes candidate analytics-file bytes and does not predict Pi/MMC latency. Per-thread worker `write_bytes` was not measured because this synchronous test model has no tokio-rusqlite worker; M008's worker proxy is not a comparable measurement for this prototype.

## 5. Invariant review

- Production remains one authoritative SQLite DB/connection/gate/worker on WAL/NORMAL: preserved.
- Publication/finalization, reservation ownership, retry ordering, recovery, backup v1, API/dashboard contracts, and migration ledger: unchanged.
- M007/M008 physical evidence and closures: immutable and still authoritative for their specific target/journal measurements.
- No prompts, request bodies, credentials, raw provider bodies, filesystem paths, or cache keys are placed in prototype events or retained in new production storage.
- Proposed outbox apply/cursor atomicity, duplicate replay, poison-event cursor stop, and reclaim-after-cursor are exercised only in temporary test databases; no production projector is introduced.

## 6. Failure and recovery review

The test-only model verifies rollback when interrupted after the projection row write but before commit, restart from the durable cursor, duplicate replay without a second effect, poison event stopping ordered progress, control mutations succeeding with analytics absent, and reclamation only through the applied cursor. The architecture record covers remaining control/analytics corruption, lock, backup/restore, cancellation, cursor-invalid, old-event-version, and retention-race outcomes.

The decisive failure is a prolonged analytics outage with a lossless outbox: any finite hard cap eventually requires blocking/rejecting otherwise-valid control work, dropping history required by current views, changing history retention semantics, or exceeding the disk bound. M010 had no authority to choose one of those product tradeoffs, so Option C is rejected under the current contract.

## 7. Migration and compatibility review

No production migration, dual-database archive, shadow read/write path, schema version, or downgrade behavior was implemented. The staged migration and rollback outline in the investigation is research only. Existing schema 54 and backup format v1 remain unchanged and authoritative.

## 8. Security review

The hypothetical event allowlist excludes credentials, prompts, request/provider bodies, paths, and cache keys; it preserves bounded sanitized diagnostic policy. Temporary test database files are confined to `tempfile` directories and deleted on test completion. No network listener, remote analytics access, or release feature is added.

## 9. Documentation and operations

Added the investigation and linked it from the SQLite deep dive. The record states that no local timing/byte result is Pi/MMC performance or NAND-wear evidence and that M007/M008 metrics remain unchanged. No operator command, runtime diagnostic, or backup procedure changes.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | No approved finite history-retention and prolonged analytics-outage admission contract exists for a future split | A separate analytics database cannot be bounded without changing current history or admission semantics | No action for current production. If a split is later requested, define this product contract in a new bounded plan before any ADR or physical qualification. |

## 11. Roadmap disposition

M010 is closed as an architecture investigation with the split rejected. The Persistence roadmap remains active because the foreground SQLite checkpoint/storage tail is unresolved; M010 does not solve that performance issue. No ADR-0002 is proposed and no production split work is authorized.

## 12. Registry updates and successor audit

The registry removes M010 from active work, records this closure, and keeps the Persistence roadmap active for its unresolved checkpoint/storage tail. Search of `plans/implementation/persistence/`, the roadmap dependency graph, and the registry found no M011 or other registered Persistence plan depending on M010; the blocked-work table has no Persistence entry. No successor can be promoted and no other Persistence plan is dependency-ready. A future split investigation is not ready until its finite history-retention and prolonged-outage admission contract is explicitly defined. No alternate eligible Persistence plan exists to continue with.

`plans/subsystems/persistence-roadmap.md` marks M010 closed/rejected and links this record. Applied with the registry update in the same closure commit.
