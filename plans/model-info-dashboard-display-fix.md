# Model Info Dashboard Display Fix Plan

## Context

The dashboard models page is still rendering model rows without useful model-info content. Current inspection shows the failure is unlikely to be in the HTML renderer itself. The dashboard route fetches model usage stats, a catalog snapshot, and a compact model-info summary map, then merges these into the rows passed to `render_models`. The renderer already looks up `model_info_map` by `base_model_id` first and by literal `model_id` second, then renders the model-info pill. The detail renderer also knows how to display status, summary, provider/callability facts, limits, metadata, benchmarks, Hugging Face metadata, conflicts, and provenance when it receives a `CanonicalModelInfo`.

The more likely defect is earlier in the path:

1. `dashboard.routes._get_model_info_summary_map()` silently returns `{}` when `app.state.model_info` is missing or when `model_info_service.get_summary_map()` raises.
2. `handle_model_detail()` silently catches all model-info lookup/backfill errors and passes `info=None` to `render_model_detail()`, producing the empty-state page.
3. `ModelInfoRepository.get_canonical()` is case-insensitive, but `get_canonical_many()` uses exact `WHERE model_id IN (...)` matching. This can cause the list page to miss canonical rows even when the detail page can find them.
4. `ModelInfoService.reconcile_catalog_snapshot()` creates canonical rows from catalog entries and persisted observations, but external metadata fetches happen later through `refresh_due_models()` or explicit force refresh. This is acceptable, but the operator needs visibility into whether canonical rows exist, whether they are due, whether sources are failing, and whether the model page is missing info because the sidecar map is empty.
5. The page has no visible degraded-state warning when model-info is unavailable, so operators cannot distinguish “all models are sparse/unmatched” from “the model-info subsystem failed.”

This plan implements a targeted correctness and observability pass without changing routing behavior or cost calculation.

## Goals

1. Stop silently hiding model-info failures from operators and logs.
2. Make model-info list-page lookup consistent with single-model detail lookup.
3. Ensure every catalog model has a canonical row visible to the dashboard, including provider-scoped rows.
4. Surface model-info health in API/runtime diagnostics and, minimally, on the dashboard models page when degraded.
5. Add regression tests for empty-map failure, case-insensitive batch lookup, provider-suffixed links, canonical backfill, and renderer visibility.

## Non-goals

1. Do not change routing selection or provider eligibility behavior.
2. Do not make external metadata mandatory for dashboard display.
3. Do not block the models page on slow external source fetches.
4. Do not treat OpenRouter, Artificial Analysis, or Hugging Face observations as authoritative over provider-native effective limits.
5. Do not alter pricing truth. External model-info pricing remains advisory and must not replace cost-calculation catalog/provider data.

## Phase 1: Add logging and degraded-state diagnostics to dashboard model-info retrieval

### Files

- `src/eggpool/dashboard/routes.py`
- `src/eggpool/dashboard/render.py`
- `tests/unit/test_dashboard.py` or a new focused dashboard-route test file if existing structure favors that

### Implementation steps

1. Add a module logger to `dashboard.routes`:

   ```python
   import logging
   logger = logging.getLogger(__name__)
   ```

2. Replace the current silent exception path in `_get_model_info_summary_map()` with explicit logging. Preserve fail-open behavior so the dashboard still renders, but log enough context to diagnose failure:

   - warning when `model_info_service is None`
   - exception when `get_summary_map()` raises
   - debug/info count when summaries are returned, preferably `len(raw_map)` and `len(compact_map)`

3. Change `_get_model_info_summary_map()` to return both the summary map and a small diagnostic object rather than only a dict. Suggested shape:

   ```python
   @dataclass(frozen=True, slots=True)
   class ModelInfoDashboardState:
       summaries: dict[str, dict[str, Any]]
       available: bool
       degraded_reason: str | None = None
       error_class: str | None = None
       summary_count: int = 0
   ```

   If minimizing surface area is preferred, use a plain `dict[str, Any]` diagnostic. The key requirement is that the renderer can show a degraded-state notice when the service is absent or errored.

4. Update `handle_models()` to unpack the new return shape. Pass `model_info_state` or the diagnostic fields to `render_models()`.

5. Update `render_models()` signature to accept optional model-info diagnostics. If model-info is disabled/missing/errored, display a small panel above the table such as:

   - `Model info unavailable: service not attached` when the service is absent.
   - `Model info unavailable: summary map fetch failed; see server logs` when an exception occurred.

   Keep this warning terse and operator-facing. Do not expose stack traces or raw exception messages in HTML.

