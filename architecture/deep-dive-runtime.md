# Deep Dive: Runtime and Process Management

Back to [Architecture](README.md). See also [overview.md §§2,10](overview.md), [Control](deep-dive-control.md), and [Background](deep-dive-background.md).

## Ownership

- `rust/src/runtime.rs` adapts CLI commands to `operations/*` services; it owns prompts, presentation, and exit-code mapping only.
- `rust/src/operations/lifecycle.rs` composes start/stop/restart/ensure-running/watchdog workflows over `process.rs`, `paths.rs`, and `control.rs`.
- `rust/src/server/mod.rs` owns the pre-bound listener, route assembly, shared `AppState`, auth, body admission, signals, and shutdown reporting. Downstream HTTP/1 transport is EggServe-owned (`eggserve-server =0.4.0`, `tower` feature).
- `rust/src/runtime_lifecycle/` owns the generation state machine; `rust/src/task_supervisor.rs` owns supervised background tasks; `rust/src/reload.rs` plus `rust/src/config_reload_policy.rs` own live publication.
- `rust/src/operations/status.rs` owns the compact proxy/provider health snapshot (shared readiness evaluation with `readyz`, no outbound probes).

## Process model

`eggpool serve` runs one native process on Tokio's `current_thread` runtime (`rust/src/main.rs`). `[server].threads` remains a validated compatibility key in diagnostics but does not select Tokio workers; its reload disposition stays restart-required. The performance campaign keeps the single-thread runtime, streaming bridge, SQLite gate, and routing lock unless comparable loopback evidence justifies a narrow change.

## Server transport boundary

`serve_listener` builds the Axum router, derives `eggserve_runtime_config`, chains `Server::builder().runtime(config).from_listener(listener).build()`, wraps the router with `TowerToEggserve::with_policy` using `RequestBodyPolicy::Stream { max_bytes: 1 GiB }` (`EGG_SERVE_REQUEST_BODY_LIMIT`), and drives it with `start_with_service`. The handle splits into a shutdown control plus passive typed completion (`into_parts` / `completion.wait()`). EggServe owns HTTP/1 parsing, connection admission, and bounded drain: at most 1024 connections and 1024 in-flight requests, explicit header/parser ceilings, and a five-second graceful connection drain inside the ten-second foreground deadline (`GRACEFUL_SHUTDOWN_TIMEOUT`).

