# OpenCode stream stability closure corrective plan

Date: 2026-07-08

## Context

This plan closes the OpenCode stream-stability/high-concurrency line after reviewing the implementation that landed in commit `c7565dd549d6677dfb6ea8754db4d058f8ad6b48`.

The implementation landed the main architecture:

- process-local stream diagnostics;
- SQLite lock contention snapshots with p50/p95/p99 samples;
- a bounded finalization retry queue;
- supervisor wiring for `finalization_retry_drain`;
- runtime metrics exposure for stream diagnostics, retry queue state, routing trace guard, and DB contention;
- high-concurrency reproducer script;
- high-concurrency integration tests and OpenCode stream-stability documentation.

The remaining work is corrective closure, not a new roadmap. The main issues found in review are:

1. `RuntimeMetricsService._snapshot_finalization_retry_queue()` appears to call an async `FinalizationRetryQueue.snapshot()` method from a synchronous helper and expands the coroutine with `**`. This likely causes the runtime retry queue snapshot to report an error and may emit an un-awaited coroutine warning.
2. The high-concurrency test harness is not truly concurrent. It loops through requests sequentially with `await asyncio.wait_for(_one(...))` rather than launching all request tasks and awaiting them together.
3. `test_fifty_concurrent_streams_no_leak` currently uses `concurrency=1`, despite its name/docstring claiming 50 concurrent streams.
4. The script and integration test define similar harness logic separately. This creates drift risk; for example, scenario names differ (`slow-stream` in the script vs `slow-token-cadence` in the test).
5. HTTPX exception classes are classified in coordinator pre-body failures, but stream diagnostics still does not expose first-class outcome labels for pool/read/connect/write/protocol timeout classes.
6. Closure validation needs an explicit runtime-metrics test that exercises `finalization_retry_queue` snapshot serialization.

## Goals

1. Fix the async runtime snapshot bug and verify `/api/stats/runtime` can serialize the finalization retry queue without warnings or fallback errors.
2. Make the reproducer genuinely concurrent so it can exercise DB lock contention, finalizer timeout, and HTTPX pool pressure realistically.
3. Restore the named 50-stream happy-path stress test to actually drive 50 concurrent streams.
4. Reduce harness duplication and align scenario names between script, test, and docs.
5. Add first-class HTTPX diagnostic outcomes where they are useful without changing request routing or retry semantics.
6. Finish with a small, repeatable closure validation command set.

## Non-goals

Do not redesign the finalizer, replace SQLite, increase default process workers, change accounting/cost formulas, change provider health semantics, or make routing traces correctness-critical. Do not increase default `server.threads` beyond 2. The default deployment posture remains `server.threads = 2`, `workers = 1`, and `database.worker_threads = 2` for file-backed production installs.

## Phase 1: Fix runtime finalization retry queue snapshot

### Files

- `src/eggpool/runtime_metrics.py`
- `src/eggpool/request/finalization_queue.py`
- `tests/unit/test_runtime_metrics.py`
- `tests/unit/test_stream_finalization_queue.py`

### Problem

`FinalizationRetryQueue.snapshot()` is async. `RuntimeMetricsService._snapshot_finalization_retry_queue()` is synchronous and currently attempts to expand the coroutine result. This is not valid and should surface as an error in runtime diagnostics.

### Implementation options

Prefer option A unless it conflicts with existing runtime metrics style.

Option A: make `_snapshot_finalization_retry_queue()` async.

- Change `RuntimeMetricsService.snapshot()` to call:

```python
result["finalization_retry_queue"] = await self._snapshot_finalization_retry_queue(probe_errors)
```

- Change `_snapshot_finalization_retry_queue()` to `async def` and `await self._finalization_retry_queue.snapshot()`.
- Keep the same `enabled: False` fallback when no queue is wired.
- Preserve bounded error handling.

Option B: make `FinalizationRetryQueue.snapshot()` synchronous.

- Since the queue currently protects its internal deque/counters with `asyncio.Lock`, this is less attractive. Do not inspect lock-protected state without a lock.
- Only choose this if the queue is refactored to a thread lock or if snapshot is explicitly best-effort and lock-free.

### Tests

