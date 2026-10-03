# Runtime Efficiency Milestone 002 — Catalog Refresh Projection and Lock-Tenure Cleanup

Status: implemented

Repository baseline: `05a5ecaa` (M002 activated after M001 closure; original interface review baseline `6489aa75aa276a12ff14c35a07b46f2eba01e2b8`)

Source roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-002--catalog-refresh-projection-and-lock-tenure-cleanup`

Long-term requirements:

- `plans/000-long-term-specification.md#5-performance-posture`
- `plans/002-long-term-roadmap.md#cross-phase-execution-rules`
- `plans/003-planning-process.md`

Applicable ADRs:

- None required. The catalog owner, schema, refresh cadence, and routing
  semantics remain unchanged.

Primary class: polish

## 1. Objective

Reduce periodic catalog-refresh heap churn and routing-visible mutex tenure by
replacing whole-cache persistence/diff snapshots with narrow owned projections,
while preserving exact catalog refresh, withdrawal, persistence, failure
retention, and runtime-generation behavior.

## 2. Why this milestone is ready

No hard dependency is open. Catalog refresh already has a clear owner in
`CatalogService`, provider fetches are gathered asynchronously before cache
mutation, routing reads use the shared in-memory `ModelCatalogCache`, and
schema-54 persistence is isolated behind `CatalogRepository`.

The intended optimization is internal projection/ownership cleanup. It does not
require a migration, new task, changed refresh cadence, or public contract.

## 3. Current implementation evidence

At baseline:

- `CatalogService` owns
  `Arc<std::sync::Mutex<ModelCatalogCache>>`, async service state, and a
  refresh lock.
- `refresh_locked` obtains a complete `cache_snapshot()` before refresh,
  applies fetched results, calls `persist().await`, obtains another complete
  snapshot, and then uses only model IDs and provider/model keys to compute
  `new_model_ids`, `withdrawn_model_ids`, and `changed_provider_keys`.
- `cache_snapshot()` deep-clones model IDs, provider keys, account support,
  account providers, freshness, outcomes, and account/provider keys.
- `persist()` locks service state and the routing-visible catalog, clones the
  complete `ModelCatalogCache`, clones pending refresh/ping state, releases
  the locks, reads existing durable catalog rows, and then
  `desired_rows(&cache, ...)` materializes another set of owned persistence
  rows.
- `ModelCatalogCache` itself includes an optional cloned `Config`, so a whole
  cache clone copies configuration state unrelated to persistence output.
- The repository already keeps semantic comparison outside the SQLite
  transaction; this milestone must retain that useful boundary.

This is periodic work rather than the dominant request hot path, but providers
with large model catalogs can make the cloning deterministic and substantial,
and routing shares the catalog mutex during the projection step.

## 4. Invariants that must not regress

- Provider model discovery and account fetch concurrency are unchanged.
- One refresh is serialized by the existing `refresh_lock`.
- Cache mutation remains atomic under the existing catalog mutex.
- No catalog/service mutex is held across provider or SQLite awaits.
- Authoritative vs non-authoritative refresh semantics are unchanged.
- Withdrawal policy and `prune_unused` behavior are unchanged.
- Refresh outcomes, observations, model events, freshness, and failure
  classification are unchanged.
- Persistence failure must leave pending refresh/ping information available for
  the same retry/recovery behavior; do not clear it optimistically.
- Durable `models`, `provider_model_metadata`, `account_models`,
  `catalog_refresh_state`, and `provider_pings` contents remain equivalent.
- Schema version remains 54; no migration.
- Routing sees the same cache state before/after each completed refresh.
- No credential or model-response body enters new diagnostics.

## 5. Scope

### In scope

- Introduce narrow internal projections for:
  - persistence rows/facts;
  - refresh-result model-ID/provider-key diffing.
- Construct owned persistence projection data while holding the catalog mutex,
  then release the mutex before all SQLite reads/writes.
- Avoid cloning the complete `ModelCatalogCache` solely to cross the async
  persistence boundary.
