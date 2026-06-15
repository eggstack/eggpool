# Phase 17: Deployment Readiness Corrections

## Purpose

Phase 16 implemented the intended release-polish architecture, but the final review identified several defects that must be corrected before GoRouter is run with real OpenCode Go subscription keys.

This phase is narrowly scoped. It is not another architecture pass. The implementing model should fix the remaining deployment blockers, close the missing regression coverage, and make the operational tooling accurately reflect runtime behavior.

The highest-priority issues are:

1. Standalone SQLite writes can leave implicit transactions open.
2. A locally supplied `x-api-key` can be forwarded to the upstream service.
3. CI invokes `pytest --cov` without declaring `pytest-cov`.
4. Historical migration `0005` was modified after publication.
5. The raw cursor API is documented as restricted but does not enforce ownership.
6. The intended coordinator-level 402 regression test is still missing.
7. The smoke test does not validate incremental streaming.
8. The systemd unit advertises unsupported SIGHUP reload behavior.
9. Error-detail redaction should cover common JSON secret forms or become fail-closed.
10. The database checker and smoke-test documentation need minor operational tightening.

---

## Implementation rules

1. Complete sections in the listed order.
2. Do not begin Raspberry Pi deployment testing until Sections 1–5 pass locally and in CI.
3. Add regression tests in the same commit as each behavior change.
4. Run focused tests after each section.
5. Run the full suite before beginning the next section.
6. Do not modify an already-published migration except to restore its exact prior contents.
7. Do not add a second database implementation or change away from SQLite.
8. Do not weaken authentication or credential filtering.
9. Do not persist or log raw request bodies, prompts, completions, authorization headers, API keys, or unredacted provider detail.
10. Any helper that performs database writes must have explicit transaction semantics.

---

# Phase exit outcomes

Phase 17 is complete when all of the following are true:

1. No standalone database write can leave an implicit SQLite transaction open.
2. Fresh application startup with configured accounts and a file-backed database succeeds.
3. Local proxy credentials are never forwarded upstream in any header form.
4. CI has all required dependencies and visibly passes on the head commit.
5. Migration history is deterministic across old and fresh installations.
6. Raw cursor access is enforced as transaction-owner-only.
7. A real coordinator-level 402 test verifies failover, cross-request cooldown, and recovery.
8. The smoke test verifies incremental streaming rather than only buffered success.
9. The systemd unit exposes only supported lifecycle operations.
10. Persisted error-detail redaction covers common JSON credential forms or error detail is disabled by default.
11. Operational scripts are safe and accurately documented.
12. The final release-validation matrix passes locally, in CI, and on the Raspberry Pi target.

---

# 1. Enforce explicit database write transactions

## Goal

Prevent `execute_write()` and `execute_insert()` from creating implicit SQLite transactions that remain open after the helper returns.

The preferred design is strict transaction ownership: every DML write must occur inside `async with db.transaction():`.

## Files

```text
src/go_aggregator/db/connection.py
src/go_aggregator/db/repositories.py
src/go_aggregator/catalog/pricing.py
src/go_aggregator/catalog/service.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/app.py
src/go_aggregator/quota/reservation.py
tests/integration/test_database_transaction_contract.py
tests/integration/test_application_startup.py
```

## Task 1.1: Make write helpers require transaction ownership

In `Database`, add:

```python
def _require_transaction_owner(self) -> None:
    if not self._current_task_owns_transaction():
        raise DatabaseError(
            "Database writes require an owned transaction; "
            "use 'async with db.transaction():'"
        )
```

Call it at the beginning of:

```text
execute_write
execute_insert
execute_returning when the SQL is mutating
```

For `execute_returning()`, this phase may simply require a transaction for every call. All known production uses are write-returning operations.

Do not automatically commit one-statement writes in these helpers. Explicit multi-statement atomicity is easier to audit and avoids mixed semantics.

## Task 1.2: Audit every production write caller

Search for:

```text
execute_write(
execute_insert(
execute_returning(
_execute_cursor(
connection.execute(
connection.commit(
connection.rollback(
```

Every production DML caller must either:

- already execute inside `Database.transaction()`, or
- be wrapped in a clearly owned transaction.

Do not rely on call-site comments.

## Task 1.3: Wrap account configuration synchronization atomically

`AccountRepository.sync_from_config()` currently performs multiple reads and writes.

Choose one ownership boundary and use it consistently.

Preferred:

```python
async def sync_from_config(...):
    async with db.transaction():
        return await self._sync_from_config_locked(...)
```

The complete operation must be atomic:

- update existing configured accounts;
- insert new configured accounts;
- disable accounts removed from configuration;
- return the final name-to-ID map.

Do not open nested transactions per account.

## Task 1.4: Fix standalone exhausted-request update

In `RequestCoordinator._handle_exhausted()`, the branch that updates an existing request without a selected attempt must use:

```python
async with self._db.transaction():
    await self._db.execute_write(...)
```

Preserve redaction before persistence.

## Task 1.5: Verify catalog and pricing boundaries

Ensure:

- `_persist_catalog()` owns one transaction for the complete catalog persistence pass;
- price snapshot insertion inherits that transaction or owns a clearly separate one;
- repository methods do not commit internally.

If `record_snapshot()` is also used independently outside catalog persistence, split it into:

```python
async def record_snapshot(...):
    async with db.transaction():
        await self._record_snapshot_locked(...)
```

and use the locked helper from an existing transaction.

## Task 1.6: Add a fresh-start integration test

Create a test that exercises the real application lifespan with:

- a temporary file-backed SQLite database;
- at least two configured accounts;
- valid local auth environment variable;
- mocked upstream catalog response;
- migrations enabled;
- crash recovery enabled.

Assert:

1. Application startup completes.
2. Accounts are committed and visible after startup.
3. Crash recovery can begin a transaction immediately after account synchronization.
4. No `cannot start a transaction within a transaction` exception occurs.
5. Shutdown closes the connection cleanly.
6. Reopening the database confirms account rows persisted.

## Task 1.7: Add contract tests

Required tests:

```text
execute_write outside transaction -> DatabaseError
execute_insert outside transaction -> DatabaseError
execute_returning outside transaction -> DatabaseError
same helpers inside transaction -> success
nested same-task transaction -> success
child task cannot inherit transaction ownership
```

## Section 1 acceptance criteria

- Every production write is transaction-owned.
- Fresh startup with configured accounts succeeds.
- No implicit transaction remains open after any helper returns.

---

# 2. Prevent local credential forwarding upstream

## Goal

Guarantee that credentials used to authenticate a client to GoRouter are removed before forwarding the request upstream.

## Files

```text
src/go_aggregator/proxy/client.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/auth.py
tests/unit/test_header_filtering.py
tests/integration/test_upstream_credential_boundary.py
scripts/smoke_test.py
```

## Task 2.1: Define credential-bearing inbound headers

Add one explicit set:

```python
LOCAL_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "proxy-authorization",
    }
)
```

Also strip any project-specific local-auth alias if one exists.

## Task 2.2: Strip all local credentials

In `filter_request_headers()`:

```python
if lower_key in LOCAL_CREDENTIAL_HEADERS:
    continue
```

Preserve the existing hop-by-hop and host/content-length removal.

No client-supplied credential-bearing header should survive filtering.

## Task 2.3: Inject protocol-appropriate upstream authentication

Centralize upstream authentication injection.

For the current OpenCode Go gateway, confirm the expected credential scheme from existing working behavior and use one explicit helper:

```python
def build_upstream_auth_headers(
    protocol: str,
    upstream_api_key: str,
) -> dict[str, str]:
    ...
```

If the same bearer header is valid for both protocol endpoint families, return only:

```python
{"Authorization": f"Bearer {upstream_api_key}"}
```

