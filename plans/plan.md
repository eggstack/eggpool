# OpenCode Go Subscription Aggregator

## Detailed Implementation Plan

## 1. Objective

Build a lightweight, LAN-hosted proxy that aggregates any number of OpenCode Go subscriptions behind a single endpoint.

The service will:

- Transparently proxy OpenCode Go model requests.
- Support both OpenAI-compatible and Anthropic-compatible upstream request paths.
- Dynamically discover currently available OpenCode Go models.
- Route requests across subscriptions according to estimated quota utilization rather than raw request count.
- Track request, token, model, latency, error, and estimated-cost statistics in SQLite.
- Expose a read-only dashboard for historical and current usage.
- Run reliably on a Raspberry Pi with Ubuntu using a single-process ASGI deployment.
- Keep prompts and model responses out of persistent storage by default.
- Authenticate model-serving endpoints with a local API key.
- Keep dashboard access unauthenticated by default for trusted-LAN deployment.

This is a personal-use service. The architecture should favor correctness, observability, and operational simplicity over high-throughput horizontal scaling.

---

## 2. Recommended Technology Stack

### Runtime

- Python 3.12 or newer
- `asyncio`
- FastAPI or Starlette
- Uvicorn
- HTTPX `AsyncClient`
- SQLite
- `aiosqlite`
- Pydantic v2
- TOML through Python's built-in `tomllib`
- Jinja2 templates
- Minimal JavaScript or HTMX for dashboard updates
- Alembic is optional; an internal migration runner is preferable for the initial small schema

### Development and quality tooling

- `uv` for dependency and environment management
- Ruff for formatting and linting
- Pyright or mypy for static type checking
- Pytest
- `pytest-asyncio`
- `respx` for HTTPX upstream mocking
- Coverage.py
- Pre-commit hooks
- GitHub Actions or equivalent CI

### Deployment

- Uvicorn with one worker
- systemd service
- SQLite WAL mode
- Application data under `/var/lib/opencode-go-aggregator`
- Configuration under `/etc/opencode-go-aggregator/config.toml`
- Secrets supplied through an environment file readable only by the service user
- Optional `ufw` rule limiting access to the local subnet

Flask should not be used for the primary implementation. Streaming proxy behavior, cancellation propagation, persistent asynchronous HTTP connections, and concurrent in-flight accounting are materially cleaner with ASGI.

---

## 3. High-Level Architecture

```text
OpenCode / Codegg
        |
        | local bearer token
        v
+--------------------------------------+
| OpenCode Go Aggregator               |
|                                      |
|  API layer                           |
|  - /v1/models                        |
|  - /v1/chat/completions              |
|  - /v1/messages                      |
|                                      |
|  Request coordinator                 |
|  - validation                        |
|  - model/protocol resolution         |
|  - account eligibility               |
|  - routing                           |
|  - reservation lifecycle             |
|  - retry/failover                     |
|                                      |
|  Upstream client                     |
|  - per-account auth                  |
|  - OpenAI-compatible pass-through    |
|  - Anthropic-compatible pass-through |
|  - streaming relay                   |
|                                      |
|  Usage and health tracking           |
|  - SQLite                            |
|  - in-memory account state           |
|  - rolling utilization               |
|  - circuit breakers                  |
|                                      |
|  Dashboard                           |
|  - summaries                         |
|  - account utilization               |
|  - model statistics                  |
|  - time series                       |
+--------------------------------------+
        |
        | selected OpenCode Go key
        v
OpenCode Go API
```

The system should preserve upstream protocol semantics rather than translating all requests into a single canonical protocol during the initial implementation.

---

## 4. Repository Layout

```text
opencode-go-aggregator/
├── pyproject.toml
├── README.md
├── LICENSE
├── config.example.toml
├── .env.example
├── .gitignore
├── scripts/
│   ├── install-systemd.sh
│   └── initialize-db.sh
├── packaging/
│   ├── opencode-go-aggregator.service
│   └── opencode-go-aggregator.tmpfiles
├── src/
│   └── go_aggregator/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── cli.py
│       ├── config.py
│       ├── constants.py
│       ├── errors.py
│       ├── logging.py
│       ├── auth.py
│       ├── models/
│       │   ├── config.py
│       │   ├── domain.py
│       │   ├── api.py
│       │   └── database.py
│       ├── db/
│       │   ├── connection.py
│       │   ├── migrations.py
│       │   ├── repositories.py
│       │   └── schema/
│       │       ├── 0001_initial.sql
│       │       └── 0002_indexes.sql
│       ├── accounts/
│       │   ├── registry.py
│       │   ├── health.py
│       │   └── state.py
│       ├── catalog/
│       │   ├── fetcher.py
│       │   ├── normalizer.py
│       │   ├── cache.py
│       │   └── service.py
│       ├── routing/
│       │   ├── eligibility.py
│       │   ├── estimator.py
│       │   ├── scorer.py
│       │   ├── reservations.py
│       │   └── router.py
│       ├── proxy/
│       │   ├── client.py
│       │   ├── headers.py
│       │   ├── openai.py
│       │   ├── anthropic.py
│       │   ├── streaming.py
│       │   ├── usage.py
│       │   └── retries.py
│       ├── stats/
│       │   ├── queries.py
│       │   ├── service.py
│       │   └── retention.py
│       ├── api/
│       │   ├── health.py
│       │   ├── models.py
│       │   ├── chat_completions.py
│       │   ├── messages.py
│       │   └── stats.py
│       ├── dashboard/
│       │   ├── routes.py
│       │   ├── templates/
│       │   │   ├── base.html
│       │   │   ├── overview.html
│       │   │   ├── accounts.html
│       │   │   ├── models.html
│       │   │   └── events.html
│       │   └── static/
│       │       ├── dashboard.js
│       │       └── dashboard.css
│       └── background/
│           ├── catalog_refresh.py
│           ├── cleanup.py
│           └── supervisor.py
└── tests/
    ├── unit/
    ├── integration/
    ├── contract/
    ├── fixtures/
    └── conftest.py
```

