# Deep Dive: Data Models

Pydantic v2 models for configuration, domain objects, API payloads, and database rows. These models are the single source of truth for schema validation and serialization boundaries.

## Module Structure

```
src/eggpool/models/
├── __init__.py
├── config.py          # TOML config models (~1571 lines)
├── api.py             # OpenAI and Anthropic request/response models
├── database.py        # SQLite row models
└── domain.py          # Domain objects shared across modules
```

## Key Components

### Config Models (`config.py`)

The largest file (~1571 lines). Defines `AppConfig` and all nested configuration models with field validators, TOML parsing, and credential validation.

**Top-level structure:**
```python
class AppConfig(BaseModel):
    server: ServerConfig
    upstream: UpstreamConfig
    database: DatabaseConfig
    routing: RoutingConfig
    models: ModelsConfig
    providers: dict[str, ProviderConfig]
    transcoder: TranscoderPolicy
    compression: CompressionConfig
    cache: CacheConfig
    model_info: ModelInfoConfig
    dashboard: DashboardConfig
    metrics: MetricsConfig
    backup: BackupConfig
    security: SecurityConfig
```

**Key nested models:**
- `ServerConfig` — host, port, workers, runtime_threads, database.worker_threads
- `ProviderConfig` — id, base_url, protocols, auth, accounts, models_endpoint, static_models, verify
- `AccountConfig` — name, api_key_env, enabled, routing_priority
- `RoutingConfig` — fairness_mode, fairness_epsilon, scope, trace_enabled
- `TranscoderPolicy` — features, thinking, openai_reasoning_fields, loss_policy, thinking_budget_defaults
- `CompressionConfig` — enabled, mode, placement, transforms, thresholds

**Validation:**
- `validate_account_credentials()` rejects API keys beginning with `Bearer` for `auth.mode = "bearer"` providers
- HTTP header name regex validation
- Provider URL validation (rejects duplicate `/v1` prefix)
- Config file parsing with `tomllib`

### API Models (`api.py`)

Pydantic models for OpenAI and Anthropic request/response payloads:
- OpenAI: `ChatCompletionRequest`, `ChatCompletionResponse`, streaming chunks
- Anthropic: `MessagesRequest`, `MessagesResponse`, streaming events
- Shared: error response envelopes, usage blocks

### Database Models (`database.py`)

SQLite row models for type-safe database access:
- Request rows, attempt rows, routing decision rows
- Account/provider/model rows
- Quota reservation rows, ping rows, backoff rows
- Model info metadata rows, compression observation rows

### Domain Models (`domain.py`)

Domain objects shared across modules:
- `ProxyRequestContext` — the per-request context object threaded through the entire lifecycle
- `TranscodeContext` — per-request transcoding state
- `FinalizationData` — data required for request finalization
- `RoutingDecision` — selected account and scoring details

## Key Invariants

- `AppConfig` is immutable after construction — changes require a new generation
- All config fields default to `RESTART_REQUIRED` unless explicitly classified as `LIVE` in `config_reload_policy.py`
- API models are the wire-format contract — changes require versioning consideration
- Database models are schema-bound — changes require migrations
- Domain models carry generation-aware state — they are not persisted directly

## Configuration

These models are not configured separately — they define the configuration schema consumed by `config.toml`. See [deep-dive-core.md](deep-dive-core.md) for config file details.

## Related

- [deep-dive-core.md](deep-dive-core.md) — Config file helpers and validation
- [deep-dive-database.md](deep-dive-database.md) — Database schema and migrations
- [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) — How domain models flow through the request path
