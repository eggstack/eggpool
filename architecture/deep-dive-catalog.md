# Deep Dive: Model Catalog

The model catalog is responsible for discovering, normalizing, pricing, and tracking capabilities of models across all configured providers.

## Module Structure

```
src/eggpool/catalog/
├── __init__.py
├── service.py              # Orchestrator (~1678 lines)
├── fetcher.py              # Provider /v1/models endpoint calls
├── normalizer.py           # Heterogeneous response normalization
├── cache.py                # In-memory catalog cache
├── protocols.py            # Per-model protocol resolution (6-tier)
├── capabilities.py         # Model capabilities (thinking, budget bounds)
├── pricing.py              # Cost estimation
├── pricing_resolver.py     # Pricing resolution pipeline
├── pricing_aliases.py      # Pricing alias mapping
├── catalog_resolvers.py    # OpenRouter and pricing resolver pipelines
├── limits.py               # Upstream context-window limit extraction
└── models_dev.py           # External models.dev metadata merge
```

## Key Components

### Catalog Service (`service.py`)

The central orchestrator (~1678 lines). Manages periodic refresh cycles from provider endpoints, merges results from multiple sources, and maintains the in-memory catalog cache. Invoked on startup and periodically by background tasks.

Key responsibilities:
- Triggers `fetch_models_for_account()` for each configured account
- Normalizes raw responses via `normalize_models()`
- Merges models.dev metadata via `merge_models_dev_metadata()`
- Applies pricing aliases via `PricingAliasResolver`
- Updates the `ModelCatalogCache` atomically
- Tracks per-account `AccountCatalogOutcome` for health attribution

### Model Fetcher (`fetcher.py`)

Calls each provider's models endpoint (configurable per provider via `[providers.<id>.models_endpoint]`). Handles HTTP errors, timeouts, and malformed responses. Returns raw model lists that the normalizer processes.

### Normalizer (`normalizer.py`)

Transforms heterogeneous provider responses into a canonical model list. Handles different response shapes (OpenAI-style `data[].id`, Anthropic-style, provider-specific formats). Extracts capabilities from metadata blocks.

### Protocol Resolution (`protocols.py`)

6-tier resolution chain for determining each model's native protocol:

1. **Explicit TOML override** — `[providers.<id>.static_models]` with explicit `protocol`
2. **Explicit per-model metadata** — from upstream response
3. **Exact known-model mapping** — hardcoded for well-known models (GPT-4, Claude 3, etc.)
4. **Known family prefix mapping** — prefix-based inference (e.g., `gpt-*` → `openai`, `claude-*` → `anthropic`)
5. **Previously persisted protocol** — from SQLite
6. **Unresolved error** — raised when no tier matches

### Capability Detection (`capabilities.py`)

Tracks per-model capabilities including:
- Thinking/reasoning support (`CapabilityStatus`: supported, unsupported, unknown)
- Budget bounds (`budget_tokens_min`, `budget_tokens_max`)
- Effort-to-budget token mappings
- Tool support, streaming support

### Pricing (`pricing.py`, `pricing_resolver.py`, `pricing_aliases.py`)

Cost estimation and pricing resolution:
- `parse_microdollars_per_million()` and `parse_price_per_1k()` for price parsing
- `PricingAliasResolver` maps aliases to canonical pricing entries
- `PricingCatalogResolver` pipeline resolves pricing from external sources
- Default aliases seeded at startup via `seed_default_aliases()`

### Cache (`cache.py`)

`ModelCatalogCache` maintains the in-memory catalog with:
- Per-account `AccountCatalogOutcome` tracking (success, partial, failure)
- Atomic swap semantics during refresh
- Thread-safe read access from request path

### Durable refresh and diagnostic writes

The five-minute discovery cadence is not equivalent to a five-minute full
catalog rewrite. `_persist_catalog()` computes stable desired-vs-durable
semantic deltas outside the SQLite write transaction. Unchanged `models` and
`provider_model_metadata` rows are left untouched, as are unchanged
`account_models` relationships. Successful freshness is stored in the compact
`catalog_refresh_state` table, one row per account, and is hydrated before the
legacy model-timestamp fallback so routing staleness remains restart-safe.

`PingRepository` writes upstream failures and success/failure transitions
immediately. Steady successful samples are coarsened to one durable sample per
account/provider pair per 30 minutes, including across restarts. The in-memory
cache still receives every successful refresh timestamp used by the current
runtime.

### Limits (`limits.py`)

`ModelLimitResolver` extracts upstream context-window limits from provider metadata. Used by the request lifecycle to enforce context-limit checks before dispatch.

### Models Dev Integration (`models_dev.py`)

Merges metadata from external `models.dev` sources:
- `fetch_models_dev_provider_models()` — fetches from models.dev API
- `merge_models_dev_metadata()` — merges with provider-sourced data
- `derive_opencode_go_supported_efforts()` — derives thinking effort support for OpenCode Go models
- `apply_supported_efforts_to_capabilities()` — applies effort mappings to capability objects

## Data Flow

```
Provider /v1/models → fetcher → normalizer → cache
                                              ↓
models.dev metadata ─────────────→ merge ──→ cache
                                              ↓
Pricing aliases ─────────────────→ resolve ──→ cache
                                              ↓
Protocol resolution ────────────────────────→ cache
                                              ↓
Catalog service ────────────────────────────→ RuntimeGeneration (atomic swap)
```

## Key Invariants

- Catalog refresh never blocks the request path — reads go to the current cache
- Atomic cache swap ensures consistent reads during refresh
- Per-account outcomes are tracked independently — one provider's failure doesn't affect others
- Protocol resolution is deterministic — same input always produces same output
- Pricing is advisory only — never used for routing decisions
- `static_models` in config is the source of truth for provider-specific protocol when live fetch fails

## Configuration

```toml
[models]
collapse_models = false           # Collapse provider-suffixed IDs
catalog_withdrawal = []           # Models to withdraw from catalog

[providers.<id>.models_endpoint]
method = "GET"                    # HTTP method
path = "/v1/models"              # Endpoint path
# method = "DISABLED"            # Skip live listing; use static_models only

[[providers.<id>.static_models]]
id = "model-name"
protocol = "openai"              # Explicit protocol override
```

## Related

- [deep-dive-providers.md](deep-dive-providers.md) — Provider configuration and contracts
- [deep-dive-model-info.md](deep-dive-model-info.md) — External metadata enrichment sidecar
- [deep-dive-routing.md](deep-dive-routing.md) — How catalog feeds into routing eligibility
