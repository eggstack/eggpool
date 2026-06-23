# AGENTS.md

Development guidelines for the EggPool project.

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — [`.opencode/skills/architecture/SKILL.md`](.opencode/skills/architecture/SKILL.md) — Design principles, request lifecycle, invariants, error hierarchy
- `deployment` — [`.opencode/skills/deployment/SKILL.md`](.opencode/skills/deployment/SKILL.md) — Production deployment, systemd, operational scripts
- `development` — [`.opencode/skills/development/SKILL.md`](.opencode/skills/development/SKILL.md) — Linting, testing, pre-commit checks, code style

## Architecture

See `architecture/README.md` for a high-level design overview covering request lifecycle, multi-provider architecture, database invariants, quota/routing, and error hierarchy.

## Code Style

- Python 3.12+ with `from __future__ import annotations` in all files
- Type hints on all function signatures and return types
- Ruff for linting (E, F, W, I, N, UP, B, A, SIM, TCH rules)
- Pyright in strict mode
- Line length: 88 characters
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- pytest with pytest-asyncio (strict mode)
- respx for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`
- Run: `uv run pytest`
- All tests must pass before committing

## Pre-commit Checks

Run before every commit:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

All must pass with zero errors.

## Multi-Provider Architecture

EggPool supports multiple upstream providers. Key components:

- **`ProviderConfig`** — per-provider base URL, protocols, account pool, upstream paths
- **`ProviderClientPool`** — per-provider `httpx.AsyncClient` with independent connection pools
- **Provider-suffixed model IDs** — `model-id/provider-id` format (e.g., `claude-sonnet-4/opencode-go`)
- **`routing/provider.py`** — `parse_model_provider()` and `format_model_provider()` utilities
- **Flat config auto-normalization** — legacy `[[accounts]]` configs become a default `opencode-go` provider

### MiniMax Templates

- `minimax` — international host `https://api.minimax.io/v1` (default for `minimax.io` keys)
- `minimax-cn` — China host `https://api.minimaxi.com/v1`

Both are OpenAI-only and use `bearer` auth. API keys must be raw tokens; EggPool prepends `Bearer ` automatically.

### Provider Roster

EggPool ships templates for 27+ providers across verified, experimental, and unverified tiers. See `docs/providers.md` for the full catalog.

**Verified** (API-key auth confirmed): opencode-go, openai, anthropic, openrouter, deepseek, together, fireworks, groq, deepinfra, gemini, xai, mistral, siliconflow, alibaba, ollama-local

**Experimental** (live-verification-required): zai, novita, minimax, minimax-cn, generalcompute, neuralwatt, ollama-cloud, cerebras, sambanova, hyperbolic, featherless, moonshot

Use `eggpool connect list` to see available providers and `eggpool connect <id>` for interactive setup.

See `architecture/README.md` for details.

## Provider Contracts

Each provider declares an explicit contract for authentication, URL composition, and model listing:

- **`ProviderAuthConfig`** — auth mode (`bearer`, `api_key`, `raw_authorization`, `none`), header name, scheme
- **`ProviderStaticHeaderConfig`** — optional static headers (e.g., attribution headers for OpenRouter)
- **`ProviderModelsEndpointConfig`** — model listing method, path, body, query params
- **`ProviderVerifyConfig`** — live verification probe settings (probe_model, probe_protocol)

Key design rules:
- `base_url` ending `/v1` plus path beginning `/v1/` is rejected (duplicate version prefix)
- `auth.mode = "none"` sends no upstream auth (used by Ollama local)
- `compose_provider_url()` always produces absolute URLs for HTTPX
- `build_auth_headers()` reads from `ProviderConfig.auth` instead of hardcoding Bearer
- All outbound dispatch paths (catalog fetch, non-streaming chat, streaming chat) use `compose_provider_url()` so providers cannot list at one host and dispatch to another

### Bearer-prefix guard

`AppConfig.validate_account_credentials()` rejects API keys that begin with the `Bearer` scheme for `auth.mode = "bearer"` providers. EggPool adds the scheme automatically; a stored `Bearer <token>` would produce `Authorization: Bearer Bearer <token>` upstream and cause 401s. The same guard runs in `scripts/verify_upstream_auth.py` so the operator gets an explicit error before any upstream call. Providers using `auth.mode = "raw_authorization"` are unaffected because they pass the value verbatim.

See `src/eggpool/providers/contract.py` for the centralized renderer.

## Provider Routing Priority and Model Collapse

Two configuration knobs control how requests for the same base model fan out
across providers and how the model appears in the catalog:

- **`routing_priority`** — `[providers.<id>]` accepts `routing_priority: int`
  with `Field(default=0, ge=0)`. Higher values are preferred. The field is
  per-provider; accounts inside a tier are still load-balanced by
  `QuotaFairScorer`. The router groups eligible accounts by priority, picks
  the highest non-empty tier, and falls through to the next tier only on
  pre-body failure or exhaustion.
