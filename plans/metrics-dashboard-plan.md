# Metrics Dashboard Implementation Plan

## Purpose

This plan wires the expanded metrics surfaces into EggPool's existing dashboard. The goal is to make provider/account routing behavior, retry behavior, pending/finalizer health, latency phase bottlenecks, and usage-cost exactness visible to an operator without requiring direct SQLite inspection or ad hoc curl calls.

Backend/schema/API work is covered in `plans/metrics-core-api-plan.md`. Runtime/process/SBC-focused observability is covered in `plans/metrics-runtime-ops-plan.md`.

## Current dashboard baseline

The dashboard already has overview, accounts, models, latency, pings, events, timeseries, and bandwidth views. JSON data is refreshed from `/api/stats/*` endpoints. The dashboard is served when `[dashboard].enabled = true`; authentication depends on `[dashboard].public`, but some new operational endpoints should stay auth-gated even in public mode.

Before implementing this plan, inspect:

- `src/eggpool/dashboard/routes.py`
- dashboard templates under `src/eggpool/dashboard/`
- static CSS/JS under `src/eggpool/dashboard/static/`
- current Chart.js usage and refresh code
- any existing navigation/tab structure

Follow the existing rendering style rather than introducing a new frontend framework.

## Dashboard design principles

Keep the dashboard operational, not decorative. Each new panel should answer a concrete question:

- Are retries hiding provider/account instability?
- Why did the router select this account and skip others?
- Are pending requests or active reservations leaking?
- Is latency coming from routing, SQLite, upstream headers, stream duration, or finalization?
- Which models/providers are using estimated rather than exact cost accounting?
- Did a catalog refresh remove models or fail against a provider?
- Is the server process suitable for a Raspberry Pi/SBC deployment?

Keep payloads bounded. Recent trace tables should default to 25-50 rows, not unbounded history. High-cardinality tables should have explicit limits and filters.

Respect public dashboard safety. Recent requests, recent routing decisions, operational events, upstream request IDs, client IPs, process/runtime details, and raw-ish error traces should require auth even when `[dashboard].public = true`. Aggregate cards/charts may follow the existing dashboard auth mode.

## Phase 1: navigation and page layout

Add new dashboard navigation entries or tabs for:

1. `Reliability` — retry, attempt, error taxonomy, and pending/finalizer health.
2. `Routing` — routing decisions, exclusions, score/penalty breakdowns.
3. `Runtime` — process/SBC/server health from `plans/metrics-runtime-ops-plan.md`.
4. Optional: `Traces` — recent requests and recent routing decisions. This can also be a section within Reliability/Routing if the dashboard should stay compact.

Do not overload the current Overview page with everything. Add only critical health cards to Overview, then link to the detailed pages.

### Overview page additions

Add a compact `System Health` row with:

- current pending request count.
- oldest pending age.
- active reservation count.
- stale finalizer cleaned count over 24h.
- finalizer timeout count over 24h.
- retry rate over selected period.
- first-attempt success rate over selected period.
- process count / worker count once runtime endpoint exists.

Use warning styling when:

- pending request count > 0 and oldest pending age exceeds upstream read timeout.
- active reservations exist without matching active requests if that can be computed.
- finalizer timeout count > 0.
- retry rate materially exceeds the recent baseline. In the first pass, use static thresholds rather than baseline learning.
- process count > expected worker count for configured deployment mode.

## Phase 2: Reliability page

### Endpoint dependencies

This page consumes:

- `GET /api/stats/attempts`
- `GET /api/stats/attempt-errors`
- `GET /api/stats/pending-health`
- `GET /api/stats/operational-events`
- existing `GET /api/stats/errors`
- existing `GET /api/events`

### Top cards

Render cards for:

- total requests.
- total attempts.
- average attempts per request.
- retry rate.
- first-attempt success rate.
- retry recovery rate.
- retry exhaustion count.
- finalizer timeout count.
- current pending count.
- oldest pending age.

These cards should update with the dashboard period selector where appropriate. Pending-health is instantaneous plus 24h operational counters, so label it explicitly.

### Charts

Add a stacked bar or grouped bar chart for attempts by provider/account:

- first-attempt successes.
- retry successes.
- retry exhausted/errors.

Add a time series for retry rate and error rate if `/api/stats/timeseries` is extended to include retry counts. If not, defer the time-series chart and use the aggregate table first.

Add a normalized error taxonomy chart once the backend exposes normalized categories. Categories should be operator-facing: auth failure, quota exhausted, rate limited, model unavailable, upstream 5xx, connect timeout, protocol error, client error, midstream failure, cancellation.

### Tables

Add an `Attempt breakdown` table grouped by provider/account/model. Columns:

- provider.
- account.
- model.
- requests.
- attempts.
- avg attempts/request.
- retry rate.
- first-attempt success rate.
- retry recovery rate.
- retry exhausted.
- top error category.

