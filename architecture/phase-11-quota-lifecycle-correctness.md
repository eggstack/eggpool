# Phase 11: Quota, Lifecycle, and Failover Correctness

## Purpose

Phase 10 established the correct architectural shape: a shared request coordinator, repository-backed persistence, startup account synchronization, model discovery, streaming pass-through, usage extraction, and expanded observability.

The remaining defects are now concentrated in composition and invariants rather than missing subsystems. This phase must make the router's actual behavior match the product goal:

- balance subscriptions by projected normalized quota utilization;
- durably account for every paid upstream attempt;
- never leave requests or reservations pending after a terminal path;
- never retry after downstream response commitment;
- preserve protocol semantics and raw response bytes;
- keep routing state current without requiring restart;
- classify and fail over account-specific failures correctly.

This phase should not introduce broad new features. It is a correctness and hardening pass over the existing vertical request lifecycle.

---

## Phase exit outcomes

Phase 11 is complete when all of the following are true:

1. Routing uses explicit five-hour, seven-day, and thirty-day capacities and offsets.
2. Active reservations and the projected cost of the incoming request affect selection.
3. Selection, durable pending-request assignment, reservation creation, attempt creation, and in-flight increment occur atomically before upstream dispatch.
4. A process crash after upstream dispatch cannot erase all evidence of the attempt.
5. Every terminal path finalizes the request and releases its reservations exactly once.
6. Ordinary upstream 4xx pass-through responses do not leave pending requests or active reservations.
7. Client cancellation is explicitly finalized as cancellation and closes upstream promptly.
8. Streaming usage parsing handles arbitrary HTTP chunk boundaries through `IncrementalSSEObserver`.
9. Final observed cost updates both SQLite and live routing state immediately.
10. Pricing migration values are dimensionally correct and cache pricing is representable.
11. Retry attempts exclude previously attempted accounts and apply account/model-specific health transitions.
12. Non-streaming response bodies are returned transparently without requiring JSON decoding and reserialization.
13. Model protocol resolution supports a mixed OpenAI/Anthropic catalog.
14. Tests demonstrate the invariants under concurrency, cancellation, retries, restart, and malformed streams.

---

# 1. Replace the legacy quota model

## 1.1 Canonical quota state

Retire the use of `max_hourly_cost_microdollars`, `max_daily_cost_microdollars`, one-hour fallback windows, one-day fallback windows, and one generic manual offset for routing decisions.

Introduce an explicit immutable quota configuration per account:

```text
AccountQuotaPolicy
- account_name
- weight
- capacity_5h_microdollars
- capacity_7d_microdollars
- capacity_30d_microdollars
- offset_5h_microdollars
- offset_7d_microdollars
- offset_30d_microdollars
```

Introduce a current usage snapshot:

```text
AccountUsageSnapshot
- observed_5h_microdollars
- observed_7d_microdollars
- observed_30d_microdollars
- active_reserved_microdollars
- active_request_count
- loaded_at
```

Introduce a projected scoring input:

```text
ProjectedAccountUsage
- policy
- snapshot
- request_estimate_microdollars
- health_penalty
```

The scorer must no longer reach into mutable `AccountQuota` internals. It should consume complete projected inputs and return deterministic scores.

## 1.2 Capacity initialization

At startup, build each account's capacities from global limits and account weight:

```text
capacity_5h = limits.five_hour_microdollars * weight
capacity_7d = limits.weekly_microdollars * weight
capacity_30d = limits.monthly_microdollars * weight
```

Use integer arithmetic. Reject weights less than or equal to zero during config validation.

## 1.3 Per-window offsets

Load these configuration values independently:

```text
five_hour_offset_microdollars
weekly_offset_microdollars
monthly_offset_microdollars
```

Do not collapse them into one offset.

Offsets must only affect their corresponding normalized window.

## 1.4 Projected score

For account `i`:

```text
p5_i = (
    observed_5h_i
    + offset_5h_i
    + active_reserved_i
    + request_estimate
) / capacity_5h_i
```

