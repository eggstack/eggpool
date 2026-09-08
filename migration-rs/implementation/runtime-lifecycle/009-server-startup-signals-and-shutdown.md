# R009 — Server Startup, Signals, Graceful Drain, and Forced Shutdown

Status: closed; see [closure record](../../closure/runtime-lifecycle/009-status.md)

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: infrastructure/invariant

## Objective

Make the Rust server process own the complete M8 lifecycle: bind-first startup, DB migration/recovery, initial generation installation, task supervisor startup, Axum serving, signal handling, reload exclusion, graceful request/finalization drain, bounded forced shutdown, and final resource close.

R009 does not implement daemon/stop/restart CLI behavior; M9 wraps this foreground lifecycle later.

## Process runtime owner

Replace the current loose `server::run` local variables with one explicit process lifecycle owner (`ServerRuntime`, `ProcessRuntimeHandle`, or equivalent) containing:

- process-owned DB;
- R003 `RuntimeManager`;
- R006 task supervisor;
- R007 reload service/serialization state;
- R008 startup recovery report/background registration;
- constructor-owned server configuration/listener metadata;
- shutdown state/notifications;
- bounded runtime diagnostics inputs.

The owner should make shutdown order obvious and testable rather than relying on local-variable drop order.

## Startup sequence

Preserve the accepted F006 bind-before-mutation safety rule.

Required order:

1. validate constructor-owned server/API-key configuration;
2. bind TCP listener before mutating DB state;
3. open process-owned DB and run existing migrations/checksum checks;
4. run/sweep C010 crash reconciliation to convergence per R008;
5. synchronize startup config-derived durable account/provider state using the same safe persistence logic R007 uses;
6. build the initial `PreparedGeneration` through R002 factory;
7. install generation 1 through R003 manager without a reload transaction;
8. commit/start R006/R008 process task specs;
9. construct Axum router over process state/runtime manager;
10. begin accepting HTTP.

If any step fails, close already-owned resources in reverse lifecycle order. Do not leave a mutated DB after a bind failure because bind already occurred first.

Initial generation installation does not count as live rehash and should have a clear generation id/digest result for diagnostics.

## Signal model

Keep the signal surface small and portable:

- Ctrl-C on all supported platforms;
- SIGTERM on Unix;
- any additional rehash signal such as SIGHUP only if R001 proves it is part of the accepted Python/operator contract. Do not invent SIGHUP as an undocumented second rehash control path.

Signal registration failure is typed and must not silently disable shutdown handling.

A second termination signal may request the forced-shutdown phase if implementation remains simple; otherwise one signal plus bounded deadline is sufficient. Document the exact behavior and test it.

## Monotonic shutdown phases

Use explicit process states such as:

```text
running -> quiescing -> draining -> closing -> stopped
                              \-> forced_closing
```

Required behavior:

### Quiesce

- set manager shutdown state so no new generation acquisition/publication/reload stage can begin;
- stop/reject new reload transactions;
- trigger Axum graceful shutdown so listener stops accepting new connections;
- stop scheduling new process/background ticks.

### Drain

- allow accepted Axum finite/stream requests to finish within the graceful deadline;
- wait for active generation leases/retirement tasks;
- drain each generation's retained `FinalizationSupervisor`;
- finish/cancel current background ticks according to their bounded task timeout;
- no provider replay or new crash-recovery attempt is introduced during drain.

### Close

- close active and retiring generation resources through R004 close order;
- close task supervisor handles;
- perform any existing safe SQLite checkpoint/flush required by process-owned services;
- close DB last.

### Forced close

When the graceful deadline expires:

- mark forced shutdown in diagnostics;
- stop waiting for client/stream leases and abort server/body tasks as allowed by Axum/Tokio ownership;
- give retained finalization a final bounded drain window where possible;
- close remaining generation transports/resources in deterministic order;
- close DB last;
- return a typed non-clean shutdown outcome rather than reporting graceful success.

