# Grouped Timeseries Dashboard Plan

## Goal

Replace the current low-value request timeseries display with a reliable grouped usage visualization. The intended operator experience is a stacked bar chart by default, showing usage per time bucket broken down by provider/model, with hover details for both the hovered series segment and total usage in that bucket. The `/timeseries` page should become the detailed usage analysis page: visual chart first, grouped detail table below, and controls for period, bucket, grouping, metric, and top-N series limit.

This plan also fixes the current chart lifecycle defect. The existing inline chart script can run before Chart.js has loaded, and overview auto-refresh replaces the chart canvas with `innerHTML` without re-running scripts. The implementation must move chart initialization into stable dashboard JavaScript and explicitly reinitialize charts after dashboard refresh.

## Current State

The existing aggregate query is `fetch_timeseries()` in `src/eggpool/stats/queries.py`. It groups only by `bucket`, returning one row per hour/day with aggregate request count, token counts, cost, error count, bytes, and TTFT. This is useful for totals and tables, but it cannot answer which models/providers contributed to usage inside a time period.

`StatsService.get_timeseries()` in `src/eggpool/stats/service.py` wraps the aggregate query and supports optional account/model filtering, but it does not support grouping dimensions.

`src/eggpool/dashboard/routes.py` wires `/timeseries` and `/api/timeseries` to the aggregate service. The overview route also fetches aggregate hourly timeseries and passes it to `render_overview()`.

`src/eggpool/dashboard/render.py` has `_render_timeseries_chart()`, but that function emits inline JavaScript that immediately calls `new Chart(...)`. `_render_layout()` appends `/static/chart.js` at the end of the document with `defer`, so the inline script can execute before `window.Chart` exists. The overview page also enables auto-refresh, and `_render_auto_refresh_script()` replaces `#dashboard-content.innerHTML`; scripts inserted this way do not execute, so charts disappear or remain uninitialized after refresh.

The standalone `/timeseries` page currently renders only a table. That is not aligned with the dashboard navigation label or the desired use case.

## Desired UX

The default `/timeseries` view should be:

```text
/timeseries?period=24h&bucket=hour&group_by=provider_model&metric=requests&limit=12
```

The page should render:

1. A control form with period, bucket, grouping, metric, and limit.
2. A stacked bar chart showing one bar per bucket. Each stack segment represents a provider/model pair by default.
3. A tooltip on each segment showing the selected series' details and total usage for that bucket.
4. A grouped detail table below the chart, using the same grouped data contract.

The overview page may include a compact version of the same chart, ideally top 6 provider/model series by requests over 24h or the selected period. The full analysis controls should live on `/timeseries`.

Default chart type should be stacked bar. A later optional line-chart toggle can be added for trend metrics, but stacked bar should be the first implementation because it shows both per-bucket total and composition.

## Data Contract

Add a grouped endpoint rather than overloading the existing aggregate `/api/timeseries` endpoint.

Recommended endpoint:

```text
GET /api/timeseries/grouped?period=24h&bucket=hour&group_by=provider_model&metric=requests&limit=12&account=&model=
```

Allowed query parameters:

- `period`: existing period syntax; default `24h`.
- `bucket`: `hour` or `day`; default `hour`.
- `group_by`: `provider_model`, `provider`, `model`, or `account`; default `provider_model`.
- `metric`: `requests`, `tokens`, `cost`, `errors`, `bytes`, `latency`, `ttft`; default `requests`.
- `limit`: integer from 1 to 25; default 12.
- `account`: optional account filter by account name.
- `model`: optional model filter by model id. Must match either `model_id` or `original_model_id`, consistent with existing model stats behavior.

Return a dashboard-neutral JSON shape, not a Chart.js-specific blob:

