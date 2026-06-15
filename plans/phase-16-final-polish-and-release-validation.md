# Phase 16: Final Polish and Release Validation

## Purpose

Phase 15 closed the major concurrency and accounting defects. This final phase is not a redesign. It is a focused release-polish pass intended to make GoRouter safe for personal beta deployment on a Raspberry Pi and straightforward to validate after future changes.

The implementing model should treat this as a test-first correctness pass. Complete the remaining behavioral fixes, add regression coverage, verify CI, then perform a real deployment smoke test.

---

## Implementation rules

1. Follow sections in order.
2. Do not add broad new features.
3. Add or update tests in the same commit as each fix.
4. Run focused tests after each section.
5. Run the complete test suite before starting the next section.
6. Do not weaken privacy checks to make tests pass.
7. Do not persist raw upstream bodies, request bodies, prompts, completions, tool arguments, API keys, authorization headers, or unredacted exception detail.
8. Preserve the current single-process SQLite architecture.
9. Prefer explicit helper methods over comments that require callers to follow fragile cursor-lifetime rules.
10. Do not declare release readiness until the Raspberry Pi smoke matrix passes.

---

## Phase exit outcomes

Phase 16 is complete when:

1. Real upstream 402 responses enter quota-exhausted cooldown and recover correctly.
2. Request-level cost and health feedback are applied even when the attempt reservation was already released.
3. Persisted error detail cannot contain credentials, authorization values, prompts, completions, passwords, or secret assignments.
4. Database and log privacy tests exercise the same secret-bearing failure input.
5. Raw cursor-returning database APIs are removed or restricted to transaction-owned use.
6. Cache-only and partial price overrides retain correct provenance and fill missing categories safely.
7. Runtime cooldown duration matches configured health-manager cooldown behavior.
8. Cache-only sub-microdollar costs are not mislabeled as exact derived zero.
9. CI reports formatting, lint, type-check, and test results on the head commit.
10. A clean Raspberry Pi deployment can start, route, fail over, recover, expose statistics, and restart without accounting drift.

---

# 1. Fix real 402 classification and cooldown behavior

## Goal

Ensure a real coordinator-level HTTP 402 response is classified as quota exhaustion and affects later independent requests.

## Files

```text
src/go_aggregator/health/health_manager.py
src/go_aggregator/request/coordinator.py
tests/unit/test_health_categories.py
tests/integration/test_quota_cooldown.py
tests/integration/test_phase16_release_validation.py
```

## Task 1.1: Expand normalized classification

Update `classify_failure_category()` so all of the following map to `FailureCategory.QUOTA_EXHAUSTED`:

```text
error_class == "quota_exhausted"
error_class contains "quotaexhausted"
error_class contains "quota_exhausted"
status_code == 402
```

Suggested implementation:

```python
if (
    status_code == 402
    or "quotaexhausted" in ec
    or "quota_exhausted" in ec
):
    return FailureCategory.QUOTA_EXHAUSTED
```

This check should occur before the generic unknown fallback.

## Task 1.2: Add real coordinator integration test

Required sequence:

1. Account A returns HTTP 402.
2. Account B returns HTTP 200.
3. First request succeeds through B after failover.
4. Submit a new independent request immediately.
5. Assert A is excluded by health cooldown.
6. Advance the test clock beyond configured cooldown.
7. Submit another independent request.
8. Assert A is eligible again.

Do not test only `HealthManager.record_quota_exhausted()` directly. The test must pass through response classification and coordinator health transition.

## Task 1.3: Verify runtime-state consistency

After the 402:

```text
HealthManager.health_state == quota_exhausted
AccountRuntimeState.health_state == quota_exhausted
```

After cooldown:

```text
both report healthy/eligible
```

## Section 1 acceptance criteria

- A real 402 changes routing behavior across requests.
- Cooldown recovery is verified through the router, not only through unit methods.

---

# 2. Separate request-level feedback from reservation cleanup

## Goal

Apply final cost, usage-window updates, health transitions, and runtime state whenever the request row transitions, even if its reservation was already released by `AttemptFinalizer` or another valid path.

## Files

```text
src/go_aggregator/request/finalizer.py
tests/integration/test_exhaustion_lifecycle.py
tests/integration/test_live_routing_feedback.py
tests/integration/test_reservation_cleanup_idempotency.py
```

## Task 2.1: Refactor post-commit gates

Replace the current broad gate:

```python
if transitioned and reservation_released:
    ...all side effects...
```

with two independent gates.

Required structure:

