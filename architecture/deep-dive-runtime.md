# Deep Dive: Runtime & Process Management

Back to [Overview](overview.md)

## Purpose

Manages the EggPool process lifecycle, runtime generations, and the supervisor + Granian worker process model. Designed for reliability on resource-constrained devices (Raspberry Pi).

## Process Model

```
┌─────────────────────────────────────┐
│         Supervisor Process           │
│  • PID file ownership               │
│  • Health probes                    │
│  • Restart management               │
│  • Daemon mode (default)            │
└──────────────┬──────────────────────┘
               │ spawns
    ┌──────────▼──────────┐
    │   Granian Worker     │
    │   workers=1          │
    │   runtime_threads=1  │
    │   (single event loop)│
    └─────────────────────┘
```

## Key Modules

### `runtime.py` — Process Lifecycle

- PID management (start/stop/restart)
- Daemon mode (default for `eggpool serve`)
- `--verbose` for foreground mode
- Health probes for supervisor
- `runtime_threads=1` is required; other values fail configuration validation
- Runtime database integrity/indeterminate-state failures close admission and
  exit the worker; systemd restart runs startup integrity and crash repair.

### `runtime_manager.py` — RuntimeManager

Runtime generation ownership:
- **`RuntimeManager`**: owns active/retiring generation slots
- **`RuntimeGeneration`**: immutable frozen-dataclass snapshot
- **`GenerationLease`**: request-path access to a generation
- **`ProcessRuntime`**: holds process-owned containers (DB connections) that outlive generations
- **Generation builder**: constructs candidate generations for live reload

Request-path code obtains `GenerationLease` via `wrap_stream_with_lease` or `leased_runtime`. A generation swap never interrupts in-flight requests.

### `runtime_dispatch.py` — Dispatch Timing

Bounded rolling-window timing recorders:
- **`DispatchOverheadRecorder`**: coordinator-internal slice (context_build → httpx send)
- **`LocalPreUpstreamRecorder`**: full EggPool-side window (ASGI entry → upstream dispatch)
- **`DispatchSpanRecorder`**: 200-sample dispatch span telemetry with request-coherent sampling (5% default; configurable via `[metrics.dispatch_spans].sample_rate`)

Both use monotonic/performance clocks. Metrics additive: `local_pre_upstream` includes context_build, body parsing, validation, segmentation, compression, and coordinator overhead; `dispatch_overhead` covers only coordinator-internal selection/persistence/dispatch.

### `runtime_metrics.py` — Runtime/Ops Metrics

`RuntimeMetricsService` gathers:
- Process topology
- Memory usage
- Background task state
- Database health
- OS load average (`os.getloadavg` + normalized per-core)
- Bounded rolling-window dispatch-overhead distribution
- Selection claim diagnostics
- Dispatch writer diagnostics
- Model info health snapshot
- `finalization_supervisor`: the active generation's bounded retained-terminal
  job snapshot, including active/retry-pending/failed counts, saturation and
  registration counters, and retry capacity/age limits. It is `null` during
  lightweight or partial startup when no supervisor is available.
- `finalization_ownership`: bounded ownership facts from `RuntimeManager`,
  including the active generation ID and supervisor counts, retiring
  generation count, total terminal references, oldest retiring age, blocked
  status, and redacted last failure class/stage.

### `runtime_paths.py` — Path Resolution (stdlib-only)

PID file and log path resolution. Must stay stdlib-only for the Raspberry Pi watchdog contract.

### `runtime_tasks.py` — Task Registration

Unified task registration for startup and candidate generation construction.

### `runtime_task_inventory.py` — Task Inventory

`RUNTIME_TASK_INVENTORY` — reviewable inventory of all background tasks.

### `fastcli.py` — Fast-Path CLI (stdlib-only)

Handles `croncheck` and `ensure-running` without importing Click:
- `croncheck`: checks if process is running, restarts if not
- `ensure-running`: ensures process is running

Both are cheap operations for Raspberry Pi watchdog cron jobs.

### `event_loop_lag.py` — EventLoopLagMonitor

Bounded event-loop lag telemetry. Monitors async event loop responsiveness.

## Runtime Generations

Generations are immutable snapshots of application state:
- **Active generation**: serves requests
- **Retiring generation**: drains in-flight requests
- **Candidate generation**: built during live reload

Live reload (`eggpool rehash`) builds a candidate generation, validates it, and atomically publishes it. In-flight requests and accepted retained finalization jobs on the retiring generation complete using the old dependencies; publication does not wait for the old generation to close.

`RequestFinalizationSupervisor` is generation-owned. Its first accepted
selected-finalization job or terminal command acquires one synchronous
terminal reference on the generation slot; duplicate registration and retries
reuse that reference. The slot closes only after
both request leases and terminal references are zero. A live retirement
deadline with an unresolved terminal reference invokes the existing fatal
worker handler and leaves the slot resident rather than closing its router,
quota, health, or client dependencies. When the final reference releases,
normal close resumes. Process shutdown may abandon references because startup
repair owns unresolved durable work after process death.

Quarantine hydration is part of candidate preparation, before publication.
`RuntimeGenerationFactory.prepare()` never catches a quarantine read or row
conversion failure to start with an empty state. Startup consequently remains
closed until complete durable quarantine state is known, while a failed rehash
candidate is aborted and the active generation retains its existing quarantine.
Authoritative catalog reappearance uses durable-first, exact-key recovery; a
failed durable clear leaves the current in-memory suppression intact.

## Process-Owned vs Generation-Leased

| Component | Ownership | Survives Generation Swap |
|-----------|-----------|-------------------------|
| Database connections | `ProcessRuntime` | Yes |
| `DispatchPersistenceWriter` | `ProcessRuntime` | Yes |
| Account registry | `RuntimeGeneration` | No (rebuilt) |
| Model catalog | `RuntimeGeneration` | No (rebuilt) |
| Health manager | `RuntimeGeneration` | No (rebuilt) |
| Quota estimator | `RuntimeGeneration` | No (rebuilt) |
| Finalization supervisor and accepted terminal jobs | `RuntimeGeneration` | No (retained until convergence) |

## Key Invariants

- Single event-loop thread is canonical (`runtime_threads=1`)
- All `asyncio.Lock` objects are loop-bound
- `MetricsWriteCoalescer` is the only component using `threading.Lock`
- `fastcli` and `runtime_paths` are stdlib-only
- PID file owned by supervisor
- Generation swap never interrupts in-flight requests or accepted retained terminal jobs
- Process-owned containers outlive any generation