Do not inject the local key into `x-api-key` for Anthropic-compatible payloads merely because the local endpoint accepts that header.

## Task 2.4: Add a credential-boundary integration test

Use a mocked upstream transport.

Send a local request containing distinctive markers in:

```text
Authorization: Bearer LOCAL_BEARER_SECRET
x-api-key: LOCAL_X_API_SECRET
Proxy-Authorization: Basic LOCAL_PROXY_SECRET
```

Configure the selected account with:

```text
UPSTREAM_ACCOUNT_SECRET
```

Assert the upstream receives:

- no local bearer marker;
- no local `x-api-key` marker;
- no proxy-authorization marker;
- exactly the expected upstream authorization header;
- no duplicate authorization fields.

Run this test through both local endpoint families:

```text
/v1/chat/completions
/v1/messages
```

## Task 2.5: Correct smoke-test headers

The smoke test should authenticate to GoRouter using one supported local mechanism only.

Preferred:

```python
headers={"authorization": f"Bearer {api_key}"}
```

Do not send the same local key as both `Authorization` and `x-api-key`.

## Section 2 acceptance criteria

- No local client credential reaches upstream.
- Upstream receives only the selected account credential.
- Both protocol endpoint families are covered by regression tests.

---

# 3. Repair CI dependency and make status verifiable

## Goal

Make the coverage job runnable in a clean frozen environment and ensure GitHub displays passing checks on the head commit.

## Files

```text
pyproject.toml
uv.lock
.github/workflows/ci.yml
```

## Task 3.1: Add `pytest-cov`

Add to the development extra:

```toml
"pytest-cov",
```

Retain `coverage[toml]` only if it is used directly or for configuration support.

Regenerate `uv.lock` using the repository's normal uv workflow.

## Task 3.2: Validate the frozen environment locally

Run exactly:

```text
uv sync --frozen --extra dev
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest --cov=go_aggregator --cov-report=term-missing --cov-report=xml
```

If scripts are intentionally excluded from strict type checking, document that decision in CI rather than leaving them unverified accidentally.

## Task 3.3: Confirm workflow triggering

The workflow must remain enabled for:

```yaml
push:
  branches: [main]
pull_request:
  branches: [main]
```

After pushing the corrective commit, verify GitHub shows separate successful checks for:

```text
lint
typecheck
test
```

Do not rely only on commit-message claims.

## Task 3.4: Add workflow dependency smoke assertion

Optional but useful: in the test job, run:

```text
uv run pytest --help | grep -- --cov
```

This fails early if the plugin disappears from the lockfile.

## Section 3 acceptance criteria

- `pytest --cov` works in a clean frozen checkout.
- Coverage artifact uploads successfully.
- Head commit has visible green CI checks.

---

# 4. Restore migration immutability

## Goal

Ensure all installations apply the same schema history regardless of installation date.

## Files

```text
src/go_aggregator/db/schema/0005_price_microdollars.sql
src/go_aggregator/db/schema/0012_price_source_default.sql
tests/integration/test_migration_compatibility.py
```

## Task 4.1: Restore migration `0005`

Restore the `source` column declaration to its exact historical value:

```sql
source TEXT NOT NULL DEFAULT 'config'
```

Do not make any other edits to `0005`.

## Task 4.2: Add a forward migration only if a default correction is needed

If the desired schema default is `upstream`, create:

```text
0012_price_source_default.sql
```

SQLite cannot directly alter a column default. Before attempting a table rebuild, determine whether a default correction is necessary.

Because production insert paths explicitly provide `source`, the preferred action is:

- restore `0005`;
- do not rebuild the table;
- document that inserts must always specify source.

Only add `0012` if a real code path depends on the database default.

## Task 4.3: Add migration compatibility tests

Create two database fixtures:

### Fresh database

Apply all migrations from zero and inspect:

- columns;
- indexes;
- price snapshot insert behavior;
- latest schema version.

