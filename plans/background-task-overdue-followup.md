# Background Task Overdue Follow-up Plan

## Context

The primary background-task overdue remediation has landed. The repo now has supervisor-owned periodic scheduling through `TaskSupervisor.register_periodic(...)`, explicit periodic heartbeat fields, converted runtime loops, dashboard rendering based on `next_run_at` / `overdue_seconds`, and runtime summary counters.

The remaining issues are narrow correctness gaps observed after reviewing the implementation:

1. `initial_delay_s` is accepted and used to prime the dashboard's first `next_run_at`, but the actual periodic runner always sleeps `interval_s` before the first tick. This makes the displayed first-run deadline diverge from actual scheduling, and it breaks the stated backup startup-delay semantics when `config.backup.startup_delay_s != config.backup.interval_s`.
2. Lifespan shutdown currently flushes buffered metrics before stopping the supervisor. Since `metrics_flush` is now a supervisor-owned periodic tick, the final shutdown flush should happen after periodic tasks are stopped/cancelled so it cannot race an in-flight periodic flush.
3. The public scheduler API accepts `run_immediately` and `timeout_s`, but both are explicitly deleted as reserved. That is acceptable only if documentation and tests make the current semantics clear. If retained, these arguments should either be implemented or made private/deferred before they become relied on by callers.

This plan closes those gaps without reopening the larger scheduler design.

## Goals

1. Make actual first-tick scheduling match the `next_run_at` value shown in runtime snapshots.
2. Preserve historical backup startup-delay semantics under `register_periodic(..., initial_delay_s=...)`.
3. Ensure final metrics shutdown flush occurs after supervisor periodic tasks have stopped.
4. Add tests that prove the scheduler's first-delay, shutdown-drain, and dashboard deadline behavior remain correct.
5. Keep the existing snapshot contract and dashboard behavior intact.

## Non-goals

Do not redesign `TaskSupervisor`, add a persistent scheduler store, change default task cadences, alter request routing, or modify model-info enrichment behavior. Do not broaden this pass into the unrelated model-info, quota, cost, or OpenRouter changes that landed in the same commit window.

## Issue 1 — `initial_delay_s` mismatch

### Current behavior

`TaskSupervisor.register_periodic(...)` accepts `initial_delay_s` and computes:

```python
delay_s = float(interval_s if initial_delay_s is None else initial_delay_s)
task._next_run_at = time.time() + delay_s
```

However, `SupervisedTask._run_periodic_loop()` does not use `delay_s`; it always sleeps `interval_s` before every tick, including the first tick:

```python
while self._running:
    await asyncio.sleep(interval_s)
    ... run tick ...
```

This means the runtime dashboard can show a first run at `now + startup_delay_s` while the task actually sleeps until `now + interval_s`. For `automatic_backup`, the app comment says `initial_delay_s = config.backup.startup_delay_s` preserves historical startup behavior, but the runner does not honor that delay.

### Required fix

Add a stored first-delay field to `SupervisedTask`, for example `_initial_delay_s: float | None = None`, and wire it through `register_periodic(...)`.

Suggested shape:

```python
@dataclass
class SupervisedTask:
    ...
    _initial_delay_s: float | None = None
    _run_immediately: bool = False
```

Then in `register_periodic(...)`:

```python
initial = 0.0 if run_immediately else (
    float(interval_s) if initial_delay_s is None else float(initial_delay_s)
)

task = SupervisedTask(
    ...,
    _interval_s=float(interval_s),
    _initial_delay_s=initial,
    _run_immediately=run_immediately,
)
task._next_run_at = time.time() + initial
```

Then in `_run_periodic_loop()`:

```python
first_delay_s = (
    float(self._initial_delay_s)
    if self._initial_delay_s is not None
    else interval_s
)
next_delay_s = first_delay_s
while self._running:
    if next_delay_s > 0:
        await asyncio.sleep(next_delay_s)
    ... run tick ...
    self._next_run_at = tick_completed + interval_s
    next_delay_s = interval_s
```

For correctness under long tick duration, keep current fixed-delay-after-completion semantics unless there is an explicit decision to move to fixed-rate scheduling. This plan assumes fixed-delay scheduling is intended because the previous inner loops all slept before work and then performed work; the next cycle began with another sleep after the work completed.

### Validation rules

- `initial_delay_s` must be `>= 0` if supplied.
- `interval_s` must remain `> 0`.
- `run_immediately=True` should mean first delay is zero. If the project does not want to support this yet, remove the argument from the public method or keep it documented as unsupported and do not expose it in app code. Prefer implementing it because it is simple and useful for future scheduler registrations.
- `timeout_s` can remain unsupported for now, but the docstring should say `timeout_s` is accepted for forward compatibility and currently unused. It should not imply enforcement.

## Issue 2 — shutdown metrics flush ordering

### Current behavior

The lifespan cleanup currently does:

1. `metrics_coalescer.flush(reason="shutdown")`
2. `supervisor.stop_all()`
3. close client pools / outbound manager / database