- Avoid complete `CacheSnapshot` construction when only model IDs and
  provider/model keys are required.
- Reduce duplicate serialization/materialization of capabilities/source
  metadata when a single owned persistence row can carry the result safely.
- Preserve failure-retry state ownership.
- Add large synthetic catalog tests that exercise the projection without using
  real providers.

### Explicitly out of scope

- Catalog refresh interval/default changes.
- Provider discovery HTTP behavior or transport concurrency.
- Cache data-structure replacement (`BTreeMap` -> hash/arc-swap/etc.) unless a
  tiny internal change is unavoidable for projection.
- Routing eligibility/selection changes.
- Schema/index/migration changes.
- Moving catalog refresh to another thread/runtime.
- Persisting less semantic information.
- Changing public `CacheSnapshot` behavior; it remains available for
  diagnostics/tests that actually need it.

## 6. Required production changes

### Narrow diff projection

Add a crate-private projection containing only the model IDs and provider/model
keys used by refresh result diffing. Capture it directly from the cache before
and after mutation instead of calling the complete diagnostics
`CacheSnapshot`.

Keep deterministic ordering equivalent to the current BTree-derived output.

### Persistence projection

Add a crate-private owned persistence projection/batch builder that can be
constructed directly from `&ModelCatalogCache` plus account IDs/pending
refresh state while holding the short synchronous lock.

The projection should contain the exact owned rows needed by
`CatalogRepository::apply_persistence_batch`; after construction, the catalog
lock must be released before any database await.

Prefer one materialization of canonical capability/source-metadata data. Do not
clone the whole cache and then walk the clone to clone row fields again.

### Pending refresh/ping failure semantics

Current `persist()` clears pending refresh/ping state only after the
repository call succeeds. Preserve this.

An implementation may retain a snapshot/copy of pending state for the database
operation, but must not lose concurrently relevant state or clear unpersisted
facts on failure. Because refreshes are already serialized, exploit that
existing invariant rather than adding another queue/lock.

### Existing durable-row comparison

Keep semantic comparison outside the SQLite transaction unless evidence proves
a smaller equivalent repository shape. Do not increase DB-gate tenure by
moving CPU/serialization into `with_transaction`.

## 7. Ordered work packages

### Work package A — Projection parity fixtures

Intent:

Lock the existing cache-to-durable and refresh-diff semantics.

Required changes:

- build deterministic synthetic catalogs covering multiple providers/accounts,
  capabilities, source metadata, withdrawals, disabled support, empty success,
  failed refresh, and static models;
- compare the new narrow projections to current complete-snapshot/desired-row
  results before deleting the old internal use;
- preserve public `CacheSnapshot` tests.

Acceptance evidence:

- exact ordered model/provider-key diff results;
- exact durable persistence batch semantics.

### Work package B — Replace whole-cache refresh diff snapshots

Intent:

Remove diagnostics-sized snapshots from periodic control flow.

Required changes:

- capture only model IDs/provider keys before and after refresh;
- compute the same set differences and output vectors;
- maintain deterministic ordering.

Acceptance evidence:

- `CatalogRefreshResult` equality across the fixture matrix.

### Work package C — Replace whole-cache persistence clone

Intent:

Own only data that must survive the SQLite await.

Required changes:

- construct the persistence projection under the catalog lock;
- release all catalog/service synchronous locks before repository awaits;
- feed the existing repository batch interface or a narrower internal
  equivalent;
- do not clone unrelated `Config`, freshness/outcome maps, or diagnostics
  projections merely for persistence.

Acceptance evidence:

- durable database rows match current expected fixtures;
- a test seam demonstrates the catalog lock can be acquired while the database
  persistence await is blocked/in progress.

### Work package D — Failure and retry retention

Intent:

Prove optimization does not turn persistence failure into lost refresh state.

Required changes:

- inject/force repository persistence failure through existing test seams;
- verify pending refresh/ping facts remain available;
- verify a subsequent successful persistence converges once without duplicate
  semantic damage.

