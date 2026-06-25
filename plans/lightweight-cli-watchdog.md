# Lightweight CLI Watchdog Plan

## Purpose

Optimize recurring status and watchdog commands so EggPool is friendly to low-power home hardware, especially Raspberry Pi and similar SBC deployments. The current `eggpool croncheck` behavior is logically simple, but invoking the CLI can still spike a core for several seconds because the main CLI module imports too much of the application stack before reaching the tiny PID check.

This plan is intentionally narrow. It should be implemented before the broader deployment cleanup because it directly addresses the observed Raspberry Pi CPU spike from cron-style checks every 5-10 minutes.

## Current Problem

`croncheck` only needs to determine whether a PID file exists and whether the recorded process is alive. The current command body already does a minimal check once reached: it reads `PID_FILE`, parses a PID, and calls the process-running helper.

However, the console entrypoint imports the main CLI module before command dispatch. The main CLI currently imports Click plus substantial EggPool modules at module import time, including account registry, auth, migrations, repositories, lifecycle helpers, config models, provider client pool, and TOML editing helpers. That is unnecessary for recurring probes.

On a Raspberry Pi, a cold Python process that imports the full CLI graph every 5 minutes can create visible CPU spikes. The recurring check path must be close to stdlib-only in the common case.

## Target Behavior

The following commands should be cheap to start and should not import the full application stack:

```bash
eggpool croncheck
eggpool --config /path/to/config.toml croncheck
eggpool ensure-running
eggpool --config /path/to/config.toml ensure-running
```

`croncheck` remains a pure probe:

```text
exit 0: server appears to be running
exit 1: server is not running or PID file is missing/stale
exit 2: optional internal check error, only if useful
```

`ensure-running` is the cron-facing repair command:

```text
if PID file exists and process is alive:
    exit 0

if PID file is missing, invalid, or stale:
    clear stale PID state if needed
    start EggPool in daemon/background mode
    optionally perform a short launch verification
    exit 0 on successful spawn
    exit nonzero on failed spawn
```

Do not overload `croncheck` with start behavior. A pure check command is useful for monitoring and scripts, while `ensure-running` clearly expresses a watchdog action.

## Implementation Plan

### 1. Add a stdlib-only fast dispatcher

Create a new module such as `src/eggpool/fastcli.py` or `src/eggpool/bootstrap.py`.

This module should use only stdlib imports in the hot path:

```text
sys
os
subprocess
pathlib
time, if a short launch verification is implemented
```

Avoid importing Click, FastAPI, Granian, Pydantic, HTTP clients, provider modules, database modules, and TOML/config parsing for `croncheck`.

Expose a function with this shape:

```python
def maybe_run_fast_command(argv: list[str]) -> int | None:
    """Run a fast-path command if argv matches one.

    Return an integer exit code when handled.
    Return None when the normal full CLI should handle the command.
    """
```

The parser only needs to recognize:

```text
--config PATH
croncheck
ensure-running
```

It does not need to be a full CLI parser. Unknown options should return `None` so the normal CLI handles them.

### 2. Make `eggpool.cli:main` a lightweight bootstrap

Keep the published entrypoint unchanged in `pyproject.toml`:

```toml
[project.scripts]
eggpool = "eggpool.cli:main"
```

Refactor `src/eggpool/cli.py` so `main()` first tries the fast dispatcher before importing the full CLI graph.

Suggested shape:

```python
def main() -> NoReturn:
    from eggpool.fastcli import maybe_run_fast_command

    code = maybe_run_fast_command(sys.argv[1:])
    if code is not None:
        raise SystemExit(code)

    from eggpool.cli_full import cli

    cli(obj={})
    raise SystemExit(0)
```

Move the current Click-heavy CLI implementation into `src/eggpool/cli_full.py`, or gradually reduce module-level imports in `cli.py` until the bootstrap is actually lightweight. The cleanest long-term shape is a tiny `cli.py` bootstrap plus a full Click implementation in `cli_full.py`.

### 3. Implement fast `croncheck`

The fast implementation should:

1. Resolve the PID file using a lightweight runtime path resolver.
2. Return 1 if the file does not exist.
3. Parse the PID as an integer.
4. Return 1 if parsing fails.
5. Probe the PID with `os.kill(pid, 0)`.
6. Return 0 if the process exists and is signalable.
7. Return 1 on `ProcessLookupError`, `PermissionError`, or `OSError`.

