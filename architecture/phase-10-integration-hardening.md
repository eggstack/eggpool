# Phase 10: Integration Hardening and Correct Request Lifecycle

## Purpose

The repository now contains most of the planned subsystems, but several are not yet connected through a single coherent request lifecycle. This phase must convert the current component-level implementation into a reliable vertical data path suitable for real OpenCode Go traffic.

The priority is not new surface area. The priority is correctness across startup, catalog discovery, account persistence, routing, reservations, upstream execution, streaming, usage reconciliation, failover, health state, and observability.

At the end of this phase, the service should be safe to run against paid subscriptions on a Raspberry Pi for normal personal use.

---

## Primary outcomes

This phase is complete when:

1. A clean installation using `config.example.toml` starts successfully and reaches the documented OpenCode Go API.
2. Configured accounts are synchronized into SQLite before any catalog or request work occurs.
3. Startup model discovery actually runs, and readiness reflects whether a usable catalog exists.
4. Both `/v1/chat/completions` and `/v1/messages` use one shared request coordinator.
5. Account selection, estimated-cost reservation, request persistence, upstream attempts, usage reconciliation, health transitions, and final accounting occur as one explicit lifecycle.
6. Routing is based on persisted five-hour, seven-day, and thirty-day usage rather than empty in-memory hourly/daily approximations.
7. Streaming responses preserve upstream status, headers, event ordering, and raw bytes.
8. Pre-body failover is implemented; replay after downstream body emission is prohibited.
9. Recorded request costs are nonzero whenever usage and pricing permit calculation.
10. Process restart, cancellation, timeout, and configuration reload cannot leave orphaned reservations or inconsistent runtime state.

---

## Non-goals

Do not expand the project during this phase into:

- A general multi-provider gateway.
- A multi-user service.
- A protocol translation layer between OpenAI and Anthropic request formats.
- A distributed or multi-worker deployment.
- A new dashboard framework.
- Prompt or response logging.
- Authoritative reconciliation with undocumented OpenCode account reset windows.
- Web-console scraping.

Existing dashboard work may be corrected to consume accurate data, but feature expansion should wait until the request lifecycle is trustworthy.

---

## Critical implementation principle

Introduce a single orchestration boundary for all data-plane requests.

Endpoint handlers must stop coordinating the request manually. They should authenticate, parse enough of the request to identify protocol/model/streaming intent, then delegate to a shared coordinator.

Recommended boundary:

```text
RequestCoordinator
├── validate and normalize request metadata
├── resolve model and protocol
├── create pending request record
├── load account usage snapshot
├── estimate projected request cost
├── atomically select and reserve account
├── create request attempt record
├── open upstream response
├── classify pre-body failures
├── fail over when safe
├── construct downstream response
├── observe usage while forwarding
├── reconcile cost and reservation
├── update account health
└── finalize request and attempt records
```

The coordinator should have no FastAPI-specific response construction below a narrow adapter layer where practical. OpenAI and Anthropic endpoints may retain protocol-specific response rendering, but selection, persistence, reservation, retry, and accounting logic must be shared.

---

# Workstream A: Configuration and startup correction

## A1. Align defaults with the actual OpenCode Go service

Update `UpstreamConfig.base_url` and `config.example.toml` to:

```toml
base_url = "https://opencode.ai/zen/go/v1"
```

Update default quota capacities to the currently documented subscription values:

```toml
[limits]
five_hour_microdollars = 12_000_000
weekly_microdollars = 30_000_000
monthly_microdollars = 60_000_000
```

Keep all capacities configurable because OpenCode may change them.

## A2. Make the example configuration authoritative

`config.example.toml` must be generated from or tested against the current Pydantic schema.

Remove obsolete fields such as:

- `expose_mode = "all"`
- `strategy = "round-robin"`
- `epsilon`
- `max_retries`
- `retry_backoff_s`
- account `priority`
- account `models`
- unsupported pricing override fields
- renamed dashboard and security fields

Add a test that loads `config.example.toml` with placeholder environment variables and asserts successful validation.

## A3. Fail closed for data-plane authentication

A missing local data-plane API key must not silently disable authentication.

