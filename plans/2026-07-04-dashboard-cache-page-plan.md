# Dashboard Cache Page Migration Plan

## Context

The dashboard Runtime page currently carries two different responsibilities:

1. process/runtime diagnostics, including process identity, memory, database, background tasks, routing runtime, DNS, outbound client, health state, and probe errors;
2. request-shaping/cache/compression diagnostics, including cache reporting, segmentation, compression opportunity/runtime/policy stats, cache-stability, synthetic cache controls, advisory tuning, and routing guardrails.

The second group is currently hidden under an HTML `<details class="advanced-request-shaping">` element inside `render_runtime`. This creates a poor operator experience because the advanced panel is collapsed by default and does not persist expanded state after page refresh or dashboard auto-refresh. Since the information is not merely incidental runtime telemetry, it should be promoted to a first-class Cache page.

The current repo state already makes this a relatively low-risk route/rendering refactor:

- `src/eggpool/dashboard/routes.py` imports and registers `render_runtime` only for `/runtime`, but it already exposes JSON endpoints for the cache-related datasets.
- `handle_runtime` currently gathers `get_transcoding_stats`, `get_cache_observability`, `get_canonical_request_segmentation`, `get_compression_observability`, `get_compression_runtime`, `get_compression_policy_stats`, `get_cache_stability`, `get_synthetic_cache_summary`, `get_compression_tuning_window_metrics`, and `runtime_metrics.snapshot()` before rendering the Runtime page.
- `_build_request_shaping_summary` in `routes.py` already centralizes the operator summary across config, cache observability, segmentation, compression, synthetic cache, advisory tuning, cache stability, and routing-runtime guardrails.
- `render_runtime` in `src/eggpool/dashboard/render.py` builds a `request_shaping_panel`, then hides the detailed cache/compression panels under `<details class="advanced-request-shaping">`.
- The top nav currently includes Overview, Reliability, Routing, Accounts, Models, Latency, Pings, Bandwidth, Traces, Events, Timeseries, and Runtime, but no Cache page.

## Goals

Move cache-related dashboard material to a dedicated `/cache` page and leave `/runtime` focused on true runtime diagnostics.

The dedicated Cache page should be bookmarkable, refresh-safe, and period-aware. It should preserve the same data fidelity as the current advanced Runtime section while improving discoverability and reducing Runtime page visual density.

## Non-goals

Do not change request accounting, cache-counter extraction, synthetic cache-control behavior, compression policy behavior, scoring/routing behavior, migrations, or database schema in this pass.

Do not add JavaScript-only persisted details state as the primary fix. The better correction is information architecture: make Cache a stable page.

Do not remove existing JSON endpoints unless a later API cleanup explicitly deprecates them. They are useful for external dashboard consumers and tests.

## Proposed final page split

### Runtime page after migration

Runtime should contain:

- Server/process cards: PID, uptime, Python/platform, RSS, open FDs, active threads, load average, dispatch overhead.
- Background task table.
- Database cards: DB path/size, WAL, sync mode, stats DB.
- Routing live-state cards: pending requests, active reservations, in-flight requests, active backoffs.
- Network/client cards: DNS cache/suppression/hits/errors, outbound builds/requests, provider clients.
- Transcoding summary, only if it is treated as runtime/protocol plumbing. If the page remains crowded, move detailed transcoding direction/loss tables to Cache only and leave a compact Runtime card linking to `/cache`.
- Health states and probe errors.
- A small cross-link panel: `Cache and request shaping metrics moved to /cache` with summary cards if inexpensive.

Runtime should not contain the advanced `<details>` block, cache reporting tables, segmentation tables, compression opportunity tables, compression runtime table, compression policy table, cache stability panel, synthetic cache controls panel, advisory tuning panel, or routing guardrails panel.

### Cache page after migration

The new `/cache` page should contain:

- Period selector using the same 1h/24h/7d/30d control used elsewhere.
- Request shaping summary at the top. This should stay as a concise overview of the active configured mode and observed behavior.
- Cache reporting panel.
- Cache stability panel.
- Synthetic cache controls panel.
- Request segmentation panel.
- Compression opportunities panel.
- Compression runtime panel.
- Compression policy panel.
- Advisory tuning panel.
- Routing guardrails panel, because it is semantically tied to the safety invariant that cache/compression signals are reporting-only and not scorer inputs.
- Optional compact Transcoding panel if cache-boundary and protocol conversion diagnostics are easiest to reason about there.

## Implementation plan

### Phase 1: Extract cache/request-shaping rendering helpers

`render_runtime` currently builds most of the cache-related HTML inline. Before adding a new page, split the large renderer into private helper functions in `src/eggpool/dashboard/render.py`.

Recommended helper boundaries:

- `_render_request_shaping_summary_panel(request_shaping_summary, period, routing_guardrails)`
- `_render_cache_reporting_panel(cache_observability, period)`
- `_render_request_segmentation_panel(canonical_request_segmentation, period)`
- `_render_compression_opportunities_panel(compression_observability, period)`
- `_render_compression_runtime_panel(compression_runtime, period)`
- `_render_compression_policy_panel(compression_policy_stats, period)`
- `_render_cache_stability_panel(cache_stability, period)`
- `_render_synthetic_cache_controls_panel(synthetic_cache_summary, period)`
- `_render_advisory_tuning_panel(compression_tuning, period)`
- `_render_routing_guardrails_panel(routing_runtime)`

Keep these helpers HTML-string based and use the existing escaping/formatting helpers. Do not introduce templates in this pass.

The extraction should be behavior-preserving first. After extraction, `render_runtime` should still produce equivalent HTML before the actual migration step. This gives a clean review boundary and makes regressions easier to isolate.

### Phase 2: Add `render_cache`

Add a new public renderer:

```python
def render_cache(
    *,
    period: str,
    theme_css: str = "",
    available_themes: list[str] | None = None,
    current_theme: str = "",
    update_info: Any | None = None,
    routing_runtime: dict[str, Any] | None = None,
    transcoding_stats: dict[str, Any] | None = None,
    cache_observability: dict[str, Any] | None = None,
    canonical_request_segmentation: dict[str, Any] | None = None,
    compression_observability: dict[str, Any] | None = None,
    compression_runtime: dict[str, Any] | None = None,
    compression_policy_stats: dict[str, Any] | None = None,
    cache_stability: dict[str, Any] | None = None,
    synthetic_cache_summary: dict[str, Any] | None = None,
    compression_tuning: dict[str, Any] | None = None,
    request_shaping_summary: dict[str, Any] | None = None,
) -> str:
    ...
```

Use `_render_layout(title="Cache", active_nav="cache", period=period, ...)`. Unlike Runtime, this page should use the selected period label, not the hard-coded `period="runtime"` footer value.

The initial body should be:

```html
<h2>Cache</h2>
<p class="sub">Cache reporting, request shaping, compression, and safety guardrails.</p>
{_render_period_selector(period, current_theme)}
{request_shaping_panel}
{cache_card}
{cache_stability_card}
{synthetic_cache_card}
{segmentation_card}
{compression_card}
{compression_runtime_card}
{compression_policy_card}
{compression_tuning_card}
{routing_guardrails_panel}
```

If the transcoding card remains useful for cache-boundary diagnostics, include it after the summary and rename the section text to clarify why it appears on Cache.

### Phase 3: Add `handle_cache`

In `src/eggpool/dashboard/routes.py`, import `render_cache` from `eggpool.dashboard.render`.

Add an async handler:

```python
async def handle_cache(
    request: Request,
    period: str | None = "24h",
    theme: str | None = None,
) -> Response:
    _get_dashboard_config(request)
    resolved_period = period or "24h"
    db = request.app.state.db
    runtime_metrics = request.app.state.runtime_metrics
    stats_service = StatsService(db)
    (
        transcoding_stats,
        cache_observability,
        canonical_request_segmentation,
        compression_observability,
        compression_runtime,
        compression_policy_stats,
        cache_stability,
        synthetic_cache_summary,
        compression_tuning,
        snapshot,
    ) = await asyncio.gather(...)
    routing_runtime = cast("dict[str, Any]", snapshot.get("routing_runtime") or {})
    request_shaping_summary = _build_request_shaping_summary(...)
    theme_css, _, current_theme, available = _get_theme_data(request, theme)
    return HTMLResponse(content=render_cache(...))
```

Use the same stats calls currently used by `handle_runtime` for these datasets. Use `resolved_period` consistently so invalid or empty values do not leak to renderer labels. Do not instantiate a separate `StatsService` more than once in the same handler.

### Phase 4: Slim `handle_runtime`

After `/cache` exists, remove the cache/compression gather calls from `handle_runtime` unless Runtime intentionally keeps a compact summary. The preferred slim Runtime implementation gathers only:

- `runtime_metrics.snapshot()`
- optionally `stats_service.get_transcoding_stats(period)` if the detailed transcoding card remains on Runtime

Then call `render_runtime` with only the runtime snapshot and any remaining runtime-only stats. Remove unused keyword arguments from `render_runtime` after the helper extraction. If keeping a link panel, pass a simple boolean or prebuilt cache-page href rather than all detailed cache datasets.

This is the main performance win: Runtime refreshes no longer execute the heavy cache/compression aggregate queries on every page load and auto-refresh cycle.

### Phase 5: Register route and nav

In `register_dashboard_routes`, add:

```python
("/cache", handle_cache, HTMLResponse),
```

Place it near Runtime or near Routing. Recommended nav order:

Overview, Reliability, Routing, Cache, Accounts, Models, Latency, Pings, Bandwidth, Traces, Events, Timeseries, Runtime.

In `_render_nav`, add:

```python
("cache", "/cache", "Cache"),
```

Make sure theme and period query parameters are preserved the same way as other nav links.

Add `handle_cache` and `render_cache` to the relevant `__all__` lists.

