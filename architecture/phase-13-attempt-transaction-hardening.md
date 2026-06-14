# Phase 13: Attempt Lifecycle and Transaction Hardening

## Purpose

Phase 12 established the intended architecture: explicit quota policies, durable pre-dispatch selection, request finalization, account-aware retry, incremental SSE observation, raw response forwarding, protocol resolution, and CI.

The remaining defects are concentrated in failure-path correctness and concurrent database ownership. This phase must make each upstream attempt independently terminal, make SQLite transaction ownership safe across concurrent asyncio tasks, and close the remaining accounting and protocol gaps.

This plan is intentionally procedural. A smaller coding model should be able to execute it in order without inventing new architecture.

---

## Phase exit outcomes

Phase 13 is complete when:

1. Every upstream attempt reaches a terminal attempt state before another attempt begins.
2. Every attempt reservation is released exactly once.
3. Successful failover leaves zero active reservations and zero incomplete attempts.
4. SQLite transactions cannot be shared accidentally across concurrent tasks.
5. Readiness checks cannot roll back another request's transaction.
6. Incoming projected request cost affects account selection.
7. Cancellation is finalized correctly at every point after durable selection.
8. Successful requests without terminal usage consume the reservation estimate.
9. New price snapshots populate integer microdollar fields correctly.
10. Missing required price categories cannot be labeled `derived`.
11. Estimated final cost affects live routing immediately.
12. Unknown model protocols fail closed rather than defaulting to OpenAI.
13. Wrong local protocol endpoints are rejected before durable selection.
14. Model-specific 404 responses can fail over to another account.
15. Finalizer idempotency includes attempt telemetry and reservation state.
16. CI includes tests for all of these invariants.

---

# 1. Add a per-attempt terminal lifecycle

## Goal

Separate attempt finalization from overall request finalization.

A failed attempt that will be retried must be terminal in SQLite and must release its own reservation before the next account is selected. It must not finalize the overall request.

## Files

```text
src/go_aggregator/request/attempt_finalizer.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/db/repositories.py
src/go_aggregator/request/finalizer.py
tests/integration/test_attempt_lifecycle.py
tests/integration/test_failover_matrix.py
```

## Task 1.1: Add `AttemptFinalizationData`

Create:

```python
@dataclass(frozen=True)
class AttemptFinalizationData:
    status_code: int | None = None
    error_class: str | None = None
    error_detail: str | None = None
    upstream_request_id: str | None = None
    bytes_emitted: int = 0
    release_reason: str = "attempt_failed"
```

## Task 1.2: Add `AttemptFinalizer`

Create a new class with this interface:

```python
class AttemptFinalizer:
    async def finalize_failed_attempt(
        self,
        selected: SelectedAttempt,
        data: AttemptFinalizationData,
    ) -> bool:
        ...
```

Required behavior inside one database transaction:

1. Mark the selected attempt completed only if `completed_at IS NULL`.
2. Persist status code, bounded error class/detail, upstream request ID, and emitted bytes.
3. Release only `selected.reservation_id` if it is still active.
4. Do not finalize the request row.
5. Return `True` only when this call performed the attempt transition.

After commit, only when `True`:

1. Remove exactly `selected.estimated_microdollars` from the in-memory reservation cache.
2. Decrement active request count exactly once.
3. Apply the health transition for this failed attempt.

## Task 1.3: Add repository methods

Add:

```python
async def finalize_if_incomplete(
    attempt_id: int,
    ...,
) -> bool
```

SQL must include:

```sql
WHERE id = ? AND completed_at IS NULL
```

Change `ReservationRepository.release()` to return whether one row changed.

## Task 1.4: Use attempt finalizer before retry

In `RequestCoordinator.execute()`:

```text
select attempt
execute upstream
on retryable pre-body failure:
    finalize failed attempt
    preserve upstream response if available
    add account to attempted set
    continue
```

Do not call `_cleanup_in_memory()` directly.

Delete `_cleanup_in_memory()` after all paths use either `AttemptFinalizer` or `RequestFinalizer`.

## Task 1.5: Keep request finalization separate

`RequestFinalizer` remains responsible for:

