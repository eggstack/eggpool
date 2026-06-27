# Runtime Dispatch Overhead and Load Metrics Implementation Plan

## Objective

Simplify the Runtime dashboard so it shows the operational signals that matter most on small deployments, especially Raspberry Pi and other SBC targets, and add a lightweight in-memory metric for EggPool-local dispatch overhead.

The target Runtime page should stop using prime card space for static or rarely actionable process topology fields. It should keep the live active-thread count, add a load-average card, add a dispatch-overhead card, and retain high-value resource cards such as RSS memory and open file descriptors.

This is a dashboard and runtime-observability change only. It should not alter routing behavior, provider behavior, durable request accounting, quota accounting, retry semantics, or database schema.

## Current state

The Runtime page is rendered by `render_runtime()` in `src/eggpool/dashboard/render.py`. The dashboard route `handle_runtime()` in `src/eggpool/dashboard/routes.py` calls `request.app.state.runtime_metrics.snapshot()` and passes the resulting snapshot into `render_runtime()`.

`RuntimeMetricsService` in `src/eggpool/runtime_metrics.py` currently returns these top-level snapshot sections:

- `server`
- `memory`
- `processes`
- `background_tasks`
- `db`
- `routing_runtime`
- `metrics_buffer`
- `outbound_client`
- `provider_client_pool`
- `dns_cache`
- `probe_errors`

The current Runtime page renders two thread-related cards:

- `Threads`: the configured server thread count, from `server.configured_server_threads`.
- `Threads (active)`: the live Python thread count, from `memory.thread_count`, sampled through `threading.active_count()`.

It also renders a `Processes` card from `processes.eggpool_process_count` with expected count in the subtitle.

The desired cleanup is to remove the configured-thread card from the normal card layout, stop rendering the process-count card in the normal layout, keep only the live active-thread count, and use the freed space for `Load average` and `Dispatch overhead`.

## Definitions

### Dispatch overhead

`dispatch_overhead` means the elapsed time from EggPool accepting/constructing a proxy request context to the moment EggPool begins the upstream provider send.

It must exclude:

- upstream DNS/TCP/TLS/provider latency after the send begins,
- provider time-to-first-token or time-to-first-byte,
- streaming duration,
- response body read time,
- finalization and metrics write time after upstream response handling.

It should include:

- request validation before dispatch,
- account eligibility checks,
- route scoring,
- request/reservation/attempt persistence,
- routing-decision persistence,
- in-memory active request and quota reservation updates,
- any scheduler/lock delay before upstream send begins.

This is intentionally an EggPool-local data-plane overhead metric. It is not a provider latency metric.

### Load average

`load_average` means operating-system load average when available, preferably the 1-minute value as the primary card metric. On Linux this comes from `os.getloadavg()`. The snapshot should also expose the 5-minute and 15-minute values plus CPU count and normalized load.

Normalized load should be computed as:

```text
normalized_load_1m = load_1m / cpu_count
```

When CPU count is unknown or zero, normalized load should be `None`.

## Design constraints

Keep this lightweight. Do not persist dispatch-overhead samples to SQLite. Do not create a migration. Do not add a new dependency.

Use monotonic timing. Do not use wall-clock timestamps for the dispatch-overhead measurement because wall-clock time can jump under NTP or manual clock adjustment.

Keep the dispatch-overhead window bounded. A `deque(maxlen=100)` is sufficient.

Make probes best-effort. Runtime metrics should not raise to the caller. If load sampling or dispatch-overhead snapshotting fails, the affected field should be omitted or populated with `None`, and a bounded probe error should be appended to `probe_errors` only when the failure is actionable.

Keep process-count collection available internally for now. The normal card can be removed, but `_snapshot_processes()` can remain because process-count anomalies may still be useful during debugging. If `process_count_warning` is true, the renderer may show a warning panel or compact warning card later; this plan does not require that warning UI in the first pass.

## Implementation phases

### Phase 1: Add a process-local dispatch-overhead recorder

Create a small class in a focused module, preferably `src/eggpool/runtime_dispatch.py` or inside `src/eggpool/runtime_metrics.py` if the project prefers fewer modules.

Recommended API:

```python
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DispatchOverheadSnapshot:
    window_size: int
    sample_count: int
    avg_ms: float | None
    min_ms: float | None
    max_ms: float | None
    p50_ms: float | None
    p95_ms: float | None


class DispatchOverheadRecorder:
    def __init__(self, window_size: int = 100) -> None:
        self._samples_ns: deque[int] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._window_size = window_size

    @property
    def window_size(self) -> int:
        return self._window_size

    def record_ns(self, elapsed_ns: int) -> None:
        if elapsed_ns < 0:
            return
        with self._lock:
            self._samples_ns.append(int(elapsed_ns))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples_ns)
        if not samples:
            return {
                "window_size": self._window_size,
                "sample_count": 0,
                "avg_ms": None,
                "min_ms": None,
                "max_ms": None,
                "p50_ms": None,
                "p95_ms": None,
            }
        samples.sort()
        count = len(samples)
        avg_ns = sum(samples) / count

        def percentile(p: float) -> float:
            # Nearest-rank or simple index percentile is fine for n=100.
            index = min(count - 1, max(0, int(round((count - 1) * p))))
            return samples[index] / 1_000_000

        return {
            "window_size": self._window_size,
            "sample_count": count,
            "avg_ms": avg_ns / 1_000_000,
            "min_ms": samples[0] / 1_000_000,
            "max_ms": samples[-1] / 1_000_000,
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
        }
```

Notes:

- `threading.Lock` is acceptable here because the critical section is tiny and the app can have thread interaction from runtime sampling and request handling.
- Do not await inside the lock.
- Do not store request IDs, model IDs, account names, bodies, headers, IP addresses, or other sensitive metadata. This recorder should store only numeric durations.
- Use nanoseconds internally to avoid precision loss and convert to milliseconds only at snapshot/render time.

### Phase 2: Attach the recorder to application state and coordinator

Instantiate the recorder during lifespan setup in `src/eggpool/app.py` before constructing `RequestCoordinator`.

Recommended placement:

- after router creation and routing configuration,
- before `RequestCoordinator(...)`,
- before `RuntimeMetricsService(...)` so the runtime service can receive the same recorder instance.

Example shape:

```python
from eggpool.runtime_dispatch import DispatchOverheadRecorder

# ... during lifespan setup

dispatch_overhead_recorder = DispatchOverheadRecorder(window_size=100)
app.state.dispatch_overhead_recorder = dispatch_overhead_recorder
```

Pass it into `RequestCoordinator`:

```python
coordinator = RequestCoordinator(
    ...,
    dispatch_overhead_recorder=dispatch_overhead_recorder,
)
```

Pass it into `RuntimeMetricsService`:

```python
app.state.runtime_metrics = RuntimeMetricsService(
    ...,
    dispatch_overhead_recorder=dispatch_overhead_recorder,
)
```

Update `RequestCoordinator.__init__()` to accept the optional recorder:

```python
def __init__(..., dispatch_overhead_recorder: Any | None = None) -> None:
    ...
    self._dispatch_overhead_recorder = dispatch_overhead_recorder
```

Avoid importing the concrete recorder type into the coordinator unless type-checking ergonomics are important. A duck-typed object with `record_ns(int)` is sufficient and avoids coupling request orchestration to dashboard internals.

### Phase 3: Measure dispatch overhead at the upstream-send boundary

The dispatch-overhead sample should be recorded immediately before EggPool begins the upstream provider send.

There are two upstream execution paths in `src/eggpool/request/coordinator.py`:

- `_execute_non_streaming()`
- `_execute_streaming()`

Both build upstream headers/body, resolve the upstream URL, select the HTTP client, build an `httpx` request, then call `client.send(...)` or equivalent streaming send. Instrument both paths.

Use the existing `ProxyRequestContext.started_monotonic` if sufficient, or add a new `started_perf_ns` field to `ProxyRequestContext` for nanosecond precision.

Recommended robust option:

```python
@dataclass
class ProxyRequestContext:
    ...
    started_monotonic_ns: int = field(default_factory=time.perf_counter_ns)
```

Then in both upstream paths:

```python
if self._dispatch_overhead_recorder is not None:
    self._dispatch_overhead_recorder.record_ns(
        time.perf_counter_ns() - context.started_monotonic_ns
    )
```