```text
p7_i = (
    observed_7d_i
    + offset_7d_i
    + active_reserved_i
    + request_estimate
) / capacity_7d_i
```

```text
p30_i = (
    observed_30d_i
    + offset_30d_i
    + active_reserved_i
    + request_estimate
) / capacity_30d_i
```

```text
score_i =
    max(p5_i, p7_i, p30_i)
    + mean_weight * mean(p5_i, p7_i, p30_i)
    + active_request_count_i * inflight_penalty
    + health_penalty_i
```

Do not clamp utilization to 1.0 before scoring. Values greater than one are useful because they preserve how far over capacity an account is.

## 1.5 Durable source of truth

SQLite remains the durable source of observed usage and active reservations.

Add a repository method that returns a coherent snapshot for all candidate accounts in as few queries as practical:

```text
UsageWindowRepository.get_account_snapshots(
    account_ids,
    now,
) -> dict[account_id, AccountUsageSnapshot]
```

The query must include:

- request cost in the previous five hours;
- request cost in the previous seven days;
- request cost in the previous thirty days;
- active reservation sum;
- active request count or equivalent runtime count.

Use completed, errored, interrupted, and estimated billable requests according to explicit accounting semantics. Exclude only requests that are known not to have reached upstream or have zero non-billable cost by policy.

## 1.6 Live cache

A short-lived snapshot cache is acceptable, but it must be invalidated after:

- reservation creation;
- reservation release;
- request finalization;
- manual offset reload;
- account weight or limit changes.

The cache should be an optimization, not the source of truth.

## 1.7 Remove dead or conflicting routing state

Retire or isolate:

- hourly and daily windows for quota selection;
- the in-memory `ReservationManager` if SQLite reservations are canonical;
- duplicated reservation state held separately by `Router` and repositories;
- generic `manual_offset` in the score path.

One authoritative reservation model is required.

### Acceptance criteria

- Persisted five-hour usage can change the selected account.
- Persisted seven-day usage can change the selected account independently of five-hour usage.
- Persisted thirty-day usage can change the selected account independently of shorter windows.
- Each offset affects only its intended window.
- Weight 2.0 doubles all three capacities.
- Active reservation cost affects the next request's selection.
- Utilization above 100% remains distinguishable from exactly 100%.

---

# 2. Atomic and durable selection transaction

## 2.1 Required critical section

The current lock boundary must be expanded. Account selection alone is insufficient.

Under one process-local selection lock, perform:

1. Ensure or create the pending request row.
2. Load coherent candidate usage and reservation snapshots.
3. Exclude previously attempted accounts.
4. Estimate projected request cost for each account/model pair.
5. Score and select an account.
6. Resolve its database account ID and API key from the same runtime registry.
7. Assign the selected account to the pending request.
8. Insert the active reservation.
9. Insert the request-attempt row.
10. Increment runtime active request count.
11. Commit the transaction.
12. Invalidate the routing snapshot cache.
13. Release the lock.

Only then may network I/O begin.

## 2.2 Database transaction API

Add an explicit transaction context to `Database`, for example:

```python
async with db.transaction():
    ...
```

Requirements:

- rollback on exception;
- no implicit nested transactions without a defined policy;
- repository methods do not commit independently while used inside the transaction;
- selection code commits before upstream dispatch;
- the lock is not held during HTTP I/O.

## 2.3 Request identity

Persist the external proxy UUID directly.

Preferred schema:

```text
requests.id TEXT PRIMARY KEY
```

If converting the existing integer primary key is too disruptive, add:

```text
proxy_request_id TEXT NOT NULL UNIQUE
```

The `x-proxy-request-id` value must be queryable directly in SQLite and dashboard diagnostics.

Do not accept a method parameter named `request_id` and then ignore it.

## 2.4 Selection result

Return a durable selection object:

```text
SelectedAttempt
- request_id
- attempt_id
- reservation_id
- account_id
- account_name
- api_key
- estimated_microdollars
- attempt_number
```

This object should be the only input needed to execute and later finalize that attempt.

## 2.5 Failure before upstream dispatch

If any step before commit fails:

- rollback all request assignment, reservation, and attempt changes;
- no upstream request may be sent;
- return an internal persistence or routing error;
- do not leave a partially selected request.

### Acceptance criteria

- Two concurrent requests cannot both score the same pre-reservation state.
- A concurrency test shows reservations spreading requests when accounts are otherwise tied.
- Killing the process after upstream dispatch but before finalization leaves a durable pending request, active reservation, and attempt record.
- Selection transaction failure results in zero upstream calls.
- The persisted proxy UUID matches the response header UUID.

---

# 3. Unified idempotent finalization

## 3.1 One finalizer

Replace scattered completion, error, pass-through, cancellation, and reservation cleanup logic with one idempotent finalization service.

Suggested interface:

```text
RequestFinalizer.finalize(
    selected_attempt,
    outcome,
    usage,
    response_metadata,
    error_metadata,
)
```

`outcome` should be an enum:

```text
completed
client_error
upstream_error
midstream_error
client_cancelled
proxy_cancelled
timeout
interrupted
```

## 3.2 Exactly-once semantics

Finalization must be safe to call more than once due to races between:

- generator exception;
- cancellation;
- response close;
- outer request task cancellation;
- shutdown.

Use conditional updates such as:

```sql
UPDATE requests
SET ...
WHERE id = ? AND status = 'pending'
```

or an explicit finalization marker.

Reservation release should similarly update only rows with `status = 'active'`.

## 3.3 Finalization transaction

One transaction should:

1. Update attempt terminal status and emitted bytes.
2. Update request terminal status and metadata.
3. Release active reservations.
4. Persist usage and exactness.
5. Persist final calculated or estimated cost.
6. Decrement runtime in-flight state.
7. Update or invalidate live quota snapshot.
8. Commit.

Health state may be updated immediately after the database transaction if it remains in-memory, but the transition reason should be persisted as an event.

## 3.4 Pass-through 4xx responses

Ordinary non-retryable upstream client errors must be treated as terminal outcomes.

Before returning the upstream body:

- finalize request as `client_error` or equivalent;
- persist status code;
- release reservation;
- mark attempt completed;
- retain upstream request ID;
- calculate any observed cost if usage exists;
- commit.

This applies to both streaming and non-streaming pre-body errors.

## 3.5 Unknown and partial usage

When no terminal usage is received:

- use captured partial token usage when available;
- otherwise use the active reservation estimate as terminal estimated cost;
- mark exactness `estimated`;
- never represent unknown billable usage as exact zero.

## 3.6 First-byte and retry metadata

Finalization must persist:

- actual first-byte latency;
- attempt count or retry count;
- final upstream request ID;
- cache-read tokens;
- cache-write/cache-creation tokens;
- reasoning tokens when explicit;
- thinking characters separately;
- emitted bytes.

### Acceptance criteria

- Every terminal path leaves no active reservation.
- A non-streaming upstream 400 leaves a terminal request and completed attempt.
- Repeated finalizer calls do not change terminal accounting or double-release.
- Unknown usage results in estimated cost, not exact zero.
- First-byte and retry values are nonzero when expected.

---

# 4. Streaming observer and cancellation correctness

## 4.1 Integrate `IncrementalSSEObserver`

Remove the per-chunk `decode().split("\n")` usage parser from `RequestCoordinator`.

Use:

```python
observer = IncrementalSSEObserver(protocol=context.protocol)

async for chunk in response.aiter_bytes():
    observer.observe(chunk)
    yield chunk

observer.flush()
usage = observer.usage
```

Forwarding must remain raw-byte pass-through. Observer parsing must never mutate, delay, or reject downstream bytes.

## 4.2 Correct frame semantics

Strengthen `IncrementalSSEObserver` so it parses full SSE events rather than treating each `data:` line independently.

Requirements:

- arbitrary byte chunk boundaries;
- UTF-8 decoder state across chunks;
- LF and CRLF;
- multiple `data:` lines per event;
- event boundaries on blank lines;
- `data:` with or without one optional space;
- comments and unknown fields ignored;
- `[DONE]` recognized;
- bounded incomplete-event memory;
- malformed telemetry logged without breaking forwarding.

