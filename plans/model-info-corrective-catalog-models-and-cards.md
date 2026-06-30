# Model Info Corrective Plan: Catalog-Complete Models Page and Functional Model Cards

## Purpose

The current model-info implementation has two related UX failures:

1. Model cards/model detail pages rarely display actual external information, especially Hugging Face model-card metadata.
2. The dashboard Models page is usage-driven: it shows models with traffic in the selected stats period, not the full aggregated model catalog Eggpool currently knows it can route to.

These should be fixed together. Model-info is supposed to describe the aggregated model catalog, not merely historical usage rows. The dashboard should expose every currently available model across all configured providers, link each to a model detail page, and then optionally attach usage statistics for the selected period.

## Current observed shape

The repo already has substantial implementation:

- `ModelInfoService` builds provider-native observations, optional OpenRouter, Artificial Analysis, and Hugging Face sources.
- `ModelInfoRepository` persists canonical rows, observations, aliases, source health, and overrides.
- `dashboard/routes.py` has `handle_models()` and `handle_model_detail()`.
- `dashboard/render.py` has `render_models()` and `render_model_detail()` with Hugging Face and benchmark sections.
- `api/model_info.py` has model-info summary/detail/source/refresh endpoints.
- `app.py` wires model-info startup reconciliation and registers `model_info_refresh` and canonical backfill tasks.

The problem is not total absence of code. The problem is that the data path is usage-first and provider-catalog-only in key locations, while external observations and configured aliases are not reliably merged into canonical detail.

## Core defects to correct

### 1. Configured aliases are defined but not seeded

`ModelInfoConfig.aliases` and `ModelInfoAliasConfig` exist, but service startup does not appear to insert configured aliases into `model_info_aliases`.

This prevents exact-source fetches from ever running for Hugging Face unless aliases were created through another path. Hugging Face is intentionally exact-alias-only, so configured alias seeding is mandatory.

Correct behavior:

- On model-info service initialization/startup, seed `config.model_info.aliases` into `model_info_aliases`.
- Configured aliases should be idempotent and update `last_seen_at`.
- Configured aliases should be source-specific.
- Ambiguous duplicate aliases should be rejected or disabled deterministically with a warning.

### 2. Startup reconciliation can wipe enriched canonical detail

`reconcile_catalog_snapshot()` builds canonical detail only from `_build_detail(model_id)`, which reads provider catalog data. That means startup/backfill reconciliation can overwrite `detail_json` that previously contained OpenRouter, Artificial Analysis, or Hugging Face enrichment.

Correct behavior:

- Canonical reconciliation must merge provider-native catalog data with latest persisted external observations.
- Provider-native data remains authoritative for callability and effective local limits.
- External observations remain advisory but durable.
- Startup reconciliation must not erase previously fetched external metadata merely because an external fetch is not currently happening.

### 3. Manual refresh does not fetch external model-card data

`POST /api/model-info/refresh?model_id=...` currently calls catalog snapshot reconciliation. That is provider-catalog-only and ignores the requested model except for response counts. The `force` and `source` parameters are documented as reserved but not honored.

Correct behavior:

- `POST /api/model-info/refresh?model_id=<id>&force=1` should refresh exactly that model immediately.
- It should run provider-native observation refresh and configured external source matching/fetching for that model regardless of `next_refresh_at`.
- `source=huggingface` should fetch only the Hugging Face source for that model when configured.
- The endpoint should return useful counts: requested, refreshed, skipped, observations, errors, sources_attempted, sources_matched.

### 4. Source provenance can claim sources that did not match

`refresh_due_models()` builds provenance source lists based on whether a source adapter exists, not whether a record was fetched/matched. This can cause the UI to show Hugging Face as a source while `huggingface_metadata` is empty.

Correct behavior:

- Canonical `provenance.sources` should list only sources that contributed an observation selected into canonical detail or persisted for that model.
- Separate source-health endpoints can show configured sources, disabled sources, and source failures.
- UI should distinguish `source configured` from `source matched this model`.

### 5. Detail schema is inconsistent

Provider-native `_build_detail()` writes flat keys such as `context_tokens`, `input_tokens`, and `output_tokens`, while render/detail code expects nested `detail["limits"]` with `effective_context` and `external_context`.