Place this immediately before the upstream `client.send(...)` call. Do not place it after `client.send(...)`, because `client.send()` includes upstream connection establishment and response-header wait in the non-streaming path.

For the existing non-streaming code, the instrumentation should be before:

```python
connect_start = time.monotonic()
response = await client.send(upstream_request, stream=True)
```

The intended shape is:

```python
if self._dispatch_overhead_recorder is not None:
    self._dispatch_overhead_recorder.record_ns(
        time.perf_counter_ns() - context.started_monotonic_ns
    )
connect_start = time.monotonic()
response = await client.send(upstream_request, stream=True)
```

For streaming, apply the same pattern immediately before the streaming upstream send begins.

Ensure one sample is recorded per actual upstream attempt, not per client request. This is preferable because retries create additional routing/selection/dispatch work and each upstream attempt has its own pre-dispatch overhead. The dashboard label should make this clear by using a subtitle such as `last 100 upstream attempts`.

If the implementation strongly prefers one sample per client request instead, record only on `attempt_num == 1`. The attempt-level design is recommended because it reflects actual router work under failover.

### Phase 4: Add dispatch-overhead and load snapshots to RuntimeMetricsService

Update `RuntimeMetricsService.__init__()` to accept `dispatch_overhead_recorder: Any | None = None` and store it.

Add a new top-level snapshot section:

```python
result["dispatch_overhead"] = self._snapshot_dispatch_overhead(probe_errors)
```

Recommended helper:

```python
def _snapshot_dispatch_overhead(self, probe_errors: list[str]) -> dict[str, Any]:
    if self._dispatch_overhead_recorder is None:
        return {
            "window_size": 100,
            "sample_count": 0,
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
            "p50_ms": None,
            "p95_ms": None,
        }
    try:
        return self._dispatch_overhead_recorder.snapshot()
    except Exception as exc:
        probe_errors.append(_truncate_probe_error(f"Dispatch overhead snapshot failed: {exc}"))
        return {"error": str(exc)}
```

Add a new top-level load section:

```python
result["load"] = self._snapshot_load(probe_errors)
```

Recommended helper:

```python
def _snapshot_load(self, probe_errors: list[str]) -> dict[str, Any]:
    cpu_count = os.cpu_count()
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        return {
            "available": False,
            "cpu_count": cpu_count,
            "load_1m": None,
            "load_5m": None,
            "load_15m": None,
            "normalized_1m": None,
            "normalized_5m": None,
            "normalized_15m": None,
        }

    def norm(value: float) -> float | None:
        if not cpu_count or cpu_count <= 0:
            return None
        return value / cpu_count

    return {
        "available": True,
        "cpu_count": cpu_count,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
        "normalized_1m": norm(load_1m),
        "normalized_5m": norm(load_5m),
        "normalized_15m": norm(load_15m),
    }
```

Ordering in `snapshot()` should put these near other process-level diagnostics:

```python
result["server"] = ...
result["memory"] = ...
result["load"] = ...
result["processes"] = ...
...
result["dispatch_overhead"] = ...
```

Keeping `processes` in the JSON is fine even if the page no longer renders it as a normal card.

### Phase 5: Update Runtime dashboard rendering

Modify `render_runtime()` in `src/eggpool/dashboard/render.py`.

Input extraction:

```python
load = _as_dict(snapshot.get("load"))
dispatch = _as_dict(snapshot.get("dispatch_overhead"))
```

Remove the configured thread card from `server_cards`:

- Remove `threads = server.get("configured_server_threads", "—")` unless used elsewhere.
- Remove the `<div class="card"><h3>Threads</h3>...configured server threads...</div>` block.

Remove the normal `Processes` card from `memory_cards`:

- Keep process warning variables only if a warning is rendered elsewhere.
- Do not include `<h3>Processes</h3>` in the normal cards.

Keep the active thread card, but rename it to `Active threads` rather than `Threads (active)`.

Add a load-average card.

Recommended formatting:

```python
load_available = bool(load.get("available", False))
load_1m = load.get("load_1m")
load_5m = load.get("load_5m")
load_15m = load.get("load_15m")
norm_1m = load.get("normalized_1m")
cpu_count = load.get("cpu_count")

if load_available and load_1m is not None:
    load_metric = f"{float(load_1m):.2f}"
    if norm_1m is not None:
        load_sub = f"{float(norm_1m):.2f}/core · {format_int(cpu_count)} CPUs"
    else:
        load_sub = f"5m {float(load_5m):.2f} · 15m {float(load_15m):.2f}"
else:
    load_metric = "—"
    load_sub = "load average unavailable"
```

Add a dispatch-overhead card.

Recommended formatting:

```python
avg_dispatch_ms = dispatch.get("avg_ms")
p95_dispatch_ms = dispatch.get("p95_ms")
sample_count = dispatch.get("sample_count", 0)
window_size = dispatch.get("window_size", 100)

if avg_dispatch_ms is None:
    dispatch_metric = "—"
else:
    dispatch_metric = format_latency(avg_dispatch_ms)

if p95_dispatch_ms is None:
    dispatch_sub = f"last {format_int(sample_count)} / {format_int(window_size)} attempts"
else:
    dispatch_sub = (
        f"p95 {format_latency(p95_dispatch_ms)} · "
        f"n={format_int(sample_count)}"
    )
```

Use the existing `format_latency()` helper if it handles floats well. If it expects integer milliseconds only, either update it carefully or add a local formatting helper for sub-ms and low-ms values:

```python
def _format_small_ms(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number < 1:
        return f"{number:.2f} ms"
    if number < 10:
        return f"{number:.1f} ms"
    return f"{number:.0f} ms"
```

Recommended card layout:

Server row:

- `Server PID`
- `Uptime`
- `Python`

Process/resource row:

- `RSS memory`
- `Open FDs`
- `Active threads`
- `Load average`
- `Dispatch overhead`

Alternatively, combine into one card row:

- `Server PID`
- `Uptime`
- `RSS memory`
- `Open FDs`
- `Active threads`
- `Load average`
- `Dispatch overhead`

The second option is denser and fits the stated simplification goal better.

Keep the database, routing, network, background task, and health sections otherwise unchanged.

### Phase 6: Optional warning-only process rendering

Do not render the process-count card during normal operation.

Optionally add a small warning panel only when `processes.process_count_warning` is true:

```html
<section class="panel warning">
  <h3>Process count warning</h3>
  <p>Observed {observed} EggPool processes; expected {expected}.</p>
</section>
```

This preserves the debugging value of process counting without spending permanent card space on it.

This warning-only rendering is optional for the first pass. If skipped, keep the JSON snapshot unchanged so future operators can inspect `/api/stats/runtime` or equivalent runtime JSON once exposed.

### Phase 7: Tests

Add focused unit tests where practical. Avoid brittle full-page HTML tests unless the project already uses them.

Recommended tests:

1. `DispatchOverheadRecorder` empty snapshot

- Construct recorder with `window_size=100`.
- Snapshot returns `sample_count == 0` and all latency values as `None`.

2. `DispatchOverheadRecorder` bounded window

- Construct recorder with `window_size=3`.
- Record four samples.
- Snapshot uses only the last three samples.
- `sample_count == 3`, `min_ms`, `max_ms`, and `avg_ms` match expected values.

3. `RuntimeMetricsService._snapshot_load()` availability

- On platforms with `os.getloadavg`, assert keys exist and `available` is boolean.
- Do not assert exact load values.
- If monkeypatching, patch `os.getloadavg` and `os.cpu_count` to deterministic values and assert normalized load.

4. Runtime renderer does not show configured/process cards

- Render `render_runtime()` with a minimal snapshot containing `configured_server_threads`, `thread_count`, `processes.eggpool_process_count`, `load`, and `dispatch_overhead`.
- Assert rendered HTML contains `Active threads`, `Load average`, and `Dispatch overhead`.
- Assert rendered HTML does not contain `configured server threads`.
- Assert rendered HTML does not contain `<h3>Processes</h3>` during non-warning operation.

5. Coordinator instrumentation smoke test

- This can be lower priority if integration tests are heavy.
- Use a fake recorder with `record_ns()` that increments a counter.
- Exercise a non-streaming request through the coordinator with a mocked HTTP client.
- Assert `record_ns()` is called before or by the time the upstream send occurs.

