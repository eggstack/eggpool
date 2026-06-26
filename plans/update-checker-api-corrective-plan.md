# Corrective Plan: Update Checker API and Dashboard Wiring

## Context

EggPool already has a server-lifespan-managed background update checker. `eggpool serve` builds the FastAPI app through `create_app()`, the app lifespan creates a `TaskSupervisor`, instantiates `UpdateChecker`, stores it on `app.state.update_checker`, registers the checker as the `update_checker` supervised task, and starts all registered background tasks. `UpdateChecker.run_periodic()` performs one immediate PyPI check at startup and then repeats every 24 hours by default.

The current implementation is therefore mostly correct for the intended operational behavior: the server checks for new package releases once per day, does not auto-install anything, and exposes the latest snapshot to server-rendered dashboard pages through `app.state.update_checker.snapshot()`.

The defect is API/documentation drift and incomplete test coverage. `src/eggpool/api/stats.py` advertises `GET /api/stats/update` in its module docstring, but no `handle_update` endpoint is registered in `register_stats_routes()`. The dashboard footer uses the server-rendered snapshot path, not that JSON endpoint, so the user-visible footer can still work. However, the API surface is inconsistent, and this makes the runtime state harder to validate from scripts, tests, or dashboard client-side code.

## Goal

Make the update-checker feature explicitly and testably wired across three surfaces:

1. Background task lifecycle: exactly one `update_checker` supervised task is registered and started during normal server startup.
2. Server-rendered dashboard: pages render the update indicator only when the checker snapshot reports an update.
3. JSON stats API: `GET /api/stats/update` returns the current update-check snapshot, with the same authentication behavior as the other dashboard stats routes.

Do not add automatic update installation. The feature must remain a passive notification/checking mechanism.

## Non-goals

Do not change the default 24-hour check interval unless a test injects a shorter interval.

Do not persist update-check state to SQLite. The snapshot is process-local runtime state; after restart the initial check repopulates it.

Do not add a second scheduler or cron integration. The existing `TaskSupervisor` is the right owner because the check belongs to the running server process and dashboard state.

Do not make PyPI lookup mandatory for server readiness. PyPI failures must not prevent startup.

## Implementation Steps

### 1. Add the missing stats API endpoint

In `src/eggpool/api/stats.py`, add a handler similar to:

```python
async def handle_update_status(request: Request) -> Response:
    """GET /api/stats/update."""
    checker = getattr(request.app.state, "update_checker", None)
    if checker is None:
        return JSONResponse(
            content={
                "current_version": "",
                "latest_version": "",
                "update_available": False,
                "install_method": "unknown",
                "update_command": "eggpool update",
                "last_check_at": 0.0,
                "last_check_error": "checker not initialized",
            }
        )
    return JSONResponse(content=checker.snapshot().to_dict())
```

Register it in `register_stats_routes()`:

```python
app.add_api_route(
    path="/api/stats/update",
    endpoint=handle_update_status,
    methods=["GET"],
    dependencies=dependencies,
)
```

Add `handle_update_status` to `__all__`.

The endpoint should use the same `dependencies` variable as the rest of `/api/stats/*`, meaning it is public only when the dashboard stats API is public, and auth-gated when dashboard auth is enabled. Do not use the stricter per-request trace auth policy; update status is not sensitive in the same way request traces are.

### 2. Keep the existing background task registration, but make it easier to test

The current app lifespan code is acceptable:

```python
from eggpool.update_checker import UpdateChecker

update_checker = UpdateChecker()
app.state.update_checker = update_checker
supervisor.register(
    "update_checker",
    update_checker.run_periodic,
)
```

Leave this in the app lifespan. If tests need a seam, prefer adding a small helper function rather than moving the feature out of lifespan ownership. For example:

```python
def _register_update_checker(app: FastAPI, supervisor: TaskSupervisor) -> UpdateChecker:
    update_checker = UpdateChecker()
    app.state.update_checker = update_checker
    supervisor.register("update_checker", update_checker.run_periodic)
    return update_checker
```

Then call this helper from `_lifespan_runtime()`. This is optional, but recommended if the current app-lifespan tests are cumbersome.

### 3. Correct API documentation and comments

`src/eggpool/api/stats.py` already lists `GET /api/stats/update`; after adding the endpoint, that docstring becomes accurate. Also check any README/dashboard docs that mention update checking. The wording should be:

- The server checks PyPI at startup and then approximately every 24 hours.
- The dashboard shows nothing when no update is available or when the check has failed.
- When a newer version is found, the dashboard footer shows the current version, latest version, and `eggpool update` command.
- The API endpoint exposes the current in-memory snapshot and does not trigger an installation.

Avoid language implying unattended upgrades.

### 4. Add focused unit tests for `UpdateChecker`

Create or extend tests around `src/eggpool/update_checker.py`.

Required cases:

- `check_once()` sets `current_version`, `latest_version`, `install_method`, `update_command`, and `update_available=True` when injected current version is lower than injected PyPI version.
- `check_once()` sets `update_available=False` when versions match.
- PyPI/network failure is swallowed and reflected in `last_check_error`, without raising.
- On failure after a prior success, the previous `latest_version` is retained and `update_available` is recomputed against that retained value.
- `snapshot()` returns an isolated copy, not a direct mutable reference.

Use injected `_http_get`, `_version_lookup`, and `_install_method_lookup` seams already present on `UpdateChecker`. Do not perform live PyPI requests in tests.

### 5. Add API route tests

Add tests for `GET /api/stats/update` using a FastAPI test client or the repository's existing async client pattern.

Required cases:

- When `app.state.update_checker` exists and its snapshot reports an update, the endpoint returns a JSON object containing at least:
  - `current_version`
  - `latest_version`
  - `update_available`
  - `install_method`
  - `update_command`
  - `last_check_at`
  - `last_check_error`
- When no checker is attached, the endpoint returns a safe fallback JSON object with `update_available=False` and a diagnostic `last_check_error`.
- Auth behavior matches the other stats endpoints. If `register_stats_routes(app, require_auth=True)` is used, unauthenticated requests should be rejected. If `require_auth=False`, the endpoint should be reachable.

A lightweight route-only app is preferable here. Do not boot the full EggPool server just to test route serialization.

### 6. Add lifespan/task-registration tests

Add or extend a lifespan test to ensure the server registers the update checker task.

Required assertion:

- After app lifespan startup, `app.state.supervisor.get_task("update_checker")` is not `None`.
- `app.state.update_checker` exists.

Avoid long sleeps. If the test starts the real supervised task, monkeypatch `UpdateChecker.run_periodic` or the checker factory so it blocks on a cancellable event rather than sleeping for 24 hours. The test only needs to prove registration and startup, not real PyPI behavior.

### 7. Add dashboard render tests

Add small pure-render tests for `_render_update_indicator()` or whichever public/private render helper is already tested.

Required cases:

- `update_info=None` renders an empty string.
- `update_available=False` renders an empty string.
- `update_available=True` with current/latest versions renders the indicator, the escaped versions, and the escaped command.

This protects the current quiet-footer contract: failed or unavailable checks should not add noisy dashboard output.

## Acceptance Criteria

The implementation is complete when:

- `GET /api/stats/update` exists and returns the current update-check snapshot.
- The endpoint follows the same auth gating as the other normal stats endpoints.
- `UpdateChecker` remains passive and never installs updates.
- The background task is still registered under the exact name `update_checker` during server lifespan startup.
- PyPI failures are non-fatal and visible only through `last_check_error` / logs, not startup failure.
- Dashboard pages continue to show no update indicator unless `update_available=True`.
- Tests cover the checker logic, endpoint serialization/auth behavior, lifespan registration, and render quiet/noisy states.

## Suggested Verification Commands

Run the targeted tests first:

```bash
pytest tests/test_update_checker.py tests/test_stats_update_endpoint.py tests/test_dashboard_update_indicator.py
```

Then run the broader server/dashboard set used in this repo:

```bash
pytest tests
```

If the project has type/lint gates configured, also run the standard local check command documented in the repository. At minimum, run:

```bash
python -m compileall src tests
```

## Operational Notes

This feature performs at most one PyPI request at startup and one request per 24 hours afterward. That is acceptable for Raspberry Pi / SBC deployments and far cheaper than cron-driven CLI checks because it reuses the already-running server process.

The update checker should remain in-memory. Persisting update metadata would add SQLite writes for a low-value cache and is not needed for dashboard correctness.

The only write-like effect of the feature should be logs. No database writes, config writes, package-manager invocations, or automatic restarts should be introduced.
