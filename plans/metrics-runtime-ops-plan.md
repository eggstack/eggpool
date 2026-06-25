# Runtime and Operations Metrics Implementation Plan

## Purpose

EggPool is intended to run comfortably on small SBCs such as Raspberry Pis. Request/account/model metrics are already useful, but they do not explain process topology, memory pressure, background task health, database contention risk, or deployment-mode problems. This plan adds a lightweight runtime/operations metrics surface to catch issues like unexpected multi-process Granian behavior, CPU/RSS spikes from cron/watchdog commands, stuck background tasks, pending request leaks, and SQLite operational degradation.

Core request/routing metrics are covered in `plans/metrics-core-api-plan.md`. Dashboard wiring is covered in `plans/metrics-dashboard-plan.md`.

## Design constraints

Do not turn EggPool into a host monitoring daemon. Runtime metrics should be process-local, cheap, and useful for deployment debugging. Avoid heavyweight dependencies. Prefer standard-library data and optional best-effort POSIX/Linux probes.

Do not poll expensive process tree scans from cron paths. `eggpool croncheck` and `eggpool ensure-running` should remain lightweight. Runtime metrics are for server endpoints/dashboard and explicit CLI diagnostics, not for every watchdog tick.

Keep runtime endpoints auth-gated even when `[dashboard].public = true`. Process IDs, memory usage, DB paths, deployment mode, and background task names are operational details.

Support Linux first. macOS can return partial information where convenient, but the acceptance target should be Linux SBC deployments.

## Current baseline

The application creates a FastAPI app, initializes a primary SQLite connection, optionally opens a read-only stats connection, wires `StatsService`, and registers background tasks through `TaskSupervisor`. The app has background jobs for catalog refresh, retention cleanup, checkpointing, usage window refresh, and stale request finalization. Granian is used as the server runtime, with configuration exposing server `threads`.

The README already documents `eggpool serve --daemon`, systemd deployment, cron fallback, and the intent to keep deployments lightweight. The runtime metrics pass should make these properties observable rather than assumed.

## Phase 1: runtime snapshot service

### Add module

Create `src/eggpool/runtime_metrics.py` or `src/eggpool/ops/runtime.py` with a `RuntimeMetricsService` class. Keep it independent of StatsService so request analytics and process diagnostics do not become tightly coupled.

Suggested constructor dependencies:

```python
class RuntimeMetricsService:
    def __init__(
        self,
        *,
        config: AppConfig,
        db: Database,
        stats_db: Database | None,
        supervisor: TaskSupervisor | None,
        router: Router | None,
        health_manager: HealthManager | None,
        started_monotonic: float,
        started_epoch: float,
    ) -> None: ...
```

Store `app.state.started_monotonic` and `app.state.started_epoch` during lifespan startup.

### Snapshot method

Expose:

```python
async def snapshot(self) -> dict[str, Any]: ...
```

The snapshot should gather best-effort data and never raise to the endpoint. If a probe fails, return `null` for that field and include a small `probe_errors` list with bounded strings.

## Phase 2: process and worker metrics

### Fields

Return:

- `pid`.
- `ppid`.
- `process_group_id` if available.
- `session_id` if available.
- `executable` basename only.
- `cmdline` redacted/truncated, or omit by default if there is concern about config paths.
- `uptime_seconds`.
- `rss_bytes`.
- `vms_bytes` if available.
- `open_fd_count` on Linux from `/proc/self/fd`.
- `thread_count` from `/proc/self/status` or `threading.active_count()` fallback.
- `configured_server_threads` from `config.server.threads`.
- `python_version`.
- `platform`.
- `is_daemonized_hint`: true if stdin is closed/not a tty or PPID/session suggests daemon mode; this is a hint only.

### Unexpected process count detection

Operators have observed multiple EggPool processes under Granian. Add a best-effort Linux process scan to count sibling/descendant processes that appear to be EggPool.

Use only standard library:

- Read `/proc/<pid>/cmdline` for numeric PIDs.
- Count entries containing `eggpool` or the known app module invocation.
- Distinguish current process, direct children, and same-session siblings where possible using `/proc/<pid>/stat` for PPID/session.

