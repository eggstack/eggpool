# Plan: TTFT Stats + Provider Ping Probes

## Overview

Add per-provider/model time-to-first-token (TTFT) statistics and active provider health
ping probes. TTFT uses query-time aggregation on the existing `first_byte_ms` column.
Ping probes repurpose the existing catalog refresh GET /models call to measure provider
latency and record health data, eliminating the need for separate probe infrastructure.

---

## Phase 1: TTFT Statistics (query-time aggregation)

### 1.1 Stats Queries — `src/eggpool/stats/queries.py`

Add AVG/P50/P99 of `first_byte_ms` to existing aggregation queries. Filter by
`streamed=1` for true TTFT (non-streaming `first_byte_ms` measures time-to-headers).

**SQLite percentile pattern** — use a window-function subquery:

```sql
-- P50 via median approximation:
(SELECT AVG(sub.first_byte_ms) FROM (
  SELECT first_byte_ms,
    ROW_NUMBER() OVER (ORDER BY first_byte_ms) as rn,
    COUNT(*) OVER () as cnt
  FROM requests
  WHERE streamed = 1 AND first_byte_ms IS NOT NULL
    AND started_at >= ? AND started_at < ?
    AND provider_id = ?
) sub
WHERE sub.rn IN ((sub.cnt + 1) / 2, (sub.cnt + 2) / 2)
) as p50_ttft_ms
```

**Changes to existing functions:**

| Function | Change |
|----------|--------|
| `fetch_summary()` | Add `avg_ttft_ms`, `p50_ttft_ms`, `p99_ttft_ms` (streamed only, all providers) |
| `fetch_account_stats()` | Add same per-account |
| `fetch_model_stats()` | Add same per-model; add `provider_id` to GROUP BY and SELECT |
| `fetch_timeseries()` | Add per-bucket TTFT stats |

**New function:**

```python
async def fetch_provider_model_ttft(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Per-provider, per-model TTFT breakdown."""
    sql = """
    SELECT
        r.provider_id,
        r.model_id,
        COUNT(*) as request_count,
        COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)
            as avg_ttft_ms,
        -- P50 and P99 via subqueries (see pattern above)
        ...
    FROM requests r
    WHERE r.started_at >= ? AND r.started_at < ?
      AND r.first_byte_ms IS NOT NULL
    GROUP BY r.provider_id, r.model_id
    ORDER BY r.provider_id, request_count DESC
    """
```

### 1.2 Stats Service — `src/eggpool/stats/service.py`

Add methods:

```python
async def get_ttft_stats(self, time_range: TimeRange) -> dict[str, Any]:
    """Global TTFT summary (AVG/P50/P99)."""

async def get_provider_model_ttft(self, time_range: TimeRange) -> list[dict[str, Any]]:
    """Per-provider, per-model TTFT breakdown."""
```

Wire into existing `get_dashboard_overview()` to include TTFT summary cards.

### 1.3 Dashboard Rendering — `src/eggpool/dashboard/render.py`

**Overview page** — add TTFT cards alongside existing latency card:

```html
<section class="cards">
  <!-- existing cards -->
  <div class="card">
    <h3>Avg TTFT (streamed)</h3>
    <p class="metric">245ms</p>
    <p class="sub">P50: 180ms · P99: 890ms</p>
  </div>
</section>
```

**Models page** — add "Provider" and "Avg TTFT" columns to the models table.
Existing columns: Model, Requests, Errors, Input tokens, Output tokens, Cost, Avg latency.
New columns: Provider, Avg TTFT.

**New page: `/latency`** — dedicated latency breakdown page:

- `render_latency()` function
- Per-provider/model TTFT table (AVG, P50, P99, request count)
- Per-provider aggregate TTFT summary cards at top
- Period selector and theme support

### 1.4 Dashboard Routes — `src/eggpool/dashboard/routes.py`

- Add `/latency` route handler
- Wire TTFT data into overview, models, accounts pages
- Add "Latency" to nav items in `_render_nav()`

### 1.5 API Endpoint — `src/eggpool/api/stats.py`

Add `/api/stats/latency` endpoint:

