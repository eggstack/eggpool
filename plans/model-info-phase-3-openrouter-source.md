# Model Info Phase 3: OpenRouter Metadata Source and Alias Integration

## Objective

Add the first external model-info source using OpenRouter's model catalog, while preserving the existing pricing resolver behavior. This phase should generalize the source-adapter pattern established in phases 1 and 2, populate richer canonical metadata for models that can be safely matched, and avoid fuzzy or ambiguous joins.

At the end of this phase, Eggpool should be able to enrich discovered models with OpenRouter-derived advisory fields such as external model ID, display/name metadata, context length where available, pricing-adjacent metadata, modality hints, created/release-ish timestamps when available, and source provenance. The feature must remain optional and non-blocking.

## Current repo touchpoints

`src/eggpool/catalog/catalog_resolvers.py` already implements an OpenRouter pricing resolver. It defines `CatalogConfig`, `CatalogEntry`, `PricingCatalogResolver`, `TTLCache`, `OpenRouterCatalogResolver`, and `CatalogResolverPipeline`.

`src/eggpool/catalog/pricing_aliases.py` already stores and resolves explicit pricing aliases. The model-info subsystem should either reuse that alias data where semantically valid or create parallel model-info aliases with the same conservative semantics.

`src/eggpool/catalog/service.py` already attaches pricing resolvers using the shared outbound client. The model-info service should also use the shared outbound client created by `OutboundClientManager` to avoid constructing fresh clients.

`src/eggpool/models/config.py` should already contain `model_info.sources.openrouter` from phase 1.

## Design constraints

Do not replace the existing pricing resolver. The pricing resolver remains responsible for cost calculation and price snapshots.

Do not make OpenRouter a source of callability. OpenRouter metadata may enrich display info for a model Eggpool already discovered, but it must not add models to the routable catalog.

Do not automatically fuzzy-match model IDs. Exact aliases and configured aliases only.

Do not require an OpenRouter API key unless needed by the configured endpoint. Missing key should degrade gracefully.

Do not perform OpenRouter lookups on the request path.

Do not store large raw catalog payloads unless `model_info.store_raw_observations` is enabled. Even then, deduplicate by hash.

## Refactoring target

The existing `OpenRouterCatalogResolver` is pricing-specific. Avoid trying to stretch `CatalogEntry` into a general model metadata record. Instead, extract shared OpenRouter fetch/parse utilities where worthwhile, or duplicate minimal safe fetch logic in `model_info/sources/openrouter.py` and revisit consolidation later.

Acceptable minimal duplication:

```text
GET /models
response.raise_for_status()
parse payload["data"] list
index by id
TTL-cache full response
```

Avoid duplication of price parsing if possible. If model-info canonical detail wants to include price-like observations, reuse `parse_price_per_1k` and microdollar parsers from `eggpool.catalog.pricing`, but keep cost-calculation authority in the existing pricing pipeline.

## Source adapter implementation

Create `src/eggpool/model_info/sources/openrouter.py`.

Implement:

```python
class OpenRouterModelInfoSource:
    name = "openrouter"

    def __init__(self, *, config: ModelInfoSourceConfig, client: ModelInfoHttpClient, cache: TTLCache | None = None) -> None: ...

    @property
    def priority(self) -> int: ...

    async def fetch_all(self) -> list[SourceModelRecord]: ...

    async def fetch_one(self, model_id: str, *, provider_id: str | None = None) -> SourceModelRecord | None: ...
```

Use a model-info-specific TTL cache. It can be generic over raw dicts or a new `ModelInfoCatalogEntry` dataclass.

Suggested entry dataclass:

```python
@dataclass(frozen=True)
class OpenRouterModelInfoEntry:
    source_model_id: str
    display_name: str | None
    context_window: int | None
    max_output_tokens: int | None
    modalities: frozenset[str]
    supports_tools: bool | None
    supports_reasoning: bool | None
    input_price_per_1k: float | None
    output_price_per_1k: float | None
    created_at: datetime | None
    raw: dict[str, object]
```

Do not assume exact OpenRouter payload fields beyond defensive parsing. Treat unknown fields as absent, not fatal.

Headers:

```python
headers = {"User-Agent": "eggpool/1.0"}
if config.resolved_api_key:
    headers["Authorization"] = f"Bearer {config.resolved_api_key}"
```

URL:

```python
base = config.base_url or "https://openrouter.ai/api/v1"
url = f"{base.rstrip('/')}/models"
```

Error behavior:

Network, HTTP, and JSON errors should raise a model-info-specific `ModelInfoSourceFetchError` caught by `ModelInfoService`, which records source health and continues.

## Parsing policy

Parse source model ID from `id` only.

Parse display name from likely fields, in order:

```text
name
title
id
```

Parse context window conservatively from known numeric fields if present, such as:

```text
context_length
context_window
max_context_tokens
```

Parse max output conservatively from:

```text
top_provider.max_completion_tokens
max_completion_tokens
max_output_tokens
```

Parse pricing if present but mark it as advisory model-info metadata, not cost-calculation truth:

```text
pricing.prompt
pricing.completion
pricing.input_cache_read
pricing.input_cache_write
```

Use existing safe parsers from `catalog.pricing` if practical.

Parse modalities from explicit architecture/modality fields when available. If no reliable field exists, infer only from obvious structured fields such as `architecture.input_modalities` and `architecture.output_modalities`. Do not infer vision from model name substrings.

Parse supported tool/reasoning only from explicit fields when available. If absent, leave `None`.

## Identity resolution

Create or extend `src/eggpool/model_info/identity.py`.

Identity resolver inputs:

