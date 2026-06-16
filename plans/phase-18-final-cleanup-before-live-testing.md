# Phase 18: Final Cleanup Before Live Testing

## Purpose

Phase 17 resolved the major deployment blockers. The remaining work is localized to operational correctness, upgrade-test quality, CLI consistency, maintenance-command ownership, optional diagnostic privacy, and deployment-test reliability.

This phase is the final code cleanup before Raspberry Pi testing. It must not introduce new product features or redesign the router. The goal is to remove ambiguity from the validation tooling so failures during live testing indicate real runtime defects rather than weaknesses in the test harness.

---

## Implementation rules

1. Complete sections in order.
2. Add focused tests in the same commit as each behavior change.
3. Run focused tests after every section.
4. Run the full test, lint, format, and type-check suite before starting live testing.
5. Do not weaken fail-closed privacy defaults.
6. Do not modify any existing migration file.
7. Do not bypass the `Database` abstraction from production code or operational scripts.
8. Do not add live-reload behavior.
9. Do not add broad features, dashboard redesigns, or routing changes.
10. Preserve the single-process SQLite architecture and LAN deployment target.

---

## Phase exit outcomes

Phase 18 is complete when:

1. The database invariant checker fails closed on missing or incompatible schemas.
2. Invariant query failures cannot be mistaken for zero violations.
3. Upgrade compatibility tests use a real historical-schema fixture rather than two fresh databases.
4. Migration count assertions are meaningful.
5. CI checks `scripts/` for formatting and linting, and operational scripts receive type validation.
6. The current head has visible successful GitHub Actions checks.
7. `models refresh` synchronizes configured accounts before catalog persistence.
8. `db vacuum` uses an explicit lock-owned administrative database helper.
9. Optional persisted error detail uses an allowlist-safe structured representation.
10. Streaming smoke validation tolerates transport chunk fragmentation.
11. Live authentication behavior is verified against one current model from each upstream endpoint family.
12. The repository is ready to move directly into Raspberry Pi smoke and soak testing.

---

# 1. Make the database invariant checker fail closed

## Goal

Prevent an empty, uninitialized, partially migrated, or structurally incompatible database from producing `Database invariants OK`.

## Files

```text
scripts/check_database.py
tests/unit/test_check_database.py
tests/integration/test_phase18_cleanup.py
```

## Current defect

The checker currently treats several query failures as empty result sets:

```python
except DatabaseError:
    return []
```

If `_migrations` or a required table is absent, invariant queries can fail and still yield zero reported violations.

## Task 1.1: Introduce explicit checker exceptions

Add:

```python
class CheckerError(Exception):
    pass

class SchemaCompatibilityError(CheckerError):
    pass

class InvariantQueryError(CheckerError):
    pass
```

These are internal script exceptions and should not leak raw SQL parameters or secrets.

## Task 1.2: Replace silent safe-query fallbacks

Replace `_safe_fetch_one()` and `_safe_fetch_all()` with helpers that preserve the difference between:

- query succeeded and returned no rows;
- query failed because schema or database access is invalid.

Suggested shape:

```python
async def _fetch_one_checked(
    db: Database,
    sql: str,
    params: Sequence[Any] = (),
    *,
    check_name: str,
) -> aiosqlite.Row | None:
    try:
        return await db.fetch_one(sql, params)
    except DatabaseError as exc:
        raise InvariantQueryError(
            f"Invariant query failed: {check_name}"
        ) from exc
```

Do not include full SQL or parameter values in the operator-facing error.

Use the same design for `_fetch_all_checked()`.

## Task 1.3: Require the migration table

`_check_schema_version()` must treat a missing `_migrations` table as a configuration/schema error.

Required behavior:

```text
_migrations missing -> exit 2
_migrations empty -> exit 2
MAX(version) < expected -> exit 2
MAX(version) > expected -> exit 2
MAX(version) == expected -> continue
```

Suggested message:

```text
Database is not initialized with the expected GoRouter schema.
Run `go-aggregator migrate` before checking invariants.
```

## Task 1.4: Verify required tables and columns before invariants

Add a schema preflight function:

```python
async def _validate_required_schema(db: Database) -> None:
```

Required tables:

```text
accounts
models
account_models
requests
request_attempts
reservations
model_price_snapshots
account_events
health_probe
_migrations
```

