# OpenCode stream stability remaining work plan

Date: 2026-07-08

## Purpose

This is the remaining-work handoff plan for the OpenCode stream instability and high-concurrency robustness line. It follows `plans/2026-07-08-opencode-stream-stability-high-concurrency-hardening.md` and narrows the next implementation pass to the work that still needs code, tests, documentation, and closure validation.

The prior plan established the working diagnosis: the exact `Failed to execute statement` message is probably emitted by OpenCode or one of its local persistence dependencies, not by EggPool's current database wrapper. EggPool can still be the indirect cause if high-concurrency streaming through EggPool causes dropped downstream responses, upstream read/pool timeouts, delayed stream finalization, or SQLite lock contention.

This follow-up plan should be treated as the execution plan for the remaining work. It prioritizes observability and deterministic reproduction before invasive finalization and contention changes.

## Current baseline assumptions

- Keep `server.threads = 2` as the default. This is the right default posture for Raspberry Pi-class deployments where a single process needs enough event-loop capacity for proxy traffic and dashboard/API traffic.
- Keep `server.workers = 1`. Do not introduce multi-process default behavior while SQLite remains the primary local persistence layer.
- Keep `database.worker_threads = 2` as the default/recommended file-backed deployment posture so stats/dashboard reads can use a read-only connection rather than the primary data-plane write connection.
- Keep routing trace default sampled. Full trace persistence is useful for debugging but should not become the default in high-concurrency streaming deployments.
- Do not change usage accounting, cost calculation, quota reservation semantics, provider health semantics, or protocol transcoding behavior except where tests prove finalization behavior is wrong.

## Remaining work overview

The remaining work is split into six implementation slices:

1. Add stream, HTTPX, and DB-lock diagnostics.
2. Add a deterministic high-concurrency stream reproducer.
3. Harden cancellation and midstream finalization with a targeted retry queue.
4. Reduce noncritical SQLite write pressure and protect finalizers from observability writes.
5. Tune/document high-concurrency OpenCode runtime profiles while preserving low-power defaults.
6. Validate closure with stress tests, leak checks, and operator documentation.

The implementation sequence matters. Do not start by changing finalization semantics without first adding diagnostics and a reproducer, because the original symptoms can come from several distinct paths.

## Slice 1: Stream, HTTPX, and DB-lock diagnostics

### Goal

Make stream failures and lock pressure directly observable. After this slice, logs and runtime diagnostics should explain whether a failed OpenCode request ended due to client cancellation, upstream midstream failure, HTTPX pool timeout, read timeout, finalizer timeout, or delayed SQLite access.

