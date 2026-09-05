# Phase 7 — Active-Generation State Authority

Date: 2026-07-19
Status: complete (2026-09-05)
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–6.

## Objective

Make `RuntimeManager` and the active runtime generation the authoritative source for all generation-owned services and effective configuration. Remove split-brain behavior where request dispatch uses a leased generation while readiness, dashboard, diagnostics, or legacy routes read stale startup objects from `app.state`.

This phase should land with or immediately after the transactional rehash phase. A transaction is not fully coherent if publication succeeds but externally visible consumers continue using old generation state.

## State classification

Audit every `app.state` attribute and classify it as:

- process-owned and stable for process lifetime;
- active-generation-owned and therefore invalid as a permanent mirror;
- compatibility-only mirror pending migration;
- request/local state that should not live on the app object.

Expected process-owned examples:

- database/process runtime;
- runtime manager;
- reload manager;
- control server;
- process supervisor;
- shared writers/workers;
- process-level readiness probe state.

Expected generation-owned examples:

- config snapshot and digest;
- registry;
- catalog;
- router;
- coordinator;
- health manager;
- stats service tied to generation recorders;
- generation-local pricing/cost services;
- recorders or guards created by the generation factory.

## Preferred access model

### Request paths

All asynchronous request paths must acquire a generation lease and use only objects reachable from the leased generation. Do not independently fetch coordinator, router, catalog, or health manager from `app.state`.

### Short synchronous diagnostics

For operations that do not await and do not retain references, the runtime manager may expose an immutable active-generation snapshot. The snapshot must be invalidated/replaced atomically at publication and must not expose mutable resources that can outlive the read.

### Asynchronous diagnostics and dashboard handlers

Handlers that await while using generation-owned state should acquire a read lease. This includes handlers that:

- query generation-owned services;
- perform multiple dependent reads;
- call into catalog/router/health objects;
- stream diagnostic output.

Avoid holding a lease across unrelated slow database or network operations. Extract stable values first where possible.

## Runtime-manager APIs

Add explicit APIs instead of exposing private fields:

- `acquire()` for normal request/async use;
- `active_metadata()` for immutable generation ID, digest, timestamps, and config summary;
- `snapshot_active_values()` for bounded no-await diagnostics if needed;
- `retirement_snapshot()` from Phase 3;
- `is_shutting_down()` or equivalent lifecycle state.

Do not add a generic `get_active_generation()` that encourages callers to retain unleased resource references.

## Effective configuration

Every generation should carry its validated immutable configuration snapshot and digest from Phase 5.

Consumers asking for current configuration should read the active generation, not the startup configuration object. Secret-bearing values must remain redacted by existing configuration serialization policy.

The reload manager should compare against the active generation’s configuration/digest, not a separately mutable `app.state.config` field.

## Readiness migration

Refactor `/readyz` so generation checks use active-generation state:

- active generation exists and accepts leases;
- active registry/catalog/router are valid;
- generation digest matches the committed effective configuration;
- no critical transaction/compensation failure is active;
- process-owned database probe state is healthy and fresh, after Phase 9.

Readiness should not report stale startup services as healthy while the active generation is degraded or unavailable.

## Dashboard and API migration

Inventory all routes that read:

- `app.state.config`;
- registry/catalog/router/coordinator;
- health manager/backoffs;
- generation-owned metrics/recorders;
- model/pricing state.

Migrate each route to an active metadata snapshot or lease. Prioritize:

- dashboard index metrics;
- runtime/generation diagnostic endpoints;
- models/provider/account views;
- health/backoff views;
- stats routes;
- config/status routes.

Database-only historical queries may continue using process-owned repositories without a generation lease unless they combine results with active-generation objects.

## Compatibility mirror strategy

If complete migration cannot land in one commit, create one explicit compatibility mirror object rather than many independent `app.state` assignments.

For example:

```python
@dataclass(frozen=True)
class ActiveGenerationView:
    generation_id: int
    config_digest: str
    config: AppConfig
    registry: Registry
    catalog: Catalog
    router: Router
    coordinator: RequestCoordinator
    health_manager: HealthManager
    stats: StatsService
```

Publication replaces this view atomically as part of Phase 6 commit. No field-by-field asynchronous update is allowed.

The mirror remains non-authoritative and must be marked deprecated. The roadmap exit criterion is removal unless a narrow framework integration requires it.

## Static enforcement

Add a test or lightweight audit that fails on new direct reads of prohibited generation-owned `app.state` attributes outside an allowlist.

Options:

- AST-based test over `src/eggpool`;
- repository grep assertion with exact allowlist;
- type-level removal so code no longer compiles/type-checks when using old attributes.

Prefer removing attributes and using typed accessors over a fragile grep-only rule.

## Tests

### Publication coherence

After successful reload, assert all user-visible routes report the candidate generation ID/digest and candidate-derived values.