Required columns should include every field referenced by the checker. At minimum:

```text
requests:
  id
  proxy_request_id
  status
  started_at
  cost_microdollars
  input_tokens
  output_tokens
  cache_read_tokens
  cache_write_tokens
  reasoning_tokens

request_attempts:
  id
  request_id
  completed_at

reservations:
  id
  request_id
  status

models:
  model_id
  protocol
  resolution_status

model_price_snapshots:
  source
```

Use `PRAGMA table_info(...)` through `Database.execute_pragma()` or normal read queries. Because the database is read-only, the helper must not mutate anything.

## Task 1.5: Return correct exit codes

Required mapping:

```text
0 = all schema checks and invariants pass
1 = schema is valid, but one or more data invariants fail
2 = database missing, unreadable, uninitialized, incompatible, or query execution failed
```

`main()` must catch `CheckerError` and return 2 with a concise message.

Unexpected exceptions should also return 2 after a generic diagnostic message. Do not print tracebacks unless a separate debug flag is explicitly enabled.

## Required tests

1. Missing database file returns 2.
2. Existing empty SQLite file returns 2.
3. Database with no `_migrations` table returns 2.
4. Database with `_migrations` but missing required tables returns 2.
5. Database at older schema version returns 2.
6. Database at newer schema version returns 2.
7. Required column missing returns 2.
8. Valid empty production schema returns 0.
9. Valid schema with one stale pending request returns 1.
10. Simulated invariant SQL failure returns 2, not 0.
11. Running the checker does not change the database file hash, WAL state, or schema.

## Section 1 acceptance criteria

- Query failure is never interpreted as no violation.
- An uninitialized database cannot pass the checker.
- Exit codes match the documented contract.

---

# 2. Replace the fake upgrade compatibility test with a historical fixture

## Goal

Verify that a database created by the previously released migration history upgrades correctly under current code.

## Files

```text
tests/fixtures/schema/
tests/integration/test_migration_compatibility.py
src/go_aggregator/db/schema/checksums.json
```

## Current defect

The existing `_fresh_db()` and `_simulated_existing_db()` both create a new database and run the current migration set. This does not exercise an upgrade path.

## Required design

Store an immutable historical schema fixture representing the repository immediately before Phase 17 migration restoration.

Preferred fixture:

```text
tests/fixtures/schema/pre_phase17_v11.sql
```

The fixture should contain either:

- a complete schema dump generated after migrations 1–11 using the historical migration contents; or
- an explicit sequence of historical SQL statements sufficient to reproduce that schema.

Do not create a binary SQLite file unless text SQL cannot reproduce the required state.

## Task 2.1: Create the historical fixture

The fixture must include:

- `_migrations` rows for versions 1 through 11;
- all tables and indexes expected at version 11;
- the historically correct `model_price_snapshots.source` default;
- at least one representative row in:
  - `accounts`;
  - `models`;
  - `model_price_snapshots`;
  - `requests`;
  - `request_attempts`;
  - `reservations`.

Use non-secret synthetic values.

## Task 2.2: Build the upgraded database from the fixture

Add:

```python
async def _historical_v11_db(tmp_path: Path) -> Database:
```

Required sequence:

1. Create a file-backed SQLite database.
2. Apply the fixture exactly.
3. Open it through `Database`.
4. Run the current `MigrationRunner`.
5. Verify no already-applied migration is re-executed.
6. Apply any migration newer than the fixture version when future versions exist.

File-backed storage is required because upgrade behavior should include actual reopen and durability semantics.

## Task 2.3: Compare behavioral schema, not raw SQL strings only

Raw `sqlite_master.sql` comparisons can differ for semantically equivalent schemas.

Compare:

- table names;
- column name/type/not-null/default/PK metadata;
- index names and indexed columns;
- migration version set;
- representative repository operations.

Required repository operations:

```text
insert and read an account
insert and finalize a request
insert and release a reservation
insert and read a price snapshot
read model protocol and resolution_status
```

## Task 2.4: Fix meaningless assertions

Replace:

```python
assert len(versions) == len(versions)
```

with:

```python
expected_versions = [
    int(path.stem.split("_")[0])
    for path in sorted(SCHEMA_DIR.glob("*.sql"))
]
assert versions == expected_versions
```

Also assert no gaps and no duplicates.

## Task 2.5: Keep checksum protection

