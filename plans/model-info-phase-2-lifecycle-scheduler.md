# Model Info Phase 2: Lifecycle Wiring, Catalog Diffing, Scheduler, and Sparse Refresh

## Objective

Wire the phase 1 model-info foundation into Eggpool's existing runtime lifecycle. After this phase, model-info reconciliation should run automatically at startup, after successful catalog refreshes, and through a supervised periodic refresh loop. Newly discovered sparse models should receive accelerated refresh attempts for a bounded period, while normal models should use slower TTL-based refresh.

This phase should still avoid new external metadata sources except the provider-native source. The focus is lifecycle correctness, queueing, source health, refresh policy, and non-interference with routing.

## Current repo touchpoints

`src/eggpool/app.py` constructs `CatalogService`, loads cached models, optionally runs `catalog.refresh()`, registers `catalog_refresh`, and starts `TaskSupervisor`.

`_catalog_refresh_loop()` currently accepts only `catalog` and `interval_s`, sleeps, then calls `catalog.refresh()`.

`TaskSupervisor` already supports named supervised background tasks with restart/backoff and runtime snapshots.

`CatalogService.refresh()` currently returns `None`. It can either be extended to return a diff result or the app-level loop can snapshot before/after catalog cache state.

`ModelCatalogCache` already exposes `get_all_models()`, provider model entries, model count, first/last seen timestamps, and account support. Prefer adding small public snapshot helpers if needed rather than reading private attributes from outside the catalog package.

## Design approach

Add model-info as an app-state service:

```python
model_info = ModelInfoService(
    config=config.model_info,
    db=db,
    catalog=catalog,
    outbound_client=outbound_client,
)
app.state.model_info = model_info
```

Register it after `CatalogService` has loaded/refreshed enough state to know the initial model set, but before background tasks start.

Do not make model-info failure fatal during startup unless the database migration itself fails. A model-info reconciliation failure should be logged and surfaced through source health/task health, not block routing readiness.

## Catalog refresh result

Prefer returning a refresh result from `CatalogService.refresh()`.

Add a dataclass in `src/eggpool/catalog/service.py` or a small `src/eggpool/catalog/types.py`:

```python
@dataclass(frozen=True)
class CatalogRefreshResult:
    live_model_ids: frozenset[str]
    new_model_ids: frozenset[str]
    withdrawn_model_ids: frozenset[str]
    changed_provider_keys: frozenset[tuple[str, str]]
    refreshed_at: float
    pruned_count: int = 0
```

Implementation detail:

Before fetching, snapshot:

```python
before_model_ids = frozenset(self._cache.get_all_models().keys())
before_provider_keys = frozenset(self._cache.get_provider_model_entries().keys())
```

After static/live tasks, prune, and persist, snapshot again:

```python
after_model_ids = frozenset(self._cache.get_all_models().keys())
after_provider_keys = frozenset(self._cache.get_provider_model_entries().keys())
```

Then compute:

```python
new_model_ids = after_model_ids - before_model_ids
withdrawn_model_ids = before_model_ids - after_model_ids
changed_provider_keys = before_provider_keys ^ after_provider_keys
```

Return the result after persistence succeeds.

If maintaining backward compatibility with tests that expect `None`, update tests explicitly. Returning a dataclass from an async function should not break production call sites that ignore the result.

## Startup wiring

In `_lifespan_runtime`:

1. Build `CatalogService` as today.
2. Attach pricing resolvers as today.
3. Load cached models as today.
4. Run startup catalog refresh as today.
5. Instantiate `ModelInfoService` after catalog state is loaded/refreshed.
6. Call `await model_info.load_cache()`.
7. If `config.model_info.enabled and config.model_info.startup_refresh`, call `await model_info.reconcile_catalog_snapshot(reason="startup")`.
8. Attach `app.state.model_info = model_info`.

The exact position should be after the initial catalog refresh and before route/task registration. Do not call external model-info source fetchers in this phase.

Wrap startup reconciliation in `try/except Exception` and log. Do not raise unless a config/migration error makes the database unusable.

## Background task wiring

Change `_catalog_refresh_loop` signature:

```python
async def _catalog_refresh_loop(
    catalog: CatalogService,
    interval_s: int,
    model_info: ModelInfoService | None = None,
) -> None:
```

