# Dashboard and Default-Install Performance Optimization Plan

Date: 2026-07-05
Status: handoff plan
Scope: EggPool default install performance, dashboard responsiveness under request load, and concurrency behavior on low-power Raspberry Pi / SBC devices.

## Context

The current default install targets lightweight LAN-hosted deployments, including Raspberry Pi-class hardware. Recent operator observation: the dashboard can load slowly while the proxy is under request load. A plausible first suspicion is Granian thread count. Repo inspection shows that the performance issue is broader and mostly due to default-install contention around the single ASGI worker, single runtime thread, and single SQLite/aiosqlite connection used by both request-path writes and dashboard analytics.

The optimization target for this plan is deliberately not maximum throughput on a large host. The target is a responsive default install on low-power devices while preserving a simple single-process operational model.

Important constraint: keep Granian `workers=1` for this line of work. Multi-worker mode would duplicate FastAPI app state, background task supervisors, catalog refresh tasks, provider client pools, in-memory health/routing state, and model-info services unless EggPool first grows explicit multi-process singleton ownership. This plan may tune runtime threads within the one worker, but it must not increase process workers.

## Current repo facts that shape the plan

`eggpool serve` hard-codes `workers=1` and passes `runtime_threads=config.server.threads` to Granian. That is the correct process model for the current architecture, because background services are owned in process-local app state.

`ServerConfig.threads` defaults to `1`. The config example currently does not show a `threads` key in `[server]`, so normal users do not see the runtime-thread knob.

`Database` serializes all SQL operations through a single connection lock per connection. This is necessary for correctness with the current aiosqlite wrapper, but it means dashboard reads can queue behind request writes when they share the primary connection.

`DatabaseConfig.worker_threads` already exists and is documented as a way to open a separate read-only stats connection when set to `2`. The code path in app startup sets `stats_db = db` by default, and only opens the read-only stats connection when `config.database.worker_threads > 1` and the database is not `:memory:`.

The dashboard overview route already fans out independent reads with `asyncio.gather`, but its own comment notes that the shared connection lock serializes per-query execution. The fan-out becomes useful only when dashboard reads have a separate stats connection or when the results are served from cache/rollups.

The cache/request-shaping dashboard and JSON handlers currently construct a fresh `StatsService(db)` from `request.app.state.db` instead of using `request.app.state.stats`. That bypasses the lifespan-wired `stats_db`, bypasses the long-lived dashboard cache on `app.state.stats`, and forces those analytics calls onto the primary request-path database connection. This is likely a direct contributor to slow dashboard/cache-page loads under load.

The default routing trace mode is `all`, with `include_score_components = true`. This creates diagnostic write pressure on every attempt. It is useful while debugging routing, but expensive as a default on low-power storage.

Several supervised periodic tasks run at 30s or 60s cadences. The supervisor supports initial delay and periodic scheduling, but the current registration pattern does not appear to systematically stagger short-cadence jobs. On a slow device, synchronized ticks can create minute-boundary DB lock bursts.

## Goals

1. Keep the default install simple and SBC-friendly.
2. Keep Granian process workers at exactly one.
3. Improve dashboard responsiveness under request load by moving dashboard analytics off the primary request-path DB connection wherever safe.
4. Reduce unnecessary write pressure in default and low-wear profiles.
5. Avoid behavior/capability regressions in routing, billing, request finalization, background tasks, and dashboard diagnostics.
6. Add observability that shows whether lock contention, expensive queries, rendering, or runtime-thread starvation is the active bottleneck.

## Non-goals

Do not add multi-process Granian workers in this pass.

Do not replace SQLite or aiosqlite.

Do not remove detailed diagnostics outright. Prefer profile-aware defaults and explicit operator knobs.

Do not weaken billing correctness, reservation finalization, crash recovery, model catalog refresh semantics, or routing fairness.

Do not make dashboard correctness depend on lossy metrics buffers. Rollups can accelerate charts and summaries, but correctness-critical request/account state remains on the durable request tables unless a query is explicitly approximate.

## Phase 1: Fix dashboard routes that bypass the stats connection

### Problem