Acceptance evidence:

- failure then retry produces the same final durable/catalog state as one
  successful refresh.

### Work package E — Large synthetic catalog characterization

Intent:

Provide structural evidence without a permanent benchmark dependency.

Required changes:

- exercise a representative large synthetic catalog (for example hundreds to
  low thousands of model/provider rows) using existing test/tooling patterns;
- record the number of full-cache/diagnostic snapshots removed and, when
  feasible, descriptive projection time/heap-sensitive evidence using standard
  tools only;
- do not add Criterion/allocator dependencies.

Acceptance evidence:

- closure can make a structural allocation/lock-tenure claim without claiming
  a target-class latency SLA.

## 8. Failure, cancellation, restart, contention semantics

A cancelled/failed provider fetch follows existing classification and must not
partially mutate persistence semantics.

Once cache mutation has occurred, a database failure must retain pending durable
facts exactly as today. This plan does not invent transactional coupling between
the in-memory cache and SQLite.

Refresh serialization remains under `refresh_lock`; no second refresh owner is
introduced.

Routing must never wait on catalog mutex while SQLite I/O is in progress.
Tests should explicitly enforce this ownership boundary if a deterministic DB
pause/fault seam exists.

Startup hydration's one-time clone-before-await pattern is not the primary
target. It may be cleaned only if the same no-lock-across-await and failure
atomicity can be preserved without widening scope.

## 9. Compatibility and migration

No schema migration, data rewrite, HTTP/CLI/config change, or public Rust API
removal.

`CacheSnapshot` remains a public/diagnostic compatibility surface.

Durable row values and deletion/withdrawal behavior must be byte/semantically
equivalent according to existing canonical JSON rules.

## 10. Required tests

Focused:

```bash
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
```

Add coverage for:

- old/new projection parity;
- large multi-provider catalogs;
- authoritative withdrawal and preserve-until-health cases;
- persistence failure then retry;
- ping/refresh-state retention;
- no catalog-lock ownership during database await;
- deterministic refresh-result ordering.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

No Cargo change is expected.

## 12. Documentation updates

Update:

- `architecture/overview.md` catalog ownership wording if necessary;
- the catalog/models deep dive that documents refresh/cache persistence;
- `plans/subsystems/runtime-efficiency-roadmap.md` for lifecycle/closure only.

Do not rewrite historical performance plans.

## 13. Acceptance criteria

- Periodic refresh persistence no longer deep-clones the complete
  `ModelCatalogCache` solely for async ownership.
- Refresh change reporting no longer builds complete before/after
  `CacheSnapshot` values solely to obtain model/provider keys.
- No routing-visible catalog mutex is held across SQLite/provider awaits.
- Persistence CPU/serialization remains outside the DB transaction/gate where
  possible.
- Failure retains pending refresh/ping facts; retry converges.
- Durable schema-54 rows and `CatalogRefreshResult` semantics are unchanged.
- Public cache snapshot/API behavior is unchanged.
- Full default/no-default qualification is green.

## 14. Stop conditions

Stop and report if:

- exact durable-row parity requires holding the catalog mutex across SQLite I/O;
- failure-safe pending-state ownership would require a new queue/task;
- the cleanup requires a schema/config/public API change;
- large-catalog evidence instead shows the current clone is negligible and the
  refactor becomes disproportionately complex;
- optimization expands into provider transport or routing policy.

## 15. Closure evidence required

Record:

- implementation commit(s);
- old/new projection ownership diagram or concise table;
- exact durable-row + refresh-result parity evidence;
- failure/retry retention evidence;
- no-lock-across-await contention test;
- large synthetic catalog characterization;
- focused + full default/no-default results;
- strict fmt/Clippy + locked release build;
- Cargo delta (expected zero);
- limitations and any one-time hydration clone left intentionally unchanged.

## 16. Handoff notes

The core rule is: project under the lock, then await with owned narrow data.
Do not make the catalog itself async merely to optimize cloning.