### Files to inspect and modify

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/providers/client_pool.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/runtime_metrics.py`
- `src/eggpool/api/stats.py`
- `src/eggpool/dashboard/routes.py`
- `src/eggpool/dashboard/render.py`
- `tests/unit/test_request_coordinator.py`
- `tests/unit/test_runtime_metrics.py`
- `tests/unit/test_database_connection.py`

### Implementation tasks

1. Define a small set of terminal stream outcome labels:
   - `stream_completed`
   - `client_cancelled`
   - `downstream_cancelled`
   - `upstream_midstream_error`
   - `upstream_read_timeout`
   - `upstream_pool_timeout`
   - `upstream_protocol_error`
   - `stream_finalizer_timeout`
   - `stream_finalizer_failed`
   - `stream_missing_final_usage`

2. Add structured logging for terminal stream outcomes. Include:
   - proxy request id
   - DB request id if allocated
   - attempt id if allocated
   - provider id
   - account name/id
   - model id
   - protocol
   - stream outcome
   - elapsed milliseconds
   - chunks emitted
   - bytes emitted
   - first-byte latency if available
   - upstream status code if available
   - HTTPX exception class if applicable
   - finalizer outcome and finalizer duration

3. Add an in-memory runtime counter surface for the stream outcomes. This should be cheap and bounded. Avoid per-request unbounded storage.

4. Improve HTTPX exception classification. At minimum classify:
   - `httpx.PoolTimeout`
   - `httpx.ReadTimeout`
   - `httpx.ConnectTimeout`
   - `httpx.RemoteProtocolError`
   - `httpx.ReadError`
   - `httpx.WriteError`
   - generic `httpx.HTTPError`

5. Add DB lock wait distribution in `Database`, not just cumulative/max counters. A ring buffer or fixed bucket histogram is sufficient. Suggested fields:
   - `lock_wait_count`
   - `lock_wait_total_ms`
   - `lock_wait_recent_max_ms`
   - `lock_wait_p50_ms`
   - `lock_wait_p95_ms`
   - `lock_wait_p99_ms`
   - `operations_by_kind`
   - `last_operation_error_class`
   - `last_operation_error_kind`

6. Expose diagnostics in a runtime/status API. The dashboard should initially render only compact high-signal fields:
   - DB lock p95/p99
   - finalizer timeout count
   - client cancellation count
   - upstream midstream error count
   - HTTPX timeout count
   - pending requests older than threshold

7. Do not log prompts, request bodies, API keys, response chunks, tool call payloads, or raw provider error bodies.

### Tests

- Completed streaming response increments `stream_completed` and records no error class.
- Client cancellation increments `client_cancelled` and does not mark provider health unhealthy.
- Injected `httpx.ReadTimeout` increments `upstream_read_timeout` and records the exception class.
- Injected `httpx.PoolTimeout` increments `upstream_pool_timeout` and records the exception class.
- DB lock histogram returns stable zero/empty values before operations and nonzero values after contended operations.
- Runtime diagnostics JSON shape is stable enough for dashboard rendering.

### Acceptance criteria

A single failed streaming request should leave enough structured evidence to answer: who ended the stream, what phase failed, whether SQLite lock pressure was present, and whether finalization completed.

## Slice 2: Deterministic high-concurrency reproducer

### Goal

Create a repeatable harness that reproduces the relevant classes of failure without relying on real providers or OpenCode. The harness should create controlled upstream and downstream behavior so finalizer leaks and runtime-state drift are testable.

### Files to add or modify

- `tests/integration/test_high_concurrency_streaming.py`
- `tests/support/streaming_upstream.py` or equivalent existing test-support module
- `scripts/repro_high_concurrency_streams.py`
- `docs/troubleshooting.md`

### Implementation tasks

1. Add a mock SSE upstream with configurable scenarios:
   - valid stream with final usage event
   - valid stream without final usage event
   - slow first byte
   - slow token cadence
   - abrupt close after N chunks
   - server stall past `read_timeout_s`
   - malformed SSE frame
   - provider-side connection reset

2. Add a concurrent downstream client harness with cancellation offsets:
   - cancel before first byte
   - cancel after first token
   - cancel midstream
   - cancel after final text but before final usage
   - consume through `[DONE]`

3. Track invariants at the end of each run:
   - zero pending request rows after bounded cleanup
   - zero active reservations for terminal requests
   - router active request counts return to zero
   - quota estimator active/reserved state returns to zero
   - finalization retry queue drains to zero once implemented
   - provider health is not penalized for downstream cancellation

4. Provide a manual script for local reproduction. Suggested options:

```bash
python scripts/repro_high_concurrency_streams.py \
  --concurrency 50 \
  --cancel-rate 0.25 \
  --scenario slow-stream

python scripts/repro_high_concurrency_streams.py \
  --concurrency 100 \
  --cancel-rate 0.50 \
  --scenario abrupt-upstream-close
```

5. The script should print a compact summary:
   - total requests
   - completed
   - cancelled
   - upstream failed
   - finalizer timed out
   - leaked pending rows
   - leaked reservations
   - max/p95 DB lock wait
   - HTTPX exception class counts

### Tests

- 50 concurrent slow streams with 25% cancellations should finish without leaks after bounded cleanup.
- Abrupt upstream close should finalize as midstream error and close upstream response.
- Stalled upstream should be classified as read timeout, not generic stream failure.
- Provider health should not be penalized for client cancellation.

### Acceptance criteria

A maintainer can run one script or integration test and reproduce the exact cleanup and lock-pressure paths that are suspected in the OpenCode incidents.

## Slice 3: Targeted finalization retry queue

### Goal

If immediate stream finalization is cancelled, blocked, or times out while waiting for SQLite, cleanup should be retried in a targeted, idempotent, bounded way rather than relying only on the broad stale-request finalizer.

### Files to add or modify

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/finalization_queue.py`
- `src/eggpool/background.py`
- `src/eggpool/app.py`
- `tests/unit/test_stream_finalization_queue.py`
- `tests/integration/test_high_concurrency_streaming.py`

### Implementation tasks

1. Add a bounded in-memory finalization retry queue. Queue entries should include only durable identifiers and safe metadata:
   - request id
   - attempt id
   - reservation id if available
   - provider/account/model/protocol
   - finalization outcome
   - partial usage if available
   - bytes/chunks emitted if tracked
   - enqueue timestamp
   - retry count

