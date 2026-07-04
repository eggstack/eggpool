# Background Task Overdue Remediation Plan

## Context

The runtime dashboard currently reports several background tasks as overdue by roughly the same amount of time. The affected tasks observed in the running deployment are:

- `catalog_refresh`
- `model_info_canonical_backfill`
- `usage_window_refresh`
- `stale_request_finalizer`
- `health_disabled_models_prune`
- `metrics_flush`

The repo inspection indicates that this is primarily a scheduler observability defect, with one additional high-priority correctness check around `catalog_refresh`.

`TaskSupervisor` records `last_started_at` only when it starts awaiting the task coroutine factory. Most registered background tasks are not one-shot jobs; they are long-lived `while True` loops with their own internal sleep cadence. Those loops normally never return, so `last_completed_at` remains unset and `last_started_at` remains pinned to process startup. The runtime dashboard then estimates `next_run` as `last_completed_at or last_started_at + interval_s`, so every long-lived task becomes permanently overdue after the first interval, even if it is healthy and iterating normally.

This plan remediates the root cause by moving periodic scheduling and heartbeat ownership into the supervisor, converting long-lived loop registrations into one-shot periodic ticks, and making the dashboard consume explicit scheduler fields instead of reconstructing timing from ambiguous lifecycle timestamps.

## Goals

1. Make runtime background-task health accurately distinguish running, sleeping, overdue, failed, cancelled, and permanently stopped tasks.
2. Eliminate false overdue warnings for healthy periodic loops.
3. Ensure each named task reports per-tick `last_run`, `next_run`, duration, success count, failure count, and last error.
4. Verify whether `catalog_refresh` is also failing due to a missing or stale `_catalog_refresh_loop` symbol, and fix that path if present.
5. Preserve current behavior of the background tasks: catalog refresh, model-info refresh/backfill, usage-window reload, stale request finalization, disabled-model pruning, metrics flush, retention cleanup, checkpointing, update checks, and automatic backup.
6. Add tests that would have caught the current mismatch between long-lived task loops and dashboard timing.

## Non-goals

This pass should not redesign provider routing, catalog model normalization, metrics rollup schema, quota estimation, or dashboard visual layout beyond the runtime task table fields needed for correctness. It should also avoid changing task cadences unless a cadence is already wrong or ambiguous.

## Current failure mode

The current supervisor lifecycle is appropriate for daemon-style tasks but not for interval projection. A supervised task calls the coroutine factory once, records `last_started_at`, then awaits the returned coroutine. For long-lived loops, that await never completes. Consequently:

- `iteration_count` does not represent periodic inner-loop iterations.
- `last_completed_at` remains null for healthy infinite loops.
- `last_started_at` represents outer task start, not last tick start.
- `interval_s` is metadata only; the supervisor does not enforce or observe it.
- The dashboard computes `next_run_at = anchor + interval_s`, where the anchor is effectively process startup for these loops.
- After uptime exceeds one interval, the dashboard displays the task as overdue forever.

For a process up for about 21 minutes with 60-second task intervals, this produces an apparent overdue age of about 20 minutes, matching the observed symptom.

## High-priority triage before implementation

Before changing the scheduler, inspect `src/eggpool/app.py` and confirm whether `_catalog_refresh_loop` is defined or imported. The registration currently references `_catalog_refresh_loop(...)` through a lambda. If the symbol is absent in the current tree, `catalog_refresh` is a real failed task in addition to the dashboard timing bug.

Triage steps:

1. Search for `_catalog_refresh_loop` in the full repo.
2. If it is absent, inspect recent commits around catalog refresh registration and restore the intended function or replace it with the new one-shot tick described below.
3. Check current runtime snapshots for `catalog_refresh.last_error_class`, `restart_count`, `done`, and `running`.
4. If `last_error_class == NameError` or the task has exhausted restarts, treat `catalog_refresh` as a separate regression and include a regression test for symbol resolution.

Expected result after remediation: `catalog_refresh` has a healthy periodic state and does not rely on a missing local symbol.

## Target architecture

Introduce supervisor-owned periodic scheduling with explicit tick heartbeats. Keep support for daemon-style tasks, but stop presenting daemon startup timestamps as periodic task run timestamps.

### New task modes

Add an explicit task mode field to `SupervisedTask`, for example:

- `daemon`: a long-running coroutine managed by the supervisor; no periodic next-run projection unless the task itself reports heartbeat.
- `periodic`: a supervisor-owned loop around a one-shot tick coroutine.