- Add a unit test that constructs `RuntimeMetricsService` with a real `FinalizationRetryQueue` and asserts `await service.snapshot()` returns:
  - `finalization_retry_queue.enabled is True`
  - `finalization_retry_queue.size == 0`
  - no probe error mentioning finalization retry queue snapshot
  - no coroutine object in the returned JSON-like structure
- Add a regression test that `json.dumps(await service.snapshot(), default=str)` succeeds.
- Add a direct queue snapshot test for non-empty queue state.

### Acceptance criteria

The runtime endpoint can expose retry queue state reliably and no un-awaited coroutine warning appears in tests.

## Phase 2: Make the high-concurrency harness truly concurrent

### Files

- `tests/integration/test_high_concurrency_streaming.py`
- `scripts/repro_high_concurrency_streams.py`
- optionally `tests/support/high_concurrency_streaming.py` or `src/eggpool/testing/high_concurrency_streaming.py` if the project has an established test-support pattern

### Problem

Both the test harness and script currently iterate over requests sequentially. Even when `concurrency=50`, the code awaits each request before starting the next. This validates repeated cleanup paths, but it does not create true concurrent pressure on:

- `_select_lock`;
- SQLite connection lock;
- HTTPX connection pool;
- streaming generator cancellation;
- finalization retry queue;
- router active counts and quota reservation state.

### Implementation

Refactor the burst driver to launch request tasks together.

Suggested structure:

```python
async def _run_concurrent_burst(...):
    deadline = time.monotonic() + budget_s
    tasks = []
    for i in range(concurrency):
        req_id = ...
        cancel = (i / max(1, concurrency)) < cancel_rate
        tasks.append(asyncio.create_task(_one(req_id, cancel=cancel)))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=budget_s,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```

Requirements:

- Keep per-request cancellation offsets deterministic.
- Record completed/cancelled/failure counts from task results rather than mutating nonlocal counters from sequential execution.
- Ensure tasks that are intentionally cancelled are counted separately from upstream failures.
- Ensure cleanup drain runs after all tasks settle.
- Use one global budget for the burst, not per-request sequential budgets.
- Preserve the summary schema used by the CLI script.

### Tests

- Add an assertion that at least N tasks overlap in time. A simple in-memory counter can track max simultaneous active `_one()` tasks; for `concurrency=50`, assert `max_active >= 10` in normal test environments.
- Preserve all existing leak assertions.
- Add a low-budget timeout test that confirms unfinished tasks are cancelled and gathered cleanly.

### Acceptance criteria

The harness produces actual overlapping streams, and the reported `concurrency` value corresponds to simultaneously scheduled request tasks.

## Phase 3: Restore and strengthen the happy-path stress test

### Files

- `tests/integration/test_high_concurrency_streaming.py`

### Problem

`test_fifty_concurrent_streams_no_leak` currently calls `_run_concurrent_burst(..., concurrency=1, cancel_rate=0.0)`, so it does not validate its own name or docstring.

### Implementation

- Change the test to use `concurrency=50`.
- Keep `cancel_rate=0.0`.
- Use short chunk delay values to keep runtime acceptable.
- Assert:
  - `completed_count >= 50` if the harness should complete all, or `completed_count == 50` if deterministic completion is guaranteed;
  - no pending rows;
  - no active reservations;
  - router active count zero;
  - quota reserved cost delta zero;
  - retry queue size zero;
  - stream diagnostics completed delta >= 50;
  - no HTTPX/upstream error counters for happy path;
  - DB lock sample count delta is positive, not merely `>= 0`, once true concurrency is introduced.

### Acceptance criteria

The happy-path test really proves 50 concurrent streams can complete without leaks.

## Phase 4: Unify script/test harness primitives and scenario names

### Files

- `tests/integration/test_high_concurrency_streaming.py`
- `scripts/repro_high_concurrency_streams.py`
- optional shared support module
- `docs/opencode-stream-stability.md`

### Problem

The script and integration test duplicate mock upstream, cancellation, burst-driving, and summary logic. This risks drift. The script uses `slow-stream`; the test uses `slow-token-cadence` for the same concept.

### Implementation

- Create a shared support module if acceptable. Because scripts may be executed outside pytest, avoid depending on pytest fixtures in shared logic.
- Move shared constants and helpers into one location:
  - scenario names;
  - cancellation offset names;
  - mock SSE chunk builders;
  - scenario response builder;
  - burst runner;
  - post-burst invariant collector.