2. Keep the immediate shielded finalizer path. If it succeeds, no queue entry is needed. If it times out or fails due to transient lock pressure, enqueue a retry entry.

3. Add a supervised drain task. Suggested behavior:
   - drain every 1-2 seconds while the queue is non-empty
   - drain less frequently when empty
   - bounded concurrency, likely 1-2 finalizers at a time
   - exponential or linear short backoff per failed entry
   - maximum age/attempts before terminal degraded state

4. Make finalization idempotent. Duplicate or late finalization must not:
   - double-release reservations
   - double-decrement active counts
   - double-write usage totals
   - create contradictory terminal request states
   - penalize provider health twice

5. Add queue metrics:
   - current queue depth
   - total enqueued
   - total finalized by retry
   - total retry failures
   - total dropped due to queue full
   - oldest queued age seconds

6. Ensure client-cancelled entries do not penalize provider health. Midstream upstream errors can retain existing provider-health semantics if already intended.

### Tests

- Hold the DB lock so cancellation finalization times out; assert queue entry is created.
- Release the lock; assert queue drains and finalizes the request.
- Re-run finalization on an already finalized request; assert no double release or double accounting.
- Fill the queue beyond capacity; assert bounded drop behavior and diagnostic counter.
- Crash/restart behavior remains safe: queued entries are in-memory only, but stale finalizer still recovers durable rows.

### Acceptance criteria

A cancellation-time finalizer timeout no longer leaves cleanup solely to the slow broad stale finalizer. It is visible, retried, bounded, and idempotent.

## Slice 4: Reduce SQLite contention from noncritical writes

### Goal

Protect correctness-critical writes, especially selection and finalization, from noncritical observability writes during high-concurrency streaming.

### Files to inspect and modify

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/routing/trace.py` or equivalent trace repository paths
- `src/eggpool/db/repositories.py`
- `src/eggpool/models/config.py`
- `config.example.toml`
- `tests/unit/test_routing_trace_config.py`
- `tests/integration/test_high_concurrency_streaming.py`

### Implementation tasks

1. Confirm `routing.trace.mode = "sampled"` remains the default.

2. Add a pressure guard for best-effort routing trace writes:
   - if DB lock p95/p99 exceeds a configurable threshold, skip trace writes temporarily;
   - emit `routing_trace_skipped_db_pressure` counter;
   - never allow trace write failure to affect request dispatch or finalization.

3. Consider a bounded trace write queue if current trace writes compete directly with finalizers. It must be drop-on-overflow and explicitly noncritical.

4. Audit finalizer transaction count. Collapse adjacent writes only where invariants stay obvious. Avoid broad rewrites that risk accounting regressions.

5. Review background jobs for DB usage during high-concurrency windows:
   - metrics flush
   - rollup refresh
   - catalog/model-info refresh
   - stale request finalizer
   - checkpoint/maintenance

6. Mark noncritical jobs as skippable/deferable under high DB lock pressure. Do not defer stale request finalization or crash-safety cleanup.

### Tests

- With artificially high DB pressure, routing trace writes are skipped and request dispatch still succeeds.
- With trace mode `all`, stress test shows no finalizer leaks, though lock pressure may be higher.
- With trace mode `off`, no trace writes occur and the runtime counter reports zero persisted traces.
- Background noncritical job deferral does not affect stale finalization.

### Acceptance criteria

Noncritical observability writes cannot starve stream finalizers under bursty streaming load.

## Slice 5: Runtime defaults, config comments, and OpenCode profile docs

### Goal

Document the correct deployment posture and avoid misconfiguration that makes OpenCode instability more likely.

### Files to modify

- `config.example.toml`
- `docs/deployment.md`
- `docs/troubleshooting.md`
- `docs/providers.md`
- `README.md` if a short pointer is warranted
- `tests/unit/test_config_examples.py` if available

### Implementation tasks

1. Ensure `server.threads = 2` is present and commented as the recommended low-power default. Explain that `workers = 1` should remain the default with SQLite.

2. Ensure `database.worker_threads = 2` is present and commented as the recommended file-backed deployment default for dashboard isolation.

3. Ensure upstream timeout and pool settings are visible in the example config:
   - `connect_timeout_s`
   - `read_timeout_s`
   - `write_timeout_s`
   - `pool_timeout_s`
   - `max_connections`
   - `max_keepalive`
   - `keepalive_timeout_s`

4. Add an OpenCode/high-concurrency profile snippet:

```toml
[server]
threads = 2
workers = 1

