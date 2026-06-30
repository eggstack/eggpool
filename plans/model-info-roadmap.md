# Model Information Roadmap

## Purpose

Eggpool already discovers callable models through provider-native model listing, static model seeds, and the persisted catalog cache. The next improvement is a sidecar model-information subsystem that enriches discovered models with advisory metadata such as benchmark summaries, context and modality facts, pricing provenance, release/family information, source health, and a short UI-facing blurb.

The feature must remain separate from routing correctness. Model discovery answers whether Eggpool can route to a model. Model information answers what Eggpool knows about that model. External metadata must never invent callability, suppress a callable model, or override local routing eligibility. The router should continue to rely on provider discovery, health/backoff, local quota policy, observed runtime stats, and explicit user configuration.

## Current repo fit

The existing architecture is already close to the desired shape.

`CatalogService` owns model discovery. It loads cached models at startup, optionally refreshes on startup, periodically refreshes through the supervised `catalog_refresh` background task, normalizes upstream `/models` responses, seeds static models, persists models into SQLite, and reconciles withdrawn models. This is the correct event boundary for model-info reconciliation.

`ModelCatalogCache` already tracks global model entries, provider-specific entries, account support, account-provider mappings, first-seen timestamps, last-seen timestamps, account refresh ages, and provider-scoped exposure. These fields are sufficient to identify new, changed, withdrawn, sparse, and stale models.

`catalog/catalog_resolvers.py` already contains the right precedent for external source integration: a source protocol, TTL cache, OpenRouter implementation, explicit alias lookup, conservative no-fuzzy-matching semantics, and advisory external pricing. The model-info subsystem should generalize this pattern rather than overloading the existing pricing-specific types.

`TaskSupervisor` already supervises periodic work with restart/backoff and runtime task snapshots. A `model_info_refresh` task can fit directly beside `catalog_refresh`, `retention_cleanup`, `checkpoint`, `usage_window_refresh`, `update_checker`, and `automatic_backup`.

The `/v1/models` serializer already exposes an `eggpool` namespace. Compact model-info summaries can live under `eggpool.model_info` without breaking the OpenAI-compatible outer shape. Full detail should be exposed through Eggpool-specific dashboard/API endpoints.

The dashboard already has a `/models` page. The first UI pass should enrich the existing usage-oriented model rows with a status pill and tooltip. A later pass can add a model-detail page.

## Design principles

Keep callability separate from advisory metadata. Provider discovery and static config decide what Eggpool can expose. External model-info sources only enrich display and operator understanding.

Preserve provenance. Every external field should carry source name, source model ID, observation timestamp, confidence, and freshness. The UI should be able to show when data is sparse, stale, conflicting, or unavailable.

Use conservative identity resolution. Exact aliases, configured aliases, provider-owned IDs, and curated mappings are acceptable. Substring, edit-distance, and marketing-name fuzzy matches should be refused by default because bad joins are worse than missing data.

Make field-wise reconciliation explicit. Provider-native discovery and user config win for callability and local effective limits. External benchmark sources win only for benchmark observations. Local Eggpool observations win for runtime latency/reliability. External pricing can enrich but must not overwrite explicit price overrides.

Do not bloat `/v1/models`. The OpenAI-compatible response should include only compact metadata when enabled. Full benchmark tables and raw observations belong in Eggpool-specific dashboard/API routes.

Handle new sparse models as a first-class state. A newly discovered model should be immediately visible as callable, marked `sparse_new` when metadata is incomplete, and refreshed more aggressively for a bounded period.

Avoid request-path network I/O. Model-info fetching must happen at startup, after catalog refresh, through a periodic background runner, or through an explicit CLI command. Chat/completion routing must never wait on external model-info sources.

## Target architecture

Add a new package:

```text
src/eggpool/model_info/
  __init__.py
  types.py
  repository.py
  service.py
  scheduler.py
  identity.py
  reconciliation.py
  sources/
    __init__.py
    base.py
    provider_catalog.py
    openrouter.py
    artificial_analysis.py
    huggingface.py
```

The subsystem has four layers.

First, source adapters fetch and parse source-specific data into `SourceModelRecord` observations. Each adapter is responsible only for fetch/parse/normalize, not for canonical truth.

Second, the identity resolver maps source observations onto Eggpool model IDs using exact IDs, configured aliases, provider IDs, and curated aliases. Ambiguous or unconfigured matches are stored as unmatched observations or ignored, not guessed.

Third, the reconciler builds canonical advisory metadata from observations using field-wise source priority and confidence rules. It writes compact canonical display metadata, provenance, conflict information, and next-refresh timestamps.

Fourth, the API/UI layer reads canonical model-info summaries and details without knowing which source produced them.

## Proposed status model

Use a small explicit status vocabulary:

```text
fresh
partial
sparse_new
stale
conflicting
unmatched
source_unavailable
manual_override
withdrawn
```

`fresh` means sufficient metadata is available and all relevant TTLs are valid.

`partial` means the model is usable and has some metadata, but one or more useful enrichment categories are missing.

`sparse_new` means the model was recently discovered and has minimal metadata. This status receives accelerated refresh for a bounded window.

`stale` means canonical metadata exists but TTL has expired.

`conflicting` means two or more sources disagree on a field that matters for display, such as context window, modality, pricing, or release/family identity.

`unmatched` means a source observation could not be safely mapped to a canonical Eggpool model ID.

`source_unavailable` means source failures prevent refresh, but cached data may remain usable.

`manual_override` means at least one field is explicitly overridden by user config. External observations should continue to refresh underneath.

`withdrawn` means Eggpool previously had metadata for a model but the model is no longer in the live catalog. This may be implemented later through tombstones; phase 1 can cascade-delete sidecar rows with catalog deletion.

## Source priority policy

