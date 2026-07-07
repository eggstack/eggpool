# Model-Info Dashboard Join Corrective Plan

## Context

The OpenRouter/model-info enrichment path is now mostly working:

- `GET /api/model-info` returns canonical summaries.
- `POST /api/model-info/refresh?model_id=minimax-m3&force=1` can match OpenRouter.
- Canonical rows can move from `sparse` to `partial`.
- `GET /api/model-info/minimax-m3` can show OpenRouter-derived limits, modalities, external IDs, and compact real observations.

However, further live testing still shows no model info displayed on the dashboard `/models` page. Based on the current repo, the remaining failure is almost certainly not storage or enrichment. It is in the dashboard list-page join from rendered model rows to canonical model-info summaries.

The likely failing chain is:

1. `handle_models()` builds catalog rows.
2. It builds `requested_ids` from each row's `base_model_id` or `model_id`.
3. It calls `model_info_service.get_summary_map(requested_ids)`.
4. It passes the compact summary map into `render_models()`.
5. `render_models()` looks up `mi_map.get(base_id) or mi_map.get(model_id)` for each row.
6. If that lookup misses, `_render_model_info_pill(None)` renders the unknown dash pill.

The API can therefore be correct while the dashboard still displays no model-info if dashboard rows use provider-suffixed IDs or if catalog row construction silently fails.

## Current suspected failure points

### 1. Provider-scoped row construction may silently return no catalog rows

`_get_provider_scoped_catalog_rows()` currently calls:

```python
provider_entries = catalog.cache.get_provider_model_entries()
```

inside a broad `except Exception: return []` block.

If `ModelCatalogCache` does not expose `get_provider_model_entries()` in the current runtime, or if that method throws, the dashboard loses catalog rows silently. `requested_ids` then becomes empty or incomplete, and the page falls back to usage stats rows only. Usage stats rows may not include `base_model_id`, which makes canonical model-info lookup fail.

This failure would not trigger the existing model-info degraded banner because the model-info service itself is still attached and `get_summary_map()` may still succeed.

### 2. Provider-suffixed model IDs may be used as canonical lookup keys

The catalog exposure layer creates provider-suffixed model IDs like:

```python
model_copy["model_id"] = f"{model_id}/{provider_id}"
model_copy["base_model_id"] = model_id
model_copy["provider_id"] = provider_id
```

The dashboard list renderer expects `base_model_id` to contain the unsuffixed canonical key:

```python
model_id = row.get("model_id", "")
base_id = row.get("base_model_id", "") or model_id
mi_info = mi_map.get(base_id) or mi_map.get(model_id)
```

If provider-scoped rows arrive with `model_id = "minimax-m3/opencode-go"` and `base_model_id` missing or incorrectly set to the same suffixed value, the renderer looks up `minimax-m3/opencode-go` while canonical model-info rows are keyed as `minimax-m3`.

### 3. Summary fetch uses requested keys, so wrong requested IDs propagate downstream

`get_canonical_many(model_ids)` returns a dict keyed by the requested IDs. If `handle_models()` requests `minimax-m3/opencode-go`, but only `minimax-m3` exists as canonical, the lookup can miss unless the repository or caller normalizes the suffix first. The existing repository case-insensitive handling does not solve provider-suffix mismatch.

### 4. Existing warnings only cover model-info service failures

`ModelInfoDashboardState.degraded_reason` currently covers:

- `service_unattached`
- `fetch_error`

It does not cover:

- catalog row construction failure
- catalog rows empty while catalog service exists
- summary rows available but zero dashboard rows matched
- requested IDs were provider-suffixed and did not match canonical rows

This leaves the operator seeing a table full of unknown info pills with no diagnostic clue.

## Goals

1. Make `/models` join model-info summaries by canonical unsuffixed model ID for both collapsed and provider-scoped rows.
2. Stop silently swallowing catalog-row construction failures.
3. Add a robust model-row normalization helper shared by `handle_models()`, filters, and renderer fallbacks.
4. Add dashboard diagnostics that distinguish model-info service failure from catalog/join failure.
5. Add tests that reproduce the exact API-correct/dashboard-empty state.

