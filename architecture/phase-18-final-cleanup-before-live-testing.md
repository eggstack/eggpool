# Phase 18: Final Cleanup Before Live Testing

## Purpose

Phase 17 resolved the major deployment blockers. Phase 18 is the
final operational cleanup before Raspberry Pi testing: it removes
ambiguity from the validation tooling so failures during live
testing indicate real runtime defects rather than weaknesses in
the test harness or operational scripts.

This phase is narrowly scoped: no new product features, no
routing changes, no live-reload behavior, no dashboard
redesigns. The goals are operational correctness, upgrade-test
quality, CLI consistency, maintenance-command ownership, optional
diagnostic privacy, and deployment-test reliability.

## Summary of changes

### 1. Database invariant checker is fail-closed

`scripts/check_database.py` previously treated query failures as
empty result sets via `except DatabaseError: return None/[]`. An
uninitialized or structurally incompatible database could report
`Database invariants OK` even when every invariant query had
silently failed.

The checker now:

- Defines `CheckerError` (base), `SchemaCompatibilityError`, and
  `InvariantQueryError` exception classes.
- Replaces `_safe_fetch_one()` / `_safe_fetch_all()` with
  `_fetch_one_checked()` / `_fetch_all_checked()` that raise
  `InvariantQueryError` on `DatabaseError`. Operator-facing
  errors do NOT include SQL text or parameter values.
- Treats a missing `_migrations` table, an empty `_migrations`
  table, and a `MAX(version)` older or newer than the supported
  range as schema-compatibility errors (exit code 2).
- Performs a required-tables-and-columns preflight
  (`_validate_required_schema`) before running any invariants.

Exit-code contract:

- `0` = all schema checks and invariants pass
- `1` = schema is valid but one or more data invariants fail
- `2` = database missing, unreadable, uninitialized,
  incompatible, or query execution failed

### 2. Historical upgrade fixture

The previous `_simulated_existing_db()` test built a database by
running the current migration set on an in-memory file. This did
not exercise the upgrade path. The new
`tests/fixtures/schema/pre_phase17_v11.sql` fixture reproduces
the v11 state without depending on the migration runner:

- `_migrations` rows for versions 1-11.
- All tables and indexes expected at v11.
- The historically correct `model_price_snapshots.source` default
  of `'config'` (matches the restored v5 migration).
- Representative rows in `accounts`, `models`,
  `model_price_snapshots`, `requests`, `request_attempts`, and
  `reservations` with synthetic non-secret values.

`tests/fixtures/schema/checksums.json` pins the fixture via
SHA-256. Any edit to the fixture fails the checksum test.

The new `tests/integration/test_migration_compatibility.py`
helper `_historical_v11_db(tmp_path)` creates a file-backed
SQLite database, applies the fixture exactly, reopens the file
through `Database`, runs the current `MigrationRunner`, and
verifies that no already-applied migration is re-executed. Schema
comparison is now behavioral (table names, column metadata, index
names, indexed columns) rather than raw `sqlite_master.sql` text.

The meaningless `assert len(versions) == len(versions)` is gone;
versions are derived from `SCHEMA_DIR.glob("*.sql")` and asserted
to match the recorded set with no gaps and no duplicates.

### 3. Operational scripts in CI

The CI workflow now runs `ruff format --check`, `ruff check`, and
`pyright` against `src/`, `tests/`, AND `scripts/`. The Pyright
configuration in `pyproject.toml` is updated to
`include = ["src", "scripts"]` with `tests` excluded.

Import-safety tests
(`tests/unit/test_script_import_safety.py`) assert that both
`scripts/check_database.py` and `scripts/smoke_test.py` can be
imported in a clean environment without:

- Reading any required environment variables.
- Opening a database or network connection.
- Emitting any output to stdout or stderr.

Operational scripts receive type validation in CI; narrow type
annotations (e.g. `cast`, `isinstance` narrowing) are preferred
over excluding the directory from Pyright.

### 4. `models refresh` synchronizes accounts