Retain the checksum manifest test.

Add a test that the historical fixture has its own SHA-256 recorded in a small fixture manifest:

```text
tests/fixtures/schema/checksums.json
```

This prevents accidental mutation of the baseline upgrade fixture.

## Required tests

1. Fresh schema reaches latest version.
2. Historical v11 fixture opens successfully.
3. Running current migrations on fixture is idempotent.
4. Fresh and upgraded schemas have equivalent production behavior.
5. Representative pre-existing rows survive upgrade.
6. Migration versions exactly match files on disk.
7. Existing migration checksum manifest passes.
8. Historical fixture checksum passes.

## Section 2 acceptance criteria

- Upgrade testing no longer compares two fresh databases.
- Pre-existing rows and schema survive a real reopen-and-upgrade path.

---

# 3. Bring operational scripts into CI

## Goal

Ensure the scripts used as deployment release gates receive formatting, linting, and type validation.

## Files

```text
.github/workflows/ci.yml
pyproject.toml
scripts/check_database.py
scripts/smoke_test.py
```

## Task 3.1: Expand Ruff coverage

Change CI to run:

```text
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
```

The local development instructions must use the same paths.

## Task 3.2: Type-check operational scripts

Preferred approach:

```toml
[tool.pyright]
include = ["src", "scripts"]
exclude = ["tests"]
```

Then run:

```text
uv run pyright src/ scripts/
```

If strict mode produces script-specific issues that are not worth broad refactoring, use narrowly scoped annotations. Do not exclude the entire scripts directory.

## Task 3.3: Add script import smoke tests

Add tests that import both scripts in a clean environment without executing `main()`.

Assert:

- no required environment variables are read at import time;
- no database or network connection is opened at import time;
- no output is emitted at import time.

## Task 3.4: Confirm current-head CI visibility

After pushing this phase:

1. Verify the GitHub Actions workflow triggers on `main`.
2. Confirm visible successful checks for:
   - lint;
   - typecheck;
   - test.
3. Confirm the coverage artifact exists.
4. Record the verified commit SHA in the final testing notes.

Do not mark Phase 18 complete based only on local command output.

## Section 3 acceptance criteria

- Operational scripts are linted and type-checked in CI.
- Current head visibly reports green GitHub checks.

---

# 4. Synchronize accounts in `models refresh`

## Goal

Make the standalone catalog refresh command produce the same persisted account/model relationships as normal application startup.

## Files

```text
src/go_aggregator/cli.py
src/go_aggregator/db/repositories.py
tests/integration/test_cli_models_refresh.py
```

## Task 4.1: Extract account config serialization

Avoid duplicating the account dictionary construction between app startup and CLI.

Add a helper in an appropriate module:

```python
def account_config_rows(config: AppConfig) -> list[dict[str, Any]]:
```

Output fields:

```text
name
api_key_env
enabled
weight
```

Use it in both:

- `app.lifespan()`;
- `cli.models_refresh()`.

## Task 4.2: Synchronize accounts before catalog refresh

In `models_refresh`:

```python
runner = MigrationRunner(db)
await runner.run()

account_repo = AccountRepository(db)
await account_repo.sync_from_config(account_config_rows(config), db)

registry = AccountRegistry(config)
...
await catalog.refresh()
```

Account synchronization must complete before catalog persistence begins.

## Task 4.3: Add file-backed CLI integration test

Use Click's test runner and a temporary TOML config.

Test sequence:

1. Create a fresh file-backed database path.
2. Configure two accounts with synthetic environment keys.
3. Mock the upstream model catalog.
4. Invoke `models refresh`.
5. Reopen the database.
6. Assert both accounts exist.
7. Assert models exist.
8. Assert `account_models` contains the expected relationships.
9. Set `models.startup_refresh = false`.
10. Start the application and verify cached models are routable.

## Task 4.4: Preserve disabled-account semantics

Configured disabled accounts should be persisted as disabled, and their model relationships should not make them eligible for routing.

Add a test with one enabled and one disabled account.

## Section 4 acceptance criteria

- `models refresh` is safe on a fresh database.
- Cached model support remains routable when startup refresh is disabled.

---

# 5. Add an explicit database maintenance API for VACUUM

## Goal

Remove the final direct production use of `db.connection.execute(...)` and encode maintenance-operation locking rules in `Database`.