Use an incremental UTF-8 decoder rather than `errors="replace"` on independent chunks, because a multibyte sequence can be split across chunks.

## 4.3 Cancellation handling

Catch `asyncio.CancelledError` explicitly.

Cancellation path:

1. Record emitted bytes and captured usage.
2. Finalize as `client_cancelled` or `proxy_cancelled`, depending on known cause.
3. Release reservation.
4. Decrement active count.
5. Close upstream response.
6. Re-raise `CancelledError`.

Do not classify normal client cancellation as account failure.

## 4.4 Midstream failure

For non-cancellation exceptions after bytes were emitted:

- do not retry;
- finalize as `midstream_error`;
- preserve partial usage and emitted byte count;
- apply upstream health failure only when attributable to upstream;
- close response in `finally`.

## 4.5 Streaming pre-body errors

When the upstream responds with a non-success status before downstream commitment:

- read the bounded error body;
- classify it;
- either retry another account or finalize and return the original status/body;
- never replace an upstream 4xx with a generic 200 stream;
- never discard the upstream error body when no retry succeeds.

### Acceptance criteria

- Usage extraction succeeds when every byte boundary of an SSE frame is split independently.
- Multibyte UTF-8 split across chunks is parsed correctly.
- Raw forwarded bytes exactly equal upstream bytes.
- Cancellation produces terminal cancellation state and no active reservation.
- Midstream errors do not produce another upstream attempt.
- Unknown SSE events do not interrupt forwarding.

---

# 5. Pricing and accounting repair

## 5.1 Corrective migration

Do not edit migration `0005` after it may have been applied.

Add migration `0006_correct_price_microdollars.sql`.

The legacy unit is dollars per 1,000 tokens. The target unit is microdollars per 1,000,000 tokens.

Correct conversion:

```text
microdollars_per_million = dollars_per_thousand * 1_000_000_000
```

Migration behavior:

- identify rows backfilled by the faulty conversion;
- recompute integer fields from legacy float columns;
- leave explicitly supplied correct integer values intact where distinguishable;
- record a source/version marker if necessary;
- add migration tests with representative prices.

Examples:

```text
$0.003 per 1K -> 3,000,000 microdollars per 1M
$3.00 per 1K -> 3,000,000,000 microdollars per 1M
$15.00 per 1K -> 15,000,000,000 microdollars per 1M
```

## 5.2 Complete price snapshot model

Add integer fields for:

```text
input_per_million_microdollars
output_per_million_microdollars
cache_read_per_million_microdollars
cache_write_per_million_microdollars
```

Retain source and metadata.

## 5.3 Cost calculation

Calculate:

```text
input_cost = input_tokens * input_rate / 1_000_000
output_cost = output_tokens * output_rate / 1_000_000
cache_read_cost = cache_read_tokens * cache_read_rate / 1_000_000
cache_write_cost = cache_write_tokens * cache_write_rate / 1_000_000
```

Use integer arithmetic and define rounding policy. For very small requests, aggregate numerator terms before integer division to reduce systematic truncation:

```text
total_numerator =
    input_tokens * input_rate
    + output_tokens * output_rate
    + cache_read_tokens * cache_read_rate
    + cache_write_tokens * cache_write_rate

cost = total_numerator // 1_000_000
```

## 5.4 Snapshot insertion

Ensure discovered or configured prices actually produce snapshots. Do not create a `CostCalculator` over an empty repository and assume nonzero derived costs will appear.

At startup/catalog refresh:

- parse price metadata when available;
- insert a new snapshot only when values or source change;
- apply explicit configured overrides;
- retain built-in fallback estimates as `estimated`, not `derived`.

## 5.5 Final-cost feedback

After finalization:

- increment the live observed usage snapshot by final cost;
- update account/model and global model EWMAs using exact or derived observations;
- use reduced weight or exclude estimated observations from EWMA learning;
- invalidate any database-backed snapshot cache.

The next request must see the completed request's cost without requiring restart.

### Acceptance criteria

