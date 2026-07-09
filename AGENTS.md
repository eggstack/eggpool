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
- Optional `orjson` backend for the JSON helper: `uv pip install 'eggpool[fast]'` (or `uv sync --extra fast`); see the `eggpool.jsonx` architecture note below. Without this extra, EggPool falls back to a stdlib implementation with identical wire behaviour.

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

# High-concurrency stream stability (OpenCode hardening) subset — stream
# diagnostics, finalization retry queue, routing trace guard, and the
# 50-stream burst integration test.
uv run pytest \
    tests/unit/test_stream_diagnostics.py \
    tests/unit/test_stream_finalization_queue.py \
    tests/unit/test_routing_trace_guard.py \
    tests/integration/test_high_concurrency_streaming.py -v

# High-concurrency reproducer CLI (no real providers; mock SSE upstream).
uv run python scripts/repro_high_concurrency_streams.py \
    --concurrency 50 --cancel-rate 0.25 --cancel-offset 2

# Model-info identity subset (tiered matching, fresh-DB service, evidence API,
# safety, migration 0049, OpenRouter contract, deployment-suffix tier, source
# diagnostics, provenance consistency).  Use the repo-relative script when
# running from outside the repo root to avoid ModuleNotFoundError.
scripts/test_model_info_identity.sh
uv run pytest \
    tests/unit/test_model_info_fresh_db_service.py \
    tests/unit/test_model_info_match_evidence_api.py \
    tests/unit/test_model_info_matching_safety.py \
    tests/unit/test_model_info_migration_0049.py \
    tests/unit/test_model_info_tiered_matching.py \
    tests/unit/test_model_info_openrouter_contract.py \
    tests/unit/test_model_info_deployment_suffix.py \
    tests/unit/test_model_info_source_diagnostics.py \
    tests/unit/test_model_info_provenance_consistency.py -v

# Background task first-run subset (run_immediately, initial_delay_s,
# never_run_not_due vs never_run_overdue labels, source_diagnostics counters)
uv run pytest tests/unit/test_background_first_run.py -v

# Model-info FastAPI route registration order (suites /aliases and /matches
# are pinned before the greedy detail route)
uv run pytest tests/unit/test_model_info_route_registration.py -v

# Lint auto-fix
uv run ruff check --fix src/