`handle_cache`, request-shaping JSON, cache observability JSON, canonical segmentation JSON, compression observability JSON, compression runtime JSON, compression policy JSON, cache stability JSON, synthetic cache JSON, tuning JSON, and transcoding stats JSON create ad hoc `StatsService(db)` instances from `request.app.state.db`.

This bypasses the read-only `stats_db` connection and bypasses the in-memory dashboard cache held by `app.state.stats`. Under load, this pulls expensive dashboard analytics into the same SQLite connection lock used by request-path writes.

### Implementation

Update the dashboard route handlers in `src/eggpool/dashboard/routes.py` so they use the shared service:

```python
stats_service = request.app.state.stats
```

Do not instantiate `StatsService(db)` from the primary database inside dashboard routes unless the app has no stats service attached, which should be a defensive 503/degraded fallback rather than the normal path.

For handlers that currently need `serialize_transcoding_stats`, keep serialization local but fetch data from `request.app.state.stats`.

For `handle_cache`, keep `runtime_metrics = request.app.state.runtime_metrics`, but change all stats calls to the shared stats service.

### Acceptance criteria

All dashboard HTML and JSON stats routes use the lifespan-wired shared stats service or an explicitly documented fallback.

No route constructs a new `StatsService(request.app.state.db)` on the hot path.

When `[database].worker_threads = 2`, cache/request-shaping pages use the read-only stats connection.

Dashboard cache hit behavior remains valid because calls go through the long-lived `StatsService` instance.

Existing dashboard tests pass.

Add or update tests to assert that cache/request-shaping handlers use `app.state.stats` rather than a newly constructed stats service.

## Phase 2: Make the low-power default use a read-only stats connection

### Problem

The code already has the right mechanism for dashboard isolation, but default config leaves `[database].worker_threads = 1`. On file-backed SQLite, this makes dashboard reads compete with request writes.

### Implementation

Change `DatabaseConfig.worker_threads` default from `1` to `2` for normal file-backed deployments if this can be represented cleanly. If Pydantic static defaults cannot depend on the database path, keep the model default as `2` and preserve the existing app startup guard that prevents a second connection for `:memory:`.

Update `config.example.toml`:

```toml
[database]
# aiosqlite uses one worker thread per SQLite connection. The default opens
# one primary read/write connection plus one read-only stats/dashboard
# connection for file-backed SQLite. This keeps dashboard analytics from
# queuing behind request-path writes on Raspberry Pi-class installs.
worker_threads = 2
```

Document that `worker_threads = 1` is the minimum-footprint mode for extremely constrained devices or tests, while `2` is the recommended Pi/default-install profile.

Keep the upper bound at `2` for now. More read connections can increase lock and cache complexity without clear benefit on low-power hardware.

### Acceptance criteria

Fresh default config uses `worker_threads = 2`.

`:memory:` tests remain stable and do not attempt a second connection.

Runtime metrics expose whether stats DB is separate from primary DB.

Docs explain the memory/thread tradeoff: one extra SQLite connection and one extra aiosqlite worker thread in exchange for substantially better dashboard responsiveness under write load.

## Phase 3: Keep one Granian worker, expose a conservative runtime-thread knob

### Problem

The app currently defaults to one Granian runtime thread, and the example config does not show the knob. Raising process workers is inappropriate for the current app-state architecture, but a small runtime-thread increase may improve responsiveness when the single worker is multiplexing streaming proxy traffic, dashboard requests, and lightweight background work.

### Implementation

Keep this invariant:

```python
workers=1
```

Do not add a `[server].workers` config option in this pass. If a field already exists elsewhere, explicitly document that it is unsupported until multi-process singleton coordination exists.

Expose `threads` in `config.example.toml` under `[server]`:

```toml
# Granian runtime threads inside the single worker process. Keep workers=1.
# 1 is minimum footprint; 2 is usually a better default for Raspberry Pi 4/5
# and other SBC installs that serve dashboard traffic during active proxy use.
threads = 2
```

Decide whether to change `ServerConfig.threads` default from `1` to `2`. Recommended: set default to `2` unless benchmark results show measurable regressions on Pi Zero-class devices. Keep `1` documented as the minimum-footprint override.

