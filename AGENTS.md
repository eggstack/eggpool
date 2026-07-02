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
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning capability mismatches.
- **Process model**: supervisor + Granian worker, PID file lifecycle, daemon mode.
- **Dashboard**: server-rendered HTML, Chart.js v4, grouped timeseries, CSS tooltips.
- **Model capabilities**: protocol-neutral `ThinkingCapability` / `ModelCapabilities` with deterministic merge. Config overrides at `[model_capabilities."<id>".thinking]` and per-provider scoped.
- **Catalog refresh**: non-destructive by default; destructive withdrawal gated on `authoritative=True AND allow_withdrawals=True`. `static_models` is the source of truth for provider-specific protocol.
- **Cache observability (Phase 1)**: every finalized request carries a `cache_counter_status` of `reported` / `not_reported` / `unknown_format`. `QuotaFairScorer` does NOT consume cache fields (asserted by `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`).
- **Canonical request segmentation (Phase 2)**: every finalized request is annotated into `stable_prefix` / `semi_stable_context` / `volatile_suffix` regions by `eggpool.transcoder.segmentation.segment_request`. The segmenter is observational — it never mutates payloads, never changes routing, and never raises on malformed input. Conservative default: when classification is uncertain the segment lands in `semi_stable_context`. Stable prefix hash and request shape hash are content-private (SHA-256 of structural descriptors, never raw prompt text). Migration `0041` adds seven columns to `requests` and a `segmentation_status` index; `EXPECTED_SCHEMA_VERSION` in `scripts/check_database.py` is 41.

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No pre-commit hooks are configured in this repo; CI runs ruff, pyright, and pytest via GitHub Actions
- `static_models` is the source of truth for provider-specific protocol — `FAMILY_PROTOCOLS` is a global fallback. Providers that serve models on a non-default protocol **must** ship `[[providers.<id>.static_models]]` rows with the correct `protocol`, otherwise live `/v1/models` fetch resolves via family prefix and the protocol check clears it to `None`, producing `ModelUnavailableError` instead of `ProtocolMismatchError`.
- Upstream-authoritative suppression: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing.
- **Routing is load-based, not cost-based**: the `QuotaFairScorer` (`src/eggpool/quota/scorer.py`) computes utilization from request count and token count, never from `cost_microdollars`. Cost is unreliable across upstreams (zero reported, unit confusion, heuristics drift) and the metrics we actually balance on are requests served and tokens processed. `cost_*` fields remain on `PersistedWindowSnapshot` and the `requests` table for audit / dashboard display only.
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- `eggpool accounts explain` hydrates the catalog from SQLite, not an empty cache. Output uses `click.echo` (no `rich` dependency).
- Startup crash recovery (`_crash_recovery`) runs at every startup and recovers ALL pending requests and active reservations with no time threshold.
- `CapabilityError` (HTTP 400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503). `BudgetResolutionError` is a subclass of `CapabilityError`.
- When constructing a `RequestCoordinator` in tests, pass an explicit `transcoder_policy` or assert the desired default; never rely on implicit `None`.
- DB migrations are numbered SQL files in `src/eggpool/db/schema/`. The `model_info_*` sidecar tables carry FKs to `models.model_id`; catalog entries may reach model-info paths before `_persist_catalog` writes them to `models`, so repository writes seed a placeholder `models` row in the same transaction.
- **Phase 1 cache observability is reporting-only**: every finalized request is tagged with `cache_counter_status` ∈ {`reported`, `not_reported`, `unknown_format`} and supporting cache-token columns. The `QuotaFairScorer` does NOT consume cache fields (asserted by `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`); only request count + token count + cost (audit) + active count + health feed routing. See `plans/cache_compression_phase_01_cache_token_observability.md`.
- When adding new Phase 1+ work that consumes cache columns, prefer reading the persisted DB columns over re-parsing the upstream payload — `normalize_usage` is only safe at request finalization time.
- **Phase 2 segmentation is observational**: `eggpool.transcoder.segmentation` produces metadata only. It never mutates request bodies, never changes routing, and never raises on malformed input. Empty requests yield `SegmentationStatus.EMPTY_REQUEST`; non-mapping payloads yield `SegmentationStatus.PARSE_FAILURE`. The `FinalizationData.segmentation` field is duck-typed as `Any | None` so the finalizer does not import the segmenter directly. Migration `0041` is non-destructive — legacy callers without segmentation render as `segmentation_status='empty_request'`. See `plans/cache_compression_phase_02_canonical_request_segmentation.md`.
- **Phase 3 transcoder cache stability is observational**: `eggpool.transcoder.cache_stability` produces metadata only. The new `CacheBoundaryTracker` carried on `TranscodeContext.cache_boundary_tracker` records every `cache_control` boundary event (preserved / relocated / dropped_unsupported_target / dropped_feature_disabled / dropped_invalid_shape / synthesized) with `source_protocol`, `target_protocol`, `source_path`, `target_path`, and `cache_control_type`. The tracker is append-only and capped at 64 annotations per request; over-cap events increment `dropped_count`. Both transcoders emit structured loss warnings (`cache_control_unsupported_by_target_protocol`, `cache_control_feature_disabled`, `cache_control_invalid_shape`, `provider_extension_not_preserved`, `stable_prefix_preserved`, `stable_prefix_reordered_canonically`) so dashboards can attribute cache hit-rate loss without re-parsing the upstream payload. The `QuotaFairScorer` still does NOT consume cache fields; cache stability is reporting-only and lives alongside Phase 1+2 observability. See `plans/cache_compression_phase_03_transcoder_cache_stability.md`.
- `cache_control_feature_disabled` is emitted for top-level Anthropic `cache_control`; `cache_control_unsupported_by_target_protocol` is emitted for nested annotations (system blocks, message blocks, tool definitions) that OpenAI cannot carry. The OpenAI→Anthropic transcoder preserves `tools[].cache_control` annotations as a structured `preserved` boundary entry; non-portable vendor fields (`defer_loading`, etc.) drop with `provider_extension_not_preserved`.

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
