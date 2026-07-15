# Dispatch Stability Milestone A — Scheduler Correctness and Baseline Instrumentation

Date: 2026-07-15
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: A of G

## Objective

Correct the confirmed periodic scheduler cadence defect, establish trustworthy timing boundaries, and create the baseline measurement harness required to evaluate every later dispatch-stability change.

This milestone must land before architectural performance work. Current periodic tasks can repeatedly sleep for `initial_delay_s` instead of using the configured interval after the first tick. That behavior increases database activity and invalidates any comparison that assumes the configured task cadence is actually in effect.

## Problem statement

`SupervisedTask._run_periodic_loop()` currently computes the first delay inside the loop on every iteration. A task configured with `initial_delay_s=5` and `interval_s=30` can therefore run every five seconds indefinitely. The assignment intended to switch subsequent sleeps to `interval_s` is local to one loop iteration and is discarded when the next iteration recomputes the delay.

Affected task registrations include, depending on configuration:

- `model_info_refresh`;
- `model_info_canonical_backfill`;
- `retention_cleanup`;
- `checkpoint`;
- `usage_window_refresh`;
- `finalization_retry_drain`;
- `stale_request_finalizer`;
- `health_disabled_models_prune`;
- `metrics_flush`;
- `update_checker`;
- `automatic_backup`;
- catalog refresh and any future periodic task using an initial delay.

The milestone also needs to make the dispatch metric boundary explicit. `DispatchOverheadRecorder` records immediately before `httpx.AsyncClient.send()`, beginning from `ProxyRequestContext.started_monotonic_ns`. Some HTTP body parsing, request validation, compression/segmentation preparation, and context construction occur before that boundary. Operators must be able to distinguish:

- total local pre-upstream latency;
- coordinator dispatch overhead;
- routing and selection latency;
- SQLite queue/transaction latency;
- upstream connection and header latency.

## Scope

### In scope

- Fix one-time initial-delay semantics.
- Preserve live task-spec updates and task supervisor lifecycle behavior.
- Add cadence and scheduler-drift diagnostics.
- Audit all task registrations for intended first-run behavior.
- Clarify and, where necessary, augment dispatch timing boundaries.
- Add a deterministic benchmark/soak harness for dispatch, SQLite contention, and background task cadence.
- Capture baseline results before milestones B–G.
- Add configuration/profile logging required to interpret measurements.

### Out of scope

- Refactoring `_select_lock` or dispatch persistence.
- Adding a dispatch writer or microbatching.
- Moving routing traces off-path.
- Chunking retention work.
- Changing Granian worker count.
- Changing routing, quota, retry, billing, transcode, compression, or cache semantics.

## Target files and modules

Primary:

- `src/eggpool/background/__init__.py`
- `src/eggpool/runtime_tasks.py`
- `src/eggpool/runtime_task_inventory.py`
- `src/eggpool/runtime_dispatch.py`
- `src/eggpool/runtime_metrics.py`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/app.py`
- `src/eggpool/models/config.py`

Tests and tooling:

- `tests/unit/test_background*.py`
- `tests/unit/test_runtime_tasks*.py`
- `tests/unit/test_runtime_dispatch*.py`
- `tests/integration/test_runtime_metrics*.py`
- `tests/integration/test_dispatch*.py`
- a new focused benchmark/soak module under `tests/performance/` or the repository's existing performance-test location
- optional operator CLI support under `src/eggpool/cli_full.py` if a baseline command is added

Documentation:

- `architecture/README.md`
- `docs/deployment.md`
- `docs/raspberry-pi.md`
- `config.example.toml`

## Workstream A1 — Correct one-time initial-delay semantics

### Required implementation

Refactor `SupervisedTask._run_periodic_loop()` so the initial delay is resolved once before the loop and consumed exactly once.

The intended state machine is:

```text
registered
  -> optional one-time initial delay
  -> tick 1
  -> interval delay
  -> tick 2
  -> interval delay
  -> tick N