```python
@router.get("/api/stats/latency")
async def handle_latency(request: Request) -> Response:
    """Per-provider/model TTFT breakdown."""
```

### 1.6 Navigation — `render.py` `_render_nav()`

Add "Latency" nav item between "Models" and "Events":

```python
items = [
    ("overview", "/?", "Overview"),
    ("accounts", "/accounts", "Accounts"),
    ("models", "/models", "Models"),
    ("latency", "/latency", "Latency"),       # NEW
    ("timeseries", "/timeseries", "Timeseries"),
    ("bandwidth", "/bandwidth", "Bandwidth"),
    ("events", "/events", "Events"),
]
```

---

## Phase 2: Provider Ping Probes (enhanced catalog refresh)

### Design: Ping = Enhanced GET /models

Instead of building separate probe infrastructure, enhance the existing catalog refresh
to record timing data. The GET /models call already hits each provider's API — we
measure latency during this call and persist results.

**Key insight:** The catalog refresh iterates per-account (each API key), but all
accounts on the same provider share one `httpx.AsyncClient`. We record per-account
ping data (to validate each API key) but also aggregate per-provider.

**Default interval:** Change from 3600s (1 hour) to 300s (5 minutes).

### 2.1 Migration — `src/eggpool/db/schema/0018_provider_pings.sql`

```sql
CREATE TABLE provider_pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    probed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    latency_ms INTEGER,
    status_code INTEGER,
    error TEXT,
    model_count INTEGER DEFAULT 0
);

CREATE INDEX idx_provider_pings_provider ON provider_pings(provider_id);
CREATE INDEX idx_provider_pings_probed ON provider_pings(probed_at);
CREATE INDEX idx_provider_pings_provider_probed
    ON provider_pings(provider_id, probed_at);
```

**Columns:**
- `provider_id` — which provider was probed
- `account_name` — which API key was used (validates the key works)
- `latency_ms` — round-trip time for GET /models
- `status_code` — HTTP status (200 = success, 401 = bad key, etc.)
- `error` — error message if request failed (timeout, connection error, etc.)
- `model_count` — number of models discovered (0 on failure)

### 2.2 Ping Repository — `src/eggpool/db/repositories.py`

Add `PingRepository` class:

```python
class PingRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_ping(
        self,
        provider_id: str,
        account_name: str,
        latency_ms: int | None,
        status_code: int | None,
        error: str | None,
        model_count: int = 0,
    ) -> None:
        """Record a single ping result."""

    async def get_recent_pings(
        self,
        provider_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get recent pings, optionally filtered by provider."""

    async def get_provider_ping_summary(
        self,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Per-provider aggregate: avg/min/max latency, success rate, last ping."""

    async def get_ping_timeseries(
        self,
        provider_id: str,
        start: str,
        end: str,
        bucket: str = "hour",
    ) -> list[dict[str, Any]]:
        """Per-bucket ping latency trend for one provider."""

    async def cleanup_old_pings(self, retain_days: int = 7) -> int:
        """Delete pings older than retention period."""
```

### 2.3 Enhanced Fetcher — `src/eggpool/catalog/fetcher.py`

Modify `fetch_models_for_account()` to return timing data:

```python
@dataclass
class FetchResult:
    """Result of a catalog fetch including timing data."""
    response: dict[str, Any]          # raw JSON response (empty dict on failure)
    latency_ms: int                   # round-trip time
    status_code: int | None           # HTTP status (None on connection error)
    error: str | None                 # error message (None on success)
    model_count: int                  # number of models in response

async def fetch_models_for_account(
    client: httpx.AsyncClient,
    api_key: str,
    account_name: str,
    models_method: str = "GET",
    models_path: str = "/models",
) -> FetchResult:
    """Fetch models with timing data for ping measurement."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    start = time.monotonic()
    try:
        if models_method.upper() == "POST":
            response = await client.post(models_path, headers=headers, json={})
        else:
            response = await client.get(models_path, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        status_code = response.status_code
        response.raise_for_status()
        data = response.json()
        model_count = len(data.get("data", []))
        return FetchResult(
            response=data,
            latency_ms=latency_ms,
            status_code=status_code,
            error=None,
            model_count=model_count,
        )
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Account %r: HTTP %d fetching models",
            account_name,
            exc.response.status_code,
        )
        return FetchResult(
            response={},
            latency_ms=latency_ms,
            status_code=exc.response.status_code,
            error=f"HTTP {exc.response.status_code}",
            model_count=0,
        )
    except httpx.RequestError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Account %r: request error fetching models: %s",
            account_name,
            exc,
        )
        return FetchResult(
            response={},
            latency_ms=latency_ms,
            status_code=None,
            error=str(exc),
            model_count=0,
        )
```

