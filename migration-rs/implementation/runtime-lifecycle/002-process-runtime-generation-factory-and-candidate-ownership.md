# R002 — Process Runtime, Generation Factory, and Candidate Ownership

Status: closed; see [closure record](../../closure/runtime-lifecycle/002-status.md)

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: infrastructure/invariant

## Objective

Create the Rust ownership model that M8 will publish later: one process-owned runtime context, one immutable generation type, one shared startup/reload generation factory, and one explicit candidate owner with deterministic async abort. R002 does **not** activate generation swapping yet.

## Process-owned state

Introduce a small `runtime_lifecycle` module (exact filenames may vary) with a `ProcessRuntime` or equivalent that owns only services intended to survive rehash.

At minimum evaluate and place:

- `Database`;
- one `Arc<ModelRouterAffinity>`;
- one shared `WireResolver`/wire-learning state;
- the later task supervisor/reload metadata slots, initially optional/not wired;
- immutable startup metadata needed to rebuild generations, such as config path.

Do not move generation-specific provider/account/router state into this container for convenience.

### Shared state behavior

- Model-router affinity must survive rehash but continue to validate cached route/model facts against the current compiled router before reuse.
- Wire learned/rejected state may survive only through the resolver's existing structural fingerprint semantics; changed candidate structure must invalidate/suppress stale learning naturally.
- Database ownership remains process-wide; candidate abort must never close it.

## Immutable generation

Add `RuntimeGeneration` containing the complete request-visible M7 graph needed for one configuration snapshot.

Required metadata:

- monotonic generation id supplied by the future manager;
- validated `Config` snapshot;
- content digest;
- `Arc<InferenceState>` or equivalent immutable M7 entry graph;
- explicit closeable generation resources required for retirement, including the `ProviderClientPool` and access to the generation's `FinalizationSupervisor`/drain boundary;
- model-router/wire/config metadata needed by diagnostics without exposing secrets.

The generation must be immutable after construction except for bounded interior state already owned by M4-M7 modules (claims, health, affinity validation, finalization jobs, etc.).

## Refactor M7 construction into one factory

Refactor `coordinator::build_inference_state` so startup and candidate generation construction use the exact same graph builder.

Expected direction:

```text
RuntimeGenerationFactory::prepare(
    process: &ProcessRuntime,
    config: Config,
    digest: String,
    generation_id: u64,
) -> Result<PreparedGeneration, GenerationBuildError>
```

The factory should:

1. validate all generation-specific preconditions before expensive resources where possible;
2. compile model-router/wire profile structures;
3. build the generation-specific `ProviderClientPool`;
4. construct credentials/account/catalog/router/quota/health state through existing M5/M7 builders;
5. construct finite and streaming coordinators using the **same process-owned wire resolver handle** rather than separate fresh resolvers;
6. inject the process-owned affinity cache rather than constructing a new affinity per generation;
7. create the generation-scoped finalization supervisor exactly once and share it across finite/stream paths;
8. assemble `InferenceState` and explicit closeable resources;
9. return a prepared, unpublished candidate.

Do not duplicate M7 retry/finalization logic in the factory.

## Candidate ownership

Add `PreparedGeneration` (or equivalent) with an explicit monotonic ownership state:

```text
building -> prepared -> transferred
building/prepared -> aborting -> aborted
```

Required behavior:

- every async-closeable resource is registered immediately after successful construction;
- construction failure after resource N closes N..1 in reverse dependency order;
- `abort().await` is idempotent from the caller's perspective and collects typed/sanitized cleanup failures;
- transfer to the future manager removes candidate cleanup ownership exactly once;
- `Drop` may assert/log an untransferred candidate but cannot be relied on to perform async close;
- candidate cleanup never closes process-owned DB/affinity/wire resolver.

A helper may use an internal resource enum instead of arbitrary boxed async callbacks if that is simpler and more type-safe.

## Generation resource close interface

Expose a narrow explicit close surface for R004. Do not implement retirement policy yet.

The close interface should make it possible to separately:

- inspect/drain the finalization supervisor;
- close provider clients/transports;
- shut down generation-local tasks once R008 adds them;
- report sanitized errors without raw provider/auth data.

## Startup compatibility

For R002, `server::run` may continue to install one static generation directly after the factory returns. The goal is to prove the factory works without changing active publication semantics before R003.

Refactor the static serve path to use the same `RuntimeGenerationFactory` so there is no old `build_inference_state` path that reload later bypasses.

## Tests

Add focused Rust tests for:

- startup factory produces an M7 inference graph with existing C011 semantics;
- finite and streaming coordinators share the intended generation finalization supervisor and process wire resolver;
- process affinity survives building two candidate generations;
- same wire fingerprint preserves learned preference across candidate construction; changed structure does not silently reuse invalid preference;
- failure before client-pool construction has no cleanup work;
- failure after client-pool construction closes it exactly once;
- failure after later graph construction closes all candidate resources in reverse order;
- explicit abort after successful preparation closes candidate resources without touching process state;
- transfer prevents later candidate abort from closing transferred resources;
- `Debug` for process/generation/candidate types contains no API key/proxy secret/full config secret.

Reuse deterministic local provider fixtures; no live provider is needed.

## Dependency/resource posture

R002 should not add `arc-swap`; that belongs to R003. No new lifecycle/task framework is expected.

Do not add a second provider pool or wire runtime merely to expose close handles. Reuse the existing M4/M7 structures.

## Scope boundaries

R002 must not:

- implement active swap/publication or request leases;
- retire an old generation;
- compute reload diffs;
- mutate durable account/provider rows for rehash;
- start recurring background tasks;
- change signal/shutdown policy;
- implement control socket/CLI.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R002 focused tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <R001 Python runtime/generation oracle tests> -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R002 closes only when:

- one shared generation factory is used by the Rust startup path;
- process-vs-generation ownership matches R001;
- affinity and wire-learning state have the intended process lifetime;
- finite/streaming paths share one generation finalization boundary;
- all candidate failure/abort paths close candidate-owned resources exactly once;
- M7 full regression remains green;
- no reload/publication behavior has been prematurely introduced.

## Closure

Write `migration-rs/closure/runtime-lifecycle/002-status.md` and promote R003.
