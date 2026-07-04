# Model-Info OpenRouter Enrichment Corrective Plan

## Context

The current investigation showed that the model-info dashboard/API path is no longer failing globally. Provider-catalog ingestion, canonical-row creation, canonical storage, API summary retrieval, and dashboard summary consumption are working.

The failure mode is narrower and now reproducible:

- Catalog/provider-only model-info rows are created correctly.
- OpenRouter can be initialized and attempted by `POST /api/model-info/refresh`.
- A valid configured alias exists for `minimax-m3 -> minimax/minimax-m3`.
- OpenRouter's public `/models` payload contains the exact `id = minimax/minimax-m3`.
- After a process restart, the same forced refresh successfully matches OpenRouter and persists an OpenRouter observation.
- The canonical row moves from `sparse` to `partial`, but the detail projection remains incomplete: limits/modalities/external IDs appear, while display name and some normalized fields are dropped.
- The API detail `observations` block is synthetic and wrong: it fabricates `source_model_id`, `provider_id`, timestamp, and confidence from canonical summary/provenance rather than reading compact observation rows.

This plan is a focused corrective pass to make OpenRouter enrichment reliable, observable, and accurately projected in the dashboard/API.

## Confirmed symptoms

### Before restart / before successful binding

A forced refresh returned:

```json
{
  "sources_attempted": ["provider_catalog", "openrouter"],
  "sources_matched": ["provider_catalog"],
  "observations": 1
}
```

`model_info_source_health` contained only `provider_catalog`, even though OpenRouter was attempted. This made the operator unable to distinguish among these states:

- OpenRouter source not initialized.
- OpenRouter source fetch failed.
- OpenRouter source fetch succeeded but returned no payload.
- OpenRouter source fetch succeeded but alias resolution missed.

### Alias and OpenRouter catalog were valid

The local DB contained:

```text
model_id    provider_id  source      alias                 active
minimax-m3  opencode-go  openrouter  minimax/minimax-m3    1
```

A direct OpenRouter catalog fetch returned:

```json
{
  "id": "minimax/minimax-m3",
  "name": "MiniMax: MiniMax M3",
  "context_length": 1048576,
  "top_provider": {
    "context_length": 524288,
    "max_completion_tokens": 512000
  },
  "pricing": {
    "prompt": "0.0000003",
    "completion": "0.0000012",
    "input_cache_read": "0.00000006"
  }
}
```

### After restart / successful binding

A forced refresh returned:

```json
{
  "sources_attempted": ["provider_catalog", "openrouter"],
  "sources_matched": ["provider_catalog", "openrouter"],
  "observations": 2
}
```

The DB then contained a real OpenRouter observation:

```text
source      model_id    source_model_id       confidence
openrouter  minimax-m3  minimax/minimax-m3    0.5
```

The API detail became `partial` and showed OpenRouter-derived limits and modalities:

```json
{
  "status": "partial",
  "sparse": false,
  "sources": ["provider_catalog", "openrouter"],
  "detail": {
    "limits": {
      "external_context": 1048576,
      "external_output": 512000
    },
    "modalities": ["image", "text", "video"],
    "external_ids": {"openrouter": "minimax/minimax-m3"}
  }
}
```

But the same detail still showed:

```json
{
  "display_name": null,
  "family": null,
  "supports_tools": null
}
```

and the API detail `observations` list incorrectly displayed the OpenRouter observation as:

```json
{
  "source": "openrouter",
  "source_model_id": "minimax-m3",
  "provider_id": "opencode-go",
  "confidence": 1.0
}
```

The persisted DB row had the correct `source_model_id = minimax/minimax-m3`, so this is an API projection bug rather than a storage bug.

## Root-cause assessment

### 1. OpenRouter no-match / stale-binding state is under-instrumented

`refresh_model_info()` appends `openrouter` to `sources_attempted` when the source exists, but currently records OpenRouter source success only after a matched `or_record` is persisted. A successful OpenRouter `fetch_all()` with zero matches does not create or update the OpenRouter source-health row.

Consequence: operators see `sources_attempted` but no source-health entry and cannot tell whether the issue is fetch, catalog shape, alias lookup, or stale in-memory state.

### 2. Alias lookup is case-sensitive and lifecycle-sensitive

