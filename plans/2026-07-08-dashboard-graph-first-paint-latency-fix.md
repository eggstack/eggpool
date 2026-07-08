# Dashboard graph first-paint latency fix plan

Date: 2026-07-08

## Context

Dashboard charts such as the overview page's request timeseries and graph surfaces on reliability, timeseries, bandwidth, cache, and related pages can remain blank for several seconds before displaying. The current code path points to a data-first rendering problem more than a Chart.js drawing problem.

The overview route blocks on a broad `asyncio.gather()` of independent stats calls before returning HTML. The request-timeseries data is passed into `render_overview()` as server-rendered inline chart payload, so the browser cannot draw the chart until the full dashboard response arrives. Other graph pages have the same shape: `handle_reliability()` waits for timeseries plus reliability aggregates, `handle_timeseries()` waits for both flat and grouped timeseries, and `handle_bandwidth()` waits for summary, bandwidth, and flat timeseries before rendering. Chart.js can add a cold-load delay, but the shared symptom across graph pages is more consistent with cold stats-cache misses, raw SQLite aggregation scans, and page-level render blocking.

The current stats cache is short-lived: `_DASHBOARD_CACHE_TTL_S = 30.0`, keyed by a 30-second wall-clock bucket for preset windows. On a cache miss, chart routes can pay full aggregation cost again. Rollup paths exist, but `StatsService.get_timeseries()` and `StatsService.get_grouped_timeseries()` fall back to raw `requests` table aggregation when rollups return empty. Raw flat timeseries groups by `strftime(..., r.started_at)`. Raw grouped timeseries joins `accounts`, groups by bucket plus provider/model/account dimensions, and then performs top-N/Other folding in Python. On a large `requests` table, those fallback queries are plausible multi-second bottlenecks.

## Goals

1. Reduce graph first-paint latency on warmed production-like databases.
2. Avoid full raw `requests` scans for common dashboard chart windows unless explicitly required.
3. Keep dashboard behavior correct for fresh installs, empty databases, and partially lagging rollups.
4. Improve perceived responsiveness by decoupling page shell render from slower graph hydration where appropriate.
5. Add instrumentation so future regressions identify the specific slow query or render stage.

## Non-goals

This plan does not change model routing, request accounting semantics, provider behavior, or cost/token formulas. It does not remove server-rendered dashboard pages. It does not add a frontend framework. It should preserve the low-power Raspberry Pi deployment target.

## Phase 1: Add per-surface dashboard latency instrumentation

Add low-overhead timing around each dashboard stats call that can gate graph rendering.

Target files:

- `src/eggpool/dashboard/routes.py`
- `src/eggpool/dashboard/telemetry.py`
- `src/eggpool/runtime_metrics.py`
- `tests/unit/test_dashboard.py` or a new focused dashboard telemetry test module

Implementation details:

- Extend `DashboardTelemetry` with per-operation timings in addition to whole-page render timings. Suggested shape:
  - `record_stage(page: str, stage: str, elapsed_ms: float, cache_hit: bool | None = None)`
  - bounded ring buffer per `(page, stage)` or exponentially-decayed summary counters
  - expose count, avg, p50, p95, p99 if existing telemetry already has percentile helpers; otherwise avg/max/recent is acceptable for the first pass
- In `handle_overview()`, wrap each gathered coroutine with a named timer. Recommended stage names:
  - `disabled_count`
  - `account_stats`
  - `model_stats`
  - `recent_events`
  - `bandwidth_daily`
  - `ping_summary`
  - `ip_stats`
  - `timeseries_flat`
  - `attempt_stats`
  - `operational_summary`
  - `pending_health`
  - `cache_observability`
  - `compression_runtime`
  - `synthetic_cache_summary`
  - `dashboard_overview`
  - `render_html`
- Repeat for graph-heavy routes:
  - `handle_timeseries()`: `timeseries_flat`, `timeseries_grouped`, `collect_account_options`, `collect_model_options`, `render_html`
  - `handle_bandwidth()`: `summary`, `bandwidth_timeseries`, `timeseries_flat`, `collect_account_options`, `render_html`
  - `handle_reliability()`: `attempt_stats`, `retry_distribution`, `pending_health`, `operational_summary`, `recent_operational_events`, `timeseries_flat`, `render_html`
  - `handle_cache()` if charts are rendered there: split each request-shaping aggregate rather than only recording whole-page latency