Choose one explicit model:

```toml
[server]
auth_enabled = true
api_key_env = "GO_AGGREGATOR_API_KEY"
```

When `auth_enabled = true`, startup must fail if the key is unavailable. Disabling authentication must require an explicit configuration setting.

## A4. Synchronize configured accounts into SQLite

Add an account repository with:

```text
sync_from_config(config.accounts)
get_by_name(name)
get_id_by_name(name)
list_enabled()
```

At startup, after migrations and before catalog initialization:

1. Upsert every configured account.
2. Update enabled state and weight.
3. Preserve historical account rows that have been removed from current configuration, but mark them disabled.
4. Store only the environment-variable name, never the secret value.
5. Return a stable mapping of account name to database ID.

Catalog persistence and request recording must not use repeated scalar subqueries for account IDs after this mapping exists.

## A5. Repair startup catalog refresh

Replace the no-op startup refresh block with a real awaited refresh.

Startup sequence:

1. Connect database.
2. Apply migrations.
3. Synchronize accounts.
4. Recover stale requests/reservations.
5. Build runtime registry.
6. Load cached catalog.
7. Refresh catalog from enabled accounts when configured.
8. Start background tasks.
9. Report readiness.

If remote refresh fails but a valid cached catalog is available and stale startup is permitted, start degraded and record the reason.

## A6. Tighten readiness

`/v1/readyz` should verify:

- Database connection is writable.
- Account synchronization completed.
- At least one enabled account has a loaded credential.
- A nonempty usable model catalog exists.
- At least one model/account pairing is eligible.
- Essential background tasks are alive.

Return HTTP 503 for degraded readiness rather than an HTTP 200 body containing only `status="degraded"`.

### Workstream A acceptance criteria

- A clean checkout can copy `config.example.toml`, set documented environment variables, migrate, and start.
- Startup immediately discovers models.
- Fresh request inserts cannot fail because account rows are missing.
- Missing local proxy authentication causes startup failure unless explicitly disabled.
- `/v1/readyz` accurately distinguishes usable, degraded, and unusable state.

---

# Workstream B: Canonical persistence model

## B1. Add a new migration; do not rewrite applied migrations

Create a forward migration that aligns the database with the real runtime model. Avoid editing existing migrations in ways that break already-created development databases.

Recommended canonical request schema:

```text
requests
- id TEXT PRIMARY KEY
- account_id INTEGER nullable until selected
- model_id TEXT NOT NULL
- protocol TEXT NOT NULL
- started_at TEXT NOT NULL
- completed_at TEXT
- status TEXT NOT NULL
- status_code INTEGER
- streamed INTEGER NOT NULL
- exactness TEXT NOT NULL
- input_tokens INTEGER
- output_tokens INTEGER
- cache_read_tokens INTEGER
- cache_write_tokens INTEGER
- reasoning_tokens INTEGER
- thinking_characters INTEGER
- cost_microdollars INTEGER
- reserved_microdollars INTEGER NOT NULL
- upstream_latency_ms INTEGER
- first_byte_ms INTEGER
- retry_count INTEGER NOT NULL
- upstream_request_id TEXT
- error_class TEXT
- error_detail TEXT
```

Use text UUIDs consistently for externally visible request IDs.

Recommended reservation schema:

```text
reservations
- id TEXT PRIMARY KEY
- request_id TEXT NOT NULL UNIQUE
- account_id INTEGER NOT NULL
- model_id TEXT NOT NULL
- estimated_tokens INTEGER NOT NULL
- estimated_microdollars INTEGER NOT NULL
- created_at TEXT NOT NULL
- expires_at TEXT NOT NULL
- released_at TEXT
- release_reason TEXT
- status TEXT NOT NULL
```

Recommended attempt schema:

```text
request_attempts
- id INTEGER PRIMARY KEY
- request_id TEXT NOT NULL
- attempt_number INTEGER NOT NULL
- account_id INTEGER NOT NULL
- started_at TEXT NOT NULL
- completed_at TEXT
- status_code INTEGER
- bytes_emitted INTEGER NOT NULL DEFAULT 0
- upstream_request_id TEXT
- error_class TEXT
- error_detail TEXT
```