## Non-goals

- Do not revisit OpenRouter enrichment logic except where live verification needs it.
- Do not add fuzzy model-info matching.
- Do not change routing semantics or `/v1/models` exposure semantics.
- Do not expand the dashboard with large new panels. Keep diagnostics compact and only shown on degraded/join-failure states.

## Implementation plan

### Phase 1: Add canonical dashboard model-id normalization

Create a small helper in `src/eggpool/dashboard/routes.py` or a dedicated dashboard utility module:

```python
def _normalize_dashboard_model_row(
    row: dict[str, Any],
    *,
    known_providers: set[str] | None,
) -> dict[str, Any]:
    ...
```

Required behavior:

1. Copy the row; do not mutate caller-owned dicts unexpectedly unless the caller explicitly reassigns.
2. Read `model_id` as a string.
3. Read existing `base_model_id` if present and non-empty.
4. Parse `model_id` with `parse_model_provider(model_id, known_providers)`.
5. Decide canonical base ID in this order:
   - existing unsuffixed `base_model_id` if present and not equal to a provider-suffixed ID
   - parsed base ID from `model_id`
   - literal `model_id`
6. Decide provider ID in this order:
   - existing `provider_id` if present
   - parsed provider suffix if present
   - empty string / `None` depending on current row shape conventions
7. Write:
   - `base_model_id = canonical_base_id`
   - `provider_id = provider_id`
   - optionally `_model_info_lookup_id = canonical_base_id`
   - optionally `_model_id_was_suffixed = True/False`

Add a companion helper:

```python
def _known_provider_ids_from_config(config: Any | None) -> set[str] | None:
    ...
```

Use the same provider set semantics already used by model detail/API routes.

### Phase 2: Fix provider-scoped catalog row construction

#### Option A: Add `ModelCatalogCache.get_provider_model_entries()`

If the intended dashboard API is direct access to provider entries, implement the missing accessor on `ModelCatalogCache`:

```python
def get_provider_model_entries(self) -> dict[tuple[str, str], dict[str, Any]]:
    """Return provider-scoped model metadata keyed by (base_model_id, provider_id)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (model_id, provider_id), entry in self._provider_models.items():
        if model_id == DEPRECATED_MODEL_ID:
            continue
        if not entry.get("protocol"):
            # Preserve current dashboard availability semantics carefully.
            # If the dashboard needs unavailable rows too, return them with protocol=None.
            pass
        copied = self.get_provider_model_entry(model_id, provider_id) or dict(entry)
        out[(model_id, provider_id)] = copied
    return out
```

Important: decide whether unresolved provider rows should be returned. The current dashboard code represents availability with `protocol` and `catalog_status`, so returning unresolved rows is acceptable and useful. Do not filter them unless the existing catalog exposure contract requires it.

#### Option B: Stop using `get_provider_model_entries()` and use existing public methods

If the repo already has a preferred exposure API, rewrite `_get_provider_scoped_catalog_rows()` to use it. For example:

```python
entries = catalog.cache.get_provider_suffixed_models(...)
```

or another existing method that returns provider-suffixed entries with `base_model_id` and `provider_id` already populated.

If using provider-suffixed entries, preserve both IDs correctly:

```python
literal_model_id = entry["model_id"]              # minimax-m3/opencode-go
base_model_id = entry.get("base_model_id")        # minimax-m3
provider_id = entry.get("provider_id")            # opencode-go
```

Then call `_normalize_dashboard_model_row()` before appending.

#### Required behavior regardless of option

- Do not catch all exceptions and return `[]` silently.
- At minimum, log the exception with stack trace and return a diagnostic marker to the route.
- Prefer introducing a `CatalogRowsState` dataclass similar to `ModelInfoDashboardState`:

```python
@dataclass(frozen=True, slots=True)
class CatalogRowsState:
    rows: list[dict[str, Any]]
    degraded_reason: str | None = None
    error_class: str | None = None
```