- Successful terminal response.
- Non-retryable client error.
- Exhausted request.
- Midstream error.
- Cancellation.

It must finalize only the final selected attempt and overall request.

Previously failed attempts must already be terminal before request finalization begins.

## Required tests

For a two-attempt success:

```text
attempt 1 -> 429
attempt 2 -> 200
```

Assert:

- Two attempt rows exist.
- Both attempts have `completed_at`.
- Attempt 1 has 429 and an error class.
- Attempt 2 has 200.
- Both reservations are released.
- No active reservations remain.
- Active request count for both accounts is zero.
- In-memory reserved cost for both accounts is zero.

Repeat for:

- 401 then 200.
- 402 then 200.
- Connect error then 200.
- 500 then 200.

## Section 1 acceptance criteria

- Every retryable failed attempt is terminal before the next selection.
- Successful failover leaves no attempt or reservation leaks.

---

# 2. Make SQLite transaction ownership task-safe

## Goal

Prevent concurrent asyncio tasks from sharing one logical transaction on the shared `aiosqlite.Connection`.

## Files

```text
src/go_aggregator/db/connection.py
src/go_aggregator/app.py
tests/integration/test_database_transactions.py
tests/integration/test_transaction_concurrency.py
```

## Task 2.1: Add a transaction lock

In `Database.__init__` add:

```python
self._transaction_lock = asyncio.Lock()
```

## Task 2.2: Add task-local ownership

Use `contextvars.ContextVar`:

```python
self._transaction_depth: ContextVar[int] = ContextVar(
    "database_transaction_depth",
    default=0,
)
```

Do not use only `connection.in_transaction` for nesting detection.

## Task 2.3: Replace `transaction()`

Required behavior:

```python
@asynccontextmanager
async def transaction(self):
    depth = self._transaction_depth.get()
    if depth > 0:
        token = self._transaction_depth.set(depth + 1)
        try:
            yield
        finally:
            self._transaction_depth.reset(token)
        return

    async with self._transaction_lock:
        token = self._transaction_depth.set(1)
        try:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self.connection.rollback()
                raise
            else:
                await self.connection.commit()
        finally:
            self._transaction_depth.reset(token)
```

Nested transactions are allowed only within the same task/context and inherit the outer commit boundary.

## Task 2.4: Prohibit direct transaction control elsewhere

Search for direct calls to:

```text
connection.commit()
connection.rollback()
BEGIN
COMMIT
ROLLBACK
```

For request lifecycle and readiness code, replace them with `Database.transaction()` or explicit database helper methods.

Repository methods used inside shared transactions must not commit.

Standalone administrative repository methods may own a transaction only if clearly documented.

## Task 2.5: Fix readiness write probe

Do not insert and then call `connection.rollback()` directly.

Use one of these exact patterns.

Preferred:

```python
async with db.transaction():
    await db.execute(
        "INSERT INTO health_probe (probe_at) VALUES (CURRENT_TIMESTAMP)"
    )
    raise _RollbackReadinessProbe()
```

Catch only `_RollbackReadinessProbe` outside and treat it as success.

Alternative: add a dedicated `db.probe_writable()` helper using a savepoint under the transaction lock.

The readiness probe must never roll back unrelated work.

## Required tests

### Concurrent transaction isolation

Task A:

- Starts transaction.
- Inserts row A.
- Waits on event.

Task B:

- Attempts transaction.
- Must block until Task A commits or rolls back.

Assert Task B does not enter its body while Task A owns the transaction.

### Rollback ownership

Task A inserts row A and waits.

Task B performs readiness probe.

Assert Task A's row survives and is not rolled back by the readiness probe.

### Nested same-task transaction

Outer transaction inserts A.

Inner transaction inserts B.

Outer raises.

Assert both A and B roll back.

## Section 2 acceptance criteria

- Concurrent tasks cannot share a transaction implicitly.
- Readiness cannot commit or roll back request lifecycle work.

---

# 3. Supply projected request estimates before selection

## Goal

Make the scorer include the estimated cost of the incoming request for every candidate account.

## Files

```text
src/go_aggregator/request/coordinator.py
src/go_aggregator/routing/router.py
src/go_aggregator/quota/estimation.py
tests/integration/test_projected_selection.py
```