Correct behavior:

Normalize canonical detail to a single schema:

```json
{
  "display_name": "...",
  "providers": ["..."],
  "protocol": "openai",
  "limits": {
    "effective_context": 220000,
    "effective_input": null,
    "effective_output": null,
    "external_context": 1000000,
    "external_output": null
  },
  "modalities": ["text"],
  "supports_tools": true,
  "supports_vision": false,
  "external_ids": {},
  "benchmarks": [],
  "huggingface_metadata": {},
  "license": null,
  "release_date": null
}
```

Renderers and API serializers should read this normalized structure.

### 6. Models page is usage-driven, not catalog-driven

`handle_models()` currently calls `stats.get_model_stats()`, so models without requests in the selected period do not appear. This hides most newly aggregated provider models.

Correct behavior:

- The Models page should be catalog-complete by default.
- Each row should represent an available catalog model/provider exposure entry, not only a usage stats row.
- Usage metrics should be joined onto catalog rows for the selected period.
- Models with zero usage should show requests/cost/tokens as zero or em dash, but still be visible and linkable.

## Desired end state

The dashboard Models page should answer: “What models can Eggpool currently expose, across all configured providers, and what do we know about each one?”

It should include:

- Every currently exposed model from `CatalogService.get_models_for_exposure()`.
- Provider-scoped rows when `collapse_models = false`.
- Collapsed rows when `collapse_models = true`, including contributing providers.
- A stable model detail link for each row.
- A model-info status pill for each row.
- Usage stats for the selected time range when present.
- Zero/empty usage indicators when no requests exist.
- Optional filters for provider, account, availability/status, and info status.

The model detail page should answer: “What does Eggpool know about this model, where did the facts come from, and how fresh/conflicted are they?”

It should include:

- Provider/callability facts.
- Effective limits.
- External IDs.
- Hugging Face metadata when configured and matched.
- Benchmarks when configured and matched.
- Source health/provenance.
- Conflicts.
- Local usage stats, if any.

## Implementation plan

## Phase A: Seed configured aliases and verify source matching

### Tasks

Add `ModelInfoService.seed_configured_aliases()`.

Pseudo-implementation:

```python
async def seed_configured_aliases(self) -> dict[str, int]:
    seeded = 0
    skipped = 0
    for alias in self._config.aliases:
        confidence = _alias_confidence_to_float(alias.confidence)
        await self._repo.upsert_alias(
            model_id=alias.model_id,
            provider_id=alias.provider_id,
            alias=alias.source_model_id,
            source=alias.source,
            confidence=confidence,
            active=True,
        )
        seeded += 1
    return {"seeded": seeded, "skipped": skipped}
```

Call it during model-info startup before `load_cache()` and before any external refresh.

Add validation helpers:

- Reject empty `source_model_id`.
- Reject aliases whose `source` is unknown unless intentionally allowing arbitrary future sources.
- Log duplicate configured aliases.
- Prefer explicit config over auto-discovered aliases.

Add CLI/API inspection:

- `GET /api/model-info/{model_id}/aliases` should show source-specific aliases with source names, not only a flat string list if possible.
- Optional CLI: `eggpool modelinfo aliases <model-id>`.

### Tests

- `test_seed_configured_huggingface_aliases_on_startup`
- `test_seed_configured_openrouter_aliases_on_startup`
- `test_configured_alias_is_idempotent`
- `test_configured_alias_enables_huggingface_fetch`
- `test_unknown_source_alias_is_rejected_or_warned`

### Acceptance criteria

A config block such as this becomes functional:

```toml
[[model_info.aliases]]
provider_id = "fireworks"
model_id = "llama-3.1-405b-instruct"
source = "huggingface"
source_model_id = "meta-llama/Llama-3.1-405B-Instruct"
confidence = "curated"
```

After startup, `model_info_aliases` contains the configured row and Hugging Face fetch can use it.

## Phase B: Make canonical detail observation-driven and non-destructive

### Tasks

Add repository methods:

```python
async def get_latest_observations_for_model(
    self,
    model_id: str,
    *,
    sources: list[str] | None = None,
) -> dict[str, SourceModelRecord | dict[str, object]]: ...
```