This ordering was safe when the metrics coalescer owned a long-running loop and shutdown flush happened inside that loop. Now `metrics_flush` is a supervisor-owned periodic tick. A final flush before `stop_all()` can overlap with an already-running periodic flush tick, or a tick may start after the final flush but before supervisor cancellation.

### Required fix

Reorder shutdown so the supervisor is stopped before the final metrics drain:

1. `supervisor.stop_all()`
2. `metrics_coalescer.flush(reason="shutdown")` with timeout and logging
3. close client pools / outbound manager / database

The key invariant is: no periodic `metrics_flush` task should be running or able to start when the final shutdown flush drains the buffer.

Suggested structure:

```python
supervisor = getattr(app.state, "supervisor", None)
if supervisor is not None:
    try:
        await supervisor.stop_all()
    except Exception:
        logger.exception("Error stopping background tasks during shutdown")

metrics_coalescer = getattr(app.state, "metrics_coalescer", None)
if metrics_coalescer is not None:
    try:
        await asyncio.wait_for(
            metrics_coalescer.flush(reason="shutdown"),
            timeout=5.0,
        )
    except Exception:
        logger.exception("Error flushing metrics buffer during shutdown")
```

If there is concern that stopping the supervisor may cancel an in-flight metrics flush after it has snapshotted and cleared the buffer but before the DB upsert completes, inspect `MetricsWriteCoalescer.flush()`. It snapshots and clears the buffer before DB I/O. Cancellation during DB I/O could lose the snapshotted batch because the local `buffer_snapshot` would not be restored. The safer medium-term option is to shield flush ticks or make `flush()` cancellation-safe. See Issue 3 below.

## Issue 3 — cancellation safety of periodic metrics flush

### Risk

`MetricsWriteCoalescer.flush()` clears `_buffer` and `_pending_events` before writing rollup rows. If the periodic `metrics_flush` task is cancelled after clearing the buffer but before `rollup_repo.upsert_many(rows)` completes, the batch can be lost.

This risk existed before if the coalescer loop was cancelled during `flush()`, but the new supervisor-owned periodic tick makes the shutdown path more obviously dependent on cancellation ordering.

### Required fix options

Prefer option A for a small corrective pass.

Option A: shield metrics flush tick from cancellation once it starts.

```python
async def _metrics_flush_once() -> None:
    await asyncio.shield(metrics_coalescer.flush(reason="periodic"))
```

Then `stop_all()` waits for the in-flight shielded flush to finish unless the outer cancellation handling still interrupts the await. Verify with a test; if `stop_all()` cancels the task awaiting `shield`, the inner task continues running, but `stop_all()` may return before it completes unless the inner task is retained. If using `shield`, retain and await the flush task explicitly or use option B.

Option B: make `MetricsWriteCoalescer.flush()` cancellation-safe by restoring the snapshot on cancellation.

```python
async with self._lock:
    buffer_snapshot = self._buffer
    event_count = self._pending_events
    self._buffer = {}
    self._pending_events = 0
try:
    rows = self._build_rollup_rows(buffer_snapshot)
    if rows:
        await self._rollup_repo.upsert_many(rows)
except asyncio.CancelledError:
    async with self._lock:
        self._merge_snapshot_back(buffer_snapshot, event_count)
    raise
```

Option B is safer but needs a merge helper because new events may have arrived while DB I/O was pending. If implemented, ensure counters do not double-count `total_received` and do not incorrectly increment `total_dropped`.

Option C: change `SupervisedTask.stop()` to support graceful cancellation for in-progress periodic ticks. It would set `_running=False`, avoid cancelling if `_tick_in_progress=True`, and await completion with a bounded timeout before cancelling. This is a broader scheduler behavior change and should only be done if tests make the semantics unambiguous.

Minimum acceptance for this follow-up: reorder shutdown and add a regression test showing the final shutdown flush occurs after supervisor stop. Stronger acceptance: make metrics flush cancellation-safe or shielded and test no buffered events are lost on stop.

## Issue 4 — snapshot duration field

The original remediation plan asked for `last_tick_duration_ms`. The landed snapshot includes tick start/completion and counters, but not an explicit duration field. The dashboard can infer some status from timestamps, but a duration field would simplify runtime diagnostics and future alerting.

Add:

```python
_last_tick_duration_ms: int | None = None
```

Set it on success and failure:

```python
self._last_tick_duration_ms = int((tick_completed - tick_started) * 1000)
```

Expose as `last_tick_duration_ms` in `snapshot()`. Add it to tests and optionally render it in the background task table at low priority or leave it API-only for now.

This is not critical for the false-overdue bug, but it completes the snapshot contract described in the previous plan and docs.

## Implementation sequence

