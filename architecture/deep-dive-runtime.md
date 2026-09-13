# Deep Dive: Runtime and Process Management

Back to [Overview](README.md)

## Purpose

The native runtime owns the EggPool process lifecycle, immutable runtime
generations, live configuration publication, bounded background work, and
graceful shutdown. The design keeps one process and one Tokio
`current_thread` runtime, which is suitable for the supported Raspberry Pi and
LAN deployments.

## Current ownership graph

```text
CLI/runtime adapter -> operations services
Axum server         -> request coordinator -> routing/provider transport
                                      -> canonical wire codecs/stream
runtime manager     -> generation factory -> supervised background tasks
config parser       -> reload policy -> transactional reload/publication
SQLite repositories <- accounting/catalog/health/maintenance
```

`rust/src/runtime.rs` adapts CLI commands to the existing operation services.
`rust/src/operations/lifecycle.rs` composes safe detached start, stop, restart,
identity-proof, and watchdog workflows over `process.rs`, `paths.rs`, and
`control.rs`; the CLI keeps prompts, presentation, and exit-code mapping.
`rust/src/server/mod.rs` owns Axum startup, route assembly, shared state, and
process lifespan. Its `middleware.rs`, `health.rs`, `dashboard.rs`, and
`inference.rs` siblings own the corresponding HTTP adapters. Request routing,
provider transport, wire adaptation, persistence, and finalization remain in
their respective modules.

## Process model

`eggpool serve` runs the native executable. The process owns PID management,
health probes, foreground/daemon startup, restart behavior, and the active and
retiring runtime generations. `rust/src/operations/lifecycle.rs` owns the
workflow composition while `process.rs` remains the authority for PID files,
independent health/control evidence, and signaling. `paths.rs` is the shared
authority for PID, log, state, and control-socket paths.

The executable is started with Tokio's `current_thread` runtime in
`rust/src/main.rs`. `[server].threads` remains a validated compatibility key
and is included in runtime diagnostics, but it does not select Tokio worker
threads. Its existing reload classification remains restart-required. A future
removal or multithread implementation requires a separate compatibility and
performance decision.

## Runtime generations

`rust/src/runtime_lifecycle.rs` owns the generation state machine:

- `RuntimeManager` owns the active and retiring generation slots.
- `RuntimeGeneration` is the immutable snapshot used by request handling.
- `GenerationLease` keeps a generation alive for an in-flight request.
- `ProcessRuntime` owns state that survives generation swaps, including the
  database and bounded learned routing state.
- `RuntimeGenerationFactory` prepares a complete candidate before publication.

A generation contains the provider client pool, catalog, account registry,
router, coordinator, health state, statistics, and generation-scoped
background tasks. Requests acquire a lease through the server/runtime
boundary. Publishing a replacement does not interrupt requests already using
the retiring generation.

The semantic model-router registry is generation-owned and compiled through
the shared `eggpool-model-routing` crate. The process-owned sticky affinity
cache survives safe swaps only when the new registry has the same semantic
fingerprint. Wire-surface learning is likewise bounded, process-owned, and
credential-free; changed surface definitions cannot reuse incompatible
observations.

## Reload and publication

`rust/src/config.rs` owns TOML shape, defaults, and validation.
`rust/src/config_reload_policy.rs` classifies live, restart-required, and
ignored changes. `rust/src/reload.rs` builds the candidate generation,
reconciles durable state, publishes it atomically, and retires the old
generation.

`eggpool rehash` is serialized. Invalid candidates do not replace the active
generation. A restart-required change is reported before publication, and a
failed candidate leaves the current generation and its process-owned state
unchanged. Reload diagnostics are owned by the reload operation rather than a
caller that may finish early.

## Background work and shutdown

`rust/src/task_supervisor.rs` registers bounded tasks for startup and
generation construction. Generation-scoped supervisors own catalog refresh,
health, maintenance, statistics, and retained finalization work according to
the active task specification. Process-scoped containers such as the metrics
coalescer and wire resolver are flushed or stopped through their existing
shutdown contracts.

Shutdown first closes control-plane admission, then retires the active
generation and joins its supervised work. Readiness, routing-trace writers,
and retained finalization work stop before the process-owned database
connections disconnect. PID cleanup and child-process joins remain bounded;
systemd or the watchdog may restart a worker that exits after an indeterminate
database state.

Crash reconciliation is a one-shot durable repair at startup. It repairs
unfinished request, attempt, and reservation rows without resurrecting
process-local routing, quota, health, wire, or supervisor state.

## Diagnostics

`eggpool runtime-status --json` and `/api/stats/runtime` expose bounded,
redacted process topology, generation, task, database, routing, and
finalization information. These diagnostics are observations, not a second
runtime authority. Use host process/socket tools for operating-system details
such as file descriptors and outbound sockets.

## Key invariants

- The native process is the lifecycle authority; server mirrors are not
  independent owners.
- A complete, validated generation is built before atomic publication.
- Generation swaps never interrupt in-flight requests or accepted retained
  terminal work.
- Process-owned state is bounded, in memory, and never stores credentials or
  raw request/provider bodies.
- Runtime and database transitions fail closed on validation, commit, or
  ownership ambiguity.
- Shutdown closes supervisors and database users in ownership order, with
  bounded joins and startup repair as the process-death safety net.