After successful `result = await catalog.refresh()`, call:

```python
if model_info is not None:
    await model_info.reconcile_catalog_refresh(result)
```

If model-info reconciliation fails, catch/log it separately so catalog refresh failures and model-info enrichment failures are distinguishable.

Register:

```python
supervisor.register(
    "catalog_refresh",
    lambda: _catalog_refresh_loop(
        catalog,
        config.models.refresh_interval_s,
        model_info if config.model_info.enabled else None,
    ),
)
```

Register separate periodic model-info refresh:

```python
if config.model_info.enabled and config.model_info.refresh_interval_s > 0:
    supervisor.register(
        "model_info_refresh",
        lambda: model_info.run_periodic_refresh(),
    )
```

This separate task handles due rows even when the catalog model set does not change.

## Scheduler plan

Create `src/eggpool/model_info/scheduler.py`.

Responsibilities:

Compute next refresh time based on status, first-seen age, source availability, and configuration.

Rank due work.

Return a bounded batch for the periodic refresh loop.

Suggested public API:

```python
@dataclass(frozen=True)
class RefreshDecision:
    model_id: str
    due: bool
    priority: int
    reason: str
    next_refresh_at: datetime
```

```python
class ModelInfoRefreshScheduler:
    def __init__(self, config: ModelInfoConfig) -> None: ...

    def next_refresh_for(
        self,
        *,
        status: ModelInfoStatus,
        first_seen_at: datetime,
        last_refreshed_at: datetime | None,
        now: datetime,
        has_conflicts: bool = False,
        source_cooldown_until: datetime | None = None,
    ) -> datetime: ...
```

Policy:

`sparse_new` and age < 48h: `now + sparse_new_initial_ttl_s`.

`sparse_new` and age < accelerated window: `now + sparse_new_later_ttl_s`.

`sparse_new` older than accelerated window: downgrade to `partial` unless truly empty.

`conflicting`: `now + conflict_ttl_s`.

`partial`: `now + partial_ttl_s`.

`fresh`: `now + known_ttl_s`.

`source_unavailable`: respect source cooldown, otherwise partial TTL.

`manual_override`: refresh external observations normally but never overwrite overridden fields.

The scheduler must not spin. Always set a non-null `next_refresh_at` after each cycle, even on source failure.

## ModelInfoService additions

Add:

```python
async def reconcile_catalog_refresh(self, result: CatalogRefreshResult) -> dict[str, int]: ...
```

Behavior:

For `new_model_ids`: create canonical row if absent, mark `sparse_new`, set `next_refresh_at=now`, and immediately refresh provider-native observations.

For `changed_provider_keys`: refresh provider-native observations for affected model IDs and recompute canonical detail/provenance.

For `withdrawn_model_ids`: phase 2 can let FK cascade handle deletion after catalog reconciliation. If the canonical row remains because model row remains, set status `withdrawn` only if the model is not live.

For unchanged live models: do not recompute unless due.

Add:

```python
async def run_periodic_refresh(self) -> None:
    while True:
        await asyncio.sleep(config.refresh_interval_s)
        await self.refresh_due_models()
```

Add:

```python
async def refresh_due_models(self) -> dict[str, int]: ...
```

This should:

1. Query repository `list_due(limit=max_models_per_cycle)`.
2. Refresh provider-native observations for those models.
3. Reconcile canonical summaries.
4. Update `last_refreshed_at` and `next_refresh_at`.
5. Record counts by status/source.

Even before external sources exist, this validates the scheduling machinery.

## Source health/backoff

Extend repository source health support from phase 1.

Model-info source health should be per source, not per model. Fields already planned:

```text
source
enabled
last_success_at
last_error_at
last_error_class
last_error_message
cooldown_until
```

Add service helpers:

```python
async def _record_source_success(source_name: str) -> None
async def _record_source_error(source_name: str, exc: BaseException) -> None
```

Backoff policy:

Provider-native source should almost never fail independently; errors should be model-scoped and logged.

External source backoff will matter in later phases. Implement generic exponential backoff now if straightforward, but it can be simple in phase 2:

```text
first failure: 15m
second failure: 1h
repeated failure: 6h
max: 24h
```

If adding failure count requires a schema field, add `failure_count INTEGER NOT NULL DEFAULT 0` to `model_info_source_health`. Otherwise infer from recent error state in memory for phase 2.