**Backward compatibility:** Existing callers that do `raw_response = await fetch_...()`
and check `if not raw_response:` must be updated. The `FetchResult.response` field
replaces the old return value. Update callers to check `result.response` instead.

### 2.4 Catalog Service Integration — `src/eggpool/catalog/service.py`

Modify `CatalogService` to accept a `PingRepository` and record pings:

```python
class CatalogService:
    def __init__(
        self,
        config: ModelsConfig,
        registry: AccountRegistry,
        db: Database,
        client_pool: ProviderClientPool | None = None,
        httpx_client: httpx.AsyncClient | None = None,
        ping_repo: PingRepository | None = None,    # NEW
    ) -> None:
        self._ping_repo = ping_repo
        ...
```

Modify `_fetch_and_process_account()` to record ping data:

```python
async def _fetch_and_process_account(self, ...):
    result = await fetch_models_for_account(...)
    # Record ping data
    if self._ping_repo is not None:
        await self._ping_repo.record_ping(
            provider_id=provider_id,
            account_name=account_name,
            latency_ms=result.latency_ms,
            status_code=result.status_code,
            error=result.error,
            model_count=result.model_count,
        )
    if not result.response:
        return
    models = normalize_models(result.response)
    # ... rest of processing
```

### 2.5 Refresh Interval Config — `src/eggpool/models/config.py`

Change default and add ping-specific config:

```python
class ModelsConfig(BaseModel):
    refresh_interval_s: int = Field(default=300, ge=0)  # Changed: 5 min (was 3600)
    expose_mode: Literal["union", "intersection", "healthy_union"] = "union"
    startup_refresh: bool = True
    stale_after_s: int = Field(default=7200, gt=0)
    allow_stale_catalog: bool = True
    ping_retain_days: int = Field(default=7, ge=1)     # NEW: ping data retention
```

### 2.6 App Wiring — `src/eggpool/app.py`

In the `lifespan()` function:

1. Create `PingRepository` alongside other repositories
2. Pass it to `CatalogService`
3. Register cleanup in the retention task

```python
# After existing repository creation:
ping_repo = PingRepository(db)

# Modify CatalogService construction:
catalog = CatalogService(
    config.models,
    registry,
    db,
    client_pool=client_pool,
    ping_repo=ping_repo,           # NEW
)

# In retention cleanup task:
async def _retention_cleanup() -> None:
    while True:
        await asyncio.sleep(3600)
        await cleanup_old_requests(db, config.dashboard.retain_request_stats_days)
        await cleanup_old_events(db, config.dashboard.retain_event_days)
        await ping_repo.cleanup_old_pings(config.models.ping_retain_days)  # NEW
        await reconcile_expired_reservations(...)
```

### 2.7 Retention Cleanup — `src/eggpool/background/cleanup.py`

Add `cleanup_old_pings()`:

```python
async def cleanup_old_pings(
    db: Database,
    retain_days: int = 7,
) -> int:
    """Delete provider pings older than the retention period."""
    async with db.transaction():
        count = await db.execute_write(
            """
            DELETE FROM provider_pings
            WHERE probed_at < datetime('now', ? || ' days')
            """,
            (f"-{retain_days}",),
        )
    if count > 0:
        logger.info("Deleted %d old provider pings (retention=%d days)", count, retain_days)
    return count
```

### 2.8 Schema Check — `scripts/check_database.py`

