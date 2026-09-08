# O003 Closure — Process Lifecycle Control and Watchdog Commands

Status: closed

Implementation commit: [`35f089f2cc8a30e44fafada1816b734e279cf28d`](https://github.com/eggstack/eggpool/commit/35f089f2cc8a30e44fafada1816b734e279cf28d)

Plan: [O003 — process lifecycle control and watchdog commands](../../implementation/operations/003-process-lifecycle-control-and-watchdog-commands.md)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| Foreground `serve` parity | Rust dispatch validates root/config/credentials, warns on zero accounts, checks duplicate PID/health/control state before bind, owns the PID file at the foreground process boundary, and reuses M8 signal/drain/forced-close handling | Pass |
| Detached daemon mode | Parent validates and spawns the same executable with an absolute config and `serve --verbose`; Unix session detachment uses `CommandExt::process_group(0)`, streams are explicit, paths are passed without a shell, and only the child writes the PID file | Pass |
| Start/duplicate safety | PID, health, and control observations are kept separate; stale PID state is cleared conservatively; startup rejects a live PID, healthy listener, or owned control socket before resource/database startup | Pass |
| `stop` and `restart` | TERM is gated by PID-file plus independent health/control identity evidence; waits are bounded; timeout aborts replacement; restart validates configuration before stopping and never signals an unproven PID | Pass |
| `rehash` | CLI performs local config/credential validation and digest calculation, calls the O002 control client, projects the frozen structured response, preserves restart-required/busy/preparation/digest/control-unavailable exit categories, and never performs a hidden restart | Pass |
| `runtime-status` | Local authenticated `/api/stats/runtime` endpoint and bounded direct HTTP client preserve the runtime schema projection, normalize wildcard bind addresses, cap body size at 1 MiB, and use 5-second connect/read deadlines | Pass |
| `croncheck` | Fast path reads only the PID file and kernel liveness probe, emits no normal output, and exits 0/1 | Pass |
| `ensure-running` | State preparation, create-new private guard, second liveness/health check, one detached spawn, and a bounded 2-second confirmation prevent duplicate watchdog starts and retry loops | Pass |
| M8 authority and drain | Server lifecycle continues through `ServerRuntime`, `RuntimeManager`, `ProcessRuntime`, the existing task supervisor, reload service, startup reconciliation, and retained finalization shutdown paths; no second runtime manager or scheduler was added | Pass |
| Secret and filesystem safety | API keys are only sent in the local status request; they are not rendered; PID/log/guard/socket files use private ownership/modes, atomic PID publication, identity-checked cleanup, and bounded response parsing | Pass |

## Command parity table

| Command | Rust behavior | Exit/result evidence |
|---|---|---|
| `serve --verbose` | Foreground server with root gate, config/credential validation, warning-only zero-account handling, duplicate checks, PID ownership, and M8 shutdown | `test_f005_server`, `test_f006_safety`, Rust all-target tests |
| `serve` | One-shot detached child running `serve --verbose`; default or explicit log target, quiet null streams, absolute config path, no shell | `runtime.rs::spawn_detached`, migration mode-preflight coverage |
| `stop` | Not-running/stale success; identity-gated TERM; bounded wait; PID cleanup only when still owned | `runtime.rs::stop`, O002 process primitives, M8 shutdown regressions |
| `restart` | Validate first, stop and confirm exit, reject unsafe identity/retained listener, spawn replacement only after exit | `runtime.rs::restart`, M8 startup/drain regressions |
| `rehash [--json]` | Thin O002 client adapter with digest and frozen response projection | O002 control suite, R007/R010/R012/R013 regressions |
| `runtime-status [--json]` | Authenticated local status endpoint/client with bounded HTTP and deterministic human output | `operations_o003::runtime_status_is_always_authenticated_and_bounded_projection` |
| `croncheck` | PID-only liveness check, no database/provider/runtime construction | `runtime.rs::croncheck` |
| `ensure-running` | Guarded one-shot detached start with post-spawn confirmation | `operations_o003::watchdog_guard_is_create_new_and_recovers_dead_owner` |

## Process-race matrix

| Race/state | Action | Outcome |
|---|---|---|
| Missing PID | `serve`, `ensure-running`, `stop` | Start may proceed; watchdog starts once; stop reports not running |
| Stale PID | Start/stop/restart/watchdog | Remove only stale/malformed local state, then continue or report stopped |
| Live PID without independent EggPool proof | Stop/restart | Refuse to signal or replace |
| Healthy listener without PID | Start/restart/watchdog | Refuse duplicate start/replacement |
| Live control socket | Startup | Refuse duplicate ownership before bind/resource initialization |
| Two watchdog callers | `create_new` guard | One caller enters the spawn section; the other returns a lock-held operational failure; owner PID recovery is bounded to a dead owner |
| PID cleanup after replacement | Server exit/stop | `clear_pid_if_matches` prevents an older process from deleting a replacement PID file |
| Long isolated temporary path | Runtime socket resolution | Use the private state runtime when representable; otherwise use the bounded private UID runtime path to stay within Unix socket limits |

## Drain/restart and retained-work evidence

The command layer does not implement a second shutdown path. `stop` and `restart`
signal the M8-owned foreground process, whose `ServerRuntime` transitions through
quiescing, draining, closing, and forced close on deadline. The required lifecycle
regression targets remain green:

```text
runtime_lifecycle_r007   PASS (6)
runtime_lifecycle_r009   PASS (5)
runtime_lifecycle_r010   PASS (4)
runtime_lifecycle_r012   PASS (10)
runtime_lifecycle_r013   PASS (7)
```

The black-box migration suite also passed graceful shutdown, listener reuse,
bind rejection without a second listener, and post-bind database failure cleanup.
No command replays an unknown in-flight upstream request; startup reconciliation
and retained finalization remain M8/C010 authority.

## Rehash and runtime-status evidence

The O002 control suite and R007/R010/R012/R013 regressions cover applied, no-op,
busy, malformed, digest, cancellation, authority, diagnostics, and shutdown
control outcomes. O003 projects every frozen JSON field (`ok`, `stage`,
`exit_code`, `generation`, `changed_sections`, `warnings`, `restart_required`,
`retirement_pending`, and `message`) and preserves human success/failure routing.

The runtime endpoint is always authentication-gated, and the focused O003 test
verified unauthenticated `401`, authenticated `200`, configured-thread and DB
fields, and JSON projection. The command client rejects malformed status lines,
non-200/auth responses, malformed JSON, timeouts, and bodies above 1 MiB without
printing the bearer key.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings   PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1 PASS (2)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1 PASS (6)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1 PASS (5)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1 PASS (10)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --all-targets                          PASS
rtk uv run pytest tests/unit/test_control_server.py tests/unit/test_runtime_paths.py tests/unit/test_runtime_daemon.py tests/unit/test_lifecycle_cli.py tests/unit/test_fastcli.py tests/unit/test_cli_rehash_format.py tests/unit/test_cli_rehash_helper.py tests/unit/test_cli_rehash_preflight.py tests/unit/test_cli_runtime_status.py tests/unit/test_api_runtime.py tests/migration_rs -q --tb=short --maxfail=1 PASS (336 passed, 3 skipped, 1 warning)
rtk uv run ruff format --check src/ tests/ scripts/                                      PASS
rtk uv run ruff check src/ tests/ scripts/                                              PASS
rtk uv run pyright src/ scripts/                                                        PASS (0 errors, 0 warnings)
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1                                     PASS (14)
rtk git diff --check                                                                     PASS
```

## Failing-before/passing-after evidence

Before O003, the Rust dispatcher returned migration-stage `NotImplemented` for
all lifecycle/control/watchdog commands, and the migration safety fixture
explicitly expected deferred daemon-mode markers. After implementation, the
commands dispatch to Rust handlers, the fixture was updated to assert validation
before side effects, and the full migration oracle passes. During qualification,
the oracle exposed two integration defects—shared Python-created state
permissions and overlong temporary Unix socket paths—which were corrected before
the final passing run.

## Dependency, schema, and security review

No new dependency, database schema, migration, or public control port was added.
The implementation uses the existing O002 `nix` dependency, Tokio networking,
standard-library process/filesystem primitives, and the existing M8 lifecycle
services. Daemon detachment uses the safe Unix `CommandExt` API; no unsafe code,
shell execution, RPC framework, or Reqwest dependency was introduced.

Owner-owned state directories are normalized to mode `0700` before lifecycle
use, files are created at mode `0600`, PID publication is atomic, and stale
cleanup checks identity/content before removal. Status reads are bounded and
authenticated; API keys never enter human/JSON output. Config validation occurs
before daemon spawn or restart destruction, and startup failures close prepared
M8 resources and the control listener.

## Unresolved findings and non-goals

None for O003 acceptance. Windows services, systemd installation, provider
onboarding, backup/update/deploy, broad OS/SBC qualification, Rust-default public
cutover, and Python retirement remain owned by later M9/M10/M11/M12 work. The
runtime-status projection reports unavailable OS memory/file-descriptor metrics
as `null` rather than inventing values.

## Planning transition

O003 is removed from the dependency-ready section and recorded as closed in the
implementation index, registry, roadmap, and handoff sequence. O004 is the only
plan promoted to dependency-ready because its hard dependency is now accepted;
O005-O010 remain queued behind their direct predecessors. M9 remains active, and
M10 remains blocked on accepted O010 closure and its own planning review.
