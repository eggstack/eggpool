# Model Info Closure Pass: Provider-Scoped Catalog Merge and Refresh ID Normalization

## Purpose

The model-info corrective implementation has landed the major architecture fixes: configured alias seeding, observation-driven canonical detail, force refresh, normalized limits, catalog-complete Models page, and collapse-aware model listing. Two closure risks remain:

1. Provider-scoped Models page rows may still hide unused provider/model pairs when the same base model has usage through another provider.
2. `POST /api/model-info/refresh?model_id=<id>` documents provider-suffixed IDs as accepted, but the service currently expects the caller to pass a canonical base model ID.

This closure pass should tighten those edges and add targeted regression tests so the model-info/dashboard path is stable.

## Current shape to preserve

Do not rework the broader model-info subsystem. The current high-level design is correct:

- `ModelInfoService.load_cache()` seeds configured aliases before external matching.
- `reconcile_catalog_snapshot()` rebuilds canonical detail from provider-native data plus latest persisted observations.
- `build_canonical_detail()` preserves external enrichment and produces normalized `detail["limits"]`.
- `refresh_model_info()` supports single-model refresh and force refresh.
- `dashboard.routes.handle_models()` now merges catalog rows with usage rows.
- `_get_catalog_rows()` respects `models.collapse_models`.
- `render_models()` looks up model-info by `base_model_id` first, then literal `model_id`.

The closure pass should make surgical corrections, not start another redesign.

## Defect 1: provider-scoped merge is still partially model-id keyed

### Problem

`_merge_models_with_catalog()` has provider-aware structures such as `catalog_by_key[(model_id, provider_id)]`, but the dedupe path still uses `seen: set[str]` keyed only by `model_id`.

That can suppress catalog rows in this scenario:

```text
Catalog rows:
  (model_id="gpt-4o", provider_id="openai")       request_count=12 via stats
  (model_id="gpt-4o", provider_id="openrouter")  request_count=0 catalog-only

Stats rows:
  (model_id="gpt-4o", provider_id="openai")
```

The stats row adds `"gpt-4o"` to `seen`. The sparse `openrouter` row is later skipped because `mid in seen`, even though the `(model_id, provider_id)` pair is distinct and should render.

This violates the provider-scoped dashboard goal: show every currently available provider/model exposure row, including unused sibling providers.

### Correct behavior

In provider-scoped mode, merge/dedupe by `(model_id, provider_id)`.

In collapsed mode, merge/dedupe by `model_id`.

Because `_merge_models_with_catalog()` currently receives only rows, it should infer merge mode from row shape or a new explicit parameter.

Preferred implementation:

```python
def _merge_models_with_catalog(
    stats_rows: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
    *,
    collapse_models: bool = False,
) -> list[dict[str, Any]]:
    ...
```

Then call:

```python
collapse_models = _read_collapse_models(app_config)
merged_rows = _merge_models_with_catalog(
    models if models is not None else [],
    catalog_rows,
    collapse_models=collapse_models,
)
```

Use helper functions:

```python
def _model_row_key(row: dict[str, Any], *, collapse_models: bool) -> tuple[str, str]:
    model_id = str(row.get("model_id") or "")
    if collapse_models:
        return (model_id, "")
    provider_id = str(row.get("provider_id") or "")
    return (model_id, provider_id)
```

Also keep an ID-only fallback for legacy stats rows that lack provider ID:

```python
def _legacy_model_id_key(row: dict[str, Any]) -> str:
    return str(row.get("model_id") or "")
```

Merge order should be:

1. Build `catalog_by_key` using `_model_row_key(catalog_row, collapse_models=...)`.
2. Build `catalog_by_id` for fallback only.
3. Iterate stats rows.
4. For each stats row, compute primary key.
5. If exact key matches a catalog row, merge diagnostic fields from that exact row.
6. Else if collapsed mode or provider ID is missing, fall back to `catalog_by_id[model_id]`.
7. Add the exact key to `seen_keys`, not only model ID.
8. Append all catalog rows whose exact key is not in `seen_keys`.

