# R009 Closure — Server Startup, Signals, Graceful Drain, and Forced Shutdown

Status: closed

Recommendation: closed; R010 promoted to dependency-ready

Implementation commits: `5f34e90` + `d04967d`

Plan: [R009 — server startup, signals, graceful drain, and forced shutdown](../../implementation/runtime-lifecycle/009-server-startup-signals-and-shutdown.md)

## Outcome

R009 replaces the loose `server::run` cleanup sequence with an explicit
`ServerRuntime` owner and `ServerRuntimeHandle`. The owner keeps the process
runtime, active-generation manager, constructor-owned server configuration,
streaming body-task tracker, shutdown phase, and bounded shutdown evidence
together. Startup remains bind-first: listener bind precedes database open,
migrations, account synchronization, C010 recovery, generation construction,
initial task installation, and HTTP acceptance.

Ctrl-C and Unix SIGTERM are the only registered termination signals. Signal
registration and delivery failures are typed. A shutdown request transitions
monotonically through quiescing, draining, closing/forced-closing, and
stopped. It closes manager admission and task scheduling, lets Axum drain
accepted connections, joins tracked stream body tasks, drains/joins generation
work, closes generation resources, and closes SQLite last.

The process-only forced-close path is intentionally separate from live
rehash retirement. It may abort tracked body tasks and close provider
transports after the bounded finalization window expires; accepted work is
never force-closed by ordinary retirement. The result is an explicit typed
`ForcedShutdown` report and the database remains reopenable for C010 recovery.

## Requirement-to-evidence matrix

| R009 requirement | Evidence | Result |
|---|---|---|
| Bind before mutation | `run_with_digest` binds before `Database::open`; `bind_failure_happens_before_database_creation_or_mutation` proves a blocked port leaves the DB path absent | Pass |
| Startup migration/recovery ordering | `run_with_digest` and `serve_listener` perform migration, account sync, and `ProcessRuntime::reconcile_startup` before factory/task setup; startup failures close the DB | Pass |
| One startup generation/task installation | `ServerRuntime` is created only after factory transfer and `install_initial_tasks`; `initial_tasks_are_installed_once_before_acceptance` observes one transition and three active callbacks | Pass |
| Explicit process lifecycle owner | `ServerRuntime` owns process/manager/body-task lifecycle; `ServerRuntimeHandle` is the shutdown API | Pass |
| Portable typed signals | `shutdown_signal` handles Ctrl-C on all platforms and SIGTERM on Unix; failures become `SignalError` and request shutdown | Pass |
| Quiesce | handle request atomically closes manager admission, calls `RuntimeTaskSupervisor::begin_shutdown`, and wakes Axum graceful shutdown | Pass |
| Graceful finite/stream drain boundary | Axum graceful shutdown is bounded; streaming execution retains its generation lease in a tracked body task; owner waits task/body/generation boundaries before DB close | Pass |
| Retained finalization ordering | manager shutdown waits generation leases and existing retirement/finalization boundaries; generation close keeps the R004 finalization-before-provider order | Pass |
| Forced shutdown | stalled lease test receives `ForcedShutdown`, records outstanding lease evidence, closes resources, and reopens the same SQLite file | Pass |
| Close-once and DB-last cleanup | generation close remains idempotent; owner joins task/body work, calls `RuntimeManager::close_for_shutdown`, then closes DB; duplicate shutdown and reopen tests pass | Pass |
| Reload/shutdown ordering | existing R007 staged swap checks reject pre-shutdown work and accept committed work during shutdown; `RuntimeManager::shutdown` wakes the admission gate and `accept_during_shutdown` adopts a committed pointer | Pass |
| Cancellation safety | `ServerRuntime` drop requests shutdown and starts detached bounded cleanup; server/body tasks are owned by the lifecycle state rather than caller-local cleanup | Pass |
| Error isolation and diagnostics | task and generation close failures are bounded in typed reports; forced state is explicit; body/generation identifiers are structural and no config/body/credential payload is added | Pass |
| Scope boundary | no daemon, control socket, CLI, backup/update capability, schema, executor redesign, or live-rehash retirement force-close was added | Pass |

## Supported difference

Rust exposes a foreground `ServerRuntimeHandle` for M9 instead of Python's
signal-aware application shutdown coordinator. The lifecycle semantics remain
the same: no new generation acquisition/publication after quiesce, accepted
generation leases remain valid through graceful drain, and process shutdown
may abandon unresolved finalization only after the bounded forced phase.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/unit/test_runtime_task_inventory.py tests/unit/test_runtime_tasks.py tests/integration/test_startup_lifecycle.py tests/integration/test_application_startup.py -q --tb=short --maxfail=1
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk git diff --check
```

Observed focused results:

- R009 Rust: 5 passed.
- Python migration oracle: 89 passed, 3 skipped.
- Python startup/task bundle: 68 passed.
- Python smoke suite: 14 passed.
- Clippy and diff checks passed.

The full Rust aggregate result is recorded before commit completion below.

## Unresolved findings

No unresolved R009 correctness, resource, security, compatibility, schema,
dependency, or scope finding remains. R010 remains responsible for removing
long-lived generation authority from production Axum state, dynamic live body
limits, readiness authority, and broader bounded runtime diagnostics. R011
remains the only plan allowed to close M8.

## Future-plan audit and registry transition

R009 is removed from the dependency-ready table and added to the completed
implementation table. R010 is promoted to the sole dependency-ready M8 plan
because its hard dependency, accepted R009 closure, is satisfied. R011 remains
queued behind R010. M9 operational CLI/control/daemon work remains blocked on
accepted R011 M8 closure and its separate planning/implementation review.