`go-aggregator models refresh` previously refreshed the catalog
without first persisting the configured accounts. The CLI now
mirrors the application lifespan:

```python
runner = MigrationRunner(db)
await runner.run()
account_repo = AccountRepository(db)
await account_repo.sync_from_config(account_config_rows(config), db)
registry = AccountRegistry(config)
client = httpx.AsyncClient(...)
catalog = CatalogService(config, registry, db, client)
await catalog.refresh()
```

`account_config_rows(config)` is a small helper added to
`go_aggregator.accounts.registry` that serializes configured
accounts to the dict shape used by `AccountRepository`. Both
`app.lifespan()` and `cli.models_refresh()` consume it, so the
two paths cannot drift.

A new file-backed integration test
(`tests/integration/test_cli_models_refresh.py`) covers the full
CLI flow plus the disabled-account semantics: a disabled
configured account is persisted as disabled and its
`account_models` rows are marked `enabled = 0`.

### 5. Explicit `Database.vacuum()` maintenance API

`VACUUM` cannot run inside a transaction. The CLI previously
called `await db.connection.execute("VACUUM")` directly, which
bypassed the connection lock and made concurrency rules
implicit.

A new `Database.vacuum()` method:

- Rejects read-only databases.
- Rejects calls from within an active transaction (any depth in
  the caller's task).
- Acquires `_connection_lock` for the complete operation.
- Executes `VACUUM`, consumes the cursor, and wraps failures in
  `DatabaseError`.
- Leaves the connection usable afterward.

The CLI `db vacuum` command uses the helper and reports a clean
error on failure. Concurrency tests verify that a transaction in
progress blocks `vacuum()` until it commits, and a `vacuum()` in
progress blocks a transaction from opening. A direct-connection
audit test fails if `cli.py` ever uses
`db.connection.execute("VACUUM")` again.

### 6. Allowlist-safe optional error-detail persistence

`security.persist_redacted_error_detail` remains fail-closed by
default (writes `NULL` when disabled). When enabled, the
redactor now uses a strict allowlist for JSON inputs:

- Only `SAFE_JSON_KEYS` are retained verbatim:
  `type`, `code`, `status`, `status_code`, `error_type`, `kind`,
  `param`, `message`, `request_id`, `trace_id`.
- Recognized sensitive keys (`api_key`, `authorization`,
  `password`, `secret`, `token`, ...) and user-content keys
  (`prompt`, `completion`, `input`, `messages`, ...) are retained
  with value `[REDACTED]` to preserve diagnostic shape.
- Arbitrary provider payload keys such as `payload`, `body`,
  `context`, `data`, `details`, `debug` are dropped entirely;
  nested objects underneath are NOT traversed into the output.
- Top-level JSON arrays have no stable diagnostic schema and are
  fail-closed to the literal `[REDACTED]`.
- The helper always returns a value bounded by
  `MAX_REDACTED_ERROR_DETAIL_CHARS = 2048`.

`docs/deployment.md` documents the new guarantee accurately:
"Error-detail persistence is disabled by default. When enabled,
GoRouter stores only a bounded allowlist of sanitized diagnostic
fields; arbitrary provider payload fields are discarded."

### 7. Fragmentation-safe stream diagnostics

`scripts/smoke_test.py` previously required the protocol-specific
SSE marker (`data:` for OpenAI, `event:` for Anthropic) to appear
within a single transport chunk. Real upstream responses split
arbitrary bytes across chunks, producing false smoke-test
failures.

The new `_RollingMarkerScanner` keeps a trailing byte buffer of
`max(len(marker) - 1, 0)` bytes and slides it forward through
each chunk. The full response body is never accumulated.

The streaming check now requires:

- `x-proxy-request-id` header (non-empty).
- `x-proxy-attempt-count` header (positive integer).
- A recognized terminal frame: `[DONE]` for OpenAI, `message_stop`
  for Anthropic when present in the upstream format.

The same header requirements apply to non-streaming checks.

Cancellation mode (`GOROUTER_TEST_STREAM_CANCEL=1`) is tracked
explicitly and skips the terminal-frame requirement, since
cancelling after the first chunk is the contract under test.

The script never prints response contents; it only emits
status codes, request IDs, attempt counts, and SSE marker
recognition.

### 8. Direct-upstream authentication verifier

A new operator-only script
`scripts/verify_upstream_auth.py` bypasses GoRouter and calls the
upstream OpenAI-compatible and Anthropic-compatible endpoints
directly using the same `Authorization: Bearer` header that
GoRouter emits. It is documented in `docs/raspberry-pi.md` and
`docs/deployment.md` but is **not** part of automated CI
execution.

Required environment:

- `GOROUTER_UPSTREAM_BASE_URL`
- `GOROUTER_TEST_UPSTREAM_KEY`
- `GOROUTER_OPENAI_MODEL`
- `GOROUTER_ANTHROPIC_MODEL`

The script:

- Sends one minimal non-streaming request to each endpoint
  family.
- Reports only status code, request ID (if present), and
  endpoint family.
- Never prints the key, body, prompt, or completion.
- Returns nonzero if either family rejects authentication.

Operational sequence for distinguishing upstream vs. proxy
defects:

1. Run `verify_upstream_auth.py` against the configured key and
   models.
2. Run `smoke_test.py` against the running proxy with the same
   models.
3. If direct succeeds but proxy fails, inspect header
   transformation and routing.
4. If both fail, treat the issue as upstream model/key
   compatibility rather than a proxy defect.

The script never enables HTTPX debug logging and command examples
in the docs use environment variables rather than command-line
key arguments. `.gitignore` covers common local secret/env file
patterns.

## Components

### Operational scripts

```
scripts/
├── check_database.py            # Read-only invariant checker
├── smoke_test.py                # Deployment smoke test
└── verify_upstream_auth.py      # Direct-upstream auth verifier
```

All three scripts are subject to ruff format, ruff check, and
pyright in CI. None of them reads required environment variables
or opens network/database connections at import time.

### Historical schema fixture

```
tests/fixtures/schema/
├── pre_phase17_v11.sql          # Hand-rolled v11 schema baseline
└── checksums.json               # SHA-256 manifest for the fixture
```

The fixture represents the production schema as it would look
immediately after migrations 1-11 have been applied. It is the
authoritative upgrade-compatibility baseline; any edit fails the
checksum test in
`tests/integration/test_migration_compatibility.py`.

### Regression matrix

`tests/integration/test_phase18_cleanup.py` is the cross-cutting
release gate. It contains 14 tests grouped A through H:

- **A. Checker fail-closed behavior** — empty file, valid schema,
  invariant violation.
- **B. Historical upgrade** — load fixture, reopen, run
  migrations, verify rows survive.
- **C. CLI catalog refresh** — two configured accounts, persisted
  account-model relationships.
- **D. Maintenance API** — `Database.vacuum()`, reopen, data
  intact.
- **E. Privacy** — enabled error-detail persistence drops unknown
  payload keys.
- **F. Streaming diagnostics** — fragmented marker recognized,
  missing proxy metadata fails.
- **G. CI configuration** — workflow includes `scripts/`, pyright
  includes `scripts`.
- **H. Migration integrity** — manifest covers all migrations,
  fixture manifest covers the historical fixture, no gaps or
  duplicates.

## Key decisions

1. **No new product features.** This phase only refactors
   existing code; it does not add routing strategies, account
   behaviors, or new endpoints.
2. **No live-reload behavior.** Configuration changes still
   require `sudo systemctl restart gorouter`.
3. **Single-process SQLite architecture preserved.** No
   additional processes or backends were introduced.
4. **No migration edits.** The plan explicitly forbids modifying
   any existing migration file; upgrade-compatibility is tested
   via a fresh historical baseline.
5. **Fail-closed privacy defaults are not weakened.** The
   allowlist narrows the surface for `error_detail` persistence
   but does not relax the default-deny default.
6. **No broad features, dashboard redesigns, or routing
   changes.** The plan constrains scope to operational hardening.