```

Do not recompute the first delay from `_initial_delay_s` on each iteration.

The implementation must continue to support live changes to `_interval_s`, `_timeout_s`, and other task scheduling attributes. A live interval update should take effect at the next appropriate tick boundary. Decide and document whether updating `initial_delay_s` after the task has already completed its first sleep has any effect. Recommended behavior: it has no effect after the initial delay has been consumed.

Use explicit scheduler state rather than inference from iteration counters where practical, for example:

- local `initial_delay_pending` boolean;
- local `next_delay_s` initialized once;
- or a private field such as `_initial_delay_consumed` if diagnostics require visibility.

Avoid resetting the initial-delay state during ordinary tick failures. A failed first tick should still be followed by the normal configured interval unless the task is fully stopped and restarted. Define whether a deliberate `stop()` followed by `start()` re-applies the initial delay. Recommended behavior: yes, because it represents a new supervisor lifecycle.

### Failure behavior

- Cancellation during initial sleep must stop promptly.
- Cancellation during a tick must stop promptly and must not schedule another tick.
- Tick exceptions continue to update failure counters and schedule the next attempt according to the normal interval/backoff contract already established by the supervisor.
- A zero or missing interval must retain current validation behavior; do not create a tight loop.

## Workstream A2 — Audit all periodic task registrations

Create a review table for every periodic task with:

- ownership: process or generation;
- configured interval;
- intended first-run behavior;
- `run_immediately` behavior;
- explicit or derived initial delay;
- timeout;
- primary resources touched;
- whether the tick performs primary SQLite writes;
- whether the tick can overlap with itself;
- whether rehash can duplicate or transition it.

Use `runtime_task_inventory.py` and `runtime_tasks.py` as the authoritative sources. Resolve any divergence between inventory and registration.

Specific review points:

- `checkpoint` currently uses `run_immediately=True`; verify that it does not also inherit an unintended initial delay.
- `model_info_refresh` uses `run_immediately=True`; verify force-on-first-run state remains one-shot.
- `metrics_flush` should retain its intended first offset but then use `metrics.flush_interval_s`.
- `automatic_backup` should honor `startup_delay_s` once and `interval_s` thereafter.
- process-owned tasks must not be duplicated on rehash.
- generation-owned tasks must stop cleanly on retirement.

Add a test that compares the inventory-derived resolved schedule with the actual registered `SupervisedTask` fields for all built-in tasks.

## Workstream A3 — Add cadence and scheduler-drift diagnostics

Extend task snapshots so operators can identify a cadence defect directly. Add fields such as:

- `configured_interval_s`;
- `configured_initial_delay_s`;
- `initial_delay_consumed`;
- `last_tick_started_at`;
- `last_tick_completed_at`;
- `last_tick_duration_ms`;
- `previous_tick_started_at` or `observed_last_interval_s`;
- `schedule_drift_s`, computed against the intended next-run timestamp;
- `next_run_at`;
- `tick_in_progress`;
- success/failure/consecutive-failure counts.

The snapshot must not require awaiting a lock or performing database I/O.

Define scheduler drift carefully:

```text
actual_tick_start - scheduled_tick_start
```

Do not conflate tick duration with drift. Positive drift means the scheduler began late, often because of event-loop starvation. A long tick that completes late should affect the following schedule according to the supervisor's documented fixed-delay or fixed-rate policy.

Document whether the scheduler is fixed-delay or fixed-rate. The current behavior is effectively fixed-delay because the next interval begins after tick completion. Preserve that behavior in this milestone unless changing it is necessary for correctness. Fixed-delay is safer for database maintenance because it prevents overlapping ticks.

## Workstream A4 — Clarify dispatch timing boundaries

Document the existing coarse dispatch metric precisely and add missing timing points if needed.

Recommended timing model:

- `request_received_ns`: earliest practical ASGI handler entry after authentication/body-limit middleware;
- `body_read_done_ns`;
- `preprocessing_done_ns`;
- `context_started_ns`: current `ProxyRequestContext.started_monotonic_ns` boundary;
- `routing_plan_done_ns`;
- `selection_claim_acquired_ns`;
- `dispatch_persistence_done_ns`;
- `upstream_send_start_ns`: current coarse dispatch endpoint;
- `upstream_headers_received_ns`.

Do not change the meaning of the existing dashboard metric silently. Either:

1. preserve `dispatch_overhead` as the coordinator-local metric and introduce a new total local pre-upstream metric; or
2. version/rename the metric and provide compatibility fields for one release.

Preferred option: preserve `dispatch_overhead` and add `local_pre_upstream_ms` or equivalent.

Ensure every timer uses a monotonic/performance clock. Wall-clock timestamps may be recorded separately for display but not duration calculations.

## Workstream A5 — Establish a deterministic baseline harness

Create a test/benchmark harness that can run without external provider variability. Use a local mock upstream supporting:

- immediate non-streaming success;
- delayed response headers;
- chunked streaming;
- long-running streaming;
- configurable 429/5xx failures;
- connection close/reset;
- controlled cancellation;
- response usage blocks for OpenAI and Anthropic shapes;
- native and transcoded request paths.

The harness should drive at least these workloads:

1. Low-volume serial native requests.
2. Moderate native concurrency.
3. Moderate transcoded concurrency.
4. Mixed native/transcoded streams.
5. Cancellation burst during active streams.
6. Retry burst with alternate accounts.
7. Dashboard polling with `database.worker_threads=1` and `2`.
8. Background tasks enabled at accelerated test intervals.
9. Deliberately blocked primary DB transaction.
10. Rehash during in-flight streams.

Capture:

- request throughput;
- local pre-upstream p50/p95/p99;
- dispatch overhead p50/p95/p99;
- routing-plan p50/p95/p99;
- selection lock wait/held p50/p95/p99;
- DB lock wait p50/p95/p99/max;
- finalization retry queue depth/age;
- background task cadence and duration;
- event-loop lag;
- RSS/FD/thread counts where available;
- DB and WAL sizes.

Store only compact machine-readable summary artifacts in CI. Do not commit large raw traces.

## Workstream A6 — Baseline operational profile logging

At startup, log one structured line with:

- Granian workers and runtime threads;
- primary/stats SQLite connection count;
- `database.worker_threads`;
- WAL and synchronous mode;
- routing trace mode/sample rate;
- metrics write mode/flush interval;
- enabled compression/transcoder/cache modes;
- background task count and process/generation ownership counts.

The log must not include secrets, provider keys, raw URLs containing credentials, or request content.

## Test plan

### Unit tests

- `initial_delay_s=5`, `interval_s=30`: first tick after 5, second/third after 30 each.
- no initial delay: all ticks use interval.
- `run_immediately=True`: first tick occurs without interval sleep; later ticks use interval.
- first tick failure: later tick uses interval, not initial delay.
- stop during initial sleep.
- stop during tick.
- stop/start lifecycle reapplies initial delay exactly once.
- live interval update applies at documented boundary.
- runtime task snapshot reports observed interval and drift correctly.
- coarse dispatch recorder remains bounded and retains existing percentile semantics.

Use a fake clock or patched sleep where possible. Avoid wall-clock-flaky tests.

### Integration tests

- Start the app with accelerated intervals and observe at least three ticks per selected task.
- Assert process-owned tasks are not duplicated by successful rehash.
- Assert generation-owned tasks are replaced/retired once.
- Run native and transcoded local-upstream requests and verify all timing fields are nonnegative and ordered.
- Run dashboard polling while dispatching and collect baseline DB lock waits.

### Regression suite

- full unit suite;
- background supervisor tests;
- runtime task inventory/reload tests;
- runtime metrics/dashboard tests;
- rehash acceptance tests;
- proxy native/transcode/streaming tests.

## Acceptance criteria

1. `initial_delay_s` is consumed once per task start lifecycle.
2. At least three-tick tests prove the second and later delays use `interval_s`.
3. Failure and cancellation do not reset one-time initial-delay state unexpectedly.
4. Every built-in periodic task has documented ownership, interval, first-run behavior, and timeout.
5. Runtime diagnostics expose configured interval, observed interval, drift, and tick duration.
6. Existing task-spec live update and rehash behavior remains correct.
7. Existing dispatch metric meaning is documented; any new total-local metric is explicitly named and monotonic-clock based.
8. A deterministic local-upstream baseline harness exists and records the required latency, DB, queue, task, and resource measurements.
9. Baseline results are captured for at least serial native, concurrent native, concurrent transcoded, mixed streaming, cancellation, dashboard polling, and blocked-DB cases.
10. No routing, billing, quota, retry, finalization, transcode, compression, cache, dashboard, or security behavior changes.
11. Full tests, ruff, format check, and pyright pass.

## Deliverable evidence for handoff

The implementation handoff should include:

- the scheduler fix commit;
- a concise before/after cadence table;
- baseline machine-readable summaries;
- a runtime snapshot showing actual intervals after startup;
- test names covering the three-tick regression;
- documented timing boundaries;
- any follow-up risks that should alter milestone B design.

## Risks and mitigations

### Risk: live interval changes become less responsive

Mitigation: re-read `_interval_s` after every tick and before scheduling the next interval. Do not cache the interval for the life of the task.

### Risk: timing instrumentation adds hot-path overhead

Mitigation: record raw integer nanosecond timestamps and aggregate through existing bounded recorders. Avoid per-request logging and expensive label construction.

### Risk: CI timing tests become flaky

Mitigation: use fake clocks, injected sleepers, events, or deterministic task stepping. Reserve real-time assertions for coarse integration tolerances.

### Risk: baseline is mistaken for a universal performance target

Mitigation: record hardware, Python, SQLite, storage, and configuration metadata. Use the baseline for relative comparisons and stability trends, not absolute guarantees across all hosts.

## Exit condition

Milestone A is complete when the scheduler cadence defect is fixed and covered, task runtime diagnostics are trustworthy, dispatch timing boundaries are explicit, and the repository has a repeatable local-upstream baseline harness suitable for validating the lock and persistence changes in milestones B and C.