### Simulated existing database

Apply migrations through version 11 using the historical `0005`, then run the current migration runner.

Assert both databases have behaviorally equivalent schemas for production operations.

## Task 4.4: Detect future migration edits

Add a lightweight migration manifest test or checksum file.

Preferred:

```text
src/go_aggregator/db/schema/checksums.json
```

The test computes SHA-256 for every applied migration and compares it to the manifest.

Updating an existing checksum should require an explicit review decision. New migrations append new entries.

## Section 4 acceptance criteria

- Historical migration contents are restored.
- Fresh and upgraded databases behave equivalently.
- Future accidental edits to applied migrations fail tests.

---

# 5. Enforce the raw cursor API contract

## Goal

Make it impossible for a raw cursor to escape connection ownership.

## Files

```text
src/go_aggregator/db/connection.py
src/go_aggregator/db/migrations.py
src/go_aggregator/background/cleanup.py
tests/integration/test_cursor_ownership.py
```

## Task 5.1: Enforce transaction ownership

At the beginning of `_execute_cursor()`:

```python
if not self._current_task_owns_transaction():
    raise DatabaseError(
        "Raw cursor access is restricted to the current transaction owner"
    )
```

Do not acquire `_connection_access()` inside this method. The transaction owner already holds the connection lock.

Implementation shape:

```python
async def _execute_cursor(...):
    if not self._current_task_owns_transaction():
        raise DatabaseError(...)
    try:
        return await self.connection.execute(sql, params)
    except Exception as exc:
        raise DatabaseError(...) from exc
```

## Task 5.2: Remove the public legacy wrapper

Delete public `execute()` unless external compatibility is demonstrably required.

Tests and seed helpers should use:

```text
execute_write
execute_insert
execute_returning
fetch_one
fetch_all
```

inside explicit transactions.

Do not keep a deprecated unsafe path solely for test convenience.

## Task 5.3: Add a dedicated PRAGMA helper

Checkpointing currently uses raw cursor access outside a transaction.

Add:

```python
async def execute_pragma(self, sql: str) -> list[aiosqlite.Row]:
```

Requirements:

- accept only SQL beginning with `PRAGMA ` after whitespace normalization;
- hold `_connection_lock` for execution and fetch;
- consume the cursor before releasing the lock;
- return rows when the PRAGMA produces rows.

Use it for:

```text
PRAGMA wal_checkpoint(PASSIVE)
```

Do not use `_execute_cursor()` for standalone checkpointing.

## Task 5.4: Keep migrations transaction-owned

Migration DDL execution may continue using `_execute_cursor()` only inside `Database.transaction()`.

Add a test that calling `_execute_cursor()` outside a transaction fails.

## Required tests

```text
raw cursor outside transaction -> DatabaseError
raw cursor inside owned transaction -> success
raw cursor in child task inheriting ContextVar -> DatabaseError or waits for its own transaction
execute_pragma consumes results under lock
concurrent checkpoint and transaction do not interleave
```

## Section 5 acceptance criteria

- No raw cursor can escape lock ownership.
- Production code contains no public cursor-returning database API.

---

# 6. Add a real coordinator-level 402 lifecycle test

## Goal

Verify the complete quota-exhaustion behavior through request execution, not only through pure classification methods.

## Files

```text
tests/integration/test_phase17_release_validation.py
tests/integration/test_quota_cooldown.py
```

## Required scenario

Configure:

```text
Account A supports model M
Account B supports model M
A is initially preferred
A upstream response: 402
B upstream response: 200
quota cooldown: short monkeypatched duration
```

Execute:

1. Send request 1.
2. Assert attempt 1 uses A and receives 402.
3. Assert attempt 2 uses B and succeeds.
4. Assert A's `HealthManager` state is `quota_exhausted`.
5. Assert A's `AccountRuntimeState` state is `quota_exhausted`.
6. Send independent request 2 before cooldown expiration.
7. Assert A is not attempted.
8. Advance the mocked clock beyond cooldown.
9. Send independent request 3.
10. Assert A is eligible again.