A minimal implementation can use a `TaskMode` string literal or enum. The snapshot should include the mode so the dashboard can render timing appropriately.

### New periodic registration API

Add a new method on `TaskSupervisor`:

```python
def register_periodic(
    self,
    name: str,
    tick_factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    interval_s: float,
    initial_delay_s: float | None = None,
    run_immediately: bool = False,
    timeout_s: float | None = None,
    max_restarts: int = 10,
) -> SupervisedTask:
    ...
```

Behavior:

- The supervisor owns the `while self._running` loop.
- For `run_immediately=True`, perform the first tick before sleeping.
- For `initial_delay_s`, sleep that amount before the first tick. If omitted and `run_immediately=False`, sleep `interval_s` before the first tick to preserve current semantics for tasks that currently sleep first.
- Record `last_tick_started_at` immediately before invoking the tick.
- Record `last_tick_completed_at`, `last_tick_duration_ms`, `success_count`, and `iteration_count` after success.
- Record `last_error_at`, `last_error_class`, `failure_count`, and `consecutive_failure_count` on tick failure.
- Continue scheduling after a tick failure unless the task has exhausted restart/failure policy.
- Compute and store `next_run_at` after each successful or failed tick, and while sleeping.
- Use monotonic time internally for sleeps/durations and wall-clock epoch for dashboard display.

The existing `register(...)` API should remain for true daemon tasks such as an update checker loop if it is not converted yet. Its snapshot should have `mode="daemon"` and should not render an overdue next-run value merely because `interval_s` is present.

### Snapshot contract

Extend `SupervisedTask.snapshot()` to return these fields:

```python
{
    "name": str,
    "registered": True,
    "mode": "periodic" | "daemon",
    "running": bool,
    "done": bool,
    "cancelled": bool,
    "restart_count": int,
    "max_restarts": int,
    "interval_s": float | None,
    "last_started_at": float | None,        # outer coroutine lifecycle, kept for compatibility
    "last_completed_at": float | None,      # outer coroutine lifecycle, kept for compatibility
    "last_tick_started_at": float | None,
    "last_tick_completed_at": float | None,
    "last_tick_duration_ms": int | None,
    "next_run_at": float | None,
    "overdue_seconds": float | None,
    "iteration_count": int,
    "success_count": int,
    "failure_count": int,
    "consecutive_failure_count": int,
    "last_failure_at": float | None,
    "last_error_at": float | None,
    "last_error_class": str | None,
}
```

For `periodic` tasks, `iteration_count` should count completed tick attempts or successful ticks consistently. Prefer `success_count + failure_count` as total attempts and keep `iteration_count` as successful completions only if existing callers already interpret it as success. Document the choice in `background/__init__.py`.

For daemon tasks, `next_run_at` and `overdue_seconds` should be null unless a daemon heartbeat is explicitly implemented. The runtime dashboard should not infer overdue state for daemon tasks.

### Overdue semantics

A task should be considered overdue only when all of the following are true:

1. `mode == "periodic"`.
2. `running == True`.
3. `next_run_at is not None`.
4. `now > next_run_at + grace_s`.
5. The task is not currently executing a tick unless the tick has exceeded a configured per-task timeout or a derived long-running threshold.

Use a grace window to avoid flicker, for example:

```python
grace_s = max(5.0, min(interval_s * 0.25, 60.0))
```

If a tick is currently running longer than expected, render it as `running long` or `tick running` rather than `overdue`, unless it has exceeded `timeout_s` or `interval_s + grace_s` by a large margin.

## Task conversion plan

Convert each affected background task from an inner infinite loop to a one-shot tick registered with `register_periodic`.

### `catalog_refresh`

Create or restore a one-shot function:

```python
async def _catalog_refresh_once(
    catalog: CatalogService,
    model_info: ModelInfoService | None,
) -> None:
    result = await catalog.refresh()
    if model_info is not None:
        await model_info.reconcile_catalog_refresh(result)
```

Adjust the exact model-info reconciliation call to match current service methods. If the current code previously used `_catalog_refresh_loop`, preserve its semantics: refresh catalog, record provider pings, persist catalog rows, and reconcile model-info canonical state after changed/new/withdrawn models.

Register it as:

```python
supervisor.register_periodic(
    "catalog_refresh",
    lambda: _catalog_refresh_once(
        catalog,
        model_info if config.model_info.enabled else None,
    ),
    interval_s=float(config.models.refresh_interval_s),
    run_immediately=False,
)
```

