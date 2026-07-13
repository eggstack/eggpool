# Live Configuration Rehash — Milestone B

## Runtime Generations and Request-Lease Infrastructure

> **Status: Completed** — see implementation in `src/eggpool/runtime_manager.py`,
> `src/eggpool/app.py`, `src/eggpool/runtime_metrics.py`, and
> `tests/unit/test_runtime_manager.py`.

## Objective

Refactor EggPool's runtime ownership so configuration-derived services can be replaced as a coherent unit without changing process-owned resources or interrupting active requests. This milestone introduces a `RuntimeManager`, immutable runtime generations, request/stream generation leases, deterministic generation startup/retirement, and generation-aware background-service ownership.

Operator-visible live reload remains disabled until milestone C. The purpose here is to establish the architecture under normal startup and shutdown first, reducing the risk that reload-specific control flow masks ownership defects.

## Design principles

- The active generation is immutable after publication.
- New requests acquire exactly one generation and retain it for their entire lifetime.
- Streaming responses retain their generation lease until stream completion or disconnect cleanup.
- The primary database, ASGI application, listener, and control-plane bootstrap remain process-owned.
- Configuration-derived clients, routing, quota, health, and task scheduling are generation-owned unless there is a documented reason otherwise.
- Startup uses the same generation builder that milestone C will use for candidate preparation.
- Generation teardown is idempotent and closes each owned resource exactly once.
- No request path should read a mixture of old and new generation services.

## Workstream B1 — Inventory and ownership map

Before moving code, create a checked-in developer note or module-level ownership table covering every object initialized in `app._lifespan_runtime` and `create_app`.

Classify each object as:

- process-owned;
- generation-owned;
- request-owned;
- repository/view over a process-owned database;
- shared immutable data;
- unresolved, requiring explicit design decision.

At minimum inventory:

- `AppConfig`;
- primary and stats `Database` instances;
- repositories;
- `AccountRegistry`;
- `ProviderClientPool`;
- `OutboundClientManager`;
- DNS/network backend;
- `Router` and quota estimator;
- `RequestCoordinator`;
- `HealthManager`;
- `CatalogService` and cache;
- model-info service;
- `StatsService`;
- metrics coalescer;
- update checker;
- backup service;
- `TaskSupervisor` and every periodic callback;
- dashboard dependencies stored on `app.state`.

The inventory must identify closures that capture startup `config`, `router`, `catalog`, or other generation candidates. These closures are a primary reload hazard.

## Workstream B2 — Define runtime generation types

Introduce a dedicated module, for example `eggpool/runtime_manager.py`.

Suggested core types:

```python
@dataclass(frozen=True)
class RuntimeGeneration:
    generation_id: int
    config: AppConfig
    config_digest: str
    registry: AccountRegistry
    router: Router
    coordinator: RequestCoordinator
    client_pool: ProviderClientPool
    outbound_manager: OutboundClientManager
    health_manager: HealthManager
    catalog: CatalogService
    supervisor: TaskSupervisor
    created_at_monotonic: float
```

The exact fields should follow the ownership inventory. Avoid storing duplicate aliases to the same mutable object without documenting lifecycle ownership.

Generation state that must mutate for lifecycle accounting should live in an internal wrapper rather than the frozen public snapshot:

```python
class _GenerationSlot:
    generation: RuntimeGeneration
    active_leases: int
    accepting_leases: bool
    retirement_started: bool
    retirement_complete: asyncio.Event
    close_lock: asyncio.Lock
```

Define `GenerationLease` as an async context manager with idempotent release semantics.

## Workstream B3 — Implement `RuntimeManager`

The manager should own:

- the active generation slot;
- monotonically increasing generation IDs;
- a lock for publication/replacement operations;
- a collection of retiring generations;
- lease accounting;
- shutdown coordination;
- generation diagnostics.

Initial API:

```python
class RuntimeManager:
    async def acquire(self) -> GenerationLease: ...
    def active_snapshot(self) -> RuntimeGeneration: ...
    async def install_initial(self, generation: RuntimeGeneration) -> None: ...
    async def begin_retirement(self, slot: _GenerationSlot) -> None: ...
    async def shutdown(self) -> None: ...
```

Do not expose a general mutable setter for active generation. Milestone C should add a narrow transactional commit method.

Lease acquisition must be race-safe with retirement. A request must either acquire the currently active accepting slot or retry against the newly active slot. It must never increment a slot after that slot is closed to new leases.

## Workstream B4 — Centralize generation construction

Extract configuration-derived startup construction from `_lifespan_runtime` into a builder:

```python
class RuntimeGenerationBuilder:
    async def build_initial(
        self,
        validation: ConfigValidationResult,
        process: ProcessRuntime,
    ) -> RuntimeGeneration:
        ...
```

Create a process-owned dependency container for stable resources:

```python
@dataclass
class ProcessRuntime:
    db: Database
    stats_db: Database
    repositories: ProcessRepositories
    metrics_store: ...
    config_path: Path
```

Builder responsibilities:

- construct outbound/network clients;
- construct provider client pool;
- load/reconcile provider and account state as currently required for startup;
- construct registry, router, quota estimator, health manager, catalog, coordinator, and other generation-owned services;
- register generation background tasks without starting them prematurely;
- clean up partially constructed resources on failure.

Use an `AsyncExitStack` or equivalent staged cleanup mechanism so failure at any construction step cannot leak clients or tasks.

Initial startup should validate configuration through milestone A's shared validation service and then call this builder. This ensures startup and future reload use the same object-construction path.

## Workstream B5 — Route request paths through generation leases

Replace direct reads of generation-owned `app.state` services in API request handlers with a runtime-manager lease.

Recommended pattern:

```python
async with request.app.state.runtime_manager.acquire() as runtime:
    return await runtime.coordinator.handle(...)
```

Streaming requires special care. If the handler returns a streaming response, the context manager cannot exit when the handler returns. Provide a wrapper around the body iterator that owns and releases the lease:

```python
async def leased_stream(iterator, lease):
    try:
        async for chunk in iterator:
            yield chunk
    finally:
        await lease.release()
```

Audit both OpenAI-compatible and Anthropic-compatible streaming paths, error paths, cancellation, client disconnects, transcoding wrappers, and finalization tasks.

The lease should cover all use of generation-owned objects, including post-stream request finalization if it still requires router/quota/health references.

## Workstream B6 — App-state compatibility layer

The dashboard, tests, and internal helpers likely read `app.state.router`, `app.state.catalog`, and similar fields directly. Avoid a large uncoordinated breakage.

Use one of these approaches:

1. Refactor consumers to retrieve the active generation through a helper.
2. Add read-only proxy objects that resolve the active generation for each operation.
3. Temporarily mirror active-generation references on `app.state` at initial install, with a tracked removal plan before milestone C.

The preferred final state is explicit generation access. Direct mutable aliases on `app.state` can create mixed-generation behavior during reload and should not survive milestone C.

Add a repository-wide audit test or static grep checklist for forbidden direct accesses to generation-owned app-state attributes.

## Workstream B7 — Generation-owned background tasks

Refactor periodic tasks whose callbacks capture configuration-derived services.

For each task, document:

- dependencies;
- whether it is process-owned or generation-owned;
- whether an in-progress tick may complete during retirement;
- whether it may be cancelled safely;
- whether duplicate overlap between old and new generations is allowed;
- cleanup/flush requirements.

Candidate generation-owned tasks include:

- catalog refresh;
- model-info refresh and canonical backfill;
- usage-window refresh;
- stale-request finalizer where thresholds/router are generation-derived;
- disabled-model pruning;
- backup scheduling where cadence/config are generation-derived;
- metrics flush if its write mode/cadence is generation-derived.

Some tasks may remain process-owned but acquire the active generation for each tick. Make that choice explicit and ensure each tick obtains one coherent lease.

During initial startup, install generation zero and start its supervisor. During process shutdown, stop new task ticks before closing generation resources.

## Workstream B8 — Deterministic retirement and shutdown

Implement idempotent generation teardown in a defined order, for example:

1. stop admitting new leases;
2. stop scheduling new periodic ticks;
3. wait for active leases to drain during normal retirement;
4. stop/cancel remaining generation tasks according to policy;
5. flush generation-owned buffered state;
6. close provider client pool;
7. close outbound manager/network backend;
8. close other generation-owned services;
9. mark retirement complete.

Process shutdown may use a bounded wait and then force cancellation/closure, because the process is exiting. Normal future reload must not force-close an active stream merely because a short timeout elapsed; instead mark retirement pending and complete asynchronously unless an operator-configured hard safety limit is introduced.

All close methods must tolerate repeated calls and partial construction.

## Workstream B9 — Diagnostics

Expose internal runtime-manager state for tests and future dashboard/API presentation:

- active generation ID;
- active digest/fingerprint;
- active lease count;
- generation creation age;
- retiring generation IDs;
- lease counts and retirement ages;
- last cleanup error per generation;
- whether shutdown is in progress.

Do not expose secrets or complete config models.

## Workstream B10 — Tests

### Runtime manager unit tests

- initial install;
- lease acquire/release;
- release idempotency;
- no acquisition after shutdown;
- race between acquisition and slot retirement;
- active/retiring diagnostics;
- teardown exactly once;
- partial builder failure cleanup;
- monotonically increasing generation IDs.

### Request lifecycle tests

- non-streaming request holds lease until response completion;
- streaming request holds lease through final yielded chunk;
- client disconnect releases lease;
- handler exception releases lease;
- transcoder exception releases lease;
- finalization/cancellation path releases lease;
- no route mixes services from different generations.

### Background task tests

- generation zero tasks start once;
- shutdown stops scheduling before client closure;
- task callbacks use coherent generation dependencies;
- in-progress task policy is honored;
- no duplicate process-owned task registration.

### Startup/shutdown regressions

- normal startup behavior and health endpoints remain correct;
- database crash recovery runs only at process startup, not generation construction;
- migrations remain process-owned and run once;
- provider/catalog/account initialization remains equivalent;
- shutdown order retains metrics flush and resource cleanup guarantees;
- strict Pyright, Ruff, and existing request/dashboard suites pass.

## Deliverables

- ownership inventory;
- process-runtime container;
- runtime generation types;
- runtime manager and lease implementation;
- centralized generation builder;
- request and streaming lease integration;
- generation-aware background task structure;
- deterministic generation teardown;
- startup through generation zero;
- runtime diagnostics;
- comprehensive concurrency and lifecycle tests.

## Acceptance criteria

- EggPool starts and serves requests through generation zero using the new runtime manager.
- Request handlers no longer directly depend on mutable generation-owned app-state objects.
- Streaming requests retain generation resources until stream completion or disconnect cleanup.
- Generation construction failure closes all partially created resources.
- Process-owned database connections and migrations are not recreated for a generation.
- Background tasks have explicit ownership and retirement behavior.
- Generation teardown closes each owned resource exactly once.
- Existing operator-visible behavior is unchanged except for intentionally improved diagnostics.
- No live reload is exposed before milestone C.
- Full test, lint, and type-check suites pass.

## Handoff notes

Milestone C should add candidate building and atomic replacement to the established manager rather than bypassing it. Do not implement reload by mutating generation zero. The first successful reload must exercise the same builder, lease, and teardown paths already validated during normal startup and shutdown.
