# AGENTS.md

Development guidelines for the opencode-go-aggregator project.

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
- All 287+ tests must pass before committing

## Pre-commit Checks

Run before every commit:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

All must pass with zero errors.

## Architecture Principles

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification
- All data-plane requests flow through `RequestCoordinator`
- SQLite is the durable source of truth for quota windows (5h/7d/30d)
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted

For detailed architecture documentation, see `architecture/` directory:
- `phase-0.md`: Repository and tooling foundation
- `phase-1.md`: Configuration, database, and application lifecycle
- `phase-2.md`: Account registry and model discovery
- `phase-3.md`: Non-streaming transparent proxy
- `phase-4.md`: Streaming proxy
- `phase-5.md`: Usage extraction and price accounting
- `phase-6.md`: Quota-aware routing and reservations
- `phase-7.md`: Retry, failover, and health management
- `phase-8.md`: Statistics API and dashboard
- `phase-9.md`: Deployment hardening
- `phase-10-integration-hardening.md`: Integration hardening and correct request lifecycle

## Import Organization

Follow ruff TCH rules:
- Move type-only imports into `TYPE_CHECKING` blocks
- Use `from __future__ import annotations` to enable forward references

## Error Handling

- Use the exception hierarchy in `errors.py`
- Config errors: `ConfigError`
- Database errors: `DatabaseError`
- Upstream errors: `UpstreamError` and subclasses
- Protocol errors: `ModelNotFoundError`, `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`
- Chain exceptions with `raise ... from err` or `raise ... from None`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
- Run all checks before committing

## File Organization

- Source code: `src/go_aggregator/`
- Tests: `tests/` (mirrors src structure)
- Configuration: `config.example.toml`, `.env.example`
- Database schema: `src/go_aggregator/db/schema/`