## Task 3.1: Add token estimate helper

Add a deterministic initial token estimator:

```python
def _estimate_request_tokens(context: ProxyRequestContext) -> int:
```

For this phase, use one documented conservative rule. Suggested:

```text
max(1_000, len(original_body) // 3)
```

Cap it at a configured or constant maximum to avoid pathological reservations.

Do not continue using a hard-coded 1,000 tokens without considering request size.

## Task 3.2: Calculate candidate estimates before routing

Inside the selection lock, before calling `router.select_account()`:

1. Determine eligible account names excluding attempted accounts.
2. Calculate `estimated_tokens` once.
3. Build:

```python
request_estimates = {
    account_name: quota_estimator.estimate_cost(
        account_name,
        context.model_id,
        estimated_tokens,
    )
    for account_name in eligible_account_names
}
```

4. Pass the map to `router.select_account()`.
5. Use the selected account's exact map value when creating the reservation.

Do not recalculate after selection.

## Task 3.3: Add router eligibility helper

Add:

```python
def get_eligible_account_names(
    self,
    model_id: str,
    exclude_accounts: set[str] | None = None,
) -> list[str]:
```

Use the same eligibility logic as `select_account()` so estimate generation and selection cannot disagree.

## Required test

Create two accounts with equal current utilization but different model/account EWMA estimates.

Make account A's projected request push it above account B.

Assert B is selected.

Also assert the persisted reservation amount equals the estimate used during scoring.

## Section 3 acceptance criteria

- Incoming projected cost affects selection.
- Reservation amount is exactly the selected projected estimate.

---

# 4. Remove request-state reconstruction during exhaustion

## Goal

Never reconstruct `SelectedAttempt` from arbitrary active reservation rows.

## Files

```text
src/go_aggregator/request/coordinator.py
src/go_aggregator/request/finalizer.py
tests/integration/test_exhaustion_lifecycle.py
```

## Task 4.1: Retain request-local selected state

In `execute()` maintain:

```python
last_selected: SelectedAttempt | None = None
```

Set it after each successful selection.

## Task 4.2: Finalize attempts as they fail

After Section 1, all retryable failed attempts are already terminal and released.

Therefore, on exhaustion:

- If no selected attempt exists, no request was dispatched; generate a proxy error only.
- If the final selected attempt failed and has not been attempt-finalized, finalize it first.
- Finalize the overall request using `last_selected` and its real estimate/reservation identity.

## Task 4.3: Delete arbitrary reservation lookup

Remove logic that queries:

```sql
SELECT id FROM reservations
WHERE request_id = ? AND status = 'active'
LIMIT 1
```

Do not synthesize `SelectedAttempt` with zero estimate values.

## Required tests

For two failed attempts:

- Both attempts terminal.
- Both reservations released.
- Overall request terminal.
- Final estimated cost is nonzero when no usage exists.
- No arbitrary reservation remains.

## Section 4 acceptance criteria

- Exhaustion uses real request-local state.
- No zero-valued synthetic attempt objects remain.

---

# 5. Cover cancellation across the entire post-selection path

## Goal

Any cancellation after durable selection must finalize the selected attempt/request and release the reservation.

## Files

```text
src/go_aggregator/request/coordinator.py
tests/integration/test_cancellation_lifecycle.py
```

## Task 5.1: Add outer cancellation handling

Wrap `_execute_upstream()` so `asyncio.CancelledError` is handled explicitly before generic exceptions.

Required behavior:

```python
except asyncio.CancelledError:
    await self._finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.CLIENT_CANCELLED,
            error_class="CancelledError",
        ),
    )
    raise
```

This covers cancellation:

- During connect.
- During request upload.
- Before upstream headers.
- Between response creation and generator consumption.

## Task 5.2: Avoid double finalization

The streaming generator cancellation path may race with outer cancellation.

Rely on idempotent finalization, but add tests proving:

- Request changes terminal state once.
- Attempt telemetry is not overwritten.
- Reservation is released once.
- Active count decrements once.

## Required tests

1. Cancel during non-streaming connect.
2. Cancel during `send(..., stream=True)` before headers.
3. Cancel during stream consumption.

