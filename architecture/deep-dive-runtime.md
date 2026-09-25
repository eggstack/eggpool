# Deep Dive: Runtime and Process Management

Back to [Architecture](README.md)

## Purpose

The native runtime owns the EggPool process lifecycle, immutable runtime
generations, live configuration publication, bounded background work, and
graceful shutdown. The design keeps one process and one Tokio
`current_thread` runtime, which is suitable for the supported Raspberry Pi and
LAN deployments.

## Current ownership graph

```text
CLI/runtime adapter -> operations services
pre-bound listener + Axum router -> TowerToEggserve -> EggServe H1 -> request coordinator -> routing/provider transport
                                       -> canonical wire codecs/stream
runtime manager     -> generation factory -> supervised background tasks
config parser       -> reload policy -> transactional reload/publication
SQLite repositories <- accounting/catalog/health/maintenance
```

`rust/src/runtime.rs` adapts CLI commands to the existing operation services.
`rust/src/operations/lifecycle.rs` composes safe detached start, stop, restart,
identity-proof, and watchdog workflows over `process.rs`, `paths.rs`, and
`control.rs`; the CLI keeps prompts, presentation, and exit-code mapping.
`rust/src/server/mod.rs` gives its pre-bound listener to the exact-pinned
EggServe H1 runtime (`eggserve-server =0.3.0`, `tower` feature) and adapts the
existing Axum router through the server-owned `TowerToEggserve`. Concretely,
`serve_listener` builds the Axum router, derives
`eggserve_runtime_config`, chains
`Server::builder().runtime(config).from_listener(listener).build()`, wraps
the router with `TowerToEggserve::with_policy` using
`RequestBodyPolicy::Stream { max_bytes: 1 GiB }`
(`EGG_SERVE_REQUEST_BODY_LIMIT`), and drives it with
`start_with_service`, splitting the handle into a shutdown control plus a
passive typed completion (`into_parts` / `completion.wait()`). EggServe owns
HTTP/1 parsing, connection admission and transport, and bounded connection
drain: at most 1024 connections and 1024 in-flight requests, explicit
header/parser ceilings, and a five-second graceful connection drain.
EggPool's server module retains route assembly, shared state, auth, body
admission, signals, process lifespan, and shutdown reporting. EggServe 0.3
policy/admission defaults remain EggServe-owned. Request routing,
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

The 2026 performance qualification measured the current-thread runtime,
streaming bridge, SQLite gate, and routing selection lock as evidence-gated
boundaries. They remain intentionally simple unless a comparable loopback
workload demonstrates material tail-latency or throughput benefit that
outweighs lifecycle and ownership complexity.

## Runtime generations

The `rust/src/runtime_lifecycle/` package owns the generation state machine.
Its modules follow state ownership rather than request flow:

- `process.rs` owns `ProcessRuntime` and process-lifetime shared resources.
- `generation.rs` owns candidate construction, immutable generation resources,
  and the generation close boundary.
- `lease.rs` owns generation slots, request leases, and retained terminal
  references.
- `manager.rs` owns the `ArcSwap` active pointer, publication gate, staged
  swaps, and the bounded retiring queue.
- `recovery.rs` owns bounded startup crash reconciliation.
- `diagnostics.rs` owns secret-free lifecycle projections and bounded helpers.
- `mod.rs` contains the compatibility re-exports only.

The state machine exposed by those modules is:

```text
candidate built -> staged -> active pointer committed -> accepted
                         \-> rolled back
old active -> retiring -> lease/finalization drain -> close -> retired
```

The public lifecycle types remain re-exported from `runtime_lifecycle` so
server, reload, operations, and integration-test callers do not depend on
the internal file layout. Bounded constants live in `mod.rs`:
`MAX_RETIRING_GENERATIONS` (4), `DEFAULT_GENERATION_CLOSE_TIMEOUT` (1s),
and `MAX_STARTUP_RECONCILIATION_PASSES` (1024).

The core ownership types are:

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
`rust/src/config_reload_policy.rs` is the single typed transition authority;
its pure `classify_transition` result is redacted and safe to carry through
operator apply paths. `rust/src/reload.rs` revalidates and reclassifies the
candidate, then builds the candidate generation, reconciles durable state,
publishes it atomically, and retires the old generation.

`eggpool rehash` is serialized. Invalid candidates do not replace the active
generation. A restart-required change is reported before publication, and a
failed candidate leaves the current generation and its process-owned state
unchanged. Reload diagnostics are owned by the reload operation rather than a
caller that may finish early.

Configuration mutations keep text editing separate from application. The
bounded editor validates and classifies the pre-edit to post-edit transition
before atomic replacement. Restart-after-mutation is composed by
`operations/lifecycle.rs`; the runtime adapter only presents the outcome.

## Background work and shutdown

`rust/src/task_supervisor.rs` remains the sole owner of supervised task
handles, callback registration, task-spec diffs, and bounded task shutdown.
It registers bounded tasks for startup and generation construction.
The canonical task names (`RUNTIME_TASK_NAMES`) are `catalog_refresh`,
`retention_cleanup`, `checkpoint`, `metrics_flush`, `update_checker`, and
`automatic_backup`, split by `TaskOwnership`: process-owned tasks
(checkpoint, metrics flush, update check, auto backup) versus
generation-leased tasks (catalog refresh, retention/cleanup) whose
callbacks receive a fresh lease per tick. `operations/metrics.rs`
coalescing and `operations/update.rs` freshness probes plug in here.
Process-scoped containers such as the metrics coalescer and wire resolver
are flushed or stopped through their existing shutdown contracts.

Shutdown first quiesces EggPool (`request_shutdown`, phase
`Running -> Quiescing`), then requests EggServe shutdown via its control
and joins the passive typed completion (`completion.wait()`) within the
single ten-second foreground deadline (`GRACEFUL_SHUTDOWN_TIMEOUT`);
EggServe's explicit five-second connection drain fits inside it. The
control listener is closed next, then `close_runtime_resources_until`
runs in ownership order: supervised-task shutdown with the remaining
deadline, metrics flush, body-task drain (aborted only when already
forced or timed out), generation-manager `close_for_shutdown`, and
finally the process-owned database close. Phase transitions
(`Quiescing -> Draining -> Closing`/`ForcedClosing -> Stopped`) and the
`ShutdownReport` (forced flag, leases/terminal references/body tasks at
deadline, task counts, database outcome) are the bounded shutdown
evidence. PID cleanup and child-process joins remain bounded;
systemd or the watchdog may restart a worker that exits after an indeterminate
database state.

Crash reconciliation is a one-shot durable repair at startup. It repairs
unfinished request, attempt, and reservation rows without resurrecting
process-local routing, quota, health, wire, or supervisor state.

## Diagnostics

`eggpool status` / `GET /api/status` expose the compact proxy/provider health
snapshot (one row per provider, shared readiness evaluation with `readyz`),
while `eggpool runtime-status --json` and `/api/stats/runtime` expose bounded,
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
