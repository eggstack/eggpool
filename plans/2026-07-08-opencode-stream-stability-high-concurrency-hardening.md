# OpenCode stream stability and high-concurrency hardening plan

Date: 2026-07-08

## Context

OpenCode occasionally reports errors such as `Failed to execute statement` and appears to timeout or drop a connection midstream while waiting for an upstream response through EggPool. The symptoms may not share a single root cause. The exact `Failed to execute statement` string does not appear to be emitted by EggPool's current database wrapper; EggPool wraps database errors as `Execute write failed`, `Execute returning failed`, `Fetch all failed`, `Fetch one failed`, `Begin transaction failed`, and related messages. The string is therefore likely emitted by OpenCode itself or by one of its local persistence dependencies. EggPool can still be the upstream trigger when high-concurrency request handling causes stream interruption, slow response forwarding, cancellation cleanup delay, or timeout propagation.

The current EggPool runtime already contains several relevant hardening pieces:

- `server.threads = 2` is the default and is reasonable for Raspberry Pi 4/5 and other low-power hosts when the dashboard is used during active proxy traffic. Keep `workers = 1`; the objective is more event-loop capacity inside one process, not multi-process SQLite contention.
- `database.worker_threads = 2` is the default and opens a separate read-only stats/dashboard connection for file-backed SQLite so dashboard analytics do not share the primary data-plane connection lock.
- Request selection keeps upstream I/O outside `_select_lock`, which is correct.
- Streaming cancellation finalization is shielded and capped, which prevents many leaked pending requests, but the current 10 s ceiling can still be hit during SQLite lock contention.
- Routing trace persistence is sampled by default, reducing write pressure compared with writing every routing decision.

The remaining risk profile is a high-concurrency feedback loop: long streaming responses occupy HTTP client pool slots; clients such as OpenCode may cancel or timeout; EggPool stream generators then attempt cancellation finalization; finalizers queue behind the primary SQLite connection lock; delayed finalization leaves transient pending requests, active counts, reservations, or backoff state stale; stale runtime state can affect selection pressure and increase the probability of further timeouts or dropped streams.

## Goals

1. Determine whether OpenCode-visible `Failed to execute statement` correlates with EggPool stream cancellation, upstream timeout, SQLite lock contention, or HTTP client pool exhaustion.
2. Make stream completion, cancellation, and midstream-error finalization robust under high concurrency.
3. Reduce request-path SQLite lock contention without regressing accounting, reservation correctness, crash recovery, or routing fairness.
4. Keep the default deployment appropriate for low-power Raspberry Pi-class devices: one process worker, two runtime threads, bounded DB and HTTP concurrency, and conservative memory footprint.
5. Add operator-facing diagnostics that make future incident reports actionable from EggPool logs and dashboard/runtime surfaces.

## Non-goals

This plan does not change OpenCode internals, OpenCode's local SQLite/state handling, provider API behavior, pricing semantics, quota fairness scoring, protocol transcoding semantics, or request accounting formulas. It does not replace SQLite. It does not increase the default process worker count. It does not make dashboard metrics real-time at the expense of proxy-path stability.

## Working hypotheses

Track these separately; do not collapse all symptoms into one presumed defect.

1. OpenCode-local statement failure: OpenCode emits `Failed to execute statement` when its own local persistence layer is interrupted by a stream timeout, cancellation, process shutdown, or concurrent session operation. EggPool is only the indirect cause if it drops or stalls the response.
2. EggPool stream cancellation cleanup pressure: client disconnects cancel the stream generator while the request finalizer is waiting on the primary SQLite lock. The shielded finalizer times out after 10 s, leaving stale cleanup to the periodic stale-request finalizer.
3. SQLite lock contention: the primary data-plane connection serializes selection writes, request finalization, reservation updates, backoff updates, trace writes, and maintenance jobs. Under burst load, write queues can make cancellation/finalization late.
4. HTTPX pool or timeout pressure: long streams can exhaust provider/account HTTP connection pools or hit `read_timeout_s`, `pool_timeout_s`, or provider-side idle timeouts.
5. Dashboard/background interference: if `database.worker_threads = 1`, or if expensive maintenance/stat reads use the primary connection, dashboard and background tasks can increase data-plane latency.
6. Provider-side instability: upstream may terminate streams midresponse or omit final usage frames. EggPool should classify and finalize these distinctly from downstream client cancellation.