6. Update `handle_model_detail()` to log exceptions from `get_summary()` and `ensure_canonical()`. Include both the displayed decoded id and the canonical lookup id.

7. Consider showing a similar warning on the detail page when `info is None` because the lookup failed. The renderer currently cannot distinguish “no row” from “lookup error.” A minimal improvement is to add an optional `model_info_error: str | None` argument to `render_model_detail()` and show a degraded warning when present.

### Acceptance criteria

- A missing `app.state.model_info` produces a visible dashboard notice and a server warning, not a silent empty info column.
- An exception from `get_summary_map()` produces a visible dashboard notice and a logged stack trace.
- Existing models/stats rows continue to render even when model-info is degraded.
- No traceback text is emitted into the HTML.

## Phase 2: Fix case-insensitive batch canonical lookup

### Files

- `src/eggpool/model_info/repository.py`
- `tests/unit/test_model_info_repository.py` or nearest existing model-info repository test file

### Implementation steps

1. Update `ModelInfoRepository.get_canonical_many()` so it has the same case-insensitive semantics as `get_canonical()`.

2. Preserve the public return contract: when called with a list of requested `model_ids`, return a dict that the dashboard can query using the requested ids. This is important because `ModelInfoService.get_summary_map(model_ids)` should produce keys matching the catalog model ids passed in by the caller.

3. Recommended implementation:

   - Return `{}` immediately for an empty list.
   - Normalize requested ids with `casefold()`.
   - Query using `lower(model_id) IN (...)` or `model_id COLLATE NOCASE IN (...)`, depending on SQLite behavior and test reliability. `lower(model_id)` is explicit but may avoid index use; catalog sizes are small enough that this is acceptable. If using `COLLATE NOCASE`, verify it applies to the `IN` comparison in SQLite.
   - Build a `casefold -> CanonicalModelInfo` map from rows.
   - Return `{requested_id: info}` for every requested id with a match.

4. For `model_ids is None`, keep existing behavior: return all canonical rows keyed by stored `row["model_id"]`.

5. Add tests:

   - Insert canonical row `GPT-4O` and call `get_canonical_many(["gpt-4o"])`; assert the result contains key `gpt-4o`.
   - Insert canonical row `mimo-v2.5` and call `get_canonical_many(["MIMO-V2.5"])`; assert the returned `CanonicalModelInfo.model_id` is the stored id but the dict key is the requested id.
   - Call `get_canonical_many([])`; assert `{}` and no SQL syntax error.

### Acceptance criteria

- List-page summary lookup has the same case-insensitive behavior as detail-page lookup.
- Dashboard model-info pills render when canonical rows exist with case differences.
- Empty requested-id lists are safe.

## Phase 3: Make canonical summary map keying robust for provider-scoped rows

### Files

- `src/eggpool/model_info/service.py`
- `src/eggpool/dashboard/routes.py`
- `tests/unit/test_dashboard.py`
- `tests/unit/test_model_info_service.py` if present

### Implementation steps

1. Inspect `ModelInfoService.get_summary_map()` behavior. It currently defaults to `self._catalog._models.keys()` when no `model_ids` argument is supplied. The dashboard calls `_get_model_info_summary_map(model_info_service)` without passing the current row ids. In provider-scoped mode, the table rows come from `catalog.cache.get_provider_model_entries()` and each sparse row has `base_model_id = model_id`, but future provider-suffixed display modes or collapsed-provider variants can make this fragile.

2. Change `handle_models()` so it computes catalog rows first or derives requested canonical ids from `catalog_rows` and stats rows, then asks model-info for exactly those base ids. Because the current code uses `asyncio.gather`, keep concurrency where practical:

   - Option A: keep current gather but after merge, if `model_info_summary_map` is empty and model-info is available, call a second targeted `get_summary_map(base_ids)`.
   - Option B: fetch `stats` and `catalog_rows` concurrently, derive ids, then fetch summary map. This is slightly slower but more deterministic and likely acceptable for the dashboard.

3. Prefer Option B for correctness. The dashboard page is not the request dataplane, and catalog/stats queries are already local. The derived ids should include:

   - `row["base_model_id"]` when present
   - `row["model_id"]` as fallback
   - parsed base id from provider-suffixed model id if needed using `parse_model_provider()` with configured providers

4. Update `_get_model_info_summary_map()` to accept optional `model_ids: Iterable[str] | None`. Forward them to `model_info_service.get_summary_map(model_ids)`.

5. Add tests where catalog rows are provider-scoped and model-info canonical rows exist only for unsuffixed/base ids. Assert the info pill is rendered for each provider row.

