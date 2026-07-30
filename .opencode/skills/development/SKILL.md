---
name: development
description: Development workflow for the EggPool project. Use when running linters, type checkers, tests, or pre-commit validation. Covers ruff, pyright, pytest, and the full pre-commit check sequence.
---

# Development Workflow

## Local Development Loop

Fast focused iteration — run only what you changed:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest <affected test paths> -q --tb=short --maxfail=1
```

## Before-Push Check

Run the same checks as the CI job:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## CI

One GitHub Actions job on every PR:

| Job | Python | What it does |
|-----|--------|-------------|
| `check` | 3.11 | ruff format + ruff check + pyright + `pytest tests/smoke/` |

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

## Focused Verification

```bash
# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Integration tests only
uv run pytest -m integration -v

# Network-dependent tests
uv run pytest -m network -v

# Lint auto-fix
uv run ruff check --fix src/
```

## Linting

- **Ruff** for linting and formatting
- Rules: E, F, W, I, N, UP, B, A, SIM, TCH
- Line length: 88 characters
- Target: Python 3.11+

```bash
# Check formatting
uv run ruff format --check src/ tests/ scripts/

# Auto-fix formatting
uv run ruff format src/ tests/ scripts/

# Check lint
uv run ruff check src/ tests/ scripts/

# Auto-fix lint
uv run ruff check --fix src/ tests/ scripts/
```

## Type Checking

- **Pyright** in strict mode
- Covers `src/` AND `scripts/`
- Use `cast` or `Any` rather than excluding files

```bash
uv run pyright src/ scripts/
```

## Testing

- **pytest** with pytest-asyncio (strict mode), `xfail_strict = true`, `--strict-markers`
- **respx** for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/smoke/`, `tests/perf/`, `tests/soak/`, `tests/contract/`

### Test Markers

Defined in `pyproject.toml`:

- **`slow`** — marks tests as slow (deselect with `-m "not slow"`)
- **`performance`** — manually invoked real-runtime performance checks
- **`live`** — opt-in live provider/network verification tests
- **`network`** — tests requiring network access or external services
- **`soak`** — manually invoked real-runtime duration/resource checks
- **`integration`** — integration tests requiring full component wiring
- **`unit`** — unit tests

### Provider Contract Tests

```bash
uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v
```

### Smoke Suite

```bash
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

The smoke suite covers: package import, config parsing, invalid config rejection, check-config validation, DB migration, one non-stream request, one streaming request, one upstream failure followed by recovery, one premature EOF, one Anthropic request, and CLI help.

## Code Style

- Python 3.11+ with `from __future__ import annotations` in all files
- Type hints on all function signatures and return types
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)
- Move type-only imports into `TYPE_CHECKING` blocks
- Follow ruff TCH rules for import organization

## Error Handling

- Use the exception hierarchy in `errors.py`
- Chain exceptions with `raise ... from err` or `raise ... from None`
- Config errors: `ConfigError`
- Database errors: `DatabaseError`
- Upstream errors: `UpstreamError` and subclasses (`AuthenticationError`, `QuotaExhaustedError`, `RateLimitError`, `ModelUnavailableError`)
- Proxy errors: `ProxyError`
- Protocol errors: `ModelNotFoundError`, `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`
- Request errors: `RequestTooLargeError`, `ContextLimitExceededError`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
