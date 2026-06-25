# Metrics Core/API Implementation Plan

## Purpose

EggPool already persists useful request/account/model statistics and exposes several dashboard JSON endpoints. The next metrics pass should not add generic counters blindly. It should fill the current observability gaps that make provider routing, retry behavior, leaked pending state, and cost-estimation quality hard to diagnose under real multi-account load.

This plan covers database/schema work, coordinator/router instrumentation, query/service additions, and JSON API endpoints. Dashboard-specific wiring is described separately in `plans/metrics-dashboard-plan.md`. Runtime/process metrics are described separately in `plans/metrics-runtime-ops-plan.md`.

## Current baseline

The current metrics surface includes `/api/stats/summary`, `/api/stats/accounts`, `/api/stats/models`, `/api/stats/timeseries`, `/api/stats/errors`, `/api/stats/latency`, `/api/stats/pings`, `/api/stats/ips`, `/api/stats/bandwidth`, and `/api/events`.

The current request rows already contain the important request-terminal facts: account, provider, model, protocol, status, status code, input/output/cache/reasoning/thinking usage, cost, cost exactness, TTFT, upstream latency, retry count, bytes received/emitted, upstream request ID, error class/detail, client IP, and stream mode. The existing attempt/reservation/backoff/event infrastructure is also present, but the public statistics layer does not yet explain attempt chains and routing decisions well enough.

## Design constraints

Keep the data plane lightweight. The metrics pass must not introduce blocking calls or expensive serialization inside the hot request path. Instrumentation should record compact primitive fields, avoid prompt/body persistence, and defer derived aggregation to read-only stats queries.

Keep public dashboard mode safe. Any endpoint that exposes recent request rows, upstream request IDs, client IPs, or internal routing decisions must be auth-gated even if `[dashboard].public = true`, unless explicitly configured otherwise later. Aggregate-only endpoints may continue to follow the existing dashboard auth behavior.

Preserve the upstream-authoritative suppression model. Local cost/quota estimates should be represented as advisory routing signals, while upstream-observed failures/backoffs should be represented as suppressive signals. Metrics should make that distinction visible.

Prefer additive schema migrations. Do not rewrite historical request rows or require operators to reset SQLite state. New tables should tolerate missing historical data.

## Phase 1: attempt and retry metrics

### Goal

Expose whether requests succeed on the first upstream attempt, after retry, or only after exhausting candidates. This is the highest-value missing diagnostic for multi-provider/account routing.

### Schema inspection and likely work

First inspect the current schema files under `src/eggpool/db/schema/` and the `AttemptRepository` implementation. If `request_attempts` already stores request ID, account ID, status code, error class, error detail, upstream request ID, bytes emitted, started/completed timestamps, and attempt number, prefer querying it directly.

If attempt rows do not include enough data, add a migration extending `request_attempts` with only the missing fields. Candidate columns:

```sql
ALTER TABLE request_attempts ADD COLUMN provider_id TEXT;
ALTER TABLE request_attempts ADD COLUMN model_id TEXT;
ALTER TABLE request_attempts ADD COLUMN protocol TEXT;
ALTER TABLE request_attempts ADD COLUMN status TEXT;
ALTER TABLE request_attempts ADD COLUMN retry_category TEXT;
ALTER TABLE request_attempts ADD COLUMN upstream_latency_ms INTEGER;
ALTER TABLE request_attempts ADD COLUMN first_byte_ms INTEGER;
```

Only add columns that are actually absent. Keep nullable defaults so historical rows remain valid.

### Instrumentation

In `RequestCoordinator._select_and_persist_attempt`, make sure every attempt row records attempt number, account, provider, model, and protocol before dispatch. In retryable pre-body failures, update the attempt row with status code, error class, retry category, and completion time before moving to the next account. In non-retryable pass-through responses, record the attempt as terminal but non-retryable. In successful requests, record the final successful attempt with latency fields and upstream request ID where available.

Use the existing `RetryClassifier` result to store a normalized `retry_category`. Avoid deriving all categories later from exception names.

### Query layer

Add these functions to `src/eggpool/stats/queries.py`:

`fetch_attempt_stats(db, start, end, account_id=None, model_id=None, provider_id=None)` should return per provider/account/model:

- `request_count`: distinct proxy or DB request count.
- `attempt_count`: total attempts.
- `retry_attempt_count`: attempts with attempt number greater than 1.
- `first_attempt_success_count`.
- `retry_success_count`.
- `retry_exhausted_count`: requests with more than one attempt whose final request status is error.
- `avg_attempts_per_request`.
- `max_attempts_for_request`.
- `retry_rate`: requests with retries / total requests.
- `first_attempt_success_rate`.
- `retry_recovery_rate`: retried requests that eventually completed / retried requests.
- `status_code` and `error_class` breakdowns, preferably nested in a companion endpoint or separate function to keep payload size controlled.

`fetch_attempt_error_breakdown(db, start, end, limit=50, provider_id=None, account_id=None, model_id=None)` should group by provider/account/model/status_code/error_class/retry_category.

The query should join `request_attempts` to `requests` and `accounts` so historical provider/model attribution remains consistent with request rows if attempt rows are sparse.

### Service layer

Add `StatsService.get_attempt_stats()` and `StatsService.get_attempt_errors()`. These should accept the same period, account, provider, and model filters used elsewhere. Keep default period `24h`.

For dashboard calls, support `use_cache=True` with the existing 30-second dashboard cache. Do not cache auth-gated recent traces by default unless the cache key includes all filters.

### API endpoints

Add routes in `src/eggpool/api/stats.py`:

- `GET /api/stats/attempts?period=24h&account=&provider=&model=`
- `GET /api/stats/attempt-errors?period=24h&limit=50&account=&provider=&model=`

Return explicit filters in the JSON response. Example shape:

```json
{
  "period": "24h",
  "account_filter": null,
  "provider_filter": null,
  "model_filter": null,
  "summary": {
    "request_count": 120,
    "attempt_count": 143,
    "retried_request_count": 18,
    "retry_rate": 0.15,
    "first_attempt_success_rate": 0.84,
    "retry_recovery_rate": 0.72
  },
  "groups": [...]
}
```

## Phase 2: routing-decision observability

### Goal

Make it possible to answer why an account was selected or skipped for a request, especially under upstream-authoritative suppression. This should distinguish real upstream backoffs from local advisory quota/cost scoring.

### Schema

Add a bounded event-style table. Suggested migration:

```sql
CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER,
    proxy_request_id TEXT,
    decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    selected_account_id INTEGER,
    selected_account_name TEXT,
    selected_score REAL,
    top_alternative_account_name TEXT,
    top_alternative_score REAL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    local_quota_mode TEXT,
    decision_latency_ms INTEGER,
    active_inflight_count INTEGER,
    health_penalty REAL,
    quota_penalty REAL,
    reservation_penalty REAL,
    exclusion_summary_json TEXT NOT NULL DEFAULT '{}',
    score_components_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(request_id) REFERENCES requests(id),
    FOREIGN KEY(selected_account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_routing_decisions_decided_at
    ON routing_decisions(decided_at);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_request
    ON routing_decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_model_provider
    ON routing_decisions(model_id, provider_id, decided_at);
```

Use JSON strings rather than many unstable columns for exclusion details. Keep the structured columns only for stable first-class dimensions.

### Router instrumentation

Add a small data object in the routing layer, for example `RoutingDecisionTrace`, containing candidate summaries and exclusion reasons. The router should produce it in the same call path that selects an account. The request coordinator should persist it after the request row exists so `request_id` can be populated.

The trace should capture:

- requested model.
- collapsed/resolved provider/model if relevant.
- candidate count.
- selected account/provider.
- score and top alternative score.
- per-account exclusion reasons. Normalize reasons into bounded strings: `account_disabled`, `operator_disabled`, `auth_failed`, `quota_backoff`, `rate_limit_backoff`, `model_unavailable`, `catalog_stale`, `protocol_mismatch`, `local_quota_advisory_deprioritized`, `no_capacity`, `already_attempted`, `unknown`.
- selected score components if available: utilization, active request penalty, health penalty, quota/reservation penalty, priority/weight adjustment.
- decision latency measured with `time.monotonic()`.

Do not persist API keys, upstream URLs with embedded secrets, request bodies, prompt text, headers, or raw exception details.

### Query/service/API

Add query functions:

- `fetch_routing_summary(db, start, end, account_id=None, provider_id=None, model_id=None)`.
- `fetch_routing_exclusions(db, start, end, limit=50, provider_id=None, model_id=None)`.
- `fetch_recent_routing_decisions(db, limit=50, account_id=None, provider_id=None, model_id=None)`.

Add service methods:

- `StatsService.get_routing_summary()`.
- `StatsService.get_routing_exclusions()`.
- `StatsService.get_recent_routing_decisions()`.

Add endpoints:

- `GET /api/stats/routing?period=24h&account=&provider=&model=`
- `GET /api/stats/routing-exclusions?period=24h&limit=50&provider=&model=`
- `GET /api/stats/recent-routing?limit=50&account=&provider=&model=`

`recent-routing` should be auth-gated regardless of public dashboard mode, because it can expose account-level operational detail.

## Phase 3: pending/stale/finalizer health metrics

### Goal

Make the previous pending-request leak and 503-saturation failure mode visible before it becomes an outage.

### Schema

Add a compact operational events table only if existing `account_events` is too account-scoped. Suggested table:

```sql
CREATE TABLE IF NOT EXISTS operational_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_operational_events_type_time
    ON operational_events(event_type, created_at);
```

Candidate event types:

- `stale_finalizer_sweep`.
- `stale_finalizer_cleaned`.
- `stale_finalizer_error`.
- `finalizer_timeout`.
- `crash_recovery`.
- `reservation_reconciliation`.
- `db_checkpoint`.
- `retention_cleanup`.

If this table feels too broad, implement `stale_finalizer_runs` instead. The more general table will likely be useful for future runtime/server health work.

### Instrumentation

In `_crash_recovery`, persist an operational event with counts of stale requests, stale reservations, affected account count, and timestamp.

In `_finalize_stale_requests_once`, persist an event for each sweep that cleans at least one row. Optionally persist zero-clean sweeps only in memory, not SQLite, to avoid unnecessary write amplification.

In the streaming cancellation finalizer timeout branch, persist a `finalizer_timeout` event with proxy request ID, DB request ID, account, model, first_byte_seen flag, bytes emitted, and elapsed milliseconds. Do not store content.

In `reconcile_expired_reservations`, persist counts only when it changes rows.

### Query/service/API

Add a `fetch_pending_health(db)` query that returns:

- current pending request count.
- oldest pending age seconds.
- pending requests older than upstream read timeout if config is available.
- active reservation count.
- active reservation total microdollars.
- oldest active reservation age seconds.
- interrupted request count over last 24h.
- cancelled request count over last 24h.
- midstream error count over last 24h.
- stale-finalizer cleaned count over last 24h.
- finalizer timeout count over last 24h.
- crash recovery count over last 24h.

Add `StatsService.get_pending_health()` and `GET /api/stats/pending-health`.

Add `GET /api/stats/operational-events?type=&limit=50` for the new operational events table. Auth-gate it regardless of public dashboard mode.

## Phase 4: latency phase metrics

### Goal

Split total latency into phases so slowdowns can be assigned to routing/SQLite/upstream/provider/stream/finalization instead of only `upstream_latency_ms` and TTFT.

### Schema

Add nullable columns to `requests` or `request_attempts`. Prefer `request_attempts` for attempt-scoped fields and `requests` for final stream duration. Candidate columns:

```sql
ALTER TABLE request_attempts ADD COLUMN selection_latency_ms INTEGER;
ALTER TABLE request_attempts ADD COLUMN persistence_latency_ms INTEGER;
ALTER TABLE request_attempts ADD COLUMN upstream_headers_ms INTEGER;
ALTER TABLE request_attempts ADD COLUMN upstream_total_ms INTEGER;
ALTER TABLE request_attempts ADD COLUMN finalizer_latency_ms INTEGER;

ALTER TABLE requests ADD COLUMN stream_duration_ms INTEGER;
ALTER TABLE requests ADD COLUMN finalizer_latency_ms INTEGER;
```

Use only monotonic deltas. Keep everything integer milliseconds.

### Instrumentation

In request coordinator:

- measure selection start/end around router selection.
- measure DB transaction/persistence latency for request/reservation/attempt creation.
- measure upstream send-to-headers latency for non-streaming and streaming.
- use existing first-byte timing for TTFT.
- measure finalizer duration inside `RequestFinalizer.finalize`, ideally with a value returned to caller or persisted inside finalizer after the update.
- measure stream duration from response handoff to stream completion/cancellation.

Avoid nested timing complexity in the first pass. Approximate phase metrics are still useful if names are explicit.

### Query/service/API

Extend `/api/stats/latency` or add `/api/stats/latency-phases`. Prefer adding a new endpoint first to avoid breaking existing dashboard consumers.

