# F002 Closure — Contract Inventory and Differential Oracle Harness

Status: closed

Recommendation: closed

Implementation commit: [`a8c3621`](https://github.com/eggstack/eggpool/commit/a8c3621)

Plan: [Foundation Milestone F002](../../implementation/foundation/002-contract-inventory-and-oracle-harness.md)

Repository baseline inspected: `5f2c8b83` (`Close Rust scaffold migration plan`)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| A — reviewed contract inventory | [`contract-inventory.md`](../../contract-inventory.md) covers the full Click command tree/options, config sections/defaults/aliases and proxy forms, composed HTTP methods/auth/routes, migration/checksum/table baseline, dashboard routes, static files, and themes | Pass |
| B — isolated launchers and environment | `PythonLauncher` invokes the checked-out package with `sys.executable -m eggpool`; `RustLauncher` invokes `rust/target/debug/eggpool`; both carry explicit identity; `IsolatedEnvironment` sets separate HOME/XDG/TMP roots plus UTC/hash determinism | Pass |
| B — wrong-implementation guard | `assert_distinct_implementations` rejects same implementation identity or executable path; the real-process test observed distinct Python/Rust PIDs and successful version probes | Pass |
| B — bounded lifecycle | `ProcessRunner` uses timeouts and process groups; `RunningProcess` has context-managed graceful/forced teardown; TCP startup polling has a monotonic deadline | Pass |
| C — structured observations | `CommandObservation`, `ConfigObservation`, `HttpObservation`/`SseFrame`, `DatabaseObservation`, `HtmlObservation`, and `StaticObservation` serialize reviewable facts including status/exit/body/schema/DOM/hash data | Pass |
| C — local stub server | `StubHttpServer` allocates `127.0.0.1:0`, drains request bodies, records only method/path/length/header names, and returns deterministic route responses | Pass |
| D — explicit normalization | [`normalization-policy.md`](../../normalization-policy.md) lists every allowed rule, rationale, and regression test; unknown JSON fields, status/exit codes, HTML text/DOM, and exact static bytes remain visible | Pass |
| E — seed fixtures | Credential-free valid/invalid config fixtures and committed Python oracle capture descriptors cover CLI version, config, health JSON, SQLite migrations, and dashboard static assets | Pass |
| security/privacy | Sanitized child environments remove inherited EggPool/provider key variables; request bodies are never retained by the stub; no fixture contains a credential or live provider endpoint | Pass |
| Python independence | Existing Python source and packaging were not changed; the harness launches the existing package externally and remains runnable as ordinary pytest tests | Pass |

## Differential and compatibility results

The F001 Rust candidate does not yet implement config, HTTP, SQLite, or SSR, so
this closure claims the comparator and Python seed side only. The harness is
wired for two-sided cases and refuses to compare two Python or two executable-
path-identical launchers.

The Python seed probes produced these representative results:

- `version`: exit 0, stdout `0.7.4\n`, empty stderr.
- valid config: accepted; invalid port fixture: rejected with category `schema`.
- a separately launched Python server returned `GET /v1/healthz` status 200 and
  `{"status":"ok"}`.
- Python's `migrate` command created 54 `_migrations` rows and a schema
  observation containing 30 non-SQLite tables (including `_migrations`) plus
  schema/checksum facts.
- the exact Python dashboard favicon hash matched the reviewed inventory;
  SSE observations preserved event, id, and ordered data lines.

Required negative comparisons also pass: changing a JSON field or CLI exit code
raises a differential mismatch, and changing HTML text/DOM or even its raw
whitespace is not erased by normalization.

## Verification commands actually run

```text
uv run ruff check tests/migration_rs                         PASS
uv run ruff format --check tests/migration_rs               PASS
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  PASS (14 tests)
```

The Rust F001 verification commands were rerun after the harness landed:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check                    PASS
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings PASS
cargo test --manifest-path rust/Cargo.toml                              PASS (3 tests)
cargo build --manifest-path rust/Cargo.toml                             PASS
```

The existing narrow Python smoke gate was also run:

```text
uv run pytest tests/smoke/ -q --tb=short --maxfail=1                  PASS
```

No full Python suite, live provider, browser, load, or performance run is
claimed for this infrastructure milestone.

## Migration boundary evidence

- No production Python behavior, config schema, database schema, API route, or
  dashboard asset changed.
- No Cargo dependency changed; the harness is Python test infrastructure and
  uses the standard library only.
- The harness creates only temporary filesystem roots. Subprocesses run with
  bounded startup/command timeouts, and server contexts terminate their process
  groups. Timeout and server teardown are covered by tests.
- SQLite observation is read-only. It records schema and selected durable row
  counts, never page-write counts, WAL residue, timestamps, request bodies, or
  credentials.

## Known limitations and unresolved findings

F002 intentionally does not port Rust behavior. F003 owns config/CLI parity,
F004 owns SQLite access, and F005 owns the first Rust HTTP/SSR slice. The
current seed capture JSON records stable representative facts rather than a
full Python response corpus; future plans must add cases at the same boundary.

Unresolved findings by severity: none.

## Planning follow-through

F002 is removed from the dependency-ready section of
`migration-rs/registry.md` and recorded as completed. F003 and F004 are now
ready for handoff because F002's comparator, normalization policy, and seed
fixtures are closed. F005 is unblocked with respect to F002 but remains
blocked on its independent F004 interface dependency for DB-backed pages.