- Keep pytest-specific fixtures in `tests/integration/test_high_concurrency_streaming.py`.
- Keep CLI argparse and printing in `scripts/repro_high_concurrency_streams.py`.
- Choose one canonical scenario name. Prefer `slow-stream` for CLI/operator-facing docs and accept `slow-token-cadence` as an alias if backwards compatibility is useful.
- Ensure script help text and docs list the same scenario names as the implementation.

### Tests

- Add a test that every documented scenario is accepted by the script/parser or shared scenario registry.
- Add a test that aliases normalize to canonical names.
- Run at least one script entrypoint test with `--concurrency 2 --scenario happy-path` using the shared harness.

### Acceptance criteria

The test and CLI script cannot drift in scenario behavior or summary schema.

## Phase 5: Improve HTTPX diagnostic outcomes without over-changing semantics

### Files

- `src/eggpool/request/stream_diagnostics.py`
- `src/eggpool/request/coordinator.py`
- `tests/unit/test_stream_diagnostics.py`
- `tests/integration/test_high_concurrency_streaming.py`

### Problem

The current diagnostics module has generic stream outcomes plus exception counters. The original plan asked for distinct labels such as read timeout, pool timeout, connect timeout, protocol error, etc. The implementation classifies HTTPX exceptions in coordinator error classes, but the runtime outcome surface does not yet expose them as first-class terminal outcomes.

### Implementation

Add first-class constants for upstream transport outcomes:

- `upstream_pool_timeout`
- `upstream_read_timeout`
- `upstream_connect_timeout`
- `upstream_write_timeout`
- `upstream_protocol_error`
- `upstream_transport_error`
- `upstream_connect_error`

Do not change retry behavior. Only change diagnostic recording.

Where to record:

- In the `_RetryableUpstreamError` path after retries exhaust, if `last_error.error_class` matches one of the known HTTPX classes, record a stream diagnostic outcome with the matching label.
- For non-streaming paths, consider either sharing the same diagnostic service or keeping the scope to streaming only. Since the user-reported problem is streaming, it is acceptable to record only streaming-related retry exhaustion.
- If a `ReadTimeout` is raised before headers are received, it may never enter the stream generator. Ensure the coordinator-level exhausted path still records it when `context.streaming` is true.

Mapping:

- `PoolTimeout` -> `upstream_pool_timeout`
- `ReadTimeout` -> `upstream_read_timeout`
- `ConnectTimeout` -> `upstream_connect_timeout`
- `WriteTimeout` -> `upstream_write_timeout`
- `RemoteProtocolError` -> `upstream_protocol_error`
- `ConnectError` -> `upstream_connect_error`
- `ReadError` / `WriteError` / generic `TimeoutException` / generic `HTTPError` -> `upstream_transport_error` unless a more specific label is available

### Tests

- Unit test all outcome labels exist in the default counter set.
- Integration test an injected `httpx.ReadTimeout` on a streaming request and assert `upstream_read_timeout` increments.
- Integration test injected `httpx.PoolTimeout` if easy to simulate with respx side effect and assert `upstream_pool_timeout` increments.
- Ensure client cancellations still increment only `client_cancelled`, not upstream labels.

### Acceptance criteria

Runtime diagnostics can distinguish OpenCode-visible failure classes without requiring log scraping of exception strings.

## Phase 6: Verify finalization retry queue semantics under forced DB contention

### Files

- `tests/unit/test_stream_finalization_queue.py`
- `tests/integration/test_high_concurrency_streaming.py`
- `src/eggpool/request/finalization_queue.py` if bug fixes are needed

### Problem

The queue exists and is wired, but closure needs a targeted test that forces the immediate cancellation finalizer to miss the 10-second window or uses a shorter injectable timeout so the queue path is exercised deterministically. Waiting 10 seconds per test is not ideal.

### Implementation

- Add a coordinator setting or test-only dependency to configure the cancellation finalizer timeout. Suggested name: `stream_cancel_finalizer_timeout_s`, default `10.0`.
- In tests, set it to a very small value such as `0.001`.
- Hold the DB connection lock or monkeypatch finalizer to block so cancellation finalization times out.
- Assert a retry entry is enqueued.
- Release/unblock finalization.
- Call `drain_once()`.
- Assert pending rows/reservations/router/quota are reconciled.
- Assert duplicate retry entries are rejected by token and increment duplicate counter.
- Assert overflow behavior increments `dropped_overflow`.