- Corrective migration produces dimensionally correct values.
- Cache-heavy requests include cache rates in cost.
- Derived costs use immutable snapshots.
- Estimated fallback costs are labeled estimated.
- A completed request changes subsequent account scoring immediately.

---

# 6. Retry, failover, and health transitions

## 6.1 Attempt exclusion

Maintain request-local state:

```text
attempted_accounts: set[str]
```

Each new selection must exclude already attempted accounts unless an explicit policy allows reuse after all alternatives are exhausted. The default should not reuse an account in the same request.

## 6.2 Configured retry budget

Use `config.routing.max_retries_before_stream` rather than a hard-coded module constant.

Define clearly whether the value means:

- total attempts; or
- retries after the first attempt.

Prefer `max_attempts` internally to avoid ambiguity.

## 6.3 Error-body-aware classification

Classification should consider:

- HTTP status;
- relevant response headers;
- bounded parsed error body;
- protocol;
- model and account context.

A generic 404 may be a path/configuration error rather than model unavailability. Only disable a model/account pairing when the response semantics support that conclusion.

## 6.4 Health transitions

Implement explicit coordinator actions:

### Authentication failure

- mark account authentication-failed;
- remove it from subsequent eligibility;
- persist event;
- attempt another account when available.

### Quota/balance exhaustion

- mark account quota cooldown/exhausted;
- apply configurable cooldown;
- persist event;
- attempt another account.

### Rate limit

- honor parsed `Retry-After` date or seconds;
- otherwise apply backoff with jitter;
- persist event;
- attempt another account.

### Model unavailable for account

- disable only that account/model pairing;
- trigger targeted catalog refresh;
- persist event;
- attempt another account.

### Transport or selected 5xx failure

- record circuit-breaker failure;
- apply cooldown after threshold;
- attempt another account.

### Invalid client request

- do not penalize account health;
- return upstream response.

### Successful request

- record success;
- clear eligible transient state;
- do not automatically restore authentication-disabled state without explicit successful probe or restart.

## 6.5 Exhausted failover response

When all candidates fail:

- return the most semantically useful final upstream error status and body;
- include proxy request ID and attempt count;
- use protocol-compatible error shaping only for proxy-generated errors;
- do not replace a real upstream error body with only `str(exception)`.

## 6.6 Attempt accounting

Every attempted account must have a completed attempt row with:

- attempt number;
- account ID;
- start and completion time;
- status code;
- error class and bounded detail;
- upstream request ID;
- emitted bytes;
- whether any response body was committed.

### Acceptance criteria

- First account 429, second account 200 results in two attempts and one completed request.
- First account 401 is disabled and another account is attempted.
- Account/model 404 disables only that pairing.
- Transport retries do not repeatedly choose the same failed account.
- Client 400 does not affect account health.
- Exhausted retries preserve useful upstream error semantics.

---

# 7. Transparent response rendering

## 7.1 Raw non-streaming responses

`PreparedProxyResponse.body` must be returned as raw bytes through `Response`, not decoded through `json.loads()` and reconstructed with `JSONResponse`.

Use the filtered upstream `content-type` header.

This preserves:

- exact JSON representation;
- non-JSON error bodies;
- future protocol payloads;
- charset parameters;
- unknown response formats.

## 7.2 Proxy-generated errors

Only proxy-generated validation/routing/internal errors should use protocol-specific error renderers.

OpenAI-compatible envelope:

```json
{
  "error": {
    "message": "...",
    "type": "server_error",
    "code": "no_eligible_account"
  }
}
```

Anthropic-compatible envelope should follow the corresponding message error structure.

## 7.3 Header behavior

Preserve safe upstream headers including:

- content type;
- request ID;
- rate-limit metadata;
- retry-after;
- cache controls where appropriate.

Remove:

- hop-by-hop headers;
- streaming content length;
- upstream authorization details.

Add:

```text
x-proxy-request-id
x-proxy-attempt-count
```

### Acceptance criteria

- Non-JSON upstream bodies are returned successfully.
- Raw JSON bytes are not reserialized.
- Proxy-generated errors are protocol-compatible.
- Final attempt count appears in headers and persistence.

