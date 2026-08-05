# Deep Dive: Provider Architecture

Back to [Overview](overview.md)

## Purpose

EggPool supports 27+ upstream LLM providers, each with its own base URL, authentication, account pool, supported protocols, and model catalog. The provider layer abstracts these differences behind a unified interface.

## Supported Providers

OpenCode Go, OpenAI, Anthropic, Groq, DeepInfra, Gemini, xAI, Mistral, SiliconFlow, DeepSeek, Together, Fireworks, OpenRouter, Alibaba, MiniMax, and 12+ more. See `docs/providers.md` for the full roster.

## Key Modules

### `providers/contract.py` — Provider Contract

Single source of truth for provider interaction:

- `compose_provider_url()` — absolute URL composition (rejects duplicate `/v1` prefix)
- `build_auth_headers()` — provider-aware auth header construction
  - `bearer` — `Authorization: Bearer <token>`
  - `api_key` — custom header (e.g., `x-api-key`)
  - `raw_authorization` — verbatim value
  - `none` — no auth header
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

All outbound dispatch paths (chat, catalog refresh) share the same `compose_provider_url()` rules.

### `providers/client_pool.py` — ProviderClientPool

Manages per-provider `httpx.AsyncClient` instances:
- Independent connection pools per provider
- Per-provider timeouts
- Optional per-account proxy support (pproxy)
- DNS caching via `DnsNetworkBackend`

### `providers/auth.py`

Provider authentication config parsing. Reads `ProviderAuthConfig` from `config.toml`.

### `providers/outbound.py` — OutboundClientManager

Optional shared `httpx.AsyncClient` for background/CLI network operations
(external pricing, model-info sources, and the opt-in update checker). When
constructed for background work its lean pool target is 8 connections with 2
keepalives; the manager is absent when no enabled feature needs it.

### `providers/dns_cache.py` — DnsNetworkBackend

DNS caching layer on top of httpx transport.

### `providers/pproxy_transport.py`

Per-account pproxy transport integration for proxy support.

### `providers/connect.py`

Provider connect/logout CLI logic. `eggpool connect` writes `routing_priority = 0` on new provider blocks.

### `providers/_templates.toml`

Provider template definitions — pre-configured settings for known providers.

## Provider Configuration

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]
routing_priority = 0

[providers.opencode-go.auth]
mode = "bearer"  # bearer | api_key | raw_authorization | none

[[providers.opencode-go.accounts]]
name = "personal"
api_key_env = "OPENCODE_GO_KEY_1"
```

### Provider-Specific Paths

- `openai_path` (default: `/chat/completions`)
- `anthropic_path` (default: `/messages`)
- `models_endpoint` — table with `method`, `path`, `query`, `body`, `required`
- `models_method` / `models_path` — legacy scalar fields

### Authentication Modes

| Mode | Header | Notes |
|------|--------|-------|
| `bearer` | `Authorization: Bearer <token>` | EggPool prepends scheme automatically |
| `api_key` | Custom header | Provider-specific |
| `raw_authorization` | Verbatim value | No scheme prepended |
| `none` | No auth header | Public endpoints |

### Bearer-Prefix Guard

`AppConfig.validate_account_credentials()` rejects API keys beginning with `Bearer` for providers using `auth.mode = "bearer"`. EggPool adds the scheme automatically; stored `Bearer <token>` would produce `Authorization: Bearer Bearer <token>`.

## Model ID Format

Models exposed with provider-suffixed IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`). `parse_model_provider()` in `routing/provider.py` is the canonical parser.

## Provider Client Pool

Each provider gets its own `httpx.AsyncClient` with:
- Independent connection pool
- Configurable timeouts
- Optional per-account pproxy transport
- DNS caching

## Legacy Flat Config

Legacy `[[accounts]]` configs auto-normalize to a default provider. New deployments should use `[providers.<id>]` blocks.

## MiniMax Templates

- **`minimax`** — international host (`https://api.minimax.io/anthropic`), Anthropic-compatible transport
- **`minimax-cn`** — China host (`https://api.minimaxi.com/v1`), OpenAI paths

## Key Invariants

- `compose_provider_url()` is the single source of truth for upstream URLs
- Same URL composition rules for catalog fetch and chat dispatch
- Bearer-prefix guard prevents double-scheme auth errors
- Provider client pool manages independent connection pools
- Provider-specific paths configurable per provider
- Legacy flat configs auto-normalize to default provider