## B2. Introduce repositories

Add explicit repository classes or functions for:

- Accounts
- Requests
- Request attempts
- Reservations
- Usage windows
- Model prices
- Account events

Endpoint and coordinator code should not contain large inline SQL blocks.

## B3. Insert requests before upstream execution

Lifecycle:

1. Insert a pending request row with real `started_at`.
2. Assign account ID and reserved amount after selection.
3. Insert each upstream attempt before opening it.
4. Update attempt on response status or failure.
5. Finalize request after stream/non-stream completion.

`started_at` and `completed_at` must represent the true interval rather than two identical database timestamps inserted after completion.

## B4. Transaction boundaries

Use a process-local `asyncio.Lock` for atomic selection and reservation, plus one SQLite transaction covering:

1. Read current persisted usage snapshot or use a coherent cached snapshot.
2. Select account.
3. Insert reservation.
4. Update request with account and reserved amount.
5. Increment runtime in-flight state.

Do not hold the lock or database transaction during network I/O.

## B5. Crash recovery

On startup:

- Mark pending requests older than the configured recovery threshold as interrupted.
- Release active reservations belonging to interrupted requests.
- Mark attempts without completion as process-interrupted.
- Rebuild runtime active/reserved counters from valid active records only.
- Record an account/application event describing recovery actions.

### Workstream B acceptance criteria

- Request, attempt, and reservation IDs use compatible types.
- Reservation persistence and loading operate against the actual schema.
- The service can restart with abandoned in-flight rows and recover deterministically.
- No successful upstream response is lost because telemetry insertion fails late.
- Repository methods, not HTTP handlers, own SQL details.

---

# Workstream C: Persisted quota windows and scoring

## C1. Replace hourly/daily approximations

Implement the actual proxy-observed rolling windows:

```text
five-hour: started_at >= now - 5 hours
weekly:    started_at >= now - 7 days
monthly:   started_at >= now - 30 days
```

Use completed requests with known or estimated cost. Define treatment for interrupted requests explicitly:

- If usage is captured, include captured/derived cost.
- If usage is unavailable, include the reservation estimate or a conservative terminal estimate.
- Do not drop failed billable attempts from accounting automatically.

## C2. Source of truth

SQLite is the durable source of truth.

Use one of these approaches:

- Query indexed aggregates at selection time with a short-lived cache; or
- Hydrate explicit five-hour/seven-day/thirty-day runtime windows from SQLite and update them after each request.

For personal-use concurrency, indexed aggregate queries plus a 1–5 second cache are likely simplest and sufficiently fast.

## C3. Apply configured capacities and offsets

For account weight `w`:

```text
capacity_5h = limits.five_hour_microdollars * w
capacity_7d = limits.weekly_microdollars * w
capacity_30d = limits.monthly_microdollars * w
```

Apply each manual offset only to its corresponding window.

Do not use one generic offset for all windows.

## C4. Include reservations in projected utilization

For each account:

```text
p5 = (used_5h + offset_5h + reserved + estimate) / capacity_5h
pw = (used_7d + offset_7d + reserved + estimate) / capacity_7d
pm = (used_30d + offset_30d + reserved + estimate) / capacity_30d
```

Score:

```text
max(p5, pw, pm)
+ mean_weight * mean(p5, pw, pm)
+ active_request_penalty
+ health_penalty
```

The scorer must consume the configured values rather than hard-coded defaults.

## C5. Cost estimator integration

Estimate request cost using:

1. Account/model EWMA.
2. Global model EWMA.
3. Model-family historical average.
4. Configured model fallback.
5. Global fallback reservation.

Correct the current tier ordering so explicit configured model overrides take precedence over broad model-family fallbacks.

Persist or rebuild EWMA inputs from historical exact/derived requests so restart does not reset the estimator.

## C6. Near-tie selection

When candidate scores are within `near_tie_epsilon`:

- Randomize among near ties.
- Prefer lower active request count.
- Permit deterministic seeded behavior in tests.

### Workstream C acceptance criteria