## Files

```text
src/go_aggregator/db/connection.py
src/go_aggregator/cli.py
tests/integration/test_database_maintenance.py
```

## Task 5.1: Add `vacuum()`

Add:

```python
async def vacuum(self) -> None:
```

Required behavior:

- reject read-only databases;
- reject calls from within an active transaction;
- acquire `_connection_lock` for the complete operation;
- execute `VACUUM`;
- consume/close the cursor;
- wrap failures in `DatabaseError`;
- leave the connection usable afterward.

Because `VACUUM` cannot run inside a transaction, it must not use `Database.transaction()`.

Suggested precondition:

```python
if self._transaction_owner is not None:
    raise DatabaseError("VACUUM cannot run while a transaction is active")
```

The lock is still required to prevent a transaction from starting concurrently.

## Task 5.2: Use the helper in CLI

Replace:

```python
await db.connection.execute("VACUUM")
```

with:

```python
await db.vacuum()
```

## Task 5.3: Audit direct connection access

Search production code for:

```text
.connection.execute
.connection.commit
.connection.rollback
.connection.cursor
```

Allowed direct uses should be confined to `Database` internals and migration internals already protected by transaction ownership.

## Required tests

1. `vacuum()` succeeds on a writable file-backed database.
2. Data remains intact after vacuum.
3. Connection remains usable after vacuum.
4. Read-only database rejects vacuum.
5. Calling vacuum inside a transaction fails.
6. Concurrent transaction waits until vacuum completes or vice versa without interleaving.
7. CLI `db vacuum` exits successfully.
8. CLI reports a clean error on failure.

## Section 5 acceptance criteria

- Operational maintenance no longer bypasses `Database`.
- VACUUM concurrency semantics are explicit and tested.

---

# 6. Make optional persisted error detail allowlist-safe

## Goal

Keep the secure default while ensuring that explicitly enabled diagnostic persistence does not retain arbitrary provider payload fields.

## Files

```text
src/go_aggregator/security/redaction.py
tests/unit/test_redaction.py
tests/integration/test_error_detail_privacy.py
docs/deployment.md
```

## Required policy

Default remains:

```python
persist_redacted_error_detail = False
```

When disabled, `error_detail` must remain `NULL`.

When enabled, structured JSON should preserve only an allowlisted diagnostic subset.

## Task 6.1: Change structured sanitization from blocklist to allowlist

For JSON objects, keep only these keys, case-insensitively:

```text
type
code
status
status_code
error_type
kind
param
message
request_id
trace_id
```

Behavior:

- recognized sensitive and content keys may be retained with value `[REDACTED]` if useful for diagnostics;
- arbitrary keys such as `payload`, `body`, `context`, `data`, `details`, or `debug` must be dropped entirely;
- nested objects under non-allowlisted keys must not be traversed into the output;
- `message` is retained only after string redaction and length bounding.

Suggested result for:

```json
{
  "type": "invalid_request",
  "message": "bad token sk-secret",
  "payload": "private source code"
}
```

is:

```json
{
  "type": "invalid_request",
  "message": "bad token [REDACTED]"
}
```

## Task 6.2: Handle top-level arrays conservatively

A provider error array has no stable diagnostic schema.

Preferred behavior:

```text
top-level JSON array -> [REDACTED]
```

Alternative acceptable behavior:

- retain only allowlisted object fields from at most the first few objects;
- discard scalar array entries.

Prefer the simpler fail-closed behavior.

## Task 6.3: Keep plain-text redaction bounded

Non-JSON text may continue through regex redaction, but enforce a maximum serialized length inside `redact_error_detail()` rather than relying solely on finalizer callers.

Add:

```python
MAX_REDACTED_ERROR_DETAIL_CHARS = 2048
```

The helper should return an already bounded value.

## Task 6.4: Document the guarantee accurately

Document:

```text
Error-detail persistence is disabled by default. When enabled,
GoRouter stores only a bounded allowlist of sanitized diagnostic
fields; arbitrary provider payload fields are discarded.
```

Do not describe it as lossless provider diagnostics.

## Required tests