```json
{
  "bucket": "hour",
  "group_by": "provider_model",
  "metric": "requests",
  "limit": 12,
  "series": [
    {
      "key": "opencode_go:gpt-5.3-codex",
      "label": "opencode_go / gpt-5.3-codex",
      "provider_id": "opencode_go",
      "model_id": "gpt-5.3-codex",
      "account_name": null,
      "is_other": false,
      "total_requests": 42,
      "error_count": 1,
      "input_tokens": 100000,
      "output_tokens": 30000,
      "cache_read_tokens": 250000,
      "cache_write_tokens": 5000,
      "reasoning_tokens": 8000,
      "total_tokens": 130000,
      "cost_microdollars": 98000,
      "bytes_received": 2000000,
      "bytes_emitted": 10000000,
      "avg_latency_ms": 22000.0,
      "avg_ttft_ms": 900.0
    }
  ],
  "buckets": ["2026-06-25 18:00:00", "2026-06-25 19:00:00"],
  "bucket_totals": [
    {
      "bucket": "2026-06-25 18:00:00",
      "request_count": 8,
      "error_count": 0,
      "input_tokens": 12000,
      "output_tokens": 4000,
      "cache_read_tokens": 30000,
      "cache_write_tokens": 1000,
      "reasoning_tokens": 2500,
      "total_tokens": 16000,
      "cost_microdollars": 18000,
      "bytes_received": 240000,
      "bytes_emitted": 1800000,
      "avg_latency_ms": 22000.0,
      "avg_ttft_ms": 900.0
    }
  ],
  "points": [
    {
      "bucket": "2026-06-25 18:00:00",
      "series_key": "opencode_go:gpt-5.3-codex",
      "label": "opencode_go / gpt-5.3-codex",
      "provider_id": "opencode_go",
      "model_id": "gpt-5.3-codex",
      "account_name": null,
      "is_other": false,
      "request_count": 8,
      "error_count": 0,
      "input_tokens": 12000,
      "output_tokens": 4000,
      "cache_read_tokens": 30000,
      "cache_write_tokens": 1000,
      "reasoning_tokens": 2500,
      "total_tokens": 16000,
      "cost_microdollars": 18000,
      "bytes_received": 240000,
      "bytes_emitted": 1800000,
      "avg_latency_ms": 22000.0,
      "avg_ttft_ms": 900.0
    }
  ]
}
```

Notes:

- Use `request_count` in point rows, not `total_requests`, to stay consistent with existing query names.
- Use `total_requests` only in series summary rows if desired; alternatively use `request_count` everywhere. Prefer consistency if this is implemented from scratch.
- Include `bucket_totals` so tooltips do not need to recompute totals repeatedly.
- Include `series` summary metadata so the frontend can build datasets, legends, labels, and table summaries without re-aggregating.
- Always include top-N series plus an `Other` series when rows exist beyond the limit.

## Backend Query Implementation

Add `fetch_grouped_timeseries()` to `src/eggpool/stats/queries.py`.

Signature:

```python
async def fetch_grouped_timeseries(
    db: Database,
    start: str,
    end: str,
    bucket: str = "hour",
    group_by: str = "provider_model",
    metric: str = "requests",
    limit: int = 12,
    account_id: int | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    ...
```

Do not interpolate raw query parameter values into SQL. Map enum values to constant SQL fragments.

Bucket mapping:

```python
bucket_fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
```

Group expression mapping:

- `provider`: key `r.provider_id`, label `r.provider_id`, include `provider_id`.
- `model`: key `COALESCE(r.original_model_id, r.model_id)`, label same, include `model_id`.
- `provider_model`: key `r.provider_id || ':' || COALESCE(r.original_model_id, r.model_id)`, label `r.provider_id || ' / ' || COALESCE(...)`, include both provider and model.
- `account`: join `accounts a ON a.id = r.account_id`; key `a.name`, label `a.name`, include `account_name`.

Metric ranking expression mapping for top-N:

- `requests`: `SUM(request_count)` or `COUNT(*)`.
- `tokens`: `SUM(total_tokens)`.
- `cost`: `SUM(cost_microdollars)`.
- `errors`: `SUM(error_count)`.
- `bytes`: `SUM(bytes_received + bytes_emitted)`.
- `latency`: rank by `SUM(request_count)` rather than average latency; latency is a display metric, not a good top-N selector.
- `ttft`: rank by `SUM(request_count)`.