For all three, assert no active reservation or incomplete attempt remains.

## Section 5 acceptance criteria

- Cancellation is safe at every point after selection.

---

# 6. Correct no-usage and partial-usage accounting

## Goal

Never treat absence of terminal usage as exact zero cost.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/catalog/pricing.py
tests/integration/test_no_usage_accounting.py
```

## Task 6.1: Always use reservation estimate when cost is unknown

Replace the current condition with:

```python
if cost_microdollars == 0 and exactness == "unknown":
    cost_microdollars = selected.estimated_microdollars
    exactness = "estimated"
```

Apply this to successful, failed, cancelled, interrupted, and midstream outcomes.

## Task 6.2: Preserve captured partial usage

If partial usage exists but pricing is incomplete:

- Calculate known categories.
- Estimate missing categories or fall back to at least the reservation estimate.
- Final cost must be:

```text
max(calculated_partial_cost, reservation_estimate)
```

unless upstream explicitly reports direct authoritative cost.

## Task 6.3: Update live snapshots for estimated cost

After finalization, increment persisted in-memory 5h/7d/30d snapshots for all nonzero final costs, including `estimated`.

Do not restrict immediate routing feedback to exact/derived values.

Use exactness only to decide EWMA learning weight.

## Required tests

- Successful stream without usage persists reservation estimate.
- Successful non-streaming response without usage persists reservation estimate.
- Midstream partial usage persists at least reservation estimate.
- Immediate next request sees the estimated cost.

## Section 6 acceptance criteria

- No billable terminal request becomes zero-cost solely because usage was absent.

---

# 7. Make price snapshot writes internally consistent

## Goal

Every newly inserted price snapshot must contain usable integer rates.

## Files

```text
src/go_aggregator/catalog/pricing.py
src/go_aggregator/db/repositories.py
src/go_aggregator/catalog/service.py
tests/unit/test_pricing.py
tests/integration/test_price_snapshot_writes.py
```

## Task 7.1: Canonical integer write API

Change `PriceRepository.record_snapshot()` to accept canonical integer fields:

```python
async def record_snapshot(
    self,
    model_id: str,
    *,
    input_per_million_microdollars: int | None,
    output_per_million_microdollars: int | None,
    cache_read_per_million_microdollars: int | None = None,
    cache_write_per_million_microdollars: int | None = None,
    source: str,
) -> None:
```

Legacy float arguments may remain in a separate compatibility helper, but they must be converted before insertion.

## Task 7.2: Convert legacy float writes

Use:

```python
int(round(dollars_per_1k * 1_000_000_000))
```

for non-null values.

Store both legacy and integer fields only when compatibility is required.

## Task 7.3: Fix `PriceSnapshotRepository.record()`

Ensure it writes:

- Input integer rate.
- Output integer rate.
- Cache rates.
- Source.

Do not create rows with only legacy floats unless integer columns are simultaneously populated.

## Task 7.4: Fix exactness rules

Determine missing required rates per token category:

```python
missing_required_rate = (
    (input_tokens > 0 and input_rate is None)
    or (output_tokens > 0 and output_rate is None)
    or (cache_read_tokens > 0 and cache_read_rate is None)
    or (cache_write_tokens > 0 and cache_write_rate is None)
)
```

If `missing_required_rate`:

- Use fallback estimation for missing categories.
- Return exactness `estimated`.

Only return `derived` when every nonzero token category has a snapshot rate.

## Task 7.5: Insert snapshots during catalog refresh

If upstream model metadata or TOML overrides provide price data:

- Normalize to integer microdollars per million.
- Insert a new snapshot only when values/source differ from latest.

If no source provides prices, retain fallback estimation; do not insert fake derived snapshots.

## Required tests

- Snapshot inserted after migration yields nonzero cost.
- Missing input rate with input tokens returns estimated.
- Missing output rate with output tokens returns estimated.
- Cache rates participate correctly.
- Duplicate unchanged snapshot is not inserted repeatedly.

## Section 7 acceptance criteria

- New snapshots cannot silently produce zero derived cost.

---

# 8. Correct latency and telemetry idempotency

## Goal

Persist real timing and prevent duplicate finalization from overwriting attempt telemetry.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/db/repositories.py
src/go_aggregator/request/coordinator.py
tests/integration/test_request_telemetry.py
```