Return aggregate p50/p95/p99/avg for:

- selection latency.
- persistence latency.
- upstream headers latency.
- TTFT.
- upstream total latency.
- stream duration.
- finalizer latency.

Group by provider and model for the high-cardinality view. Keep the default top-N limited by request count.

## Phase 5: richer cost/cache/reasoning exactness metrics

### Goal

Show which accounts/providers/models have exact provider-reported usage versus locally estimated cost, which providers omit usage, and which models create high cache/reasoning overhead.

### Query/service changes

Extend account/model stats or add companion endpoints to expose:

- `cache_read_tokens`.
- `cache_write_tokens`.
- `reasoning_tokens`.
- `thinking_characters`.
- `exact_count`, `derived_count`, `estimated_count`, `unknown_count`.
- `estimated_cost_fraction`.
- `unknown_cost_fraction`.
- `cache_read_ratio = cache_read_tokens / input_tokens`.
- `cache_write_ratio = cache_write_tokens / input_tokens`.
- `reasoning_output_ratio = reasoning_tokens / output_tokens`.
- `avg_cost_per_request`.
- `avg_cost_per_1k_tokens` where denominator is non-zero.

Add these fields to `fetch_account_stats` and `fetch_model_stats` rather than making additional per-row queries. Existing summary already computes global exactness and cache/reasoning totals, so this is mostly SQL extension work.

### API compatibility

Adding fields to existing JSON response objects is acceptable. Avoid changing existing field names.

## Phase 6: recent request trace

### Goal

Provide a safe, bounded, auth-gated debugging view for the last N requests without storing prompts or bodies.

### Query/service/API

Add `fetch_recent_requests(db, limit=50, account_id=None, provider_id=None, model_id=None, status=None)` and `StatsService.get_recent_requests()`.

Add `GET /api/stats/recent-requests?limit=50&account=&provider=&model=&status=`. This endpoint must require auth even if the dashboard is public.

Return only metadata:

- request row ID.
- proxy request ID.
- upstream request ID.
- started/completed timestamps.
- account/provider/model/protocol.
- status/status code.
- error class, but not error detail unless the existing security config explicitly permits it.
- input/output/cache/reasoning tokens.
- cost microdollars and exactness.
- first byte/upstream latency.
- retry count.
- bytes received/emitted.
- streamed flag.
- client IP only if existing dashboard IP stats are enabled; otherwise omit or hash later.

## Phase 7: tests

Add unit and integration tests for each new query and endpoint.

Recommended tests:

- attempt stats with one first-attempt success, one retry success, one retry exhaustion.
- routing decision persistence with selected account and excluded candidates.
- routing decision redaction: no API key/header/body fields are persisted.
- pending health with active pending row, active reservation, stale finalizer event, and finalizer timeout event.
- latency phase aggregation with null-safe historical rows.
- cost exactness ratios with zero-token denominators.
- recent request endpoint auth behavior when dashboard public mode is enabled.
- migration compatibility against an empty DB and against a DB with preexisting request rows.

Run:

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Also run the deployment smoke test against a live local server after dashboard wiring:

```bash
uv run python scripts/smoke_test.py
```

## Implementation order

1. Add migrations and repository methods for operational events/routing decisions only.
2. Add attempt stats queries from existing schema before adding new columns.
3. Add API endpoints with JSON-only tests.
4. Add router decision trace persistence.
5. Add pending/finalizer operational events.
6. Add latency phase columns and instrumentation.
7. Extend account/model cost/cache/reasoning exactness fields.
8. Add recent request trace endpoint with strict auth.
9. Wire dashboard panels in the separate dashboard plan.

## Acceptance criteria

The implementation is complete when:

- `/api/stats/attempts` explains first-attempt success, retries, and retry recovery by account/provider/model.
- `/api/stats/routing` explains selected versus skipped accounts and distinguishes upstream suppressive backoffs from local advisory scoring.
- `/api/stats/pending-health` shows pending request and reservation health with stale/finalizer/crash-recovery counters.
- `/api/stats/latency-phases` decomposes latency enough to distinguish routing/DB/upstream/stream/finalizer bottlenecks.
- account/model stats expose cache/reasoning/cost-exactness ratios.
- `/api/stats/recent-requests` provides a bounded metadata-only trace and is never publicly accessible without auth.
- All new endpoints are covered by tests and do not store prompts, request bodies, API keys, raw headers, or unredacted provider error details.