Preserve current startup behavior: startup refresh remains controlled by `config.models.startup_refresh`; periodic refresh should sleep first unless existing `_catalog_refresh_loop` ran immediately.

### `model_info_refresh`

Replace `model_info.run_periodic_refresh()` registration with a one-shot tick:

```python
async def _model_info_refresh_once(model_info: ModelInfoService) -> None:
    result = await model_info.refresh_due_models()
    if result["refreshed"] > 0:
        logger.info(...)
```

Register with `interval_s=float(config.model_info.refresh_interval_s)`.

Keep `run_periodic_refresh()` temporarily for backward compatibility or tests, but mark it as a thin wrapper or deprecate it internally after all call sites move.

### `model_info_canonical_backfill`

Replace `model_info.run_backfill_missing_canonical()` registration with:

```python
async def _model_info_backfill_once(model_info: ModelInfoService) -> None:
    result = await model_info.backfill_missing_canonical()
    if result["backfilled"] > 0:
        logger.info(...)
```

Register with `interval_s=60.0`.

### `usage_window_refresh`

Replace the local `while True` function with:

```python
async def _refresh_usage_windows_once() -> None:
    await router.quota_estimator.load_persisted_windows()
```

Register with `interval_s=60.0`. Preserve the existing exception logging via the supervisor's per-tick failure logging, or wrap this tick if a domain-specific log message is still desired.

### `stale_request_finalizer`

Register `_finalize_stale_requests_once(...)` directly:

```python
supervisor.register_periodic(
    "stale_request_finalizer",
    lambda: _finalize_stale_requests_once(
        db=db,
        router=router,
        quota_estimator=router.quota_estimator,
        max_pending_seconds=config.upstream.read_timeout_s,
    ),
    interval_s=60.0,
)
```

Keep `_finalize_stale_requests(...)` only for compatibility tests if needed. Prefer tests around `_finalize_stale_requests_once` and the supervisor periodic wrapper.

### `health_disabled_models_prune`

Register `_prune_health_disabled_models_once(app.state)` directly with `interval_s=60.0`.

### `metrics_flush`

Do not register `metrics_coalescer.run(metrics_stop_event)` as a periodic task. Register a one-shot flush:

```python
supervisor.register_periodic(
    "metrics_flush",
    lambda: metrics_coalescer.flush(reason="periodic"),
    interval_s=float(config.metrics.flush_interval_s),
)
```

Shutdown still needs a final flush. Move shutdown responsibility into lifespan cleanup:

1. Set `metrics_stop_event` only if it remains useful elsewhere; otherwise remove it.
2. On app shutdown, after `await supervisor.stop_all()`, call `await metrics_coalescer.flush(reason="shutdown")` with exception suppression/logging.
3. Ensure `stop_all()` cancels sleeping periodic tasks cleanly and does not skip final flush.

### Retention cleanup and checkpoint

These are also local infinite loops. Convert them in the same pass to avoid leaving known false-overdue candidates for longer-interval tasks:

- `_retention_cleanup_once`: cleanup old requests, events, pings, rollups, and reconcile expired reservations.
- `_checkpoint_once`: call `checkpoint_database(db)`.

Register with `interval_s=3600.0` and `interval_s=14400.0`, respectively.

### Update checker and automatic backup

Inspect whether `UpdateChecker.run_periodic()` and `automatic_backup_loop()` are inner loops. If yes, either convert now or mark them as `daemon` without next-run projection. Prefer converting if the underlying operation has a clean one-shot API:

- `update_checker.check_once()` or equivalent.
- `automatic_backup_once(...)` or equivalent.

If no one-shot API exists, add one and make the loop wrapper call the one-shot API. This keeps future observability consistent.

## Dashboard remediation

Update `render_runtime()` background task table so it does not calculate next-run from `last_started_at` or `last_completed_at`.

New rendering rules:

1. Read `mode`, `next_run_at`, `overdue_seconds`, `last_tick_started_at`, `last_tick_completed_at`, `last_tick_duration_ms`, `success_count`, `failure_count`, and `last_error_class` from the snapshot.
2. For `periodic` tasks:
   - If `running=false`, show `stopped` or `failed` depending on `done`, `cancelled`, and restart state.
   - If `last_tick_started_at` exists and `last_tick_completed_at` is older or null, show `tick running` with elapsed duration.
   - If `overdue_seconds > 0`, show `overdue <age>`.
   - Else if `next_run_at` exists, show `in <delta>`.
   - Else show `—`.
