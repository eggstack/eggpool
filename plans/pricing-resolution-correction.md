# Pricing Resolution Correction Plan

## Purpose

EggPool currently records and displays estimated request cost by summing persisted `requests.cost_microdollars`. That design is correct as a durable accounting primitive, but the price resolution path can produce materially inflated totals when a provider does not expose usable pricing metadata or when only partial pricing is available. The observed case is roughly 30 million MiMo 2.5 tokens displaying about $92, which is consistent with the current generic fallback pricing path rather than cheap provider/model-specific API pricing.

The goal of this plan is to make EggPool prefer actual provider/API cost data, avoid long-lived manual overrides, prevent accidental similarly named model conflation such as MiMo 2.5 versus MiMo 2.5 Pro, and clearly expose when displayed costs are exact, derived from a trusted catalog, partial, or heuristic.

## Current Behavior Summary

The current flow is roughly:

1. The catalog normalizer stores each upstream model row using the exact upstream `id` as `model_id` and preserves extra upstream fields in `source_metadata`.
2. The catalog service attempts to insert a model price snapshot by inspecting TOML overrides first, then upstream metadata.
3. Upstream `source_metadata.pricing.prompt` and `source_metadata.pricing.completion` are parsed as per-token prices when present.
4. Cache read/write prices are only parsed from `cache_read_per_million_microdollars` and `cache_write_per_million_microdollars` metadata fields.
5. `CostCalculator.calculate_cost()` uses the latest snapshot for the exact `(model_id, provider_id)` pair.
6. If required rates are missing, especially for nonzero cache read/write tokens, the calculator may take the maximum of the partial calculated cost and the generic fallback estimate.
7. `_estimate_cost()` currently uses `$3/M input` and `$15/M output`, expressed as `$0.003/1K input` and `$0.015/1K output`.
8. Dashboard totals are aggregates over persisted request cost rows; the dashboard is not the primary source of the pricing error.

This means the most likely failure modes are missing provider-specific price snapshots, partial price snapshots that lack cache rates, or historical rows finalized before correct snapshots were available.

## Design Goals

1. Provider-authoritative pricing should be the first-class source of truth whenever the selected provider exposes actual API pricing.
2. Manual TOML overrides should remain available for emergency/operator correction, but should not be the normal maintenance path.
3. External catalogs such as OpenCode Zen or OpenRouter should be supported as curated fallback sources when the selected provider does not expose pricing.
4. External catalog lookup must be deterministic and safe. Do not use fuzzy matching to resolve model IDs across catalogs.
5. Pro/non-Pro and similarly named variants must never be conflated.
6. Missing cache pricing must not cause known cheap input/output pricing to be replaced wholesale by the global unknown-model fallback.
7. Cost provenance should be visible in stats and dashboard UI so operators can distinguish actual/derived cost from heuristic estimates.
8. Historical cost rows should be correctable through an explicit backfill/recompute command, not silently mutated during normal reads.

## Non-Goals

1. Do not implement live billing reconciliation against provider account invoices in this phase.
2. Do not attempt fuzzy or semantic model-name matching.
3. Do not remove operator overrides; demote them from the primary path to an explicit escape hatch.
4. Do not make dashboard reads dynamically recompute costs. Persisted request rows should remain the durable source of stats.

## Proposed Architecture

Add a layered pricing resolution pipeline with explicit provenance.

Resolution order:

1. Provider model metadata or provider pricing endpoint.
2. Provider-specific static metadata bundled in EggPool, if the provider is known and stable.
3. External catalog fallback, preferably OpenCode Zen for EggPool's target use case, then OpenRouter where useful.
4. Operator TOML override, used as an explicit emergency source and clearly marked as `config`.
5. Local heuristic fallback, clearly marked as estimated and low-confidence.

The exact order between operator override and external catalog can be configurable. The recommended default is:

1. Provider-authoritative upstream.
2. Operator override, if present, because the operator may be correcting a known provider issue.
3. Curated external catalog fallback.
4. Heuristic fallback.