## Phase 1: Add incident-grade stream and DB contention diagnostics

Target files:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/runtime_metrics.py`
- `src/eggpool/api/stats.py`
- `src/eggpool/dashboard/routes.py`
- `src/eggpool/dashboard/render.py`
- `tests/unit/test_request_coordinator.py`
- `tests/unit/test_runtime_metrics.py`

Implementation details:

- Add structured event logging for every terminal streaming path. Each event should include `proxy_request_id`, `db_request_id` when available, provider id, account name, model id, protocol, stream outcome, elapsed ms, bytes emitted, first byte ms, upstream connect/header/read ms, attempt number, and exception class if present.
- Emit distinct stream outcome values:
  - `stream_completed`
  - `client_cancelled`
  - `downstream_send_cancelled`
  - `upstream_midstream_error`
  - `stream_finalizer_timeout`
  - `stream_finalizer_failed`
  - `stream_usage_missing_final_event`
- In `_build_stream_generator()`, classify exceptions from `upstream_response.aiter_bytes()` separately from exceptions thrown while yielding to the downstream ASGI layer. If needed, wrap the upstream iterator advance and downstream yield boundary so upstream read failures and downstream cancellations do not collapse into one broad `Exception` path.
- Add `Database` lock-wait histogram counters rather than only cumulative and max wait. Minimal implementation:
  - total operations by kind: read/write/transaction/pragma/vacuum
  - lock wait count
  - cumulative lock wait ms
  - recent max lock wait ms
  - p50/p95/p99 approximated using an in-memory ring buffer or small fixed buckets
  - last error class and last error operation kind
- Expose the new DB and stream counters on an existing runtime diagnostics endpoint or add a focused runtime section.
- Add a compact dashboard runtime card for:
  - SQLite lock p95/p99
  - finalizer timeout count
  - client cancellation count
  - midstream error count
  - active pending requests older than N seconds
  - HTTPX pool timeout count if available from error classification
- Ensure logs do not include request bodies, API keys, prompts, tool payloads, or raw upstream chunks.

Validation:

- Unit test that completed streams produce `stream_completed` diagnostics.
- Unit test that downstream cancellation produces `client_cancelled` diagnostics and does not mark provider health unhealthy.
- Unit test that injected upstream iterator failure produces `upstream_midstream_error` diagnostics and finalizes the request as `MIDSTREAM_ERROR`.
- Unit test that DB contention snapshot includes new histogram fields and remains stable before any operations.

## Phase 2: Build a deterministic high-concurrency stream reproducer

Target files:

- `tests/integration/test_high_concurrency_streaming.py`
- `tests/support/streaming_upstream.py` or existing test support utilities
- `scripts/repro_high_concurrency_streams.py`
- `docs/troubleshooting.md`

Implementation details:

- Add a local mock SSE upstream that can simulate:
  - slow first byte
  - slow per-token streaming
  - abrupt upstream close after N chunks
  - valid stream without final usage frame
  - valid stream with final usage frame
  - response that exceeds configured read timeout
  - response that holds connection long enough to exercise pool pressure
- Add a concurrent client harness that can run N streaming requests through EggPool and cancel a configurable percentage at several offsets:
  - before first byte
  - after first token
  - midstream
  - immediately after final text but before final usage frame
  - after `[DONE]`
- Capture these metrics from one run:
  - completed streams
  - cancelled streams
  - upstream midstream errors
  - finalizer timeouts
  - pending request rows after run
  - active reservations after run
  - router active request counts after run
  - DB lock p95/p99
  - HTTPX error classes
- Provide a script mode for manual reproduction outside pytest. It should print a compact summary suitable for bug reports.

Validation:

- Integration test with 50 concurrent mock streams should finish with zero leaked pending requests, zero active reservations, and zero nonzero router active counts after cleanup.
- Integration test with injected downstream cancellations should not record account health failures.
- Integration test with abrupt upstream close should finalize as midstream error and close the upstream response.
- The script should work against a temporary config and temporary SQLite database.

## Phase 3: Make cancellation finalization eventually reliable, not only best-effort

Target files:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/background.py` or a new `src/eggpool/request/finalization_queue.py`
- `src/eggpool/app.py`
- `tests/unit/test_stream_finalization_queue.py`
- `tests/integration/test_high_concurrency_streaming.py`