### Acceptance criteria

- The models page asks model-info for the same model ids it is about to render.
- Provider-scoped rows can display canonical model-info even when the table row shape differs from the canonical row key.
- No regression in collapsed mode.

## Phase 4: Ensure canonical backfill actually covers dashboard-visible catalog rows

### Files

- `src/eggpool/model_info/service.py`
- `src/eggpool/app.py`
- `tests/unit/test_model_info_service.py`
- `tests/unit/test_app_lifespan.py` or existing startup/lifespan tests

### Implementation steps

1. Review `reconcile_catalog_snapshot()` and `backfill_missing_canonical()` against dashboard-visible rows.

2. `reconcile_catalog_snapshot()` iterates `self._catalog._models.keys()`. Confirm that every model returned by `catalog.cache.get_provider_model_entries()` is also present in `_models`. If not, adjust reconciliation to include both:

   ```python
   model_ids = set(self._catalog._models.keys())
   model_ids.update(mid for (mid, _pid) in self._catalog._provider_models.keys())
   ```

3. `get_summary_map()` should use the same union when no explicit model id list is provided.

4. In `ensure_canonical()`, the `in_catalog` check currently uses `model_id in self._catalog._models`. Change it to treat a model as in-catalog if it appears in either `_models` or `_provider_models`:

   ```python
   in_catalog = model_id in self._catalog._models or any(mid == model_id for (mid, _pid) in self._catalog._provider_models)
   ```

   Because this accesses private cache internals already used elsewhere in this service, this is consistent with current style.

5. If provider-specific rows can carry richer metadata than the global `_models` row, consider updating `_build_detail()` to fall back to the first provider-specific entry when the global entry is missing or sparse.

6. Startup already calls `load_cache()`, `reconcile_catalog_snapshot()`, `backfill_missing_canonical()`, and `backfill_legacy_detail_blocks()`. Keep the sequence, but add logging of row counts from the startup model-info pass even when counts are zero.

### Acceptance criteria

- Every dashboard-visible catalog model can get a canonical model-info row.
- `ensure_canonical()` works for catalog rows visible only through provider-specific entries.
- Startup logs show model-info reconcile/backfill counts clearly.

## Phase 5: Add model-info health to runtime diagnostics/API

### Files

- `src/eggpool/runtime_metrics.py` or `src/eggpool/api/stats.py`, depending on current diagnostics layout
- `src/eggpool/model_info/service.py`
- `src/eggpool/model_info/repository.py`
- `tests/unit/test_runtime_metrics.py`

### Implementation steps

1. Add a lightweight `ModelInfoService.health_snapshot()` method. Suggested payload:

   ```python
   {
       "enabled": True,
       "canonical_count": int,
       "catalog_model_count": int,
       "provider_model_count": int,
       "due_count": int,
       "source_health": {...},
       "last_error": ... optional compact source-level data,
   }
   ```

2. Add repository helpers if needed:

   - `count_canonical()`
   - `count_due(now)`
   - optionally `count_observations_by_source()`

3. Wire the snapshot into `/api/stats/runtime` or another existing diagnostics endpoint. Runtime diagnostics are preferable because operators already use that page for subsystem health.

4. Ensure failures in model-info health snapshot are captured as diagnostic errors, not raised through the runtime endpoint.

5. Add tests for:

   - service missing -> `enabled: false` or absent with clear reason
   - service present with zero canonical rows -> count reports zero, no exception
   - source health rows are included without raw payloads

### Acceptance criteria

- Operators can determine from API/runtime output whether model-info is attached, has canonical rows, has due rows, and has source failures.
- No raw model source payloads are exposed in runtime diagnostics.

## Phase 6: Force-refresh and manual operator recovery path

### Files

- Existing model-info API route file, if present, or add under `src/eggpool/api/`
- `src/eggpool/app.py` route registration if needed
- CLI route if `eggpool models refresh` already supports model-info flags
- tests around API/CLI command if available

### Implementation steps

1. Locate existing API endpoints for model-info force refresh. The service already has `refresh_model_info()` and `force_refresh_batch()`, so this may only require surfacing or documenting the existing route.

2. Ensure there is an operator path to force a bounded model-info refresh for the catalog:

   - one model: `model_id`, optional `source`, `force=true`
   - batch: bounded by `model_info.max_models_per_cycle` or an explicit safe upper bound

3. If an endpoint already exists, ensure it returns `sources_attempted`, `sources_matched`, `observations`, `refreshed`, `skipped`, and `errors`, matching service output.