- Routing decisions differ according to five-hour, seven-day, and thirty-day persisted usage.
- Restart does not reset apparent subscription utilization.
- Manual offsets affect only their intended windows.
- Concurrent requests reserve projected usage before later requests score accounts.
- Account weights scale capacities correctly.

---

# Workstream D: Shared request coordinator

## D1. Introduce protocol-neutral request metadata

Define:

```text
ProxyRequestContext
- request_id
- protocol
- model_id
- streaming
- original_body
- incoming_headers
- started_at
- client metadata safe for telemetry
```

Do not rigidly validate all provider request fields. Parse only enough JSON to identify `model`, `stream`, and safe proxy mutations.

## D2. Coordinator method shape

Suggested API:

```python
async def execute(
    context: ProxyRequestContext,
) -> PreparedProxyResponse:
    ...
```

`PreparedProxyResponse` should represent either:

- Buffered status, headers, body, and final metrics; or
- Open upstream stream, status, headers, observer, and finalizer callback.

The upstream response must be opened and inspected before FastAPI commits downstream status.

## D3. Endpoint responsibilities

`chat_completions.py` and `messages.py` should only:

1. Enforce local authentication.
2. Read bounded request body.
3. Parse model and stream flags.
4. Build protocol context.
5. Call coordinator.
6. Render the prepared response.

Remove duplicate selection, database, usage, and error logic from both endpoints.

## D4. Finalization guarantee

All paths must use `try/finally` or an async context manager so the following occur exactly once:

- Upstream response close.
- Reservation release.
- Active request decrement.
- Attempt finalization.
- Request finalization.
- Health update.
- Usage estimator update.

Idempotent finalization is required because disconnect and exception paths may race.

### Workstream D acceptance criteria

- OpenAI and Anthropic endpoints share one lifecycle implementation.
- Every selected account has a matching reservation and attempt row.
- Every reservation is reconciled exactly once.
- Cancellation, timeout, database error, and upstream error paths finalize state.
- Endpoint files contain no duplicated routing or persistence implementation.

---

# Workstream E: Correct streaming behavior

## E1. Open upstream before downstream response creation

Do not return `StreamingResponse(status_code=200)` before inspecting upstream.

Use HTTPX low-level streaming flow:

```text
request = client.build_request(...)
response = await client.send(request, stream=True)
inspect status and headers
retry if safe
construct downstream response
```

## E2. Preserve upstream status and headers

For the chosen final attempt:

- Use the upstream HTTP status.
- Preserve protocol-relevant headers after hop-by-hop filtering.
- Preserve `content-type`.
- Preserve safe rate-limit and request-ID headers.
- Add proxy request and retry-count headers.
- Remove upstream `content-length` for streaming.

## E3. Forward raw bytes

Replace line-based forwarding with `aiter_bytes()`.

Pipeline:

```text
upstream bytes
├── immediately yield unchanged bytes downstream
└── copy into bounded incremental SSE observer
```

The observer may parse complete frames for usage but must never be required for forwarding.

## E4. Incremental SSE observer

The observer must:

- Support arbitrary chunk boundaries.
- Preserve blank-line delimiters.
- Handle CRLF and LF.
- Ignore unknown events.
- Bound incomplete-frame memory.
- Treat malformed usage events as telemetry errors, not stream corruption.
- Track whether any downstream bytes have been emitted.

## E5. Request usage metadata

For OpenAI-compatible streaming requests, merge:

```json
{
  "stream_options": {
    "include_usage": true
  }
}
```

Preserve existing `stream_options` fields.

Do not mutate Anthropic requests unless required by documented protocol behavior.

## E6. Cancellation

On downstream disconnect or task cancellation:

- Stop reading upstream.
- Close upstream response.
- Finalize request as `client_cancelled`.
- Retain captured usage.
- Reconcile remaining cost conservatively.
- Do not retry.

## E7. Usage semantics

Do not record Anthropic thinking character length as reasoning tokens.

Use:

- Upstream-reported reasoning token values when present.
- A separate optional `thinking_characters` field otherwise.
- `reasoning_tokens = NULL` when unknown.

### Workstream E acceptance criteria

