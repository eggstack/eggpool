# Runtime Efficiency Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/runtime-efficiency/002-catalog-refresh-projection-lock-tenure-cleanup.md`

Source subsystem roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-002--catalog-refresh-projection-and-lock-tenure-cleanup`

Repository baseline reviewed: `05a5ecaa` (M002 activation; original interface baseline `6489aa75aa276a12ff14c35a07b46f2eba01e2b8`)

Implementation commits:

- `93f0a157` — project catalog refresh rows under the cache lock
- `5adf6d3f` — begin M002 closure and reconcile status

## 1. Executive finding

M002 is complete. Refresh result diffing now captures only ordered model IDs and provider/model keys. Persistence rows are projected directly from cache references into owned database rows, without cloning the complete `ModelCatalogCache`. Service-state pending facts are copied and both service-state and catalog mutex guards are dropped before SQLite access. Forced persistence failure followed by a provider failure and successful persistence proves the original successful refresh and ping facts remain available for retry. Schema, durable row semantics, and public `CacheSnapshot` remain unchanged.

No deterministic DB-pause test seam existed. The lock boundary is enforced structurally by keeping the synchronous projection block separate from all repository awaits and was reviewed in `persist` and `ensure_hydrated`; failure/retry is covered by an injected SQLite trigger test.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Narrow refresh-result projections | `ModelCatalogCache::diff_projection`; large fixture equality against the model/key portion of `CacheSnapshot`; all catalog refresh tests | pass | Ordered BTree-derived model IDs and provider/model keys preserved. |
| Direct owned persistence projection | `ModelCatalogCache::persistence_projection`; 600 model / 1,200 provider-row fixture; catalog/database suites | pass | Rows are built directly from references; no whole-cache clone or diagnostics snapshot. |
| Exact durable row semantics | Five `catalog_refresh` tests, including normalized persistence, disabled/static, malformed/empty, withdrawal, and failure/retry; `database_compatibility` and `routing_domain` suites | pass | Schema-54 rows and public snapshot behavior unchanged. |
| Release locks before DB await | Source review of `persist` and `ensure_hydrated`; projection returns owned `CatalogPersistenceBatch`; no guard is in scope at repository await | pass | No deterministic pause seam exists for a direct contention test. `deep-dive-catalog.md` records the ownership sequence. |
| Retain pending refresh/ping on failure | `persistence_failure_retains_refresh_and_ping_facts_for_retry` installs a `BEFORE INSERT` abort trigger, removes it, retries with provider failure, and verifies prior success state plus both pings persisted | pass | Demonstrates retry uses retained facts and does not require a second successful provider response. |
| Large synthetic catalog | `catalog::cache::projection_tests::large_projection_matches_snapshot_and_legacy_row_walk` | pass | 600 global models, 1,200 provider rows, 1,200 support rows; compares ordering and canonical metadata/capability serialization. Structural evidence only; no latency SLA claimed. |
| No migration/public API/connection change | Source/Cargo diff review; schema 54 full tests | pass | No dependency or migration delta. `CacheSnapshot` remains public and unchanged. |
| Full default/no-default verification | Both complete serial workspace suites | pass | Commands and environment controls below. |

## 3. Production implementation evidence

`CacheDiffProjection` holds only ordered model IDs and provider/model keys needed by `CatalogRefreshResult`. `refresh_locked` captures this projection before and after refresh in place of full `CacheSnapshot` values.

`ModelCatalogCache::persistence_projection` walks the cache maps directly and creates the owned `CatalogPersistenceBatch`. It serializes capabilities once per persisted model/provider row, clones only database-owned row fields, filters provider rows against persisted global model IDs, and maps support directly from the account-support map. No unrelated configuration, freshness, outcome, or diagnostics maps are cloned for persistence.

`CatalogService::persist` copies pending refresh/ping facts and account IDs under the service-state mutex, releases it, projects rows under the catalog mutex, releases that guard, and only then performs repository reads/writes. Pending facts clear only after repository success. Hydration similarly clones the cache under its mutex, performs database hydration without either mutex held, then publishes under short locks. No database work moved inside the SQLite transaction.

Before/after ownership:

| Path | Before | After |
|---|---|---|
| Refresh diff | Full `CacheSnapshot` before and after, then allocate ordered sets from vectors | Ordered model/key sets projected directly |
| Persistence | Clone full cache (including optional `Config`), walk cloned cache, clone durable fields into rows | Direct cache-reference walk into one owned persistence batch |
| Pending state | Clone pending state while nested with full cache clone | Copy pending facts/account IDs under service state, release it before cache projection |
| Database boundary | Owned batch then DB awaits | Same semantics; no catalog/service mutex crosses an await |

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
CARGO_BUILD_JOBS=1 cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --lib projection_tests -- --test-threads=1
CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --test database_compatibility --test routing_domain --test routing_domain_d008 --test runtime_lifecycle_r008 -- --test-threads=1
umask 077; mkdir -m 700 /tmp/eggpool-m002-full-runtime-rerun-20261003; EGGPOOL_PID_FILE=/tmp/eggpool-m002-full-runtime-rerun-20261003/eggpool.pid EGGPOOL_RUNTIME_DIR=/tmp/eggpool-m002-full-runtime-rerun-20261003 CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
umask 077; mkdir -m 700 /tmp/eggpool-m002-nodefault-runtime-20261003; EGGPOOL_PID_FILE=/tmp/eggpool-m002-nodefault-runtime-20261003/eggpool.pid EGGPOOL_RUNTIME_DIR=/tmp/eggpool-m002-nodefault-runtime-20261003 CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
```

