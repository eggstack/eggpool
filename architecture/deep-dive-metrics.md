# Deep Dive: Metrics & Telemetry

Back to [Overview](overview.md)

## Purpose

Structured observability across three subsystems: metrics buffering, thinking/reasoning counters, and runtime process diagnostics.

## Module Structure

```
src/eggpool/metrics/
├── __init__.py
├── buffer.py              # Low-wear metrics buffer with periodic flush
├── thinking.py            # Thinking/reasoning decision counters
└── failure_effects.py     # Failure effect counters

src/eggpool/
├── event_loop_lag.py      # Event-loop lag monitor (~225 lines)
├── runtime_metrics.py     # Process/memory/DB metrics
└── runtime_dispatch.py    # Dispatch timing recorders
```

## Key Components

### Metrics Buffer (`metrics/buffer.py`)

Low-wear metrics buffer with periodic flush to SQLite. Designed for SBC deployments where heavyweight monitoring is unwanted.

- Bounded in-memory buffer
- Periodic flush to avoid per-request DB writes
- Process-owned — does not survive generation swaps

### Thinking Metrics (`metrics/thinking.py`)

`ThinkingMetricsCounter` tracks thinking/reasoning decision outcomes with low-cardinality labels:

**Labels:** protocol, decision, capability_status, provider_id

**Counters:**
- `requested` — thinking was requested by client
- `transcoded` — thinking fields were transcoded
- `dropped` — thinking fields were dropped
- `rejected` — thinking was rejected (capability mismatch)
- `unknown_capability` — provider capability unknown
- `unsupported_capability` — provider doesn't support thinking
- `budget_clamped` — budget was clamped to provider limits
- `stream_delta` — streaming thinking delta observed
- `response_block` — non-streaming thinking block observed

**API surfaces:**
- `GET /api/stats/thinking` — counter snapshot
- `/api/stats/runtime` includes `thinking_metrics`
- Dashboard overview page — Thinking/Reasoning stat card

**Request trace:** each thinking-related request carries a `thinking_trace` dict on `ProxyRequestContext` with fields: `requested`, `client_protocol`, `request_fields`, `requested_effort`, `resolved_budget_tokens`, `budget_clamped`, `capability_status`, `capability_source`, `upstream_protocol`, `upstream_fields`, `decision`. Serialized to `thinking_trace_json` on the `requests` table (migration 0039).

### Failure Effect Counters (`metrics/failure_effects.py`)

Records normalized failure effect counters for dashboard and diagnostics. Tracks retry outcomes, circuit transitions, and quarantine events.

### Event-Loop Lag Monitor (`event_loop_lag.py`)

Lightweight event-loop lag monitor (~225 lines) designed for SBC/Raspberry Pi deployments:

- Measures gap between expected and actual wake time on periodic callback
- Fixed-size sample buffer (`deque(maxlen=200)`)
- Single background task that sleeps between measurements
- No per-request allocations

**Snapshot output:**
```python
@dataclass(frozen=True, slots=True)
class EventLoopLagSnapshot:
    window_size: int
    sample_count: int
    avg_ms: float | None
    min_ms: float | None
    max_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
```

- Process-local (never persisted)
- Exposes only non-secret diagnostic data
- Cadence: 1 second default between measurements

### Runtime Metrics (`runtime_metrics.py`)

Gathers process topology, memory, background task state, database health, OS load average, and bounded dispatch-overhead distribution:

- Process info (PID, uptime, Python version)
- Memory usage (RSS, VMS)
- Background task state (running, pending, failed)
- Database health (connection status, WAL size)
- OS load average (`os.getloadavg` + normalized per-core)
- Dispatch overhead distribution (via `DispatchOverheadRecorder`)

Exposed via `/api/stats/runtime` and `eggpool runtime-status --json`.

### Dispatch Timing Recorders (`runtime_dispatch.py`)

Two distinct timing slices measuring EggPool-side latency:

**`DispatchOverheadRecorder`** (always-on):
- Coarse coordinator slice
- From `ProxyRequestContext.started_monotonic_ns` to just before `httpx.AsyncClient.send()`
- Bounded rolling-window distribution

**`LocalPreUpstreamRecorder`** (opt-in, detailed span sampling):
- Full EggPool-side window
- From `request_received_monotonic_ns` (ASGI handler entry) to just before upstream dispatch
- Covers: context_build, body parsing, validation, segmentation, compression, coordinator dispatch overhead

Both use monotonic/performance clocks. The two metrics are additive when detailed sampling is enabled.

## Configuration

```toml
[metrics]
# buffering and flush modes configured here

[event_loop_lag]
# cadence, window_size configurable
```

## Key Invariants

- Metrics are best-effort and process-local — never persisted except via flush
- Failed probes return `null` rather than raising
- `probe_errors` capped to 16 truncated entries
- `/api/stats/runtime` always auth-gated even with public dashboard
- Dispatch timing uses monotonic clocks — immune to wall-clock adjustments
- Thinking metrics are request-scoped — no cross-request aggregation in counters

## Related

- [deep-dive-dashboard.md](deep-dive-dashboard.md) — Dashboard metrics display
- [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) — How timing is captured in the coordinator
- [deep-dive-runtime.md](deep-dive-runtime.md) — Process model and generation lifecycle
