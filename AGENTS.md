# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — design principles, request lifecycle, invariants, error hierarchy
- `deployment` — production deployment, systemd, operational scripts
- `development` — linting, testing, pre-commit checks, code style

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
- CI installs with `uv sync --frozen --extra dev` (locks match `uv.lock` exactly)
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

## Focused Verification

```bash
uv run pytest tests/unit/test_contract.py -v            # single test file
uv run pytest tests/unit/ -v                             # all unit tests
uv run pytest -k "test_something" -v                     # single test by name
uv run ruff check --fix src/                             # auto-fix lint in one dir
```

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

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
- Shared assets: `src/eggpool/_share/` (bundled config examples for pipx installs)

## Architecture Index

> Full design details are in `architecture/README.md` and the `architecture` skill.

- **Request lifecycle**: `RequestCoordinator` orchestrates endpoint → routing → persistence → dispatch → finalization.
- **Multi-provider architecture**: provider-suffixed model IDs (`model-id/provider-id`), `ProviderClientPool`, `OutboundClientManager`.
- **Provider contracts**: `compose_provider_url()` is the single source of truth for upstream URLs.
- **Protocol transcoding**: transparent request/response format conversion between OpenAI and Anthropic protocols. Implemented in `src/eggpool/transcoder/` and `src/eggpool/request/coordinator.py`.
- **Database invariants**: SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML.
- **Quota and routing**: tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning capability mismatches. `TranscodeLossError` (HTTP 400) for loss-policy reject. `ProtocolMismatchError` for endpoint/model-protocol mismatches.
- **Process model**: supervisor + Granian worker, PID file lifecycle, daemon mode.
- **Dashboard**: server-rendered HTML, Chart.js v4, grouped timeseries, CSS tooltips.
- **Model capabilities**: protocol-neutral `ThinkingCapability` / `ModelCapabilities` with deterministic merge. Config overrides at `[model_capabilities."<id>".thinking]` and per-provider scoped.
- **Catalog refresh**: non-destructive by default; destructive withdrawal gated on `authoritative=True AND allow_withdrawals=True`. `static_models` is the source of truth for provider-specific protocol.
- **Cache & request-shaping diagnostics**: dashboard `/cache` page renders cache reporting, request segmentation, compression opportunities, compression runtime, policy overrides, native cache preservation, synthetic cache controls, advisory tuning, and routing guardrails. The `/runtime` page shows only the summary panel + link. `QuotaFairScorer` does NOT consume cache, compression, synthetic-cache, or tuning fields — routing stays load-based (request count + token count + active count + health). `tests/unit/test_routing_guardrails.py` pins the invariant.
- **Rollup correctness invariants**: `usage_rollups.bucket_start` is `YYYY-MM-DD HH:MM:SS` UTC (matches `format_dt`) — legacy `T...Z` rows are normalized by migration `0047_normalize_rollup_bucket_start.sql`. Cache read share uses the bounded denominator `cache_read / (input + cache_read + cache_write)`, returns `None` when zero. Rollup-backed summaries are gated by `_rollup_is_fresh` so a stalled coalescer cannot under-report the in-flight hour. `fresh_tokens = input + output` and `accounted_tokens = input + output + cache_read + cache_write` are added to the summary payload. Runtime diagnostics expose `rollup_freshness.staleness_seconds` under `/api/stats/runtime`.
- **Model info sources**: `src/eggpool/model_info/` enriches model metadata from multiple sources (OpenRouter, Artificial Analysis, HuggingFace, provider catalog) with dedup, identity resolution, and scheduler-driven refresh.
- **Background tasks**: `src/eggpool/background/` manages retention cleanup, periodic tasks, and startup crash recovery via `TaskSupervisor`. `TaskSupervisor.register(...)` covers daemon-style long-lived coroutines; `TaskSupervisor.register_periodic(...)` covers supervisor-owned periodic scheduling around a one-shot tick factory and records `mode`, `last_tick_started_at`, `last_tick_completed_at`, `next_run_at`, `overdue_seconds`, `success_count`, `failure_count`, `consecutive_failure_count`, and `last_error_class` so healthy sleeping loops no longer render as ``overdue`` (the legacy `last_started_at + interval_s` math is gone — see `plans/background-task-overdue-remediation.md`). Overdue detection uses a 25%-of-interval grace band (clamped to 5-60 s). Background task summary (`registered` / `running` / `failed` / `overdue` / `last_error_count`) is exposed under `/api/stats/runtime` via `background_task_summary`. The `metrics_flush` task is supervisor-driven one-shot now; the lifespan shutdown path performs the final `metrics_coalescer.flush(reason="shutdown")` after `stop_all()` so buffered analytics still hit disk on a clean shutdown. Legacy `_finalize_stale_requests`, `_prune_health_disabled_models_loop`, and `_catalog_refresh_loop` `while True` wrappers are retained as `# pyright: ignore[reportUnusedFunction]` legacy entry points so existing tests still drive the seam-decomposed helpers (`_finalize_stale_requests_once`, `_prune_health_disabled_models_once`, `_catalog_refresh_once`).
- **Health management**: `src/eggpool/health/` implements `HealthManager` circuit breaker and per-account health tracking for routing eligibility.
- **Retry classification**: `src/eggpool/retry/` classifies upstream errors for failover and retry decisions.
- **Security**: `src/eggpool/security/` handles header redaction middleware and security utilities.
- **Integrations**: `src/eggpool/integrations/` generates external tool configs (OpenCode, Claude Code, Aider, Codex, etc.).
- **Safe compression and advanced overrides**: `src/eggpool/transcoder/compression/` implements observe/safe compression, policy resolution, advisory tuning, and the deterministic markers used for operator visibility.
- **Replay fixtures**: `tests/fixtures/cache_compression/` holds sanitized JSON fixtures (content-private, never enter routing). `tests/helpers/cache_compression_replay.py` is the harness. Treat them as developer regression assets, not operator surfaces.

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No pre-commit hooks are configured in this repo; CI runs ruff, pyright, and pytest via GitHub Actions
- `static_models` is the source of truth for provider-specific protocol — `FAMILY_PROTOCOLS` is a global fallback. Providers that serve models on a non-default protocol **must** ship `[[providers.<id>.static_models]]` rows with the correct `protocol`, otherwise live `/v1/models` fetch resolves via family prefix and the protocol check clears it to `None`, producing `ModelUnavailableError` instead of `ProtocolMismatchError`.
- Upstream-authoritative suppression: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing.
- **Routing is load-based, not cost-based**: the `QuotaFairScorer` (`src/eggpool/quota/scorer.py`) computes utilization from request count and token count, never from `cost_microdollars`. Cost is unreliable across upstreams (zero reported, unit confusion, heuristics drift) and the metrics we actually balance on are requests served and tokens processed. `cost_*` fields remain on `PersistedWindowSnapshot` and the `requests` table for audit / dashboard display only.
- **Pricing safeguard invariants**: ambiguous bare upstream pricing metadata defaults to dollars-per-million unless explicit token evidence or unambiguous field names prove otherwise; nested `pricing.cache_*` fields inherit the surrounding pricing-cluster unit regime; implausible snapshot rates are rejected before persistence; and `RequestFinalizer` never persists a positive local `estimated` cost as canonical spend when `choose_bounded_estimated_cost()` selects a lower plausible local value. Historical cleanup goes through `eggpool stats repair-costs` (dry-run first), not ad hoc SQL updates.
- **Reservation-fallback canonicalization** (plans/2026-07-03-...): `selected.estimated_microdollars` is a routing budget, not a bill. The finalizer routes positive local `estimated` cost through `choose_bounded_estimated_cost()` (`src/eggpool/catalog/pricing.py`); when both values are plausible, the lower one wins and nothing later in finalization floors that choice back to the reservation. The reservation ceiling is bounded by `_QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS` ($2.50), well below `MAX_REQUEST_COST_MICRODOLLARS` ($250), so a regression cannot use the reservation as canonical billing. `CostCalculator` validates the **raw, pre-clamp** cost-per-token against `_MAX_TRUSTED_COST_PER_TOKEN_MICRODOLLARS` in both partial and derived paths so an inflated snapshot can no longer hide behind the per-request clamp. `QuotaEstimator.record_usage()` refuses to seed the EWMA on a first observation whose per-token rate exceeds the estimated ceiling, so a unit-misclassification sample cannot permanently poison future reservations. The dashboard's `_render_reservation_fallback_warning` and `reservation_fallback_rows` / `reservation_fallback_excess_microdollars` summary fields surface the failure class so it remains visible after the bug is fixed.
- **Reservation estimates do NOT floor canonical cost**: reservation estimates are routing budgets and audit fields — they do NOT floor canonical request cost. Precedence: (1) provider-reported cost wins; (2) trusted local `exact`/`derived`/`partial` costs win; (3) local `estimated` and reservation-only estimates both go through `choose_bounded_estimated_cost()` (`src/eggpool/catalog/pricing.py`), which picks the lower plausible value with the reservation as fallback only when the local estimate is implausible. Nothing after `choose_bounded_estimated_cost()` in the finalizer may unconditionally raise canonical cost back to the reservation. Regression fixture: MiniMax provider with `model_id="MiniMax-M3"`, local estimated `21_848` μ$ vs reservation `5_411_079` μ$ — canonical must be `21_848`, not the reservation. See `tests/unit/test_request_finalizer.py::test_estimated_local_cost_beats_higher_reservation_floor_regression`.
- **Model-info dashboard visibility is never silent**: dashboard failure paths in `_get_model_info_summary_state` / `handle_model_detail` (`src/eggpool/dashboard/routes.py`) always log the traceback and surface a degraded-state notice in the rendered HTML. The renderer never hides missing model-info; if the subsystem is broken operators see it on the `/models` page and in `eggpool.dashboard.routes` logs. The same contract holds for the detail page.
- **Model-info canonical lookup is case-insensitive at every layer**: `ModelInfoRepository.get_canonical` (`COLLATE NOCASE`) and `get_canonical_many` (`lower(model_id) IN (...)` with `casefold` normalization) agree, and `get_canonical_many` keys the returned dict by the requested id so dashboard callers can use either casing.
- **Model-info alias and observation lookup is case-insensitive**: `get_aliases_for_model`, `list_alias_rows_for_model`, and the new `list_compact_observations_for_model` (Phase 4 of the OpenRouter enrichment plan) all use `WHERE lower(model_id) = lower(?)` so configured aliases and persisted observations remain reachable across provider casing drift (`MiniMax-M3` vs `minimax-m3`). Stored `model_id` casing is preserved in returned rows.
- **Alias candidate selection is deterministic**: `resolve_openrouter_record()` (in `src/eggpool/model_info/identity.py`) uses `choose_alias_candidates()` to prefer exact-case rows over case-folded rows, then `dedupe_alias_strings()` to collapse identical alias strings before declaring ambiguity. Multiple distinct aliases that resolve into the OpenRouter index are kept; multiple that don't resolve fall through to direct/pricing rules. Conflicting folded aliases with no exact-case row produce a clean no-match so the caller can surface `ambiguous_aliases`. Each surviving row is annotated with `match_kind = "exact_case" | "case_folded"` and surfaced under `source_diagnostics.openrouter.alias_rows` so operators can audit the choice.
- **API detail observation fallback is non-misleading**: `_detail_response()` (`src/eggpool/api/model_info.py`) accepts `observations_error`. When the repository read fails, the response returns `observations: []` plus an `observations_error: <ExcClass>` key instead of synthesizing external-source rows. The legacy `_synthetic: true` projection is retained only for direct test-double callers of `_detail_response()`. The dashboard detail page mirrors the contract via `_render_model_observations_section(observations_error=...)`, which renders an "Observation read failed" panel with the error class name when set.
- **OpenRouter source health reflects catalog availability, not match success**: `ModelInfoService.refresh_model_info` / `refresh_due_models` call `record_source_success("openrouter", payload_count=N)` immediately after a successful OpenRouter fetch — regardless of whether any local model matched. `model_info_source_health.last_success_at` and `last_payload_count` therefore describe the external source, not local model coverage.
- **OpenRouter cache bypass on forced refresh**: when configured aliases exist but none match the cached catalog, `OpenRouterModelInfoSource.invalidate_cache()` runs and the forced fetch retries once. The retry is recorded as `cache_retry: true` under `source_diagnostics.openrouter` so operators can see the cache invalidation.
- **API detail observations reflect persisted DB rows**: `handle_model_info_detail()` reads per-source observation rows via `ModelInfoRepository.list_compact_observations_for_model()`. Rows carry real `source_model_id`, `provider_id`, `observed_at`, and `confidence`. Raw payloads are never returned. The dashboard detail page renders the same rows in an Observations panel.
- **Canonical detail display-name promotion**: `build_canonical_detail()` promotes an external `display_name_<source>` (e.g. `display_name_openrouter = "MiniMax: MiniMax M3"`) into `detail.display_name` only when the provider did not seed one. `detail.display_name_source` records the chosen source so operators know where the name came from.
- **Source-scoped advisory pricing**: OpenRouter $/Mtok pricing lives under `detail.pricing.openrouter`, separate from authoritative local cost accounting. The legacy `pricing_observation` block is preserved for back-compat.
- **Manual refresh reseeds configured aliases**: `refresh_model_info()` calls `seed_configured_aliases()` before external-source matching so newly added `[model_info.aliases]` rows (admin tooling, config reload) take effect without a process restart. Seeding is idempotent.
- **Catalog-visible models must have a canonical row path**: `reconcile_catalog_snapshot`, `ensure_canonical`, `get_summary_map`, and `_build_detail` (`src/eggpool/model_info/service.py`) all treat both `catalog._models` and `catalog._provider_models` keys as in-catalog. Provider-scoped rows that never appear in the global model index still receive canonical rows and dashboard pills.
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- `eggpool accounts explain` hydrates the catalog from SQLite, not an empty cache. Output uses `click.echo` (no `rich` dependency).
- Startup crash recovery (`_crash_recovery`) runs at every startup and recovers ALL pending requests and active reservations with no time threshold.
- `CapabilityError` (HTTP 400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503). `BudgetResolutionError` is a subclass of `CapabilityError`.
- When constructing a `RequestCoordinator` in tests, pass an explicit `transcoder_policy` or assert the desired default; never rely on implicit `None`.
- DB migrations are numbered SQL files in `src/eggpool/db/schema/`. The `model_info_*` sidecar tables carry FKs to `models.model_id`; catalog entries may reach model-info paths before `_persist_catalog` writes them to `models`, so repository writes seed a placeholder `models` row in the same transaction.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ModelInfoSourceFetchError`, `ContextLimitExceededError`, `CapabilityError`

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (~74 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