Recommended SQL shape:

```sql
WITH base AS (
    SELECT
        strftime(?, r.started_at) AS bucket,
        <group_key_expr> AS raw_series_key,
        <group_label_expr> AS raw_series_label,
        r.provider_id AS provider_id,
        COALESCE(r.original_model_id, r.model_id) AS model_id,
        a.name AS account_name,
        COUNT(*) AS request_count,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0) AS error_count,
        COALESCE(SUM(r.input_tokens), 0) AS input_tokens,
        COALESCE(SUM(r.output_tokens), 0) AS output_tokens,
        COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
        COALESCE(SUM(r.cache_write_tokens), 0) AS cache_write_tokens,
        COALESCE(SUM(r.reasoning_tokens), 0) AS reasoning_tokens,
        COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0) AS total_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) AS cost_microdollars,
        COALESCE(SUM(r.bytes_received), 0) AS bytes_received,
        COALESCE(SUM(r.bytes_emitted), 0) AS bytes_emitted,
        COALESCE(AVG(r.upstream_latency_ms), 0) AS avg_latency_ms,
        COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0) AS avg_ttft_ms
    FROM requests r
    JOIN accounts a ON a.id = r.account_id
    WHERE r.started_at >= ? AND r.started_at < ?
      <account_filter>
      <model_filter>
    GROUP BY bucket, raw_series_key, raw_series_label, r.provider_id,
             COALESCE(r.original_model_id, r.model_id), a.name
), ranked AS (
    SELECT
        raw_series_key,
        raw_series_label,
        provider_id,
        model_id,
        account_name,
        SUM(request_count) AS total_requests,
        SUM(error_count) AS error_count,
        SUM(input_tokens) AS input_tokens,
        SUM(output_tokens) AS output_tokens,
        SUM(cache_read_tokens) AS cache_read_tokens,
        SUM(cache_write_tokens) AS cache_write_tokens,
        SUM(reasoning_tokens) AS reasoning_tokens,
        SUM(total_tokens) AS total_tokens,
        SUM(cost_microdollars) AS cost_microdollars,
        SUM(bytes_received) AS bytes_received,
        SUM(bytes_emitted) AS bytes_emitted,
        CASE WHEN SUM(request_count) > 0
             THEN SUM(avg_latency_ms * request_count) / SUM(request_count)
             ELSE 0 END AS avg_latency_ms,
        CASE WHEN SUM(request_count) > 0
             THEN SUM(avg_ttft_ms * request_count) / SUM(request_count)
             ELSE 0 END AS avg_ttft_ms,
        ROW_NUMBER() OVER (ORDER BY <ranking_expr> DESC, raw_series_label ASC) AS series_rank
    FROM base
    GROUP BY raw_series_key, raw_series_label, provider_id, model_id, account_name
), folded AS (
    SELECT
        b.bucket,
        CASE WHEN r.series_rank <= ? THEN b.raw_series_key ELSE '__other__' END AS series_key,
        CASE WHEN r.series_rank <= ? THEN b.raw_series_label ELSE 'Other' END AS label,
        CASE WHEN r.series_rank <= ? THEN b.provider_id ELSE NULL END AS provider_id,
        CASE WHEN r.series_rank <= ? THEN b.model_id ELSE NULL END AS model_id,
        CASE WHEN r.series_rank <= ? THEN b.account_name ELSE NULL END AS account_name,
        CASE WHEN r.series_rank <= ? THEN 0 ELSE 1 END AS is_other,
        SUM(b.request_count) AS request_count,
        SUM(b.error_count) AS error_count,
        SUM(b.input_tokens) AS input_tokens,
        SUM(b.output_tokens) AS output_tokens,
        SUM(b.cache_read_tokens) AS cache_read_tokens,
        SUM(b.cache_write_tokens) AS cache_write_tokens,
        SUM(b.reasoning_tokens) AS reasoning_tokens,
        SUM(b.total_tokens) AS total_tokens,
        SUM(b.cost_microdollars) AS cost_microdollars,
        SUM(b.bytes_received) AS bytes_received,
        SUM(b.bytes_emitted) AS bytes_emitted,
        CASE WHEN SUM(b.request_count) > 0
             THEN SUM(b.avg_latency_ms * b.request_count) / SUM(b.request_count)
             ELSE 0 END AS avg_latency_ms,
        CASE WHEN SUM(b.request_count) > 0
             THEN SUM(b.avg_ttft_ms * b.request_count) / SUM(b.request_count)
             ELSE 0 END AS avg_ttft_ms
    FROM base b
    JOIN ranked r ON r.raw_series_key = b.raw_series_key
    GROUP BY b.bucket, series_key, label, provider_id, model_id, account_name, is_other
)
SELECT * FROM folded ORDER BY bucket, is_other ASC, label ASC;
```