At minimum, return latest row per `(source, source_model_id)` or latest row per source. If multiple observations exist for a source, choose the most recent active alias match.

Add conversion from observation row to a normalized record/dict:

```python
async def get_latest_observation_payloads(model_id) -> list[dict[str, object]]
```

Create a canonical detail builder:

```python
def build_canonical_detail(
    *,
    model_id: str,
    provider_detail: dict[str, object],
    observations: list[SourceModelRecord | ObservationPayload],
    existing_detail: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    ...
```

The builder should:

- Normalize provider limits into `detail["limits"]`.
- Merge latest OpenRouter observation into external IDs, external context, modalities, and advisory pricing.
- Merge latest Artificial Analysis observation into benchmarks.
- Merge latest Hugging Face observation into `huggingface_metadata`, license, task/pipeline tags, model type, downloads/likes.
- Preserve previously known external fields if a source is temporarily unavailable and the observation has not expired.
- Build provenance only for fields/sources actually used.
- Build conflicts field-wise.

Update all canonical write paths to use the canonical detail builder:

- `reconcile_catalog_snapshot()`
- `reconcile_catalog_refresh()`
- `refresh_due_models()`
- `ensure_canonical()` where possible
- manual single-model refresh path

### Tests

- `test_startup_reconcile_preserves_existing_huggingface_metadata`
- `test_startup_reconcile_preserves_existing_benchmarks`
- `test_provider_detail_and_external_observations_merge_into_normalized_limits`
- `test_huggingface_observation_populates_huggingface_metadata`
- `test_openrouter_observation_populates_external_ids_and_external_context`
- `test_provenance_sources_only_include_used_observations`

### Acceptance criteria

Once an HF observation is fetched and persisted, it remains visible after restart/startup reconciliation unless explicitly expired/removed.

Canonical `detail_json` uses one normalized schema understood by both API and dashboard renderers.

## Phase C: Implement true single-model/force refresh

### Tasks

Add service method:

```python
async def refresh_model_info(
    self,
    model_id: str,
    *,
    provider_id: str | None = None,
    source: str | None = None,
    force: bool = False,
) -> dict[str, object]: ...
```

Behavior:

1. Normalize provider-suffixed IDs using known providers where needed.
2. Ensure canonical row exists.
3. Refresh provider-catalog observation for that model.
4. If `source is None or source == "openrouter"`, attempt OpenRouter exact/alias match.
5. If `source is None or source == "artificial_analysis"`, attempt AA match.
6. If `source is None or source == "huggingface"`, use configured HF aliases and fetch exact repo IDs.
7. Persist observations.
8. Rebuild canonical detail from provider + latest observations.
9. Set `last_refreshed_at = now` and schedule next refresh.
10. Return detailed counts and source outcomes.

Update `handle_model_info_refresh()`:

- Honor `model_id`.
- Honor `source`.
- Honor `force`.
- Without `model_id`, run due refresh unless `force=1`, in which case refresh a bounded batch of all catalog models.

Example response:

```json
{
  "status": "ok",
  "requested": 1,
  "refreshed": 1,
  "skipped": 0,
  "errors": 0,
  "sources_attempted": ["provider_catalog", "huggingface"],
  "sources_matched": ["provider_catalog", "huggingface"],
  "observations": 2
}
```

### Tests

- `test_manual_refresh_model_id_refreshes_only_requested_model`
- `test_manual_refresh_force_bypasses_next_refresh_at`
- `test_manual_refresh_source_huggingface_fetches_hf_only_plus_provider`
- `test_manual_refresh_reports_source_matched_only_on_record`
- `test_manual_refresh_unknown_model_creates_unmatched_canonical_without_crash`

### Acceptance criteria

A user can force-refresh one model and immediately see HF/card data if alias/source are configured and reachable.

## Phase D: Make Models page catalog-complete

### Tasks

Introduce a dashboard-facing catalog row assembler. Options:

1. Add method on `CatalogService`, e.g. `get_dashboard_models_for_exposure(health_manager=None)`.
2. Add helper in `dashboard/routes.py` that calls `catalog.get_models_for_exposure(health_manager=health_mgr)` and normalizes rows.

Preferred route-level flow:

```python
catalog = request.app.state.catalog
health_mgr = getattr(request.app.state, "health_manager", None)
catalog_models = catalog.get_models_for_exposure(health_manager=health_mgr)
usage_stats = await stats.get_model_stats(time_range, account_name=account or None, use_cache=True)
model_info_map = await _get_model_info_summary_map(model_info_service, model_ids=base_ids)
rows = merge_catalog_models_with_usage(catalog_models, usage_stats, model_info_map)
```

Add merge function:

```python
def merge_catalog_models_with_usage(
    catalog_models: list[dict[str, Any]],
    usage_rows: list[dict[str, Any]],
    model_info_map: dict[str, Any],
) -> list[dict[str, Any]]:
    ...
```

Merge keys:

- For provider-scoped mode: `(base_model_id or model_id, provider_id)`.
- For collapsed mode: `model_id` and contributing providers.
- For legacy usage rows without provider: match by model_id only.

Each output row should include:

```text
model_id
base_model_id
provider_id or providers
available = true/false
catalog_status
routing_priority
routing_priority_max
usage request_count
usage cost_microdollars
usage token totals
usage exactness counts
info_status/info_pill payload
```

Models with no usage row should have:

```text
request_count = 0
error_count = 0
cost_microdollars = 0
input_tokens = 0
output_tokens = 0
total_tokens = 0
avg_latency_ms = None
avg_ttft_ms = None
tokens_per_second = None
exactness counts = 0
```

Add optional query filters:

```text
provider=<provider-id>
account=<account-name>       # existing account filter should still apply to usage stats
info_status=sparse|partial|fresh|conflict|unmatched
availability=available|unavailable|all
used=all|used|unused
```

The first corrective patch does not need every filter, but provider and used/unused are high-value.

### Rendering changes

Update `render_models()` to assume rows are catalog-complete.

Add columns or badges:

- Availability: available/unavailable or exposed/hidden.
- Providers: for collapsed rows, show comma-separated provider IDs.
- Usage: continue showing requests/cost/tokens.
- Info: status pill.

Change empty state:

- If catalog is empty: `No models discovered from configured providers.`
- If filters hide rows: `No models match the selected filters.`
- Do not use `No model data for this period` for the catalog-complete default.

Ensure links use base canonical ID for detail lookup but preserve display/provider suffix when useful:

```text
/models/{urlencoded model_id}
```

For provider-suffixed IDs, detail route already strips known provider suffixes, but link generation should URL-encode `/` safely.

### Tests

- `test_models_page_includes_catalog_model_with_zero_usage`
- `test_models_page_includes_all_exposed_catalog_models`
- `test_models_page_merges_usage_stats_for_used_model`
- `test_models_page_provider_scoped_rows_when_not_collapsed`
- `test_models_page_collapsed_rows_when_collapse_models_enabled`
- `test_models_page_unused_filter_shows_zero_usage_models`
- `test_models_page_used_filter_hides_zero_usage_models`
- `test_models_page_model_info_pill_uses_base_model_id_for_provider_suffixed_row`
- `test_model_detail_link_url_encodes_provider_suffixed_id`

### Acceptance criteria

The Models page shows all currently available/exposed models even with zero requests in the selected period.

Usage columns remain meaningful and are joined in when available.

Every listed model has a detail link.

## Phase E: Fix API/dashboard detail display schema

### Tasks

Normalize detail object once, then simplify render/API code.

Update `_build_detail()` or new detail builder to output nested `limits`.

Update `_enrich_detail_from_record()` to write:

```python
limits = detail.setdefault("limits", {})
limits["external_context"] = record.context_window
limits["external_output"] = record.max_output_tokens
```

Update conflict detection to read normalized limits:

```python
local_ctx = detail.get("limits", {}).get("effective_context")
ext_ctx = record.context_window
```

Update API `_detail_response()` to avoid rebuilding shape from mismatched flat keys if canonical detail is already normalized.

Update dashboard `render_model_detail()` to read normalized fields only, with compatibility fallback for existing flat rows during migration:

```python
ctx = limits.get("effective_context") or detail.get("context_tokens")
```

### Tests