Also assert:

- all attempts are terminal;
- all reservations are released;
- no active count remains for completed requests;
- the 402 attempt affects health once;
- no raw provider body is persisted.

Do not use `time.sleep()` in this test. Monkeypatch the relevant time source.

## Section 6 acceptance criteria

- Real 402 behavior is covered end to end.
- Cross-request cooldown and recovery are release-gated.

---

# 7. Make the smoke test validate actual streaming

## Goal

Confirm incremental delivery and proper stream completion on the target deployment.

## Files

```text
scripts/smoke_test.py
tests/unit/test_smoke_test.py
```

## Task 7.1: Use `httpx.Client.stream()`

For streaming OpenAI and Anthropic requests:

```python
with client.stream(
    "POST",
    url,
    headers=headers,
    json=payload,
    timeout=DEFAULT_TIMEOUT,
) as response:
    ...
```

## Task 7.2: Validate first-byte delivery

Record:

```text
request start
response headers received
first nonempty chunk received
stream completion
```

Require:

- successful HTTP status;
- proxy request ID header;
- proxy attempt-count header;
- nonempty first chunk;
- complete consumption without transport error;
- response context exits cleanly.

Do not print chunk contents.

## Task 7.3: Validate SSE structure minimally

For OpenAI-compatible streaming, require at least one of:

```text
data:
[DONE]
```

For Anthropic-compatible streaming, require at least one valid event/data frame marker expected from the proxy path.

Do not parse or print model-generated text.

## Task 7.4: Add cancellation smoke option

Optional environment-controlled test:

```text
GOROUTER_TEST_STREAM_CANCEL=1
```

When enabled:

1. open a stream;
2. read the first nonempty chunk;
3. close the response early;
4. wait briefly;
5. run database invariant endpoint/script afterward.

This must remain optional because live provider timing is variable.

## Task 7.5: Correct environment documentation

Either require both model environment variables or clearly label defaults as examples.

Preferred: require explicit values so stale generic IDs do not produce misleading deployment failures.

## Section 7 acceptance criteria

- The smoke test verifies incremental streaming rather than buffered success.
- No response content or secret is printed.

---

# 8. Correct systemd lifecycle behavior

## Goal

Remove unsupported reload semantics and make the unit reproducible on Ubuntu/Raspberry Pi.

## Files

```text
ops/gorouter.service
README.md
```

## Task 8.1: Remove unsupported `ExecReload`

Delete:

```ini
ExecReload=/bin/kill -HUP $MAINPID
```

Do not add another reload command until GoRouter has a tested configuration-reload implementation.

## Task 8.2: Document restart workflow

Document:

```text
sudo systemctl restart gorouter
sudo systemctl status gorouter
journalctl -u gorouter -n 100 --no-pager
```

for configuration and key changes.

## Task 8.3: Validate hardening directives

On the target Pi, run:

```text
systemd-analyze security gorouter.service
systemd-analyze verify ops/gorouter.service
```

Confirm `SystemCallFilter=@system-service` and `RestrictNamespaces=yes` do not prevent Python, SQLite WAL, DNS, or network operation.

If a directive must be relaxed, document the exact runtime failure that justified it.

## Section 8 acceptance criteria

- The unit exposes only supported operations.
- Configuration changes are documented as restart-required.

---

# 9. Strengthen persisted error-detail privacy

## Goal

Close common gaps in pattern-based redaction or move to a safer default.

## Files

```text
src/go_aggregator/security/redaction.py
src/go_aggregator/request/finalizer.py
src/go_aggregator/request/attempt_finalizer.py
tests/unit/test_redaction.py
tests/integration/test_error_detail_privacy.py
```

## Preferred policy

Persist no arbitrary provider error detail by default.

Add configuration:

```python
persist_redacted_error_detail: bool = False
```