```python
EXPECTED_SCHEMA_VERSION = 18  # was 17

REQUIRED_TABLES: frozenset[str] = frozenset({
    ...,
    "provider_pings",   # NEW
})

REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    ...,
    "provider_pings": frozenset({
        "id", "provider_id", "account_name", "probed_at",
        "latency_ms", "status_code", "error", "model_count",
    }),
}
```

---

## Phase 3: Ping Dashboard

### 3.1 Ping Stats Queries — `src/eggpool/stats/queries.py`

```python
async def fetch_ping_summary(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Per-provider ping summary: avg/min/max latency, success rate, last ping."""
    sql = """
    SELECT
        pp.provider_id,
        COUNT(*) as ping_count,
        COALESCE(AVG(pp.latency_ms), 0) as avg_latency_ms,
        COALESCE(MIN(pp.latency_ms), 0) as min_latency_ms,
        COALESCE(MAX(pp.latency_ms), 0) as max_latency_ms,
        SUM(CASE WHEN pp.error IS NULL THEN 1 ELSE 0 END) as success_count,
        SUM(CASE WHEN pp.error IS NOT NULL THEN 1 ELSE 0 END) as failure_count,
        ROUND(
            100.0 * SUM(CASE WHEN pp.error IS NULL THEN 1 ELSE 0 END) / COUNT(*),
            1
        ) as success_rate,
        MAX(pp.probed_at) as last_ping_at,
        (SELECT pp2.latency_ms FROM provider_pings pp2
         WHERE pp2.provider_id = pp.provider_id
         ORDER BY pp2.probed_at DESC LIMIT 1) as last_latency_ms,
        (SELECT pp3.model_count FROM provider_pings pp3
         WHERE pp3.provider_id = pp.provider_id
         ORDER BY pp3.probed_at DESC LIMIT 1) as last_model_count
    FROM provider_pings pp
    WHERE pp.probed_at >= ? AND pp.probed_at < ?
    GROUP BY pp.provider_id
    ORDER BY pp.provider_id
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_ping_timeseries(
    db: Database,
    provider_id: str,
    start: str,
    end: str,
    bucket: str = "hour",
) -> list[dict[str, Any]]:
    """Per-bucket ping latency trend for one provider."""
    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    sql = f"""
    SELECT
        strftime(?, pp.probed_at) as bucket,
        COUNT(*) as ping_count,
        COALESCE(AVG(pp.latency_ms), 0) as avg_latency_ms,
        COALESCE(MIN(pp.latency_ms), 0) as min_latency_ms,
        COALESCE(MAX(pp.latency_ms), 0) as max_latency_ms,
        SUM(CASE WHEN pp.error IS NULL THEN 1 ELSE 0 END) as success_count,
        SUM(CASE WHEN pp.error IS NOT NULL THEN 1 ELSE 0 END) as failure_count
    FROM provider_pings pp
    WHERE pp.provider_id = ?
      AND pp.probed_at >= ? AND pp.probed_at < ?
    GROUP BY bucket
    ORDER BY bucket
    """
    rows = await db.fetch_all(sql, (fmt, provider_id, _format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_ping_recent(
    db: Database,
    provider_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Most recent pings, optionally filtered by provider."""
    params: list[Any] = []
    provider_filter = ""
    if provider_id:
        provider_filter = " WHERE pp.provider_id = ?"
        params.append(provider_id)
    params.append(limit)
    sql = f"""
    SELECT
        pp.provider_id,
        pp.account_name,
        pp.probed_at,
        pp.latency_ms,
        pp.status_code,
        pp.error,
        pp.model_count
    FROM provider_pings pp
    {provider_filter}
    ORDER BY pp.probed_at DESC
    LIMIT ?
    """
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]
```

### 3.2 Ping Stats Service — `src/eggpool/stats/service.py`

```python
async def get_ping_summary(self, time_range: TimeRange) -> list[dict[str, Any]]:
    return await fetch_ping_summary(self._db, time_range.start_str(), time_range.end_str())

async def get_ping_timeseries(
    self, provider_id: str, time_range: TimeRange, bucket: str = "hour"
) -> list[dict[str, Any]]:
    return await fetch_ping_timeseries(
        self._db, provider_id, time_range.start_str(), time_range.end_str(), bucket
    )

async def get_ping_recent(
    self, provider_id: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    return await fetch_ping_recent(self._db, provider_id, limit)
```