```python
if transitioned:
    if reservation_released:
        remove_reservation(...)
        decrement_active_request_count(...)

    if cost_microdollars > 0:
        record_usage(...)
        increment persisted 5h/7d/30d snapshot

    apply health transition when appropriate
    update runtime state
```

Only aggregate reservation cleanup depends on `reservation_released`.

## Task 2.2: Preserve health idempotency

Continue honoring:

```python
health_already_applied
```

Request finalization after an already finalized failed attempt must not increment health twice.

However, request-level success and terminal state should still update runtime state when the request transitions.

## Task 2.3: Add exhausted-attempt regression

Test:

1. Final attempt is attempt-finalized and reservation released.
2. Overall request is then finalized as exhausted.
3. Assert reservation aggregate does not decrement again.
4. Assert final estimated cost is immediately added to live quota state.
5. Assert the next routing decision observes that cost without waiting 60 seconds.

## Task 2.4: Add success-after-external-release regression

Simulate a legitimate prior reservation transition, then finalize a completed request.

Assert:

- no double reservation cleanup;
- success health resets correctly;
- final cost and usage snapshot update immediately.

## Section 2 acceptance criteria

- Reservation transition controls only reservation cleanup.
- Request transition controls final cost and terminal request effects.

---

# 3. Redact or eliminate persisted error detail

## Goal

Guarantee that arbitrary exception or provider error text cannot persist secrets or user content.

## Files

```text
src/go_aggregator/security/redaction.py
src/go_aggregator/request/finalizer.py
src/go_aggregator/request/attempt_finalizer.py
src/go_aggregator/db/repositories.py
tests/unit/test_redaction.py
tests/integration/test_phase15_end_to_end.py
tests/integration/test_phase16_release_validation.py
```

## Required policy

Persist these fields freely:

```text
error_class
status_code
upstream_request_id
safe diagnostic code
```

Do not persist arbitrary raw error strings.

Choose one of these implementations. Prefer Option A.

### Option A: safe redaction helper

Create:

```python
def redact_error_detail(value: str | None) -> str | None:
```

Required redactions:

```text
Authorization header values
Bearer tokens
API keys beginning with sk-
password=...
secret=...
api_key=...
JSON prompt fields
JSON completion fields
URL userinfo
sensitive query parameters such as key, token, api_key, access_token
```

Replace matched values with stable markers such as:

```text
[REDACTED]
```

Then truncate to the existing maximum length.

### Option B: no persisted detail

Set persisted `error_detail` to `None` by default and rely on error class/status/request ID.

This is safer and acceptable if diagnostic detail is not essential.

## Task 3.1: Apply one policy everywhere

Apply the same helper before persistence in:

- `RequestFinalizer`
- `AttemptFinalizer`
- any direct repository attempt/request update path
- account-event details if they ever include provider text

## Task 3.2: Strengthen privacy test

Use one secret-bearing `error_detail` containing:

```text
sk-FAKE_API_KEY
Authorization: Bearer test-token
"prompt": "private prompt"
"completion": "private completion"
password=secret123
api_key=abc123
https://user:pass@example.test/path?token=secret
```

After finalization:

1. Scan all database text columns.
2. Inspect request and attempt error fields directly.
3. Scan captured application logs.
4. Assert no original secret marker appears.
5. Assert redaction marker appears when Option A is used.

Do not exclude the rows receiving the secret-bearing detail.

## Task 3.3: Avoid aiosqlite SQL-parameter logging in production

Confirm application logging does not set `aiosqlite` to DEBUG in normal operation.

Document that debug-level dependency logging can expose SQL parameters and should not be enabled on systems processing sensitive metadata.

## Section 3 acceptance criteria

- Secret-bearing error detail is safe in both database and logs.
- The privacy test validates the actual persistence path.

---

# 4. Remove unsafe raw cursor lifetime from the database API

## Goal

Prevent callers from asynchronously consuming a cursor after the connection lock has been released.

## Files

```text
src/go_aggregator/db/connection.py
src/go_aggregator/db/repositories.py
src/go_aggregator/background/cleanup.py
src/go_aggregator/db/migrations.py
tests/unit/test_database.py
tests/integration/test_connection_operation_serialization.py
```

## Task 4.1: Add explicit helpers

Add:

```python
async def execute_write(
    self,
    sql: str,
    params: Sequence[Any] = (),
) -> int:
    """Execute while locked and return rowcount."""
```

```python
async def execute_insert(
    self,
    sql: str,
    params: Sequence[Any] = (),
) -> int:
    """Execute while locked and return lastrowid."""
```

```python
async def execute_returning(
    self,
    sql: str,
    params: Sequence[Any] = (),
) -> list[aiosqlite.Row]:
    """Execute and fetch all returned rows while locked."""
```