# Type check with errors only
uv run pyright src/ scripts/ 2>&1 | head -20
```

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

> All `uv run pytest` commands above assume the Eggpool repo root as the
> working directory.  When invoking from a sibling project root, use the
> repo-relative script instead, which always ``cd``s into the repo root
> before running pytest:
>
> ```bash
> EGGPOOL_REPO=/path/to/eggpool "$EGGPOOL_REPO/scripts/test_model_info_identity.sh"
> ```

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
- **Protocol transcoding**: transparent request/response format conversion between OpenAI and Anthropic protocols. Implemented in `src/eggpool/transcoder/` and `src/eggpool/request/coordinator.py`. The streaming hot path (transcoded-stream-dispatch-fixes) is tuned for high-concurrency coding-agent loads: the coordinator's `IncrementalSSEObserver` is the single observer (the transcoder no longer runs its own), `StreamingTranscoder.feed`/`flush` are synchronous (no per-chunk `await`), translated output per upstream chunk is coalesced via `b"".join(out_chunks)`, `upstream_include_usage` is computed once in `_execute_streaming` and threaded into `_build_stream_generator`, and frame helpers use compact JSON separators `(",", ":")`. The transcoder's `usage` property returns a default `StreamUsageResult()`; finalization must read usage from the coordinator's observer.
- **JSON backend (`eggpool.jsonx`)**: wire bodies, SSE frame helpers, and hot-path request body parsing go through `eggpool.jsonx`. The helper exposes `loads(data)`, `dumps_bytes(obj)`, `dumps_str(obj)`, `active_backend()` and the `USING_ORJSON` flag. The preferred backend is `orjson`; install with `uv pip install 'eggpool[fast]'` (`orjson>=3.10`). When the `fast` extra is not installed (e.g. Raspberry Pi targets where the `orjson` wheel is unavailable), the helper falls back to a stdlib implementation with the same compact-separator behaviour. Override at runtime with `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`. The active backend is logged at startup (`json_backend=orjson|stdlib` in the Granian profile line). Wire bodies, SSE frame helpers, `_safe_json`, `IncrementalSSEObserver._flush_event`, `encode_json_body()`, the transcoding tool-argument stringification, and the request-path body parses all route through the helper; tests under `tests/unit/test_jsonx.py` are parametrised across both backends to keep the contract honest. Off the request path, stdlib `json` is allowed for deterministic hashing (e.g. `cache_stability.py`) and persisted diagnostic metadata (e.g. `finalizer.py`, `segmentation.py`, `prepared.py`, `compression/apply.py`, `compression/analyzer.py`) where the output is not on the wire.
- **Database invariants**: SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML.
- **Quota and routing**: tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning capability mismatches. `TranscodeLossError` (HTTP 400) for loss-policy reject. `ProtocolMismatchError` for endpoint/model-protocol mismatches.
- **Process model**: supervisor + Granian worker (`workers=1`), PID file lifecycle, daemon mode (default for `eggpool serve`; `--verbose` for foreground). Default `runtime_threads=2`, `database_worker_threads=2` (separate read-only stats connection). Startup logs `Granian profile: workers=1 runtime_threads=N database_worker_threads=M access_log=...`.
- **Dashboard**: server-rendered HTML, Chart.js v4, grouped timeseries, CSS tooltips. Dashboard HTML pages use a 30s in-memory cache (`DashboardTelemetry`) for heavy aggregate queries. Telemetry exposes p50/p95 render times and slowest route under `/api/stats/runtime`. Progressive graph hydration (Phase 7) decouples page shell render from chart data: chart pages render a loading shell and hydrate via `data-chart-endpoint` after the shell arrives, with per-namespace/per-period cache TTLs (1h=15s, 24h=30-60s, 7d=120-240s, 30d=240-300s) and rollup-first query behavior suppressing raw fallback for windows >2h. Overview token row uses three explicit cards (`Accounted tokens`, `Fresh tokens`, `Provider cache hit rate`) so cache-heavy workloads (`cache_read > fresh_tokens`) cannot look like an arithmetic bug; the headline `Accounted tokens` card surfaces `input + output + cache_read + cache_write` while `Fresh tokens` keeps the legacy `input + output` semantics. The `Provider cache hit rate` card uses protocol-aware canonical terms: `cache_read_tokens_canonical` (hits), `cache_write_tokens_canonical` (warmup, not hits), and `cache_eligible_input_tokens` (denominator). Per-protocol denominator rules: OpenAI-compatible input is total-billed prompt tokens (`prompt_tokens`); Anthropic denominator is `input_tokens + cache_read + cache_creation` because Anthropic `input_tokens` is fresh-only. Cache writes/creation are never described as hits. Cache read share stays bounded to `cache_read / (input + cache_read + cache_write)`. Stats summary payload exposes `total_tokens` (legacy fresh-token volume, kept for API backward compatibility), `fresh_tokens`, and `accounted_tokens` — the dashboard uses `accounted_tokens` without changing the `total_tokens` API value. Dashboard `/models` page joins canonical model-info summaries to rendered rows via `_normalize_dashboard_model_row()` (splitting provider-suffixed `model_id` into `base_model_id`/`provider_id`/`_model_info_lookup_id`) and surfaces join-failure diagnostics through `ModelInfoDashboardState.matched_row_count` / `unmatched_sample`. `CatalogRowsState` mirrors `ModelInfoDashboardState` for the catalog row builder; the previously-silent `except Exception: return []` blocks now log the traceback and set `degraded_reason="fetch_error"`. The catalog accessor `ModelCatalogCache.get_provider_model_entries()` is the source of truth for the provider-scoped `/models` rows: returns a deterministic `dict[(model_id, provider_id), dict]`, excludes the deprecated placeholder, applies configured capability overrides when `_config` is attached, and emits shallow copies so callers cannot mutate the cache. Unresolved rows (`protocol=None`) are kept so the dashboard can render them as `available=False, catalog_status="unavailable"` rather than silently dropping them.
- **Model capabilities**: protocol-neutral `ThinkingCapability` / `ModelCapabilities` with deterministic merge. Config overrides at `[model_capabilities."<id>".thinking]` and per-provider scoped.
- **Catalog refresh**: non-destructive by default; destructive withdrawal gated on `authoritative=True AND allow_withdrawals=True`. `static_models` is the source of truth for provider-specific protocol.
- **Cache & request-shaping diagnostics**: dashboard `/cache` page renders provider cache counters, request segmentation, compression, policy overrides, native cache preservation, EggPool cache annotations, tuning suggestions, and routing isolation. `QuotaFairScorer` does NOT consume cache, compression, synthetic-cache, or tuning fields — routing stays load-based (request count + token count + active count + health). `tests/unit/test_routing_guardrails.py` pins the invariant.
- **Model info sources**: `src/eggpool/model_info/` enriches model metadata from multiple sources (OpenRouter, Artificial Analysis, HuggingFace, provider catalog) with dedup, identity resolution, and scheduler-driven refresh.
- **Model-info tiered identity matching**: `src/eggpool/model_info/matching.py` resolves local model IDs to source records through 6 tiers (configured_exact_alias → exact_source_id → normalized_exact → **deployment_suffix_normalized_exact (tier 2b, opt-in)** → regex_rule → similarity_guarded). Tier 0 and 1 stay exact; tier 2 uses `normalize_model_key()` on `MiniMax-M3`/`MiniMax M3`/`MiniMax: MiniMax M3` to match `minimax/minimax-m3`; tier 2b conservatively strips `DEPLOYMENT_SUFFIX_TOKENS` (`highspeed`, `fast`, `turbo`, `speed`, `lowlatency`, `lowlat`) only when the original carries a digit/family anchor, the candidate set is unique, and the original does NOT contain any `SEMANTIC_VARIANT_TOKENS` (`pro`, `mini`, `flash`, `lite`, `max`, `plus`, `instruct`, `chat`, `reasoning`, `thinking`, `preview`, `code`, `coder`, `omni`); tier 3 has built-in regex rules for `minimax`, `claude`, `gemini`; tier 4 is guarded `SequenceMatcher` (disabled by default). Non-exact accepted matches persist evidence via `model_info_match_evidence` and stamp `match_method`/`discovered_by`/`diagnostics_json` on `model_info_aliases`. Aggregator provider IDs (e.g. `opencode-go`) are stripped via `strip_provider_namespace`, never treated as vendor namespaces. Artificial Analysis (`ModelInfoService._resolve_aa_record`) shares the same tiered resolver and match-evidence audit trail.
`aliases/matches` endpoints use `_decode_model_info_lookup_id()` for provider-suffix stripping, matching the detail endpoint's canonical lookup.
- **Model-info source diagnostics**: `ModelInfoService.source_diagnostics()` enumerates every configured source (`provider_catalog`, `openrouter`, `artificial_analysis`, `huggingface`) with `configured` / `constructed` / `requires_api_key` / `api_key_present` / `reason` fields. `GET /api/model-info/sources` merges the live `model_info_source_health` snapshot with the diagnostics so operators see why a source has no `last_success_at` row (e.g. `requires_api_key`, `not_constructed`, `disabled`). The handler tolerates both sync and async health snapshots via `inspect.isawaitable` so legacy test doubles that pass `AsyncMock` keep working.
- **Preserved external IDs in provenance**: `build_canonical_detail()` credits `provenance.sources` for any `existing_detail.external_ids[*]` key that wasn't contributed this cycle, populating `provenance.source_states[<src>] = "preserved_external_id"`. Detail cycles therefore distinguish three source states explicitly: `contributed` (newly fetched this cycle), `preserved_external_id` (carried from the prior canonical row because the external ID persists in `external_ids`), and `absent`.
- **Background tasks**: `src/eggpool/background/` manages retention cleanup, periodic tasks, and startup crash recovery via `TaskSupervisor`. First-tick semantics: default callers sleep `interval_s` before the first tick; `initial_delay_s` overrides the first-tick delay; `run_immediately=True` fires the first tick without delay. `run_immediately` and `initial_delay_s` are mutually exclusive. Operator-critical tasks (`update_checker`, `checkpoint`, `model_info_refresh`) are registered with `run_immediately=True` in `app.py:_lifespan_runtime` so the first dashboard paint reflects real state, not "never run". `SupervisedTask.snapshot()` exposes a `first_run_state` field resolved by `_first_run_state()` (one of `last_success` / `last_error` / `never_run_not_due` / `never_run_startup_deferred` / `never_run_overdue`) so the runtime API and dashboard can distinguish a freshly started task that is merely waiting for its first tick from a task that has actually missed its deadline. `/api/stats/runtime` `background_task_summary` carries `never_run_not_due` and `never_run_overdue` counters. Lifespan shutdown stops the supervisor first, then performs the final `metrics_coalescer.flush(reason="shutdown")`.
- **Update checker (`src/eggpool/update_checker.py`)**: two distinct paths share the same PyPI lookup helper but must not share state. `UpdateChecker` (background/periodic, default 24h, `run_immediately=True`) caches `UpdateInfo` for the dashboard footer indicator and `/api/stats/update`. `async_check_for_update()` (CLI one-shot used by `eggpool update`) performs its own live PyPI lookup and MUST NOT read `UpdateChecker.snapshot()` — a stale dashboard cache could mask a real newer release. The CLI helper is freshness-aware: it sends `Cache-Control: no-cache, max-age=0`, `Pragma: no-cache`, and a `User-Agent: eggpool/update-check` header; it appends a `?cb=<monotonic_ns>` cache-bust query parameter; and, when the first fresh response says `latest <= current`, it performs one additional fresh PyPI request with a different cache-bust token before concluding "already up to date". Version comparison goes through the module-level `is_newer_version(current, latest)` helper (PEP 440 ordering backed by `_pep440_key`) — both CLI and `UpdateChecker._is_newer` MUST use it instead of raw string equality, since `0.5.10` vs `0.5.9` and `0.5.9.post1` vs `0.5.9` miscompare lexicographically. `_fetch_pypi_response_sync()` accepts `fresh: bool = False` and `cache_bust_token: str | None = None`; the background checker passes neither, keeping its traffic profile unchanged. The dashboard footer indicator markup wraps the inner pill in `<span class="footer-update-indicator">` so CSS can center the message (`display: flex; justify-content: center; align-items: center; flex-wrap: wrap; width: 100%`) without disturbing the surrounding period/refresh/ready controls.
- **Health management**: `src/eggpool/health/` implements `HealthManager` circuit breaker and per-account health tracking for routing eligibility.
- **Retry classification**: `src/eggpool/retry/` classifies upstream errors for failover and retry decisions.
- **Security**: `src/eggpool/security/` handles header redaction middleware and security utilities.
- **Integrations**: `src/eggpool/integrations/` generates external tool configs (OpenCode, Claude Code, Aider, Codex, etc.).
- **Safe compression**: `src/eggpool/transcoder/compression/` implements observe/safe compression, policy resolution, advisory tuning, and deterministic markers for operator visibility.
- **Replay fixtures**: `tests/fixtures/cache_compression/` holds sanitized JSON fixtures (content-private, never enter routing). `tests/helpers/cache_compression_replay.py` is the harness.
- **Performance hot path (Phases 0–5)**: correctness-preserving optimizations in `src/eggpool/transcoder/` and `src/eggpool/routing/`. Key invariants: `Router.build_routing_plan()` is the authoritative selection path (no fallback to legacy `select_accounts()`); safe-mode `apply_safe_compression()` returns the original payload by identity on no-op; `DispatchSpanRecorder` provides 200-sample dispatch span telemetry. Detailed phase notes live in `architecture/README.md` and the `architecture` skill.
- **High-concurrency stream stability (OpenCode hardening)**: incident-grade diagnostics and bounded retry/reconciliation for sustained coding-agent streaming loads. `StreamDiagnostics` (`src/eggpool/request/stream_diagnostics.py`) records stream outcomes (`stream_completed`, `client_cancelled`, `upstream_midstream_error`, `stream_finalizer_timeout`, `stream_finalizer_failed`) with bounded ring histograms (`completed_ms`, `client_cancel_ms`, `finalizer_timeout_ms`) and is exposed under `/api/stats/runtime`. `FinalizationRetryQueue` (`src/eggpool/request/finalization_queue.py`) drains cancellation finalizations that escaped the 10s shielded path; the supervisor owns the periodic task (active cadence 1.5s, idle 15s, `initial_delay_s=5.0`). `RoutingTraceGuard` (`src/eggpool/request/routing_trace_guard.py`) skips diagnostic routing trace writes when the rolling SQLite lock-wait p95 exceeds `routing.trace.skip_above_lock_wait_p95_ms` (default 200ms) — traces are diagnostic and their absence must never fail dispatch. `_finalize_stale_requests_once` (`src/eggpool/app.py`) reconciles per-account runtime state (router active counts, quota estimator reservations) and emits per-account structured diagnostics. HTTPX error classification in `coordinator.py` distinguishes `PoolTimeout`, `ReadTimeout`, `ConnectTimeout`, `WriteTimeout`, `RemoteProtocolError`, `ReadError`, and `WriteError` from the generic `TimeoutException` so logs show which layer failed. First-class HTTPX diagnostic outcome labels (`upstream_pool_timeout`, `upstream_read_timeout`, `upstream_connect_timeout`, `upstream_write_timeout`, `upstream_protocol_error`, `upstream_connect_error`, `upstream_transport_error`) are exposed in `StreamDiagnostics` and surfaced under `/api/stats/runtime` `stream_diagnostics`. The shared test harness (`tests/helpers/stream_stability_harness.py`) provides canonical scenario names (`slow-stream` as primary, `slow-token-cadence` as alias), SSE helpers, cancellation logic, and the scenario-to-response builder used by both the integration test and the CLI reproducer. The high-concurrency reproducer (`tests/integration/test_high_concurrency_streaming.py`, 50 streams, configurable cancel rate) and the CLI mirror (`scripts/repro_high_concurrency_streams.py`) validate that no requests leak as `pending` after a burst. Operator playbook lives in `docs/opencode-stream-stability.md`; high-concurrency HTTPX profiles in `docs/providers.md`. `Database.contention_snapshot()` surfaces `lock_wait_p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `sample_count` so the routing trace guardrail and dashboard share a consistent lock-pressure view. The `RuntimeMetricsService` async `_snapshot_finalization_retry_queue()` correctly awaits the queue snapshot without coroutine leaks.

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No pre-commit hooks are configured in this repo; CI runs ruff, pyright, and pytest via GitHub Actions
- `static_models` is the source of truth for provider-specific protocol — `FAMILY_PROTOCOLS` is a global fallback. Providers that serve models on a non-default protocol **must** ship `[[providers.<id>.static_models]]` rows with the correct `protocol`, otherwise live `/v1/models` fetch resolves via family prefix and the protocol check clears it to `None`, producing `ModelUnavailableError` instead of `ProtocolMismatchError`.
- Upstream-authoritative suppression: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing.
- **Routing is load-based, not cost-based**: the `QuotaFairScorer` (`src/eggpool/quota/scorer.py`) computes utilization from request count and token count, never from `cost_microdollars`. Cost is unreliable across upstreams (zero reported, unit confusion, heuristics drift) and the metrics we actually balance on are requests served and tokens processed. `cost_*` fields remain on `PersistedWindowSnapshot` and the `requests` table for audit / dashboard display only.
- **Pricing safeguard invariants**: ambiguous bare upstream pricing metadata defaults to dollars-per-million unless explicit token evidence or unambiguous field names prove otherwise; nested `pricing.cache_*` fields inherit the surrounding pricing-cluster unit regime; implausible snapshot rates are rejected before persistence; and `RequestFinalizer` never persists a positive local `estimated` cost as canonical spend when `choose_bounded_estimated_cost()` selects a lower plausible local value. Historical cleanup goes through `eggpool stats repair-costs` (dry-run first), not ad hoc SQL updates.
- **Reservation estimates do NOT floor canonical cost**: reservation estimates are routing budgets and audit fields — they do NOT floor canonical request cost. Precedence: (1) provider-reported cost wins; (2) trusted local `exact`/`derived`/`partial` costs win; (3) local `estimated` and reservation-only estimates both go through `choose_bounded_estimated_cost()` (`src/eggpool/catalog/pricing.py`), which picks the lower plausible value with the reservation as fallback only when the local estimate is implausible. Nothing after `choose_bounded_estimated_cost()` in the finalizer may unconditionally raise canonical cost back to the reservation. Regression fixture: MiniMax provider with `model_id="MiniMax-M3"`, local estimated `21_848` μ$ vs reservation `5_411_079` μ$ — canonical must be `21_848`, not the reservation. See `tests/unit/test_request_finalizer.py::test_estimated_local_cost_beats_higher_reservation_floor_regression`.
- **Model-info lookups are case-insensitive at every layer**: `ModelInfoRepository.get_canonical`, `get_canonical_many`, `get_aliases_for_model`, and `list_compact_observations_for_model` all normalize via `COLLATE NOCASE` or `lower(model_id) = lower(?)`. Stored `model_id` casing is preserved in returned rows.
- **`eggpool update` must always make a live PyPI lookup**: the CLI helper `async_check_for_update()` (`src/eggpool/update_checker.py`) does its own fresh PyPI request and MUST NOT consult `UpdateChecker.snapshot()`. The background checker is intentionally conservative (no freshness bypass); the CLI path is intentionally aggressive (one extra fresh request on a stale first response) so an install decision cannot be made from a stale CDN cache. Use the module-level `is_newer_version()` helper for ordering — never raw string equality.
- **Tiered matching default**: `ModelInfoMatchingConfig.similarity` defaults to `False`; `normalized_exact` and `regex_rules` default to `True`; `deployment_suffix_normalized_exact` defaults to `True` (tier 2b). To enable similarity, set `matching.similarity = True` in `[model_info]` config; to opt out of deployment-suffix stripping set `matching.deployment_suffix_normalized_exact = False`. Discovered alias evidence is persisted by default; pass `persist_discovered_aliases=False` to opt out. Tier 2b refuses to strip any candidate whose original contains a `SEMANTIC_VARIANT_TOKENS` token (`pro`, `mini`, `flash`, `lite`, `nano`, `instruct`, `chat`, `reasoning`, `thinking`, `haiku`, `sonnet`, `opus`, `coder`, `codex`) and never strips when the candidate set is ambiguous.
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
