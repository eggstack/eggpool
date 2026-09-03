# Deep Dive: Dashboard & Stats

Back to [Overview](overview.md)

## Purpose

Self-updating server-rendered HTML dashboard with 50 themes, plus a comprehensive JSON API for stats and operational data.

## Architecture

```
┌─────────────────────────────────────┐
│         FastAPI Application          │
│                                      │
│  ┌──────────────┐  ┌──────────────┐ │
│  │ Dashboard     │  │ Stats API    │ │
│  │ (HTML pages)  │  │ (JSON)       │ │
│  └──────┬───────┘  └──────┬───────┘ │
│         │                  │         │
│  ┌──────▼──────────────────▼───────┐ │
│  │       StatsService              │ │
│  │   (30s in-memory cache)         │ │
│  └──────────────┬──────────────────┘ │
│                 │                    │
│  ┌──────────────▼──────────────────┐ │
│  │       Stats Queries             │ │
│  │   (SQL aggregations)            │ │
│  └─────────────────────────────────┘ │
└─────────────────────────────────────┘
```

## Key Modules

### `dashboard/routes.py`

Route registration for all dashboard pages:
- `/` — Overview
- `/accounts` — Account breakdown
- `/models` — Model catalog with stats
- `/models/{model_id:path}` — Model detail page
- `/latency` — Latency breakdown
- `/events` — Recent events
- `/timeseries` — Time-series charts
- `/bandwidth` — Bandwidth heatmap
- `/pings` — Provider ping health
- `/reliability` — Retry and pending health
- `/routing` — Routing decision analysis
- `/traces` — Recent request traces
- `/runtime` — Live runtime metrics
- `/cache` — Request shaping (cache, segmentation)

### `dashboard/render.py`

HTML rendering for all dashboard pages. Server-rendered, no JavaScript framework.

### `dashboard/telemetry.py`

`DashboardTelemetry` — per-route render duration tracking.

### `dashboard/timeseries_buckets.py`

Time-series bucket computation for charts.

### `dashboard/escape.py`

HTML escaping utilities. All source-provided text is HTML-escaped.

### `dashboard/theme.py`

Theme management.

### `dashboard/themes/`

50 CSS theme files.

### `dashboard/static/`

Static assets (CSS, JS, chart.js).

### `dashboard/_resources.py`

Resource helper functions.

## Dashboard Pages

### Overview (`/`)

Summary cards:
- Request count, token usage, cost
- Provider health status
- Active requests
- Update indicator

### Models (`/models`)

- Model catalog with request/token/cost stats
- Provider-scoped or collapsed view
- Model-info status pills (fresh/partial/sparse/stale/conflict)
- Links to detail pages

### Runtime (`/runtime`)

- Process topology
- Memory usage
- Background task state
- Database health
- OS load average
- Dispatch overhead distribution
- Selection claim diagnostics
- Request-shaping relocation panel

### Cache (`/cache`)

Request shaping surface with 7 cards:
1. **Request shaping** — operator summary
2. **Provider cache counters** — coverage and hit rates
3. **Request segmentation** — stable/semi-stable/volatile
4. **Compression** — observe/safe mode outcomes
5. **Compression policy** — per-policy rollup
6. **Cache stability** — transcoded count
7. **Routing isolation** — guardrail state

Plus advanced diagnostics disclosure.

### Model Detail (`/models/{model_id:path}`)

Full model-info detail with status cards, summary, provider/callability, metadata, benchmarks, Hugging Face metadata, conflicts, and provenance.

Model-info cards render persisted canonical and source-health truth. A sparse
row remains sparse when external sources have not been attempted, failed, or
did not match; the dashboard does not synthesize benchmark or metadata claims.
Source health and match-evidence surfaces distinguish source outages from
successful catalogs with a model-level identity miss. Automatic enrichment is
driven by catalog refresh opportunities and per-row due state; the manual
refresh endpoint is an operator diagnostic.

## Stats Service

### `stats/service.py` — StatsService

Orchestrates all stats queries with 30s in-memory cache. Reduces database load for dashboard and API.

### `stats/queries.py`

SQL query functions for all dashboard/API endpoints:
- Request/token/cost aggregations
- Cache observability
- Segmentation stats
- Compression observability + runtime
- Routing decision analysis
- Provider health
- Time-series data

### `stats/cache_metrics.py`

`derive_cache_metric_terms()` — provider cache hit rate computation.

### `stats/grouped_timeseries.py`

Time-series aggregation for charts.

### `stats/segmentation.py`

Segmentation stats aggregation.

### `stats/transcoding.py`

Transcoding stats aggregation.

### `stats/dashboard_explain.py`

`eggpool stats explain-dashboard` diagnostic command.

## JSON API Endpoints

Representative read-only JSON surface (registered in
`api/stats.py`, `api/runtime.py`, `api/update.py`, `api/backoff.py`,
`api/network.py`, `api/model_info.py`, and the dashboard routes):
`/api/stats/summary`, `/api/stats/accounts`, `/api/stats/models`,
`/api/stats/timeseries`, `/api/stats/bandwidth`, `/api/stats/errors`,
`/api/stats/latency`, `/api/stats/pings`, `/api/stats/ips`,
`/api/stats/attempts`, `/api/stats/retries`, `/api/stats/routing`,
`/api/stats/routing-selections`, `/api/stats/routing-exclusions`,
`/api/stats/routing-skew`, `/api/stats/routing/eligibility`,
`/api/stats/operational`, `/api/stats/pending-health`,
`/api/stats/pricing-provenance`, plus:

| Endpoint | Purpose |
|----------|---------|
| `/api/stats/recent-requests` | Bounded recent-requests list (auth-gated) |
| `/api/stats/recent/{id}` | Request detail (auth-gated) |
| `/api/stats/cache-observability` | Cache counter coverage |
| `/api/stats/canonical-request-segmentation` | Segmentation stats |
| `/api/stats/cache-stability` | Cache stability |
| `/api/stats/request-shaping` | Operator-facing summary |
| `/api/stats/transcoding` | Transcoding stats (JSON) |
| `/api/stats/thinking` | Thinking/reasoning counters |
| `/api/stats/runtime` | Runtime metrics |
| `/api/stats/update` | Update checker state |
| `/api/backoffs` | Active upstream-derived account backoffs |
| `/api/network/diagnostics` | Outbound-client pool diagnostics |
| `/api/model-info` | Model info summary |
| `/api/model-info/{model_id}` | Model info detail |
| `/api/model-info/{model_id}/matches` | Match evidence |
| `/api/model-info/{model_id}/aliases` | Alias list |
| `/api/model-info/sources` | Source health |
| `/api/model-info/refresh` | Manual refresh |

## Key Invariants

- No raw prompts, tool outputs, system messages, request bodies, or auth headers in any card or JSON response
- `QuotaFairScorer` never consumes any dashboard/stats field
- Empty-DB responses are stable zero shapes
- Bad window parameters return HTTP 400, not 500
- Dashboard auth gate protects all routes
- Stats cache reduces database load (30s TTL)
