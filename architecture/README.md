# Architecture

High-level design overview for the EggPool aggregator.

## Package Structure

```
src/eggpool/
├── accounts/          # Account registry and runtime state
├── api/               # API endpoint handlers (chat completions, messages, stats)
├── background/        # TaskSupervisor, retention cleanup, periodic tasks
├── catalog/           # Model catalog, pricing, protocols, fetcher, normalizer, limits
├── dashboard/         # Self-updating server-rendered HTML dashboard
├── db/                # SQLite connection, migrations, repositories, schema
├── health/            # Circuit breaker and health tracking
├── integrations/      # External tool configuration generation (OpenCode, Claude Code)
├── models/            # Pydantic config, domain, API, and database models
├── providers/         # ProviderClientPool, pproxy transport, connect CLI
├── proxy/             # Transparent proxy, SSE observer, usage extraction
├── quota/             # Quota estimation, reservations, scoring
├── request/           # RequestCoordinator, finalizers, body reader, limit enforcement
├── retry/             # Error classification and failover
├── routing/           # Quota-aware routing, eligibility, provider parsing
├── security/          # Header redaction, security utilities
├── stats/             # Statistics queries and service
├── deploy/            # Bundled systemd/logrotate/cron snippets for CLI output
├── _share/            # Bundled config examples and assets for pipx installs
├── auth.py            # Local API key authentication (constant-time)
├── cli.py             # Click CLI commands
├── errors.py          # Exception hierarchy
├── logging.py         # Structured logging setup
└── constants.py       # Project-wide constants
```

## Request Lifecycle

All data-plane requests flow through `RequestCoordinator`:

1. **Endpoint** (`api/chat_completions.py` or `api/messages.py`) extracts model ID, parses provider suffix
2. **Routing** selects an eligible account via quota-aware scoring (`routing/router.py`)
3. **Attempt** is persisted to SQLite before upstream dispatch
4. **Provider Contract** renders absolute URL (`compose_provider_url()`) and auth headers (`build_upstream_headers()`) from `providers/contract.py`
5. **Proxy** sends the request via the provider's `httpx.AsyncClient` from `ProviderClientPool`
6. **Streaming** is handled by `proxy/sse_observer.py` with chunk-level usage extraction
7. **Finalization** records usage, releases reservations, updates health state

All outbound dispatch paths (non-streaming chat, streaming chat, catalog refresh) share the same `compose_provider_url()` rules so a provider cannot list models at one host and dispatch requests to another. The coordinator's `_get_upstream_url()` returns an absolute URL for provider-configured paths, falling back to bare paths only when no provider config is loaded.

Key invariants:
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- The same URL composition rules apply to catalog fetch and chat dispatch

## Multi-Provider Architecture

EggPool supports 27+ upstream providers (OpenCode Go, OpenAI, Anthropic, Groq, DeepInfra, Gemini, xAI, Mistral, SiliconFlow, DeepSeek, Together, Fireworks, OpenRouter, Alibaba, MiniMax, and more), each with its own base URL, account pool, supported protocols, and model catalog. See `docs/providers.md` for the full roster.

### MiniMax templates

- **`minimax`** — international host `https://api.minimax.io/anthropic`. Anthropic-compatible transport (key sent as `x-api-key` plus `anthropic-version: 2023-06-01`). Model listing is `DISABLED` (the Anthropic-compatible host does not expose `/models`), so the catalog is seeded from a static `[[providers.minimax.static_models]]` table covering the current `MiniMax-*` token-plan lineup. Default for keys from `minimax.io`.
- **`minimax-cn`** — China host `https://api.minimaxi.com/v1` with the same OpenAI paths as a standard provider. Live verification is required because the China endpoint family has not been confirmed against EggPool's Anthropic-compatible transport.

The stored key must be the raw token; EggPool prepends the configured auth scheme automatically. An optional `[providers.<id>.verify]` block lets the verifier know which model to probe when neither `--openai-model` nor `--anthropic-model` is passed on the CLI.

### Provider Configuration

Providers are configured under `[providers.<id>]` in `config.toml`:

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[[providers.opencode-go.accounts]]
name = "personal"
api_key_env = "OPENCODE_GO_KEY_1"
```

Legacy flat `[[accounts]]` configs auto-normalize to a default `opencode-go` provider.

### Client Pool

`ProviderClientPool` (`providers/client_pool.py`) manages per-provider `httpx.AsyncClient` instances with independent connection pools, timeouts, and optional per-account proxy support.

### Model ID Format

Models are exposed with provider-suffixed IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`). `parse_model_provider()` in `routing/provider.py` is the canonical suffix parser; `catalog/cache.py` retains a compatibility alias.

### Provider-Specific Paths