Forced process shutdown may abandon an external client stream because the process is exiting; this exception must not be reused by live-rehash retirement.

## Reload/shutdown race

R007 and R009 must define one ordering:

- shutdown before reload stage -> reload rejects/aborts candidate;
- shutdown during pre-commit candidate work -> candidate aborts and shutdown proceeds;
- shutdown after the reload admission gate closes -> the reload service resolves the bounded acceptance window to commit or rollback/compensate, then shutdown adopts the accepted active/retiring generations;
- shutdown after reload accept -> retirement/shutdown jointly own the old slot exactly once.

No race may leave admission permanently gated, a candidate unowned, or a retirement task detached.

## Axum graceful shutdown integration

Current `axum::serve(...).with_graceful_shutdown(shutdown_signal())` is not sufficient by itself because it does not own generation/finalization/task/DB drain.

Refactor so the signal triggers the process lifecycle owner; server completion and runtime drain are coordinated before `server::run` returns.

Streaming body tasks spawned by M7/server must be tracked by Axum/lease ownership well enough that manager lease counts accurately represent remaining generation work.

## Startup/shutdown error isolation

A background-task or old-generation close error must not panic the runtime. Aggregate bounded typed shutdown diagnostics and choose clean/non-clean exit result.

A fatal invariant such as compensation failure, DB close failure, or unresolved finalization after forced deadline may produce a nonzero server error but must not require DB deletion/reset on next start; R008/C010 recovery handles durable nonterminal state.

## Tests

Required deterministic integration tests:

### Startup

- listener bind failure leaves DB file/state untouched;
- migration/recovery failure closes listener/resources and does not serve;
- crash leftovers reconcile before first HTTP request;
- initial generation and task specs installed exactly once;
- initial factory failure closes candidate/provider pool and DB.

### Graceful signal shutdown

- finite request in flight receives completion before close;
- streaming request drains and finalizes before provider pool/DB close;
- retained finalizer blocked briefly causes shutdown to wait, then close after release;
- task tick finishes/cancels according to policy;
- DB closes last.

### Forced shutdown

- deliberately stalled stream exceeds graceful deadline;
- forced state is reported;
- no new request/reload accepted after shutdown start;
- resources close once and process returns without hanging;
- same DB can be reopened and C010 reconciliation converges any interrupted durable rows.

### Reload race

Place barriers at pre-stage, gate-closed, pointer-commit, acceptance, and retirement scheduling; trigger shutdown at each point and assert exactly one coherent active/closed ownership outcome.

### Multiple signals/cancellation

- duplicate shutdown request is idempotent;
- caller cancellation of `server::run` cannot bypass cleanup if the process runtime owns resources;
- signal handler failure returns typed error.

## Executor/thread scope

Do not turn R009 into a Tokio executor redesign. The current runtime threading behavior remains as accepted unless another canonical plan explicitly owns `server.threads` execution parity. M8 classifies it restart-required but does not need a multi-runtime/work-stealing abstraction for local deployments.

## Scope boundaries

R009 must not:

- implement daemonization/systemd/croncheck/stop/restart CLI;
- implement update/backup CLI;
- add a control socket;
- change M7 retry/finalization policy;
- force-close old generations during normal live rehash;
- add OS-specific service managers.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R009 lifecycle/shutdown tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <Python startup/shutdown/runtime-manager tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R009 closes only when:

- bind-first startup is preserved;
- C010 recovery occurs before first request acceptance;
- process runtime owns every generation/task/DB close path;
- graceful shutdown drains accepted requests and retained finalization before resource close;
- forced shutdown is bounded, explicit, and recoverable on next start;
- reload/shutdown races cannot strand gate/candidate/retirement ownership;
- DB closes last and is reopenable/readable;
- no daemon/control/M9 scope was pulled forward.

## Closure

Write `migration-rs/closure/runtime-lifecycle/009-status.md` and promote R010.