The package boundaries should remain explicit. In particular, request proxying, routing, accounting, and dashboard concerns should not be combined in endpoint handlers.

---

## 5. Configuration Model

Use a single TOML file for non-secret configuration. API keys should normally be referenced through environment variables.

Example:

```toml
[server]
host = "192.168.1.40"
port = 8080
api_key_env = "GO_AGGREGATOR_API_KEY"
log_level = "INFO"
access_log = true

[upstream]
base_url = "https://opencode.ai/zen/go/v1"
connect_timeout_seconds = 10
read_timeout_seconds = 900
write_timeout_seconds = 30
pool_timeout_seconds = 10
max_connections = 20
max_keepalive_connections = 10
keepalive_expiry_seconds = 30

[database]
path = "/var/lib/opencode-go-aggregator/usage.sqlite3"
busy_timeout_ms = 5000
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_seconds = 900
startup_refresh = true
expose_mode = "union"
stale_after_seconds = 3600
allow_stale_catalog = true

[routing]
strategy = "quota_fair"
near_tie_epsilon = 0.01
max_retries_before_stream = 2
unknown_request_reservation_microdollars = 20000
inflight_penalty = 0.01
health_penalty = 10.0
randomize_near_ties = true

[limits]
five_hour_microdollars = 12000000
weekly_microdollars = 30000000
monthly_microdollars = 60000000

[dashboard]
enabled = true
public = true
retain_request_stats_days = 365
store_request_content = false
refresh_interval_seconds = 15

[security]
allowed_hosts = ["192.168.1.40", "localhost"]
cors_origins = []
trust_proxy_headers = false
redact_headers = ["authorization", "x-api-key"]

[[accounts]]
name = "go-1"
api_key_env = "OPENCODE_GO_KEY_1"
enabled = true
weight = 1.0

[[accounts]]
name = "go-2"
api_key_env = "OPENCODE_GO_KEY_2"
enabled = true
weight = 1.0

[model_overrides."minimax-m3"]
protocol = "anthropic"

[model_overrides."kimi-k2.7"]
protocol = "openai"
```

### Configuration requirements

- Reject duplicate account names.
- Reject missing environment variables for enabled accounts.
- Reject zero or negative account weights.
- Reject unsupported `expose_mode` and routing strategies.
- Validate database and bind paths before the server begins accepting traffic.
- Never print API key values.
- Provide a `check-config` CLI command.
- Support `SIGHUP` for atomic configuration reload.
- On reload, retain in-flight requests on the old account objects while new requests use the new registry.
- Do not file-watch the TOML initially.

---

## 6. Domain Model

Define strongly typed internal domain objects.

### Account

```text
Account
- id
- name
- enabled
- weight
- secret reference
- created_at
```

### Account runtime state

```text
AccountRuntimeState
- health_state
- cooldown_until
- consecutive_failures
- last_success_at
- last_failure_at
- active_request_count
- reserved_microdollars
- model availability map
```

### Model descriptor

```text
ModelDescriptor
- model_id
- display_name
- protocol
- capabilities
- source metadata
- first_seen_at
- last_seen_at
```

### Request accounting record

```text
RequestRecord
- request_id
- account_id
- model_id
- protocol
- start time
- completion time
- status code
- streaming
- usage exactness
- token counts
- estimated or exact cost
- latency
- time to first byte
- retries
- upstream request ID
- terminal error class
```

### Usage exactness

Use an enum:

```text
exact
derived
estimated
unknown
```

- `exact`: upstream explicitly supplied cost and usage.
- `derived`: upstream supplied token usage; cost was calculated from a known price snapshot.
- `estimated`: cost inferred from historical averages or fallback assumptions.
- `unknown`: the request could not be accounted for reliably.

---

## 7. Database Schema

Use explicit SQL migrations.

### `accounts`

