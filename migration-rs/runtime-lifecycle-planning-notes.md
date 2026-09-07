# M8 Runtime Lifecycle Planning Notes

Status: planning reference

Baseline audited: `e2be716018c365030ab06e648af71ed7588d9ad3`.

These notes capture the source review that produced the M8 plans. They are not an implementation plan and do not override the subsystem roadmap or registry.

## Python behavioral authorities

### `src/eggpool/runtime_manager.py`

The Python runtime already separates process-owned and generation-owned state and implements a real publication protocol rather than replacing a global object reference.

Important behavior to preserve:

- immutable `RuntimeGeneration` snapshots;
- explicit candidate ownership (`building -> prepared -> transferred` or `-> aborted`) with reverse-order async cleanup;
- active/retiring/closing/closed generation lifecycle;
- request leases and separate retained-terminal ownership;
- a lease-admission gate around publication;
- one pending generation swap with stage/commit/rollback/finalize semantics;
- old-generation retirement only after accepted work drains;
- deterministic generation close order and bounded shutdown;
- secret-free generation/reload diagnostics.

Rust does not need the same class count. It does need the same ownership transitions.

### `src/eggpool/generation_factory.py`

Python uses one generation factory for startup and live reload. Candidate construction includes the provider pool, account/catalog/router state, health/quota state, M7 coordinator, finalization supervisor, model-router registry, and generation task registration. Startup-only DB migration/crash recovery remains outside the factory.

Rust currently builds most of this graph inside `coordinator::build_inference_state`. M8 should refactor that function into one generation factory instead of adding a separate reload-only constructor.

Python also intentionally keeps the wire-profile resolver and model-router affinity process-owned so compatible learned/sticky state survives generation swaps. Rust currently creates fresh wire resolvers for finite and streaming coordinators and a fresh affinity cache for each `build_inference_state`; M8 is the correct milestone to move those to process ownership while retaining M7 semantics.

### `src/eggpool/config_reload_policy.py`

Every config field has an explicit disposition: `LIVE`, `RESTART_REQUIRED`, or `IGNORED`; unknown fields default fail-closed to restart-required. Secret-bearing changes render as `<changed>` rather than exposing old/new values. Mixed live and restart-required changes are rejected rather than partially applied.

The Python table is the oracle for R005. Rust should not infer reloadability from which fields happen to be easy to rebuild.

### `src/eggpool/reload_transaction.py`

Reload is a monotonic transaction, not “parse file then swap.” Candidate preparation, persistence delta, process transitions, commit acceptance, observable state, retirement, abort, and compensation are distinct phases. Cancellation before the commit point aborts cleanly; cancellation after acceptance cannot strand a half-committed state.

Rust can simplify the transaction because its active pointer and task-spec commit can be smaller, but it must retain:

- one serialized reload owner;
- expected-generation revalidation;
- candidate cleanup before acceptance;
- a narrow admission gate during cross-resource acceptance;
- typed rollback/compensation on failure;
- no network work in the commit window.

### `src/eggpool/runtime_task_inventory.py`

Python has an authoritative task inventory with process and generation-leased ownership. The important invariant is not the exact class hierarchy; it is that process tasks exist once and generation-dependent work cannot continue against stale services after publication.

The inventory names currently include `catalog_refresh`, `retention_cleanup`, `checkpoint`, `metrics_flush`, `update_checker`, and `automatic_backup`. Some underlying Rust business capabilities remain future operational work. M8 should build one scheduler and make any deferred callback explicit rather than create placeholders or let M9 invent another supervisor.

### `src/eggpool/cli_rehash_helper.py`

The current Python user flow validates locally, sends `reload_config` to the running server over the control socket, reports generation/changed sections/retirement status, and distinguishes validation, restart-required, and control-unavailable outcomes.

M8 owns the server-side typed reload service/result. M9 owns the Rust CLI/control transport that invokes it.

