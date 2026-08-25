# Deep Dive: Data Models

Back to [Overview](overview.md)

## Purpose

Pydantic v2 models for configuration, domain objects, API payloads, and database rows. These models are the single source of truth for schema validation and serialization boundaries.

## Module Structure

```
src/eggpool/models/
├── __init__.py
├── config.py          # TOML config models (~1571 lines)
├── api.py             # Internal API models (health, errors, model listing)
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
    models: ModelsConfig
    routing: RoutingConfig
    limits: LimitsConfig
    pricing: PricingConfig
    dashboard: DashboardConfig
    security: SecurityConfig
    metrics: MetricsConfig
    maintenance: MaintenanceBudgetConfig
    backup: BackupConfig
    readiness_probe: ReadinessProbeConfig
    network: NetworkConfig
    proxies: dict[str, ProxyConfig]
    accounts: list[AccountConfig]
    providers: dict[str, ProviderConfig]
    model_overrides: dict[str, ModelOverrideConfig]
    model_capabilities: dict[str, ModelCapabilitiesOverrideConfig]
    transcoder: TranscoderPolicy
    model_info: ModelInfoConfig
    update_checker: UpdateCheckerConfig
```

**Key nested models:**
- `ServerConfig` — host, port, threads (constrained `le=1`), max_request_body_bytes
- `ProviderConfig` — id, base_url, protocols, auth, accounts, models_endpoint, static_models, verify
- `AccountConfig` — name, api_key, api_key_env, enabled, weight, proxy, proxy_url
- `RoutingConfig` — fairness_mode, fairness_epsilon, fairness_scope, trace
- `TranscoderPolicy` — features, loss_policy, openai_reasoning_fields, thinking_budget_defaults
- `DatabaseConfig` — path, busy_timeout_ms, wal, synchronous, worker_threads

**Validation:**
- `validate_account_credentials()` rejects API keys beginning with `Bearer` for `auth.mode = "bearer"` providers
- HTTP header name regex validation
- Provider URL validation (rejects duplicate `/v1` prefix)
- Config file parsing with `tomllib`

### API Models (`api.py`)

Pydantic models for internal API payloads (health, readiness, errors, model listing):
- `HealthResponse`, `ReadyResponse` — status probes
- `ErrorResponse` — error envelope
- `ModelObject`, `ModelListResponse` — model catalog listing

### Database Models (`database.py`)

SQLite row models for type-safe database access:
- Request rows, attempt rows
- Account, model, and account-model rows
- Quota reservation rows

### Domain Models (`domain.py`)

Domain objects shared across modules:
- `Provider` — provider record with id, base_url, protocols
- `Account` — account record with id, name, weight, provider_id
- `AccountRuntimeState` — health, cooldown, active request counts
- `ModelDescriptor` — model metadata with capabilities and source info
- `UsageExactnessLevel` — enum for token usage accuracy

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
