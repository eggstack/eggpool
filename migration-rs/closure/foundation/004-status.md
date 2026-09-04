# F004 Closure — SQLite Schema and Repository Compatibility Baseline

Status: closed

Recommendation: closed

Implementation commit: [`9cc9fc4`](https://github.com/eggstack/eggpool/commit/9cc9fc4)

Plan: [Foundation Milestone F004](../../implementation/foundation/004-sqlite-schema-and-repository-baseline.md)

Repository baseline inspected: `0bb5aaf4` (`Close Rust migration foundation F003`)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| A — canonical schema inventory | `rust/build.rs` discovers all canonical `src/eggpool/db/schema/*.sql` files, requires a matching entry in Python's `checksums.json`, and embeds the exact source files at build time; the Rust test asserts 54 migrations numbered 1–54 | Pass |
| B — checksum and ledger safety | `MigrationRunner::validate_checksums` rejects altered content; startup validates every embedded SHA-256, checks unknown ledger versions, accepts both current `.sql` names and the historical no-extension names, and never rewrites applied migrations | Pass |
| C — open/pragmas/ownership | `Database` uses one `tokio-rusqlite` worker plus one semaphore permit, enables foreign keys, applies configured busy timeout/WAL/synchronous/journal limit, supports read-only URI opens, and converts F003's database config | Pass |
| D — transactions and failure behavior | `with_transaction` issues `BEGIN IMMEDIATE`, explicitly commits or rolls back, returns typed busy/transaction/rollback/commit errors, and closes admission after an unprovable rollback; rollback tests prove no partial row remains | Pass |
| E — minimal typed repositories | Account, model, request, provider-ping, and usage-rollup repositories provide typed reads plus account/request/ping compatibility writes needed by the first read plane | Pass |
| F — Python/Rust compatibility | The Rust suite upgrades the Python v11 historical fixture through migration 54, reads representative account/request rows, writes and completes a request, then reads that row with Python's stdlib SQLite driver | Pass |
| G — contention/read-only/restart boundary | Competing file-backed writers produce a bounded typed busy result; current databases open read-only and reject writes; no in-memory repair or schema reset is used, and process restart remains the owner of terminal worker recovery | Pass |
| H — no schema fork/dependency scope | No numbered migration or Python schema file changed. The only Rust runtime addition is `tokio-rusqlite` (with bundled SQLite) and the required Tokio sync/time features; full finalization, quota, maintenance, backup, and cutover remain unported | Pass |

## Differential and database results

- Fresh startup applied exactly 54 canonical migrations and a second startup
  applied none.
- The Python `pre_phase17_v11.sql` fixture upgraded with versions 12–54 and
  retained its representative account, model, request, attempt, reservation,
  and pricing data.
- Rust-created request rows retained Python-compatible status and integer
  token units: `('success', 3, 2)` on Python readback.
- WAL, `foreign_keys=1`, `busy_timeout=5000`, and `synchronous=NORMAL` were
  observed through SQLite pragmas.
- The Rust migration layer owns no alternate schema directory and does not
  increment the schema version.

## Verification commands actually run

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings    PASS
cargo test --manifest-path rust/Cargo.toml                                   PASS (13 tests)
uv run pytest tests/migration_rs tests/integration/test_migration_compatibility.py tests/unit/test_db.py -q --tb=short --maxfail=1 PASS (55 tests)
uv run pytest tests/smoke/ -q --tb=short --maxfail=1                       PASS (14 tests)
uv run ruff format --check src/ tests/ scripts/                              PASS
uv run ruff check src/ tests/ scripts/                                       PASS
uv run pyright src/ scripts/                                                 PASS (0 errors)
git diff --check                                                             PASS
```

## Security, contention, restart, and known limitations

No credentials, request bodies, or fixture secrets are embedded. The Rust
database API does not expose a pooled writer or a repair queue. Busy/locked
contention is bounded by SQLite's configured timeout and typed for callers.
Commit failure with a verified rollback remains a typed usable failure, while
rollback failure or an unverified commit outcome closes admission and requires
a fresh process-owned database worker.

The repository surface is intentionally the F005 read-plane baseline, not a
claim of full data-plane parity. Request finalization, reservations and quota
accounting, catalog synchronization, health state transitions, rollups beyond
the summary query, backup/recovery, lifecycle integration, and operational
cutover remain later work. No unresolved findings by severity.

## Planning follow-through

F004 is removed from the dependency-ready section of `migration-rs/registry.md`
and recorded in the completed section. F005's F003 config and F004 read
interface dependencies are now satisfied; F005 is marked ready for handoff.
No other currently represented future plan is blocked on F004.