4. Add a dashboard button only if there is already an established dashboard mutation pattern. Otherwise keep this as CLI/API only to avoid introducing CSRF/mutation semantics into the server-rendered dashboard.

5. Update docs or help text for `eggpool models refresh` to distinguish catalog refresh from model-info enrichment refresh if they are separate operations.

### Acceptance criteria

- An operator can force model-info enrichment without restarting EggPool.
- The command/API reports whether provider_catalog, OpenRouter, Artificial Analysis, or Hugging Face actually matched.
- The operation is bounded and does not block the dataplane indefinitely.

## Phase 7: Regression test matrix

### Required tests

1. Dashboard route fail-open with visibility:

   - mock `model_info_service.get_summary_map()` to raise
   - assert models table still renders
   - assert degraded warning is present
   - assert logs include stack trace

2. Missing service:

   - no `app.state.model_info`
   - assert table renders and warning says model-info service is unavailable

3. Case-insensitive batch lookup:

   - canonical row stored as mixed case
   - request lower-case id
   - assert returned map key matches requested id and value is present

4. Provider-scoped rows:

   - catalog has `(model_id, provider_id)` entries
   - canonical row exists for unsuffixed `model_id`
   - assert each provider row renders the model-info pill

5. Detail page lazy backfill:

   - no canonical row exists
   - model is in catalog
   - hit `/models/{model_id}`
   - assert canonical row is created and detail page no longer shows only the empty state

6. Detail page exception observability:

   - mock `ensure_canonical()` to raise
   - assert page renders degraded message without raw traceback
   - assert log captures exception

7. Startup reconciliation coverage:

   - cache has provider-specific entry but missing global entry, if this state is representable
   - run reconcile/backfill
   - assert canonical row exists

8. Runtime diagnostics:

   - service present with source health rows
   - assert counts and source health appear
   - assert raw JSON observations are not present

### Test command

Run targeted tests first:

```bash
pytest tests/unit/test_model_info_repository.py tests/unit/test_dashboard.py tests/unit/test_runtime_metrics.py
```

Then run the broader unit suite:

```bash
pytest tests/unit
```

Run static checks:

```bash
ruff check src tests
pyright
```

## Suggested implementation order

1. Add logging to `_get_model_info_summary_map()` and `handle_model_detail()` first. This gives immediate operational signal and is low risk.
2. Fix `get_canonical_many()` case-insensitive behavior and add repository tests.
3. Pass targeted model ids from the models route into model-info summary lookup.
4. Expand reconciliation/backfill to include provider-model keys if tests reveal a gap.
5. Add dashboard degraded-state notices.
6. Add runtime diagnostics.
7. Add or document force-refresh operator path.
8. Run full tests and inspect the live dashboard.

## Manual verification checklist

After implementation, test on a real EggPool instance:

1. Start EggPool with model-info enabled and dashboard enabled.
2. Open `/models` and verify every catalog-visible model has either a model-info pill or a visible degraded warning explaining why not.
3. Click several provider-scoped model links, including ones whose IDs contain `/` and require path quoting.
4. Confirm the detail page shows provider/callability facts from provider catalog even before external enrichment.
5. Trigger a model-info force refresh for one known model. Confirm the detail page updates `last_refreshed_at`, `sources`, and any external advisory fields that matched.
6. Temporarily break an external source or disable `app.state.model_info` in a test harness. Confirm the dashboard renders rows and shows a degraded warning rather than silently empty info.
7. Check `/api/stats/runtime` or the chosen diagnostics endpoint for model-info counts and source health.
8. Review logs for model-info startup reconciliation counts and any source fetch failures.

## Risk notes

- Do not make model-info failures fatal to dashboard rendering. The models page is operationally useful even when sidecar enrichment is broken.
- Do not expose raw source payloads, raw exception messages, API keys, or upstream URLs in HTML.
- Case-insensitive lookup should preserve requested keys for callers. Otherwise the dashboard lookup can still fail if it asks by one casing and the returned dict is keyed by another.
- Be careful with provider-suffixed model ids. The detail route already strips provider suffixes using configured providers; list-page lookup should not accidentally request canonical rows using suffixed path ids.
- Runtime diagnostics should be bounded and fast. Count queries are fine; scanning all observations or full raw JSON is not.

## Expected outcome

After this pass, the models page should no longer silently show no model info. If canonical rows exist, info pills and detail pages should render. If the model-info subsystem is unavailable or broken, the operator should see an explicit degraded-state warning and the server logs should contain the traceback. The list page and detail page should agree on canonical lookup behavior, including case-insensitive matching and provider-scoped dashboard rows.