3. For `daemon` tasks:
   - Show `daemon` or `running` in status.
   - Show `—` for next run unless the daemon supplies an explicit heartbeat/next run.
4. Add columns or subtext for `Last run`, `Duration`, `Success`, `Failures`, and `Last error` if this does not make the table too wide. If space is constrained, keep the table compact and expose details in a secondary diagnostics row or tooltip.

Remove the stale comment that says all tasks are long-lived loops and therefore anchor on `last_started_at`. That comment describes the bug, not desired behavior.

## Runtime API remediation

`RuntimeMetricsService._snapshot_background_tasks()` can remain mostly unchanged if the supervisor snapshot owns the fields. Validate that no runtime endpoint or dashboard code assumes the old field set only.

Add a lightweight derived summary to the runtime snapshot if useful:

```python
"background_task_summary": {
    "registered": int,
    "running": int,
    "failed": int,
    "overdue": int,
    "last_error_count": int,
}
```

This is optional, but useful for future overview cards.

## Tests

Add focused tests under the existing test structure. Prefer deterministic short intervals and avoid real 60-second sleeps.

### Supervisor unit tests

1. `test_register_periodic_runs_tick_and_updates_heartbeat`
   - Register a periodic task with `interval_s=0.05`.
   - Let it run at least two ticks.
   - Assert `last_tick_started_at`, `last_tick_completed_at`, `next_run_at`, `success_count`, and `iteration_count` update.
   - Assert `last_started_at` remains the outer lifecycle timestamp but is not used for `next_run_at`.

2. `test_periodic_task_failure_records_error_and_continues`
   - First tick raises a custom exception, second tick succeeds.
   - Assert `failure_count == 1`, `last_error_class` is set, `success_count >= 1`, and the task remains running.

3. `test_periodic_task_overdue_only_when_scheduler_misses_deadline`
   - Use a controllable clock if the supervisor supports injection, or test a pure helper that computes overdue state from snapshot fields.
   - Assert sleeping healthy task is not overdue.
   - Assert a forced stale `next_run_at` produces `overdue_seconds`.

4. `test_daemon_task_does_not_project_next_run_from_start_time`
   - Register a daemon-style coroutine with `interval_s` metadata if backward compatibility keeps that possible.
   - Assert `next_run_at is None` and `overdue_seconds is None`.

### App registration tests

1. `test_lifespan_registers_periodic_tasks_with_one_shot_ticks`
   - Build a minimal app config with model info enabled and metrics buffered.
   - Start lifespan with mocked catalog/model-info/coalescer if needed.
   - Assert named tasks have `mode == "periodic"`.

2. `test_catalog_refresh_registration_resolves_symbol`
   - Explicitly start only the `catalog_refresh` task or invoke its tick factory.
   - Assert no `NameError` and no immediate supervisor failure.

3. `test_metrics_flush_shutdown_flushes_once`
   - Record a buffered event.
   - Stop the app/supervisor.
   - Assert final shutdown flush was invoked and buffer drained.

### Dashboard rendering tests

1. `test_runtime_background_table_uses_next_run_at`
   - Provide a synthetic snapshot with `last_started_at` 20 minutes ago, `next_run_at` 30 seconds in the future.
   - Assert rendered HTML says `in 30s` or equivalent, not `overdue`.

2. `test_runtime_background_table_renders_overdue_from_overdue_seconds`
   - Provide `overdue_seconds=120` and assert `overdue` appears.

3. `test_runtime_background_table_daemon_no_overdue`
   - Provide a daemon snapshot with old `last_started_at` and `interval_s=60`.
   - Assert no `overdue` string is rendered.

### Integration smoke test

Run a short-lived test app with reduced intervals, wait for multiple cycles, fetch `/api/stats/runtime` or render runtime snapshot, and verify:

- `model_info_canonical_backfill`, `usage_window_refresh`, `stale_request_finalizer`, `health_disabled_models_prune`, and `metrics_flush` report nonzero heartbeat progress.
- `next_run_at` advances over time.
- No healthy task is overdue after normal cycles.
- If catalog refresh is enabled, it does not exhaust restarts.

## Migration and compatibility notes

Keep old snapshot keys for compatibility:

- `last_started_at`
- `last_completed_at`
- `iteration_count`
- `restart_count`
- `last_error_at`
- `last_error_class`
- `interval_s`

