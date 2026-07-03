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

### Phase 8 — Routing Guardrails & Non-Interference Tests

Phase 8 codifies the invariant that cache/compression metrics NEVER
enter account scoring, health removal, or route reselection. One
focused test file is the regression surface:

- `tests/unit/test_routing_guardrails.py` — 19 tests across 7 classes:
  - `TestScorerIgnoresPhase5Fields` — `inspect.signature` audit on
    `QuotaFairScorer.score_accounts` plus behavioural pin (identical
    load with adversarial cost produces identical scores).
  - `TestScorerAcceptsNoCacheOrCompressionParameter` — static substring
    audit on both scorer parameters and `RoutingScore` fields.
  - `TestSameProviderFairnessUnderAdversarialCacheAndCompression` —
    5 scenarios run through `Router.select_account` 40 times each,
    asserting fair rotation despite skewed cache hits / compression
    savings / stable-prefix hashes.
  - `TestCompressionFallbackDoesNotAffectHealth` — Phase 5 fail-closed
    fallback never touches `HealthManager`.
  - `TestPolicyResolverDoesNotAffectRouting` — Phase 6 resolver is
    information-only, no accounts removed.
  - `TestNoPostCompressionReroute` — `score_accounts` signature is
    exactly 4 parameters; router uses scorer once per attempt.
  - `TestRuntimeDiagnosticSurface` — pins the `guardrails` field shape
    on `RuntimeMetricsService._snapshot_routing_runtime`.

**Critical rules**:

- Phase 8 scoring-input boundary is exactly 4 parameters:
  `account_names`, `model_name`, `active_requests`, `request_estimates`.
  Adding any cache/compression/policy/stable-prefix field to
  `QuotaFairScorer.score_accounts` or to `RoutingScore` is a Phase 8
  regression and MUST fail this test file.
- Same-provider fairness must hold under adversarial cache/compression
  metrics. If you add a new cache/compression column, add a fairness
  scenario here.
- Compression fallback (`apply_safe_compression`'s
  `failed_fallback=True`) MUST NOT increment provider error counters
  or write `account_backoffs` rows. Health remains upstream-observed.
- `resolve_compression_policy` MUST NOT mutate the route. Provider-
  specific match fields (`match_provider_ids`, `match_provider_kinds`,
  `match_models`) are silently skipped pre-route; never reroute to
  satisfy a policy override.
- The `guardrails` field on `routing_runtime` is HARDCODED constants.
  It must always read `"reporting_only"` / `false` / the canonical
  scorer input list. Never derive it from request content.

Acceptance:

```bash
uv run pytest tests/unit/test_routing_guardrails.py -v
```

### Phase 11 -- Replay Fixtures & Regression Harness

Phase 11 ships a tiny test-only replay harness so operators can pin
down the high-risk Phase 2/3/5/9 behaviour without ever shipping a real
prompt to disk.

- **Fixtures** -- `tests/fixtures/cache_compression/{openai,anthropic,transcode,routing,stats}/*.json`
  plus `tests/fixtures/cache_compression/README.md` (schema, sentinel
  reference, sanitization rules, repeat expansion, **replay-shape
  semantics**).  All prompts use the seven sentinel strings; any new
  fixture MUST follow the README.
- **Helper module** -- `tests/helpers/cache_compression_replay.py`
  exposes `load_fixture`, `expand_repeats`, `run_full_replay`,
  `run_provider_bound_synthetic_replay` (explicit provider-bound
  lifecycle for transcode fixtures), `ReplayBundle`,
  `safe_policy`/`observe_policy`/`disabled_policy`,
  `synthetic_cache_config`, `run_segmentation`/`run_compression`/
  `run_transcode`/`run_synthetic`, `path_keys`, `collect_segment_strings`.
- **Regression suite** -- `tests/unit/test_replay_fixtures_regression.py`
  pins 8 invariants: stable-prefix preservation, volatile-only
  mutation, provider-bound synthetic cache, native cache_control
  preservation, fail-closed fallback, request-shape hashing, harness
  surface sanity, and routing non-interference.  Phase 12 polish pass
  added `TestProviderBoundSyntheticReplay` (provider-bound contract) and
  `TestReplaySmoke` (cheap default-suite smoke) classes.
- **Replay shape semantics** -- `run_full_replay()` records which shape
  was used via `ReplayBundle.synthetic_cache_shape`:
  - `disabled` -- no `synthetic_cache` config supplied
  - `client_bound` -- synthetic cache ran on the client-shape payload
    (used when `client_protocol == target_protocol`)
  - `provider_bound` -- transcode ran first, synthetic cache ran on the
    provider-bound body using `target_protocol` (matches production)
  - `provider_bound_unavailable` -- transcode produced no provider body;
    no synthetic cache applied
- **Sanitization linter** -- `tests/unit/test_replay_fixtures_sanitization.py`
  enforces no bearer tokens, no `sk-...` keys, no `Authorization:`
  lines, no oversized strings, no real prompt text, and unique fixture
  names.

**Critical rules**:

- Phase 11 is reporting-only.  No Phase 11 column lands in the database
  and no Phase 11 field enters `QuotaFairScorer.score_accounts`.
- All replay fixtures and helpers MUST stay content-private.  Never
  log raw request content on failure -- emit fixture name + status
  delta only.
- When adding a new Phase 2/5/9 regression case, drop the fixture JSON
  under the right subdirectory and assert via the harness helpers;
  do not import production DB code.
- The sanitization linter MUST pass before any fixture can land.
- For transcode fixtures, prefer `run_provider_bound_synthetic_replay()`
  when you need to assert provider-bound synthetic-cache behaviour;
  `run_full_replay()` already runs synthetic cache on the provider-bound
  body for transcode fixtures but the dedicated helper makes the
  intent explicit.

Acceptance (default smoke coverage; runs on every pytest):

```bash
uv run pytest tests/unit/test_replay_fixtures_regression.py tests/unit/test_replay_fixtures_sanitization.py -v
```

Full matrix (exhaustive modes x fixtures; gated by the
`cache_compression_replay_full` marker):

```bash
uv run pytest -m cache_compression_replay_full tests/unit/test_replay_fixtures_regression.py -v
```

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