- Upstream 401/402/404/429/5xx streaming responses do not become downstream 200 responses.
- SSE output is byte-for-byte identical to the selected upstream body in contract tests.
- Unknown events and arbitrary chunk boundaries are tolerated.
- OpenAI stream usage is captured when upstream supports it.
- Client disconnect closes upstream promptly and leaves no active reservation.

---

# Workstream F: Usage and pricing reconciliation

## F1. Unified usage result

Use a single result model:

```text
UsageResult
- input_tokens
- output_tokens
- cache_read_tokens
- cache_write_tokens
- reasoning_tokens
- thinking_characters
- direct_cost_microdollars
- exactness
- terminal_usage_seen
```

Exactness values:

```text
exact
- upstream supplied direct billable cost and usage

derived
- upstream supplied tokens and cost was calculated from a known immutable price snapshot

estimated
- historical/request reservation estimate used

unknown
- insufficient data to calculate
```

## F2. Non-streaming extraction

Implement protocol-specific extractors for buffered JSON responses.

Capture:

- Standard input/output token fields.
- Cache-read/cache-write fields.
- Reasoning fields when explicitly provided.
- Upstream request ID.
- Direct cost if ever supplied.

## F3. Price snapshots

Align the price schema with integer microdollar arithmetic.

Recommended fields:

```text
model_id
valid_from
input_per_million_microdollars
output_per_million_microdollars
cache_read_per_million_microdollars
cache_write_per_million_microdollars
source
metadata_json
```

Never rewrite historical snapshots.

## F4. Price source order

Use:

1. Current model metadata when explicit and trustworthy.
2. TOML override.
3. Built-in maintained fallback table.
4. Historical EWMA estimate.
5. Unknown.

Do not claim derived cost is exact.

## F5. Terminal estimation

When terminal usage is absent:

- Use captured partial usage when available.
- Otherwise reconcile with the reservation estimate.
- Mark exactness `estimated`.
- Include estimated terminal cost in quota windows so interrupted streams do not become free from the router's perspective.

### Workstream F acceptance criteria

- Successful billable requests no longer persist zero cost by default.
- Cache token counts reach SQLite and dashboard queries.
- Historical costs remain tied to the price snapshot used at request time.
- Interrupted requests have explicit estimated/unknown accounting semantics.
- Dashboard labels exactness accurately.

---

# Workstream G: Failover and account health integration

## G1. Normalize failure classification

Classify errors into:

```text
authentication
authorization
quota_exhausted
rate_limited
model_unavailable
invalid_request
upstream_server_error
connect_timeout
read_timeout
connection_failure
protocol_error
midstream_failure
client_cancelled
internal_error
```

Only failures attributable to an account or upstream should alter account health.

## G2. Pre-body retry policy

Retry another account only when:

- No downstream body bytes have been emitted.
- Failure class is retryable.
- Retry budget remains.
- Another account supports the model/protocol.
- Request replay policy permits it.

Retryable initial cases:

- Connect failure.
- Connect timeout.
- Selected 5xx.
- 402 quota/balance failure.
- 429 rate limiting.
- Account-specific model-not-found.

Do not retry invalid client requests.

## G3. Account-specific transitions

- 401: mark account `authentication_failed` until reload or explicit successful probe.
- 402: mark account quota-exhausted/cooldown; retain local usage history.
- 429: honor `Retry-After`; otherwise exponential cooldown with jitter.
- Model 404: mark only account/model unavailable and trigger targeted catalog refresh.
- Repeated 5xx/transport failure: open circuit breaker.
- Success: reset appropriate transient failure state.

## G4. Persist attempts

Each upstream account attempt must be recorded, including failed attempts before final success.

This is required because failed attempts may still affect upstream accounting and are necessary for diagnosing routing behavior.

## G5. No midstream replay

Once bytes have been emitted downstream:

- Never switch accounts.
- Finalize as midstream failure if upstream terminates abnormally.
- Preserve captured usage and emitted byte count.

### Workstream G acceptance criteria

- A failed first account can fail over before downstream response commitment.
- Midstream failures never duplicate output.
- Health states affect eligibility in subsequent requests.
- Model-specific failures do not disable unrelated models on the account.
- All attempts appear in persistent observability data.