---

# 8. Mixed-protocol model resolution

## 8.1 Resolver abstraction

Add:

```text
ModelProtocolResolver.resolve(model_id, model_metadata) -> openai | anthropic
```

Resolution priority:

1. Explicit TOML override.
2. Explicit per-model upstream endpoint/protocol metadata.
3. Maintained exact-model mapping.
4. Maintained model-family mapping.
5. Previously persisted protocol when model identity is unchanged.
6. Fail closed as unresolved.

Do not assign one protocol to the entire `/models` response solely from top-level response shape.

## 8.2 Catalog persistence

Persist:

- resolved protocol;
- resolution source;
- source metadata;
- first and last seen timestamps.

A protocol change should be observable as an event.

## 8.3 Route validation

Reject a request sent to the wrong local protocol endpoint when the resolved model protocol conflicts, unless an explicit compatibility policy is configured.

Return a protocol-compatible validation error explaining the correct endpoint family without leaking account details.

### Acceptance criteria

- One catalog refresh can contain both OpenAI-family and Anthropic-family models.
- Explicit overrides win.
- Unresolved protocols are not guessed silently.
- Protocol changes are persisted and observable.

---

# 9. Request-body bounds and middleware truthfulness

## 9.1 Streaming body limit

The current post-buffer length check is not a real memory limit.

Implement bounded request-body reading:

- reject immediately when `Content-Length` exceeds configured maximum;
- otherwise consume `request.stream()` incrementally;
- stop and return 413 once accumulated bytes exceed the limit;
- do not retain more than the configured maximum plus minimal parser overhead.

## 9.2 Security middleware

Either enforce or remove configuration for:

- allowed hosts;
- CORS origins;
- trusted proxy headers.

Install `TrustedHostMiddleware` and `CORSMiddleware` where configured.

Do not claim proxy-header trust behavior unless ASGI server and application configuration actually implement it.

### Acceptance criteria

- Oversized chunked requests do not require full-body buffering.
- Allowed-host configuration is enforced.
- CORS configuration is enforced or removed from the schema.

---

# 10. Schema and repository changes

## 10.1 Migration set

Expected new migrations:

```text
0006_correct_price_microdollars.sql
0007_request_identity_and_finalization.sql
0008_quota_policy_fields.sql      # only if quota policy is persisted
0009_price_cache_rates.sql
```

Keep migrations forward-only and test upgrade from a database at migration 0005.

## 10.2 Repository API cleanup

Recommended repositories:

```text
AccountRepository
RequestRepository
AttemptRepository
ReservationRepository
UsageSnapshotRepository
PriceSnapshotRepository
AccountEventRepository
```

Add compound methods matching transaction invariants rather than forcing coordinator code to compose fragile low-level calls:

```text
RequestRepository.create_or_get_pending(...)
ReservationRepository.sum_active_by_accounts(...)
AttemptRepository.create_attempt(...)
RequestRepository.finalize_if_pending(...)
UsageSnapshotRepository.load_projected_inputs(...)
```

Avoid repeated construction of `AccountRepository` inside every coordinator helper. Inject repositories once.

## 10.3 No repository-local commits in shared transactions

Repository methods should not commit unless their API explicitly owns the entire transaction. The coordinator or finalizer should own transaction boundaries.

### Acceptance criteria

- Upgrade from migration 0005 succeeds.
- All repository calls can participate in coordinator-owned transactions.
- No hidden commit splits selection or finalization atomicity.

---

# 11. Test plan

## 11.1 Quota scorer unit tests

Use explicit numeric fixtures.

Test:

- five-hour-only imbalance;
- seven-day-only imbalance;
- thirty-day-only imbalance;
- over-capacity values greater than 1.0;
- account weights;
- independent offsets;
- active reservation contribution;
- projected request estimate contribution;
- active request penalty;
- health penalty;
- deterministic seeded near-tie selection.

## 11.2 Atomic-selection concurrency tests

Use two accounts and many simultaneous tasks.

Prove:

- selection and reservation occur under the same critical section;
- each subsequent task sees earlier active reservations;
- distribution is not a herd onto the first account;
- simulated repository failure produces no upstream calls;
- persisted records are committed before mocked upstream execution begins.

The current single-request “atomic” test must be replaced with a real concurrent test.

## 11.3 Terminal-path matrix

For both protocol families and streaming/non-streaming modes:

| Path | Expected request outcome | Reservation | Retry |
|---|---|---|---|
| 200 success | completed | released | no |
| 400 client error | client_error | released | no |
| 401 first account | success or auth error | released | yes if alternative |
| 402 first account | success or exhausted | released | yes |
| 404 model-specific | success or unavailable | released | yes |
| 429 | success or rate limited | released | yes |
| connect timeout | success or timeout | released | yes |
| 500 | success or upstream error | released | yes |
| midstream reset | midstream_error | released | no |
| client cancellation | client_cancelled | released | no |

Assert request, attempt, reservation, health, and event rows for each case.

## 11.4 SSE boundary tests

Generate a known SSE byte sequence and split it:

- at every byte boundary;
- across multibyte UTF-8 characters;
- across CRLF pairs;
- between `data:` and payload;
- inside JSON strings;
- across blank-line event delimiters.

Assert identical downstream bytes and identical extracted usage.

## 11.5 Pricing migration tests

Create a pre-0006 database with legacy price rows and run migrations.

Assert exact conversions for representative decimal prices and null fields.

## 11.6 Restart tests

- Dispatch a request after durable selection commit.
- Simulate process death before finalization.
- Restart.
- Confirm crash recovery retains attempt evidence, releases stale reservation, and assigns estimated accounting rather than erasing usage.
- Confirm routing remains influenced by historical usage after restart.

## 11.7 Response transparency tests

- Raw JSON whitespace preserved.
- Non-JSON 502 body preserved.
- Content type with charset preserved.
- Binary-safe body passed through for non-streaming path.
- Proxy-generated errors use protocol envelope.

## 11.8 Load/soak tests

- 20 concurrent streams with reservation-aware balancing.
- Repeated cancellation with zero leaked active reservations.
- Repeated pre-body failovers.
- Long-running usage updates without restart.
- SQLite WAL remains bounded through checkpoint policy.

---

# 12. Recommended implementation sequence

## Step 1: Correct pricing migration immediately

Add and test migration `0006` before more development databases accumulate incorrect integer snapshots.

## Step 2: Introduce canonical quota policy and snapshot objects

Replace scorer inputs and configure capacities/offsets. Verify pure scorer unit tests before coordinator changes.

## Step 3: Implement coherent usage/reservation snapshot query

Make SQLite provide the exact data required by scoring.

## Step 4: Refactor selection into one committed transaction

Add `SelectedAttempt`, expand the lock boundary, and prove concurrent behavior.

## Step 5: Build idempotent finalizer

Route all existing success and failure paths through it, including ordinary 4xx responses.

## Step 6: Integrate incremental SSE observer and cancellation handling

Remove old per-chunk line parsing. Add explicit `CancelledError` behavior.

## Step 7: Feed final usage and cost back into live routing

Invalidate snapshots and update EWMAs after finalization.

## Step 8: Repair failover and health semantics

Add attempted-account exclusion and explicit transitions for 401/402/404/429/transport/5xx.

## Step 9: Make responses transparent

Return raw non-streaming bytes and preserve final upstream errors.

## Step 10: Resolve mixed model protocols

Add resolver and catalog persistence changes.

## Step 11: Enforce body limits and declared middleware

Finish operational security consistency.

## Step 12: Run full matrix and soak tests

Do not mark Phase 11 complete based only on unit tests or successful happy-path requests.

---

# 13. Expected file changes

Major modifications are expected in:

```text
src/go_aggregator/models/config.py
src/go_aggregator/app.py
src/go_aggregator/db/connection.py
src/go_aggregator/db/repositories.py
src/go_aggregator/db/schema/0006_correct_price_microdollars.sql
src/go_aggregator/db/schema/0007_request_identity_and_finalization.sql
src/go_aggregator/db/schema/0009_price_cache_rates.sql
src/go_aggregator/quota/estimation.py
src/go_aggregator/quota/scorer.py
src/go_aggregator/routing/router.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/request/finalizer.py
src/go_aggregator/proxy/sse_observer.py
src/go_aggregator/proxy/usage.py
src/go_aggregator/catalog/pricing.py
src/go_aggregator/catalog/normalizer.py
src/go_aggregator/catalog/protocols.py
src/go_aggregator/health/health_manager.py
src/go_aggregator/api/chat_completions.py
src/go_aggregator/api/messages.py
src/go_aggregator/api/errors.py
src/go_aggregator/stats/queries.py
src/go_aggregator/stats/service.py
```

Potential new abstractions:

```text
AccountQuotaPolicy
AccountUsageSnapshot
ProjectedAccountUsage
SelectedAttempt
RequestFinalizer
FinalizationOutcome
ModelProtocolResolver
UsageSnapshotRepository
```

Each abstraction must own a concrete invariant. Avoid adding wrappers that merely rename existing calls.

---

# 14. Invariants

The following invariants must be encoded in tests and comments near the transaction boundaries:

1. No upstream request is sent before pending request, reservation, and attempt records are committed.
2. Selection and reservation observe one coherent state under one critical section.
3. Previously attempted accounts are excluded from normal failover selection.
4. Every terminal path invokes idempotent finalization.
5. Every active reservation reaches released or expired state.
6. A pass-through 4xx is still a terminal lifecycle event.
7. Cancellation finalizes accounting before propagating cancellation.
8. No retry occurs after any downstream body byte is emitted.
9. Unknown usage is not represented as exact zero cost.
10. Final cost changes live routing state immediately.
11. The proxy request UUID is persisted and queryable.
12. Raw response forwarding does not depend on telemetry parsing.
13. Model-specific failure cannot disable unrelated models.
14. Client errors do not penalize account health.
15. Per-window offsets never bleed into other quota windows.
16. Pricing units are dimensionally explicit and tested.
17. Prompts, completions, tool arguments, and API keys remain absent from persistent telemetry.

---

# 15. Phase exit validation

Run this end-to-end sequence with two mocked accounts and then real personal accounts:

1. Start from a database at migration 0005 and verify corrective migrations.
2. Confirm model discovery resolves at least one model for each protocol family.
3. Seed account A with high five-hour usage and verify B is selected.
4. Seed B with high seven-day usage and verify scoring changes appropriately.
5. Add active reservations and verify projected selection changes.
6. Submit simultaneous requests and verify no herd selection.
7. Inspect SQLite before the upstream mock returns and confirm committed pending request, reservation, and attempt.
8. Return a non-streaming 400 and verify terminal finalization.
9. Return 429 from A and 200 from B; verify two attempts and A cooldown.
10. Return model-specific 404 from A and 200 from B; verify only A/model is disabled.
11. Split an SSE terminal usage event at every byte boundary and verify extraction.
12. Cancel a stream after several chunks and verify cancellation finalization and upstream close.
13. Reset a stream mid-response and verify no retry.
14. Verify final request costs include cache token pricing.
15. Submit another request immediately and verify it sees the prior cost without restart.
16. Restart and verify historical usage still drives routing.
17. Return a non-JSON upstream error body and verify transparent pass-through.
18. Confirm dashboard and direct SQL agree on usage, reservations, retries, exactness, and health events.
19. Search SQLite and logs for known prompt, completion, key, and authorization marker strings; none may appear.

---

# Definition of done

Phase 11 is done when GoRouter's routing and accounting behavior is correct under concurrency and failure, not merely when the happy path works.

The final implementation must demonstrate:

- true projected quota balancing across three independent windows;
- atomic, durable pre-dispatch accounting;
- exactly-once terminal finalization;
- correct streaming usage parsing and cancellation cleanup;
- dimensionally correct cost calculation;
- immediate live routing feedback;
- account-aware failover without repeated selection of failed accounts;
- transparent downstream responses;
- mixed-protocol catalog resolution;
- complete lifecycle evidence in SQLite without storing user content.