- `test_detail_builder_outputs_nested_limits`
- `test_detail_renderer_displays_effective_context_from_nested_limits`
- `test_detail_renderer_fallback_displays_legacy_flat_context_tokens`
- `test_api_detail_response_contains_normalized_limits`
- `test_context_conflict_detection_reads_normalized_limits`

### Acceptance criteria

Model detail pages display effective context/output limits and external context when available.

Existing canonical rows with flat detail do not render as empty before next refresh.

## Phase F: Backfill and migration behavior

### Tasks

Add a one-time or periodic canonical repair path:

```python
async def repair_canonical_detail_shape(limit: int = 500) -> dict[str, int]: ...
```

It should:

- Read canonical rows.
- Convert flat limit keys into nested `limits`.
- Merge latest persisted external observations.
- Correct misleading provenance sources.
- Preserve first_seen_at.
- Update last_refreshed_at only if a real refresh happened; otherwise update a separate `repaired_at` provenance field.

Run this during startup after migrations/backfill, bounded to avoid large write spikes. The periodic backfill task can continue repairing batches.

Alternatively, add a CLI/manual endpoint:

```text
eggpool modelinfo repair
POST /api/model-info/repair   # auth-gated, optional
```

### Tests

- `test_repair_converts_flat_context_to_nested_limits`
- `test_repair_merges_latest_hf_observation_into_canonical_detail`
- `test_repair_removes_unmatched_sources_from_provenance`
- `test_repair_is_idempotent`

### Acceptance criteria

Existing databases recover without manual deletion of model-info tables.

## Verification checklist

Manual check with only provider catalog:

1. Start Eggpool.
2. Open `/models` with no requests in the period.
3. Confirm all discovered provider models are listed.
4. Confirm each model has a status pill and detail link.
5. Confirm unused models show zero usage.

Manual check with Hugging Face alias:

1. Add a configured alias:

```toml
[model_info.sources.huggingface]
enabled = true

[[model_info.aliases]]
provider_id = "fireworks"
model_id = "llama-3.1-405b-instruct"
source = "huggingface"
source_model_id = "meta-llama/Llama-3.1-405B-Instruct"
confidence = "curated"
```

2. Restart Eggpool.
3. Confirm alias row exists through `/api/model-info/<model>/aliases`.
4. Run `POST /api/model-info/refresh?model_id=<model>&source=huggingface&force=1`.
5. Confirm source health shows Hugging Face success.
6. Confirm model detail page shows Hugging Face metadata.
7. Restart Eggpool.
8. Confirm Hugging Face metadata remains visible.

Manual check with OpenRouter/AA:

1. Configure aliases for a known model.
2. Force refresh.
3. Confirm external IDs/benchmarks appear only when records actually matched.
4. Confirm provenance does not list sources with no matched observation.

## Expected files to modify

Likely required:

```text
src/eggpool/model_info/service.py
src/eggpool/model_info/repository.py
src/eggpool/model_info/identity.py
src/eggpool/api/model_info.py
src/eggpool/dashboard/routes.py
src/eggpool/dashboard/render.py
src/eggpool/api/models.py
src/eggpool/models/config.py
```

Likely tests:

```text
tests/model_info/test_aliases.py
tests/model_info/test_service_refresh.py
tests/model_info/test_reconciliation.py
tests/api/test_model_info.py
tests/dashboard/test_models_catalog_complete.py
tests/dashboard/test_model_detail.py
```

## Non-goals

Do not add fuzzy Hugging Face search.

Do not scrape model-card HTML.

Do not use model-card metadata to suppress routing.

Do not make external source failures affect readiness.

Do not require a model to have usage before it appears on the Models page.

Do not remove usage stats from the Models page; join them onto catalog rows instead.

## Success criteria

The Models page lists every model Eggpool currently exposes from aggregated provider catalogs, including unused models.

Every listed model links to a model detail/card page.

Configured Hugging Face aliases are seeded and used for exact API fetches.

Forced refresh for one model actually fetches external model-card data.

Canonical detail is rebuilt from provider-native facts plus latest persisted external observations.

Startup reconciliation does not wipe existing model-card metadata.

Provenance sources only include sources that actually contributed matched observations.

Detail pages display normalized limits, Hugging Face metadata, benchmarks, conflicts, and source provenance when available.

All changes preserve the central invariant: model-info is advisory and must not change routing eligibility.