---

# Workstream H: Runtime reload semantics

## H1. Avoid partial object replacement

Current reload behavior must not replace only `app.state.registry` while catalog and router retain the old registry.

Implement one of the following:

### Preferred: immutable runtime generation

```text
RuntimeGeneration
- config
- account registry
- catalog service/cache
- router/coordinator
- upstream client
- generation ID
```

On SIGHUP:

1. Parse and validate new config.
2. Synchronize account rows.
3. Construct a complete new generation.
4. Refresh or hydrate its catalog.
5. Atomically swap `app.state.runtime`.
6. Keep old generation alive until its in-flight count reaches zero.
7. Close old HTTP client.

### Acceptable temporary alternative

Disable SIGHUP reload and document that configuration changes require service restart.

A disabled, honest feature is preferable to inconsistent live state.

## H2. Reload scope

A valid reload must account for changes to:

- Accounts and keys.
- Weights and offsets.
- Upstream URL and HTTP pool settings.
- Routing configuration.
- Model overrides.
- Catalog refresh settings.
- Authentication key reference.

Dashboard route topology may remain startup-fixed for this phase.

### Workstream H acceptance criteria

- No request can be selected from one registry and authenticated through another.
- Removed accounts stop receiving new traffic after swap.
- In-flight requests complete against their original runtime generation.
- Invalid config leaves the current generation untouched.

---

# Workstream I: Security and middleware enforcement

## I1. Enforce declared settings

Install or remove configuration options for:

- Trusted hosts.
- CORS.
- Proxy-header trust.
- Request body limit.
- Header redaction.

Do not expose declarative settings that have no effect.

## I2. Body limits

Apply configurable request-body limits before reading the full body. Return protocol-compatible 413 responses.

## I3. Error shaping

Create protocol-specific error renderers.

OpenAI-compatible endpoints should return an OpenAI-style error envelope.

Anthropic-compatible endpoints should return an Anthropic-style error envelope.

Stable internal proxy error codes should distinguish:

- Unsupported model.
- No eligible account.
- Catalog unavailable.
- Authentication unavailable.
- Upstream exhausted.
- Internal persistence failure.

Do not expose account names, secret references, SQL details, or raw exceptions.

## I4. Database failure policy

Define fail-closed behavior for accounting-critical database failures.

Before upstream dispatch:

- If pending request/reservation cannot be persisted, do not send paid traffic.

After downstream bytes begin:

- Continue forwarding if possible.
- Log and expose degraded telemetry state internally.
- Attempt bounded finalization without corrupting the stream.

### Workstream I acceptance criteria

- Declared host/CORS/body-limit settings are enforced.
- Missing accounting persistence cannot silently send untracked paid requests.
- Client errors use protocol-compatible envelopes.
- Internal diagnostics do not leak through public responses.

---

# Workstream J: Dashboard and statistics correction

Do not redesign the dashboard. Correct it to use the canonical lifecycle data.

## J1. Account utilization

Display per account:

- Five-hour observed cost and projected utilization.
- Seven-day observed cost and projected utilization.
- Thirty-day observed cost and projected utilization.
- Active reservations.
- Active requests.
- Health state and cooldown.
- Exact/derived/estimated/unknown accounting proportions.

## J2. Imbalance metric

Normalize cost by account capacity/weight before calculating imbalance.

Raw cost coefficient of variation is not meaningful when account capacities differ.

## J3. Request and attempt visibility

Expose sanitized recent operational failures and retries without storing prompt/response content.

## J4. Query consistency

Statistics must derive from the same canonical request fields used by routing, not a parallel in-memory view.

### Workstream J acceptance criteria

- Dashboard totals agree with direct SQLite aggregate queries.
- Utilization indicators match router inputs.
- Failed attempts and failovers are visible.
- Exactness labels prevent estimated values from being presented as authoritative.

---

# Testing plan

## 1. Configuration tests

- `config.example.toml` validates.
- Official upstream default is correct.
- Missing local authentication key fails closed.
- Duplicate accounts and invalid weights fail.
- Reload validation failure preserves current runtime.