Add an `Attempt errors` table. Columns:

- provider.
- account.
- model.
- status code.
- retry category.
- error class.
- count.
- last seen.

Add a `Pending/finalizer health` table or detail panel. Fields:

- pending count.
- oldest pending age.
- active reservations.
- oldest reservation age.
- active reserved microdollars.
- stale finalizer cleaned count.
- finalizer timeout count.
- crash recovery count.
- interrupted/cancelled/midstream counts.

### Operational events

Add a small recent operational events table. Show only sanitized event type, severity, timestamp, and parsed details fields that are explicitly safe. Avoid dumping arbitrary JSON as raw text unless escaped and visibly marked as diagnostic.

Recommended filters:

- all.
- stale finalizer.
- finalizer timeout.
- crash recovery.
- reservation reconciliation.
- database/checkpoint.

## Phase 3: Routing page

### Endpoint dependencies

This page consumes:

- `GET /api/stats/routing`
- `GET /api/stats/routing-exclusions`
- `GET /api/stats/recent-routing`
- existing `GET /api/stats/accounts`
- existing model stats for filter population

### Top cards

Render cards for:

- total routing decisions.
- average candidate count.
- average excluded count.
- local advisory-deprioritized count.
- upstream suppressive exclusion count.
- no-eligible-account count.
- average decision latency.
- p95 decision latency if backend exposes percentiles.

### Exclusion taxonomy

The most important dashboard element is a chart/table separating advisory from suppressive causes:

Suppressive upstream/operator causes:

- authentication failed.
- quota exhausted backoff.
- rate-limit backoff.
- model unavailable.
- operator disabled.
- account disabled.
- protocol mismatch.

Advisory/local causes:

- high local quota estimate.
- active reservation pressure.
- active in-flight penalty.
- low provider priority.
- health penalty below suppressive threshold.

The dashboard should label these explicitly as `Suppressive` and `Advisory` so operators can verify the intended design: local accounting influences priority, but upstream-observed failures control exclusion.

### Routing decision table

Add a recent routing decisions table with filters for account/provider/model. Columns:

- time.
- request/proxy ID short form.
- model.
- selected provider/account.
- candidate count.
- excluded count.
- selected score.
- top alternative.
- top alternative score.
- local quota mode.
- decision latency.
- dominant exclusion reason.

Add an expandable row detail view that shows parsed `exclusion_summary_json` and `score_components_json`. Keep this escaped/sanitized and never show request body/header data.

### Account routing balance panel

Reuse existing account stats and utilization imbalance. Add:

- selected count by account.
- skipped/excluded count by account.
- retry-after-failure count by account.
- current backoff reason and expiration.

This makes it obvious when one account is being overused or when accounts exist but are mostly suppressed.

## Phase 4: Latency page extension

### Endpoint dependencies

This page consumes:

- existing `GET /api/stats/latency`
- new `GET /api/stats/latency-phases`

### Add latency phase chart

Add one chart for aggregate latency phases over the selected period:

- selection.
- persistence/SQLite.
- upstream headers.
- TTFT.
- stream duration.
- finalizer.
- upstream total.

Avoid stacked bars if values overlap semantically. A grouped bar chart by phase with p50/p95/p99 is clearer.

### Provider/model latency table

Add a table grouped by provider/model with:

- requests.
- avg TTFT.
- p50 TTFT.
- p99 TTFT.
- avg selection latency.
- avg persistence latency.
- avg upstream headers latency.
- avg stream duration.
- avg finalizer latency.
- tokens/sec.

Highlight likely bottleneck phase with a derived `dominant_phase` field if backend provides it; otherwise calculate in JS from averages.

## Phase 5: Cost/cache/reasoning exactness enhancements

### Endpoint dependencies

This work mostly extends existing:

- `GET /api/stats/accounts`
- `GET /api/stats/models`
- `GET /api/stats/summary`

### Account page additions

Add columns to the account table:

- exact cost fraction.
- estimated cost fraction.
- unknown cost fraction.
- cache read tokens.
- cache write tokens.
- reasoning tokens.
- cache read/input ratio.
- cache write/input ratio.
- reasoning/output ratio.
- average cost/request.
- average cost/1K tokens.

Use compact numeric formatting. Do not over-format microdollars until converted to dollars in visible display.

### Model page additions

Add the same fields to the model table. This is especially useful for provider/model combinations where usage metadata is absent or where long-context/cache behavior dominates cost.

### Overview additions

Add a small `Usage Quality` card:

- exact cost percentage.
- estimated cost percentage.
- unknown cost percentage.
- total cache read/write tokens.
- total reasoning tokens.

If exactness degrades, the user should immediately know cost/routing estimates are less trustworthy.

## Phase 6: Recent request trace page or panel

### Endpoint dependencies

This page consumes:

- `GET /api/stats/recent-requests`