Return:

- `eggpool_process_count`.
- `eggpool_child_process_count`.
- `eggpool_same_session_process_count`.
- `expected_worker_process_count`.
- `process_count_warning`: true if observed count exceeds expected by more than one.

Expected count must be conservative. Depending on Granian supervisor/worker behavior, one supervisor plus one worker may be expected even when application workers are one. The plan should verify the current `serve` command implementation before hard-coding this. If the app intends to run a single Granian worker plus a supervisor process, set expected accordingly and label it explicitly.

Do not kill processes or attempt remediation from the metrics endpoint.

## Phase 3: background task health

### TaskSupervisor inspection

Inspect `src/eggpool/background.py` or equivalent. If `TaskSupervisor` does not expose task state, add a read-only `snapshot()` method.

Suggested fields per task:

- `name`.
- `registered`.
- `running`.
- `done`.
- `cancelled`.
- `last_started_at` if tracked.
- `last_completed_at` if tracked.
- `last_error_at` if tracked.
- `last_error_class`.
- `restart_count` or `failure_count` if the supervisor restarts loops.

If current supervisor only starts long-running loops and does not track iterations, implement minimal task status first: registered/running/done/cancelled/exception class. Add iteration counters later inside the specific loops.

### Instrument periodic tasks

For each app-level background task, add low-cost heartbeat fields:

- `catalog_refresh`: last attempted, last success, last failure, last duration, last model count.
- `retention_cleanup`: last run, last duration, cleaned request/event/ping counts if available.
- `checkpoint`: last run, last duration, success/failure.
- `usage_window_refresh`: last success/failure, last duration.
- `stale_request_finalizer`: last run, cleaned count, last failure.

This can be implemented with a small shared `BackgroundTaskMonitor` object stored on `app.state` and passed to loops. Avoid writing heartbeat rows to SQLite every minute; in-memory is enough for live runtime health. Persist only significant operational events as described in `metrics-core-api-plan.md`.

## Phase 4: database operational health

### Fields

Add runtime snapshot fields for SQLite:

- configured database path.
- `is_memory_db`.
- `wal_enabled` from config and optionally `PRAGMA journal_mode`.
- `synchronous` from config and optionally `PRAGMA synchronous`.
- `busy_timeout_ms` from config.
- primary connection connected flag if available.
- stats connection separate/read-only flag.
- database file size bytes.
- WAL file size bytes.
- SHM file size bytes.
- last checkpoint time if the app tracks it.
- last checkpoint error if any.

### Optional contention signal

If feasible without intrusive changes, add a lightweight counter in `Database` for:

- total write operations.
- total read operations.
- total transaction count.
- last operation error class.
- cumulative time spent waiting on the connection lock.
- max observed lock wait ms.

This should be a later sub-step because it touches the DB abstraction. If implemented, use monotonic timing around the async lock acquisition inside `Database` methods and store counters in memory only.

Expose these fields under a `db` object in `/api/stats/runtime`.

## Phase 5: in-flight and quota runtime health

### Fields

The runtime endpoint should include a compact `routing_runtime` object:

- current in-flight requests total.
- in-flight requests by account if router exposes it.
- active reservations count from DB or existing StatsService pending-health function.
- active reserved microdollars.
- current health states by account: healthy/cooldown/disabled/quota/rate-limited/auth-failed if available.
- number of active backoff rows.
- oldest pending request age seconds.

If this duplicates `/api/stats/pending-health`, the runtime endpoint can embed a reduced subset and link callers to the full endpoint. Avoid multiple expensive DB scans in one dashboard refresh; dashboard code can fetch the dedicated pending endpoint separately.

## Phase 6: runtime API endpoint

Add `src/eggpool/api/runtime.py` or extend `src/eggpool/api/stats.py` with:

- `GET /api/stats/runtime`
- optional `GET /api/stats/runtime/processes` if process table detail is too bulky for the main snapshot

Prefer one endpoint first.

### Auth behavior

Register runtime routes separately from dashboard-public stats. Even if `register_stats_routes(... require_auth=False)` is used for a public dashboard, runtime routes should always use `Depends(require_auth)` unless a future explicit config option is added.