When false:

```text
error_detail = None
```

Persist only:

```text
error_class
status_code
upstream_request_id
safe event type
```

When explicitly enabled, apply the strengthened redactor below.

## Task 9.1: Add JSON key/value redaction

Cover case-insensitive JSON keys:

```text
authorization
api_key
apikey
password
secret
token
access_token
refresh_token
prompt
completion
input
messages
```

For user-content-bearing keys such as `input` and `messages`, replace the entire value when encountered in error detail.

Support quoted and unquoted scalar forms where practical.

## Task 9.2: Add structured sanitization where possible

If an error body parses as JSON, prefer recursive object sanitization over regex:

```python
def sanitize_error_object(value: Any) -> Any:
```

Rules:

- redact sensitive-key values recursively;
- retain safe keys such as `type`, `code`, and bounded `message` only after string redaction;
- limit depth, item count, and total serialized size;
- never preserve arbitrary prompt/message arrays.

Use regex only as fallback for non-JSON text.

## Task 9.3: Expand privacy tests

Required cases:

```json
{"api_key": "secret"}
{"password": "secret"}
{"authorization": "Bearer secret"}
{"token": "secret"}
{"messages": [{"role": "user", "content": "private"}]}
{"input": "private prompt"}
```

Also test nested objects, arrays, mixed case, URL query strings, bearer tokens, and `sk-` keys.

After request and attempt finalization, scan every text column in the database and captured logs for all secret markers.

## Section 9 acceptance criteria

- Default persistence is fail-closed or redaction covers common structured forms.
- Privacy tests exercise actual database persistence.

---

# 10. Tighten operational scripts

## Goal

Make diagnostics safe and accurately described.

## Files

```text
scripts/check_database.py
scripts/smoke_test.py
README.md
```

## Task 10.1: Open the invariant checker read-only

Add read-only database support to `Database` or use a dedicated URI connection:

```text
file:/path/to/db?mode=ro
```

The invariant checker must not:

- change journal mode;
- create WAL files;
- apply migrations;
- write health-probe rows;
- mutate pragmas beyond safe read-only settings.

If `Database` gains `read_only=True`, ensure write helpers and transactions fail immediately.

## Task 10.2: Handle schema-version mismatch

The checker should first inspect `_migrations` and report a clear error when the database schema is older or newer than the checker expects.

Do not crash with a raw `no such column` exception.

## Task 10.3: Add exit-code documentation

Document:

```text
0 = all invariants pass
1 = invariant violation
2 = configuration/database access error
```

## Task 10.4: Remove ambiguous smoke defaults

Require explicit model IDs or dynamically validate that configured IDs appear in `/v1/models` before attempting live calls.

## Section 10 acceptance criteria

- The database checker is genuinely read-only.
- Operational failures are clear and actionable.

---

# 11. Final Phase 17 regression matrix

Create:

```text
tests/integration/test_phase17_release_validation.py
```

The following scenarios are mandatory.

## A. Fresh startup transaction safety

- File-backed fresh database.
- Two configured accounts.
- Migrations, account sync, and crash recovery complete.
- Restart also succeeds.

## B. Write-helper contract

- Writes outside transactions fail.
- Writes inside owned transactions persist.
- Child tasks cannot inherit ownership.

## C. Credential boundary

- Local bearer, local `x-api-key`, and proxy authorization never reach upstream.
- Selected upstream account credential does reach upstream.
- Test both protocol families.

## D. CI dependency smoke

- `pytest --help` includes `--cov` in the locked dev environment.

## E. Migration compatibility

- Fresh database and simulated upgraded database behave equivalently.
- Migration checksum test passes.

## F. Raw cursor restriction

- Outside transaction fails.
- Inside owner transaction succeeds.
- Checkpoint helper remains concurrency-safe.

## G. Real 402 lifecycle

- A returns 402.
- B succeeds.
- A excluded on next request.
- A recovers after cooldown.