Implementation note: SQLite may not allow some aliases in `GROUP BY`/window contexts exactly as written. It is acceptable to simplify into two separate queries: one query for ranked top series, another for folded points using a Python `set` of top keys. Given expected dashboard data volume is modest and retention is bounded, a Python fold may be clearer and less SQL-fragile.

Recommended simpler approach:

1. Query grouped raw rows by bucket + series for the requested window.
2. In Python, aggregate totals by `series_key`, rank top-N according to selected metric.
3. Fold non-top rows into `__other__` per bucket.
4. Build `points`, `series`, `buckets`, and `bucket_totals` in Python.

This is easier to test, avoids SQL alias complexity, and is still bounded by dashboard retention and time range.

## Service Layer

Add `StatsService.get_grouped_timeseries()` in `src/eggpool/stats/service.py`.

Signature:

```python
async def get_grouped_timeseries(
    self,
    time_range: TimeRange,
    bucket: str = "hour",
    group_by: str = "provider_model",
    metric: str = "requests",
    limit: int = 12,
    account_name: str | None = None,
    model_id: str | None = None,
    *,
    use_cache: bool = False,
) -> dict[str, Any]:
    ...
```

Behavior:

- Validate `bucket`, `group_by`, and `metric`; normalize invalid values to defaults.
- Clamp `limit` to `1..25`.
- Resolve `account_name` to `account_id`; return an empty payload if the account does not exist.
- Use the dashboard cache for overview and page rendering:

```python
key = self._dashboard_cache_key(
    "grouped_timeseries",
    time_range,
    bucket,
    group_by,
    metric,
    str(limit),
    account_name or "",
    model_id or "",
)
```

- Delegate to `queries.fetch_grouped_timeseries()`.

Export the new query from `src/eggpool/stats/__init__.py` if consistent with existing exports.

## Routes

In `src/eggpool/dashboard/routes.py`:

1. Extend `handle_timeseries()` parameters:

```python
async def handle_timeseries(
    request: Request,
    period: str | None = "24h",
    bucket: str = "hour",
    account: str | None = None,
    model: str | None = None,
    group_by: str = "provider_model",
    metric: str = "requests",
    limit: int = 12,
    theme: str | None = None,
) -> Response:
```

2. Fetch both grouped and aggregate data initially:

```python
series, grouped = await asyncio.gather(
    stats.get_timeseries(...),
    stats.get_grouped_timeseries(..., use_cache=True),
)
```

3. Pass `grouped`, selected controls, account/model filters, and table data into `render_timeseries()`.

4. Add `handle_grouped_timeseries_json()` and route it at `/api/timeseries/grouped`.

5. Use the same validation/clamping logic as the HTML route.

6. Update `handle_overview()` to fetch compact grouped data instead of aggregate chart data if the overview chart is kept:

```python
stats.get_grouped_timeseries(
    time_range,
    bucket="hour",
    group_by="provider_model",
    metric="requests",
    limit=6,
    use_cache=True,
)
```