The key point is that operator overrides should not be required for ordinary known public model prices.

## Data Model Changes

### Price Snapshot Provenance

Extend price snapshot metadata so a row can say where the value came from with enough granularity to debug it later.

Recommended fields, either as new columns or structured `source_metadata` if the existing schema favors JSON-like metadata:

- `source`: existing broad source, keep values such as `upstream`, `config`, `mixed`.
- `source_detail`: more specific value, e.g. `provider_models`, `provider_pricing_endpoint`, `opencode_zen`, `openrouter`, `static_catalog`, `operator_override`.
- `source_confidence`: e.g. `authoritative`, `curated_alias`, `exact_external_id`, `operator`, `heuristic`.
- `source_model_id`: external catalog model ID when applicable.
- `source_provider_id`: external catalog/provider ID when applicable.
- `resolved_at`: timestamp for resolution.
- `stale_after`: optional timestamp or TTL boundary for catalog-sourced prices.

If schema churn should be minimized, encode the extra details in a `metadata_json` column or a companion table. Avoid overloading the single `source` string with too many compound values.

### Model Pricing Alias Table

Add a deterministic alias registry for mapping provider-native model IDs to external catalog model IDs.

Suggested table:

```sql
CREATE TABLE IF NOT EXISTS model_pricing_aliases (
    provider_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    catalog_source TEXT NOT NULL,
    catalog_model_id TEXT NOT NULL,
    confidence TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, upstream_model_id, catalog_source)
);
```

This can be seeded from code or migrations with known safe aliases.

Examples:

```text
provider_id       upstream_model_id       catalog_source   catalog_model_id        confidence
opencode-go       xiaomi/mimo-v2.5        openrouter       xiaomi/mimo-v2.5        exact
opencode-go       mimo-v2.5               openrouter       xiaomi/mimo-v2.5        curated_alias
opencode-go       mimo-v2.5-pro           openrouter       xiaomi/mimo-v2.5-pro    curated_alias
```

Resolver rule: if an exact alias is absent and more than one candidate could match, return no external price. Never choose between `mimo-v2.5` and `mimo-v2.5-pro` by substring or edit distance.

## Pricing Extraction Fixes

### Parse OpenRouter-Compatible Cache Pricing

The current `_maybe_insert_price_snapshot()` path parses `pricing.prompt` and `pricing.completion` but not the cache fields used by OpenRouter-style records.

Add support for:

- `pricing.input_cache_read`
- `pricing.input_cache_write`
- optionally `pricing.cache_read`
- optionally `pricing.cache_write`

These should be parsed with `parse_price_per_1k(value, default_unit="token")`, then converted into the existing canonical microdollars-per-million representation for cache rates.

Implementation detail: `parse_price_per_1k()` returns dollars per 1K. For a cache price represented as dollars per token, parsing with `default_unit="token"` should return dollars per 1K. To convert to microdollars per million tokens, use the same conversion logic as input/output snapshots:

```python
per_million_microdollars = round(price_per_1k * 1_000_000_000)
```

Add a helper to avoid duplicated conversion semantics.

### Parse Broader Provider Price Field Names

Some providers and aggregators expose fields under variant names. Add a small, explicit field mapping rather than ad hoc checks.

Input candidates:

- `input_price_per_1k`
- `prompt_price_per_1k`
- `prompt`
- `pricing.prompt`
- `pricing.input`

Output candidates:

- `output_price_per_1k`
- `completion_price_per_1k`
- `completion`
- `pricing.completion`
- `pricing.output`

Cache read candidates:

- `cache_read_per_million_microdollars`
- `input_cache_read_per_million_microdollars`
- `pricing.input_cache_read`
- `pricing.cache_read`

Cache write candidates:

- `cache_write_per_million_microdollars`
- `input_cache_write_per_million_microdollars`
- `pricing.input_cache_write`
- `pricing.cache_write`

Keep the mapping exact and field-name based. Do not infer price semantics from free-form display names.

## Cost Calculation Policy Change

