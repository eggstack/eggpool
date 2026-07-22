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
uv run python scripts/audit_xfail_skips.py
```

## Focused Verification

Run specific test subsets without waiting for the full suite:

```bash
# Request-path correctness only (routing, transcoding, finalization)
uv run pytest -m request_path -v

# JSON backend parity (eggs/loads/dumps_bytes/dumps_str across stdlib and orjson)
uv run pytest tests/unit/test_jsonx.py -v

# Run jsonx tests against the stdlib fallback to validate parity
EGGPOOL_JSON_BACKEND=stdlib uv run pytest tests/unit/test_jsonx.py -v

# Dashboard and cache-page tests only
uv run pytest -m dashboard -v

# Performance baseline tests only
uv run pytest -m performance -v

# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Lint auto-fix
uv run ruff check --fix src/

# Type check with errors only
uv run pyright src/ scripts/ 2>&1 | head -20

# D2 background-and-observability reload tests (live rehash D2):
uv run pytest \
    tests/unit/test_runtime_task_inventory.py \
    tests/unit/test_d2_transitions.py \
    tests/unit/test_runtime_tasks.py \
    tests/unit/test_background.py \
    -v

# Dispatch-stability baseline (Milestone A5)
uv run pytest tests/perf/test_dispatch_baseline.py -m performance -v

# Reload correctness baseline (Phase 1)
uv run pytest tests/integration/reload/ -v

# Control server validation and socket hardening tests
uv run pytest tests/unit/test_control_server.py -v

# Reload persistence/publication atomicity tests
uv run pytest tests/integration/reload/test_persistence_publication_split.py -v

# Proxy request generation-coherent handling tests
uv run pytest tests/unit/test_proxy_request_hotpath_modes.py -v
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
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`, `tests/perf/`, `tests/soak/`, `tests/live/`

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run coverage run -m pytest
uv run coverage report
```

### Test Markers

Defined in `pyproject.toml` and applied at the module level via `pytestmark`:

**CI partition markers** (define which CI job runs which tests):
- **`integration`** — integration tests (run in unit-integration job).
- **`reload`** — reload and live-rehash tests (dedicated reload-control job).
- **`network`** — tests requiring network access (run in unit-integration job).
- **`soak`** — soak and long-running stability tests (dedicated soak-audit job).

**Feature markers**:
- **`request_path`** — routing, transcoding, finalization, and provider contract tests. Covers `tests/unit/test_routing*.py`, `tests/unit/test_contract*.py`, and `tests/unit/test_request_finalizer.py`.
- **`dashboard`** — dashboard rendering, cache-page, and API endpoint tests. Covers `tests/unit/test_dashboard*.py`, `tests/unit/test_api*.py`, and `tests/unit/test_compression_stats_phase7.py`.
- **`performance`** — performance baseline and regression guards. Covers `tests/perf/test_perf_baseline.py` and `tests/perf/test_perf_regression.py`.
- **`slow`** — marks tests as slow (run in nightly CI, deselect in PR CI).
- **`cache_compression_replay_full`** — full matrix replay for cache/compression fixtures.
- **`live`** — opt-in live external-source tests (requires network access to real APIs).
- **`extended_soak`** — extended-soak mode only tests (stability gates for long-running validation).

### CI Partitions

CI runs 6 parallel jobs on GitHub Actions:

| Job | Python | Command |
|-----|--------|---------|
| lint | 3.12 | `ruff format --check` + `ruff check` |
| typecheck | 3.12 | `pyright src/ scripts/` |
| unit-integration | 3.11, 3.12 | `pytest -m "not slow and not performance and not soak and not extended_soak and not live"` |
| reload-control | 3.11, 3.12 | `pytest tests/integration/reload/` |
| performance | 3.12 | `pytest -m performance` |
| soak-audit | 3.12 | `pytest -m soak` + `audit_xfail_skips.py` |

