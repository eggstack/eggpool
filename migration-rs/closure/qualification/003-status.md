# Q003 Closure — Database Upgrade, Rollback, Backup, and Recovery Compatibility

Status: accepted; closed 2026-09-09

Implementation candidate: `482856a898c40979c70106bd29958fcc454115ca`

Plan: [Q003 — database upgrade, rollback, backup, and recovery compatibility](../../implementation/qualification/003-database-upgrade-rollback-backup-recovery-compatibility.md)

## Outcome

Q003 is accepted. The Rust database, repository, operations, backup, and
recovery surface was qualified at the Python/Rust migration boundary without
adding a qualification-only schema or resetting durable state. The focused
qualification contains 11 black-box scenarios and the aggregate runner records
four passing evidence commands with no failure or infrastructure result.

The implementation and focused evidence are in:

- [`q003-fixture-matrix.json`](../../fixtures/qualification/q003-fixture-matrix.json)
- [`qualification_database.py`](../../../scripts/qualification_database.py)
- [`test_q003_database_compatibility.py`](../../../tests/migration_rs/test_q003_database_compatibility.py)
- [`003-run.json`](003-run.json) and [`003-run.md`](003-run.md)

The aggregate uses manifest `m10-q003.v1`, candidate
`482856a898c40979c70106bd29958fcc454115ca`, Python 3.11.9, macOS x86_64,
and loopback-only network policy. The JSON artifact is 1,763 bytes with SHA-256
`54ffef362f0c4f73f8d6dbf6dcdceafb76dfeaf97a9d75e2194d4aac19f34b5a`; the
Markdown artifact is 919 bytes with SHA-256
`d2071f462f77014f009dd3f743f2c0c1f9e03c06cb557b5fedbec7e13c31bb77`.

## Q001 ownership coverage

The accepted Q001 manifest remains frozen. Q003 covers all five assigned
durable/operations cells:

| Q001 cell | Evidence |
|---|---|
| `q001.operations.backup-recover` | Rust online backup, Python runtime backup, Rust recovery, archive validation and retention/fault cases |
| `q001.operations.update-deploy` | Rust update/deploy command paths are included in the complete O010 two-sided command-surface qualification composed by the Q003 aggregate |
| `q001.durable.lifecycle` | Python-created and Rust-created request/attempt lifecycle rows, finite and streaming requests |
| `q001.durable.quota-health` | Rust-generated durable catalog, quota, health, routing, and rollup rows reopened by Python |
| `q001.durable.migrations` | Historical migration 1–11 snapshot upgraded through the current schema version 54, plus latest Python/Rust startup migration |

No Q001 cell was reclassified, normalized away, or left unowned.

## Transition and recovery results

- Historical Python v11 SQL fixture, SHA-256
  `a65d21db88e0319a87ef91b9be59f7f135a13c06b9e9036bdd0d096b4a34447b`, and
  checksum manifest, SHA-256
  `bee248bba09edbb5e19268748c1cde56f4256517e3cf49a256b892103527c4dd`,
  migrated under Rust through schema version 54 and served finite and streaming
  requests.
- Latest Python-created durable state opened under Rust; Rust startup,
  migration, ordinary request writes, catalog/operator writes, vacuum, and
  checkpoint completed; the final Python reference reopened the database and
  performed repository read/write operations.
- Rust-created and Rust-mutated durable state reopened by the Python reference
  with semantic account, model, request, attempt, reservation, routing, and
  rollup observations intact.
- Rust online backup captured committed rows from an active SQLite WAL and was
  inspected/restored by Python. A Python `sqlite3.Connection.backup` runtime
  archive was recovered by Rust. Both paths preserve the committed event and
  logical `META`, `config.toml`, optional `.env`, and `usage.sqlite3` archive
  shape.
- Missing metadata, invalid config, invalid database, traversal, duplicate
  member, and oversized member archives all failed closed while preserving the
  original usable config/database state.

The supported cross-implementation archive boundary is the online-snapshot
shape `META`, `config.toml`, optional `.env`, and `usage.sqlite3`. Legacy raw
sidecar archives containing `usage.sqlite3-wal` or `usage.sqlite3-shm` remain
outside that boundary; this is the O006 contract and is recorded explicitly in
the Q003 fixture matrix.

## Findings and implementation corrections

The boundary run found and corrected two real compatibility defects:

1. Rust rejected ordinary Python `zipfile` entries because Python leaves the
   Unix file-type bits unset. Rust now accepts that ordinary-file encoding
   while continuing to reject special-file members.
2. Python runtime backup used the source configuration basename as the archive
   member. It now always emits the logical `config.toml` member required by
   the shared archive contract.

The fault run also exposed that the Rust ZIP crate deduplicates member names
before indexed iteration. Rust now pre-scans the bounded central directory and
rejects duplicate raw member names before restore validation. No unresolved
high- or medium-severity Q003 finding remains; no dependency or schema fork was
introduced.

## Verification evidence

The canonical bounded aggregate was run as:

```text
rtk uv run python scripts/qualification_database.py --skip-build
# 4 pass, 0 fail, 0 infrastructure-error; 50,719 ms
```

Its four commands reported:

```text
rtk uv run pytest tests/migration_rs/test_q003_database_compatibility.py -q --tb=short --maxfail=1
# 11 passed
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o006 --test operations_o007 --test database_compatibility -- --test-threads=1
# 14 passed
rtk uv run pytest tests/unit/test_lifecycle_backup.py tests/integration/test_migration_compatibility.py -q --tb=short --maxfail=1
# 56 passed
rtk uv run pytest tests/migration_rs/test_o010_operations.py -q --tb=short --maxfail=1
# 2 passed
```

Plan-level repository gates also passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
# pass
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
# No issues found
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
# 440 passed (52 suites, 207.33s)
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# 128 passed, 3 skipped
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
# 14 passed
rtk uv run ruff format --check src/ tests/ scripts/
# 739 files already formatted
rtk uv run ruff check src/ tests/ scripts/
# All checks passed
rtk uv run pyright src/ scripts/
# 0 errors, 0 warnings, 0 informations
rtk git diff --check
# pass
```

Focused affected Python suites, including backup, migration, maintenance,
database, fault-matrix, and Q003 tests, passed with 132 tests. The Q003 Rust
targeted command and the complete Rust all-targets command were both rerun
after the implementation changes.

## Registry transition and future-plan audit

Q003 moves from ready to complete in the implementation plan, registry,
qualification README, handoff sequence, and qualification roadmap. Q004 is
promoted to **ready** as the sole dependency-ready M10 plan because its direct
dependency is now accepted. Q005–Q010 remain queued behind their direct
predecessors. M10 remains active; M11 remains blocked on accepted Q010 and its
separate planning review; M12 remains sequenced behind M11. No later plan is
unblocked by this closure.