### Results

All listed commands passed. Focused results: catalog refresh 5, cache projection unit 1, database compatibility 9, routing domain 12, routing domain D008 4, runtime lifecycle R008 4. Full default and no-default workspace suites both exited successfully with serial test execution; provider transport reported 35 default and 36 no-default tests, with the no-default SSH-config rejection included.

The first full default run exposed an unrelated flaky fixture in `operations_o002`: it used fixed 30/60/80 ms sleeps to guess that detached handlers had completed, and one assertion failed under the slow serial suite. The fixture now uses explicit `Notify` release/completion boundaries. Its focused 8-test target passed and the complete default suite passed on rerun. This correction aligns with the repository cancellation-test convention; no production control behavior changed.

The host requires one Cargo job, zero test debug info, a private PID/runtime directory, and `umask 077` for the full test suite. An earlier parallel linker attempt was avoided; there are no remaining failed or skipped M002 gates.

## 5. Invariant review

- Provider fetch concurrency, refresh serialization, cache mutation, withdrawal policy, and event/result semantics remain unchanged.
- Ordered BTree iteration preserves output ordering; large-fixture projection ordering matches the old snapshot ordering.
- Database row selection still excludes deprecated and unresolved global rows and retains provider/support relationships for persisted models.
- Service-state pending data clears only after database batch success; injected failure/retry verifies retained refresh outcome and both pings.
- Cache and service-state guards end before provider or database awaits; no new task, queue, lock, DB connection, or transaction scope was introduced.
- No credential, raw body, or new diagnostic field was added.

## 6. Failure and recovery review

The trigger-based failure test aborts the durable refresh insert after in-memory cache mutation. The first refresh reports a persistence error; the in-memory model remains visible. After removing the trigger, the next provider response is a failure, yet persistence converges using the retained earlier successful refresh row and both buffered pings. No duplicate model/support rows result. This preserves the non-transactional cache/SQLite recovery contract.

## 7. Migration and compatibility review

No schema migration, index, durable rewrite, configuration change, or public API change. SQLite remains behind the existing single DB gate/worker and `CatalogRepository`; semantic comparison remains outside the transaction. `CacheSnapshot` compatibility is unchanged. No rollback limitation beyond ordinary code revert.

## 8. Security review

The projection contains only already-durable catalog facts and account IDs. It does not include credentials, request bodies, or new log/diagnostic data. Persistence remains bounded by current catalog contents; no unbounded buffer or side task was introduced.

## 9. Documentation and operations

Updated `architecture/deep-dive-catalog.md` with refresh diff projection, persistence row ownership, lock release before DB awaits, pending-state retry semantics, and retained `CacheSnapshot` behavior. No operator command or recovery procedure changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M002-scoped unresolved finding | — | — |

No direct DB-pause contention fixture was available; structural ownership and forced DB failure/retry are recorded above. This is not a blocker because source scopes make the guard/await boundary explicit. Persistence M007 remains independently blocked on physical Pi/MMC evidence.

## 11. Roadmap disposition

Milestone closed; M003 may proceed. M003 is a measurement-first dashboard query qualification with no dependency on catalog projection, no shared production interface, and no requirement to alter this milestone. It remains ready at the M002 closure baseline. M001 is closed. Persistence M007 remains unchanged and blocked.

## 12. Registry updates

`plans/registry.md` and `plans/subsystems/runtime-efficiency-roadmap.md` mark M002 closed and link this record. M003 remains ready for sequential handoff. The registry status transition and this closure record are committed together.
