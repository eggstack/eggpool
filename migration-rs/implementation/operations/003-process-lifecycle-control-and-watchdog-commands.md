# O003 — Process Lifecycle Control and Watchdog Commands

Status: queued behind O002

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted O002.

## Objective

Replace the migration-stage command stubs for server lifecycle/control with real Rust behavior: `serve` daemon mode and foreground option semantics, `stop`, `restart`, `rehash`, `runtime-status`, `croncheck`, and `ensure-running`. Compose O002 process/control primitives with the closed M8 runtime; do not duplicate M8 lifecycle logic.

`serve --verbose` already boots the Rust server. O003 makes the complete documented lifecycle usable.

## Part A — foreground `serve` parity

Preserve current foreground behavior while closing deferred options:

- `--verbose` remains foreground and suitable for systemd;
- root refusal by default and `--as-root` explicit override follow the O001 oracle;
- `--log-file`/`--quiet` semantics are rejected/ignored exactly where Python defines them for foreground;
- validate config/account credentials before listener startup;
- warn on zero provider accounts without converting the warning into a crash;
- perform duplicate-instance checks before binding;
- write/clear PID at the same ownership boundary as the Rust foreground process, not a worker child;
- retain M8 signal/graceful/forced shutdown behavior.

Do not recreate Granian's supervisor/worker topology. The Rust process is the server process; parity is operational, not process-count identity.

## Part B — detached daemon `serve`

Implement daemon mode as a one-shot parent spawn of the same Rust executable in foreground mode, matching the Python design intent.

Requirements:

- parent validates config and duplicate-instance state before spawn;
- child receives resolved `--config` and foreground flag without a recursive daemon flag;
- use process/session detachment available safely on supported Unix targets;
- reviewed stdin/stdout/stderr handling;
- `--log-file` or default state log path, with `--quiet` null output semantics;
- no shell interpolation for executable/config/log paths;
- parent reports spawned PID/log/PID-file paths and returns promptly according to oracle;
- child owns PID-file lifecycle;
- failed exec/spawn returns non-zero without writing a false running PID;
- daemon parent cancellation cannot kill an already-successfully-spawned child unexpectedly.

If true session detachment cannot be implemented without unsafe code, use a small well-maintained safe Unix process crate only after demonstrating the standard `Command` API is insufficient. No daemon framework.

## Part C — stop/restart

`stop` must:

- handle not-running and stale-PID cases per oracle;
- prefer safe O002 identity/control evidence;
- request graceful process termination;
- wait for bounded timeout;
- report timeout/nonexistent/permission failures with compatible exit category;
- never signal a process that cannot be safely associated with EggPool.

`restart` must:

- stop first when running;
- wait for confirmed exit before new start;
- preserve resolved config/log mode;
- never start a replacement while the old listener may still own the port/socket;
- never replay unknown in-flight upstream requests; M8 drain + C010 startup reconciliation remain authoritative.

Test restart when retained finalization exists, a stream is active, the old process is slow to drain, and shutdown times out.

## Part D — rehash

Implement `rehash [--json]` as a thin CLI adapter:

1. local config validation + content digest;
2. O002 control-client `reload_config` request;
3. exact O001 structured/human response projection and exit category.

Do not restart automatically on invalid config. Preserve explicit control-unavailable/timeouts/protocol errors. Restart-required responses remain structured restart-required outcomes; no hidden restart.

O003 must reuse R013's `ReloadService` semantics and must not special-case wire policy or generation publication.

## Part E — runtime-status

Port the Python contract that reads the local authenticated `/api/stats/runtime` projection and renders human/JSON output.

Reuse the existing inbound API/auth contract. Build a bounded local HTTP client using the already-present Hyper/Rustls stack or a tiny direct loopback HTTP path; do not add Reqwest solely for this command.

Requirements:

- resolve host/port/API key using reviewed config/env semantics;
- never print API key;
- bounded connect/read/body size/timeouts;
- distinguish not-running, auth/protocol, and server error classes;
- JSON mode preserves schema; human mode remains deterministic/secret-free.

If O001 proves runtime-status may query an unencrypted local HTTP endpoint only, do not invent TLS management.

## Part F — `croncheck` and `ensure-running`

These are intentionally cheap watchdog commands.

`croncheck`:

- must avoid constructing provider pools/database/runtime just to check liveness;
- use PID/health evidence from O002;
- preserve exact exit-0/exit-1 contract and fast-path stdout behavior.

`ensure-running`:

- return success without action when healthy/running;
- clear only safely identified stale local state;
- start detached server once when stopped;
- use a local interprocess guard or equivalent bounded race prevention so two simultaneous cron invocations do not spawn two servers;
- recheck health/process state after winning the start race;
- never enter a retry/restart loop.

The guard can be a create-new lock file/OS file lock only if implemented with current dependencies; keep it local and recover stale ownership safely.

## Command dispatch cleanup

Update `rust/src/runtime.rs` so these commands no longer fall through `NotImplemented`. Keep command bodies small; shared behavior belongs in O002/operations services.

Do not delete `NotImplemented` yet: later M9 commands still need explicit migration-stage failure until their plans land.

## Tests

Add focused O003 command/process tests using temporary roots and child fixture executables/loopback Rust server:

- foreground startup/root/config/duplicate cases;
- daemon spawn/log/quiet/path-with-spaces/failed-exec;
- stale PID and live foreign process safety;
- stop graceful success/already stopped/permission/timeout;
- restart normal and slow-drain cases;
- rehash applied/noop/invalid/restart-required/Busy/control unavailable/json;
- runtime-status human/json/not-running/auth/bounded malformed body;
- croncheck exact fast path;
- concurrent ensure-running spawns exactly one server;
- SIGTERM during active finite/stream work retains M8 terminal invariants;
- no leaked child/lock/PID/socket/log handle after test convergence.

No root/systemd mutation is required here.

## Non-goals

- provider onboarding;
- backup/update/deploy;
- Windows service support;
- systemd service installation;
- alternate control protocol.

## Verification

Run fmt, Clippy, focused O002/O003, R009-R013, C008-C011 where shutdown/handoff can regress, aggregate Rust tests, targeted Python lifecycle/rehash/runtime-status/fastcli tests, migration oracle, and static Python checks if fixtures changed.

## Closure evidence

Write `migration-rs/closure/operations/003-status.md` with a command parity table, process-race matrix, drain/restart evidence, rehash/status outputs, dependency changes, and unresolved findings.

## Acceptance criteria

O003 closes only when the documented Rust server can be started foreground/detached, safely detected/stopped/restarted/reloaded/inspected by Rust commands, watchdog races cannot duplicate the process, and ordinary stale/malformed local process state cannot require a manual restart/database reset beyond the requested lifecycle action.

Accepted O003 promotes only O004.