Avoid config loading. Avoid database loading. Avoid creating config files. Avoid importing `eggpool.constants` if that imports nontrivial modules; if constants are lightweight, it is acceptable, but the fastest path should make PID resolution explicit and cheap.

### 4. Add `ensure-running`

Implement fast `ensure-running` in the same module. It should use the same PID check as `croncheck`.

When the server is already running, it exits 0 quickly.

When the PID is stale, it should remove the stale PID file before spawning.

When the server is not running, it should start EggPool using the daemon/runtime path from `plans/daemon-and-runtime.md`. Until that plan is implemented, a minimal version can spawn:

```bash
python -m eggpool --config /absolute/config.toml serve --daemon-child
```

or:

```bash
eggpool --config /absolute/config.toml serve --daemon-child
```

The child must not re-enter `ensure-running` or recursively daemonize. Prefer centralizing this in `eggpool.runtime.start_server()` after the daemon/runtime plan is implemented.

### 5. Use per-user PID path semantics

Recurring commands must not collide between root, the invoking user, systemd, and cron contexts.

Prefer this PID path order:

```text
$EGGPOOL_PID_FILE, if set
$XDG_RUNTIME_DIR/eggpool.pid, if XDG_RUNTIME_DIR is set
~/.local/state/eggpool/eggpool.pid, otherwise
```

If keeping `/tmp`, use a UID-scoped name such as `/tmp/eggpool-$UID.pid`, not a shared `/tmp/eggpool.pid`.

This path resolver should be in a lightweight module that the fast CLI can import without dragging in the full app.

### 6. Add import-budget tests

Add tests that guard against accidental regression.

At minimum, use a subprocess to run:

```bash
python -X importtime -m eggpool croncheck
```

or invoke the installed console script in a controlled environment. The test does not need to assert exact timing, which is hardware-dependent. Instead, it should assert that the fast path does not import known heavy modules.

Forbidden imports for fast `croncheck` and already-running `ensure-running`:

```text
fastapi
granian
httpx
pydantic
aiosqlite
eggpool.app
eggpool.providers.*
eggpool.db.*
eggpool.models.config
eggpool.lifecycle.*
```

If exact import assertions are brittle, add a lightweight `EGGPOOL_FASTCLI_DEBUG_IMPORTS=1` test hook that reports whether the full CLI path was bypassed.

### 7. Add behavior tests

Cover these cases:

```text
croncheck exits 1 when PID file is missing
croncheck exits 1 when PID file is invalid
croncheck exits 1 and clears nothing when PID is stale
croncheck exits 0 when PID is alive
ensure-running exits 0 and does not spawn when PID is alive
ensure-running clears stale PID before spawning
ensure-running exits nonzero when spawn fails
ensure-running uses absolute config path when provided
ensure-running does not create config files in arbitrary cwd
```

### 8. Update cron deployment to call `ensure-running`

The cron deployment plan should generate entries that call `ensure-running`, not check-only `croncheck`.

Example generated personal crontab entries:

```cron
@reboot /home/user/.local/bin/eggpool --config /home/user/.config/eggpool/config.toml ensure-running >> /home/user/.local/state/eggpool/cron.log 2>&1
*/5 * * * * /home/user/.local/bin/eggpool --config /home/user/.config/eggpool/config.toml ensure-running >> /home/user/.local/state/eggpool/cron.log 2>&1
```

Use absolute binary, config, and log paths. Do not rely on cron's sparse PATH.

## Acceptance Criteria

`eggpool croncheck` is a fast path and does not import the full CLI graph.

`eggpool croncheck` does not create a config file, parse TOML, open SQLite, load providers, import FastAPI, or import Granian.

`eggpool ensure-running` exits almost immediately when the server is already running.

`eggpool ensure-running` starts the server only when the PID is missing or stale.

Cron watchdog entries call `ensure-running` with absolute paths.

On Raspberry Pi-class hardware, recurring checks no longer spike a core for several seconds in the already-running case.

## Non-Goals

Do not rewrite every CLI command in this phase.

Do not replace Click for the full interactive/operator CLI.

Do not redesign systemd deployment in this phase except where needed to share PID/runtime path helpers.

Do not merge backup cron and watchdog cron behavior; backup cron is handled in the install/deploy simplification plan.