1. Add `_initial_delay_s`, optional `_run_immediately`, and `_last_tick_duration_ms` fields to `SupervisedTask`.
2. Update `TaskSupervisor.register_periodic(...)` to validate and store `initial_delay_s` and `run_immediately` instead of deleting `run_immediately`.
3. Update `_run_periodic_loop()` to sleep `initial_delay_s` before the first tick, then `interval_s` between subsequent ticks.
4. Ensure `_next_run_at` is initialized to the actual first deadline and updated after each tick.
5. Add `last_tick_duration_ms` to `snapshot()`.
6. Reorder lifespan shutdown: stop supervisor first, then perform final `metrics_coalescer.flush(reason="shutdown")`, then close clients and databases.
7. Decide whether to implement metrics flush cancellation safety in this pass. If yes, prefer a cancellation-safe `flush()` restore path with tests. If no, document the residual risk and add a follow-up TODO near the shutdown flush.
8. Update docs/comments that currently describe `run_immediately` and `timeout_s` as reserved if the implementation changes.
9. Run targeted tests, then full test suite.

## Tests to add or update

### Background supervisor tests

1. `test_register_periodic_honors_initial_delay_before_first_tick`
   - Register a task with `interval_s=0.20`, `initial_delay_s=0.05`.
   - Assert the first tick fires near 0.05s, not near 0.20s.
   - Assert the pre-first-tick snapshot `next_run_at` is consistent with `initial_delay_s`.

2. `test_register_periodic_run_immediately_first_tick`
   - Register with `interval_s=1.0`, `run_immediately=True`.
   - Assert first tick occurs without waiting one second.
   - Keep timing tolerance loose enough for CI.

3. `test_register_periodic_rejects_negative_initial_delay`
   - `initial_delay_s=-1` raises `ValueError`.

4. `test_periodic_snapshot_reports_last_tick_duration_ms`
   - Tick sleeps briefly.
   - Assert `last_tick_duration_ms` is non-null and non-negative after completion.

5. `test_periodic_next_run_matches_initial_delay_before_start`
   - Register but do not start.
   - Snapshot should report `next_run_at` roughly `now + initial_delay_s`.

### App/shutdown tests

1. `test_lifespan_shutdown_stops_supervisor_before_final_metrics_flush`
   - Use a fake supervisor and fake metrics coalescer or monkeypatch app state objects.
   - Record call order.
   - Assert `stop_all` occurs before `flush("shutdown")`.

2. `test_automatic_backup_initial_delay_wired_to_scheduler`
   - Build app config with `backup.enabled=true`, `backup.interval_s` large, `backup.startup_delay_s` small.
   - Inspect the registered `automatic_backup` task snapshot before start or immediately after registration.
   - Assert first `next_run_at` matches startup delay, not interval.
   - If internal field access is required, keep it in unit-level supervisor tests and only smoke-test app registration mode.

3. `test_metrics_shutdown_flush_after_periodic_stop_drains_buffer`
   - In buffered metrics mode, add a fake/real buffered event.
   - Trigger lifespan shutdown.
   - Assert shutdown flush was called after supervisor stop and buffer is empty.

### Metrics coalescer cancellation test if implementing Issue 3

1. `test_metrics_flush_restores_buffer_on_cancellation`
   - Use a rollup repo stub whose `upsert_many()` blocks/cancels.
   - Populate buffer.
   - Cancel `flush()` after snapshot clearing.
   - Assert buffered event count is restored.

2. `test_metrics_flush_does_not_double_count_received_on_restore`
   - Ensure restoring the buffer does not increment `total_events_received` again.

## Acceptance criteria

The follow-up pass is complete when:

1. `initial_delay_s` affects actual first tick timing, not only dashboard projection.
2. `automatic_backup` first run honors `config.backup.startup_delay_s` when configured.
3. `run_immediately=True` either works and is tested, or the argument is removed/deferred so callers cannot assume it works.
4. Runtime snapshots include `last_tick_duration_ms` or docs are corrected to remove that promised field.
5. Lifespan shutdown stops supervisor tasks before final metrics flush.
6. No final metrics flush can race with a newly-started periodic `metrics_flush` tick during normal clean shutdown.
7. Targeted unit tests cover the first-delay, dashboard deadline, and shutdown-order cases.
8. `ruff`, `pyright`, and targeted pytest pass.

## Operator verification after implementation

After deploying the follow-up:

1. Restart EggPool.
2. Open `/runtime` immediately after startup.
3. Confirm periodic tasks show first `Next run` values matching their configured initial delays.
4. For `automatic_backup`, set a short `startup_delay_s` in a test config and verify the first backup attempt occurs after that startup delay, not after the full backup interval.
5. Let the service run for two cycles of the 60-second tasks. Confirm `usage_window_refresh`, `stale_request_finalizer`, `health_disabled_models_prune`, `model_info_canonical_backfill`, and `metrics_flush` show fresh next-run values and do not drift into false overdue.
6. Trigger a clean shutdown with buffered metrics enabled and confirm the final flush completes after background task stop without errors in logs.

## Suggested commit breakdown

1. `background: honor periodic initial delay`
2. `app: stop supervisor before shutdown metrics flush`
3. `tests: cover periodic first-delay and shutdown flush order`

A single focused commit is also acceptable because the changes are tightly coupled around scheduler correctness.