1. Default finalizer behavior persists `NULL`.
2. `payload` containing private source code is dropped.
3. `data`, `details`, and nested unknown keys are dropped.
4. Safe `type`, `code`, `status`, and request IDs are retained.
5. `message` is redacted and bounded.
6. Top-level arrays are fail-closed.
7. Prompt, completion, messages, input, API key, token, and authorization markers are absent.
8. Database-wide secret scan passes after enabled diagnostic persistence.

## Section 6 acceptance criteria

- Optional error-detail persistence cannot retain arbitrary provider payloads.
- The default remains fail-closed.

---

# 7. Make stream-marker detection robust to chunk boundaries

## Goal

Avoid false smoke-test failures when an SSE marker is split across HTTP transport chunks.

## Files

```text
scripts/smoke_test.py
tests/unit/test_smoke_test.py
```

## Task 7.1: Add rolling marker detection

Maintain a trailing byte buffer of:

```python
max(len(required_marker) - 1, 0)
```

For every chunk:

```python
combined = tail + chunk
if required_marker in combined:
    saw_marker = True
tail = combined[-tail_length:]
```

Do not accumulate the full response body.

## Task 7.2: Validate attempt-count header

The current check records an empty attempt-count header but does not fail on it.

Require both:

```text
x-proxy-request-id
x-proxy-attempt-count
```

Parse attempt count as a positive integer.

Apply the same requirement to non-streaming checks.

## Task 7.3: Validate completion semantics

For a normal OpenAI stream, require either:

```text
[DONE]
```

or another explicitly supported terminal frame emitted by the proxy.

For Anthropic streams, require a recognized terminal event such as `message_stop` when present in the supported upstream format.

Cancellation mode should not require terminal completion.

Track separate state:

```text
saw_stream_marker
saw_terminal_marker
cancelled_intentionally
```

## Task 7.4: Keep content private

Tests may use synthetic marker payloads, but the live script must not print model text or complete chunks.

## Required tests

1. `data:` split across two chunks is detected.
2. `event:` split across three chunks is detected.
3. Missing request ID fails.
4. Missing attempt count fails.
5. Non-integer attempt count fails.
6. Normal stream without terminal marker fails when terminal marker is required.
7. Cancellation mode succeeds after first chunk without terminal marker.
8. Full response contents never appear in formatted output.

## Section 7 acceptance criteria

- Arbitrary transport chunking does not cause false failures.
- Header and terminal-frame validation is stricter.

---

# 8. Add a live upstream authentication validation procedure

## Goal

Confirm empirically that the selected OpenCode Go subscription credential works for both endpoint families using the exact authentication header emitted by GoRouter.

This section adds test tooling and documentation. It must not embed real keys or call live services in CI.

## Files

```text
scripts/smoke_test.py
scripts/verify_upstream_auth.py
docs/raspberry-pi.md
docs/deployment.md
.gitignore
```

## Task 8.1: Add an optional direct-upstream authentication verifier

Create:

```text
scripts/verify_upstream_auth.py
```

Required environment:

```text
GOROUTER_UPSTREAM_BASE_URL
GOROUTER_TEST_UPSTREAM_KEY
GOROUTER_OPENAI_MODEL
GOROUTER_ANTHROPIC_MODEL
```

The script should:

1. Send one minimal non-streaming OpenAI-compatible request directly upstream using `Authorization: Bearer`.
2. Send one minimal non-streaming Anthropic-compatible request directly upstream using the same authentication scheme.
3. Report only status code, request ID if present, and endpoint family.
4. Never print the key, body, prompt, or completion.
5. Return nonzero if either family rejects authentication.

Do not add this script to automated CI execution.

## Task 8.2: Compare direct and proxied behavior

Document the deployment sequence:

1. Verify the key directly against each endpoint family.
2. Run GoRouter smoke test using the same models.
3. If direct succeeds but proxy fails, inspect header transformation/routing.
4. If both fail, treat it as upstream model/key compatibility rather than proxy failure.

## Task 8.3: Require current advertised model IDs

Before live calls, query GoRouter `/v1/models` and verify configured smoke-test model IDs exist.

The direct verifier cannot rely on GoRouter's model list, so it should require explicit operator-provided IDs.

Document that model examples are illustrative and may change.

## Task 8.4: Prevent accidental secret capture

- Ensure the verifier never enables HTTPX debug logging.
- Ensure command examples use environment variables rather than command-line key arguments.
- Add common local secret/env files to `.gitignore` if not already covered.
- Document clearing shell history if operators manually exported real keys in an interactive shell.