- Add cache-hit visibility inside `StatsService` if practical. Minimal option: add a `StatsService._dashboard_cache_stats` dict with per-namespace hit/miss counters and expose it via runtime metrics. Better option: return a small internal `CacheLookup` result from `_get_dashboard_cache()` or record the hit/miss there by namespace.
- Add runtime dashboard output for slow dashboard stages. Keep it compact: top 10 recent slow stages, p95 by stage, and cache hit/miss counts by stats namespace.

Validation:

- Unit test that telemetry records stage names when route handlers are invoked with stubbed stats calls.
- Unit test that cache hit/miss counters increment for a representative `StatsService.get_timeseries(..., use_cache=True)` path.
- Manual: open `/runtime` after loading graph pages and confirm slow-stage telemetry is visible.

## Phase 2: Profile and index the SQLite query path

Add a local diagnostic command or test utility that reports `EXPLAIN QUERY PLAN` for the exact dashboard queries against the configured database. Do not rely on intuition about indexes.

Target files:

- `src/eggpool/cli.py` or a new `src/eggpool/cli_stats.py` helper, depending on current CLI layout
- `src/eggpool/stats/queries.py`
- `src/eggpool/db/schema/*.sql`
- `tests/integration/test_database_maintenance.py` or a new migration/index test

Implementation details:

- Add an operator-facing diagnostic command, e.g. `eggpool stats explain-dashboard --period 24h --bucket hour --group-by provider_model`.
- It should print query plans and elapsed timings for:
  - `fetch_timeseries()` flat query
  - `fetch_grouped_timeseries()` provider/model/account variants
  - `fetch_summary()` fallback path
  - `fetch_account_stats()`
  - `fetch_model_stats()`
  - `fetch_bandwidth_timeseries()`
  - rollup queries in `UsageRollupRepository` for flat/grouped chart paths
- Add or verify indexes for the common raw fallback filters:
  - `requests(started_at)`
  - `requests(account_id, started_at)`
  - `requests(model_id, started_at)`
  - `requests(original_model_id, started_at)`
  - `requests(provider_id, started_at)`
  - where cost is acceptable, a covering-ish dashboard index for high-frequency aggregates may include `status`, `input_tokens`, `output_tokens`, `cost_microdollars`, `bytes_received`, `bytes_emitted`, `first_byte_ms`, but avoid over-indexing the hot write path on Raspberry Pi.
- Inspect existing migrations before adding indexes. Only add missing indexes. Use `CREATE INDEX IF NOT EXISTS` in a new numbered migration.
- If grouped timeseries remains slow due to expression grouping, prefer rollup improvements rather than large expression indexes on the raw request table.

Validation:

- Migration test confirms indexes exist after migration.
- `EXPLAIN QUERY PLAN` should show range searches over indexes instead of full scans for raw fallback queries.
- Write overhead must remain acceptable; do not add more indexes than the measured plans justify.

## Phase 3: Make rollups authoritative for common dashboard chart windows

The dashboard should not routinely fall back to raw `requests` scans for normal 24h/7d/30d graph views. Use rollups as the default chart source and restrict raw fallback to small, explicit, or live-tail windows.

Target files:

- `src/eggpool/stats/service.py`
- `src/eggpool/db/rollup_repository.py`
- `src/eggpool/metrics/buffer.py` or the current rollup writer/coalescer code
- tests covering rollup freshness and fallback behavior

Implementation details:

- For `get_timeseries()`:
  - Prefer rollups for `24h`, `7d`, and `30d` when `_rollup_repo` exists.
  - If rollups return historical buckets but may miss the current open bucket, merge rollup rows with a bounded raw live-tail query for only the current bucket or last N minutes. Avoid scanning the whole selected period.
  - If rollups return empty and the selected range is larger than a small threshold, return an empty stable payload plus a diagnostic flag rather than silently scanning raw rows. Suggested threshold: raw fallback allowed for `1h` and custom ranges under 2 hours; otherwise require rollup or bounded live-tail.
- For `get_grouped_timeseries()`:
  - Same rule: rollups first, bounded live-tail merge for current bucket, no full raw fallback for large windows.
  - Reduce the rollup query limit from hardcoded `10000` if measured as costly. The service only needs enough rows to select top-N plus Other per requested grouping. Prefer top-series selection in SQL/rollup repository rather than fetching thousands of rows into Python.