Change `CostCalculator.calculate_cost()` so missing cache pricing does not promote the whole request to global fallback when known input/output prices are available.

Current problematic policy:

- Calculate known categories.
- If a required rate is missing, compute generic fallback for the full request.
- Use `max(calculated_partial, fallback)`.

Recommended replacement:

1. Calculate known categories exactly.
2. For each missing category with nonzero tokens, estimate only that missing category using a category-specific fallback.
3. Mark exactness/provenance as `partial` or `estimated_partial`.
4. Do not replace known categories with a full-request global fallback.

Suggested fallback categories:

- Missing input rate: fallback only input tokens.
- Missing output rate: fallback only output tokens.
- Missing cache read rate: fallback cache read tokens at a conservative fraction of input fallback, or zero if provider semantics suggest cache reads are free and no price is published. Prefer conservative but visibly estimated.
- Missing cache write rate: fallback cache write tokens at input fallback unless a provider-specific policy says otherwise.

A minimal safe first implementation:

```text
known_input_cost + known_output_cost + known_cache_cost + fallback_for_missing_categories_only
```

Exactness mapping:

- `exact`: provider returned billed cost directly, if such a path exists.
- `derived`: all nonzero billable categories have trusted rates.
- `partial`: at least one nonzero category was priced with a category fallback but at least one category had trusted rates.
- `estimated`: no trusted rates existed and the local heuristic priced the request.
- `unknown`: no token usage and no upstream billing information.

If adding `partial` is too invasive, initially store it as `estimated` but add `pricing_source_detail` so dashboard can identify the partial case. Prefer the explicit enum if feasible.

## External Catalog Resolver

Create a new module such as:

```text
src/eggpool/catalog/pricing_resolver.py
```

Responsibilities:

1. Receive `(provider_id, model_id, source_metadata)`.
2. Try provider-authoritative price extraction from the current model row.
3. Try provider-specific configured/static extraction.
4. Try external catalog resolvers using exact IDs and alias table.
5. Return a structured `ResolvedPricing` object.

Suggested dataclass:

```python
@dataclass(frozen=True)
class ResolvedPricing:
    input_price_per_1k: float | None
    output_price_per_1k: float | None
    cache_read_per_million_microdollars: int | None
    cache_write_per_million_microdollars: int | None
    source: str
    source_detail: str
    source_confidence: str
    source_model_id: str | None = None
    source_provider_id: str | None = None
```

### OpenCode Zen Resolver

Add an interface for OpenCode Zen pricing before wiring a concrete endpoint if the endpoint shape is not yet stable. Keep this behind a feature/config switch if needed.

Expected behavior:

- Fetch catalog periodically, not per request.
- Cache catalog responses with TTL.
- Match exact canonical model IDs first.
- Use the alias table for provider-native names.
- Refuse ambiguous mappings.
- Persist snapshots only when values changed.

Configuration:

```toml
[pricing.catalogs.opencode_zen]
enabled = true
priority = 10
ttl_seconds = 86400
```

### OpenRouter Resolver

OpenRouter exposes a public OpenAI-compatible model catalog with pricing fields for many models. Use it as a fallback catalog, not as authoritative pricing for a different provider.

Configuration:

```toml
[pricing.catalogs.openrouter]
enabled = true
priority = 20
ttl_seconds = 86400
base_url = "https://openrouter.ai/api/v1"
```

Rules:

- Query `/models` on refresh.
- Index by exact `id`.
- Parse `pricing.prompt`, `pricing.completion`, `pricing.input_cache_read`, and `pricing.input_cache_write`.
- Do not use display-name fuzzy matching.
- Do not use OpenRouter prices when the selected provider exposes its own prices unless explicitly configured.

## Provider-Specific Pricing Endpoint Support

Some providers may have pricing endpoints separate from `/models`. Add provider config support:

```toml
[providers.minimax.pricing]
enabled = true
method = "GET"
path = "/models/pricing"
format = "openai_compatible" # or provider-specific enum
```

