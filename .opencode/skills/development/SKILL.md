---
name: development
description: Development workflow for the EggPool project. Use when running linters, type checkers, tests, or pre-commit validation. Covers ruff, pyright, pytest, and the full pre-commit check sequence.
---

# Development Workflow

## Pre-commit Checks

Run before every commit. All must pass with zero errors:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
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

- **pytest** with pytest-asyncio (strict mode)
- **respx** for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run coverage run -m pytest
uv run coverage report
```

### Provider Contract Tests

Run contract-specific tests:
```bash
uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v
```

### URL Composition Tests

`compose_provider_url()` is the single source of truth for upstream URL
construction. Catalog fetch, non-streaming chat, and streaming chat all
call it through the provider config. Verify the consistency with:

```bash
uv run pytest tests/unit/test_contract_urls.py tests/unit/test_fetcher.py tests/unit/test_coordinator_provider.py -v
```

### Provider Routing Priority Tests

Tier-based routing is tested in `tests/unit/test_routing_priority.py`.
Key test classes:

- `TestGroupByPriority` — pure `_group_by_priority()` helper
- `TestRouterTieredSelection` — end-to-end tier selection, fall-through, failover ordering
- `TestMixedPriorityLoadBalance` — mixed priorities with load balance within tier
- `TestTierFallthroughOnCooldown` — top tier in cooldown falls through to lower tier
- `TestFailoverTierBoundary` — `exclude_accounts` skips tiers, failover list is contiguous by tier

Run with:

```bash
uv run pytest tests/unit/test_routing_priority.py -v
```

### Phase 7 — Cache/Compression Dashboard & Runtime Views Tests

Phase 7 unifies the data from Phases 1–6 into dashboard cards and
runtime API endpoints. Three focused unit test files cover the new
surface:

- `tests/unit/test_compression_stats_phase7.py` — query-layer tests
  for `fetch_compression_runtime`, `fetch_compression_policy_stats`,
  `fetch_cache_stability_summary` in `src/eggpool/stats/queries.py`.
- `tests/unit/test_dashboard_phase7.py` — render-layer tests for the
  four new runtime cards (`compression`, `compression_runtime`,
  `compression_policy`, `cache_stability`) and the routing-separation
  notice in `src/eggpool/dashboard/render.py`.
- `tests/unit/test_api_phase7.py` — endpoint-layer tests for the six
  `/api/stats/...` JSON endpoints and auth gating.

All three together are the Phase 7 acceptance test set:

```bash
uv run pytest tests/unit/test_compression_stats_phase7.py tests/unit/test_dashboard_phase7.py tests/unit/test_api_phase7.py -v
```

**Critical rules**:

- Phase 7 test fixtures must enable `dashboard.enabled = True`
  (unlike runtime tests, which use the runtime-only route set and
  leave the dashboard disabled).
- Phase 7 must NEVER consume cache or compression fields in the
  `QuotaFairScorer`. The pinned invariants are
  `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`
  (Phase 1), the same shape repeated for Phase 2/3/4/5/6/7. If you
  add a new Phase X field, add a corresponding scorer-isolation test
  in `test_routing.py` before wiring it into a dashboard card or
  stats roll-up.
- Phase 7 dashboard cards must never include raw prompts, tool
  outputs, system messages, or request bodies. The render tests
  assert negative space (`assert "<raw prompt marker>" not in html`)
  not just positive presence.

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
- Run all checks before committing