### Auth behavior

This page must be hidden or require re-authentication when `[dashboard].public = true`. The implementation may show a disabled/locked card explaining that recent request traces require API-key-authenticated access.

### Table columns

Show:

- started/completed time.
- proxy request ID short form.
- upstream request ID short form.
- protocol.
- provider/account.
- model.
- streamed.
- status/status code.
- error class.
- retry count.
- input/output/cache/reasoning tokens.
- cost and exactness.
- TTFT.
- upstream latency.
- bytes received/emitted.

Do not show prompt content, request body, response body, raw headers, API keys, or unredacted provider error detail.

### Filters

Add filters for:

- provider.
- account.
- model.
- status.
- protocol.
- streamed vs non-streamed.

Keep default limit at 50. Allow 10/25/50/100 if backend caps at 100.

## Phase 7: Runtime page integration

The runtime page depends on `plans/metrics-runtime-ops-plan.md`. Once backend runtime endpoints exist, add:

- process count.
- expected worker count.
- PID/PPID.
- uptime.
- RSS memory.
- open file descriptors if available.
- configured Granian threads.
- active background tasks.
- DB path and WAL/read-only stats connection state.
- in-flight requests.
- cron/daemon/systemd deployment hints if detectable.

This page should be intentionally small. It is for SBC sanity checks and deployment debugging, not deep host monitoring.

## Phase 8: frontend implementation details

### Data fetch structure

Extend the existing dashboard JS fetch layer rather than duplicating fetch logic per page. Add a small helper for authenticated-only endpoints so public dashboard mode can display a locked state instead of repeatedly failing.

Recommended helpers:

- `fetchStats(path, params)` for aggregate dashboard-safe stats.
- `fetchPrivateStats(path, params)` for auth-required trace/runtime/operational endpoints.
- `formatDurationMs(value)`.
- `formatAgeSeconds(value)`.
- `formatTokens(value)`.
- `formatMicrodollars(value)` / `formatDollarsFromMicrodollars(value)`.
- `formatPercent01(value)` and `formatPercent100(value)`, since current endpoints may use both ratio and percent conventions.

### Chart reuse

Do not create a new chart for every metric if a table is clearer. Preferred chart additions:

- Retry outcomes by provider/account.
- Routing exclusion taxonomy split by suppressive/advisory.
- Latency phase p50/p95/p99.
- Cost exactness by provider/model.

Everything else can be cards/tables first.

### Refresh behavior

Use existing dashboard refresh interval. For expensive/recent endpoints, refresh less often if needed:

- aggregate cards/charts: dashboard refresh interval.
- recent request trace: dashboard refresh interval or 15s minimum, whichever is larger.
- runtime process metrics: 10s minimum if page is visible.
- operational events: dashboard refresh interval.

Avoid polling hidden pages if existing dashboard JS can detect active tabs. If not, leave optimization for a later pass.

## Phase 9: tests

Add tests at three levels.

Template/render tests:

- New nav entries exist when dashboard enabled.
- Public dashboard renders locked state for private trace/runtime panels.
- Required data attributes or script variables are present for new pages.

API/JS contract tests:

- Dashboard endpoint handlers can serialize the new response shapes.
- Empty datasets render without JS errors or broken charts.
- Null latency/cost fields are rendered as `—` or `0` consistently.

Smoke tests:

- `scripts/smoke_test.py` should hit the new aggregate endpoints.
- Add optional smoke coverage for auth-gated endpoints when an API key is supplied.

Run:

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
uv run python scripts/smoke_test.py
```

## Implementation order

1. Add backend endpoints from `metrics-core-api-plan.md` first.
2. Add nav/page skeletons with empty-state rendering.
3. Wire Reliability page: attempts, attempt errors, pending health, operational events.
4. Wire Routing page: summary, exclusions, recent decisions.
5. Extend Latency page with phase metrics.
6. Extend Accounts/Models/Overview with exactness/cache/reasoning fields.
7. Add Recent Requests trace page with auth-gated behavior.
8. Add Runtime page after runtime endpoint lands.
9. Add smoke test endpoint coverage.

## Acceptance criteria

The dashboard wiring is complete when:

- Overview shows critical retry and pending/finalizer health without overwhelming the existing summary.
- Reliability page shows attempts, retries, retry recovery, attempt errors, and pending/finalizer status.
- Routing page explains selected/skipped accounts and separates suppressive upstream/operator exclusions from advisory local scoring.
- Latency page decomposes latency phases enough to locate SQLite/router/upstream/stream/finalizer bottlenecks.
- Accounts and Models pages show cost exactness, cache, and reasoning breakdowns.
- Recent request traces are available only behind auth and never expose prompt/body/header/API-key data.
- Runtime page gives a quick SBC sanity check once runtime endpoints exist.
- Empty states, historical rows with null fields, and public-dashboard locked states render cleanly.
