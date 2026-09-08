# M8 Runtime Generations, Rehash, Background Tasks, and Process Lifecycle Roadmap

Status: active implementation; R008 dependency-ready

Repository baseline for M8 planning: `e2be716018c365030ab06e648af71ed7588d9ad3` (accepted C011 / M7 closure).

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, accepted ADR-0001 through ADR-0003, the closed M4-M7 roadmaps, and the accepted C011 M7 closure.

## Purpose

M8 replaces Python/Granian generation and process-lifecycle machinery with a smaller Rust-native runtime without weakening the behavior that makes live rehash safe. M8 owns immutable generation snapshots, linearizable request leases, atomic generation publication, candidate abort/retirement, live config classification and rehash, process/background task ownership, startup recovery scheduling, graceful/forced shutdown, and runtime diagnostics.

The Python runtime is a behavioral oracle, not a structural template. Rust should preserve the observable lifecycle and safety invariants while using a compact set of Rust primitives (`Arc`, `ArcSwap`, Tokio tasks/signals, SQLite transactions, and narrow synchronization) rather than porting Python's large manager/reload modules line-for-line.

## Ownership boundary

### Process-owned

The process lifetime owns resources that must survive generation swaps:

- the primary SQLite `Database` and repositories over it;
- the active-generation `RuntimeManager` and reload serialization state;
- `ModelRouterAffinity`, so valid sticky decisions can survive a rehash and be revalidated against the new compiled router;
- one shared `WireResolver`, so learned/rejected wire state can survive when candidate fingerprints remain compatible;
- the process task supervisor and task-spec registry;
- the C010 crash reconciler entry point and startup recovery report;
- reload diagnostics/last-result metadata;
- listener/server constructor state whose config is explicitly `RESTART_REQUIRED`;
- future M9 control/CLI adapters, which are not implemented by M8.

### Generation-owned

Each immutable generation owns the configuration-dependent service graph used by requests:

- validated `Config` snapshot plus content digest and monotonic generation id;
- the M7 `InferenceState` and its finite/streaming coordinators;
- provider/account client pool and generation-scoped transport configuration;
- account/catalog/routing/quota/health state embedded in the M5/M7 graph;
- compiled model-router registry and other immutable routing facts;
- generation-scoped finalization supervisor and any generation-local diagnostics needed by M7;
- closeable resources constructed from live configuration.

A generation may not mutate another generation's service graph. Process-owned caches may be shared only when the cache itself revalidates entries against generation-specific structure/fingerprints.

## M8 invariants

1. **One generation per request lifetime.** A finite request uses one generation from admission through terminal registration. A stream holds the same generation until stream completion/disconnect and retained terminal ownership has been registered.
2. **Linearizable acquisition/publication.** No lease can be admitted to the old generation after the publication commit point, and no request may observe a half-published candidate.
3. **Immutable request authority.** Await-capable handlers do not read mutable startup copies of live config/services after acquiring a generation.
4. **Candidate isolation.** Candidate construction may allocate candidate-owned resources but cannot change the active runtime, process task schedule, or durable config-derived rows before the commit phase.
5. **Abort closes everything it owns.** A failed/cancelled candidate is closed exactly once in deterministic reverse dependency order. Async cleanup is explicit; `Drop` is not the primary cleanup mechanism.
6. **Rehash is fail-closed.** Unknown config fields default to restart-required. Invalid config, mixed live/restart-required diffs, digest mismatch, retirement backlog, or candidate/preflight failure leave the active generation unchanged.
7. **No old/new mixing.** A request never combines the old router/coordinator with the new provider pool/config/body limit or vice versa.
8. **Retirement preserves accepted work.** Live rehash never force-closes a generation with active leases or retained M7 finalization work merely to finish retirement faster.
9. **Retirement is bounded as manager state.** Completed retirement tasks/slots are reaped; an unresolved retirement backlog has an explicit small cap and blocks additional rehash rather than growing without bound.
10. **M7 terminal work drains before transport close.** Retirement waits for request leases, then retained finalization convergence, before closing provider transport/resources.
11. **Process tasks are singleton and non-overlapping.** A process-owned recurring task cannot duplicate across generations. Generation-dependent ticks acquire the active generation for that tick and never retain stale service references between ticks.
12. **Reload transaction is serialized.** At most one live rehash transaction owns candidate/publication state at a time.
13. **The publication gate is narrow.** Config parsing, validation, diffing, candidate construction, persistence-delta preparation, and task-diff preflight occur before request admission is gated.
14. **Restart-required fields never partially apply.** Host/port/API-auth/listener/DB-constructor and other frozen fields require restart; M8 does not silently apply a subset of a mixed diff.
15. **Shutdown is monotonic.** Once shutdown starts, no new generation publication is accepted. Existing accepted requests/finalization work get a bounded graceful drain before any forced close.
16. **Crash recovery never replays provider work.** M8 schedules C010 reconciliation at startup; it does not reinterpret or expand C010's semantics.
17. **Diagnostics are secret-free.** Generation IDs, digest prefixes, counts, durations, states, and task names are allowed; API keys, proxy credentials, raw request/provider bodies, session values, and full secret-bearing config are not.
18. **No Rust-only schema fork.** Rehash persistence uses the existing SQLite schema and remains readable by the Python reference.

## Implementation sequence

```text
M7 C011 closed
   |
   v
R001 runtime/reload contract + deterministic oracle freeze
 -> R002 process runtime + shared generation factory + candidate ownership
 -> R003 active generation manager + ArcSwap publication + request/stream leases
 -> R004 retirement + retained-finalization drain + resource close
 -> R005 config diff/reload policy + redacted change model
 -> R006 process task supervisor + authoritative task-spec staging
 -> R007 transactional live rehash + persistence/task/runtime commit (closed)
 -> R008 generation-leased maintenance/recovery/background integration (ready)
 -> R009 server startup, signals, graceful/forced shutdown
 -> R010 active-generation authority audit + runtime/reload diagnostics
 -> R011 integrated differential qualification + M8 closure
   |
   v
M9 planning/implementation eligibility
```