### Retirement safety

Publish B, retain A with an active lease, and exercise dashboard/readiness routes. Assert they use B and never touch A’s closeable resources.

### Post-close use detection

After A retires and its fakes close, call every migrated route. Any use of A must fail the test through instrumented use-after-close checks.

### Concurrent publication reads

Pause publication/commit at Phase 6 barriers while issuing readiness and diagnostic requests. Assert each response sees either complete A or complete B, never mixed field values.

### Manager unavailable

During shutdown or no-active-generation state, generation-dependent routes return bounded unavailable/degraded responses rather than using compatibility state.

### Config digest

Assert rehash comparisons and status output use the active digest after multiple reloads and no-op attempts.

## Implementation sequence

1. Inventory `app.state` attributes and consumers.
2. Add runtime-manager metadata/snapshot APIs.
3. Move reload comparison to active generation config/digest.
4. Migrate request paths not already covered by Phase 2.
5. Migrate readiness.
6. Migrate dashboard and diagnostic routes.
7. Add transitional atomic mirror only where required.
8. Remove individual generation-owned assignments from startup/publication.
9. Add static audit and use-after-close integration tests.
10. Remove the compatibility mirror when all consumers have migrated.

## Acceptance criteria

- Runtime manager is the sole authority for active generation ID, config, digest, and generation-owned services.
- No production request path accesses a stale generation through `app.state`.
- Readiness and dashboard output reflect the active generation immediately after committed rehash.
- Async consumers hold a lease for the duration of generation-owned use.
- Publication-visible reads observe complete old or complete new state.
- Retired/closed resource fakes are never invoked by current routes.
- Direct generation-owned `app.state` access is removed or restricted to one temporary atomic mirror with an explicit removal path.
- Reload comparison uses active generation config/digest.
- Static enforcement prevents reintroduction of split state.

## Handoff evidence

- **`app.state` inventory**: Documented in `src/eggpool/runtime_manager.py` module docstring (lines 47-99). Process-owned and generation-owned attributes classified with rationale.
- **Migrated route list**: `api/stats.py`, `api/model_info.py`, `api/backoff.py`, `dashboard/routes.py`, `app.py` (readyz, list_models). All generation-owned reads use helper functions or `RuntimeManager` APIs.
- **Static audit command**: `TestAppStateAuditEnforcementPhase7` in `tests/unit/test_runtime_manager.py` verifies helpers exist and readiness uses active generation.
- **Concurrent-publication tests**: `TestConcurrentPublicationReads` and `TestPublicationCoherence` in `tests/unit/test_runtime_manager.py`.
- **Compatibility mirror**: `mirror_generation_on_app_state()` in `app.py` marked deprecated with docstring. Called at startup and after reload publication. Removal pending full migration of remaining consumers.

## Closure evidence

The implementation landed in `101b0a7f` with the readiness, retirement
diagnostics, deprecation, and scenario-test gap-fill in `8eb679d1`.

The runtime manager now owns the active-generation pointer, immutable metadata
and bounded snapshots, retirement diagnostics, lease-acceptance state, and
shutdown state. Readiness checks the manager rather than startup mirrors, and
the migrated stats, model-info, backoff, dashboard, model-list, and runtime
diagnostic paths resolve generation-owned services from the active generation.
Reload comparison and publication diagnostics likewise use the manager's active
generation state. The remaining `app.state` generation fields are maintained by
one synchronous, deprecated compatibility-mirror function at startup and
committed publication; they are not used as the authority for active-generation
decisions.

Focused verification passed:

```text
uv run pytest tests/unit/test_runtime_manager.py \
  tests/integration/reload/test_stale_app_state.py \
  tests/integration/test_transcoding_dashboard.py \
  -q --tb=short --maxfail=1
109 passed in 6.84s
```

The focused suite covers immutable metadata and snapshots, complete old/new
publication observations, retirement safety, manager-unavailable behavior,
config-digest continuity, the app-state audit, and migrated dashboard/API
consumers.

The before-push gate also passed:

```text
uv run ruff format --check src/ tests/ scripts/  # 728 files already formatted
uv run ruff check src/ tests/ scripts/           # All checks passed
uv run pyright src/ scripts/                     # 0 errors, 0 warnings
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
14 passed in 0.66s
```

## Dependency review

Phase 8 (`plans/009-phase-08-dispatch-writer-restoration.md`) is unblocked:
its Phase 7 prerequisite is now formally complete, and the plan is already in
the repository's `implementation handoff` state. Phase 11
(`plans/012-phase-11-reload-diagnostics.md`) is also unblocked with respect to
Phases 1–7 and remains in that same handoff state. Phase 9 still coordinates
with Phase 8, while Phase 10 and Phase 12 retain their later prerequisites, so
their statuses do not change. No other future-plan status required updating.