Add startup logging that prints the effective process/concurrency profile:

```text
Granian profile: workers=1 runtime_threads=N database_worker_threads=M access_log=...
```

### Acceptance criteria

The serve path still uses exactly `workers=1`.

`config.example.toml` exposes `server.threads` with low-power guidance.

Docs explicitly explain why workers stay one.

Runtime status or startup logs make the effective thread settings visible.

No background task is duplicated.

## Phase 4: Stagger short-cadence background tasks

### Problem

Multiple periodic tasks run at 30s or 60s cadences. If they are all registered at startup with the same first-delay convention, they can wake together and briefly contend for SQLite on low-power devices.

Tasks to inspect and stagger include:

- `metrics_flush`
- `usage_window_refresh`
- `stale_request_finalizer`
- `health_disabled_models_prune`
- `model_info_canonical_backfill`
- `catalog_refresh` when the refresh interval is short
- any future short-cadence runtime cleanup jobs

### Implementation

Add deterministic initial offsets at registration time. Avoid random jitter that makes tests flaky. Use stable offsets by task name, capped to a safe fraction of the interval.

Preferred helper:

```python
def periodic_initial_offset(name: str, interval_s: float, *, max_fraction: float = 0.5) -> float:
    ...
```

The helper should return a deterministic float in `[0, interval_s * max_fraction]`, probably based on a small hash of the task name. For latency-sensitive tasks, use explicit offsets instead of hash-derived values.

Suggested explicit offsets:

- `metrics_flush`: keep near its existing cadence; offset 5s for a 30s interval.
- `usage_window_refresh`: 10s.
- `stale_request_finalizer`: 25s.
- `health_disabled_models_prune`: 40s.
- `model_info_canonical_backfill`: 50s.

Make sure first-run behavior remains acceptable. Do not delay safety-critical crash recovery; this plan only affects periodic follow-up tasks after startup recovery has completed.

### Acceptance criteria

Runtime dashboard shows short-cadence tasks with distinct next-run times after startup.

No test assumes all periodic tasks share exactly the same first next-run timestamp.

No task becomes falsely overdue due only to an initial offset.

Under synthetic load, DB lock wait spikes are lower at minute boundaries.

## Phase 5: Reduce default diagnostic write pressure without removing capability

### Problem

Routing traces are written for every attempt by default. On low-power devices with SQLite on microSD, this increases write volume and lock contention. The route diagnostics are valuable, but default installs need a balanced profile.

### Implementation options

Option A, conservative default docs only:

Keep `routing.trace.mode = "all"` but add a clearly documented low-power profile in `config.example.toml`:

```toml
[routing.trace]
mode = "sampled"
sample_rate = 0.05
include_score_components = false
```

Option B, preferred for default install:

Change the default to:

```toml
[routing.trace]
mode = "sampled"
sample_rate = 0.05
include_score_components = false
```

Then document how operators can restore full trace fidelity:

```toml
[routing.trace]
mode = "all"
include_score_components = true
```

If changing defaults is judged too behaviorally visible, implement a profile mechanism first:

- `eggpool onboard` asks for `balanced` vs `full-diagnostics`.
- `balanced` uses sampled traces and separate stats DB.
- `full-diagnostics` keeps all traces.

### Acceptance criteria

No billing, retry, quota, or finalization behavior depends on routing trace rows.

The dashboard degrades gracefully when trace data is sampled.

Docs explain that sampling affects diagnostics only.

Tests cover sampled/off trace modes and verify core routing still works.

## Phase 6: Prefer rollups and cache for heavy dashboard aggregates

### Problem

The stats service already has dashboard caching and rollup-backed paths, but some endpoints still run many direct aggregate queries over `requests`. On a busy Pi, full-window scans can dominate dashboard render time.

### Implementation

Audit `StatsService` methods used by overview, accounts, models, latency, reliability, routing, timeseries, bandwidth, and cache pages.

For each endpoint, classify as:

- exact and cheap: keep direct query.
- exact but frequent: use dashboard cache with bounded TTL.
- approximate/aggregate acceptable: use rollups when fresh, with direct-query fallback.
- debug trace: keep bounded by explicit `limit`.