`get_aliases_for_model(model_id, source=...)` uses an exact `WHERE model_id = ?` lookup. The catalog can contain both `MiniMax-M3` and `minimax-m3` depending on provider. A configured alias attached to one casing does not serve the other. This is a latent bug even though `minimax-m3` eventually matched.

The fact that a restart made the same alias/source combination work suggests at least one of these lifecycle issues:

- Configured aliases were seeded only during startup and a running process did not observe newly added alias rows/config updates.
- The OpenRouter source TTL cache held an older catalog snapshot during the first forced refresh.
- Alias/canonical identity resolution had inconsistent casing across provider-specific model IDs and base model IDs.

### 3. Canonical merge drops valid normalized OpenRouter metadata

OpenRouter parsing produces `display_name`, `context_window`, `max_output_tokens`, `modalities`, `supports_tools`, `supports_reasoning`, pricing, and `source_model_id` in the normalized source record. The canonical detail builder is clearly merging limits/modalities/external IDs but is not carrying `display_name` into `detail.display_name` when provider detail lacks it.

Consequence: dashboard cards/details show enriched limits but still look empty or under-enriched.

### 4. API detail observations are synthetic and inaccurate

`_build_observations()` in the API detail path derives observations from canonical `provenance.sources` and canonical `detail.providers`, instead of reading compact rows from `model_info_observations`. It fabricates:

- `source_model_id = info.model_id`
- `provider_id = first provider`
- `observed_at = info.last_seen_at`
- `confidence = 1.0`

Consequence: API consumers and dashboard debugging surfaces show false observation metadata for external sources.

## Goals

1. Make OpenRouter enrichment deterministic for configured aliases without requiring process restart.
2. Make source health reflect successful fetches even when no model matched.
3. Preserve and expose useful OpenRouter normalized metadata in canonical detail.
4. Return accurate compact observation metadata in API detail responses.
5. Add regression coverage for the exact `minimax-m3 -> minimax/minimax-m3` path and its failure modes.
6. Avoid fuzzy matching. Keep deterministic / exact matching semantics, but improve case handling, diagnostics, and lifecycle refresh.

## Non-goals

- Do not add fuzzy alias matching or substring model matching.
- Do not make OpenRouter models routable by virtue of appearing in OpenRouter.
- Do not replace provider-native pricing/cost accounting with OpenRouter advisory pricing.
- Do not require Artificial Analysis or Hugging Face to be enabled for the model page to show OpenRouter-derived detail.

## Implementation plan

### Phase 1: Source-health and refresh diagnostics

#### 1.1 Record OpenRouter fetch success independently from match success

In `ModelInfoService.refresh_model_info()`:

- Inside `_fetch_openrouter()`, after `records = await self._openrouter_source.fetch_all()`, call:

```python
await self.record_source_success("openrouter", payload_count=len(records))
```

before alias resolution.

- Keep `sources_matched.append("openrouter")` only when an `or_record` is actually persisted.
- Do not treat zero matches as an error.

Apply the same pattern in `refresh_due_models()`:

- After bulk `or_records = await self._openrouter_source.fetch_all()`, call `record_source_success("openrouter", payload_count=len(or_records))`.
- Continue to record source errors on exceptions.

Expected result:

- `/api/model-info/sources` shows `openrouter` after any successful catalog fetch, even if a local model did not match.
- `last_payload_count` shows the OpenRouter catalog size.

#### 1.2 Add explicit match-miss diagnostics to forced refresh response

Extend the result shape for `refresh_model_info()` with a diagnostics block:

```json
{
  "source_diagnostics": {
    "openrouter": {
      "initialized": true,
      "fetched": true,
      "catalog_count": 329,
      "alias_candidates": ["minimax/minimax-m3"],
      "matched_source_model_id": null,
      "miss_reason": "no_exact_alias_match"
    }
  }
}
```

Suggested miss reasons:

- `source_not_initialized`
- `fetch_error`
- `empty_catalog`
- `no_aliases`
- `alias_not_in_catalog`
- `ambiguous_aliases`
- `matched`

Keep this field stable enough for operators/tests, but do not require dashboard use in this pass.

#### 1.3 Add debug logging around OpenRouter resolution

Add debug-level logs for:

- catalog count after `fetch_all()`
- alias candidates returned by repository lookup
- selected OpenRouter `source_model_id`
- miss reason

Avoid logging raw API payloads or API keys.

### Phase 2: Alias lookup correctness and lifecycle

#### 2.1 Make alias lookup case-insensitive by local model ID

Modify `ModelInfoRepository.get_aliases_for_model()` and `list_alias_rows_for_model()` to use case-insensitive matching on local `model_id`:

```sql
WHERE lower(model_id) = lower(?) AND source = ? AND active = 1
```

For list rows, preserve stored casing in returned rows.

Rationale: external alias binding should not silently fail because one provider reports `MiniMax-M3` and another reports `minimax-m3`.

#### 2.2 Ensure deterministic ambiguity handling after case-insensitive lookup

Case-insensitive lookup can return aliases from multiple casing variants. Preserve exact-match preference:

1. Prefer rows whose stored `model_id` exactly equals the requested model ID.
2. If exact rows exist, use only those rows.
3. If no exact rows exist, use case-insensitive rows.
4. If the resulting aliases map to multiple different OpenRouter records, treat as ambiguous and do not match.
5. If multiple rows all point to the same alias/source ID, de-duplicate and allow the match.

This avoids merging unrelated provider-cased rows while still fixing harmless casing drift.

#### 2.3 Add alias reseed path on manual refresh

Before external-source matching in `refresh_model_info()`, call `seed_configured_aliases()` or a lighter `refresh_configured_alias_for_model(lookup_id)` method.

Constraints:

- Keep this idempotent.
- Avoid re-seeding the full alias config on every row during large batch refresh if it becomes expensive.
- For `force_refresh_batch()`, seed configured aliases once at the beginning of the batch.

Rationale: a running process should not require restart for newly configured aliases to become effective if config was reloaded or if alias rows were added by an admin path.

#### 2.4 Add optional source cache bypass for forced refresh

For `force=True`, provide a way to bypass the OpenRouter TTL cache or refresh it if no match is found but an alias exists. Options:

- Add `fetch_all(force_refresh: bool = False)` to source adapters and cache layer.
- Or add `invalidate_cache()` to `OpenRouterModelInfoSource` and call it on forced refresh no-match.

Preferred conservative behavior:

1. Fetch with cache.
2. If alias candidates exist but no alias is found in the cached catalog, invalidate/re-fetch once.
3. Retry exact resolution.
4. Record diagnostics indicating `cache_retry: true`.

This protects normal scheduled refreshes from excessive outbound fetches while making manual force refresh useful for debugging newly published models.

### Phase 3: Canonical merge completeness

#### 3.1 Locate and patch `build_canonical_detail()`

Patch the canonical detail builder so source-normalized metadata is merged into detail when provider detail lacks the field.

Required merge behavior for OpenRouter observations:

- `detail.display_name`: use provider display name first; otherwise use OpenRouter normalized `display_name` if present.
- `detail.family`: preserve provider/curated family first; otherwise optional source family if available.
- `detail.limits.external_context`: use normalized `context_window`.
- `detail.limits.external_output`: use normalized `max_output_tokens`.
- `detail.modalities`: union provider and source modalities.
- `detail.supports_tools`: use source value if provider value is missing.
- `detail.supports_reasoning` / `thinking_capability`: preserve if already modeled in canonical detail; otherwise add a stable nested capability block.
- `detail.external_ids.openrouter`: use normalized/source `source_model_id`.
- `detail.pricing.openrouter`: include advisory input/output/cache-read pricing if present, clearly separated from authoritative local cost accounting.
- `detail.release_date`: use source normalized `created_at` only if a true release date is not available; otherwise consider a separate `created_at` field to avoid semantic drift.

#### 3.2 Preserve existing detail non-destructively

`build_canonical_detail()` already accepts `existing_detail`. Ensure new fields are merged non-destructively:

- Do not erase existing manual/curated fields with null source values.
- Do not downgrade populated detail when an external source temporarily fails.
- When current observation payloads are empty but existing detail has external IDs/limits from previous observations, preserve them unless the source explicitly marks withdrawal/conflict.

#### 3.3 Improve status refinement

Check `_refine_status_from_detail()` behavior after merge:

- A row with provider callability + OpenRouter context/output/external ID should be at least `partial` and `sparse=false`.
- It should not be `fresh` unless benchmark/HF/manual completeness criteria are met.
- If external context conflicts materially with provider-configured effective context, keep conflict semantics explicit.

### Phase 4: Accurate API detail observations

#### 4.1 Replace synthetic observation projection

Stop building API observations purely from `provenance.sources`.

Add repository method:

```python
async def list_compact_observations_for_model(self, model_id: str) -> list[dict[str, Any]]:
    ...
```

Return compact, raw-payload-free rows from `model_info_observations`:

- `source`
- `source_model_id`
- `provider_id`
- `observed_at`
- `confidence`
- optional `normalized_summary` fields:
  - `display_name`
  - `context_window`
  - `max_output_tokens`
  - `modalities`

Do not return `raw_json`.

#### 4.2 Make detail endpoint async observation-aware

`_detail_response(info)` is currently sync and cannot query the repo. Options:

Preferred:

- Change `_detail_response()` to accept `observations` as an argument.
- In `handle_model_info_detail()`, fetch compact observations from `model_info.repo` after loading the canonical row.
- Pass observations into `_detail_response(info, observations=observations)`.

Fallback:

- Add compact observations into `CanonicalModelInfo.detail` during merge. This is less ideal because it duplicates persisted observation rows into canonical detail.

Preferred API shape:

```json
"observations": [
  {
    "source": "provider_catalog",
    "source_model_id": "minimax-m3",
    "provider_id": "opencode-go",
    "observed_at": "2026-07-04T15:42:37.826380+00:00",
    "confidence": 1.0
  },
  {
    "source": "openrouter",
    "source_model_id": "minimax/minimax-m3",
    "provider_id": null,
    "observed_at": "2026-07-04T15:42:40.344754+00:00",
    "confidence": 0.5
  }
]
```

#### 4.3 Keep summary endpoint compact

Do not add observations to `GET /api/model-info` summary list. Keep detailed observations only on `GET /api/model-info/{model_id}` to avoid bloating the dashboard list payload.

### Phase 5: Dashboard/model-page polish

#### 5.1 Verify model page uses enriched summary

The dashboard model list currently only needs compact summary values, but after merge fixes it should show better status/source pills. Verify:

- `minimax-m3` shows `partial` rather than `sparse`.
- Source pill includes OpenRouter.
- Provider remains `opencode-go`.
- Detail drawer/link shows external context/output and display name.

#### 5.2 Add small diagnostic fields where useful

On model detail page, show:

- external IDs
- external context/output
- source list
- last observed timestamps per source

Keep this minimal; avoid turning the runtime page back into a dense phase-history dashboard.

### Phase 6: Tests

#### 6.1 Unit tests for OpenRouter parsing

Add or extend tests for `OpenRouterModelInfoSource` parsing:

- Parses `id = minimax/minimax-m3`.
- Parses `name = MiniMax: MiniMax M3` into `display_name`.
- Parses `context_length` into `context_window`.
- Parses `top_provider.max_completion_tokens` into `max_output_tokens`.
- Parses modalities and supported parameters when present.
- Parses pricing into advisory `input_price_per_1k` / `output_price_per_1k` with existing units intact.

#### 6.2 Unit tests for alias resolution

Add tests for `resolve_openrouter_record()` and repository alias lookup:

- Exact configured alias resolves.
- Case variant local model ID resolves when unambiguous.
- Duplicate rows pointing to the same source alias de-duplicate.
- Multiple aliases pointing to different OpenRouter IDs are ambiguous and do not match.
- Provider-catalog fallback aliases still work.
- No fuzzy/substr matching is introduced.

#### 6.3 Service tests for refresh behavior

Add a fake OpenRouter source/client returning a catalog containing `minimax/minimax-m3` and assert:

- `refresh_model_info("minimax-m3", force=True)` attempts and matches OpenRouter.
- It persists two observations: provider catalog + OpenRouter.
- It records OpenRouter source success with payload count.
- Canonical status becomes `partial`, `sparse=false`.
- Canonical detail includes:
  - `display_name = MiniMax: MiniMax M3`
  - `limits.external_context = 1048576`
  - `limits.external_output = 512000`
  - `external_ids.openrouter = minimax/minimax-m3`
  - modalities include text/image/video where source provides them.