The final sort can stay:

```python
merged.sort(key=lambda r: (-request_count, model_id, provider_id))
```

### Edge cases

If a stats row lacks provider ID in provider-scoped mode:

- Use model ID fallback for diagnostic fields.
- Add a seen key `(model_id, "")`, not `(model_id, provider_id)` for all providers.
- Do not suppress catalog provider rows with explicit provider IDs.

If duplicate catalog rows share the same provider-scoped key:

- Last write wins today, but this should not happen.
- Optionally log a debug warning; not necessary for closure.

If `collapse_models = true`:

- Continue deduping by model ID.
- A stats row with any provider should merge onto the collapsed row for the same model ID.
- The dashboard should not duplicate provider rows.

### Tests

Add or extend dashboard route/helper tests.

Required tests:

```text
test_merge_provider_scoped_keeps_unused_sibling_provider
```

Setup:

```python
catalog_rows = [
    sparse(model_id="gpt-4o", provider_id="openai", request_count=0),
    sparse(model_id="gpt-4o", provider_id="openrouter", request_count=0),
]
stats_rows = [
    {"model_id": "gpt-4o", "provider_id": "openai", "request_count": 12}
]
merged = _merge_models_with_catalog(stats_rows, catalog_rows, collapse_models=False)
assert ("gpt-4o", "openai") in rows
assert ("gpt-4o", "openrouter") in rows
assert openrouter.request_count == 0
```

```text
test_merge_provider_scoped_legacy_stats_without_provider_does_not_hide_catalog_providers
```

Setup:

```python
stats_rows = [{"model_id": "gpt-4o", "request_count": 12}]
catalog_rows = [openai row, openrouter row]
merged = _merge_models_with_catalog(..., collapse_models=False)
assert legacy stats row exists
assert openai catalog row exists or diagnostic merge is deterministic
assert openrouter catalog row exists
```

Depending on desired display, either retain the legacy stats row as a separate traffic-only row or merge it into the first catalog row. The key requirement: do not hide provider rows.

```text
test_merge_collapsed_still_dedupes_by_model_id
```

Setup:

```python
catalog_rows = [collapsed row model_id="gpt-4o", providers=["openai", "openrouter"]]
stats_rows = [{"model_id": "gpt-4o", "provider_id": "openai", "request_count": 12}]
merged = _merge_models_with_catalog(..., collapse_models=True)
assert len([r for r in merged if r["model_id"] == "gpt-4o"]) == 1
assert merged[0]["providers"] == ["openai", "openrouter"]
```

```text
test_models_page_catalog_complete_mixed_used_unused_provider_rows
```

Full-ish route/renderer test if infrastructure exists. It should validate that the rendered HTML includes both provider rows for the same base model.

### Acceptance criteria

Provider-scoped Models page lists every `(model_id, provider_id)` catalog exposure row even when another provider for the same model has usage.

Collapsed Models page still lists one row per model ID.

Legacy stats rows without provider ID do not suppress catalog provider rows.

Existing usage columns and model-info pill lookup still work.

## Defect 2: manual model-info refresh documents provider-suffixed IDs but passes them raw

### Problem

`handle_model_info_refresh()` documents `?model_id=<id>` as accepting provider-suffixed IDs, but it passes the query value directly into `model_info.refresh_model_info()`.

`refresh_model_info()` explicitly states the caller must pass the canonical base model ID and does not strip suffixes.

The detail-page route already handles this correctly by calling `parse_model_provider(decoded_id, known_providers)` before lookup. The refresh API should use the same normalization.

### Correct behavior

`POST /api/model-info/refresh?model_id=gpt-4o/openai&force=1` should refresh canonical row `gpt-4o` with `provider_id="openai"` as an optional source filter for provider-catalog matching.

Implementation steps:

1. In `handle_model_info_refresh()`, URL-decode `model_id_filter`.
2. Load app config from `request.app.state.config`.
3. Build `known_providers = set(config.providers)` when config exists.
4. Call `parse_model_provider(decoded_id, known_providers)`.
5. Pass `lookup_id` to `refresh_model_info()` and `provider_id=_provider_suffix`.
6. Return both the requested and canonical IDs in the response.

Pseudo-code:

```python
from urllib.parse import unquote
from eggpool.routing.provider import parse_model_provider

raw_requested_id = model_id_filter
requested_id = unquote(raw_requested_id)
config = getattr(request.app.state, "config", None)
known_providers = set(config.providers) if config is not None else None
lookup_id, provider_suffix = parse_model_provider(requested_id, known_providers)

result = await model_info.refresh_model_info(
    lookup_id,
    provider_id=provider_suffix,
    source=source_filter,
    force=force,
)
```

Response:

```json
{
  "status": "ok",
  "scope": "model",
  "requested_model_id": "gpt-4o/openai",
  "model_id": "gpt-4o",
  "provider_id": "openai",
  "requested": 1,
  "refreshed": 1,
  "skipped": 0,
  "errors": 0,
  "sources_attempted": ["provider_catalog", "huggingface"],
  "sources_matched": ["provider_catalog", "huggingface"],
  "observations": 2
}
```

Keep backward compatibility by retaining `model_id` as the canonical ID, not the raw requested ID. Add `requested_model_id` for clarity.

### Source filter validation

Currently `source_filter` can be any string. The service silently ignores unknown source values because none of the `if source in (...)` branches run.

This closure pass should add strict validation in the API layer:

Allowed values:

```text
provider_catalog
openrouter
artificial_analysis
huggingface
all
```

Normalize:

```python
if source_filter in (None, "", "all"):
    source_arg = None
else:
    source_arg = source_filter
```

Reject unknown source with HTTP 400:

```json
{"error": "unknown model-info source: foo"}
```

Service behavior should also treat `source="provider_catalog"` specially. Currently provider-catalog is always attempted, and external sources run only when `source in (None, ...)`. That means `source="provider_catalog"` is already effectively provider-only; keep this behavior and document it in tests.

### Tests

API tests:

```text
test_model_info_refresh_normalizes_provider_suffixed_model_id
```

Mock/stub `model_info.refresh_model_info` and assert it receives:

```python
model_id="gpt-4o"
provider_id="openai"
force=True
```

```text
test_model_info_refresh_unsuffixed_model_id_provider_none
```

Assert unsuffixed IDs pass `provider_id=None`.

```text
test_model_info_refresh_unknown_source_returns_400
```

```text
test_model_info_refresh_source_all_maps_to_none
```

```text
test_model_info_refresh_provider_catalog_source_is_provider_only
```

Service tests:

```text
test_refresh_model_info_provider_id_limits_provider_catalog_record_selection
```

Setup provider-catalog source returns two records for same model under two providers. Calling `refresh_model_info("gpt-4o", provider_id="openrouter", force=True, source="provider_catalog")` should persist the openrouter provider observation, not openai.

### Acceptance criteria

Manual refresh accepts both base IDs and provider-suffixed IDs.

Provider suffix is passed to service as `provider_id`, not embedded in canonical `model_id`.

Unknown source filters return HTTP 400.

`source=all` and absent source both mean all enabled sources.

`source=provider_catalog` refreshes provider-native observations only.

## Optional tightening: URL encoding in model links

### Problem

`render_models()` builds links as:

```python
f'<a href="/models/{escape_attr(model_id)}?...'
```

HTML escaping is not URL path quoting. If model IDs contain `/`, `?`, `#`, spaces, or other path-sensitive characters, this may route incorrectly. Provider-suffixed IDs intentionally contain `/`; the route uses `{model_id:path}`, so slash works for provider suffixes, but arbitrary model IDs may also contain slash-like provider namespaces from external APIs.

### Preferred behavior

Use URL path quoting for model ID path segments, while preserving the deliberate provider-suffix slash only if the route expects it.

Two safe options:

Option A: quote the full model ID and rely on `{path}` plus `unquote()`:

```python
from urllib.parse import quote
href_id = quote(str(model_id), safe="")
```

Then `/models/{model_id:path}` receives `gpt-4o%2Fopenai` and `handle_model_detail()` unquotes it.

Option B: quote path parts but preserve provider suffix slash:

```python
href_id = "/".join(quote(part, safe="") for part in str(model_id).split("/"))
```

Option A is simpler and avoids ambiguity with model IDs that naturally contain slashes. The existing detail route already calls `unquote(model_id)`.

### Tests

```text
test_model_detail_link_url_encodes_slash_in_model_id
```

or adjust the existing link test if one exists.

Acceptance criterion: model detail links for provider-suffixed IDs still work, and IDs with raw URL metacharacters render safe hrefs.

## Optional tightening: info-status filter and compact summary status names

`render_models()` status filters use raw statuses such as `sparse_new`, while API compact summary sometimes maps statuses to display labels such as `sparse` or `conflict`. If `_get_model_info_summary_map()` returns compact dicts instead of `CanonicalModelInfo` objects, `info_status=sparse_new` may not match display status `sparse`.

Check the actual helper shape. If it returns compact mapped statuses, normalize both sides:

```python
_STATUS_ALIASES = {
    "sparse": "sparse_new",
    "conflict": "conflicting",
    "source-unavailable": "source_unavailable",
    "manual": "manual_override",
}
```

Use canonical status internally and display labels only in render.

Tests:

```text
test_info_status_filter_accepts_sparse_and_sparse_new
```

This is lower priority than the merge and refresh normalization bugs.

## Expected files to modify

Likely:

```text
src/eggpool/dashboard/routes.py
src/eggpool/dashboard/render.py
src/eggpool/api/model_info.py
src/eggpool/model_info/service.py
```

Potentially tests:

```text
tests/dashboard/test_models_catalog_complete.py
tests/dashboard/test_routes_models.py
tests/api/test_model_info.py
tests/model_info/test_service_refresh.py
```

If those exact files do not exist, add tests near the existing dashboard/model-info test modules.

## Verification checklist

Manual provider-scoped case:

1. Configure two providers that expose the same base model.
2. Keep `models.collapse_models = false`.
3. Send traffic through only one provider/model pair.
4. Open `/models` for the active period.
5. Confirm both provider rows appear.
6. Confirm active provider row has usage counts.
7. Confirm unused provider row has zero usage.
8. Confirm both rows link to the model detail page and show model-info pill.

Manual collapsed case:

1. Set `models.collapse_models = true`.
2. Restart or reload config as appropriate.
3. Open `/models`.
4. Confirm one row per model ID.
5. Confirm providers list/primary provider display is reasonable.
6. Confirm usage counts still appear.

Manual refresh case:

1. Call:

```bash
curl -X POST 'http://127.0.0.1:8000/api/model-info/refresh?model_id=gpt-4o/openai&force=1'
```

2. Confirm response includes:

```json
"requested_model_id": "gpt-4o/openai",
"model_id": "gpt-4o",
"provider_id": "openai"
```

3. Confirm no canonical row is created for literal `gpt-4o/openai` unless that is genuinely the catalog model ID.

4. Call with invalid source:

```bash
curl -X POST '.../api/model-info/refresh?model_id=gpt-4o&source=bad&force=1'
```

5. Confirm HTTP 400.

## Non-goals

Do not alter source adapter behavior.

Do not introduce fuzzy alias matching.

Do not change routing eligibility.

Do not change `/v1/models` exposure semantics.

Do not change the core `CatalogService` refresh logic.

Do not rework model-info schema.

## Success criteria

The Models page is truly catalog-complete in provider-scoped mode, including unused sibling providers for a used base model.

Collapsed mode remains one row per base model.

Manual refresh accepts provider-suffixed IDs and updates the canonical base model row.

Unknown refresh source filters are rejected explicitly.

Model detail links remain safe and route correctly for provider-suffixed IDs.

Regression tests cover the exact two closure bugs.