- **`collapse_models`** — `[models]` accepts `collapse_models: bool` (default
  `false`). When `false`, the catalog exposes one provider-suffixed entry per
  `(model_id, provider_id)`. When `true`, the same base model collapses to a
  single unsuffixed `model_id` and is routed across every provider that
  supports it.

The two knobs are independent. `collapse_models` changes the catalog shape;
`routing_priority` changes selection order inside that shape. A single
request still picks one upstream account. Configuration changes require a
service restart.

CLI surface:

- `eggpool connect` writes `routing_priority = 0` on every newly created
  provider block and leaves existing blocks untouched, so operators can edit
  one number to rebalance.
- `eggpool configsetup opencode` honors `collapse_models`: suffixed model
  IDs when `false`, unsuffixed when `true`.
- `/v1/models` includes an `eggpool.routing_priority` extension field on
  each suffixed entry.

See `plans/provider_priority.md` for the full design and `docs/providers.md`
for the worked example with three providers and three priorities.

## Model Context Limits

EggPool supports configurable effective context limits per model per provider:

- **`ModelLimitOverrideConfig`** — reusable limit fields (context, input, output tokens, enforcement)
- **`ModelOverrideConfig`** — global overrides (inherits limit fields + protocol, pricing)
- **`ProviderConfig.model_overrides`** — per-provider limit overrides
- **`catalog/limits.py`** — `ModelLimitResolver`, `EffectiveModelLimits`, `conservative_limits()`
- **Precedence**: provider override > global override > upstream metadata > unknown
- **Unsuffixed models** use conservative minimum across all providers
- **`eggpool configsetup opencode --json-only`** generates OpenCode config with model limits

See `docs/model-limits.md` for operator documentation.

## Error Handling

Use the exception hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` — base for all aggregator errors
- `ConfigError` — invalid or missing configuration
- `DatabaseError` — database-related failures
- `UpstreamError` — base for upstream API errors (`status_code` attribute)
  - `TemporaryUpstreamError` — temporary upstream errors (502, 503, 504)
  - `TransientUpstreamError` — transient upstream errors (retries may succeed)
  - `AuthenticationError` — upstream rejects credentials
  - `QuotaExhaustedError` — upstream account quota exhausted
  - `RateLimitError` — upstream rate-limited (`retry_after` attribute)
  - `ModelUnavailableError` — model not available upstream
- `ProxyError` — general proxy/transport errors
- `ModelNotFoundError` — requested model does not exist (`model_id` attribute)
- `NoEligibleAccountError` — no account can serve the request (503)
- `CatalogUnavailableError` — model catalog not available (503)
- `AuthenticationUnavailableError` — upstream credentials cannot be loaded (503)
- `UpstreamExhaustedError` — all upstream attempts exhausted (502)
- `AccountSuspendedError` — account suspended (503)
- `RequestTooLargeError` — request body exceeds configured limit
- `ContextLimitExceededError` — estimated request context exceeds configured model limit

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the aggregation proxy server (default command) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool onboard` | Run the interactive onboarding setup |
| `eggpool connect` | Connect to a new provider interactively |
| `eggpool connect list` | List available providers for connection |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Restart the server to apply configuration changes |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool stop` | Stop the running server |
| `eggpool set` | Set a server configuration value and restart |
| `eggpool getkey` | Print the current server API key |
| `eggpool newkey` | Generate a new server API key |
| `eggpool edit` | Open the configuration file in the default editor |
| `eggpool configsetup` | Print configuration snippets for code editors |
| `eggpool configsetup opencode` | Print OpenCode provider config JSON with model limits |
| `eggpool configsetup claude-code` | Print Claude Code config snippet |
| `eggpool update` | Check for updates and reinstall if newer |
| `eggpool models refresh` | Refresh the model catalog from upstream (syncs accounts first) |
| `eggpool accounts status` | Show configured account status and key environment variables |
| `eggpool accounts list` | List configured provider accounts and API key backends |
| `eggpool dashboard public` | Toggle dashboard public access |
| `eggpool db vacuum` | Reclaim SQLite space via the lock-owned `Database.vacuum()` helper |
| `eggpool init-config` | Write bundled config.example.toml to current directory or TARGET |
| `eggpool deploy systemd` | Print the systemd unit + install instructions |
| `eggpool deploy logrotate` | Print the logrotate config + install instructions |
| `eggpool deploy cron` | Print the daily-backup cron entry + install instructions |
| `eggpool deploy all` | Print every deployment snippet in sequence |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).
Configuration changes require a service restart; live reload is intentionally
not supported.

## Import Organization

Follow ruff TCH rules:
- Move type-only imports into `TYPE_CHECKING` blocks
- Use `from __future__ import annotations` to enable forward references

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
- Run all checks before committing

## File Organization

- Source code: `src/eggpool/`
- Tests: `tests/` (mirrors src structure)
- Configuration: `config.example.toml`, `.env.example`
- Database schema: `src/eggpool/db/schema/`
- Operational scripts: `scripts/`
- Deployment files: `deploy/`
- Documentation: `docs/`
- Architecture: `architecture/`
- Config examples: `config-examples/` (OpenCode JSONC, Claude Code env)