Only the dependency-ready table in `../registry.md` authorizes implementation. R008 is the sole ready plan after accepted R007 closure.

## Structural design

Rust should converge on a small runtime surface, names adjustable but responsibilities fixed:

- `ProcessRuntime`: process-owned database/shared caches/task supervisor/reload metadata.
- `RuntimeGeneration`: immutable generation snapshot and closeable generation resources.
- `PreparedGeneration`: candidate owner that can `abort().await` or transfer ownership.
- `GenerationSlot`: lifecycle/lease/retirement metadata around one `Arc<RuntimeGeneration>`.
- `GenerationLease`: explicit request/stream lease; local counter release may use `Drop` because it is synchronous process-local ownership, unlike durable finalization.
- `RuntimeManager`: `ArcSwap` active pointer plus a narrow publication lock/gate, retiring-slot table, and shutdown state.
- `ReloadPolicy`: exhaustive fail-closed config diff classification.
- `RuntimeTaskSupervisor`: one process-owned scheduler with staged spec diffs.
- `ReloadService`: serialized validate/diff/build/preflight/stage/commit/retire transaction.

`arc-swap` is the one expected new runtime dependency. Do not add a lifecycle framework, actor system, async trait framework, DI container, job queue, ORM, or second web/client stack.

## Active-generation authority conversion

Current Rust `AppState` captures startup `Config`, `ProviderClientPool`, and one `Arc<InferenceState>`. M8 must remove those as live authorities. In particular:

- inference handlers acquire one generation and hold it through finite completion or the spawned streaming body task;
- readiness/model/routing/account-dependent reads use the active generation;
- live `server.max_request_body_bytes` is enforced using the acquired generation before unbounded body buffering, not only by a startup `RequestBodyLimitLayer`;
- constructor-owned auth/listener/dashboard-route topology remains startup-owned only where the reload policy marks it restart-required;
- process-level DB/dashboard rollup reads may use process-owned repositories but must acquire a generation when their rendering/selection behavior depends on live config.

A direct `Arc<InferenceState>` escape from `RuntimeManager` into long-lived Axum state is a closure blocker.

## Live rehash transaction

The intended Rust transaction is smaller than Python's implementation but keeps the important boundaries:

1. serialize rehash and snapshot expected active id/digest;
2. read/validate candidate config and verify optional expected digest;
3. compute redacted typed diff; no-op when identical; reject any restart-required change;
4. build candidate and prepare durable/task deltas without changing active state;
5. preflight staged process-task changes;
6. close the short lease-admission gate and stage the candidate against the expected active generation;
7. in one bounded acceptance window, apply the existing-schema config-derived persistence delta, commit the staged runtime pointer, and commit the staged task-spec state; rollback/compensate while the gate is still closed if any mandatory step fails;
8. reopen admission only after the accepted state is coherent;
9. transfer candidate ownership and schedule old-generation retirement;
10. publish a secret-free structured result.

No provider/network call, catalog refresh, backup, or other unbounded work belongs inside the publication gate.

## Background ownership

M8 builds the scheduler/runtime ownership needed for Python's inventory without dragging M9 CLI surfaces forward.

- Process-owned task loops exist once per process and are reconfigured transactionally from task specs.
- Generation-dependent callbacks acquire the active generation for each tick and release it before sleeping.
- Startup crash reconciliation is a one-shot process lifecycle action, not a recurring provider replay mechanism.
- A task whose underlying business capability is intentionally owned by M9 may remain unregistered until that capability exists, but its inventory/spec semantics must be explicit so M9 plugs into one scheduler instead of creating a second one. M8 closure must identify any such deferred callbacks; it may not silently run placeholders.

## M8/M9 boundary

M8 owns the server-side/runtime capability for live reload and shutdown. M9 owns the user-facing operational CLI/control transport and packaging around it.

M8 therefore exposes typed APIs such as `ReloadService::reload(...)`, runtime snapshots, shutdown handles, and task-supervisor diagnostics. It does **not** implement `eggpool rehash`, daemon control sockets, `stop/restart`, install/systemd/croncheck, backup/recover CLI, update CLI, or packaging.

## Verification posture

Every handoff uses deterministic local fixtures. No paid/live provider is a normal closure prerequisite. The primary qualification is Python-oracle fixtures plus Rust concurrency/fault tests over the actual Axum/M7 runtime.

Do not create a broad OS/architecture CI matrix in M8. M10 owns release/SBC characterization.

## M8 closure

R011 may close M8 only when:

- startup and reload build the same generation-owned graph;
- candidate failure/cancellation cannot change active state or leak resources;
- acquisition/publication is linearizable under concurrency;
- finite and streaming requests survive rehash on their original generation while new requests use the new generation;
- no live handler reads stale startup generation state;
- all Rust config fields are classified fail-closed and secret changes are redacted;
- live and restart-required/no-op/invalid/mixed reload cases match the accepted oracle;
- task specs are singleton, non-overlapping, reload-safe, and generation-dependent callbacks never use a retired generation;
- old generations retire only after leases and retained finalization converge;
- startup C010 recovery, graceful shutdown, forced shutdown, and reload/shutdown races converge without DB reset or provider replay;
- runtime/reload diagnostics remain bounded and secret-free;
- no unresolved high/medium M8 correctness/security issue remains;
- M9 receives explicit stable reload/lifecycle/task interfaces and no M9 implementation is promoted automatically.