[database]
worker_threads = 2
busy_timeout_ms = 10000

[upstream]
read_timeout_s = 900
pool_timeout_s = 60
max_connections = 128
max_keepalive = 64

[routing.trace]
mode = "sampled"
```

5. Add a diagnostic low-noise profile for reproducing instability:

```toml
[routing.trace]
mode = "off"
```

6. Explain when to raise `read_timeout_s` versus `pool_timeout_s`:
   - raise `read_timeout_s` for long legitimate coding-agent streams or slow providers;
   - raise `pool_timeout_s` when connection slots are temporarily saturated but expected to free;
   - raise `max_connections`/`max_keepalive` only when provider limits and host memory/file descriptors allow.

7. Add troubleshooting instructions for OpenCode:
   - capture EggPool request id if available;
   - compare streaming vs non-streaming behavior;
   - check runtime diagnostics for finalizer timeouts and DB lock p95/p99;
   - check HTTPX exception class counts;
   - check whether dashboard polling is active;
   - check `database.worker_threads` and routing trace mode.

### Tests

- Example config parses.
- Documentation snippets either parse directly or are clearly marked as partial snippets.
- Config default tests assert `server.threads == 2` and `database.worker_threads == 2` if such tests already exist or can be added cleanly.

### Acceptance criteria

An operator using OpenCode through EggPool can find and apply a safe default/high-concurrency profile without increasing process workers or accidentally amplifying SQLite contention.

## Slice 6: Closure validation and release checklist

### Goal

Prove the fix set closes the observed risk class without hiding regressions.

### Validation matrix

Run all relevant existing tests plus the new stress/repro tests:

```bash
pytest
pytest tests/integration/test_high_concurrency_streaming.py -q
python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.25 --scenario slow-stream
python scripts/repro_high_concurrency_streams.py --concurrency 100 --cancel-rate 0.50 --scenario slow-stream
python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.00 --scenario abrupt-upstream-close
python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.00 --scenario read-timeout
```

For each run, confirm:

- all terminal requests are finalized;
- no pending request rows remain after bounded cleanup;
- no active reservations remain for terminal requests;
- router active counts return to zero;
- finalization retry queue drains to zero;
- DB lock p95/p99 are visible;
- HTTPX failures are classified;
- client cancellations do not create provider-health penalties;
- dashboard remains responsive enough and does not share the primary data-plane DB connection in the default file-backed config;
- accounting totals remain consistent with existing tests.

### Manual OpenCode validation

Use a real OpenCode session only after mock stress validation passes:

1. Start EggPool with `server.threads = 2`, `workers = 1`, `database.worker_threads = 2`, `routing.trace.mode = "sampled"`, and high-concurrency upstream timeouts if needed.
2. Run several concurrent OpenCode coding tasks through EggPool.
3. Repeat with one forced cancellation/interruption.
4. Confirm EggPool classifies cancellations separately from provider errors.
5. Confirm no pending rows, active reservations, or active counts remain after the session ends.
6. If OpenCode still reports `Failed to execute statement`, compare the timestamp to EggPool diagnostics. If EggPool shows clean stream completion, the error is likely OpenCode-local and should be investigated separately upstream.

## Definition of done

This remaining-work line is done when:

- diagnostics explain all stream terminal paths;
- the high-concurrency reproducer exists and is documented;
- cancellation finalizer timeout has a targeted retry path;
- stale runtime reconciliation is idempotent and tested;
- noncritical trace/dashboard/background writes cannot starve finalizers;
- docs recommend `server.threads = 2`, `workers = 1`, and `database.worker_threads = 2` for default file-backed deployments;
- high-concurrency OpenCode profile is documented;
- stress tests pass without leaked pending rows, reservations, or active counts;
- provider health is not penalized for downstream client cancellation;
- existing accounting/cost/dashboard/protocol tests continue to pass.

## Suggested commit breakdown

1. `Add stream and DB contention diagnostics`
2. `Add high concurrency streaming reproducer`
3. `Add targeted stream finalization retry queue`
4. `Reduce trace write pressure under DB contention`
5. `Document opencode high concurrency runtime profile`
6. `Add stream stability closure tests`

Keep each commit independently reviewable. Avoid mixing diagnostics, queue semantics, and docs in one large patch unless absolutely necessary.