## Current Rust state before M8

### `rust/src/runtime.rs`

`serve --verbose` calls `server::run` directly. The module has no runtime generation manager, reload service, task supervisor, or coordinated shutdown owner.

### `rust/src/server.rs`

`AppState` currently captures:

- startup `Config`;
- process `Database`;
- startup `ProviderClientPool`;
- one static `Arc<InferenceState>`.

The router installs `RequestBodyLimitLayer` from startup config. Auth, readiness, dashboard rendering, and inference handlers read startup state directly. This is correct for the pre-M8 static server but becomes a stale-generation hazard as soon as providers/accounts/model routers/body limits become live-reloadable.

M8 must convert `AppState` into process state plus generation acquisition. Constructor-owned restart-required state may remain static.

### `rust/src/coordinator/endpoints.rs`

`InferenceState` is already a useful immutable generation boundary, but `build_inference_state` currently constructs fresh:

- account/catalog/router graph;
- provider coordinators;
- finalization supervisor;
- two separate wire resolvers;
- model-router registry;
- `ModelRouterAffinity`.

R002 should preserve `InferenceState` as the M7 service graph while injecting process-owned affinity/wire resolver handles and returning explicit closeable generation resources.

### `rust/Cargo.toml`

The existing stack is sufficient except for atomic Arc publication. `arc-swap` is justified for M8. Do not add `tokio-util`, an async lifecycle framework, actors, a workflow engine, or an alternate runtime solely for reload.

## Recommended Rust shape

```text
ProcessRuntime
  database
  affinity
  wire_resolver
  task_supervisor
  runtime_manager
  reload metadata
      |
      +--> PreparedGeneration --abort--> closed
      |         |
      |       transfer
      v         v
RuntimeManager --ArcSwap--> GenerationSlot --> Arc<RuntimeGeneration>
                            lease count          Config/digest/id
                            retirement state     InferenceState
                            finalization drain   ProviderClientPool
```

The manager may use `ArcSwap` for the active pointer plus a very small synchronization section around lease claim/publication. Lock-free purity is not a goal; linearizable correctness with negligible hot-path work is.

A practical acquisition algorithm is:

1. observe the publication-gate state;
2. enter a tiny synchronous publication read/claim section;
3. re-check the gate;
4. `load_full()` the active slot and increment its lease count before leaving the section;
5. return `GenerationLease`;
6. release decrements the process-local lease counter and notifies retirement.

Publication uses the matching exclusive section to mark the old slot non-accepting and replace the `ArcSwap` pointer. The short gate may remain closed across the bounded DB/runtime/task acceptance transaction, but not across config parsing or candidate network preparation.

## Important simplifications relative to Python

Rust should not mechanically port:

- a large compatibility mirror of every generation service onto server state;
- a general reversible-process-transition framework when a staged `PreparedTaskDiff` covers the current real transition;
- generation-owned scheduler loops that strongly retain their generation and create retirement cycles;
- async cleanup through destructors;
- process-local references to generation services outside explicit leases.

A single process scheduler whose generation-dependent callbacks acquire the active generation each tick is simpler than Python's split supervisors and preserves the no-stale-generation behavior.

## Qualification traps

The following tests are high-value because they catch designs that look correct in sequential tests:

- lease acquisition racing publication at the exact commit point;
- streaming body task outliving the Axum handler across a rehash;
- finalization job still active after the request lease drops;
- invalid candidate after opening provider resources;
- reload cancellation while the admission gate is closed;
- DB commit failure after staged pointer replacement but before admission reopens;
- process task spec change failing preflight;
- repeated rehash while an older generation is stalled by a long stream;
- shutdown racing a rehash and a retained finalizer;
- a new request after rehash seeing old `max_request_body_bytes`, providers/accounts, model-router registry, or readiness state;
- affinity/wire learned state surviving only when valid under the new generation;
- diagnostics accidentally serializing config secrets.