## Required tests

Unit tests with mocked HTTP transport must verify:

1. Both endpoint families receive exactly one bearer header.
2. No `x-api-key` is sent.
3. Key value is absent from stdout/stderr.
4. Response content is absent from stdout/stderr.
5. One failed endpoint produces nonzero exit status.
6. Transport errors produce a concise non-secret diagnostic.

## Section 8 acceptance criteria

- The deployment operator has a deterministic way to distinguish upstream auth incompatibility from proxy defects.
- No real credentials are introduced into tests or repository content.

---

# 9. Final cleanup regression matrix

Create:

```text
tests/integration/test_phase18_cleanup.py
```

This file should provide high-level release-gate coverage without duplicating all detailed unit tests.

## A. Checker fail-closed behavior

- Empty SQLite file returns 2.
- Valid schema returns 0.
- Invariant violation returns 1.

## B. Historical upgrade

- Load historical fixture.
- Reopen and run current migration runner.
- Verify representative rows survive.

## C. CLI catalog refresh

- Fresh database plus two configured accounts.
- Refresh catalog.
- Verify account-model relationships.

## D. Maintenance API

- Vacuum through `Database.vacuum()`.
- Reopen and verify data.

## E. Privacy

- Enabled error-detail persistence drops unknown payload keys.
- Database-wide marker scan passes.

## F. Streaming diagnostics

- Fragmented marker is recognized.
- Missing proxy metadata headers fail.

## G. CI configuration

- CI workflow includes `scripts/` in Ruff commands.
- Pyright configuration includes `scripts`.

## H. Migration integrity

- Migration manifest covers all migration files.
- Historical fixture manifest covers all historical fixtures.

---

# 10. Required command matrix

After implementation, run exactly:

```text
uv sync --frozen --extra dev
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/unit/test_check_database.py
uv run pytest tests/integration/test_migration_compatibility.py
uv run pytest tests/integration/test_cli_models_refresh.py
uv run pytest tests/integration/test_database_maintenance.py
uv run pytest tests/unit/test_redaction.py tests/integration/test_error_detail_privacy.py
uv run pytest tests/unit/test_smoke_test.py
uv run pytest tests/integration/test_phase18_cleanup.py
uv run pytest --cov=go_aggregator --cov-report=term-missing --cov-report=xml
```

Then verify visible GitHub Actions success on the pushed head commit.

---

# 11. Recommended implementation order

Follow this order exactly:

1. Invariant checker fail-closed behavior.
2. Historical migration fixture and upgrade tests.
3. CI coverage for operational scripts.
4. `models refresh` account synchronization.
5. Explicit `Database.vacuum()` maintenance path.
6. Allowlist-safe optional error-detail persistence.
7. Fragmentation-safe stream diagnostics.
8. Direct-upstream auth verification script and documentation.
9. Phase 18 regression matrix.
10. Full local command matrix.
11. Visible GitHub CI verification.
12. Raspberry Pi smoke and soak testing.

---

# 12. Suggested commit sequence

```text
fix: make database checker fail closed on schema errors
test: add historical schema upgrade fixture
ci: validate operational scripts
fix: sync accounts before cli catalog refresh
refactor: add lock-owned database vacuum operation
security: allowlist persisted diagnostic error fields
ops: handle fragmented streaming markers
ops: add direct upstream authentication verifier
test: add phase 18 cleanup regression matrix
docs: finalize live testing procedure
```

---

# 13. Definition of done

Phase 18 is complete only when:

1. The invariant checker cannot return success for an empty or incompatible database.
2. Upgrade compatibility uses a real historical baseline fixture.
3. Migration assertions and fixture checksums are meaningful.
4. `scripts/` pass formatting, linting, and type checking in CI.
5. GitHub displays successful checks for the current head.
6. `models refresh` produces complete persisted account-model relationships on a fresh database.
7. No production CLI path directly executes SQL through `db.connection`.
8. Optional persisted diagnostics retain only an allowlisted bounded subset.
9. Stream diagnostics tolerate arbitrary chunk boundaries and validate required proxy headers.
10. Direct and proxied upstream authentication can be tested without exposing credentials.
11. The full Phase 18 regression matrix passes.
12. The repository can move directly into Raspberry Pi smoke testing and soak testing without another code-planning phase.
