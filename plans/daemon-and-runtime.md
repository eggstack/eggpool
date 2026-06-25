# Daemon and Runtime Plan

## Purpose

Add a clean direct-run daemon mode and make all background server starts quiet, detached, and PID/log aware. This supports the private home/SBC deployment target where users often want to start EggPool without keeping a terminal attached, and it gives the cron watchdog a reliable primitive for starting the server only when needed.

This plan should be implemented after or alongside `plans/lightweight-cli-watchdog.md`. The watchdog needs a cheap `ensure-running` command; this plan provides the runtime behavior that `ensure-running` should call.

## Current Problem

`eggpool serve` runs Granian in the foreground. That is the correct default for debugging and manual operator sessions, but it ties up the shell and streams logs to the terminal.

The existing runtime helper already has a background spawn function, but it is incomplete for daemon use:

```python
argv = [sys.executable, "-m", "eggpool", "--config", resolved, "serve"]
subprocess.Popen(argv, cwd=cwd or os.getcwd(), start_new_session=True)
```

Issues:

```text
stdout/stderr are not redirected
stdin is not detached from the caller
log destination is not explicit
PID path can collide across root/user/systemd/cron contexts
spawn success is not verified
background starts still invoke the full CLI path unless fast dispatch is added
runtime behavior is not exposed directly as `eggpool serve --daemon`
```

## Target User Behavior

Foreground debug mode remains unchanged:

```bash
eggpool serve
```

Daemon mode starts the server in the background and returns the shell promptly:

```bash
eggpool serve --daemon
```

Optional explicit log path:

```bash
eggpool serve --daemon --log-file ~/.local/state/eggpool/eggpool.log
```

The watchdog should be able to start the server through the same runtime path:

```bash
eggpool ensure-running
```

Stopping and restarting should work against daemon-launched processes:

```bash
eggpool stop
eggpool restart
```

## Runtime Path Decisions

Use predictable per-user state paths for personal/private deployments.

Recommended PID path order:

```text
$EGGPOOL_PID_FILE, if set
$XDG_RUNTIME_DIR/eggpool.pid, if XDG_RUNTIME_DIR is set
~/.local/state/eggpool/eggpool.pid, otherwise
```

Recommended log path order for daemon mode:

```text
--log-file PATH, if passed
$EGGPOOL_LOG_FILE, if set
~/.local/state/eggpool/eggpool.log, otherwise
/dev/null, only if explicitly requested with --quiet or --no-log
```

Avoid a shared `/tmp/eggpool.pid`. If `/tmp` must be used, scope by UID:

```text
/tmp/eggpool-$UID.pid
```

The same PID resolver must be used by:

```text
serve
serve --daemon
croncheck
ensure-running
stop
restart
systemd personal unit
cron watchdog
```

## Implementation Plan

### 1. Add runtime path helpers

Create or refactor a lightweight module, for example `eggpool.runtime_paths`, with no heavy imports.

Functions:

```python
def state_dir() -> Path: ...
def default_pid_file() -> Path: ...
def default_log_file() -> Path: ...
def ensure_state_dir() -> Path: ...
```

This module may be imported by the fast CLI. Keep it stdlib-only.

Update `eggpool.constants.PID_FILE` or deprecate it in favor of the runtime path resolver. If keeping the constant for compatibility, ensure it follows the same resolver.

### 2. Extend `runtime.start_server()`

Change the helper to accept daemon/log behavior explicitly.

Suggested signature:

```python
def start_server(
    config_path: str,
    *,
    cwd: str | None = None,
    daemon: bool = True,
    log_path: str | None = None,
    quiet: bool = True,
    verify: bool = False,
    verify_timeout_s: float = 3.0,
) -> subprocess.Popen[bytes]:
    ...
```

Behavior for daemon/quiet mode:

```text
resolve absolute config path
ensure state/log directory exists
open stdin from /dev/null
open stdout/stderr to log file or /dev/null
set cwd to configured data/state dir or provided cwd
start_new_session=True
spawn child process running foreground serve
optionally wait briefly for process liveness or PID file creation
return Popen handle or raise RuntimeError on spawn failure
```

Do not pass `--daemon` to the child. The child must run the normal foreground server code.

Child command should be one of:

```bash
python -m eggpool --config /absolute/config.toml serve
```

or, if the fast bootstrap makes this cheaper and safer:

```bash
/path/to/eggpool --config /absolute/config.toml serve
```

Prefer `sys.executable -m eggpool` for venv correctness unless it causes import-loop problems.

### 3. Add `serve --daemon`

Extend the `serve` command:

```bash
eggpool serve --daemon
eggpool serve --daemon --log-file PATH
eggpool serve --daemon --quiet
```

Foreground `eggpool serve` should remain the debugging/operator path and should still print Granian logs to the terminal.