## Task 8.1: Add latency to `FinalizationData`

Add:

```python
upstream_latency_ms: int | None = None
```

Pass the actual elapsed value from coordinator paths.

Delete:

```python
int((time.time() - time.time()) * 1000)
```

## Task 8.2: Stop duplicate attempt overwrite

In `RequestFinalizer.finalize()`:

1. Call `finalize_if_pending()`.
2. If it returns `False`, exit the transaction without updating attempt or reservation.

Alternative: use independent `AttemptRepository.finalize_if_incomplete()` and release-if-active checks, but ensure duplicate calls cannot alter terminal fields.

## Task 8.3: Preserve first terminal attempt data

Add a test:

1. Finalize attempt with status 200 and bytes 100.
2. Call finalizer again with status 500 and bytes 0.
3. Assert attempt remains 200 and bytes 100.

## Section 8 acceptance criteria

- Stored latency is real.
- Duplicate finalization cannot overwrite attempt telemetry.

---

# 9. Make quota eligibility use the same three-window state as scoring

## Goal

Remove obsolete hourly/daily limit checks from hard eligibility.

## Files

```text
src/go_aggregator/quota/estimation.py
src/go_aggregator/quota/scorer.py
src/go_aggregator/routing/router.py
tests/unit/test_quota.py
tests/integration/test_readiness_quota.py
```

## Task 9.1: Replace `AccountQuota.is_within_limits()`

Implement against:

- Persisted 5h cost.
- Persisted 7d cost.
- Persisted 30d cost.
- Per-window offsets.
- Active reserved cost.
- Three configured capacities.

Do not use old one-hour or one-day windows for eligibility.

## Task 9.2: Decide over-capacity policy explicitly

Recommended behavior:

- Accounts above a quota capacity remain scoreable but receive high utilization.
- They are ineligible only when marked quota-exhausted by upstream health state or when a configuration option requires hard cutoff.

This avoids all accounts becoming unavailable because proxy-observed rolling windows differ from authoritative upstream resets.

If this recommendation is used, remove quota hard cutoff from `is_within_limits()` and rename the method to avoid implying authority.

## Task 9.3: Align readiness

`has_eligible_pairing()` must use the same account eligibility semantics as actual routing.

Readiness must not report unavailable while `select_account()` would route successfully, or report ready when selection cannot succeed.

## Section 9 acceptance criteria

- Readiness and routing agree.
- Obsolete hourly/daily windows do not control eligibility.

---

# 10. Fail closed for unknown model protocols

## Goal

Do not silently classify unresolved models as OpenAI-compatible.

## Files

```text
src/go_aggregator/catalog/normalizer.py
src/go_aggregator/catalog/protocols.py
src/go_aggregator/catalog/service.py
src/go_aggregator/catalog/cache.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/api/chat_completions.py
src/go_aggregator/api/messages.py
tests/unit/test_protocol_resolution.py
tests/integration/test_catalog_mixed_protocol.py
```

## Task 10.1: Remove protocol default from normalizer/cache

Do not assign `"openai"` merely because protocol is absent.

Use:

```text
protocol = None
```

or omit the key until resolved.

## Task 10.2: Resolve in one place

In `CatalogService._fetch_and_process_account()`:

1. Read raw per-model metadata.
2. Load persisted protocol for the same model when available.
3. Call resolver with exact priority:

```text
config override
upstream metadata
exact mapping
family mapping
persisted protocol
unresolved
```

4. If unresolved:

- Do not expose or route the model.
- Log model ID and metadata keys only.
- Persist unresolved status only if schema supports it safely.

## Task 10.3: Add actual Go model families

Add maintained mappings for the current OpenCode Go families used by this project, such as the deployed GLM, Kimi, MiMo, DeepSeek, MiniMax, and Qwen identifiers.

Keep the mapping in one module and test exact representative IDs.

## Task 10.4: Preserve protocol source

Pass the actual resolver source into cache and persistence.

Do not recompute source later using only model ID, because that can lose `upstream_metadata` resolution.

## Task 10.5: Validate endpoint before durable selection