- Add a stable metadata field to timeseries JSON payloads when useful, e.g. `source: rollup|raw|mixed|empty` and `degraded_reason: rollup_empty|rollup_stale|none`. For backward compatibility, keep existing flat `/api/timeseries` as a list unless changing the contract is handled carefully. The grouped endpoint already returns a dict, so it can carry metadata more easily.
- Ensure `usage_rollups` are populated by the metrics coalescer for all fields required by current graph renderers:
  - request count
  - error count
  - input/output tokens
  - cache read/write tokens if grouped charts can select token metrics
  - cost microdollars
  - bytes received/emitted
  - average latency and TTFT inputs
- If rollup freshness is the reason for fallback, fix coalescer cadence and flush behavior rather than using raw scans as the dashboard escape hatch.

Validation:

- Unit tests for `get_timeseries()`:
  - rollup rows returned directly for complete 24h data
  - mixed rollup + live-tail for current bucket
  - raw fallback allowed for short custom windows
  - raw fallback suppressed for 7d/30d when rollups are empty
- Unit tests for `get_grouped_timeseries()` with the same cases.
- Integration test with seeded large `requests` table confirms dashboard chart calls do not execute full-period raw scans for common windows.

## Phase 4: Extend dashboard cache semantics by query class

The fixed 30-second dashboard cache is too short for expensive historical aggregates. Split cache TTLs by namespace and period.

Target files:

- `src/eggpool/stats/service.py`
- `src/eggpool/models/config.py` if making this configurable
- `config.example.toml` only if a user-facing option is justified
- tests for cache expiry behavior

Implementation details:

- Replace `_DASHBOARD_CACHE_TTL_S` with a small policy function:
  - live/current health snapshots: 5-15 seconds
  - 1h charts: 15-30 seconds
  - 24h charts: 60 seconds
  - 7d and 30d charts: 120-300 seconds
  - static-ish lists such as model options: cache until catalog refresh or 300 seconds
- Keep the max-entry cap bounded, but consider increasing from 32 if stage telemetry shows churn across pages/periods. Use an LRU eviction rather than oldest-by-insert if churn matters.
- Add stale-while-refresh semantics if feasible:
  - return a recently expired cached value immediately when present
  - trigger a best-effort refresh only in the request path if it is cheap, or leave refresh to the next normal request if background refresh would add complexity
- Ensure cache keys include all semantic filters: period, bucket, group_by, metric if it affects data, account, model, include_disabled.

Validation:

- Cache policy tests confirm 24h/7d chart keys survive beyond 30 seconds.
- Telemetry confirms repeated navigation across dashboard graph pages has high cache-hit rate.

## Phase 5: Progressive graph hydration to improve first paint

Even after query optimization, dashboard pages should not delay the entire shell for graph data. Convert chart-heavy surfaces from fully blocking SSR payloads to progressive hydration while preserving graceful no-JS fallback where possible.

Target files:

- `src/eggpool/dashboard/render.py`
- `src/eggpool/dashboard/routes.py`
- `src/eggpool/dashboard/static/dashboard.js`
- tests in `tests/unit/test_dashboard.py`

Implementation details:

- Add a reusable chart loading shell renderer:
  - container with fixed height to avoid layout shift
  - spinner or text state: `Loading chart data...`
  - empty state: `No data for selected period`
  - error state: `Chart data unavailable`
- For overview:
  - remove `stats.get_timeseries()` from the first blocking `handle_overview()` gather, or make it optional behind a server-rendered stale-cache lookup only.
  - render the chart container with `data-chart-endpoint="/api/timeseries?..."`.
  - have `dashboard.js` fetch the endpoint after bootstrap and render the chart.
- For reliability and bandwidth:
  - apply the same endpoint-driven hydration for chart surfaces while leaving top-line cards server-rendered.
- For the dedicated timeseries page:
  - keep filters and table skeleton server-rendered.
  - hydrate flat and grouped chart payloads independently so one slow graph does not block the other.
  - optionally hydrate the detailed table after the graph if the table uses the same heavy payload.
- Preserve auto-refresh behavior:
  - when `#dashboard-content` is replaced by the existing auto-refresh script, reinitialize graph hydration.
  - do not stack intervals. The current code already tracks the flat timeseries interval at namespace scope; extend that discipline to new hydration intervals.
- Prevent thundering-herd refreshes:
  - use the existing `data-timeseries-busy` guard pattern for chart fetches.
  - if multiple charts need the same endpoint, dedupe in-flight fetches by URL inside `dashboard.js`.

Validation:

- Unit tests verify overview HTML no longer embeds the blocking flat timeseries payload when progressive mode is enabled.
- JS string tests verify chart hydration discovers `data-chart-endpoint`, renders loading/empty/error states, and avoids stacked intervals.
- Manual browser validation: first contentful dashboard shell should appear before graph data when an artificial delay is injected into `/api/timeseries`.