If keeping the return type simple for now, log loudly and add a route-level warning when `catalog is not None` but no rows were produced.

### Phase 3: Normalize rows before requested-id collection and merge

In `handle_models()`:

1. Build `known_providers` from app config.
2. Normalize every catalog row immediately after `_get_catalog_rows()`.
3. Normalize every stats row returned from `stats.get_model_stats()` before merging.
4. Build `requested_ids` only from normalized canonical base IDs:

```python
requested_ids = {
    str(row.get("_model_info_lookup_id") or row.get("base_model_id") or row.get("model_id"))
    for row in catalog_rows
    if ...
}
```

5. If `models` contains usage rows for IDs not in catalog rows, include their normalized lookup IDs too before calling `get_summary_map()`.

This matters because a model with usage but missing from current catalog may still have canonical model-info.

Implementation detail: currently `handle_models()` calls `asyncio.gather(stats.get_model_stats(...), _get_model_info_summary_state(...))`, but requested IDs are computed before stats rows are available. To include stats-only rows, either:

- fetch stats and model-info sequentially for correctness; or
- keep the current concurrent path but add a second pass for stats-only rows only when model-info misses are high; or
- request all canonical summaries by passing `model_ids=None`.

Preferred for simplicity and correctness: pass `model_ids=None` for the dashboard list until the join is stable. The canonical table is small in current deployments (tens/hundreds of rows), and this avoids under-requesting. If scale becomes an issue later, add a two-phase optimized path.

### Phase 4: Harden renderer lookup fallback

In `render_models()`:

1. Accept row-level `_model_info_lookup_id` if present.
2. Parse `model_id` as a fallback if known providers are available, or pass precomputed normalized keys from the route.
3. Lookup in this order:

```python
lookup_id = row.get("_model_info_lookup_id")
base_id = row.get("base_model_id")
model_id = row.get("model_id")
mi_info = (
    mi_map.get(str(lookup_id))
    or mi_map.get(str(base_id))
    or mi_map.get(str(model_id))
)
```

Because render code should remain simple and not depend on app config, the route should precompute `_model_info_lookup_id`.

4. Add a debug-only marker or data attribute to the pill cell, for example:

```html
<td data-model-id="minimax-m3/opencode-go" data-model-info-key="minimax-m3">...</td>
```

This makes grep-based live debugging easier. Keep it harmless and escaped.

### Phase 5: Add dashboard join diagnostics

Add low-noise diagnostics in the `/models` page when the join fails.

Compute in `handle_models()` after `filtered_rows` and `model_info_summary_map` are known:

- `model_info_summary_count`
- `dashboard_model_row_count`
- `model_info_matched_row_count`
- `model_info_unmatched_row_count`
- `unmatched_sample` of up to 5 `{model_id, base_model_id, lookup_id, provider_id}`

Add to `ModelInfoDashboardState` or a sibling object.

Render a warning only when suspicious:

- summary_count > 0
- row_count > 0
- matched_row_count == 0

Message:

> Model info is loaded but did not match any dashboard rows. This usually means provider-suffixed model IDs are not being normalized to canonical IDs.

This would have exposed the current failure immediately.

### Phase 6: Tests

Add tests in `tests/unit/test_dashboard.py` or a new `tests/unit/test_dashboard_model_info_join.py`.

#### Test 1: Renderer joins provider-suffixed row to canonical summary

Input:

```python
models = [
    {
        "model_id": "minimax-m3/opencode-go",
        "base_model_id": "minimax-m3",
        "provider_id": "opencode-go",
        "request_count": 0,
        ...
    }
]
model_info_map = {
    "minimax-m3": {
        "model_id": "minimax-m3",
        "status": "partial",
        "sparse": False,
        "summary": "Callable via opencode-go.",
        "sources": ["provider_catalog", "openrouter"],
        "last_refreshed_at": "2026-07-04T15:42:40Z",
    }
}
```

Assert:

- `pill-partial` appears.
- `No model info available` does not appear for that row.
- `Sources: provider_catalog, openrouter` appears in tooltip/aria text.