Implementation details:

- Keep the immediate shielded finalizer on cancellation, but change the failure mode when the 10 s timeout is hit.
- Add an in-memory bounded finalization retry queue for selected attempts that could not finalize during cancellation. Queue entries should contain only durable identifiers and finalization metadata needed to retry safely:
  - request id
  - attempt id
  - reservation id
  - account name/id
  - provider/model/protocol
  - outcome (`CLIENT_CANCELLED` or `MIDSTREAM_ERROR`)
  - observed partial usage and bytes emitted
  - enqueue timestamp
  - retry count
- Register a supervised periodic task that drains this queue with bounded concurrency. Suggested initial cadence: every 1-2 seconds while non-empty, otherwise every 15-30 seconds. Keep the implementation simple and compatible with the existing `TaskSupervisor` cadence model; do not introduce an unbounded background task loop if supervisor conventions prefer periodic functions.
- Use idempotent repository methods for retry finalization. Re-finalization must be safe if the immediate finalizer eventually succeeded after the timeout boundary or if the stale finalizer already ran.
- Add a maximum retry age and terminal counter. If an entry cannot finalize after a bounded period, log an error and rely on the stale finalizer, but expose this as an operator-visible degraded state.
- Ensure runtime active count and quota reservation cleanup are reconciled when finalization succeeds from the retry queue.
- Do not apply provider health penalties for queued `CLIENT_CANCELLED` finalizations.

Validation:

- Force the immediate cancellation finalizer to timeout by holding the DB lock in a test; assert an entry is queued.
- Release the DB lock; assert the retry queue finalizes the request, releases the reservation, and decrements active counts.
- Assert duplicate finalization attempts do not double-release reservations or double-decrement active counts.
- Assert queue size is bounded and increments a dropped-entry counter if overloaded.

## Phase 4: Reduce request-path SQLite write amplification

Target files:

- `src/eggpool/models/config.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/db/repositories.py`
- `config.example.toml`
- `docs/config.md` or `docs/deployment.md`
- `tests/unit/test_routing_trace_config.py`

Implementation details:

- Keep `[routing.trace].mode = "sampled"` as the default and document that high-throughput installs should use `sampled` or `off`, not `all`.
- Add runtime guardrails for routing trace writes:
  - if DB lock wait p95 exceeds a configurable threshold, skip best-effort routing trace writes until pressure drops;
  - record `routing_trace_skipped_db_pressure` count.
- Consider moving trace writes to an observability write queue separate from the correctness-critical selection path. The trace currently writes after the selection lock is released, which is good, but it still uses the primary DB connection and can contend with finalizers. A bounded queue with drop-on-overflow semantics is acceptable because traces are diagnostic.
- Audit finalizer writes for avoidable multiple transactions. The finalizer should update request, attempt, reservation, usage window, rollup/coalescer, and operational events with the minimum transaction count compatible with existing invariants.
- Prefer coalescer/buffered writes for non-critical telemetry surfaces where correctness does not require per-request synchronous persistence.

Validation:

- Stress test with routing trace `off`, `sampled`, and `all`; compare DB lock p95 and finalizer timeout counts.
- Unit test that DB-pressure trace skipping never prevents request dispatch.
- Unit test that default config remains sampled and backwards-compatible.

## Phase 5: Tune and document high-concurrency HTTP client behavior

Target files:

- `src/eggpool/models/config.py`
- `src/eggpool/providers/client_pool.py`
- `config.example.toml`
- `docs/providers.md`
- `docs/troubleshooting.md`
- `tests/unit/test_provider_client_pool.py`

Implementation details:

- Keep current defaults conservative, but document high-concurrency streaming profiles. The default `server.threads = 2` remains reasonable. Do not increase `workers` above one by default.
- Add recommended profile snippets:
  - low-power default: `threads = 2`, `worker_threads = 2`, `max_connections = 100`, `max_keepalive = 20`, `read_timeout_s = 300`.
  - high-concurrency coding-agent streaming: `threads = 2`, `worker_threads = 2`, `max_connections = 128-256`, `max_keepalive = 64-128`, `pool_timeout_s = 60`, `read_timeout_s = 900`.
  - diagnostic low-noise mode: routing traces off, dashboard auto-refresh slower, read timeout high.