Keep existing aggregate `get_timeseries()` available for tables and backwards-compatible `/api/timeseries`.

## Renderer Changes

In `src/eggpool/dashboard/render.py`:

1. Replace `_render_timeseries_chart(period, initial_data)` with a grouped chart renderer:

```python
def _render_grouped_timeseries_chart(
    grouped: dict[str, Any],
    period: str,
    bucket: str,
    group_by: str,
    metric: str,
    limit: int,
    account_filter: str = "",
    model_filter: str = "",
    compact: bool = False,
) -> str:
    ...
```

2. The renderer should emit:

- A `<section class="panel timeseries-chart-panel">`.
- A `<canvas class="grouped-timeseries-chart" data-chart-id="...">`.
- A sibling `<script type="application/json" class="grouped-timeseries-data">...</script>` containing the grouped JSON payload.
- Optional `data-*` attributes for `period`, `bucket`, `group-by`, `metric`, `limit`, `account`, `model`.

3. Do not emit inline JavaScript that calls Chart.js.

4. Add a controls form above the chart on `/timeseries`:

- Period selector should preserve `bucket`, `group_by`, `metric`, `limit`, `account`, `model`, and `theme`. The existing `_render_period_selector()` currently only preserves theme; either extend it with hidden params or write a specialized timeseries controls form.
- Bucket select: `hour`, `day`.
- Group select: `provider_model`, `provider`, `model`, `account`.
- Metric select: `requests`, `tokens`, `cost`, `errors`, `bytes`, `latency`, `ttft`.
- Limit select/input: 6, 8, 12, 16, 20, 25.
- Account/model text filters.

5. Update `render_timeseries()` to show:

- heading;
- controls;
- grouped chart panel;
- grouped detail table;
- optional aggregate table below or behind a `Summary table` section.

6. Create `_render_grouped_timeseries_table(grouped)`:

Columns:

- Bucket
- Provider
- Model
- Account, only when useful or when `group_by == account`
- Requests
- Errors
- Input tokens
- Output tokens
- Cache read
- Cache write
- Reasoning
- Total tokens
- Cost
- BW received
- BW emitted
- Avg latency
- Avg TTFT

Use existing formatters: `format_int`, `format_tokens`, `format_microdollars`, `format_bytes`, `format_latency`.

7. Keep existing aggregate table support as a fallback for empty grouped payloads or as a secondary section.

8. Update `render_overview()` to call `_render_grouped_timeseries_chart(... compact=True)` if grouped overview data is available. If not changing overview immediately, remove the broken aggregate chart from overview until the JS lifecycle is fixed.

## JavaScript Lifecycle Fix

In `src/eggpool/dashboard/static/dashboard.js`:

1. Add `namespace.initGroupedTimeseriesCharts = function initGroupedTimeseriesCharts() { ... }`.

2. Behavior:

- Find all `.grouped-timeseries-chart` canvases.
- For each canvas, find the corresponding `.grouped-timeseries-data` JSON script in the same panel.
- Parse the grouped payload.
- Build Chart.js datasets from `payload.series` and `payload.points`.
- Destroy a prior chart instance if one exists on the canvas. Use a `WeakMap` or attach `canvas.__eggpoolChart`.
- If `window.Chart` is missing, return without throwing. Optionally set a visible message in the panel: `Chart library not loaded`.

3. Build stacked bar datasets:

```javascript
const labels = payload.buckets;
const datasets = payload.series.map((series) => ({
  label: series.label,
  data: labels.map((bucket) => metricValue(pointByBucketAndSeries[bucket]?.[series.key], payload.metric)),
  stack: "usage",
}));
```

4. Metric value mapping:

- `requests`: `request_count`
- `tokens`: `total_tokens`
- `cost`: `cost_microdollars / 1_000_000`
- `errors`: `error_count`
- `bytes`: `bytes_received + bytes_emitted`
- `latency`: `avg_latency_ms`
- `ttft`: `avg_ttft_ms`