Each provider can configure custom upstream paths:
- `openai_path` (default: `/chat/completions`)
- `anthropic_path` (default: `/messages`)
- `models_endpoint` — `[providers.<id>.models_endpoint]` table with `method`, `path`, `query`, `body`, `required`. Use `method = "DISABLED"` for providers that do not expose a live model listing (catalog is then populated from `static_models`).
- `models_method` / `models_path` — legacy scalar fields still accepted; auto-synthesized into a default `models_endpoint` table on parse.

### Provider Contracts

Each provider declares an explicit contract for authentication, URL composition, and model listing via `ProviderAuthConfig`, `ProviderStaticHeaderConfig`, `ProviderModelsEndpointConfig`, and `ProviderVerifyConfig` in `config.toml`.

`src/eggpool/providers/contract.py` centralizes:
- `compose_provider_url()` — absolute URL composition (rejects duplicate `/v1` prefix)
- `build_auth_headers()` — provider-aware auth header construction (`bearer`, `api_key`, `raw_authorization`, `none`)
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

The coordinator calls `_build_upstream_headers()` and `_get_upstream_url()` which use the provider contract when available, falling back to legacy Bearer auth and bare paths respectively.

#### Bearer-prefix guard

`AppConfig.validate_account_credentials()` rejects API keys that begin with the `Bearer` scheme for providers configured with `auth.mode = "bearer"`. EggPool adds the scheme automatically, so a stored `Bearer <token>` would produce `Authorization: Bearer Bearer <token>` upstream and cause 401s. The same guard runs in `scripts/verify_upstream_auth.py` so the operator gets an explicit error before any upstream call. Providers using `auth.mode = "raw_authorization"` are unaffected because they pass the value verbatim.

## Database

SQLite via aiosqlite with WAL mode. Single-connection serialization via a lock + ContextVar.

### Key Invariants

- Every DML write must run inside `async with db.transaction():`
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Child tasks cannot inherit transaction ownership

### Schema Migrations

Ordered SQL migrations in `db/schema/` (0001 through 0020). Checksums tracked in `checksums.json`.

### Repositories

| Repository | Purpose |
|------------|---------|
| `AccountRepository` | Account CRUD, config sync |
| `RequestRepository` | Request lifecycle (pending → selected → completed) |
| `ReservationRepository` | Quota reservations with release/reconciliation |
| `AttemptRepository` | Per-request attempt tracking |
| `UsageWindowRepository` | Aggregated cost queries (5h/7d/30d) |
| `PriceSnapshotRepository` | Model price snapshots |
| `ProviderRepository` | Provider CRUD and config sync |
| `PingRepository` | Provider health ping results |

## Quota and Routing

Routing happens in two stages: a *priority grouping* step picks the highest
non-empty tier of providers, then a `QuotaFairScorer` load-balances inside
that tier.

The grouping step partitions eligible `AccountRuntimeState` records by their
provider's `routing_priority` (default `0`, must be `>= 0`). The router
selects the highest-priority tier that contains at least one eligible account;
if every account in that tier becomes unhealthy, exhausted, or fails pre-body,
the request falls through to the next tier. The `QuotaFairScorer` runs
unchanged against the accounts of the chosen tier, balancing across:

- Quota utilization across 5h/7d/30d windows
- In-flight request penalty
- Health penalty for degraded accounts
- Random tie-breaking for near-equal scores

The `weight` field continues to bias scoring inside a single tier. `weight`
orders accounts within a tier; `routing_priority` orders tiers.

Accounts are excluded from routing when:
- Upstream-observed failure (`quota_exhausted`, `rate_limited`, auth, 5xx) is still inside its bounded backoff window (recovers after cooldown)
- Account is explicitly disabled or suspended by the operator
- Model is not supported by the account (catalog/protocol incompatibility)
- Health circuit breaker is open
- `local_quota_mode = "hard_cap"` is enabled AND local estimate exceeds capacity (opt-in legacy behavior; default is `score_only` advisory)

In the default `score_only` mode, local cost and quota estimates influence
routing **priority** only — above-capacity accounts stay eligible. Only
upstream-observed failures, explicit operator disablement, and catalog/
protocol incompatibility can suppress routing. See
`plans/upstream-authoritative-suppression.md` for the full design.