## 2. Startup integration tests

- Empty database migration and account synchronization.
- Immediate startup catalog refresh.
- Remote refresh failure with valid cached catalog.
- Empty catalog causes degraded readiness.
- Missing accounts prevent readiness.
- Stale request/reservation recovery.

## 3. Routing tests

Use fixed timestamps and seeded randomization.

- Five-hour usage changes selection.
- Seven-day usage changes selection.
- Thirty-day usage changes selection.
- Offsets apply to correct windows.
- Weights scale capacities.
- Reservations affect concurrent selection.
- Restart hydration preserves routing behavior.
- Near ties randomize without systematic first-account bias.

## 4. Non-streaming contract tests

For both protocols:

- Success preserves status, headers, and unknown body fields.
- Upstream 400/401/402/404/429/500 responses preserve appropriate semantics.
- Usage and cost are persisted.
- Failed first account can retry another account.
- Request/attempt/reservation rows finalize correctly.

## 5. Streaming contract tests

- Raw upstream bytes equal downstream bytes.
- CRLF and LF streams.
- SSE frames split at every possible byte boundary.
- Unknown event types.
- OpenAI terminal usage.
- Anthropic start/delta usage.
- Upstream non-200 before body.
- Midstream failure after body.
- Client cancellation.
- Slow consumer/backpressure.
- No replay after first byte.

## 6. Persistence tests

- Migration from existing schema.
- UUID request/reservation compatibility.
- Price snapshot immutability.
- Finalization idempotency.
- Crash recovery.
- Failed telemetry write before dispatch prevents upstream call.
- Late telemetry write failure does not corrupt an active stream.

## 7. Health and failover tests

- 401 disables account.
- 402 cools/exhausts account.
- 429 honors `Retry-After`.
- Account/model 404 suppresses only that pairing.
- Repeated transport failures open circuit.
- Success closes eligible transient circuit.
- Invalid client request does not affect health.

## 8. Reload tests

- Add account.
- Remove account.
- Change key reference.
- Change upstream base URL.
- Change weights and offsets.
- Reload during active stream.
- Invalid reload leaves previous generation active.

## 9. Soak tests

- 10–20 concurrent long streams.
- Catalog refresh during streaming.
- Repeated client disconnects.
- Repeated systemd restart/recovery cycle.
- 24-hour synthetic request workload.
- Stable memory, file descriptors, and SQLite WAL growth.

---

# Recommended implementation sequence

## Step 1: Repair installability and startup

Implement Workstream A completely.

Do not proceed until a fresh local deployment can start, discover models, expose them, and pass readiness.

## Step 2: Align schema and repositories

Implement Workstream B before connecting reservations or retries.

Create migration tests against both an empty database and a database created by the current migrations.

## Step 3: Build coordinator with non-streaming path

Implement Workstream D for non-streaming requests first.

Connect:

- Pending request creation.
- Persisted usage snapshot.
- Selection and reservation.
- One upstream attempt.
- Usage extraction.
- Cost calculation.
- Finalization.

Do not add failover until this path is reliable.

## Step 4: Replace quota model

Implement Workstream C and verify routing from persisted data.

Remove or retire the current hourly/daily in-memory quota approximation.

## Step 5: Repair streaming

Implement Workstream E using the same coordinator lifecycle.

This step must pass byte-preservation and cancellation tests before moving on.

## Step 6: Add pricing and complete accounting

Implement Workstream F.

Verify that the dashboard and routing both see nonzero, correctly classified costs.

## Step 7: Integrate health and failover

Implement Workstream G only after response commitment boundaries are explicit.

## Step 8: Resolve reload semantics

Implement Workstream H or temporarily remove live reload support.

## Step 9: Enforce security and correct dashboard

Implement Workstreams I and J.

## Step 10: Soak and deployment validation

Run the complete acceptance test on the target Raspberry Pi or a comparable ARM64 Linux host.

---

# File-level change map

Expected major modifications:

```text
config.example.toml
src/go_aggregator/models/config.py
src/go_aggregator/app.py
src/go_aggregator/auth.py
src/go_aggregator/db/schema/0004_integration_hardening.sql
src/go_aggregator/db/repositories.py or db/repositories/*
src/go_aggregator/accounts/registry.py
src/go_aggregator/catalog/service.py
src/go_aggregator/catalog/normalizer.py
src/go_aggregator/quota/estimation.py
src/go_aggregator/quota/scorer.py
src/go_aggregator/quota/reservation.py
src/go_aggregator/routing/router.py
src/go_aggregator/proxy/client.py
src/go_aggregator/proxy/streaming.py
src/go_aggregator/proxy/usage.py
src/go_aggregator/api/chat_completions.py
src/go_aggregator/api/messages.py
src/go_aggregator/api/errors.py
src/go_aggregator/coordinator.py or request/coordinator.py
src/go_aggregator/stats/queries.py
src/go_aggregator/stats/service.py
```

Expected new abstractions:

```text
AccountRepository
RequestRepository
ReservationRepository
AttemptRepository
UsageWindowRepository
PriceSnapshotRepository
RequestCoordinator
ProxyRequestContext
PreparedProxyResponse
RuntimeGeneration
ProtocolErrorRenderer
IncrementalSSEObserver
```

Avoid introducing all abstractions as empty wrappers. Each should own a concrete invariant or transaction boundary.

---

# Invariants to enforce

These invariants should be documented in code and tested directly:

1. No paid upstream request is sent without a persisted pending request and active reservation.
2. Selection and reservation are atomic relative to other local requests.
3. Every reservation is finalized exactly once.
4. Every upstream attempt has a persistent attempt record.
5. Retry is forbidden after downstream body emission.
6. The selected account registry and selected account API key come from the same runtime generation.
7. SQLite is the durable source of quota-window usage.
8. Unknown usage is never represented as exact zero cost.
9. Stream forwarding does not depend on successful telemetry parsing.
10. Data-plane authentication cannot be disabled accidentally by a missing environment variable.
11. Model-specific failure cannot disable unrelated models on the same account.
12. Request and response content are not persisted.

---

# Phase exit acceptance test

Perform the following end-to-end test against mocked upstreams first, then real OpenCode Go subscriptions:

1. Start from an empty database using the checked-in example configuration.
2. Confirm immediate model discovery and healthy readiness.
3. Submit one non-streaming OpenAI-compatible request.
4. Submit one streaming OpenAI-compatible request with terminal usage.
5. Submit one non-streaming Anthropic-compatible request.
6. Submit one streaming Anthropic-compatible request.
7. Verify persisted request, attempt, reservation, token, latency, exactness, and cost data.
8. Bias one account with historical five-hour usage and verify the other is selected.
9. Bias one account only in the weekly window and verify routing changes.
10. Start simultaneous requests and verify reservations prevent herd selection.
11. Return 429 from the first account and verify pre-body failover.
12. Return 404 for one account/model and verify only that pairing is suppressed.
13. Terminate an upstream stream mid-response and verify no replay occurs.
14. Disconnect the client and verify upstream closure and reservation release.
15. Restart the service and verify usage history still drives routing.
16. Reload configuration during an active stream and verify generation consistency.
17. Confirm dashboard totals match direct database aggregates.
18. Search logs and SQLite for prompts, completions, API keys, and authorization headers; none should exist.

---

# Definition of done

Phase 10 is done when the repository no longer merely contains implementations of the planned subsystems, but demonstrably composes them into a correct request lifecycle.

Specifically:

- Fresh deployment works from documented configuration.
- Model discovery occurs at startup.
- Accounts exist in SQLite before dependent writes.
- Requests are persisted before upstream dispatch.
- Routing uses persisted five-hour, seven-day, and thirty-day cost windows.
- Cost reservations affect concurrent selection.
- Both protocols share a coordinator.
- Streaming preserves upstream status, headers, and bytes.
- Failover occurs only before response body commitment.
- Usage and cost are reconciled with explicit exactness.
- Health state influences future eligibility.
- Restart and reload preserve coherent state.
- Dashboard statistics reflect the same data used by routing.
- Integration and contract tests cover the complete vertical path.
- The service can run against paid subscriptions without known silent accounting, routing, or replay failures.