### Acceptance criteria

The queue is not only present; its failure-mode path is deterministic and covered by tests.

## Phase 7: Runtime endpoint and docs closure

### Files

- `docs/opencode-stream-stability.md`
- `docs/troubleshooting.md` if present
- `README.md` if it already links to the stability doc
- `tests/unit/test_runtime_metrics.py`
- optional API route tests for `/api/stats/runtime`

### Implementation

- Add a runtime endpoint/API test that verifies these sections serialize cleanly:
  - `db.contention`
  - `stream_diagnostics`
  - `finalization_retry_queue`
  - `routing_trace_guard`
  - `dashboard_telemetry.separate_stats_db`
  - configured `runtime_threads` and `database_worker_threads`
- Update docs with the final closure test commands:

```bash
pytest tests/unit/test_runtime_metrics.py -q
pytest tests/unit/test_stream_diagnostics.py -q
pytest tests/unit/test_stream_finalization_queue.py -q
pytest tests/integration/test_high_concurrency_streaming.py -q
python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.25 --scenario slow-stream
python scripts/repro_high_concurrency_streams.py --concurrency 100 --cancel-rate 0.50 --scenario slow-stream
```

- Document expected successful summary values:
  - `leaked_pending_rows == 0`
  - `leaked_active_reservations == 0`
  - `router_active_requests_after == 0`
  - `finalization_retry_queue_size == 0`
  - DB lock p95/max present when contention was observed

### Acceptance criteria

Operators have a clear, working closure validation path for OpenCode stream stability.

## Phase 8: CI/status check and manual validation

### Commands

Run locally:

```bash
pytest
pytest tests/integration/test_high_concurrency_streaming.py -q
python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.25 --scenario slow-stream
python scripts/repro_high_concurrency_streams.py --concurrency 100 --cancel-rate 0.50 --scenario slow-stream
```

Then, if GitHub Actions are available, verify the commit status/check runs for the closure patch. If the GitHub connector reports no status contexts, state that explicitly in the handoff notes; do not claim CI passed unless a real run is visible or the local pytest output is provided.

### Manual OpenCode smoke test

Use a real OpenCode session only after mock tests pass:

1. Configure EggPool with:

```toml
[server]
threads = 2
workers = 1

[database]
worker_threads = 2

[routing.trace]
mode = "sampled"
```

2. Run several concurrent OpenCode requests through EggPool.
3. Cancel one request midstream.
4. Check runtime diagnostics:
   - cancellation should increment `client_cancelled`;
   - no active reservations remain;
   - no pending request rows remain;
   - finalization retry queue drains;
   - provider health stays healthy for client cancellations.
5. If OpenCode still reports `Failed to execute statement` while EggPool shows clean completion/cancellation, treat the error as likely OpenCode-local and investigate OpenCode state persistence separately.

## Definition of done

This closure pass is complete when:

- `RuntimeMetricsService` correctly awaits or synchronously obtains finalization retry queue snapshots.
- `/api/stats/runtime` can serialize finalization retry queue state without errors or coroutine warnings.
- The high-concurrency harness schedules tasks concurrently rather than sequentially.
- `test_fifty_concurrent_streams_no_leak` actually runs 50 concurrent streams.
- Cancellation matrix and scenario matrix still pass with true concurrency.
- The finalization retry timeout/enqueue/drain path is covered deterministically.
- HTTPX timeout/pool/protocol classes have first-class diagnostic outcomes or an explicit documented reason for leaving them as exception counters only.
- Script/test scenario names are aligned or normalized through aliases.
- Docs list the final closure commands and expected summary invariants.
- No accounting, cost, protocol transcoding, routing fairness, cache/compression, or dashboard regressions are introduced.

## Suggested commit breakdown

1. `Fix finalization retry queue runtime snapshot`
2. `Make stream stability reproducer truly concurrent`
3. `Restore 50 stream happy path stress test`
4. `Unify stream stability harness scenarios`
5. `Add HTTPX stream diagnostic outcome labels`
6. `Add finalization retry queue contention tests`
7. `Document stream stability closure validation`

Keep the runtime snapshot fix as the first commit because it is the clearest bug and lowest-risk patch.