## H. Streaming smoke unit behavior

- First nonempty chunk is observed before full completion.
- Stream response is closed on success and early cancellation.

## I. Privacy

- Structured JSON secret fields are absent from database and logs.
- Default configuration stores no arbitrary error detail if fail-closed mode is selected.

## J. Operational checker

- Read-only checker leaves database files and schema unchanged.
- Correct exit codes are returned.

---

# 12. Recommended implementation order

Follow this order exactly:

1. Explicit database write ownership.
2. Upstream credential-boundary fix.
3. CI dependency correction.
4. Migration immutability restoration.
5. Raw cursor enforcement.
6. Coordinator-level 402 test.
7. Real streaming smoke test.
8. Systemd lifecycle correction.
9. Privacy hardening.
10. Read-only operational checker.
11. Full Phase 17 regression matrix.
12. Raspberry Pi deployment smoke and soak.

Do not begin live deployment testing before Sections 1–5 are complete and CI is green.

---

# 13. Suggested commit sequence

```text
fix: require explicit transactions for database writes
security: strip local credentials before upstream forwarding
ci: add pytest-cov to frozen development dependencies
fix: restore immutable price migration history
refactor: enforce transaction-owned raw cursor access
 test: add coordinator-level quota exhaustion lifecycle
ops: validate incremental streaming in smoke tests
ops: remove unsupported systemd reload action
security: make persisted error details fail-closed
ops: make database invariant checker read-only
test: add phase 17 deployment readiness matrix
```

Remove the accidental leading space from the `test:` commit subject when implementing.

---

# 14. Release-readiness checklist

## Database

- [ ] Every DML write requires an owned transaction.
- [ ] Fresh startup succeeds with configured accounts.
- [ ] Restart succeeds without nested-transaction errors.
- [ ] Raw cursors are transaction-owner-only.
- [ ] Migration checksums pass.
- [ ] Fresh and upgraded schemas behave equivalently.

## Credentials and privacy

- [ ] Local bearer credentials never reach upstream.
- [ ] Local `x-api-key` never reaches upstream.
- [ ] Upstream receives only the selected account credential.
- [ ] Error detail is disabled by default or structurally sanitized.
- [ ] Database and log secret scans pass.

## CI

- [ ] `pytest-cov` is locked.
- [ ] Ruff format passes.
- [ ] Ruff lint passes.
- [ ] Pyright passes.
- [ ] Full tests with coverage pass.
- [ ] GitHub displays green checks on the head commit.

## Runtime behavior

- [ ] Real 402 failover/cooldown/recovery passes.
- [ ] Streaming smoke test observes incremental chunks.
- [ ] Stream cancellation leaves no leaked reservation or attempt.
- [ ] systemd restart works.
- [ ] No unsupported reload action is advertised.

## Operations

- [ ] Database checker is read-only.
- [ ] Invariant checker passes after restart.
- [ ] Explicit current model IDs are used in smoke testing.
- [ ] Raspberry Pi smoke matrix passes.
- [ ] Soak test completes without leaks, schema drift, or credential exposure.

---

# Definition of done

Phase 17 is complete only when:

1. The application can start from a fresh file-backed database with configured accounts.
2. No implicit SQLite transaction remains open between lifecycle stages.
3. Local proxy authentication material is proven absent from upstream requests.
4. CI runs successfully from the frozen lockfile and displays green checks.
5. Historical migrations are immutable and compatibility-tested.
6. Raw cursor access cannot escape transaction ownership.
7. Real coordinator-level 402 behavior is regression-tested.
8. The smoke test verifies true incremental streaming.
9. Operational service instructions match supported application behavior.
10. Persisted diagnostic detail is fail-closed or robustly sanitized.
11. The database checker is read-only and passes after restart.
12. The Raspberry Pi deployment smoke test and soak test complete without invariant violations.
13. GoRouter can reasonably be labeled deployment-ready for personal LAN beta use.