```sql
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL,
    weight REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Do not store API keys in SQLite.

### `models`

```sql
CREATE TABLE models (
    model_id TEXT PRIMARY KEY,
    display_name TEXT,
    protocol TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
```

### `account_models`

```sql
CREATE TABLE account_models (
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    available INTEGER NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_error TEXT,
    PRIMARY KEY (account_id, model_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);
```

### `requests`

```sql
CREATE TABLE requests (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    status_code INTEGER,
    streamed INTEGER NOT NULL,
    exactness TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost_microdollars INTEGER,
    reserved_microdollars INTEGER NOT NULL,
    latency_ms INTEGER,
    first_byte_ms INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    upstream_request_id TEXT,
    error_class TEXT,
    error_detail TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
```

Keep `error_detail` sanitized and size-limited.

### `reservations`

```sql
CREATE TABLE reservations (
    request_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    estimated_microdollars INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
```

### `model_price_snapshots`

```sql
CREATE TABLE model_price_snapshots (
    id INTEGER PRIMARY KEY,
    model_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    input_per_million_microdollars INTEGER,
    output_per_million_microdollars INTEGER,
    cache_read_per_million_microdollars INTEGER,
    cache_write_per_million_microdollars INTEGER,
    source TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
```

### `account_events`

```sql
CREATE TABLE account_events (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
```

### Indexes

At minimum:

```sql
CREATE INDEX idx_requests_started_at ON requests(started_at);
CREATE INDEX idx_requests_account_started ON requests(account_id, started_at);
CREATE INDEX idx_requests_model_started ON requests(model_id, started_at);
CREATE INDEX idx_requests_error_started ON requests(error_class, started_at);
CREATE INDEX idx_account_events_account_time ON account_events(account_id, occurred_at);
```

### Database operating mode

On startup:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

Use a serialized write path or a small write lock around multi-statement reservation operations. A single Uvicorn process avoids multi-process coordination problems.

---

## 8. Model Discovery

### Startup behavior

1. Load configured accounts.
2. Open the database and apply migrations.
3. Load the last cached model catalog.
4. Fetch `/models` concurrently for each enabled account.
5. Normalize each response.
6. Update `models` and `account_models`.
7. Build the in-memory model registry.
8. Mark the application ready.

If all startup model fetches fail but a non-stale cached catalog exists and `allow_stale_catalog` is enabled, start in degraded mode. `/readyz` should report degraded status and include a machine-readable reason.

### Periodic behavior

- Refresh every configured interval.
- Add randomized jitter so all account requests do not align exactly.
- Fetch separately per account.
- Record account-specific model visibility.
- Preserve historical model rows when a model disappears.
- Mark unavailable models rather than deleting them.
- Trigger immediate refresh for an account after an upstream model-not-found response.

### Catalog exposure modes

`union`:

- Expose a model if at least one healthy or temporarily cooling account supports it.

`intersection`:

- Expose only models supported by every enabled account.

`healthy_union`:

- Expose only models supported by at least one currently healthy account.

Use `union` as the default. Include availability metadata in the dashboard but do not add nonstandard fields to client-facing `/v1/models` responses unless they are known to be tolerated.

### Protocol resolution

Protocol should be resolved in this order:

1. Explicit TOML model override.
2. Upstream model metadata.
3. Known model-family rule.
4. Last persisted protocol value.
5. Fail closed with a catalog error.

Do not guess protocol at request time when no reliable mapping exists.

---

## 9. API Surface

### Data plane

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/messages
```

These endpoints require the local bearer token.

### Health

```text
GET /healthz
GET /readyz
```

`/healthz` confirms that the process event loop and HTTP server are functioning.

`/readyz` confirms:

- Database is writable.
- At least one enabled account is eligible.
- A model catalog is available.
- Background supervisors are alive.

### Statistics

```text
GET /api/stats/summary
GET /api/stats/accounts
GET /api/stats/models
GET /api/stats/timeseries
GET /api/stats/errors
GET /api/events
```

### Dashboard

```text
GET /
GET /accounts
GET /models
GET /events
```

The dashboard should consume the same statistics service as the JSON API.

---

## 10. Authentication and Header Handling

### Local authentication

Accept:

```http
Authorization: Bearer <local-proxy-key>
```

Return `401` for missing or invalid credentials.

Use constant-time comparison.

### Upstream authentication

Before forwarding:

- Remove the local `Authorization` header.
- Insert the selected account's OpenCode Go credential.
- Remove hop-by-hop headers.
- Recalculate or omit `Content-Length`.
- Preserve client tracing headers only when explicitly permitted.
- Add an internal request ID.
- Do not forward client-provided account-selection headers.

### Hop-by-hop headers to remove

At minimum:

```text
connection
keep-alive
proxy-authenticate
proxy-authorization
te
trailers
transfer-encoding
upgrade
```

### Response handling

Preserve:

- Content type
- Cache-control where relevant
- Upstream request identifiers
- Rate-limit and retry headers when useful
- SSE semantics

Remove or normalize:

- Hop-by-hop headers
- Upstream `Content-Length` when streaming
- Headers exposing upstream implementation details when they interfere with proxy behavior

---

## 11. Routing Eligibility

An account is eligible only when all of the following are true:

- Enabled in configuration.
- Credential loaded successfully.
- Not in authentication-failed state.
- Not in an active circuit-breaker cooldown.
- Not locally considered exhausted for the relevant quota policy.
- Supports the requested model.
- Supports the requested protocol.
- Has not exceeded any configured local concurrency ceiling.

Eligibility must be evaluated from a coherent state snapshot.

If no account is eligible:

- Return `503 Service Unavailable` when the condition is temporary.
- Return `400` or `404` when the requested model is not exposed.
- Return `502` if catalog inconsistency prevents reliable routing.
- Include a stable proxy error code in the JSON response.
- Do not leak account names or key details to ordinary clients.

---

## 12. Usage Windows and Quota Accounting

The initial implementation should use rolling windows unless authoritative reset boundaries become available.

### Five-hour window

Sum observed request cost where:

```text
started_at >= now - 5 hours
```

### Weekly window

Use a rolling seven-day window initially:

```text
started_at >= now - 7 days
```

### Monthly window

Use a rolling 30-day window initially:

```text
started_at >= now - 30 days
```

Document that these are proxy approximations. If OpenCode later exposes authoritative windows, add a pluggable quota-source abstraction.

### Capacity weighting

For account weight `w`:

```text
five_hour_capacity = configured_five_hour_limit * w
weekly_capacity = configured_weekly_limit * w
monthly_capacity = configured_monthly_limit * w
```

### Manual offsets

Support optional offsets per account:

```toml
five_hour_offset_microdollars = 0
weekly_offset_microdollars = 0
monthly_offset_microdollars = 0
```

Offsets represent usage incurred outside the proxy or manual reconciliation.

---

## 13. Request Cost Estimation

The router must reserve projected usage before dispatch.

### Estimation hierarchy

For a requested model:

1. Account/model exponentially weighted moving average.
2. Global model exponentially weighted moving average.
3. Model-family moving average.
4. Configured per-model fallback.
5. Global unknown-request fallback.

### EWMA

Use:

```text
new_estimate = alpha * observed_cost + (1 - alpha) * old_estimate
```

A default `alpha` around `0.2` is reasonable.

Maintain separate estimates for:

- Streaming and non-streaming requests if data supports it.
- Exact/derived observations only by default.
- Estimated observations with a reduced update weight.

### Conservative reservation

Apply a configurable safety factor:

```text
reserved_cost = max(base_estimate * safety_factor, minimum_reservation)
```

A default safety factor around `1.15` avoids chronic under-reservation.

### Unknown-cost requests

When no historical estimate exists, use the global configured fallback. The fallback should be conservative enough to prevent every new model request from targeting the same account.

---

## 14. Routing Score

For account `i`, define projected normalized utilization:

```text
p5_i = (
    observed_5h_i
    + offset_5h_i
    + inflight_reserved_i
    + request_estimate
) / capacity_5h_i
```

```text
pw_i = (
    observed_7d_i
    + offset_week_i
    + inflight_reserved_i
    + request_estimate
) / capacity_week_i
```

```text
pm_i = (
    observed_30d_i
    + offset_month_i
    + inflight_reserved_i
    + request_estimate
) / capacity_month_i
```

Base score:

```text
score_i =
    max(p5_i, pw_i, pm_i)
    + mean_weight * mean(p5_i, pw_i, pm_i)
    + inflight_count_penalty
    + health_penalty
```

Recommended initial values:

```text
mean_weight = 0.15
inflight_count_penalty = active_requests * 0.01
health_penalty = 0 for healthy accounts
health_penalty = large positive value for degraded accounts
```

### Near-tie handling

If several accounts are within `near_tie_epsilon` of the best score:

- Choose randomly among them.
- Weight random choice inversely by active request count.
- Use a deterministic seeded RNG in tests.

This avoids repeatedly favoring the first configured account.

### Atomic reservation

Selection and reservation must occur atomically with respect to other requests.

Within one process, use an `asyncio.Lock` around:

1. Reading current account usage snapshot.
2. Scoring candidates.
3. Selecting an account.
4. Creating the reservation.
5. Incrementing in-memory reserved usage and active request count.

Do not hold this lock during network I/O.

---

## 15. Reservation Lifecycle

### Creation

Before upstream dispatch:

- Generate proxy request ID.
- Calculate estimated cost.
- Select account.
- Insert reservation record.
- Increment in-memory reservation totals.
- Insert the initial request row.

### Success

On completion:

- Parse exact or derived usage.
- Calculate final cost.
- Remove reservation.
- Update request row.
- Decrement account in-flight state.
- Update estimator.
- Mark account success.

### Pre-stream failure

If a retry is permitted:

- Mark the attempt in an internal attempt table or request event.
- Release the first account's reservation.
- Apply health-state consequences.
- Reserve against the replacement account.
- Increment retry count.

The request row should ultimately represent the final selected account, or a separate `request_attempts` table should be introduced. The latter is preferable once failover is implemented.

Recommended additional table:

```sql
CREATE TABLE request_attempts (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status_code INTEGER,
    error_class TEXT,
    upstream_request_id TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(id)
);
```

### Cancellation

If the downstream client disconnects:

- Cancel upstream reading promptly.
- Release reservation.
- Persist any usage captured before cancellation.
- Mark exactness accordingly.
- Record error class `client_cancelled`.
- Do not retry.

### Crash recovery

On startup:

- Find reservations older than a configurable threshold.
- Mark corresponding requests as interrupted.
- Remove stale reservations.
- Record a recovery event.

---

## 16. Transparent Proxy Behavior

### Request body handling

For the first version:

- Read and validate only enough JSON to obtain `model` and `stream`.
- Preserve the original parsed structure.
- Avoid reconstructing request bodies through rigid Pydantic schemas that discard unknown fields.
- Permit provider-specific and future fields.
- Apply only narrowly scoped mutations such as requesting usage information where safe.

### OpenAI-compatible requests

Route:

```text
POST /v1/chat/completions
```

Potential safe mutation:

```json
{
  "stream_options": {
    "include_usage": true
  }
}
```

Only add or merge this when streaming is enabled and the field is supported.

### Anthropic-compatible requests

Route:

```text
POST /v1/messages
```

Preserve:

- `anthropic-version`
- beta headers
- tool definitions
- thinking configuration
- cache-control fields
- SSE event order

### Unknown fields

Unknown request and response fields must pass through unchanged.

The proxy should be tolerant of evolving upstream schemas.

---

## 17. Streaming Implementation

Streaming is the highest-risk implementation area and should be developed early.

### Required properties

- Forward chunks without buffering the complete response.
- Preserve byte order.
- Preserve SSE delimiters.
- Measure time to first byte.
- Detect downstream cancellation.
- Parse a copy of the stream for usage and errors.
- Avoid decoding and re-encoding arbitrary payload bytes.
- Avoid cross-account retry after response bytes have been emitted.

### Streaming pipeline

```text
upstream HTTPX response iterator
        |
        +--> downstream byte stream
        |
        +--> incremental SSE observer
                |
                +--> usage events
                +--> upstream error events
                +--> terminal event
```

### Observer design

The observer must:

- Buffer only incomplete SSE frames.
- Split complete events on blank-line boundaries.
- Parse `event:` and `data:` fields.
- Ignore unknown event types.
- Enforce a maximum frame size.
- Record malformed frames without interrupting transparent forwarding.
- Never block downstream forwarding on database writes.

Usage persistence should occur after stream completion or through a small asynchronous event queue.

### First-byte semantics

Record first-byte latency when the first non-empty body chunk arrives from upstream.

### Stream completion categories

```text
completed
upstream_error_before_body
upstream_error_midstream
client_cancelled
proxy_cancelled
timeout
malformed_terminal_usage
```

---

## 18. Usage Extraction

Implement protocol-specific usage adapters behind a shared interface.

```python
class UsageObserver(Protocol):
    def observe_json_response(self, payload: Mapping[str, Any]) -> None: ...
    def observe_sse_event(self, event_name: str | None, data: bytes) -> None: ...
    def finalize(self) -> UsageResult: ...
```

### Usage result

```text
UsageResult
- input_tokens
- output_tokens
- cache_read_tokens
- cache_write_tokens
- reasoning_tokens
- direct_cost_microdollars
- exactness
- raw usage metadata
```

Do not persist unrestricted raw response objects. Store only bounded, sanitized usage metadata.

### Price calculation

When direct cost is absent:

```text
cost =
    input_tokens * input_rate
    + output_tokens * output_rate
    + cache_read_tokens * cache_read_rate
    + cache_write_tokens * cache_write_rate
```

Store the price snapshot identifier or effective timestamp used for calculation.

Use integer arithmetic with rates normalized per million tokens.

---

## 19. Price Metadata

Model pricing may change independently of model IDs.

### Sources

Preferred order:

1. Pricing metadata returned by the model endpoint.
2. Explicit configured model price overrides.
3. Maintained built-in fallback table.
4. Unknown price.

### Snapshot semantics

When pricing changes:

- Insert a new immutable snapshot.
- Do not rewrite historical request costs.
- Associate each derived request cost with the price effective when the request began.

### Unknown pricing

When token usage exists but price is unknown:

- Record tokens exactly.
- Mark cost as estimated or unknown.
- Use historical model request cost for routing.
- Display the accounting limitation in the dashboard.

---

## 20. Error Classification and Health State

Define a normalized internal error taxonomy.

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

### Health transitions

#### Authentication failure

- State: `authentication_failed`
- Remove account from eligibility.
- Require config reload or successful explicit probe to restore.

#### Quota or balance exhaustion

- State: `quota_exhausted`
- Apply cooldown.
- Retry another account before streaming.
- Recheck after cooldown.

#### Rate limit

- State: `cooldown`
- Honor `Retry-After`.
- Otherwise apply exponential backoff with jitter.

#### Server or transport failure

- Increment consecutive failures.
- Open circuit after configurable threshold.
- Short cooldown.
- Reset consecutive failures after success.

#### Model unavailable

- Mark only the account/model relation unavailable.
- Trigger targeted model refresh.
- Do not disable the entire account.

### Circuit breaker

Initial settings:

```text
failure threshold: 3 consecutive eligible failures
base cooldown: 30 seconds
maximum cooldown: 10 minutes
success reset: immediate
```

Invalid client requests must not count against account health.

---

## 21. Retry and Failover Rules

Retry only when all conditions are true:

- No downstream response body bytes have been emitted.
- Failure is classified as retryable.
- Another eligible account supports the model.
- Retry budget is not exhausted.
- Request is safe to replay under the proxy's policy.

For ordinary LLM completion requests, replay before response emission is generally acceptable, but the proxy should still expose a configuration option:

```toml
retry_non_idempotent_before_stream = true
```

Do not retry:

- Client-side validation errors.
- Authentication failure caused by the local proxy key.
- Unsupported model or protocol when no alternative exists.
- Midstream failures.
- Client cancellation.
- Requests after any downstream bytes have been sent.

Preserve the final upstream status and error payload when all retry attempts fail, while adding a proxy request ID header.

---

## 22. Dashboard Design

The dashboard should remain operationally useful without becoming a separate application framework.

### Overview page

Show:

- Total requests over selected interval.
- Exact, derived, estimated, and unknown accounting proportions.
- Total observed cost.
- Input, output, cache-read, and cache-write tokens.
- Current active streams.
- Error rate.
- Average and p95 latency.
- Average and p95 time to first byte.
- Account utilization imbalance.

### Accounts page

For every account:

- Health state.
- Current cooldown.
- Active requests.
- Reserved estimated cost.
- Five-hour projected utilization.
- Seven-day projected utilization.
- Thirty-day projected utilization.
- Request count.
- Error count.
- Last success.
- Last failure.
- Models available.

### Models page

For every model:

- Protocol.
- Accounts supporting it.
- Request count.
- Observed cost.
- Average cost per request.
- Token counts.
- Cache utilization.
- Average and p95 latency.
- Average and p95 first-byte latency.
- Error rate.
- Exactness proportions.

### Timeline page or chart section

Allow:

- Hourly aggregation for recent periods.
- Daily aggregation for long periods.
- Account, model, and protocol filters.
- Request, cost, token, and error metrics.

### Events page

Show sanitized operational events:

- Account entered cooldown.
- Authentication failure.
- Model appeared or disappeared.
- Catalog refresh failed.
- Circuit breaker opened or closed.
- Database recovery removed stale reservation.

### Dashboard implementation constraints

- No prompt or completion content.
- No API keys.
- No raw authorization headers.
- No account secret references.
- Bound all free-text fields.
- HTML escape all displayed data.
- Use parameterized SQL.
- Prefer server-rendered pages and JSON polling over a large frontend framework.

---

## 23. Statistics Query Layer

Keep dashboard SQL out of HTTP route functions.

Provide service methods such as:

```text
get_summary(start, end)
get_account_stats(start, end)
get_model_stats(start, end)
get_timeseries(start, end, bucket, filters)
get_error_breakdown(start, end)
get_recent_events(limit)
```

Use SQLite's time and grouping functions carefully. Store timestamps in UTC ISO-8601 form or integer epoch milliseconds consistently.

For percentile calculations, either:

- Compute in Python from bounded result sets initially.
- Add histogram or rollup tables later if volume requires it.

For personal use, direct indexed queries should be sufficient.

---

## 24. Background Tasks

Use a small supervised task manager started during application lifespan.

### Tasks

- Model catalog refresh.
- Stale reservation cleanup.
- Retention cleanup.
- Periodic database checkpoint.
- Optional health probes for disabled-by-cooldown accounts.

### Supervision

If a background task exits unexpectedly:

- Log the exception.
- Record an application event.
- Restart with bounded exponential backoff.
- Mark readiness degraded when the failed task is operationally essential.

Do not silently lose the catalog refresh loop.

---

## 25. Logging and Observability

Use structured logs.

Fields should include:

```text
timestamp
level
event
proxy_request_id
account_id or safe account name
model_id
protocol
status_code
latency_ms
first_byte_ms
retry_count
error_class
```

Never log:

- Local bearer token.
- OpenCode Go credentials.
- Full request bodies.
- Prompt content.
- Completion content.
- Tool arguments.
- Repository content.

Support human-readable logs by default and optional JSON logs.

Add response headers:

```text
x-proxy-request-id
x-proxy-retry-count
```

Do not expose selected account identity unless a debug mode is explicitly enabled.

---

## 26. Security Model

The service is intended for a trusted LAN but should still use reasonable safeguards.

### Required controls

- Bearer authentication on all `/v1/*` routes.
- Constant-time key comparison.
- API keys loaded from environment variables.
- Config and environment files restricted to service user.
- No prompt or completion persistence.
- No permissive CORS by default.
- Host-header allowlist.
- Request body size limit.
- SSE frame size limit.
- Sanitized error storage.
- Secret redaction in logs.
- No dashboard mutation endpoints.
- No remote account management through the dashboard.
- Bind to a specific LAN IP or firewall the port to the LAN subnet.

### Optional later controls

- Dashboard authentication.
- TLS termination through Caddy.
- Per-client local API keys.
- Client-specific quotas.
- mTLS.
- Audit log signing.

These should not delay the MVP.

---

## 27. CLI

Provide:

```text
go-aggregator serve --config /path/to/config.toml
go-aggregator check-config --config /path/to/config.toml
go-aggregator migrate --config /path/to/config.toml
go-aggregator models refresh --config /path/to/config.toml
go-aggregator accounts status --config /path/to/config.toml
go-aggregator db vacuum --config /path/to/config.toml
```

`serve` should be the default command.

The CLI should return nonzero exit codes for invalid configuration, failed migration, and unusable database state.

---

## 28. Implementation Phases

## Phase 0: Repository and tooling foundation

### Deliverables

- Initialize Python package.
- Add `pyproject.toml`.
- Configure `uv`.
- Add Ruff, type checking, pytest, and coverage.
- Add CI.
- Add example config and environment files.
- Add structured logging.
- Add initial README with scope and limitations.

### Acceptance criteria

- `uv sync` succeeds.
- Lint, format, type checking, and tests run in CI.
- Package starts with a placeholder health endpoint.
- Secrets and local database files are ignored by Git.

---

## Phase 1: Configuration, database, and application lifecycle

### Deliverables

- TOML configuration parser.
- Environment secret resolution.
- Validation.
- SQLite connection manager.
- Migration runner.
- WAL configuration.
- FastAPI/Starlette application factory.
- Lifespan-managed HTTPX client.
- `/healthz` and `/readyz`.
- `check-config` and `migrate` CLI commands.

### Acceptance criteria

- Invalid accounts and missing secrets fail startup cleanly.
- Database migrations are idempotent.
- Readiness reflects database availability.
- Shutdown closes HTTP and database resources without warnings.
- No secret appears in logs or exceptions.

---

## Phase 2: Account registry and model discovery

### Deliverables

- Account registry.
- Runtime health state.
- Per-account model fetch.
- Catalog normalization.
- Protocol resolution.
- SQLite catalog persistence.
- Cached startup behavior.
- Periodic refresh task.
- `/v1/models`.

### Acceptance criteria

- Multiple account catalogs are merged correctly.
- Union and intersection modes behave as configured.
- Model disappearance marks availability false without deleting history.
- One failed account does not block successful catalog refreshes.
- Cached catalog supports degraded startup.
- `/v1/models` requires local authentication.

---

## Phase 3: Non-streaming transparent proxy

### Deliverables

- Request authentication.
- Header filtering.
- Model and protocol validation.
- Basic account selection.
- OpenAI-compatible non-streaming proxy.
- Anthropic-compatible non-streaming proxy.
- Response pass-through.
- Initial request ledger.
- Upstream error classification.

### Acceptance criteria

- Unknown request fields are preserved.
- Local authorization is replaced with selected upstream authorization.
- Both upstream protocols work.
- Responses preserve status and relevant headers.
- Invalid local credentials never reach upstream.
- Prompt and response content are not persisted.

---

## Phase 4: Streaming proxy

### Deliverables

- Byte-preserving streaming relay.
- Downstream cancellation propagation.
- First-byte timing.
- SSE observer.
- OpenAI stream usage extraction.
- Anthropic stream usage extraction.
- Midstream error accounting.
- No-retry-after-first-byte enforcement.

### Acceptance criteria

- First chunks are forwarded without whole-response buffering.
- Streaming tool-call events preserve order and content.
- Unknown SSE events pass through unchanged.
- Client disconnect cancels upstream work.
- Midstream failures do not trigger replay.
- Memory use remains bounded for long streams.

This phase should receive focused integration and soak testing before advanced routing work.

---

## Phase 5: Usage extraction and price accounting

### Deliverables

- Shared usage result model.
- Protocol-specific usage adapters.
- Price snapshot storage.
- Derived cost calculation.
- Exactness classification.
- EWMA model cost estimator.
- Dashboard-visible accounting quality.

### Acceptance criteria

- Exact upstream usage is stored when available.
- Derived costs use immutable price snapshots.
- Unknown pricing does not fabricate exact cost.
- Interrupted streams are classified correctly.
- Cost arithmetic uses integers.

---

## Phase 6: Quota-aware routing and reservations

### Deliverables

- Rolling window queries.
- Manual offsets.
- Weighted capacities.
- Cost estimation hierarchy.
- Atomic reservations.
- Quota-fair scorer.
- Near-tie randomization.
- Reservation reconciliation.
- Crash recovery.

### Acceptance criteria

- Concurrent requests do not all select the same apparently idle account.
- Account weights affect normalized capacity.
- Routing favors the least projected-utilized account.
- Reservations are released on success, failure, and cancellation.
- Stale reservations are cleaned after process restart.
- Routing decisions are deterministic under seeded tests.

---

## Phase 7: Retry, failover, and health management

### Deliverables

- Retry classification.
- Pre-stream account failover.
- Circuit breakers.
- Rate-limit cooldowns.
- Authentication disable state.
- Account/model-specific unavailability.
- Request attempt history.
- Targeted catalog refresh.

### Acceptance criteria

- Retry occurs only before response body emission.
- 401 disables only the affected account.
- 404/model failure removes only the account/model pairing.
- 429 honors `Retry-After`.
- Circuit breakers reopen after cooldown.
- Invalid client requests do not degrade account health.
- All attempts are observable.

---

## Phase 8: Statistics API and dashboard

### Deliverables

- Statistics query layer.
- Summary endpoint.
- Account, model, timeline, error, and event endpoints.
- Server-rendered dashboard.
- Period selection and filters.
- Accounting-quality indicators.
- Utilization imbalance metric.

### Acceptance criteria

- Dashboard contains no secrets or content.
- Account utilization reflects observed cost plus reservations and offsets.
- Model and account filters return correct aggregates.
- Time-series queries are indexed and remain responsive.
- HTML output is escaped.
- Dashboard remains usable without JavaScript for basic views.

---

## Phase 9: Deployment hardening

### Deliverables

- systemd unit.
- Dedicated system user.
- Filesystem layout.
- Environment file example.
- Log rotation guidance.
- Backup and restore procedure.
- Firewall guidance.
- Graceful `SIGHUP` reload.
- Graceful shutdown.
- Raspberry Pi installation documentation.

### Acceptance criteria

- Service starts on boot.
- Service restarts after failure.
- Database persists across upgrades.
- Configuration reload does not interrupt in-flight requests.
- Process runs without root privileges.
- Port is reachable only from intended LAN sources.
- Upgrade and rollback steps are documented.

---

## 29. Testing Strategy

## Unit tests

Cover:

- TOML validation.
- Environment secret resolution.
- Protocol resolution.
- Header filtering.
- Model catalog union and intersection.
- Cost arithmetic.
- EWMA updates.
- Quota-window calculations.
- Routing score.
- Tie-breaking.
- Eligibility.
- Error classification.
- Circuit-breaker transitions.
- SSE frame parsing.
- Usage extraction.
- Reservation state transitions.

## Integration tests

Use mocked HTTPX upstreams.

Scenarios:

- Successful OpenAI non-streaming response.
- Successful Anthropic non-streaming response.
- OpenAI stream with terminal usage.
- Anthropic stream with terminal usage.
- Unknown SSE event.
- Split SSE frames across arbitrary chunks.
- Client cancellation.
- Upstream connection timeout.
- 401 account failure.
- 402 quota failure.
- 429 with and without `Retry-After`.
- 404 model-specific failure.
- 500 failover.
- Midstream upstream failure.
- All accounts unavailable.
- Concurrent routing and reservation contention.
- Catalog refresh with divergent account model lists.
- Stale cached startup.

## Contract tests

Capture representative sanitized upstream responses and assert:

- Unknown fields survive proxying.
- Streaming events survive byte-for-byte.
- Status codes and content types are preserved.
- Usage adapters tolerate additional fields.
- Client-visible errors remain protocol-compatible.

## Database tests

- Migration from empty database.
- Repeated migration.
- WAL enabled.
- Concurrent readers during writes.
- Reservation recovery.
- Retention cleanup.
- Price snapshot immutability.
- Aggregate query correctness.

## Load and soak tests

The target is modest, but long-running behavior matters.

Test:

- 10–20 concurrent streams.
- Streams lasting 30–60 minutes.
- Repeated disconnects.
- Catalog refresh during active requests.
- SIGHUP during active requests.
- SQLite writes under streaming load.
- One-week synthetic request history.
- Memory stability.
- File descriptor stability.

## Security tests

- Missing local API key.
- Incorrect local API key.
- Attempted upstream authorization override.
- Oversized request body.
- Oversized SSE frame.
- Host-header mismatch.
- Malformed JSON.
- Malformed upstream SSE.
- Secret redaction in logs.
- HTML injection in model and error metadata.
- SQL injection attempts in dashboard filters.

---

## 30. Initial Acceptance Test

A practical end-to-end MVP test should prove the following:

1. Configure two OpenCode Go subscriptions.
2. Start the proxy on a Raspberry Pi.
3. Point OpenCode at the proxy base URL.
4. Fetch models through `/v1/models`.
5. Run requests against at least one OpenAI-compatible model.
6. Run requests against at least one Anthropic-compatible model.
7. Confirm streaming output arrives incrementally.
8. Confirm requests distribute according to projected utilization.
9. Confirm token and cost data appear in SQLite.
10. Confirm the dashboard shows per-account and per-model statistics.
11. Temporarily invalidate one key and confirm failover.
12. Induce a model-specific failure and confirm only that account/model relation is suppressed.
13. Restart the service and confirm stale reservations are recovered.
14. Verify no prompt, completion, or API key appears in the database or logs.

---

## 31. Deferred Features

Do not include these in the first implementation unless they become necessary:

- Full OpenAI-to-Anthropic or Anthropic-to-OpenAI protocol translation.
- Multi-user authentication.
- Per-client quota enforcement.
- PostgreSQL.
- Multiple Uvicorn workers.
- Distributed coordination.
- Redis.
- Kubernetes.
- Remote dashboard exposure.
- Prompt or response logging.
- Account creation or key management through the dashboard.
- Automatic scraping of OpenCode's web console.
- Complex frontend framework.
- General-purpose provider support.
- Model tokenizer replication.
- Billing-grade accounting claims.

These are deliberate exclusions, not omissions.

---

## 32. Known Limitations

The implementation must document:

- Usage is authoritative only for traffic that passes through the proxy.
- OpenCode may not expose exact subscription reset windows.
- Rolling seven-day and thirty-day windows are approximations.
- Interrupted streams may not contain terminal usage.
- Published prices may not perfectly match upstream subscription accounting.
- Accounts used outside the proxy require manual offsets for accurate balancing.
- Model metadata and protocol behavior can change.
- Aggregating multiple subscriptions may not be an explicitly supported OpenCode deployment pattern.
- LAN-only deployment reduces but does not eliminate security obligations.

---

## 33. Recommended First Development Slice

The first implementation slice should be vertical rather than layer-complete.

Build:

1. Configuration and account loading.
2. SQLite migrations.
3. HTTPX client.
4. Per-account model discovery.
5. Authenticated `/v1/models`.
6. One OpenAI-compatible non-streaming proxy path.
7. Basic round-robin routing.
8. Request ledger.
9. One integration test with mocked upstreams.
10. systemd-independent local launch documentation.

Then immediately add streaming before investing heavily in the dashboard or advanced quota scoring. Streaming semantics are the most likely source of architectural rework, so they should be validated before the accounting and presentation layers become extensive.

---

## 34. Definition of Done

The project is ready for regular personal use when:

- OpenCode and Codegg can use a single configured base URL.
- Both supported upstream protocol families work.
- Model discovery updates without source changes.
- Request streams remain transparent and cancellable.
- Routing uses normalized projected quota utilization.
- In-flight requests reserve estimated usage.
- Pre-stream failures fail over safely.
- Midstream requests are never replayed.
- Account health state is observable and self-recovering where appropriate.
- SQLite history survives restart.
- Dashboard statistics are accurate within documented accounting limitations.
- Secrets and model content are absent from persistent telemetry.
- The service runs under systemd on the Raspberry Pi without root.
- Tests cover routing, streaming, accounting, retries, and recovery.
- Operational limitations are clearly documented.