#### 6.4 API tests for observation projection

Add endpoint-level tests:

- `GET /api/model-info/minimax-m3` returns real `observations` from DB.
- OpenRouter observation has `source_model_id = minimax/minimax-m3`.
- OpenRouter observation does not inherit provider ID from provider catalog.
- OpenRouter confidence reflects persisted observation confidence, not hardcoded `1.0`.
- Raw payloads are not present in API response.

#### 6.5 Regression test for no-match observability

Configure OpenRouter to return a catalog that does not include the alias.

Assert:

- `sources_attempted` includes `openrouter`.
- `sources_matched` does not include `openrouter`.
- `source_diagnostics.openrouter.miss_reason = alias_not_in_catalog` or equivalent.
- `model_info_source_health` contains/updates `openrouter` with successful fetch and payload count.
- No OpenRouter observation row is persisted.

### Phase 7: Operator verification commands

After implementation, verify with the existing `usage.sqlite3` database or a test DB.

#### 7.1 Force refresh

```bash
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"

curl -sS -X POST "$BASE/api/model-info/refresh?model_id=minimax-m3&force=1" \
  | python3 -m json.tool
```

Expected:

```json
{
  "sources_attempted": ["provider_catalog", "openrouter"],
  "sources_matched": ["provider_catalog", "openrouter"],
  "observations": 2
}
```

#### 7.2 Check source health

```bash
sqlite3 usage.sqlite3 <<'SQL'
.headers on
.mode column
SELECT source, enabled, last_success_at, last_error_at, failure_count, last_payload_count
FROM model_info_source_health
ORDER BY source;
SQL
```

Expected:

- `openrouter` row exists after any successful OpenRouter fetch.
- `last_payload_count` is non-null / greater than zero when fetch succeeded.

#### 7.3 Check canonical detail

```bash
sqlite3 usage.sqlite3 <<'SQL'
.headers on
.mode column
SELECT
  model_id,
  status,
  sparse,
  json_extract(detail_json, '$.display_name') AS display_name,
  json_extract(detail_json, '$.limits.external_context') AS external_context,
  json_extract(detail_json, '$.limits.external_output') AS external_output,
  json_extract(detail_json, '$.external_ids.openrouter') AS openrouter_id,
  json_extract(detail_json, '$.modalities') AS modalities
FROM model_info_canonical
WHERE lower(model_id) = lower('minimax-m3');
SQL
```

Expected:

- `status = partial`
- `sparse = 0`
- `display_name = MiniMax: MiniMax M3`
- `external_context = 1048576`
- `external_output = 512000`
- `openrouter_id = minimax/minimax-m3`

#### 7.4 Check API detail

```bash
curl -sS "$BASE/api/model-info/minimax-m3" | python3 -m json.tool
```

Expected:

- `detail.display_name` is populated.
- `detail.limits.external_context` is populated.
- `detail.external_ids.openrouter` is populated.
- `observations[]` includes real compact rows from DB.
- The OpenRouter observation uses `source_model_id = minimax/minimax-m3`.

## Risk notes

- Case-insensitive alias lookup can increase ambiguity. Mitigate by exact-case preference and de-duplication before resolution.
- Recording source success for no-match fetches changes source-health semantics. This is desirable: source health should represent source availability, not local model match success.
- Adding advisory OpenRouter pricing to canonical detail must not affect cost calculations. Keep pricing under a clearly named source-specific detail block.
- Avoid storing raw source payloads in API responses. The repository can store raw JSON, but API detail should remain compact.

## Acceptance criteria

The pass is complete when:

1. `minimax-m3` force refresh matches OpenRouter without requiring restart after aliases are present.
2. OpenRouter source health records successful fetches even when no local model matched.
3. `GET /api/model-info/minimax-m3` shows OpenRouter-enriched display name, limits, modalities, and external ID.
4. API detail observations reflect persisted `model_info_observations` rows rather than synthetic provenance-derived rows.
5. Case variants such as `MiniMax-M3` and `minimax-m3` do not silently lose configured aliases when unambiguous.
6. Regression tests cover success, no-match, ambiguity, API projection, and source-health cases.