Field-wise authority should be deterministic.

Callability and exposure: provider-native account discovery, static model seeds, and existing catalog state only.

Local effective limits: user model/provider overrides first, then provider-native metadata, then external metadata, then unknown.

Provider aliases: provider-native IDs first, configured aliases second, external catalog aliases third.

Benchmarks: benchmark-specific sources first, leaderboard datasets second, provider/model-card claims third, unknown otherwise.

Pricing: direct provider metadata and explicit overrides first, existing pricing resolver and price snapshots second, OpenRouter or other external catalog observations third, generic fallback last if configured.

Runtime speed/reliability: local Eggpool observations only.

Display summary: deterministic template derived from canonical fields. Do not require an LLM-generated blurb.

## Refresh strategy

On startup, after catalog cache load and optional startup refresh, reconcile all live catalog models against `model_info_canonical`.

After every successful catalog refresh, compute a model-set diff and enqueue model-info refresh for new models, sparse models, stale models, conflicting models, and changed provider-scoped metadata.

Periodic `model_info_refresh` should process due rows according to `next_refresh_at`, bounded by `max_models_per_cycle` and per-source rate/backoff status.

Suggested defaults:

```text
known model TTL: 24h
partial model TTL: 12h
sparse_new TTL: 1h for first 48h
sparse_new TTL: 6h after 48h and before 7d
conflicting TTL: 2h, capped by source backoff
source failure backoff: exponential, max 24h
manual override: never overwrite overridden fields, but continue source refresh
```

## Configuration outline

Add a new `[model_info]` section rather than expanding `[models]` or `[pricing]`.

```toml
[model_info]
enabled = true
startup_refresh = true
refresh_interval_s = 21600
known_ttl_s = 86400
partial_ttl_s = 43200
sparse_new_initial_ttl_s = 3600
sparse_new_later_ttl_s = 21600
sparse_new_accelerated_days = 7
conflict_ttl_s = 7200
max_models_per_cycle = 50
include_in_models_endpoint = true
store_raw_observations = true

[model_info.sources.openrouter]
enabled = true
priority = 100
ttl_seconds = 86400
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[model_info.sources.artificial_analysis]
enabled = false
priority = 50
ttl_seconds = 86400
api_key_env = "ARTIFICIAL_ANALYSIS_API_KEY"

[model_info.sources.huggingface]
enabled = false
priority = 200
ttl_seconds = 604800
api_key_env = "HUGGINGFACE_TOKEN"
```

## Database outline

Prefer sidecar tables keyed by `models.model_id`. Do not overload `models.source_metadata` or `provider_model_metadata.source_metadata`; those are already part of discovery, protocol resolution, limit resolution, and catalog persistence.

Core tables:

```text
model_info_canonical
model_info_observations
model_info_aliases
model_info_source_health
```

Optional later table:

```text
model_info_benchmarks
```

In phase 1, benchmark details can live in canonical/detail JSON. Add a relational benchmark table only when dashboard sorting/filtering by benchmark becomes necessary.

## API and dashboard outline

Add compact summaries under `/v1/models` only when `model_info.include_in_models_endpoint = true`:

```json
"eggpool": {
  "model_info": {
    "status": "partial",
    "summary": "New large-context model; benchmark metadata sparse.",
    "sources": ["provider_catalog", "openrouter"],
    "last_refreshed_at": "2026-06-29T20:00:00Z"
  }
}
```

Add Eggpool-specific JSON endpoints:

```text
GET /api/model-info
GET /api/model-info/{model_id:path}
GET /api/model-info/sources
POST /api/model-info/refresh   # auth-gated, optional/manual later
```

Dashboard first pass:

```text
/models table:
  status pill
  short tooltip
  sources count
  last refreshed age
  sparse/conflict indicator
```

Later dashboard pass:

```text
/model-info/{model_id} detail page:
  canonical facts
  provider aliases
  benchmark rows
  source observations
  conflicts
  refresh state
  local Eggpool runtime stats
```

## Phase breakdown

Phase 1: foundation, schema, config, provider-native source, canonical sidecar cache, CLI show/refresh. No external network dependency.

Phase 2: lifecycle wiring, catalog-diff reconciliation, background scheduler, sparse-new refresh policy, source health/backoff.

Phase 3: OpenRouter metadata source and alias integration. Generalize the existing pricing-resolver pattern without replacing pricing resolution.

Phase 4: API and dashboard integration. Add compact `/v1/models` enrichment, dashboard JSON endpoints, model table tooltips/status pills, and runtime visibility.

Phase 5: benchmark/model-card sources. Add optional Artificial Analysis and Hugging Face adapters, benchmark/detail rendering, conflict surfacing, and final hardening.

## Non-goals for the initial roadmap

Do not use web scraping.

Do not change routing eligibility based on public benchmarks.

Do not make external metadata refresh a readiness requirement.

Do not perform external lookups on the request path.

Do not add fuzzy model matching by default.

Do not store client request content or model prompts as part of model-info enrichment.

Do not require Artificial Analysis or Hugging Face credentials for baseline functionality.

## Acceptance criteria for the full roadmap

Eggpool can display useful model-info summaries for all discovered models using at least provider-native observations.

Newly discovered models are tagged `sparse_new` when metadata is incomplete and are refreshed more aggressively for a bounded window.

Known models are refreshed on a slower TTL and remain usable when external sources are unavailable.

External sources can be enabled, disabled, added, or removed through a source-adapter abstraction.

All advisory facts carry source/provenance/freshness information.

The OpenAI-compatible `/v1/models` response remains valid and compact.

Dashboard model rows show model-info status without requiring a separate detail page.

Full detail endpoints expose canonical info, source observations, conflicts, and source health.

The router remains independent from benchmark/model-card metadata.