```bash
uv run pytest -m request_path -v     # routing/transcoding/finalization only
uv run pytest -m dashboard -v        # dashboard and cache-page only
uv run pytest -m performance -v      # performance baseline only
uv run pytest -m "not slow" -v       # skip slow tests
uv run pytest -m soak tests/soak/ -v # short PR soak
uv run pytest tests/soak/ -v         # soak validation and workload profiles
uv run pytest -m extended_soak -v    # extended-soak mode only
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

### Tiered identity matching tests

Tiered identity matching tests cover normalization, 5-tier resolution, and integration:

```bash
# Tiered identity matching tests only
uv run pytest tests/unit/test_model_info_normalization.py tests/unit/test_model_info_tiered_matching.py tests/unit/test_model_info_tiered_integration.py tests/unit/test_model_info_openrouter_contract.py -v
```

### Runtime visibility tests

Cache/compression runtime visibility is covered by three focused unit
test files plus the cache-page acceptance test:

- `tests/unit/test_compression_stats_phase7.py` — query-layer tests
  for `fetch_compression_runtime`, `fetch_compression_policy_stats`,
  `fetch_cache_stability_summary` in `src/eggpool/stats/queries.py`.
- `tests/unit/test_dashboard_cache_page.py` — render-layer tests for
  the cache-page cards (`compression`, `compression_runtime`,
  `compression_policy`, `cache_stability`) and the routing-separation
  notice in `src/eggpool/dashboard/render.py`.
- `tests/unit/test_api_phase7.py` — endpoint-layer tests for the six
  `/api/stats/...` JSON endpoints and auth gating.

The cache-page acceptance set:

```bash
uv run pytest tests/unit/test_compression_stats_phase7.py tests/unit/test_dashboard_cache_page.py tests/unit/test_api_phase7.py -v
```

**Critical rules**:

- Runtime request-shaping test fixtures must enable `dashboard.enabled = True`
  (unlike runtime tests, which use the runtime-only route set and
  leave the dashboard disabled).
- Runtime request-shaping surfaces must NEVER consume cache or compression fields in the
  `QuotaFairScorer`. The pinned invariants are
  `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`
  (Phase 1), the same shape repeated for Phase 2/3/4/5/6/7. If you
  add a new Phase X field, add a corresponding scorer-isolation test
  in `test_routing.py` before wiring it into a dashboard card or
  stats roll-up.
- Runtime dashboard cards must never include raw prompts, tool
  outputs, system messages, or request bodies. The render tests
  assert negative space (`assert "<raw prompt marker>" not in html`)
  not just positive presence.

### Routing guardrails & non-interference tests

Routing guardrails codify the invariant that cache/compression metrics
NEVER enter account scoring, health removal, or route reselection. One
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

- The scoring-input boundary is exactly 4 parameters:
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

### Request finalizer cost precedence

Canonical cost precedence: provider-reported → trusted local exact/derived/partial → bounded-estimated. Reservation estimates are routing budgets, NOT cost floors. The regression test `test_estimated_local_cost_beats_higher_reservation_floor_regression` in `tests/unit/test_request_finalizer.py` pins the invariant. Run with:

```bash
uv run pytest tests/unit/test_request_finalizer.py -v
```

### High-concurrency streaming tests

The OpenCode hardening line ships three focused test files plus the
CLI repro harness:

- `tests/unit/test_stream_diagnostics.py` — counter / histogram
  contract for `StreamDiagnostics`, `Database.contention_snapshot()`
  lock-wait histogram shape, and `RuntimeMetricsService.snapshot()`
  wiring.
- `tests/unit/test_stream_finalization_queue.py` — bounded queue
  semantics (dedup, overflow, max-age, idempotent re-finalize,
  transient failure retry budget, stale-entry skip).
- `tests/unit/test_routing_trace_guard.py` — guard disabled /
  threshold-zero / record_written / db-pressure skip / below
  threshold allow / insufficient-samples / configure / singleton.
- `tests/unit/test_routing_trace_mode.py` — sampled-default and
  `include_score_components = false` defaults, mode transitions.
- `tests/integration/test_high_concurrency_streaming.py` — 50-stream
  burst with configurable cancel rate, asserting the closure
  validation matrix (no leaked pending rows, no active reservations,
  router active counts return to zero, the finalization retry queue
  drains to zero, HTTPX / upstream error class counts are empty for
  the no-failure path, client cancellation does not register as an
  upstream error, provider health remains `healthy`).

Acceptance:

```bash
uv run pytest tests/unit/test_stream_diagnostics.py \
    tests/unit/test_stream_finalization_queue.py \
    tests/unit/test_routing_trace_guard.py \
    tests/unit/test_routing_trace_mode.py \
    tests/integration/test_high_concurrency_streaming.py -v
```

The CLI repro mirror does not need a running pytest and is useful for
local triage:

```bash
uv run python scripts/repro_high_concurrency_streams.py \
    --concurrency 50 --cancel-rate 0.25 --cancel-offset 2
```

Routing trace guard and mode tests can also be run independently:

```bash
uv run pytest tests/unit/test_routing_trace_guard.py tests/unit/test_routing_trace_mode.py -v
```

### Replay fixtures & regression harness

The replay harness pins high-risk request-shaping behavior without ever
shipping a real prompt to disk.

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
  pins stable-prefix preservation, volatile-only mutation,
  provider-bound synthetic cache, native cache_control preservation,
  fail-closed fallback, request-shape hashing, harness surface sanity,
  and routing non-interference. `TestProviderBoundSyntheticReplay` and
  `TestReplaySmoke` cover the provider-bound contract and default-suite
  smoke path.
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

- Replay fixtures are reporting-only. No replay-fixture field lands in
  the database and no replay-fixture field enters
  `QuotaFairScorer.score_accounts`.
- All replay fixtures and helpers MUST stay content-private.  Never
  log raw request content on failure -- emit fixture name + status
  delta only.
- When adding a new request-shaping regression case, drop the fixture JSON
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
