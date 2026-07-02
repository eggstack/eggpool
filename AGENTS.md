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
- **Phase 3 loss-policy enforcement**: when `loss_policy = "reject"` is set on `[transcoder]`, the body transcoder raises `eggpool.transcoder.errors.TranscodeLossError` (rendered as HTTP 400 with `invalid_request_error`) before upstream dispatch if any of the five protected cache-control loss kinds is recorded (`cache_control_unsupported_by_target_protocol`, `cache_control_feature_disabled`, `cache_control_invalid_shape`, `provider_extension_not_preserved`, `stable_prefix_reordered_canonically`). The `warn` default preserves the v1 behaviour. The preflight in `proxy_request.py` also enforces `loss_policy = "reject"` more broadly (any loss warning) and runs in `warn` mode internally so it can collect the full warning list.
- **Phase 4 observe-mode compression accounting is observational**: `eggpool.transcoder.compression.analyze_compression` runs the compression analyzer over the Phase 2 segmentation and produces a per-request `CompressionObservation` (candidate counts, savings estimates, latency, reason codes). The analyzer is content-private — production never passes `text_hints`; only test fixtures do, so regex/JSON detection paths are exercised without exposing raw prompts. The analyzer never mutates the request body, never changes routing, never synthesises provider cache controls, and never raises on malformed input. The `QuotaFairScorer` still does NOT consume compression fields; compression accounting is reporting-only and lives alongside Phase 1–3 observability. Migration `0042` adds 12 columns (`compression_status`, `compression_mode`, candidate / token / latency / warning counts, `compression_reason_code_counts_json`, `compression_summary_json`) and 2 indexes to `requests`; `EXPECTED_SCHEMA_VERSION` in `scripts/check_database.py` is 42. See `plans/cache_compression_phase_04_observe_mode_compression_accounting.md`. The dashboard/API roll-up is exposed at `/api/stats/compression-observability` via `stats.service.get_compression_observability`.
- **Phase 5 safe suffix compression is the first request-mutating layer**: `eggpool.transcoder.compression.apply.apply_safe_compression` runs after the Phase 4 analyzer and before model rewrite, mutating only eligible `volatile_suffix` segments (six transforms: `fold_repeated_lines`, `compact_logs`, `compact_search_results`, `elide_base64_blobs`, `minify_machine_json`, `compact_stack_traces`). Deterministic markers `[EggPool compression: <transform> | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]` are unified across all six transforms via `eggpool.transcoder.compression.markers.build_marker` and allow the original content to be reproduced from the digest. `content_path` in segmentation is a concrete JSON path that resolves to actual string leaves of the request payload (NOT semantic role labels). Stable-prefix content hash is computed via `eggpool.transcoder.segmentation.stable_prefix_content_hash`, which extracts canonical stable-prefix content from the payload via stable-prefix segment paths and hashes it (EXACT content hash, not just structural descriptor). The fail-closed verification re-hashes the TRANSFORMED payload's stable-prefix content and triggers `failed_fallback=True` if it differs from the original. Context-limit checks happen before compression (compression cannot rescue over-limit requests). Compression/cache metrics do NOT affect same-provider account routing. Migration `0043` adds 13 columns + 2 indexes to `requests`; `EXPECTED_SCHEMA_VERSION` in `scripts/check_database.py` is 43. See `plans/cache_compression_phase_05_safe_suffix_compression.md`. The `QuotaFairScorer` still does NOT consume compression fields; Phase 5 stays load-based (request count + token count + active count + health).
- **Phase 5 corrective pass** completed 2026-07-02: Production paths now resolve to real JSON leaves; stable-prefix content hashing is exact; fail-closed actually verifies the mutated payload; markers are unified via `markers.build_marker`. See `plans/cache_compression_phase_05_corrective_pass.md`.
- **Phase 5 Anthropic tool_result closure** completed 2026-07-02: `_segment_anthropic_message_block` is now multi-segment (returns `list[RequestSegment]`); string `tool_result` content emits `("messages", i, "content", j, "content")`; nested-list `tool_result` content emits one segment per text leaf (`("messages", i, "content", j, "content", k, "text")`); non-text nested blocks emit no segment; missing/unrecognised content emits a non-compressible semi-stable segment at the block level for observability. Documented as concrete JSON paths in `architecture/README.md` § Path Semantics. Asserted by `tests/unit/test_compression_apply_production.py::test_anthropic_tool_result_string` and `::test_anthropic_tool_result_nested_list` with hard `assert result.applied is True`. Two `stable_prefix_content_hash` invariance regressions in `tests/unit/test_stable_prefix_content_hash.py` confirm Anthropic tool_result content changes do not break the stable-prefix cache key. Observe-mode analyzer in Phase 4 is unchanged because it consumes segment metadata only. See `plans/cache_compression_phase_05_anthropic_tool_result_closure.md`.
- **Phase 6 compression policy controls**: `[[compression.policies]]` overrides the global `[compression]` config per-request via `resolve_compression_policy` in `src/eggpool/transcoder/compression/policy_resolver.py`. The resolver matches `client_id` (from `x-eggpool-client`), `client_name` (from `User-Agent`), `source_protocol`, `requested_model`, and `transcoded`; provider-specific matchers (`match_provider_ids`, `match_provider_kinds`, `match_models`) are reserved for post-route resolution (silently skipped pre-route). Match is union OR across fields; merged in file order with last-match-wins for scalars and field-by-field merge for `transforms`. The reserved name `"default"` produces a catch-all override (no match fields = fires on every request). Fail-closed: malformed overrides skip and emit a structured warning; resolution never raises. The static-prefix safety rail still applies: `compress_static_prefix = true` in a non-default override is rejected unless global `allow_static_prefix_override = true`. Migration `0044` adds `compression_policy_name`, `compression_policy_source`, `compression_policy_warnings_json` columns + an index to `requests`; `EXPECTED_SCHEMA_VERSION` in `scripts/check_database.py` is 44. Stats roll-up at `/api/stats/compression-observability` includes `by_policy`, `by_policy_source`, `policy_warning_count_total`. The `QuotaFairScorer` does NOT consume policy fields; Phase 6 stays load-based. See `plans/cache_compression_phase_06_policy_controls.md`.
- **Phase 7 dashboard/runtime views & operator diagnostics**: Phases 1–6 produce data; Phase 7 makes it operationally usable. Six JSON API endpoints expose the per-phase roll-ups under `/api/stats/`: `cache-observability` (Phase 1), `canonical-request-segmentation` (Phase 2), `cache-stability` (Phase 3, narrow summary — full per-boundary detail lives on the in-memory `CacheBoundaryTracker`), `compression-observability` (Phase 4 + Phase 6 `by_policy`), `compression-runtime` (Phase 5 outcomes: applied, fallback, latency, per-transform tokens, warnings, cache_safety stable-prefix preserved/mismatch), and `compression-policies` (Phase 6 per-policy table; `<global>` sentinel first). The runtime page (`render_runtime` in `src/eggpool/dashboard/render.py`) renders four new cards: compression (observe/apply/fallback/candidates/savings), compression_runtime (mode strip + latency + transforms + warnings), compression_policy (per-policy table with `<global>` sentinel), and cache_stability (transcoded count + Phase 3 in-memory note). A static **routing-separation notice** card always renders on the runtime page: cache/compression metrics are reporting-only and not consumed by the `QuotaFairScorer`. No raw prompts, tool outputs, system messages, request bodies, or auth headers appear in any dashboard card or JSON response. The `QuotaFairScorer` does NOT consume any Phase 7 fields. No new migrations (Phase 7 is view-only). See `plans/cache_compression_phase_07_dashboard_runtime_views.md`.
- **Phase 8 routing guardrails & non-interference guarantees**: Codifies the routing invariant that cache/compression metrics NEVER enter account scoring, health removal, or route reselection. Three pins: (1) **hardcoded runtime diagnostic** — `RuntimeMetricsService._snapshot_routing_runtime` returns a constant `guardrails` dict (`routing_cache_compression_mode: "reporting_only"`, `routing_uses_cache_metrics: false`, `routing_uses_compression_metrics: false`, `routing_uses_stable_prefix_hash: false`, `routing_uses_compression_policy: false`, plus the allowed scorer input list); exposed via `GET /api/stats/runtime` and rendered as a **Routing guardrails (Phase 8)** card on the runtime page next to the routing-separation notice. (2) **Static + behavioural test pin** — `tests/unit/test_routing_guardrails.py` (19 tests) asserts the `QuotaFairScorer.score_accounts` signature accepts only the four canonical inputs (`account_names`, `model_name`, `active_requests`, `request_estimates`), that `RoutingScore` carries no cache/compression field, that identical load with adversarial cache/compression metrics produces identical scores, that two same-provider accounts with skewed cache hits / compression savings / stable-prefix hashes still get fair rotation, that compression fallbacks (`apply_safe_compression`'s `failed_fallback=True`) never affect provider health, that policy resolution (`resolve_compression_policy`) is observational-only and never reroutes. (3) **Documentation invariant** — every phase doc states the scorer does NOT consume the phase's columns. Same-provider account fairness (e.g., multiple OpenAI subscriptions) is preserved because cache hit ratios or compression savings never enter the score. A future cache-aware routing mode would require an explicit `routing.cache_aware = true` config flag plus per-provider support detection, a cost model using cached-token prices, backtesting, per-client opt-in, and dashboard warnings; Phase 8 deliberately does NOT implement it. No new migrations. See `plans/cache_compression_phase_08_routing_guardrails.md` and `architecture/README.md` § Routing Guardrails and Non-Interference (Phase 8).
- **Phase 9 synthetic cache controls are opt-in and observational**: `eggpool.transcoder.cache_synthesis` produces metadata (and, when enabled and not in dry-run, mutates the provider-bound body with synthetic Anthropic-style `cache_control` annotations around protected `stable_prefix` segments). The selector is disabled by default and dry-run by default. Only protected `stable_prefix` segments are eligible; volatile suffix and compressed content are never annotated. Native `cache_control` is never duplicated. The `QuotaFairScorer` does NOT consume synthetic cache fields; routing stays load-based (request count + token count + active count + health). Migration `0045` adds 9 columns + 2 indexes to `requests`; `EXPECTED_SCHEMA_VERSION` in `scripts/check_database.py` is 45. Stats roll-up at `/api/stats/synthetic-cache-observability`. Per-policy overrides ride on Phase 6 `[[compression.policies]]` via the `synthetic_cache_*` overlay fields.

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