Note: latency and TTFT are not additive. For these metrics, prefer a non-stacked bar or line chart in a follow-up. In this first implementation, either disable `latency`/`ttft` for stacked view or render grouped bars without stacking for those metrics. The safest first pass is to omit latency/TTFT metric options from the chart controls and keep them in tooltips/table only. If included, document that they use per-series averages and should not be summed.

5. Tooltip behavior:

- Title: bucket label.
- Segment label: provider/model label and selected metric value.
- Additional lines:
  - requests
  - errors
  - total tokens
  - cache read/write
  - reasoning tokens
  - cost
  - bandwidth received/emitted
  - avg latency
  - avg TTFT
- Add bucket total lines after a separator:
  - total requests
  - total tokens
  - total cost
  - total errors

6. Add formatting helpers or reuse existing `EggPoolDashboard.formatCount`, `formatDurationMs`, and `formatPercent`. Add:

- `formatBytes(bytes)` with B/KB/MB/GB/TB scaling.
- `formatTokens(tokens)` with K/M/B/T scaling.
- `formatDollarsFromMicro(microdollars)`.

7. Load order:

Update `_render_layout()` so pages with charts include both Chart.js and dashboard.js in correct order:

```html
<script defer src="/static/chart.js"></script>
<script defer src="/static/dashboard.js"></script>
```

Because both scripts are `defer`, they execute in document order after parsing. `dashboard.js` should call `initGroupedTimeseriesCharts()` on `DOMContentLoaded` or immediately if the DOM is ready.

8. Auto-refresh integration:

Update `_render_auto_refresh_script()` after `content.innerHTML = next.innerHTML;`:

```javascript
if (window.EggPoolDashboard?.initGroupedTimeseriesCharts) {
  window.EggPoolDashboard.initGroupedTimeseriesCharts();
}
```

This must happen after the DOM replacement.

9. The chart refresh interval inside the chart initializer is optional. Prefer relying on dashboard auto-refresh for overview and manual/page reload for `/timeseries` in the first pass. Avoid multiple independent intervals per chart until there is a cleanup story.

## Styling

Update `src/eggpool/dashboard/static/dashboard.css`:

- Add `.timeseries-chart-panel` and `.chart-container` classes instead of inline height styles.
- Suggested desktop height: 360px on `/timeseries`, 260-300px compact on overview.
- Add responsive behavior for narrow screens.
- Style controls as a compact grid or flex row that wraps.
- Ensure the chart canvas has `max-width: 100%` and stable height.

## Tests

Add or update tests in the existing test structure. If no dashboard-specific tests exist yet, create focused unit tests for query/service output and renderer content.

Backend query/service tests:

1. Seed requests across two providers, multiple models, multiple buckets.
2. Assert `fetch_grouped_timeseries(... group_by="provider_model")` returns distinct series keys and per-bucket points.
3. Assert top-N folding creates `__other__` when more than `limit` series exist.
4. Assert `bucket_totals` equal the sum of folded points, including `Other`.
5. Assert model relinking: rows with `original_model_id` are grouped under the original model id.
6. Assert account filter returns only matching account data.
7. Assert unknown account through `StatsService.get_grouped_timeseries()` returns an empty stable payload, not `None` or an exception.
8. Assert invalid `bucket`, `group_by`, `metric`, and `limit` normalize/clamp safely.

Route tests:

1. `GET /api/timeseries/grouped` returns 200 and the expected JSON keys.
2. `GET /timeseries` includes the chart canvas, JSON data island, controls, and grouped table.
3. Auth behavior matches existing dashboard auth behavior.

Renderer tests:

1. `_render_grouped_timeseries_chart()` escapes labels and does not inject raw HTML from provider/model names.
2. The renderer emits no inline executable chart code; it should emit JSON data only.
3. Empty grouped payload renders a useful empty state.

Static JS cannot be thoroughly unit-tested without a browser harness, so keep it simple and defensive. At minimum, ensure pages load `/static/chart.js` before `/static/dashboard.js` when `include_chart_js=True`.