```text
Eggpool provider_id
Eggpool model_id
source name
source_model_id candidate
configured aliases
existing model_info_aliases
pricing aliases if intentionally reused
```

Rules:

1. Exact provider/model alias row in `model_info_aliases` wins.
2. Exact source model ID equals Eggpool model ID may match if the provider/source context is not contradictory.
3. Explicit configured aliases may match.
4. Existing pricing aliases may be used only if they are exact or curated and the source is the same `openrouter` catalog.
5. Ambiguous matches return no match and record an `unmatched` observation or diagnostic.
6. No substring/edit-distance matching.

Add config support for model-info aliases if not already present:

```toml
[[model_info.aliases]]
provider_id = "minimax"
model_id = "minimax-m3"
source = "openrouter"
source_model_id = "minimax/minimax-m3"
confidence = "curated"
```

If adding config aliases is too much for phase 3, seed only exact aliases and rely on repository alias rows created by provider-native observations.

## Service integration

Extend `ModelInfoService` source construction:

```python
sources = [ProviderCatalogModelInfoSource(...)]
if config.sources.openrouter.enabled:
    sources.append(OpenRouterModelInfoSource(config=config.sources.openrouter, client=outbound_client))
```

Add source refresh flow:

For a due model:

1. Refresh provider-native observation first.
2. Attempt OpenRouter match through identity resolver.
3. If a source candidate exists, fetch catalog if TTL expired or cached catalog absent.
4. Convert matched entry to `SourceModelRecord`.
5. Persist observation and aliases.
6. Reconcile canonical record.
7. Record source success/error.

Prefer bulk fetch per source per cycle. OpenRouter's `/models` catalog is naturally bulk. Avoid fetching the entire catalog once per model.

Suggested cycle structure:

```python
async def refresh_due_models(self) -> dict[str, int]:
    due = await repo.list_due(limit=config.max_models_per_cycle)
    provider_records = provider_source.records_for(due)
    openrouter_catalog = await openrouter.fetch_all_indexed() if any due can use it else {}
    for model in due:
        collect matching source records
        reconcile
```

If the adapter interface initially exposes only `fetch_all()`, let service call it once and index in memory.

## Reconciliation additions

OpenRouter should contribute these canonical fields when provider-native/local values do not already provide them:

```text
display_name
external_ids.openrouter
context_window_external
max_output_tokens_external
modalities_external
pricing_observation.openrouter
created_at/release-ish date if present
source list
```

Do not overwrite:

```text
local effective context limits
provider-native callability
explicit model/provider overrides
runtime stats
pricing snapshots used by CostCalculator
```

Conflict detection:

If provider-native discovered context and OpenRouter context both exist and differ materially, add a conflict entry:

```json
{
  "context_window": {
    "provider_catalog": 220000,
    "openrouter": 1000000,
    "selected": "provider_catalog/effective_limit",
    "reason": "local/provider effective limit wins for Eggpool display"
  }
}
```

Conflict status should be `conflicting` only when the conflict affects a displayed canonical field. If the external field is stored as an alternate observation but not selected, status can remain `partial` with conflict detail present.

## Raw payload storage

Respect `model_info.store_raw_observations`.

If true, store raw OpenRouter entry JSON after bounding size. If a single entry exceeds a conservative limit, store only selected fields plus raw hash.

If false, store `{}` in `raw_json` and keep `normalized_json` plus `raw_hash`.

Do not store the entire OpenRouter catalog as one observation. Store one observation per matched source model.

## Tests

Adapter parsing tests:

`test_openrouter_source_parses_basic_model_entry`

`test_openrouter_source_parses_pricing_defensively`

`test_openrouter_source_missing_optional_fields_returns_record`

`test_openrouter_source_bad_payload_returns_empty_catalog`

`test_openrouter_source_http_error_records_fetch_error`

Identity tests:

`test_identity_exact_alias_match`

`test_identity_refuses_ambiguous_aliases`

`test_identity_refuses_substring_match`

`test_identity_reuses_exact_pricing_alias_only_if_source_matches`

Service tests:

`test_refresh_due_models_fetches_openrouter_catalog_once_per_cycle`

`test_openrouter_observation_enriches_canonical_detail`

`test_openrouter_context_conflict_is_recorded`

`test_openrouter_failure_records_source_health_and_preserves_cached_info`

`test_openrouter_disabled_skips_source_without_error`

Config tests:

`test_openrouter_model_info_source_defaults`

`test_openrouter_api_key_env_resolution`

## Manual verification

Enable OpenRouter source in config.

Start Eggpool and confirm no startup failure when OpenRouter is unreachable.

Run `eggpool modelinfo refresh` and confirm source health shows OpenRouter success or nonfatal error.

For a model with an exact alias, confirm `model_info_observations` contains one OpenRouter observation.

Confirm `model_info_canonical.detail_json` includes `external_ids.openrouter` and provenance.

Confirm `/v1/models` remains unchanged in this phase unless phase 4 has already landed.

Disable OpenRouter source and confirm model-info refresh still works with provider-native observations only.

## Acceptance criteria

OpenRouter model-info source exists behind the model-info source abstraction.

OpenRouter fetches are TTL-cached and use the shared outbound HTTP client.

Exact/curated alias matching works; fuzzy matching is refused.

Matched OpenRouter observations are persisted with provenance.

Canonical model-info detail can include OpenRouter-enriched fields.

Conflicts between provider/local and OpenRouter metadata are recorded without changing routing.

OpenRouter failures are recorded in source health and do not break startup, catalog refresh, or routing.

Existing pricing resolver and cost-calculation paths continue to work unchanged.