These helpers should become no-ops with respect to lock acquisition when the current task owns a transaction.

## Task 4.2: Migrate callers

Replace raw cursor use:

```text
cursor.rowcount
cursor.lastrowid
await cursor.fetchall()
```

with the explicit helpers.

Priority files:

```text
db/repositories.py
request/attempt_finalizer.py
background/cleanup.py
db/migrations.py
```

## Task 4.3: Restrict or remove `execute()`

Preferred:

- Rename raw `execute()` to `_execute_cursor()`.
- Document it as transaction-owner-only.
- Use it only internally when absolutely necessary.

Acceptable:

- Keep public `execute()` temporarily but raise when called outside an owned transaction.

Do not retain the current API contract that relies on callers consuming the cursor “before yielding.”

## Task 4.4: Make migrations transaction-owned

Run each migration file inside `Database.transaction()`.

Do not directly call:

```python
db.connection.commit()
db.connection.rollback()
```

outside the database abstraction.

## Required tests

- `execute_returning()` remains serialized under concurrent transactions.
- `execute_insert()` returns correct lastrowid.
- `execute_write()` returns correct rowcount.
- No raw cursor escapes a non-transaction connection lock.
- Migration rollback leaves no partial schema/version state.

## Section 4 acceptance criteria

- Cursor lifetime cannot escape connection ownership.
- Migration commits use the same transaction abstraction as runtime writes.

---

# 5. Correct price override provenance and partial filling

## Goal

Make each pricing category independently resolvable and preserve the true source of configured values.

## Files

```text
src/go_aggregator/catalog/service.py
src/go_aggregator/models/config.py
tests/integration/test_price_snapshot_writes.py
tests/unit/test_pricing.py
```

## Task 5.1: Detect any configured pricing override

Replace input/output-only override detection with:

```python
has_any_override_pricing = override and any(
    value is not None
    for value in (
        override.input_price_per_1k,
        override.output_price_per_1k,
        override.cache_read_per_million_microdollars,
        override.cache_write_per_million_microdollars,
    )
)
```

If true, source must be `config`.

## Task 5.2: Fill each category independently

Use independent fallback resolution:

```python
if input_price is None:
    load input metadata
if output_price is None:
    load output metadata
if cache_read_price is None:
    load cache-read metadata
if cache_write_price is None:
    load cache-write metadata
```

Do not require both values in a pair to be absent.

## Task 5.3: Define mixed provenance

A single snapshot currently has one source field. When configuration overrides one category and metadata fills another, use source:

```text
mixed
```

Allowed sources should be:

```text
config
upstream
mixed
```

Do not incorrectly label mixed or cache-only configuration as upstream.

## Required tests

1. Cache-only config override creates source `config`.
2. Input config override plus upstream output creates source `mixed`.
3. Cache-read config plus upstream cache-write creates source `mixed`.
4. Missing categories remain null rather than copied from unrelated rates.
5. Unchanged mixed snapshot is not duplicated.

## Section 5 acceptance criteria

- Every token category resolves independently.
- Snapshot source accurately represents provenance.

---

# 6. Unify configured cooldown duration

## Goal

Remove the hard-coded runtime quota cooldown and keep both health representations synchronized.

## Files

```text
src/go_aggregator/accounts/state.py
src/go_aggregator/accounts/registry.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/health/health_manager.py
tests/integration/test_health_state_consistency.py
```

## Preferred design

Make `HealthManager` the authoritative source for transient health and cooldown timing.

`AccountRuntimeState` should retain:

```text
enabled
weight
active request count
model availability
```

but should not independently invent cooldown durations.

## Minimum acceptable change

Change:

```python
record_failure(error_class: str)
```

to:

```python
record_failure(
    error_class: str,
    *,
    cooldown_seconds: float | None = None,
) -> None
```

For quota exhaustion, require the coordinator to pass the same configured cooldown used by `HealthManager`.

For rate limits, pass parsed `Retry-After`.

## Required tests

- Configured quota cooldown of a non-default duration produces identical expiry in both health systems.
- Parsed rate-limit cooldown produces identical eligibility behavior.
- Authentication failures remain permanent until explicitly reset.

## Section 6 acceptance criteria

- Runtime and health-manager cooldowns cannot diverge due to hard-coded values.

---

# 7. Correct cache-only zero-cost exactness

## Goal

Avoid labeling a nonzero cache-only usage event as exact derived zero when integer microdollar rounding truncates it.

## Files

```text
src/go_aggregator/catalog/pricing.py
tests/unit/test_pricing.py
```