`build_router` (`rust/src/server/mod.rs:1033-1090`) always mounts health/readiness (`/v1/healthz`, `/v1/readyz`), `/v1/models`, the authenticated integrations/profile plus runtime/update/status projections (`/api/integrations/v1/profile`, `/api/stats/runtime`, `/api/stats/update`, `/api/status`), the four inference routes (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/responses/compact`), and `/static/*`. Dashboard pages (`/`, `/accounts`, `/models`, `/models/{*model_id}`, `/latency`, `/events`, `/timeseries`, `/bandwidth`, `/pings`, `/reliability`, `/routing`, `/traces`, `/runtime`, `/cache`) and the observability JSON (`/api/stats/summary`, `/api/stats/transcoding`, `/api/stats/cache-observability`, `/api/stats/canonical-request-segmentation`, `/api/stats/cache-stability`, `/api/stats/request-shaping`, `/api/timeseries`, `/api/timeseries/grouped`) mount only when `dashboard.enabled`.

Inference handlers (`rust/src/server/inference.rs:119-154`) make exactly one `coordinator::endpoints::execute_endpoint` call per request (`rust/src/coordinator/endpoints.rs:777`); `execute_finite` (`endpoints.rs:750`) and `execute_stream` (`endpoints.rs:929`) are thin wrappers over it, and `/v1/responses/compact` uses the finite-only `execute_compact_finite` (`endpoints.rs:865`).

## Runtime generations

`rust/src/runtime_lifecycle/` splits by ownership, re-exported from `mod.rs` so callers never depend on file layout:

- `process.rs` — `ProcessRuntime`: database handle, affinity, wire resolver, task supervisor, metrics coalescer, update-checker state. Built via `new` / `new_with_config` / `with_config_path_and_config`.
- `generation.rs` — `RuntimeGeneration` (immutable snapshot: config, digest, inference state, provider pool, finalization), `RuntimeGenerationFactory::prepare` / `prepare_with_durable_accounts` (single construction path), `PreparedGeneration` (`transfer` exactly once, `abort` with wire-preference rollback), `CandidateOwnership` (`Prepared`/`Transferred`/`Aborting`/`Aborted`).
- `lease.rs` — `GenerationSlot` (`GenerationSlotState`: `Active`/`Retiring`/`DrainingFinalization`/`Closing`/`Closed`/`FailedClose`), `GenerationLease` (pins one generation across awaits), `GenerationFinalizationGuard` (retained terminal reference, rejected once drain begins), `GenerationAcquireError`, `GenerationStageError`, `GenerationSwapError`.
- `manager.rs` — `RuntimeManager` (`ArcSwap` active pointer, publication epoch, retiring queue, retirement tasks): `acquire` (gated lease), `stage` (closes admission, transfers candidate), `StagedGenerationSwap` (`commit_pointer` → `accept` / `rollback`, plus `accept_during_shutdown` and fail-closed `fail_closed`), `schedule_retirement`, `close_for_shutdown`, `drain_retirements_with_deadline`.
- `recovery.rs` — `reconcile` plus `StartupRecoveryReport` / `StartupRecoveryError`.
- `diagnostics.rs` — secret-free projections (`RuntimeDiagnosticsSnapshot`, `ActiveGenerationDiagnostics`, `PublicationDiagnostics`, `RetiringGenerationDiagnostics`, `ReloadDiagnostics`, `TaskDiagnostics`, `ShutdownDiagnostics`, `RuntimeDiagnosticCounters`).

State machine: `candidate built -> staged -> pointer committed -> accepted` (or `rolled back`); `old active -> retiring -> lease/finalization drain -> close -> retired`. Publishing a replacement never interrupts leases held on the retiring generation. Bounded constants in `rust/src/runtime_lifecycle/mod.rs:52-56`: `MAX_RETIRING_GENERATIONS` (4), `DEFAULT_GENERATION_CLOSE_TIMEOUT` (1s), `MAX_STARTUP_RECONCILIATION_PASSES` (1024). Slot states (`Active`/`Retiring`/`DrainingFinalization`/`Closing`/`Closed`/`FailedClose`) live at `rust/src/runtime_lifecycle/lease.rs:21-28`.

## Reload and publication

`rust/src/config.rs` owns TOML shape and validation. `config_reload_policy.rs::classify_transition` is the only reload-vs-restart authority: pure, deterministic, redacted; mixed changes are wholly restart-required; `[integrations].advertise_base_url` is `Live` profile output only and never changes the listen socket. `ReloadService` (`reload` / `reload_path` / `reload_bytes`) revalidates, reclassifies, builds the complete candidate, reconciles durable provider/account rows in a caller-owned transaction, stages task/wire deltas, commits pointer plus transaction, and retires the old generation. `ReloadResultCategory` (`Applied`/`Noop`/`RestartRequired`/`ValidationFailed`/`StaleDigest`/`Busy`/`RetirementBacklog`/`Aborted`/`CompensationFailed`) is the typed outcome; failure leaves the active generation and process-owned state unchanged.

## Background work and shutdown

`task_supervisor.rs` supervises fixed-delay `RuntimeTaskSpec` tasks (`rust/src/task_supervisor.rs:132-145`) with `TaskOwnership::{Process, ActiveGenerationLeased}` over the canonical inventory (`RUNTIME_TASK_NAMES`, `task_supervisor.rs:101-108`; specs at `:216-301`): process-owned (`checkpoint`, `metrics_flush`, `update_checker`, `automatic_backup`) versus generation-leased (`catalog_refresh`, `retention_cleanup`). Details in [Background](deep-dive-background.md).

Shutdown starts with quiesce (`ServerRuntimeHandle::request_shutdown`, `rust/src/server/mod.rs:584-609`: phase to `Quiescing`, admission closed via `manager.shutdown()` plus task-supervisor `begin_shutdown`). `serve_listener` (`server/mod.rs:927`) then runs EggServe control shutdown plus `completion.wait()`, then control-socket `close`. `close_runtime_resources_until` (`server/mod.rs:495-577`) continues with supervised-task `shutdown_with_timeout` on the remaining deadline, bounded metrics flush, body-task drain (aborted only when forced/timed out), generation-manager `close_for_shutdown`, and finally the process-owned database `close`. `ShutdownPhase` (`Running` → `Quiescing` → `Draining` → `Closing`/`ForcedClosing` → `Stopped`) and `ShutdownReport` (forced flag, leases/terminal references/body tasks at deadline, task counts, database outcome) are the bounded evidence.

## Recovery and diagnostics

Startup reconciliation is a one-shot bounded repair (`rust/src/runtime_lifecycle/recovery.rs:57`, max 1024 passes) of unfinished request, attempt, and reservation rows; it never resurrects routing, quota, health, wire, or supervisor state. `eggpool status` / `GET /api/status` expose the compact health snapshot; `eggpool runtime-status --json` and `GET /api/stats/runtime` expose the bounded redacted topology above. Diagnostics observe; they are never a second runtime authority.

## Invariants

- One process, one `current_thread` runtime, one `ArcSwap` active generation.
- Complete validated candidates publish atomically; in-flight leases drain on the retiring generation.
- At most 4 retiring generations; failed closes stay resident for diagnosis (process exit may force them).
- Process-owned state is bounded, in memory, and secret-free (no credentials, prompts, raw bodies, cache keys).
- All transitions fail closed; startup repair is the process-death safety net.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
```
