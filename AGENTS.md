# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — design principles, request lifecycle, invariants, error hierarchy
- `deployment` — production deployment, systemd, operational scripts
- `development` — linting, testing, pre-commit checks, code style

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
- Entry point: `src/eggpool/cli.py` → `eggpool` console script
- Config: `config.toml` + `.env` for API keys

## Pre-commit Checks (run before every commit)

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

All four must pass with zero errors.

## Code Style

- Python 3.11+ with `from __future__ import annotations` in ALL files
- Type hints on all function signatures and return types
- Ruff: E, F, W, I, N, UP, B, A, SIM, TCH rules
- Pyright strict mode — covers `src/` AND `scripts/` (not tests)
- Line length: 88 chars
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- pytest with `asyncio_mode = "strict"` (from `pyproject.toml`)
- respx for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`
- Provider contract tests: `uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v`

## File Organization

- Source: `src/eggpool/`
- Tests: `tests/` (mirrors src structure)
- Config: `config.example.toml`, `.env.example`
- DB schema: `src/eggpool/db/schema/`
- Scripts: `scripts/` (operational, also type-checked by pyright)
- Deployment: `deploy/`

## Multi-Provider Architecture

- Provider-suffixed model IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`)
- `ProviderClientPool` manages per-provider `httpx.AsyncClient` with independent connection pools
- Flat `[[accounts]]` configs auto-normalize to a default `opencode-go` provider
- `parse_model_provider()` and `format_model_provider()` in `routing/provider.py`

## Provider Contracts

Key design rules that are easy to get wrong:

- `compose_provider_url()` is the single source of truth for upstream URLs — catalog fetch, non-streaming chat, and streaming chat all call it
- `build_auth_headers()` reads from `ProviderConfig.auth` — never hardcode Bearer
- `base_url` ending `/v1` + path beginning `/v1/` is rejected (duplicate version prefix)
- `auth.mode = "none"` sends no upstream auth (Ollama local)
- All outbound dispatch paths use `compose_provider_url()` so providers cannot list at one host and dispatch to another

## Bearer-prefix Guard

`AppConfig.validate_account_credentials()` rejects API keys beginning with `Bearer` for `auth.mode = "bearer"` providers. EggPool prepends `Bearer ` automatically; storing `Bearer <token>` produces `Authorization: Bearer Bearer <token>` and causes 401s. Providers using `auth.mode = "raw_authorization"` are unaffected.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ContextLimitExceededError`

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No CI workflows or pre-commit hooks are configured in this repo
- `Database.vacuum()` is the only sanctioned path for `VACUUM` in production code
- Every DML write must run inside `async with db.transaction():`
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- Requests must be persisted before upstream dispatch; pre-body failures can retry, but no retry after first downstream byte

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the aggregation proxy server (default command) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool onboard` | Run the interactive onboarding setup |
| `eggpool connect` | Connect to a new provider interactively |
| `eggpool connect list` | List available providers |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Restart to apply config changes |
| `eggpool models refresh` | Refresh model catalog from upstream |
| `eggpool configsetup opencode` | Print OpenCode provider config JSON with model limits |
| `eggpool db vacuum` | Reclaim SQLite space |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