## Task 7.1: Expand zero-cost check

Replace:

```python
if cost_microdollars == 0 and (input_tokens > 0 or output_tokens > 0):
```

with:

```python
if cost_microdollars == 0 and any(
    (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
    )
):
    exactness = "estimated"
```

The request finalizer will conservatively floor estimated cost at the reservation estimate.

## Required tests

- One cache-read token with a low nonzero rate does not remain `derived` zero.
- One cache-write token with a low nonzero rate does not remain `derived` zero.
- Larger cache-only usage with representable cost remains `derived`.

## Section 7 acceptance criteria

- Positive billable usage cannot be reported as exact zero solely due to integer rounding.

---

# 8. CI and release verification

## Goal

Make repository status independently verifiable rather than relying only on local commit messages.

## Files

```text
.github/workflows/ci.yml
pyproject.toml
README.md
```

## Task 8.1: Confirm CI triggers

CI must run on:

```yaml
on:
  push:
    branches: [main]
  pull_request:
```

The GitHub connector currently shows no status checks on recent direct commits. Confirm the workflow is present, enabled, and triggered by pushes to the default branch.

## Task 8.2: Required jobs

CI must run:

```text
uv sync --frozen
ruff format --check
ruff check
pyright
pytest
```

Use Python 3.12.

## Task 8.3: Remove “pre-existing” type-check allowance

The release gate should not state that Pyright failures are acceptable because they are pre-existing.

Choose one:

- Fix the remaining type errors.
- Add narrowly scoped, documented suppressions.

CI must exit successfully.

## Task 8.4: Add deterministic test environment

Set:

```text
PYTHONHASHSEED=0
TZ=UTC
```

Avoid tests dependent on wall-clock sleeps where practical. Use monkeypatched time for cooldown and expiry tests.

## Task 8.5: Publish coverage artifact

Generate coverage for the release-validation run. A strict percentage gate is optional, but the artifact should make untested lifecycle branches visible.

Minimum command:

```text
pytest --cov=go_aggregator --cov-report=term-missing --cov-report=xml
```

## Section 8 acceptance criteria

- Head commit shows successful CI status checks.
- Type checking is genuinely green.
- Release tests are deterministic.

---

# 9. Raspberry Pi deployment smoke test

## Goal

Validate the intended real deployment environment: Ubuntu on Raspberry Pi, LAN-only service, SQLite persistence, and multiple OpenCode Go subscriptions.

## Files

```text
scripts/smoke_test.py
scripts/check_database.py
ops/gorouter.service
ops/gorouter.env.example
README.md
```

## Task 9.1: Add non-secret smoke-test script

Create a script that accepts:

```text
base URL
proxy API key from environment
one OpenAI-protocol model ID
one Anthropic-protocol model ID
```

It should test:

1. `/v1/healthz`
2. `/v1/readyz`
3. `/v1/models`
4. One non-streaming OpenAI-family request
5. One streaming OpenAI-family request
6. One non-streaming Anthropic-family request
7. One streaming Anthropic-family request
8. Statistics endpoint availability
9. Proxy request IDs and attempt-count headers

Do not print request content or secrets.

## Task 9.2: Add database invariant checker

Create a read-only diagnostic script that verifies:

```text
no pending requests older than threshold
no incomplete attempts for terminal requests
no active reservations for terminal requests
no negative token/cost values
no duplicate proxy_request_id
all resolved models have valid protocol
all price snapshots have recognized source
```

Exit nonzero on invariant violation.

## Task 9.3: Add systemd unit example

Provide a hardened but practical unit:

```text
Restart=on-failure
RestartSec=5
WorkingDirectory=...
EnvironmentFile=...
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=<database directory>
```

Do not embed keys in the unit file.

## Task 9.4: Real deployment matrix

On the Pi:

1. Start with a fresh database.
2. Confirm all migrations apply.
3. Confirm catalog refresh discovers current models.
4. Run smoke script.
5. Send at least 20 mixed requests.
6. Confirm subscription distribution changes with usage.
7. Force one invalid/disabled key and verify failover.
8. Exercise one rate limit or mocked failure if real 429 cannot be induced safely.
9. Restart service during or after traffic.
10. Run database invariant checker.
11. Confirm dashboard/statistics persist across restart.
12. Confirm LAN clients can connect while non-LAN exposure remains blocked by network configuration.

## Task 9.5: 24-hour soak

Run low-rate mixed traffic for 24 hours.

Observe:

```text
memory usage
SQLite/WAL size
active reservations
pending requests
HTTPX connection count
background task health
catalog refresh behavior
usage-window drift
```