Upstream-derived backoffs (429, 402, model-unavailable) persist across
restarts in the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`)
and are rehydrated into the in-memory `HealthManager` at startup.
Local-estimate overage never produces a backoff row.

A single request still picks one upstream account. Failover across priority
tiers happens only through the existing `exclude_accounts` retry path.
When every candidate account has been attempted and exhausted mid-request,
the coordinator raises `UpstreamExhaustedError` (502) — synthetic 503 is
reserved for genuine pre-dispatch unavailability (no enabled accounts,
missing credentials, all explicitly disabled, model unknown).

## Provider Routing Priority and Model Collapse

Two related configuration knobs let operators control how requests for the
same base model fan out across providers and how that model appears in the
catalog.

- **`routing_priority`** — `[providers.<id>].routing_priority` is a non-negative
  integer (default `0`). Higher values are preferred. The field is per-provider,
  not per-account: keys of the same provider share a tier and are
  load-balanced by `QuotaFairScorer`.
- **`collapse_models`** — `[models].collapse_models` is a boolean (default
  `false`). When `false`, the catalog exposes one provider-suffixed entry per
  `(model_id, provider_id)`. When `true`, the same base model collapses to a
  single unsuffixed `model_id` and is routed across every provider that
  supports it.

`collapse_models` and `routing_priority` are independent. Either can change
without re-deriving the other. Both require a service restart.

### Default behavior

With defaults (`collapse_models = false`, `routing_priority = 0`), three
providers that all expose `minimax-m2.7` (`opencode-go`, `minimax`,
`generalcompute`) are surfaced as three distinct suffixed model IDs:
`minimax-m2.7/opencode-go`, `minimax-m2.7/minimax`,
`minimax-m2.7/generalcompute`. Each suffixed ID routes only against its own
provider's accounts, load-balanced within the provider.

### Worked example

A `generalcompute`-first / `minimax`-second / `opencode-go`-last ordering
with three `opencode-go` keys load-balancing inside their tier:

```toml
[models]
# collapse_models = false  # default; emit suffixed IDs

[providers.opencode-go]
routing_priority = 0  # load balance within this tier

[providers.minimax]
routing_priority = 2

[providers.generalcompute]
routing_priority = 3  # tried first
```

A request for `minimax-m2.7/generalcompute` first hits the
`generalcompute` accounts (load balanced inside the tier). If every
`generalcompute` account fails pre-body, the coordinator retries the
`minimax` tier, then the `opencode-go` tier. A request for
`minimax-m2.7/opencode-go` only ever hits `opencode-go` accounts regardless
of priority — priority only orders the eligible account set inside one
suffixed (or unsuffixed) model ID.

### Catalog exposure and CLI surface

- `/v1/models` includes an `eggpool.routing_priority` extension field on
  each suffixed entry.
- `eggpool configsetup opencode` generates suffixed IDs when
  `collapse_models = false` and a single unsuffixed ID per base model when
  `collapse_models = true`.
- `eggpool connect` writes `routing_priority = 0` on every newly created
  provider block and leaves existing blocks untouched, so operators can edit
  one number to rebalance.

## Error Hierarchy

```
AggregatorError (base)
├── ConfigError
├── DatabaseError
├── UpstreamError (status_code attribute)
│   ├── TemporaryUpstreamError
│   ├── TransientUpstreamError
│   ├── AuthenticationError
│   ├── QuotaExhaustedError
│   ├── RateLimitError (retry_after attribute)
│   └── ModelUnavailableError
├── ProxyError
├── ModelNotFoundError (model_id attribute)
├── NoEligibleAccountError
├── CatalogUnavailableError
├── AuthenticationUnavailableError
├── UpstreamExhaustedError
├── AccountSuspendedError
├── RequestTooLargeError
└── ContextLimitExceededError
```

## Model Context Limits

EggPool supports configurable effective context limits per model per provider, allowing operators to advertise smaller context windows than the provider physically supports.

### Configuration

- **`ModelLimitOverrideConfig`** — reusable Pydantic model with `max_context_tokens`, `max_input_tokens`, `max_output_tokens`, `enforce_context_limit`
- **Global overrides** — `[model_overrides.<model-id>]` applies to all providers
- **Provider overrides** — `[providers.<id>.model_overrides.<model-id>]` per provider

### Resolution

`ModelLimitResolver` in `catalog/limits.py` resolves effective limits per field with precedence:
1. Provider-specific override
2. Global override
3. Upstream-reported metadata
4. Unknown (None)

### Exposure

- **Unsuffixed models** — `conservative_limits()` takes the minimum across all visible providers
- **Provider-suffixed models** — each provider's exact limits are preserved
- **`/v1/models`** — includes namespaced `eggpool.limits` extension for observability

### OpenCode Integration

`eggpool configsetup opencode --json-only` generates OpenCode provider config with explicit `limit.context`, `limit.input`, and `limit.output` per model. This drives OpenCode's native compaction machinery.

## Security

- Local client credentials are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- API keys stored as environment variable names, never in SQLite
- Constant-time comparison for API key verification
- Fail-closed error detail redaction (configurable)
- Optional CORS and trusted host middleware

## Background Tasks

`TaskSupervisor` (`background/__init__.py`) manages long-running loops with restart-on-failure and exponential backoff:
- Catalog refresh (configurable interval, default 300s)
- Retention cleanup (old requests, events, pings)
- Periodic checkpoint
- Usage window refresh
- Reservation expiry reconciliation