Pay special attention to:

- TTFT percentiles in summary queries.
- routing distribution/exclusion breakdown over `routing_decisions`.
- compression/cache observability queries over `requests`.
- model stats with catalog-complete sparse row merging.
- recent events and recent operational events.

Where the stats service already has `use_cache`, make sure dashboard routes consistently pass `use_cache=True` for dashboard-rendered aggregate data. Keep API endpoints exact unless they are explicitly dashboard-specific JSON endpoints.

Add cache keys for request-shaping/cache page aggregates if they are called frequently by both the HTML page and JSON widgets.

### Acceptance criteria

Dashboard overview and cache pages avoid repeated direct scans within the cache TTL.

The dashboard cache remains bounded by `_DASHBOARD_CACHE_MAX_ENTRIES`.

API endpoints that are documented as exact remain exact.

Page render tests confirm stable output across cache miss and cache hit.

## Phase 7: Add observability for the actual bottleneck

### Problem

The repo tracks database lock contention counters, but dashboard operators need route-level and query-level visibility to distinguish database contention from render cost, runtime thread starvation, upstream load, and background task collisions.

### Implementation

Add low-overhead dashboard performance telemetry:

1. Per-dashboard-route render duration.
2. Optional per-stats-method duration histogram or rolling summary.
3. DB contention deltas per dashboard request: before/after `Database.contention_snapshot()` for primary DB and stats DB.
4. Runtime profile summary: Granian workers fixed at 1, runtime threads, database worker_threads, separate_stats_db boolean, access_log status, metrics write mode, routing trace mode.

Expose this in `/api/stats/runtime` and a compact runtime page section. Do not persist high-cardinality per-request dashboard timing rows by default; keep it in memory.

Possible payload:

```json
{
  "dashboard_runtime": {
    "recent_render_ms_p50": 42.1,
    "recent_render_ms_p95": 210.4,
    "slowest_recent_route": "/cache",
    "db_lock_wait_delta_ms_p95": 83.0,
    "separate_stats_db": true
  }
}
```

### Acceptance criteria

Operators can see whether dashboard slowness correlates with DB lock wait.

Runtime page shows whether the current config is using the low-power recommended profile.

Telemetry is in-memory and bounded.

Telemetry does not materially increase request-path overhead.

## Phase 8: Access-log and deployment profile cleanup

### Problem

`access_log = true` is helpful during setup but can add I/O noise under active proxy load. The example config also spreads performance knobs across server, database, routing, metrics, and dashboard sections without a single default-install explanation.

### Implementation

Add a documentation section in `docs/deployment.md` or a new `docs/performance.md`:

- Recommended Raspberry Pi default profile.
- Minimum-footprint profile.
- Full-diagnostics profile.
- Symptoms and knobs: slow dashboard, DB lock wait, high write volume, microSD wear, slow runtime page, stale background tasks.

Recommended Pi/default profile should include:

```toml
[server]
threads = 2
access_log = false  # optional after initial setup

[database]
worker_threads = 2
wal = true
synchronous = "NORMAL"

[metrics]
write_mode = "balanced"
flush_interval_s = 30

[routing.trace]
mode = "sampled"
sample_rate = 0.05
include_score_components = false
```

Keep the current full-diagnostics profile available:

```toml
[server]
access_log = true

[routing.trace]
mode = "all"
include_score_components = true
```

### Acceptance criteria

Operators can choose a profile without reading source code.

Docs are explicit that process workers remain one.

`config.example.toml` remains concise and does not become another phase-by-phase option dump.

## Phase 9: Regression and benchmark plan

### Unit and integration tests

Add tests for:

- Dashboard cache/request-shaping routes use `app.state.stats`.
- `worker_threads = 2` opens a read-only stats connection for file-backed SQLite.
- `worker_threads = 2` does not create a second connection for `:memory:`.
- `serve` still passes `workers=1` to Granian.
- `server.threads` is passed to `runtime_threads`.
- Periodic task offsets are deterministic.
- Runtime metrics expose separate_stats_db and effective concurrency profile.
- Routing trace sampled/off modes do not alter routing outcome.