### 3.3 Overview Page — Add Provider Health Section

On the overview page (`render_overview()`), add a "Provider Health" section after
the bandwidth heatmap:

```html
<section class="panel">
  <h3>Provider Health</h3>
  <table class="data">
    <thead><tr>
      <th>Provider</th>
      <th>Status</th>
      <th>Avg Latency</th>
      <th>Last Ping</th>
      <th>Models</th>
      <th>Success Rate</th>
    </tr></thead>
    <tbody>
      <tr>
        <td>opencode-go</td>
        <td class="healthy">healthy</td>
        <td>142ms</td>
        <td>2 min ago</td>
        <td>47</td>
        <td>100%</td>
      </tr>
    </tbody>
  </table>
</section>
```

### 3.4 Ping Page — `src/eggpool/dashboard/render.py`

New `render_pings()` function:

- Provider health summary cards at top (per-provider: status, avg latency, success rate)
- Per-provider ping latency timeseries (sparkline or table)
- Recent pings table: Provider, Account, Time, Latency, Status, Models, Error
- Period selector and theme support

### 3.5 Ping Routes — `src/eggpool/dashboard/routes.py`

Add `/pings` route:

```python
@router.get("/pings")
async def pings(request: Request) -> Response:
    """Provider ping health page."""
    period = request.query_params.get("period", "24h")
    theme = request.query_params.get("theme")
    time_range = _resolve_period(period)
    ping_summary = await stats.get_ping_summary(time_range)
    recent_pings = await stats.get_ping_recent(limit=50)
    # ... render
```

### 3.6 Ping API — `src/eggpool/api/stats.py`

```python
@router.get("/api/stats/pings")
async def handle_pings(request: Request) -> Response:
    """Provider ping statistics."""

@router.get("/api/stats/pings/{provider_id}")
async def handle_provider_pings(request: Request, provider_id: str) -> Response:
    """Ping timeseries for one provider."""
```

### 3.7 Navigation — Add "Pings" Nav Item

```python
items = [
    ("overview", "/?", "Overview"),
    ("accounts", "/accounts", "Accounts"),
    ("models", "/models", "Models"),
    ("latency", "/latency", "Latency"),
    ("pings", "/pings", "Pings"),              # NEW
    ("timeseries", "/timeseries", "Timeseries"),
    ("bandwidth", "/bandwidth", "Bandwidth"),
    ("events", "/events", "Events"),
]
```

---

## Phase 4: Tests

### 4.1 TTFT Stats Tests — `tests/unit/test_stats_queries.py`

- Test `fetch_summary()` includes TTFT fields
- Test `fetch_model_stats()` includes provider_id and TTFT
- Test `fetch_provider_model_ttft()` returns correct per-provider/model breakdown
- Test P50/P99 calculation with known data
- Test streamed-only filtering (non-streamed excluded from TTFT)

### 4.2 Ping Repository Tests — `tests/unit/test_ping_repository.py`

- Test `record_ping()` inserts correctly
- Test `get_provider_ping_summary()` aggregates correctly
- Test `get_ping_timeseries()` buckets correctly
- Test `cleanup_old_pings()` respects retention

### 4.3 Fetcher Tests — `tests/unit/test_catalog_fetcher.py`

- Test `FetchResult` dataclass fields
- Test timing measurement (latency_ms > 0 on success)
- Test error capture (status_code, error message on HTTP error)
- Test connection error handling
- Test model_count extraction

### 4.4 Catalog Service Tests — `tests/unit/test_catalog_service.py`

- Test ping recording during refresh (mock PingRepository)
- Test ping recorded on failure (timeout, HTTP error)
- Test model_count recorded correctly

### 4.5 Dashboard Render Tests — `tests/unit/test_dashboard_render.py`

- Test `render_latency()` produces valid HTML
- Test `render_pings()` produces valid HTML
- Test TTFT cards on overview page
- Test provider health section on overview
- Test ping page with empty data