Daemon parent behavior:

```text
validate/resolve config path enough to spawn safely
refuse to daemonize if an existing live PID is found, unless --replace is added
spawn child through runtime.start_server(... daemon=True ...)
print a short success message with PID/log path
exit 0
```

Optional `--replace` can stop a stale/live server before starting, but this is not required for the first pass. `restart` already covers that use case.

### 4. Ensure PID file writing is early and reliable

Confirm where the server writes its PID file today. If it is not written early in the server lifecycle, add explicit PID file write at the beginning of foreground serve after configuration is accepted but before Granian blocks.

On clean shutdown, clear the PID file. On stale PID detection by `ensure-running`, clear it before spawning.

Handle multiple process cases carefully. EggPool should run Granian with one worker for small SBC/private deployment. The PID file should represent the long-lived parent/server process that `stop` should terminate.

### 5. Redirect daemon output correctly

Daemon mode should not attach to the caller's terminal.

Use:

```text
stdin:  /dev/null
stdout: log file or /dev/null
stderr: same log file or separate err log
```

Default should probably be a log file under state dir, not `/dev/null`, because users need diagnostics when a background start fails.

Suggested default:

```text
~/.local/state/eggpool/eggpool.log
```

Systemd deployments can rely on journal logs instead and do not need `serve --daemon` inside the service unit. Systemd should run foreground `serve` and manage the process itself.

### 6. Update `ensure-running` to call runtime daemon start

Once runtime daemon start exists, `ensure-running` should use it rather than open-coding subprocess behavior.

Common case:

```text
live PID -> exit 0, no config load, no spawn
```

Repair case:

```text
missing/stale PID -> spawn daemon using absolute config path and log path
```

This keeps the recurring cron check cheap while still using the robust runtime code when a start is actually needed.

### 7. Update `stop` and `restart`

`stop` should use the same PID resolver as daemon mode.

`restart` should stop a live daemon/server and start a new daemon by default, unless the command is explicitly intended to be foreground. Current restart behavior already spawns in the background; make that behavior explicit and use the new `start_server()` options.

Potential command behavior:

```bash
eggpool restart                 # restart in daemon/background mode
eggpool restart --foreground    # optional, if useful later
```

### 8. Avoid root/user path confusion

Daemon mode itself should be user-contextual. If a user runs `sudo eggpool serve --daemon`, the service will run as root and use root paths unless explicitly guarded.

For private deployment, print a warning or refuse unless `--as-root` is passed:

```text
Refusing to daemonize as root for personal deployment.
Run as your normal user, or pass --as-root if this is intentional.
```

Systemd production mode is handled separately in the install/deploy simplification plan.

## Test Plan

### Unit tests

```text
runtime path resolver respects EGGPOOL_PID_FILE
runtime path resolver uses XDG_RUNTIME_DIR when set
runtime path resolver falls back to ~/.local/state/eggpool
log resolver respects --log-file / EGGPOOL_LOG_FILE
start_server opens stdin/stdout/stderr correctly in daemon mode
start_server does not pass --daemon to child
start_server uses absolute config path
stop uses the same PID file as serve --daemon
restart uses the same PID/log resolver
```

### Behavior tests

```text
eggpool serve still runs foreground by default
eggpool serve --daemon returns promptly
eggpool serve --daemon writes or reports a PID
eggpool serve --daemon does not stream logs into the terminal
eggpool stop terminates daemon-started server
eggpool restart restarts daemon-started server
eggpool ensure-running no-ops when daemon is alive
eggpool ensure-running starts daemon when PID is absent
eggpool ensure-running clears stale PID before start
```

### Raspberry Pi validation

On the target Pi or similar SBC:

```bash
time eggpool croncheck
time eggpool ensure-running
eggpool serve --daemon
sleep 2
eggpool croncheck
eggpool stop
```

Expected result: `croncheck` and already-running `ensure-running` should complete quickly without multi-second core spikes. `serve --daemon` should return promptly and leave logs in the documented location.

## Acceptance Criteria

`eggpool serve` remains foreground and useful for debugging.

`eggpool serve --daemon` starts a detached background server and returns promptly.

Daemon mode redirects stdin/stdout/stderr and does not tie up the shell.

Daemon mode writes logs to a predictable location by default.

Daemon mode and watchdog commands share the same PID path.

`eggpool stop` and `eggpool restart` work against daemon-started servers.

`ensure-running` uses daemon runtime only when the server is not already running.

Systemd units do not use `serve --daemon`; systemd should run foreground `serve` and manage the process itself.

## Non-Goals

Do not redesign installer behavior in this phase.

Do not automate systemd or cron deployment in this phase, except to expose runtime primitives they can use.

Do not implement production hardening here.

Do not change Granian worker count beyond preserving the intended single-worker behavior.