### Phase 6: Remove the non-persistent `<details>` UX

Delete this block from the Runtime body:

```html
<details class="advanced-request-shaping">
  <summary>Advanced request-shaping details</summary>
  ...
</details>
```

Do not replace it with a new details element on the Cache page. The dedicated page exists specifically to make these panels persist across refreshes. If page length is a concern, use normal headings, cards, compact tables, or anchor links rather than disclosure state.

Recommended small Runtime replacement:

```html
<section class="panel">
  <h3>Cache and request shaping</h3>
  <p class="sub">Detailed cache reporting, compression, synthetic cache controls, advisory tuning, and routing guardrails are now on the Cache page.</p>
  <p><a href="/cache?period=24h&amp;theme=...">Open Cache diagnostics</a></p>
</section>
```

Preserve the current theme query parameter. Use a safe period value; `24h` is fine if Runtime itself continues to use the synthetic `runtime` footer period.

### Phase 7: Improve page-level information architecture

Add anchor-friendly section headings or a lightweight local index at the top of Cache if the page is long:

- Summary
- Cache reporting
- Cache stability
- Synthetic cache controls
- Request segmentation
- Compression
- Advisory tuning
- Routing guardrails

This can be normal `<a href="#cache-reporting">` links with stable IDs. Keep it server-rendered and no-JS.

Use distinct section names. Avoid overloading `Cache controls` for both provider-reported cache counters and synthetic cache controls. Recommended terminology:

- `Cache reporting`: provider/upstream-observed cache counters.
- `Synthetic cache controls`: EggPool-added provider-bound cache annotations.
- `Cache stability`: preservation of cache boundaries through protocol transcoding.
- `Request shaping`: combined operator summary.

### Phase 8: Tests and verification

Add or update dashboard tests to cover:

1. `/cache` returns 200 with dashboard enabled.
2. `/cache?period=1h` renders the selected period and all top-level section headings.
3. Top nav marks Cache active on `/cache` and Runtime active on `/runtime`.
4. `/runtime` no longer renders `Advanced request-shaping details` or `class="advanced-request-shaping"`.
5. `/runtime` still renders background tasks, DB cards, routing live-state cards, network cards, health states, and probe errors.
6. Existing JSON endpoints still return stable shapes:
   - `/api/stats/cache-observability`
   - `/api/stats/cache-stability`
   - `/api/stats/synthetic-cache-observability`
   - `/api/stats/canonical-request-segmentation`
   - `/api/stats/compression-observability`
   - `/api/stats/compression-runtime`
   - `/api/stats/compression-policies`
   - `/api/stats/compression-tuning`
   - `/api/stats/request-shaping`
7. The Cache page does not expose raw prompt content, request bodies, or unescaped model/account/provider identifiers.
8. Theme preservation works across the new Cache nav link.

Run:

```bash
ruff check src tests
pyright
pytest
```

If the full suite is too slow locally, at minimum run the dashboard route/render tests plus the stats tests touching the cache/compression query methods.

## Suggested refactor sequence for implementer

1. Extract helper functions from `render_runtime` without changing output.
2. Add `render_cache` using those helpers.
3. Add `handle_cache` and route/nav registration.
4. Remove cache/compression-heavy args from `render_runtime` and slim `handle_runtime`.
5. Add tests for new route and Runtime slimming.
6. Update docs/screenshots if the dashboard docs mention Runtime as the home for cache/compression metrics.

## Acceptance criteria

- `/cache` is available from the top navigation and renders without JavaScript dependency.
- `/cache` contains the existing advanced cache/request-shaping/compression information that was previously hidden under Runtime's details block.
- `/runtime` is materially shorter and focused on process/runtime diagnostics.
- The non-persistent `Advanced request-shaping details` disclosure is gone from Runtime.
- Cache/compression JSON endpoints remain backward compatible.
- The new page preserves period and theme controls.
- No routing/scoring logic changes; cache/compression/synthetic/tuning metrics remain reporting-only unless an existing explicit runtime path already did otherwise.
- Dashboard pages continue to escape dynamic text.
- `ruff`, `pyright`, and relevant pytest targets pass.

## Risks and mitigations

The largest risk is accidentally changing the semantics of cache/compression panels while extracting helpers from the large `render_runtime` function. Mitigate by doing a behavior-preserving helper extraction first and testing for key headings/labels before changing the page split.

The second risk is duplicated expensive stats calls. Avoid this by moving detailed cache/compression calls out of `handle_runtime` after `/cache` exists. Runtime should not continue to gather heavy aggregates just to discard them.

The third risk is stale navigation or period behavior. Use the existing `_render_period_selector` and `_render_nav` conventions so theme and period propagation remain consistent with the rest of the dashboard.

## Follow-up polish after migration

After the first migration lands, consider adding small overview cards to the main Overview page that deep-link to Cache:

- Cache reported rate
- Cache read/write tokens
- Synthetic cache mode
- Compression mode and actual savings tokens

Keep Overview to one row of cards. The detailed tables belong on `/cache`.