Add new keys without removing old ones. Dashboard should prefer new fields when present. External consumers of `/api/stats/runtime` should continue to parse old snapshots.

If any tests currently assert that `iteration_count` increments only when the outer coroutine exits, update them. That behavior is not useful for periodic observability.

## Implementation sequence

1. Add `mode`, periodic heartbeat fields, and snapshot fields to `SupervisedTask`.
2. Implement `TaskSupervisor.register_periodic(...)` and the internal periodic runner.
3. Add supervisor unit tests for periodic success, periodic failure, daemon non-overdue behavior, and stop/cancel semantics.
4. Convert `metrics_flush` first because it is self-contained and exposes a clear before/after heartbeat.
5. Move shutdown final flush into lifespan cleanup.
6. Convert `usage_window_refresh`, `stale_request_finalizer`, and `health_disabled_models_prune` to one-shot ticks.
7. Convert `model_info_refresh` and `model_info_canonical_backfill` to one-shot ticks; retain the old loop methods if needed for compatibility.
8. Fix or replace `catalog_refresh` registration. Confirm `_catalog_refresh_loop` is either restored or no longer referenced.
9. Convert retention cleanup and checkpoint to `register_periodic`.
10. Inspect update checker and automatic backup; convert to one-shot periodic tasks if practical, otherwise mark as daemon with no next-run projection.
11. Update runtime dashboard rendering to use `next_run_at` and `overdue_seconds` from the snapshot.
12. Add dashboard rendering tests.
13. Add runtime integration smoke test with short intervals.
14. Run `ruff`, `pyright`, and the targeted pytest subset.
15. Run the full test suite if the targeted subset is clean.

## Acceptance criteria

The pass is complete when all of the following are true:

1. The six originally affected tasks no longer show false overdue status after the process has been up longer than their interval.
2. Runtime snapshots expose explicit `mode`, `last_tick_started_at`, `last_tick_completed_at`, `last_tick_duration_ms`, `next_run_at`, `overdue_seconds`, `success_count`, and `failure_count` for periodic tasks.
3. The runtime dashboard does not compute next run from `last_started_at + interval_s` for daemon or long-lived tasks.
4. `catalog_refresh` starts without `NameError`, does not exhaust restarts, and advances heartbeat after at least one interval.
5. `metrics_flush` still performs a final shutdown flush.
6. Existing startup refresh behavior remains unchanged.
7. Existing cleanup/finalizer side effects remain unchanged.
8. Tests cover periodic heartbeat advancement, failure continuation, daemon non-overdue rendering, dashboard use of `next_run_at`, and catalog refresh symbol resolution.
9. `ruff`, `pyright`, and relevant tests pass.

## Operator verification checklist

After deploying the fix, restart EggPool and check the runtime page after at least two task intervals. For the default 60-second tasks, wait more than two minutes.

Expected observations:

- `model_info_canonical_backfill`, `usage_window_refresh`, `stale_request_finalizer`, `health_disabled_models_prune`, and `metrics_flush` show either `in <duration>` or a recent last-run timestamp, not a monotonic overdue age.
- `catalog_refresh` shows `running` and does not report `NameError` or max restarts.
- `restart_count` remains stable unless a real upstream/DB error occurs.
- `failure_count` remains zero or explains real transient failures via `last_error_class`.
- `metrics_buffer.last_flush_ts` advances when buffered mode is enabled and events are flowing.
- Pending requests and active reservations remain bounded; stale request finalizer still records cleanup events when leaks are present.

## Risk notes

The main risk is accidentally changing first-run timing. Several existing loops sleep before their first periodic action. Preserve that behavior unless there is an explicit reason to run immediately. Startup-specific work is already handled elsewhere for catalog refresh, model info, crash recovery, and reservation reconciliation.

The second risk is final shutdown behavior for metrics. `metrics_coalescer.run()` currently performs a final flush after its loop exits. Once it is replaced with one-shot periodic flushes, the lifespan cleanup must explicitly flush on shutdown.

The third risk is over-counting failures. A tick that catches and logs its own exceptions will appear successful to the supervisor. Prefer letting the supervisor catch exceptions for new one-shot ticks, except where the task must convert expected domain errors into non-fatal results.

## Suggested commit breakdown

1. `background: add periodic task heartbeat support`
2. `app: convert runtime background loops to supervisor periodic ticks`
3. `dashboard: render scheduler-owned background task timing`
4. `tests: cover background task heartbeat and overdue rendering`

These can be separate commits or a single focused remediation commit if the repo is moving quickly.