## Sparse-new behavior

When a model appears in the catalog but has no canonical row:

1. Insert canonical row.
2. Set `status = 'sparse_new'`.
3. Set `sparse = 1`.
4. Set `first_seen_at` from catalog model info if available, otherwise now.
5. Set `last_seen_at = now`.
6. Set `last_refreshed_at = NULL` initially.
7. Set `next_refresh_at = now`.
8. Add a summary such as: `New model detected; metadata sparse. Callable via discovered provider(s). External details not yet verified.`

After each refresh, compute coverage:

```text
has_provider_observation
has_display_name
has_effective_context_or_upstream_context
has_capability_flags
has_pricing_state
has_family_or_release
has_benchmark_state
```

Suggested status transitions:

If no provider observation: `unmatched` or `source_unavailable` depending cause.

If newly discovered and coverage <= 2: `sparse_new`.

If newly discovered and coverage > 2 but no benchmarks/family: `partial`.

If beyond accelerated window and still sparse: `partial` with `sparse=1`; do not poll hourly forever.

If conflicting fields exist: `conflicting` unless manual override resolves the display field.

## Avoiding write amplification

This feature must respect the repo's SBC/microSD use case.

Do not rewrite canonical rows when the computed payload is byte-identical. Compare `detail_json`, `provenance_json`, `conflicts_json`, `status`, `summary`, and `next_refresh_at` before issuing writes where practical.

Deduplicate observations by `(source, source_model_id, raw_hash)`.

For provider-native observations, avoid writing an observation every refresh when the normalized provider metadata has not changed.

Batch writes inside one transaction per refresh cycle.

Honor `max_models_per_cycle`.

## Runtime metrics visibility

No dashboard UI is required yet, but runtime/task snapshots should show the `model_info_refresh` task automatically through the existing `TaskSupervisor` snapshot.

Optionally add simple operational events later, but do not require it in this phase.

## Tests

Catalog diff tests:

`test_catalog_refresh_returns_new_model_ids`

`test_catalog_refresh_returns_withdrawn_model_ids`

`test_catalog_refresh_returns_changed_provider_keys`

If direct integration into `CatalogService.refresh()` is difficult, test the helper that computes diffs from before/after snapshots.

Scheduler tests:

`test_sparse_new_initial_refresh_interval`

`test_sparse_new_later_refresh_interval`

`test_sparse_new_exits_accelerated_window`

`test_conflicting_uses_conflict_ttl`

`test_fresh_uses_known_ttl`

`test_scheduler_never_returns_past_time_after_refresh`

Service tests:

`test_startup_reconcile_creates_rows_for_all_catalog_models`

`test_catalog_refresh_new_model_sets_due_now`

`test_periodic_refresh_respects_max_models_per_cycle`

`test_refresh_due_models_deduplicates_unchanged_observations`

`test_model_info_failure_does_not_raise_from_catalog_refresh_loop`

App wiring tests:

`test_lifespan_attaches_model_info_when_enabled`

`test_model_info_disabled_does_not_register_task`

`test_model_info_enabled_registers_model_info_refresh_task`

`test_catalog_refresh_loop_invokes_model_info_reconcile_after_success`

## Manual verification

Start Eggpool with `model_info.enabled = true`.

Confirm startup logs show model-info cache load and startup reconciliation.

Confirm `model_info_canonical` contains rows for all live catalog models.

Confirm sparse new models get `next_refresh_at` close to now or within the configured sparse TTL.

Confirm `model_info_refresh` appears in runtime background task snapshots.

Temporarily disable model-info and confirm app starts normally and no task is registered.

Run catalog refresh and confirm model-info reconciliation is invoked only after successful catalog refresh.

## Acceptance criteria

Model-info service is attached to app state when enabled.

Startup reconciliation creates/updates canonical rows from the existing catalog.

`CatalogService.refresh()` returns or otherwise exposes enough diff information for model-info reconciliation.

Successful catalog refreshes trigger model-info reconciliation without affecting routing.

A supervised `model_info_refresh` task processes due rows.

Sparse-new models receive accelerated refresh scheduling for a bounded period.

Provider-native observation refreshes are deduplicated and do not cause excessive writes.

Failures in model-info work do not break catalog refresh, readiness, or request routing.
