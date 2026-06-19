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

See `architecture/README.md` for details.

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

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the aggregation proxy server (default command) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool models refresh` | Refresh the model catalog from upstream (syncs accounts first) |
| `eggpool accounts status` | Show configured account status and key environment variables |
| `eggpool accounts list` | List configured provider accounts and API key backends |
| `eggpool db vacuum` | Reclaim SQLite space via the lock-owned `Database.vacuum()` helper |
| `eggpool connect` | Interactive provider connection setup |
| `eggpool connect list` | List available providers for connection |
| `eggpool logout` | Remove a configured provider account |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).

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