## Phase 6: Chart.js load-path cleanup

Chart.js is probably secondary, but the current cold-load path can still add latency. Keep this pass small.

Target files:

- `src/eggpool/dashboard/render.py`
- `src/eggpool/dashboard/static/dashboard.js`
- `src/eggpool/app.py`

Implementation details:

- Add `<link rel="preload" href="/static/chart.js" as="script">` only on pages that include charts.
- Keep `defer` and the existing `whenChartReady()` guard.
- Consider serving `chart.js` with strong caching and an immutable cache-busted path or query string tied to package version. The current `max-age=86400` is acceptable, but immutable versioned static assets would avoid repeated conditional checks and allow longer caching.
- Ensure `dashboard.js` does not wait for Chart.js to initialize non-chart controls. This is already mostly true: `initTimeseriesControls()`, nav toggle, update copy, and number steppers run outside `whenChartReady()`.

Validation:

- Existing tests for Chart.js race should still pass.
- Cold browser load should show `chart.js` starting earlier in the network waterfall.

## Phase 7: Acceptance benchmarks

Add reproducible performance checks rather than relying on subjective dashboard feel.

Suggested benchmark setup:

- Seed SQLite with representative data sizes:
  - small: 1,000 requests
  - medium: 100,000 requests
  - large: 1,000,000 requests if test runtime permits locally, not necessarily in CI
- Include multiple providers, accounts, models, statuses, streamed/non-streamed rows, and cache token fields.
- Populate rollups for the same windows.

Targets on a normal developer machine:

- Warm overview HTML response excluding network: under 200 ms.
- Cold overview HTML response with rollups: under 500 ms for 100k rows.
- `/api/timeseries?period=24h&bucket=hour`: under 100 ms from rollups.
- `/api/timeseries/grouped?period=24h&bucket=hour&group_by=provider_model&limit=12`: under 150 ms from rollups.
- No common 24h/7d/30d chart request should full-scan the raw `requests` table when rollups exist.

Targets on Raspberry Pi-class hardware should be looser but explicit after measurement. Suggested initial target: visible page shell under 500 ms warm, graph hydration under 1 second warm, under 2 seconds cold with rollups.

## Regression tests and guardrails

Add tests that lock the intended behavior:

- `handle_overview()` should not require flat timeseries to return before shell rendering once progressive hydration lands.
- `StatsService.get_timeseries()` should prefer rollups and only use raw fallback for short windows or live-tail merge.
- `StatsService.get_grouped_timeseries()` should prefer rollups and avoid full raw fallback for long windows.
- Dashboard telemetry should record per-stage timings.
- Chart hydration should not stack intervals after auto-refresh replaces `#dashboard-content`.
- Existing dashboard rendering, escaping, and Chart.js race tests must continue to pass.

Recommended local validation commands:

```bash
pytest -m dashboard
pytest tests/unit/test_dashboard.py
pytest tests/unit/test_stats_service.py
pytest tests/integration/test_database_maintenance.py
ruff check src tests
pyright
```

If full test suite time is acceptable:

```bash
pytest
```

## Implementation order

1. Land instrumentation first. It should be low-risk and will identify whether the slowest path is timeseries, grouped timeseries, compression/cache observability, model stats, or another aggregate.
2. Add query-plan diagnostics and only then add missing indexes.
3. Fix rollup-first chart behavior and suppress large-window raw fallback.
4. Extend cache TTL by query class.
5. Add progressive graph hydration.
6. Polish Chart.js preloading and static asset caching.
7. Add performance fixtures and regression tests.

## Risks

- Suppressing raw fallback can temporarily show empty charts if rollups are not being populated correctly. Mitigate by surfacing a clear `rollup_empty` or `rollup_stale` diagnostic and by fixing rollup writer coverage first.
- More indexes can slow hot request finalization writes on low-power devices. Add only indexes confirmed by `EXPLAIN QUERY PLAN` and benchmark write overhead.
- Progressive hydration changes perceived behavior and may complicate auto-refresh. Keep shell SSR stable, hydrate charts incrementally, and test interval cleanup.
- Longer dashboard cache TTL can show slightly stale graphs. This is acceptable for operator dashboards if freshness labels remain accurate and live health cards keep shorter TTLs.

## Expected outcome

After this line of work, graph pages should render the page shell quickly, charts should hydrate from rollups or cache rather than raw full-table scans, and runtime telemetry should identify any remaining slow dashboard stage without requiring manual source inspection.