### Manual checks

Run:

```bash
ruff check src tests
pyright
pytest
```

Then run focused tests if the suite is large:

```bash
pytest -m dashboard
pytest -m performance
pytest tests/test_dashboard*.py tests/test_runtime*.py tests/test_database*.py
```

### Low-power benchmark

Create a repeatable local benchmark script, preferably under `scripts/` or `benchmarks/`, that can run on a Raspberry Pi without external providers by using a stub upstream.

Benchmark matrix:

1. Current default: `threads=1`, `worker_threads=1`, trace all.
2. Stats isolation only: `threads=1`, `worker_threads=2`, trace all.
3. Runtime thread bump: `threads=2`, `worker_threads=2`, trace all.
4. Balanced low-power: `threads=2`, `worker_threads=2`, sampled traces, access log off.

Metrics:

- `/` p50/p95 render time while proxy requests are active.
- `/cache` p50/p95 render time while proxy requests are active.
- `/runtime` p50/p95 render time.
- request-path p50/p95 overhead through the proxy.
- DB max lock wait and cumulative lock wait deltas.
- RSS memory.
- CPU utilization.
- SQLite WAL size growth over 10 minutes.
- routing trace row volume.

Use a load level that resembles the real deployment: multiple long-lived streaming requests plus occasional dashboard refreshes. Avoid unrealistic desktop-class request floods as the primary benchmark.

### Acceptance criteria

The balanced low-power profile materially improves dashboard p95 latency under load versus current default.

Request-path latency does not regress materially.

RSS remains acceptable for Raspberry Pi-class devices.

No background task becomes duplicated or permanently overdue.

## Risks and mitigations

### Risk: second stats connection increases memory or file-handle footprint

Mitigation: keep max at 2; document `worker_threads = 1` minimum-footprint mode; confirm RSS in benchmark.

### Risk: read-only stats connection sees stale data during active writes

Mitigation: SQLite WAL readers see a consistent snapshot. Dashboard analytics can tolerate sub-second snapshot isolation. Correctness-critical writes remain on primary DB.

### Risk: runtime thread bump increases context switching on very small devices

Mitigation: benchmark Pi-class hardware. Keep `threads = 1` documented. If necessary, make onboarding choose minimum-footprint vs balanced.

### Risk: routing trace sampling reduces diagnostic visibility

Mitigation: make full diagnostics an explicit profile. Ensure dashboard labels sampled trace mode. Keep per-request bounded traces for recent requests where needed.

### Risk: background task staggering delays useful maintenance work

Mitigation: do not delay startup crash recovery or initial catalog load. Only stagger periodic maintenance ticks. Keep delays well below each task interval.

### Risk: dashboard cache returns mutable objects that renderers mutate

Mitigation: audit renderers. The service currently relies on renderers not mutating cached values. If any renderer mutates, copy at the mutation boundary or normalize to immutable-ish local structures.

## Suggested implementation order

1. Patch dashboard/cache/request-shaping routes to use `app.state.stats`.
2. Add tests for stats service reuse and separate stats DB use.
3. Change default `database.worker_threads` to 2 and update example config/docs.
4. Expose `server.threads` in example config; keep `workers=1`; consider default `threads=2` after quick benchmark.
5. Add deterministic periodic task offsets.
6. Add runtime/dashboard bottleneck telemetry.
7. Add or document low-power and full-diagnostics profiles.
8. Evaluate trace sampling default after benchmarks.
9. Run full tests and low-power benchmark matrix.

## Definition of done

The default install remains a single Granian worker process.

Dashboard pages use the read-only stats connection when available.

File-backed default installs use a separate stats DB connection unless explicitly configured otherwise.

The config and docs make the Raspberry Pi optimization profile obvious.

The runtime page reports enough information to diagnose whether dashboard slowness is database lock contention, render cost, or runtime saturation.

Background tasks remain healthy and no longer cluster all short-cadence DB work on the same second.

The request path, accounting, routing, background cleanup, and model catalog behavior are unchanged except for intentional diagnostic write-pressure reductions when a low-power profile is selected.