After soak:

- restart service;
- run invariant checker;
- compare dashboard totals to direct SQL aggregates;
- confirm no secret markers in logs or database.

## Section 9 acceptance criteria

- Fresh install, restart, failover, streaming, stats, and persistence work on the target Pi.
- No invariant violations appear after the soak test.

---

# 10. Final regression matrix

Create or expand:

```text
tests/integration/test_phase16_release_validation.py
```

The following tests are mandatory.

## A. Real 402 lifecycle

- A returns 402.
- B succeeds.
- New request excludes A.
- A recovers after configured cooldown.

## B. Already-released reservation

- Attempt finalizer releases reservation.
- Request finalizer transitions request.
- Cost updates immediately.
- No second aggregate decrement occurs.

## C. Secret-bearing error detail

- Same secret-bearing input is passed through finalization.
- Database scan and log scan both pass.

## D. Cursor ownership

- No raw cursor is consumed after a non-transaction lock is released.
- `execute_returning()` remains safe under concurrency.

## E. Partial price overrides

- Config input plus upstream output yields complete mixed snapshot.
- Cache-only config is labeled config.

## F. Cooldown parity

- Both health representations use the configured duration.

## G. Cache rounding

- Tiny cache-only usage is not exact zero.

## H. Restart invariants

- Simulated restart leaves no leaked attempts/reservations.
- Usage totals and protocol metadata reload correctly.

## I. Privacy

Search all database text fields and captured logs for:

```text
API key marker
Authorization marker
prompt marker
completion marker
password marker
secret marker
```

None may remain unredacted.

---

# 11. Recommended implementation order

Follow this order exactly:

1. Real 402 classification.
2. Request-level feedback gate refactor.
3. Error-detail redaction/elimination.
4. Database cursor API cleanup.
5. Price provenance and partial fallback.
6. Cooldown-duration unification.
7. Cache-only zero exactness.
8. CI verification and type-check cleanup.
9. Phase 16 regression matrix.
10. Raspberry Pi smoke test.
11. 24-hour soak and release decision.

Do not begin deployment testing before Sections 1–8 pass locally and in CI.

---

# 12. Suggested commit sequence

```text
fix: classify real quota exhaustion responses
fix: separate request feedback from reservation cleanup
security: redact persisted error details
refactor: replace raw cursor database operations
fix: preserve mixed pricing provenance
fix: unify configured cooldown timing
fix: avoid exact zero for cache-only usage
ci: enforce complete release checks
test: add phase 16 release regression matrix
ops: add raspberry pi smoke and invariant checks
docs: document beta deployment procedure
```

---

# 13. Release-readiness checklist

## Code correctness

- [ ] Real 402 cooldown works end to end.
- [ ] Final cost feedback is independent of reservation cleanup.
- [ ] No double reservation or active-count decrement.
- [ ] Cache-only and partial pricing behave correctly.
- [ ] Health state is consistent across routing and dashboard state.

## Privacy

- [ ] Secret-bearing error detail is redacted or not stored.
- [ ] Database privacy scan passes.
- [ ] Log privacy scan passes.
- [ ] API keys are loaded only from environment variables.

## Database

- [ ] All operations are connection-serialized.
- [ ] No unsafe raw cursor escapes lock ownership.
- [ ] Migrations are atomic.
- [ ] Invariant checker passes after restart and soak.

## CI

- [ ] Formatting passes.
- [ ] Lint passes.
- [ ] Pyright passes.
- [ ] Full pytest suite passes.
- [ ] Coverage artifact is produced.
- [ ] GitHub shows successful status checks on the release commit.

## Deployment

- [ ] Fresh Raspberry Pi install succeeds.
- [ ] systemd restart behavior works.
- [ ] OpenAI and Anthropic protocol families both work.
- [ ] Streaming and non-streaming requests work.
- [ ] Failover works with one bad subscription.
- [ ] Statistics persist across restart.
- [ ] 24-hour soak completes without leaks or invariant violations.

---

# Definition of done

Phase 16 is complete only when:

1. The remaining three correctness/privacy blockers are fixed.
2. All focused regression tests pass.
3. The complete local suite passes.
4. GitHub CI is visibly green on the head commit.
5. The Raspberry Pi smoke matrix passes on a fresh database.
6. The 24-hour soak completes without leaked reservations, incomplete attempts, memory growth, database drift, or secret persistence.
7. The database invariant checker exits successfully after restart.
8. README deployment instructions reproduce the working setup without embedding credentials.
9. The repository can reasonably be labeled a personal beta release.