### 4.6 Integration Tests — `tests/integration/`

- Test full catalog refresh → ping recording → stats query flow
- Test ping data appears in dashboard after refresh

---

## File Change Summary

| File | Action | Phase |
|------|--------|-------|
| `src/eggpool/stats/queries.py` | Add TTFT aggregation + ping stats queries | 1, 3 |
| `src/eggpool/stats/service.py` | Add `get_ttft_stats()`, `get_ping_*()` methods | 1, 3 |
| `src/eggpool/dashboard/render.py` | Add TTFT cards, latency page, ping page, provider health section | 1, 3 |
| `src/eggpool/dashboard/routes.py` | Add `/latency`, `/pings` routes, wire TTFT/ping data | 1, 3 |
| `src/eggpool/api/stats.py` | Add `/api/stats/latency`, `/api/stats/pings` endpoints | 1, 3 |
| `src/eggpool/catalog/fetcher.py` | Add `FetchResult` dataclass, timing measurement | 2 |
| `src/eggpool/catalog/service.py` | Accept `PingRepository`, record pings during refresh | 2 |
| `src/eggpool/db/schema/0018_provider_pings.sql` | New migration | 2 |
| `src/eggpool/db/repositories.py` | Add `PingRepository` | 2 |
| `src/eggpool/background/cleanup.py` | Add `cleanup_old_pings()` | 2 |
| `src/eggpool/models/config.py` | Change default `refresh_interval_s` to 300, add `ping_retain_days` | 2 |
| `src/eggpool/app.py` | Wire `PingRepository`, pass to catalog, update retention task | 2 |
| `config.example.toml` | Update refresh interval docs, add ping config | 2 |
| `scripts/check_database.py` | Bump schema to 18, add `provider_pings` table | 2 |
| `tests/unit/test_stats_queries.py` | TTFT query tests | 4 |
| `tests/unit/test_ping_repository.py` | Ping repository tests | 4 |
| `tests/unit/test_catalog_fetcher.py` | Fetcher timing tests | 4 |
| `tests/unit/test_catalog_service.py` | Ping recording tests | 4 |
| `tests/unit/test_dashboard_render.py` | Latency/ping page tests | 4 |
| `tests/integration/test_ping_e2e.py` | End-to-end ping flow test | 4 |

---

## Implementation Order

1. **Phase 1.1-1.2**: Stats queries and service for TTFT (add AVG/P50/P99 to existing queries)
2. **Phase 1.3-1.6**: Dashboard rendering, routes, API, nav for TTFT
3. **Phase 2.1-2.2**: Migration and PingRepository
4. **Phase 2.3-2.4**: Enhanced fetcher with timing + catalog service integration
5. **Phase 2.5-2.8**: Config, app wiring, retention, schema check
6. **Phase 3.1-3.7**: Ping stats queries, dashboard, routes, API
7. **Phase 4**: Tests for all phases

---

## Design Decisions

1. **TTFT = `first_byte_ms WHERE streamed=1`**: Non-streaming first_byte_ms measures
   time-to-headers, not first token. Filter to streamed requests for true TTFT.

2. **Ping = catalog refresh**: No separate probe infrastructure. The GET /models call
   already validates connectivity, API key, and discovers models. Adding timing
   measurement to this existing call gives us ping data for free.

3. **Per-account pings**: Each API key is validated independently. A provider might
   have multiple accounts — if one key fails (401), others might still work.

4. **Provider-level aggregation**: For dashboard display, aggregate ping data by
   provider_id. Per-account detail is available but provider-level is the primary view.

5. **5-minute default interval**: Balances freshness with API rate limits. Configurable
   via `refresh_interval_s`. The existing `stale_after_s` (2h) still governs catalog
   staleness enforcement.

6. **7-day ping retention**: Ping data is high-frequency (every 5 min × N providers).
   7 days at 5-min intervals = ~2000 rows per provider. Lightweight.

7. **P50/P99 via window functions**: SQLite supports ROW_NUMBER() and COUNT() OVER().
   The median approximation using AVG of two middle rows is standard for even-count
   datasets. For P99, use the value at CEIL(0.99 * count).