### Response shape

Example:

```json
{
  "server": {
    "pid": 1234,
    "ppid": 1,
    "uptime_seconds": 3821,
    "python_version": "3.12.4",
    "platform": "Linux-...",
    "configured_server_threads": 1
  },
  "processes": {
    "eggpool_process_count": 2,
    "eggpool_child_process_count": 1,
    "expected_worker_process_count": 2,
    "process_count_warning": false
  },
  "memory": {
    "rss_bytes": 50331648,
    "vms_bytes": 180000000,
    "open_fd_count": 42,
    "thread_count": 4
  },
  "background_tasks": [...],
  "db": {...},
  "routing_runtime": {...},
  "probe_errors": []
}
```

## Phase 7: CLI diagnostics

Add a lightweight explicit command:

```bash
eggpool runtime-status
```

This should call the local server endpoint when the server is running, using the configured API key, and print a compact terminal summary:

- server PID/uptime.
- observed EggPool process count vs expected.
- RSS memory.
- open FD count.
- background task failures.
- pending requests / active reservations.
- DB WAL size.

Do not import the whole server stack for `croncheck`. Keep this command explicit and operator-facing.

If the server is not running, the command should exit non-zero with a clear message. It should not start the server.

## Phase 8: Dashboard runtime page

Dashboard wiring details live in `plans/metrics-dashboard-plan.md`, but the backend should provide fields needed for:

- process count card.
- RSS memory card.
- uptime card.
- background task status table.
- DB file/WAL size card.
- worker/thread count card.
- warning when observed process count exceeds expected.

Use coarse polling. Runtime data does not need sub-second refresh.

## Phase 9: tests

### Unit tests

Add tests for `RuntimeMetricsService.snapshot()` with mocked/minimal dependencies:

- snapshot does not raise when `/proc` is unavailable.
- memory fields are null-safe.
- process count warning is true when observed exceeds expected.
- DB file size handles missing files and `:memory:`.
- background task snapshot handles running, cancelled, done, and exception states.
- probe errors are bounded and do not include secrets.

### API tests

- `/api/stats/runtime` requires auth even when dashboard public mode is enabled.
- authenticated call returns stable top-level keys.
- endpoint returns 200 with partial data if a probe fails.

### CLI tests

- `eggpool runtime-status` prints useful summary when mocked endpoint returns data.
- command exits non-zero when server is unreachable.
- `croncheck` remains lightweight and does not import runtime/server modules. Add an import regression test if the CLI split has existing tests.

## Phase 10: documentation

Update docs after implementation:

- README Dashboard and Stats section: mention runtime and reliability pages.
- `docs/deployment.md`: mention `eggpool runtime-status` for diagnosing daemon/systemd/cron deployments.
- Add a short troubleshooting section for unexpected process count, high RSS, stale pending requests, and WAL growth.

Document that runtime metrics are best-effort and Linux-oriented.

## Implementation order

1. Inspect current `TaskSupervisor`, `serve`/Granian startup, and `Database` abstractions.
2. Add `RuntimeMetricsService` with process/memory/platform snapshot only.
3. Add `/api/stats/runtime` with strict auth.
4. Add process count scan and expected count logic.
5. Add background task snapshot support.
6. Add DB file/WAL/config snapshot.
7. Add reduced in-flight/pending/routing runtime fields.
8. Add `eggpool runtime-status` CLI command without affecting croncheck import cost.
9. Wire dashboard runtime page.
10. Add docs/tests.

## Acceptance criteria

The runtime metrics pass is complete when:

- `/api/stats/runtime` returns a best-effort process/memory/background-task/DB/routing runtime snapshot.
- The endpoint is auth-gated even with a public dashboard.
- Unexpected EggPool process multiplicity is visible with an explicit warning field.
- RSS memory, FD count, thread count, uptime, and configured server threads are visible where supported.
- Background task state/failures are visible without requiring log inspection.
- DB path/WAL/file size and stats-connection mode are visible.
- `eggpool runtime-status` gives a compact operator-facing summary and does not affect croncheck performance.
- All probes fail soft and never expose API keys, request/response bodies, raw headers, or unredacted secrets.