- Add warnings about over-tuning on Raspberry Pi: very high connection limits can increase memory, file descriptors, TLS state, and upstream/provider throttling.
- Improve HTTPX error classification in logs:
  - `PoolTimeout`
  - `ReadTimeout`
  - `ConnectTimeout`
  - `RemoteProtocolError`
  - `ReadError`
  - `WriteError`
- If HTTPX exposes connection-pool telemetry only indirectly, classify by exception and request phase rather than attempting fragile private attribute inspection.
- Ensure per-account proxy transports inherit the same limits and diagnostics as provider-level clients.

Validation:

- Unit test that provider and account-specific clients are built with configured limits and timeouts.
- Integration stress script should show whether failures are pool timeouts, read timeouts, upstream protocol errors, or client cancellations.
- Documentation should explicitly distinguish `server.threads` from HTTP connection limits and SQLite worker connections.

## Phase 6: Add stale runtime reconciliation after finalizer failure

Target files:

- `src/eggpool/app.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/quota/estimation.py`
- `tests/unit/test_stale_request_finalizer.py`
- `tests/integration/test_high_concurrency_streaming.py`

Implementation details:

- Extend the stale-request finalizer so it not only transitions stale DB rows and releases reservations, but also reconciles in-memory runtime state for affected accounts:
  - router active request counts
  - quota estimator in-memory reservations
  - health-manager request slots if applicable
- Ensure reconciliation is idempotent and does not decrement below zero.
- Add diagnostics for every reconciliation action, including account/model and stale age.
- Keep crash recovery broad and startup-safe, but make runtime stale reconciliation frequent enough to recover from cancellation finalizer timeout quickly.
- Avoid marking provider health unhealthy solely because a client cancelled or because the finalizer timed out.

Validation:

- Create stale pending rows and active reservations in a test; assert one finalizer pass releases DB and runtime state.
- Assert repeated finalizer passes are no-ops.
- Assert client-cancelled stale rows do not create provider backoff.

## Phase 7: Protect the data plane from dashboard and maintenance interference

Target files:

- `src/eggpool/app.py`
- `src/eggpool/stats/service.py`
- `src/eggpool/background.py`
- `src/eggpool/dashboard/routes.py`
- `config.example.toml`
- `docs/deployment.md`
- `tests/unit/test_database_config.py`

Implementation details:

- Keep `database.worker_threads = 2` as the documented default for file-backed production installs.
- Add a runtime warning when `database.worker_threads = 1` and dashboard is enabled, because dashboard reads will share the primary connection and can increase request-path lock pressure.
- Add a runtime warning if an expensive stats/dashboard endpoint falls back to primary `db` instead of `stats_db` in a file-backed deployment.
- Ensure maintenance tasks such as checkpoint, cleanup, rollup refresh, stale finalizer, and model-info backfills expose duration and DB lock impact.
- Consider pausing noncritical dashboard/stat refresh work when data-plane DB lock p95 crosses a threshold. Do not pause stale-request finalization or crash-safety tasks.
- Ensure dashboard auto-refresh defaults are not too aggressive for high-concurrency streaming workloads.

Validation:

- Unit test that app startup wires `stats_db` as a distinct read-only connection when `worker_threads > 1` and path is file-backed.
- Unit test that warning is emitted for `worker_threads = 1` with dashboard enabled.
- Stress test with dashboard polling on/off should show bounded impact after this phase.

## Phase 8: Add OpenCode-focused troubleshooting workflow

Target files:

- `docs/troubleshooting.md`
- `docs/providers.md`
- `README.md` if a short pointer is warranted
- `scripts/repro_opencode_stream_instability.md` or a shell snippet under docs

Implementation details:

- Add a troubleshooting section for OpenCode stream drops and `Failed to execute statement`.
- State clearly that the exact statement error may be OpenCode-local, while EggPool should be checked for correlated cancellation/timeout/midstream diagnostics.
- Provide a minimal data collection recipe:
  - EggPool version and config excerpts for `[server]`, `[database]`, `[upstream]`, `[routing.trace]`, provider account count, and dashboard enabled state.
  - count of concurrent OpenCode sessions/agents.
  - whether requests are streaming.
  - whether the failure occurs with non-streaming requests.
  - EggPool runtime diagnostics: DB lock p95/p99, finalizer timeout count, client cancellation count, midstream error count, HTTPX exception class counts.
  - provider id/account/model involved.
- Provide a diagnostic config profile:

```toml
[server]
threads = 2

[database]
worker_threads = 2
busy_timeout_ms = 10000

[upstream]
read_timeout_s = 900
pool_timeout_s = 60
max_connections = 128
max_keepalive = 64

[routing.trace]
mode = "off"
```

- Note that `routing.trace.mode = "off"` is diagnostic and high-throughput friendly, but `sampled` remains the normal default because routing traces are useful for routing investigation.

Validation:

- Documentation examples must parse under `eggpool check-config` if copied into a complete config.
- Troubleshooting docs should tell operators how to distinguish client cancellation from upstream midstream failure.

## Phase 9: Acceptance criteria

This line of work is complete when all of the following hold:

1. A deterministic high-concurrency streaming test can run with at least 50 concurrent mock streams and forced downstream cancellations without leaking pending requests, active reservations, or router active counts after bounded cleanup.
2. Stream cancellation finalizer timeout no longer means cleanup is only left to the broad stale finalizer; a targeted retry queue or equivalent mechanism retries until successful or explicitly reports terminal failure.
3. Runtime diagnostics show DB lock p95/p99, stream cancellation count, finalizer timeout count, midstream error count, pending stale count, and HTTPX exception class counts.
4. Provider health is not penalized for downstream client cancellation.
5. Routing trace pressure can be reduced under load without changing routing correctness.
6. `server.threads = 2`, `database.worker_threads = 2`, and one process worker remain the documented default posture for Raspberry Pi-class installs.
7. Documentation includes a clear OpenCode troubleshooting path and high-concurrency streaming config profile.
8. Existing request accounting, usage/cost rollups, crash recovery, dashboard rendering, protocol transcoding, and synthetic cache/compression behavior remain compatible with current tests.

## Suggested implementation order

1. Diagnostics first: add stream outcome logs, DB lock histogram, runtime counters, and tests.
2. Reproducer second: add the high-concurrency mock SSE harness and cancellation matrix.
3. Cleanup reliability third: add targeted finalization retry queue and stale runtime reconciliation.
4. Contention reduction fourth: route trace pressure guardrails and finalizer transaction audit.
5. Config/docs fifth: high-concurrency HTTP profile, OpenCode troubleshooting workflow, and default posture clarification.

## Manual verification checklist

After implementation, run the following on a local file-backed SQLite database:

```bash
eggpool check-config --config /path/to/config.toml
eggpool migrate --config /path/to/config.toml
eggpool serve --config /path/to/config.toml --verbose
python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.25
python scripts/repro_high_concurrency_streams.py --concurrency 100 --cancel-rate 0.50 --slow-first-byte-ms 1500
```

Then verify:

- no pending request rows older than the test window;
- no active reservations for completed/cancelled requests;
- router active counts return to zero;
- finalization retry queue drains to zero;
- DB lock p95 is visible in runtime diagnostics;
- any failures are classified as client cancellation, upstream midstream error, pool timeout, read timeout, or finalizer timeout rather than an opaque generic exception.

## Risk notes

- Increasing default `server.threads` beyond 2 may improve dashboard responsiveness but can increase simultaneous DB lock waiters and memory pressure on low-power hosts. Keep the default at 2 unless benchmarks prove otherwise.
- Increasing HTTP connection limits can mask upstream/provider throttling and increase file descriptor usage. Document high-concurrency values as profiles, not universal defaults.
- A finalization retry queue must be bounded and idempotent. An unbounded cleanup queue would trade leaked request rows for memory pressure.
- Do not convert diagnostic trace persistence into a correctness dependency. Trace failures must remain best-effort.
- Do not treat OpenCode's `Failed to execute statement` as definitely fixed by EggPool changes unless the repro shows the error disappears when EggPool stops dropping/stalling streams.