If no provider-specific endpoint exists, leave disabled and rely on model metadata or external catalogs.

Do not assume every provider uses OpenRouter field names. Use provider-specific parser enums where necessary.

## Dashboard and Stats UX

### Overview Cards

Add a visible cost provenance summary near total cost:

```text
Total cost: $X.XX
Derived from provider/catalog prices: Y%
Partial estimates: Z%
Heuristic estimates: W%
```

### Model Table

Add columns or hover text:

- Cost exactness/provenance.
- Price source: provider, config, opencode_zen, openrouter, heuristic.
- Effective input/output/cache rates.
- Warning badge if model cost is mostly estimated.

### Diagnostics Tab

Add a pricing diagnostics section showing:

- Models with high spend and no trusted price snapshot.
- Models whose price source is external catalog fallback.
- Models with nonzero cache tokens and missing cache read/write rates.
- Model IDs with possible ambiguous external catalog candidates, shown as warnings only; do not auto-select.

Example warning:

```text
mimo-v2.5 on provider opencode-go has no provider price. External catalog contains both xiaomi/mimo-v2.5 and xiaomi/mimo-v2.5-pro. No fallback price was applied because no exact alias exists.
```

## CLI Backfill Command

Add an explicit command to recompute historical request costs after pricing fixes land.

Suggested command:

```bash
eggpool stats recompute-costs --since 2026-06-01 --provider opencode-go --model mimo-v2.5 --dry-run
```

Then:

```bash
eggpool stats recompute-costs --since 2026-06-01 --provider opencode-go --model mimo-v2.5 --apply
```

Dry-run output should include:

- Number of rows matched.
- Old total cost.
- New total cost.
- Delta.
- Count by old exactness and new exactness.
- Price source used.

Guardrails:

- Require `--apply` for mutation.
- Write an audit event with old/new aggregate and filter parameters.
- Do not recompute rows with upstream exact billed cost unless `--include-exact` is supplied.

## Test Plan

### Unit Tests: Price Parsing

Add tests covering:

1. `$0.10 / 1M` parses to the correct dollars-per-1K representation.
2. OpenRouter per-token string `0.000000105` parses correctly with `default_unit="token"`.
3. `pricing.prompt` and `pricing.completion` become input/output snapshot values.
4. `pricing.input_cache_read` and `pricing.input_cache_write` become cache microdollars-per-million values.
5. Invalid price strings are ignored with warning and do not crash catalog ingestion.

### Unit Tests: Cost Calculator

Add tests covering:

1. Full trusted input/output pricing with no cache tokens returns `derived` and does not use fallback.
2. Trusted input/output plus nonzero cache tokens plus missing cache rates estimates only the missing cache category.
3. Missing all rates falls back to the global heuristic.
4. Partial known rates never produce a cost larger than full generic fallback unless the known rates themselves justify it.
5. Cache-only usage with no cache rate is marked estimated/partial rather than derived.

### Unit Tests: Alias Resolver

Add tests covering:

1. Exact external ID match resolves.
2. Curated alias resolves.
3. Ambiguous MiMo 2.5 versus MiMo 2.5 Pro candidates do not resolve without alias.
4. Substring matching is not used.
5. Provider-scoped aliases do not leak across providers.

### Integration Tests: Catalog Refresh

Mock a provider `/models` response containing OpenRouter-like pricing:

```json
{
  "id": "xiaomi/mimo-v2.5",
  "pricing": {
    "prompt": "0.000000105",
    "completion": "0.00000028",
    "input_cache_read": "0.000000021",
    "input_cache_write": "0.000000105"
  }
}
```

Expected result:

- One provider-scoped price snapshot is inserted.
- Input/output/cache values are all populated.
- Source detail indicates upstream/provider metadata, not heuristic.

### Regression Test: $92 MiMo Scenario

Build a fixture with roughly 30M MiMo 2.5 tokens and cheap MiMo pricing. Assert:

1. Cost does not use the `$3/M + $15/M` fallback when trusted price exists.
2. Total is near expected cheap-model cost.
3. Dashboard summary provenance reports zero or near-zero heuristic cost for those rows.

## Suggested Implementation Order

### Phase 1: Low-Risk Parsing and Partial-Cost Fix

1. Add a helper for parsing per-token cache prices into microdollars per million.
2. Extend `_maybe_insert_price_snapshot()` to parse OpenRouter-style cache fields.
3. Change cost calculation to fallback only missing categories rather than whole request.
4. Add focused unit tests for parsing and cost calculation.

This phase should directly address the likely inflated-cost case without adding external network dependencies.

### Phase 2: Pricing Resolver Refactor

1. Extract price resolution from `CatalogService._maybe_insert_price_snapshot()` into a dedicated resolver module.
2. Introduce `ResolvedPricing` and explicit source detail/confidence metadata.
3. Preserve existing config override semantics.
4. Update snapshot insertion to use structured resolver output.
5. Add tests for provider metadata, config overrides, and mixed provenance.

### Phase 3: External Catalog Fallbacks

1. Add catalog resolver interface and TTL cache.
2. Implement OpenRouter catalog resolver.
3. Add OpenCode Zen resolver interface and concrete implementation when endpoint/schema is confirmed.
4. Add deterministic alias registry/table.
5. Refuse ambiguous matches.
6. Add resolver tests for MiMo 2.5 and MiMo 2.5 Pro separation.

### Phase 4: Dashboard and CLI Backfill

1. Add provenance/exactness breakdown to stats queries if current exactness counts are not enough.
2. Add dashboard warnings for high-spend estimated rows.
3. Add pricing diagnostics table.
4. Add `eggpool stats recompute-costs` dry-run/apply command.
5. Add audit events for recomputations.

## Acceptance Criteria

1. A model row with OpenRouter-style `pricing.prompt`, `pricing.completion`, `pricing.input_cache_read`, and `pricing.input_cache_write` produces a complete price snapshot.
2. A request with trusted input/output prices and missing cache rates no longer replaces the entire cost with the global fallback.
3. MiMo 2.5 and MiMo 2.5 Pro cannot be conflated by resolver logic.
4. External catalog fallback is only applied on exact ID or curated alias.
5. Dashboard displays how much cost is exact, derived, partial, or heuristic.
6. A dry-run recompute command can show the delta for historical estimated MiMo rows before mutation.
7. Existing provider-scoped snapshot behavior is preserved; a price from one provider does not silently become authoritative for another provider.

## Operational Notes

Until this lands, operators can identify affected rows with:

```sql
SELECT
  COALESCE(original_model_id, model_id) AS model,
  provider_id,
  exactness,
  SUM(input_tokens) AS input_tokens,
  SUM(output_tokens) AS output_tokens,
  SUM(cache_read_tokens) AS cache_read_tokens,
  SUM(cache_write_tokens) AS cache_write_tokens,
  SUM(cost_microdollars) / 1000000.0 AS dollars,
  SUM(cost_microdollars) * 1.0 / NULLIF(SUM(input_tokens + output_tokens), 0)
    AS microdollars_per_visible_token
FROM requests
WHERE lower(COALESCE(original_model_id, model_id)) LIKE '%mimo%'
GROUP BY COALESCE(original_model_id, model_id), provider_id, exactness
ORDER BY dollars DESC;
```

And inspect snapshots with:

```sql
SELECT
  model_id,
  provider_id,
  input_price_per_1k,
  output_price_per_1k,
  input_per_million_microdollars,
  output_per_million_microdollars,
  cache_read_per_million_microdollars,
  cache_write_per_million_microdollars,
  source,
  captured_at
FROM model_price_snapshots
WHERE lower(model_id) LIKE '%mimo%'
ORDER BY captured_at DESC, id DESC;
```

If affected rows show `exactness = estimated` and roughly `3.0` microdollars per visible token, they are using the generic fallback path. If rows have nonzero cache tokens and missing cache prices, prioritize Phase 1.
