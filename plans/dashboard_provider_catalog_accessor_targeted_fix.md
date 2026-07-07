# Dashboard Provider Catalog Accessor Targeted Fix Plan

## Context

The dashboard model-info join corrective pass landed the right shape of fix for `/models`:

- rows are normalized through `_normalize_dashboard_model_row()`;
- stats rows and catalog rows get `_model_info_lookup_id`;
- `render_models()` looks up `_model_info_lookup_id` before `base_model_id` and literal `model_id`;
- the summary fetch uses `model_ids=None` so dashboard join does not under-request canonical rows;
- join diagnostics warn when model-info summaries exist but no rendered rows match.

However, the current implementation still has a concrete gap in the provider-scoped catalog path. `_get_provider_scoped_catalog_rows()` calls:

```python
provider_entries = catalog.cache.get_provider_model_entries()
```

but the current `ModelCatalogCache` implementation appears to expose only single-entry accessors such as `get_provider_model_entry()`, `get_model_for_provider()`, and related routing helpers. There is no visible `get_provider_model_entries()` method in the cache implementation.

This means real provider-scoped `/models` can still hit `AttributeError`, produce `CatalogRowsState(degraded_reason="fetch_error")`, and lose catalog-complete rows. Stats-only rows may now join model-info correctly, but zero-usage catalog rows will still be absent, and the dashboard remains degraded.

The next fix should be narrow: add the missing provider-scoped cache accessor or rewrite the dashboard path to use an existing public API. The simplest and least invasive target is adding `ModelCatalogCache.get_provider_model_entries()` with semantics matching what the dashboard already expects.

## Goal

Make `_get_provider_scoped_catalog_rows()` work against the real `ModelCatalogCache` so `/models` can build provider-scoped catalog rows and join them to canonical model-info summaries.

## Non-goals

- Do not change model-info enrichment, OpenRouter matching, or canonical merge logic.
- Do not change routing availability semantics.
- Do not change `/v1/models` exposure shape.
- Do not add broad dashboard UI changes.
- Do not remove the new join diagnostics; keep them as guardrails.

## Required behavior

`ModelCatalogCache.get_provider_model_entries()` should:

1. Return a mapping keyed by `(base_model_id, provider_id)`.
2. Include provider-scoped entries from `_provider_models`.
3. Exclude the deprecated placeholder model ID.
4. Preserve unresolved entries with `protocol=None` so the dashboard can render them as unavailable instead of hiding them.
5. Return shallow copies, not internal mutable dicts.
6. Apply configured capability overrides when config is attached, matching `get_provider_model_entry()` behavior.
7. Avoid global fallback. This accessor is specifically for provider-scoped rows, so it should not borrow another provider's protocol or metadata.
8. Be deterministic: return keys sorted by model ID and provider ID, or at least construct in deterministic order if callers iterate it directly.

## Implementation plan

### Phase 1: Add cache accessor

In `src/eggpool/catalog/cache.py`, add a method near `get_provider_model_entry()`:

```python
def get_provider_model_entries(self) -> dict[tuple[str, str], dict[str, Any]]:
    """Return provider-scoped model metadata keyed by (model_id, provider_id).

    This is read-only dashboard/catalog introspection. It exposes one
    row per provider-specific cache entry without global fallback so
    the dashboard can render provider-scoped availability and join each
    row to canonical model-info using the unsuffixed base model ID.
    """
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for model_id, provider_id in sorted(self._provider_models):
        if model_id == DEPRECATED_MODEL_ID:
            continue
        entry = self._provider_models.get((model_id, provider_id))
        if entry is None:
            continue
        if self._config is None:
            result[(model_id, provider_id)] = dict(entry)
        else:
            overridden = self.get_provider_model_entry(model_id, provider_id)
            result[(model_id, provider_id)] = dict(overridden or entry)
    return result
```

Notes:

- Use `sorted(self._provider_models)` for stable row order.
- Do not filter `protocol=None`; dashboard uses that to compute `available=False` / `catalog_status="unavailable"`.
- Do not call `get_model_for_provider()` because it can fall back to the global row. The dashboard needs exact provider rows.
- `get_provider_model_entry()` is acceptable because it returns the exact provider row with overrides and no global fallback.

### Phase 2: Verify dashboard row construction against real cache

The existing `_get_provider_scoped_catalog_rows()` can remain structurally unchanged once the accessor exists. It should now receive a real provider-entry mapping and emit rows like:

```python
{
    "model_id": "minimax-m3",
    "base_model_id": "minimax-m3",
    "provider_id": "opencode-go",
    "providers": ["opencode-go"],
    "available": True,
    "catalog_status": "available",
    "protocol": "openai",
}
```

After `handle_models()` normalization, the row should also carry:

```python
{
    "_model_info_lookup_id": "minimax-m3",
    "_model_id_was_suffixed": False,
}
```

If a future path passes a suffixed public model ID, normalization still handles it.

### Phase 3: Fix or strengthen tests

The existing test `test_get_provider_scoped_catalog_rows_returns_state_dataclass()` should pass once the accessor is present. Confirm it uses a real `ModelCatalogCache`, seeds `_provider_models[("minimax-m3", "opencode-go")]`, calls `_get_provider_scoped_catalog_rows()`, and receives one row.

Add or adjust tests as needed:

#### 3.1 Cache accessor direct test

Add a direct test in `tests/unit/test_catalog_cache.py` or `tests/unit/test_dashboard_model_info_join.py`:

```python
def test_model_catalog_cache_provider_model_entries_returns_exact_provider_rows():
    cache = ModelCatalogCache()
    cache._provider_models[("minimax-m3", "opencode-go")] = {
        "model_id": "minimax-m3",
        "protocol": "openai",
        "capabilities": {},
    }
    rows = cache.get_provider_model_entries()
    assert list(rows) == [("minimax-m3", "opencode-go")]
    assert rows[("minimax-m3", "opencode-go")]["protocol"] == "openai"
```

#### 3.2 Includes unresolved provider rows

```python
def test_provider_model_entries_include_unresolved_rows_for_dashboard_availability():
    cache._provider_models[("minimax-m3", "opencode-go")] = {"protocol": None}
    rows = cache.get_provider_model_entries()
    assert ("minimax-m3", "opencode-go") in rows
```

#### 3.3 Excludes deprecated placeholder

```python
def test_provider_model_entries_excludes_deprecated_placeholder():
    cache._provider_models[(DEPRECATED_MODEL_ID, "opencode-go")] = {"protocol": "openai"}
    assert cache.get_provider_model_entries() == {}
```

#### 3.4 Returns copies

```python
def test_provider_model_entries_returns_copies():
    rows = cache.get_provider_model_entries()
    rows[("minimax-m3", "opencode-go")]["protocol"] = "mutated"
    assert cache._provider_models[("minimax-m3", "opencode-go")]["protocol"] == "openai"
```

#### 3.5 Applies provider capability overrides if easy to construct

If existing tests already have config fixture helpers, add a targeted override test. If this is too much fixture work for this pass, rely on `get_provider_model_entry()` behavior and keep the method implementation small.

### Phase 4: Live verification

After implementing, run targeted tests:

```bash
pytest tests/unit/test_dashboard_model_info_join.py tests/unit/test_dashboard_models_catalog.py
```

Then run a broader dashboard/model-info subset:

```bash
pytest tests/unit/test_model_info_openrouter_enrichment.py \
       tests/unit/test_model_info_alias_resolution.py \
       tests/unit/test_dashboard.py \
       tests/unit/test_dashboard_model_info_join.py \
       tests/unit/test_dashboard_models_catalog.py
```

Against a live instance:

```bash
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"

curl -sS "$BASE/models" \
  | grep -Eo 'pill-(fresh|partial|sparse|stale|conflict|unmatched|unknown)|Model info is loaded but did not match|Model info unavailable' \
  | sort | uniq -c
```

Expected:

- no catalog fetch-error warning;
- no all-miss join warning when `/api/model-info` has matching canonical rows;
- at least some non-unknown model-info pills, e.g. `pill-partial` or `pill-sparse`.

Also inspect the debug attributes:

```bash
curl -sS "$BASE/models" \
  | grep -Eo 'data-model-id="[^"]+"|data-model-info-key="[^"]+"|data-provider-id="[^"]+"' \
  | head -80
```

Expected:

- `data-model-info-key` is unsuffixed canonical ID, e.g. `minimax-m3`;
- provider is carried separately in `data-provider-id`.

## Acceptance criteria

This targeted pass is complete when:

1. `ModelCatalogCache.get_provider_model_entries()` exists and returns exact provider-scoped rows.
2. `_get_provider_scoped_catalog_rows()` no longer degrades with `AttributeError` against the real cache.
3. Provider-scoped catalog rows appear on `/models` even when request stats are empty.
4. Rows join to model-info summaries through `_model_info_lookup_id` / `base_model_id`.
5. The existing dashboard join tests pass, especially `test_get_provider_scoped_catalog_rows_returns_state_dataclass()`.
6. Live `/models` shows non-unknown model-info pills when `/api/model-info` contains canonical rows.

## Suggested final commit message

```text
Add provider-scoped catalog entries accessor for dashboard joins
```

## Notes for implementer

Keep this patch narrow. The previous dashboard join commit already added normalization, rendering fallbacks, diagnostics, and tests. The missing piece is the cache accessor contract that the route now depends on. Avoid changing route/render behavior unless the accessor test reveals a concrete mismatch.