### Phase 8: Manual verification

Run the standard project checks:

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Then manually run EggPool and open `/runtime`.

Verify:

- The configured `Threads` card is gone.
- The normal `Processes` card is gone.
- `Active threads` still appears and shows a plausible live count.
- `Load average` appears on Linux/macOS and shows 1-minute load plus normalized-per-core subtitle where CPU count is available.
- `Dispatch overhead` appears as `—` before any upstream attempts.
- After making several OpenAI-compatible and/or Anthropic-compatible requests, `Dispatch overhead` shows a numeric average and p95.
- Dispatch-overhead values remain low under idle conditions and rise if SQLite contention or routing lock contention is artificially induced.
- Existing provider latency, TTFT, attempts, routing, and timeseries pages remain unchanged.

## Acceptance criteria

The implementation is complete when:

- Runtime dashboard no longer renders the static configured server thread card.
- Runtime dashboard no longer renders the normal process-count card.
- Runtime dashboard renders one active-thread card sourced from `threading.active_count()`.
- Runtime dashboard renders a load-average card with at least 1-minute load and CPU-normalized subtitle when available.
- Runtime dashboard renders a dispatch-overhead card sourced from a bounded in-memory rolling window of the last 100 upstream attempts.
- Dispatch overhead is measured before upstream provider send begins and excludes provider/network latency.
- Dispatch overhead uses monotonic/perf-counter timing, not wall-clock timing.
- No request body, prompt, auth header, account credential, or client IP metadata is stored in the recorder.
- No SQLite migration is added.
- Existing durable request accounting and provider latency accounting are unchanged.
- Tests cover the recorder and dashboard rendering behavior.

## Risks and mitigations

### Risk: dispatch-overhead metric is confused with provider latency

Mitigation: Label the card `Dispatch overhead` and use subtitle text such as `p95 X ms · n=Y attempts`. Avoid names like `Request latency` or `Router latency`.

### Risk: measurement accidentally includes upstream latency

Mitigation: Record immediately before `client.send(...)` or equivalent streaming send. Do not record after send returns.

### Risk: recording overhead affects hot path

Mitigation: Store only one integer in a bounded deque under a tiny lock. No database writes, no allocations beyond deque append behavior, no logging on the hot path.

### Risk: process-count debugging value is lost

Mitigation: Keep `_snapshot_processes()` and the JSON snapshot fields for now. Only remove the normal dashboard card. A later pass can add warning-only rendering if useful.

### Risk: os.getloadavg unavailable on some platform

Mitigation: Return `available: false` and render `—` with subtitle `load average unavailable`.

## Suggested file changes

- `src/eggpool/runtime_dispatch.py`
  - Add `DispatchOverheadRecorder`.

- `src/eggpool/app.py`
  - Instantiate recorder.
  - Store on `app.state`.
  - Pass into `RequestCoordinator` and `RuntimeMetricsService`.

- `src/eggpool/request/coordinator.py`
  - Add optional recorder parameter.
  - Add `started_monotonic_ns` or `started_perf_ns` to `ProxyRequestContext`.
  - Record elapsed nanoseconds immediately before upstream send in both streaming and non-streaming paths.

- `src/eggpool/runtime_metrics.py`
  - Accept recorder.
  - Add `_snapshot_dispatch_overhead()`.
  - Add `_snapshot_load()`.
  - Include `dispatch_overhead` and `load` in `snapshot()`.

- `src/eggpool/dashboard/render.py`
  - Remove configured server thread card.
  - Remove normal process-count card.
  - Rename active thread card to `Active threads`.
  - Render `Load average` card.
  - Render `Dispatch overhead` card.

- `tests/`
  - Add recorder tests.
  - Add runtime renderer tests.
  - Add load snapshot tests if there is an existing runtime metrics test module.

## Non-goals

- Do not expose a new public API endpoint unless one already exists for runtime snapshot JSON and needs to include the new fields.
- Do not add Prometheus support.
- Do not persist dispatch-overhead samples.
- Do not alter existing latency database columns.
- Do not change routing score behavior.
- Do not remove process-count snapshot internals in this pass.
