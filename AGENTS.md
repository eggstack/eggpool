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

Run specific test subsets without waiting for the full suite:

```bash
# Request-path correctness only (routing, transcoding, finalization)
uv run pytest -m request_path -v

# Dashboard and cache-page tests only
uv run pytest -m dashboard -v

# Performance baseline tests only
uv run pytest -m performance -v

# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Hot-path request-path closure tests (Phases 1–5 + corrective polish + final polish)
uv run pytest tests/unit/test_proxy_request_hotpath_modes.py tests/unit/test_hotpath_corrective_polish.py tests/unit/test_runtime_dispatch_spans_dashboard.py -v

# Lint auto-fix
uv run ruff check --fix src/

# Type check with errors only
uv run pyright src/ scripts/ 2>&1 | head -20
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
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`, `tests/perf/`
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
- **Process model**: supervisor + Granian worker (`workers=1`), PID file lifecycle, daemon mode (default for `eggpool serve`; `--verbose` for foreground). Default `runtime_threads=2`, `database_worker_threads=2` (separate read-only stats connection). Startup logs `Granian profile: workers=1 runtime_threads=N database_worker_threads=M access_log=...`.
- **Dashboard**: server-rendered HTML, Chart.js v4, grouped timeseries, CSS tooltips. Dashboard HTML pages use a 30s in-memory cache (`DashboardTelemetry`) for heavy aggregate queries. Telemetry exposes p50/p95 render times and slowest route under `/api/stats/runtime`. Dashboard `/models` page joins canonical model-info summaries to rendered rows via `_normalize_dashboard_model_row()` (splitting provider-suffixed `model_id` into `base_model_id`/`provider_id`/`_model_info_lookup_id`) and surfaces join-failure diagnostics through `ModelInfoDashboardState.matched_row_count` / `unmatched_sample`. `CatalogRowsState` mirrors `ModelInfoDashboardState` for the catalog row builder; the previously-silent `except Exception: return []` blocks now log the traceback and set `degraded_reason="fetch_error"`. The catalog accessor `ModelCatalogCache.get_provider_model_entries()` is the source of truth for the provider-scoped `/models` rows: returns a deterministic `dict[(model_id, provider_id), dict]`, excludes the deprecated placeholder, applies configured capability overrides when `_config` is attached, and emits shallow copies so callers cannot mutate the cache. Unresolved rows (`protocol=None`) are kept so the dashboard can render them as `available=False, catalog_status="unavailable"` rather than silently dropping them.
- **Model capabilities**: protocol-neutral `ThinkingCapability` / `ModelCapabilities` with deterministic merge. Config overrides at `[model_capabilities."<id>".thinking]` and per-provider scoped.
- **Catalog refresh**: non-destructive by default; destructive withdrawal gated on `authoritative=True AND allow_withdrawals=True`. `static_models` is the source of truth for provider-specific protocol.
- **Cache & request-shaping diagnostics**: dashboard `/cache` page renders provider cache counters, request segmentation, compression, policy overrides, native cache preservation, EggPool cache annotations, tuning suggestions, and routing isolation. `QuotaFairScorer` does NOT consume cache, compression, synthetic-cache, or tuning fields — routing stays load-based (request count + token count + active count + health). `tests/unit/test_routing_guardrails.py` pins the invariant.
- **Model info sources**: `src/eggpool/model_info/` enriches model metadata from multiple sources (OpenRouter, Artificial Analysis, HuggingFace, provider catalog) with dedup, identity resolution, and scheduler-driven refresh.
- **Model-info tiered identity matching**: `src/eggpool/model_info/matching.py` resolves local model IDs to source records through 5 tiers (configured_exact_alias → exact_source_id → normalized_exact → regex_rule → similarity_guarded). Tier 0 and 1 stay exact; tier 2 uses `normalize_model_key()` on `MiniMax-M3`/`MiniMax M3`/`MiniMax: MiniMax M3` to match `minimax/minimax-m3`; tier 3 has built-in regex rules for `minimax`, `claude`, `gemini`; tier 4 is guarded `SequenceMatcher` (disabled by default). Non-exact accepted matches persist evidence via `model_info_match_evidence` and stamp `match_method`/`discovered_by`/`diagnostics_json` on `model_info_aliases`. Aggregator provider IDs (e.g. `opencode-go`) are stripped via `strip_provider_namespace`, never treated as vendor namespaces.
- **Background tasks**: `src/eggpool/background/` manages retention cleanup, periodic tasks, and startup crash recovery via `TaskSupervisor`. First-tick semantics: default callers sleep `interval_s` before the first tick; `initial_delay_s` overrides the first-tick delay; `run_immediately=True` fires the first tick without delay. `run_immediately` and `initial_delay_s` are mutually exclusive. Lifespan shutdown stops the supervisor first, then performs the final `metrics_coalescer.flush(reason="shutdown")`.
- **Health management**: `src/eggpool/health/` implements `HealthManager` circuit breaker and per-account health tracking for routing eligibility.
- **Retry classification**: `src/eggpool/retry/` classifies upstream errors for failover and retry decisions.
- **Security**: `src/eggpool/security/` handles header redaction middleware and security utilities.
- **Integrations**: `src/eggpool/integrations/` generates external tool configs (OpenCode, Claude Code, Aider, Codex, etc.).
- **Safe compression**: `src/eggpool/transcoder/compression/` implements observe/safe compression, policy resolution, advisory tuning, and deterministic markers for operator visibility.
- **Replay fixtures**: `tests/fixtures/cache_compression/` holds sanitized JSON fixtures (content-private, never enter routing). `tests/helpers/cache_compression_replay.py` is the harness.
- **Performance hot path (Phases 0–5)**: correctness-preserving optimizations in `src/eggpool/transcoder/` and `src/eggpool/routing/`. Key invariants: `Router.build_routing_plan()` is the authoritative selection path (no fallback to legacy `select_accounts()`); safe-mode `apply_safe_compression()` returns the original payload by identity on no-op; `DispatchSpanRecorder` provides 200-sample dispatch span telemetry. Detailed phase notes live in `architecture/README.md` and the `architecture` skill.

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No pre-commit hooks are configured in this repo; CI runs ruff, pyright, and pytest via GitHub Actions
- `static_models` is the source of truth for provider-specific protocol — `FAMILY_PROTOCOLS` is a global fallback. Providers that serve models on a non-default protocol **must** ship `[[providers.<id>.static_models]]` rows with the correct `protocol`, otherwise live `/v1/models` fetch resolves via family prefix and the protocol check clears it to `None`, producing `ModelUnavailableError` instead of `ProtocolMismatchError`.
- Upstream-authoritative suppression: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing.
- **Routing is load-based, not cost-based**: the `QuotaFairScorer` (`src/eggpool/quota/scorer.py`) computes utilization from request count and token count, never from `cost_microdollars`. Cost is unreliable across upstreams (zero reported, unit confusion, heuristics drift) and the metrics we actually balance on are requests served and tokens processed. `cost_*` fields remain on `PersistedWindowSnapshot` and the `requests` table for audit / dashboard display only.
- **Pricing safeguard invariants**: ambiguous bare upstream pricing metadata defaults to dollars-per-million unless explicit token evidence or unambiguous field names prove otherwise; nested `pricing.cache_*` fields inherit the surrounding pricing-cluster unit regime; implausible snapshot rates are rejected before persistence; and `RequestFinalizer` never persists a positive local `estimated` cost as canonical spend when `choose_bounded_estimated_cost()` selects a lower plausible local value. Historical cleanup goes through `eggpool stats repair-costs` (dry-run first), not ad hoc SQL updates.
- **Reservation estimates do NOT floor canonical cost**: reservation estimates are routing budgets and audit fields — they do NOT floor canonical request cost. Precedence: (1) provider-reported cost wins; (2) trusted local `exact`/`derived`/`partial` costs win; (3) local `estimated` and reservation-only estimates both go through `choose_bounded_estimated_cost()` (`src/eggpool/catalog/pricing.py`), which picks the lower plausible value with the reservation as fallback only when the local estimate is implausible. Nothing after `choose_bounded_estimated_cost()` in the finalizer may unconditionally raise canonical cost back to the reservation. Regression fixture: MiniMax provider with `model_id="MiniMax-M3"`, local estimated `21_848` μ$ vs reservation `5_411_079` μ$ — canonical must be `21_848`, not the reservation. See `tests/unit/test_request_finalizer.py::test_estimated_local_cost_beats_higher_reservation_floor_regression`.
- **Model-info lookups are case-insensitive at every layer**: `ModelInfoRepository.get_canonical`, `get_canonical_many`, `get_aliases_for_model`, and `list_compact_observations_for_model` all normalize via `COLLATE NOCASE` or `lower(model_id) = lower(?)`. Stored `model_id` casing is preserved in returned rows.
- **Tiered matching default**: `ModelInfoMatchingConfig.similarity` defaults to `False`; normalized_exact and regex_rules default to `True`. To enable similarity, set `matching.similarity = True` in `[model_info]` config. Discovered alias evidence is persisted by default; pass `persist_discovered_aliases=False` to opt out.
- **Provider namespace ≠ vendor namespace**: aggregator providers like `opencode-go` are stripped before matching (`strip_provider_namespace`), never used as vendor prefixes. Only first-party vendor prefixes (`anthropic`, `openai`, `google`, `minimax`, etc.) participate in vendor tie-breaks.
- **Match evidence audit trail**: every non-exact accepted match writes a `model_info_match_evidence` row with `match_method`, `confidence`, and `diagnostics_json`. Inspect via `repo.list_match_evidence(model_id, source=...)`, `GET /api/model-info/{id}/matches`, or `SELECT * FROM model_info_match_evidence`. The detail endpoint also includes a compact `match_evidence` field.
- **Model-info dashboard visibility is never silent**: dashboard failure paths always log the traceback and surface a degraded-state notice in the rendered HTML. The renderer never hides missing model-info.
- **OpenRouter source health reflects catalog availability, not match success**: `record_source_success("openrouter", payload_count=N)` fires after a successful fetch regardless of local match count.
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