#### Test 2: Route normalizes suffixed stats-only row before model-info lookup

Use a lightweight fake `stats`, fake `model_info_service`, and fake catalog if existing dashboard tests already use such patterns.

Assert that `model_info_service.get_summary_map()` receives `{"minimax-m3"}`, not `{"minimax-m3/opencode-go"}`.

#### Test 3: Catalog row construction does not silently disappear

If implementing `get_provider_model_entries()`:

- Seed `ModelCatalogCache` with `_provider_models[("minimax-m3", "opencode-go")]`.
- Call `_get_provider_scoped_catalog_rows()`.
- Assert one row is returned with:
  - `model_id` set according to dashboard convention
  - `base_model_id = "minimax-m3"`
  - `provider_id = "opencode-go"`

If using public provider-suffixed exposure instead:

- Assert provider-suffixed entries preserve unsuffixed `base_model_id`.

#### Test 4: Join diagnostics trigger on all-miss state

Input:

- `model_info_summary_map` has at least one canonical row.
- dashboard rows have non-matching suffixed IDs and no normalized lookup ID.

Assert:

- warning text appears.
- unmatched sample contains the rendered row ID and intended lookup ID.

#### Test 5: No false warning on normal empty model-info state

Input:

- no canonical summaries yet
- dashboard rows exist

Assert:

- no join-failure warning, because model-info may simply not have populated yet.

### Phase 7: Live verification commands

Run these after implementation.

#### 7.1 Confirm API still has model-info

```bash
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"

curl -sS "$BASE/api/model-info" | python3 - <<'PY'
import json, sys
payload = json.load(sys.stdin)
rows = payload.get('data', [])
print('summary_rows', len(rows))
print('sample_ids', [r.get('model_id') for r in rows[:10]])
PY
```

Expected: non-zero rows.

#### 7.2 Confirm dashboard now renders model-info pills

```bash
curl -sS "$BASE/models" \
  | grep -Eo 'pill-(fresh|partial|sparse|stale|conflict|unmatched|unknown)|No model info available|Model info is loaded but did not match' \
  | sort | uniq -c
```

Expected:

- `pill-partial` or `pill-sparse` present for known canonical rows.
- `pill-unknown` may still exist for true unmatched rows, but should not dominate if API has matching canonical rows.
- No join-failure warning.

#### 7.3 Inspect rendered links and model-info keys

If data attributes are added:

```bash
curl -sS "$BASE/models" \
  | grep -Eo 'data-model-id="[^"]+"|data-model-info-key="[^"]+"|href="/models/[^"]+' \
  | head -80
```

Expected:

- provider-suffixed model links may exist
- `data-model-info-key` should be unsuffixed, e.g. `minimax-m3`

#### 7.4 Check specific model detail still works

```bash
curl -sS "$BASE/api/model-info/minimax-m3" | python3 -m json.tool | head -160
```

Expected:

- `status = partial` or better
- sources include `openrouter` after refresh
- detail external IDs and observations are correct

## Acceptance criteria

This fix is complete when:

1. `/api/model-info` has canonical rows and `/models` displays non-unknown model-info pills for those rows.
2. Provider-suffixed dashboard rows join to unsuffixed canonical model-info rows.
3. Catalog row construction failures are logged and surfaced as dashboard diagnostics instead of silently returning an empty list.
4. `requested_ids` passed to model-info summary lookup are canonical base IDs, not provider-suffixed public IDs.
5. Renderer fallback uses `_model_info_lookup_id` / `base_model_id` before literal `model_id`.
6. Tests cover API-correct/dashboard-missing regression with provider-suffixed rows.
7. Live verification shows model-info pills on `/models` and no join-failure warning.

## Suggested final commit message

```text
Fix dashboard model-info joins for provider-scoped rows
```

## Notes for implementer

Keep the patch narrow. The enrichment/storage/API path has already been heavily touched and is no longer the primary suspect. This pass should focus on the dashboard list page and only the join between rendered model rows and canonical model-info summaries.