Manual validation:

1. Run `uv run eggpool serve` with a local test database.
2. Generate requests across at least two providers/models.
3. Open `/timeseries?period=24h&bucket=hour&group_by=provider_model&metric=requests`.
4. Confirm stacked bars render.
5. Hover each segment and verify series + bucket totals.
6. Change grouping to provider/model/account and confirm chart/table update.
7. Wait through one dashboard refresh cycle on overview and confirm the chart remains visible.
8. Confirm browser console has no `Chart is not defined` or JSON parse errors.

## Migration and Compatibility

No database migration should be required. The implementation uses the existing `requests`, `accounts`, `model_id`, `original_model_id`, provider, token, byte, cost, and latency fields.

Keep existing `/api/timeseries` and aggregate table behavior for compatibility. Add `/api/timeseries/grouped` as the new richer contract.

Do not remove current aggregate functions unless a later cleanup confirms no external users rely on them.

## Performance Considerations

The dashboard uses a separate read-only SQLite connection when the database path is not `:memory:`, so grouped stats should not block the data-plane connection lock under normal deployment. Keep the query bounded by existing period presets and dashboard retention.

Use the existing dashboard TTL cache for grouped timeseries. A 30-second cache is acceptable for the dashboard and avoids repeated grouped aggregation during auto-refresh or repeated page loads.

Clamp top-N limit to 25. Fold long tails into `Other`. This is both a visual readability requirement and a performance guard.

Do not introduce client-side polling loops per chart in the first implementation. Reuse page auto-refresh where already enabled and manual refresh elsewhere.

## Edge Cases

- Empty database: render an empty chart/table state without JS errors.
- Only one model/provider: stacked chart should still render one dataset.
- Many models/providers: top-N + Other should preserve bucket totals.
- Same model through different providers: default `provider_model` grouping must disambiguate.
- Deprecated or withdrawn models: use `original_model_id` when present.
- Errors with missing token/cost fields: use zeros via `COALESCE`.
- Pending requests: current aggregate timeseries counts all rows. Keep behavior consistent unless there is a specific reason to exclude pending; tooltips/table can expose status-specific counts in a future pass.
- Latency/TTFT: do not stack averages. Keep latency/TTFT in table and tooltip. Add line/grouped non-stacked view later if needed.

## Suggested Implementation Order

1. Implement `fetch_grouped_timeseries()` with Python folding after a raw grouped SQL query.
2. Add `StatsService.get_grouped_timeseries()` with validation, account resolution, model filtering, and cache support.
3. Add `/api/timeseries/grouped` and extend `/timeseries` route to fetch grouped data.
4. Add `_render_grouped_timeseries_chart()`, `_render_grouped_timeseries_table()`, and a dedicated timeseries controls form.
5. Update `/static/dashboard.js` with chart initialization, formatting helpers, and safe destroy/reinitialize behavior.
6. Update `_render_layout()` script order and `_render_auto_refresh_script()` reinitialization hook.
7. Update overview to use the grouped compact chart or temporarily keep chart only on `/timeseries` until stable.
8. Add tests for query/service/routes/rendering.
9. Run `uv run ruff check .`, `uv run pyright`, and `uv run pytest`.

## Acceptance Criteria

- `/timeseries` renders a stacked bar chart by default.
- Default chart groups by provider/model and shows request counts per hour over 24h.
- Hover tooltip shows both segment details and bucket totals.
- The grouped detail table below the chart exposes model/provider-level rows with tokens, cost, bandwidth, errors, and latency fields.
- Top-N folding into `Other` preserves bucket totals.
- The chart does not fail with `Chart is not defined`.
- Overview auto-refresh does not erase or break the chart.
- Existing `/api/timeseries` continues to work.
- New `/api/timeseries/grouped` returns a stable documented JSON payload.
- Tests cover grouping, model relinking, top-N folding, empty state, unknown account, and route rendering.