At the start of `RequestCoordinator.execute()`:

1. Fetch model from catalog cache.
2. If absent, return model-not-found.
3. If protocol unresolved, return catalog/protocol error.
4. Compare resolved model protocol to `context.protocol`.
5. Reject mismatch before creating any request, reservation, or attempt row.

Use protocol-specific 400 responses in endpoints.

## Required tests

- Unknown model with no mapping is not exposed.
- Unknown model cannot be routed as OpenAI by default.
- Anthropic model through `/chat/completions` returns 400 with zero request rows.
- OpenAI model through `/messages` returns 400 with zero request rows.
- Persisted protocol is used only after stronger sources fail.
- Protocol source remains `upstream_metadata` when that source resolved it.

## Section 10 acceptance criteria

- Protocol resolution is fail-closed and enforced before accounting begins.

---

# 11. Add body-aware 404 classification

## Goal

Retry account-specific model-not-found responses without treating every HTTP 404 as a model failure.

## Files

```text
src/go_aggregator/retry/classification.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/health/health_manager.py
tests/unit/test_retry_classification.py
tests/integration/test_failover_matrix.py
```

## Task 11.1: Extend classifier input

Change classifier signature to accept a bounded body:

```python
classify(
    status_code: int,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> RetryableError
```

Parse JSON when possible; otherwise inspect a bounded lowercased text representation.

## Task 11.2: Detect model-specific 404

Classify as model unavailable only when body semantics include model-specific signals, for example:

```text
model not found
unknown model
unsupported model
model is not available
```

Return a distinct category:

```python
RetryCategory.MODEL_UNAVAILABLE
```

Other 404 responses remain non-retryable deployment/path errors.

## Task 11.3: Apply account/model transition

For `MODEL_UNAVAILABLE`:

- Finalize failed attempt.
- Disable only the account/model pairing.
- Mark that pairing unavailable in catalog cache.
- Retry another account.
- Trigger a bounded targeted catalog refresh if available.

## Required tests

- Model-specific 404 from A, 200 from B -> two attempts, success.
- Generic HTML 404 -> no failover and no model disable.
- Disabled model affects only A/model, not other models on A.

## Section 11 acceptance criteria

- Model-specific 404 failover works without over-classifying all 404 responses.

---

# 12. Finish operational cleanup

## Goal

Remove remaining transaction/accounting drift and error-shaping inconsistencies.

## Files

```text
src/go_aggregator/app.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/api/errors.py
src/go_aggregator/api/chat_completions.py
src/go_aggregator/api/messages.py
tests/integration/test_error_envelopes.py
```

## Task 12.1: Commit reservation expiry reconciliation

Run `reconcile_expired()` inside `db.transaction()`.

For each expired reservation, also reconcile in-memory reservation totals. Preferred implementation:

- Query expired rows with account name and estimate.
- Mark them expired in one transaction.
- After commit, subtract exact amounts from in-memory tracking and active counts if still represented.

## Task 12.2: Catch body limit errors specifically

Endpoints must catch only `RequestTooLargeError` for 413.

Do not convert cancellation, disconnect, or unrelated read failures into 413.

## Task 12.3: Protocol-specific proxy errors

When no upstream response exists, `_handle_exhausted()` should return a structured proxy error object or raise a typed exception that endpoint renderers convert into:

- OpenAI-compatible error envelope.
- Anthropic-compatible error envelope.

Do not emit bare:

```json
{"error": "string"}
```

## Task 12.4: Remove stale comments and deprecated behavior

After tests pass, remove:

- Connection-wide nesting comments that no longer describe behavior.
- `_cleanup_in_memory()`.
- Arbitrary reservation reconstruction.
- Obsolete hourly/daily eligibility code if no longer used.
- Any protocol default to OpenAI.

## Section 12 acceptance criteria

- Background reconciliation is durable and synchronized.
- Error responses match endpoint protocol.

---

# 13. Required final test matrix

Create or expand:

```text
tests/integration/test_phase13_end_to_end.py
```

The following tests are mandatory.

## A. Successful failover cleanup

For each first-attempt failure:

```text
401
402
429
500
connect error
model-specific 404
```

Second account returns 200.

Assert:

- Overall response succeeds.
- Two attempts exist.
- Both attempts terminal.
- Both reservations released.
- Zero active reservations.
- Zero active request counts.
- Zero in-memory reserved totals.

## B. Concurrent transaction ownership

Run two concurrent request selections plus a readiness probe.

Assert:

- No transaction leakage.
- No unrelated rollback.
- All three operations reach consistent terminal state.

## C. Projected request selection

Incoming estimate changes selected account and persisted reservation matches the scoring estimate.

## D. Cancellation stages

Cancel:

- Before upstream headers.
- During stream.
- During non-streaming response wait.

Assert complete cleanup for each.

## E. No-usage success

Successful response without usage must persist nonzero estimated cost and influence immediate next routing.

## F. New price snapshot

Insert snapshot after migrations and calculate nonzero derived cost.

## G. Missing price category

Use input tokens with missing input rate. Assert exactness `estimated`, not `derived`.

## H. Protocol fail closed

Unknown model is neither exposed nor routed.

Wrong endpoint produces 400 and no lifecycle rows.

## I. Model-specific 404

A returns semantic model-not-found; B succeeds. Only A/model is disabled.

## J. Duplicate finalization

Second finalization call cannot overwrite request, attempt, or reservation terminal fields.

## K. Restart recovery

Create committed pending request, reservation, and incomplete attempt. Simulate restart and confirm recovery remains compatible with new per-attempt semantics.

## L. Privacy regression

Search database and logs for prompt, response, API key, and authorization marker strings. None may appear.

---

# 14. Recommended implementation order

Follow this order exactly:

1. Task-safe transaction ownership.
2. Per-attempt finalizer.
3. Retry loop integration and removal of leaked-attempt cleanup.
4. Request-local exhaustion handling.
5. Cancellation coverage.
6. Projected request estimates.
7. No-usage accounting and immediate estimated-cost feedback.
8. Price snapshot write consistency.
9. Telemetry latency and full idempotency.
10. Quota eligibility alignment.
11. Protocol fail-closed resolution and endpoint validation.
12. Body-aware 404 classification.
13. Operational cleanup and protocol error envelopes.
14. Full Phase 13 integration matrix.

Do not begin protocol work before attempt and transaction correctness are complete.

---

# 15. Suggested commit sequence

```text
fix: serialize sqlite transaction ownership by task
fix: add per-attempt finalization and reservation release
fix: retain selected attempt state through failover exhaustion
fix: finalize cancellation across pre-response paths
fix: include projected request cost in routing
fix: account for successful responses without usage
fix: make price snapshot writes use canonical integer units
fix: persist real latency and strengthen finalizer idempotency
fix: align quota eligibility with three-window scoring
fix: fail closed on unresolved model protocols
fix: classify account-specific model 404 responses
fix: synchronize expiry cleanup and protocol error envelopes
test: add phase 13 lifecycle matrix
```

---

# 16. Definition of done

Phase 13 is done only when all of these statements are true:

1. Each failed attempt is completed before another attempt begins.
2. Each attempt reservation is released exactly once.
3. Successful failover leaves no active reservation or incomplete attempt.
4. SQLite transaction ownership is serialized across tasks.
5. Readiness cannot interfere with request transactions.
6. Incoming projected cost affects account selection.
7. Exhaustion uses retained `SelectedAttempt` state, not reconstructed rows.
8. Cancellation is safe before headers, during streaming, and during buffered responses.
9. Successful no-usage responses consume estimated quota.
10. Estimated costs affect immediate subsequent routing.
11. New price snapshots populate integer rates.
12. Missing billable rates cannot be labeled derived.
13. Stored latency is real.
14. Duplicate finalization cannot overwrite attempt telemetry.
15. Quota eligibility and scoring use consistent three-window semantics.
16. Unresolved protocols are not exposed or routed.
17. Wrong protocol endpoints fail before lifecycle persistence.
18. Model-specific 404 can fail over without disabling unrelated models.
19. Reservation expiry reconciliation is committed and synchronized in memory.
20. Proxy-generated errors use protocol-compatible envelopes.
21. Full CI and Phase 13 integration tests pass.
22. No request content or secrets are persisted.
