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

## Architecture Index

> Full design details are in `architecture/README.md` and the `architecture` skill.

- **Request lifecycle**: `RequestCoordinator` orchestrates endpoint → routing → persistence → dispatch → finalization. See `architecture/README.md` § Request Lifecycle.
- **Multi-provider architecture**: provider-suffixed model IDs (`model-id/provider-id`), `ProviderClientPool`, `OutboundClientManager`. See `architecture/README.md` § Multi-Provider Architecture.
- **Provider contracts**: `compose_provider_url()` is the single source of truth for upstream URLs. See `architecture/README.md` § Provider Contracts and § Provider Contract Rendering.
- **Protocol transcoding**: transparent request/response format conversion between OpenAI and Anthropic protocols. Phase 2 body translation, Phase 3 streaming SSE translation, Phase 4 routing eligibility widening, and Phase 5 operator controls and docs are implemented in `src/eggpool/transcoder/` and `src/eggpool/request/coordinator.py`. See `architecture/README.md` § Protocol Transcoding.
- **Database invariants**: SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML. See `architecture/README.md` § Database.
- **Quota and routing**: tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor. See `architecture/README.md` § Quota and Routing.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. See `architecture/README.md` § Error Hierarchy.
- **Process model**: supervisor + Granian worker, PID file lifecycle, daemon mode. See `architecture/README.md` § Daemon Mode.
- **Dashboard**: server-rendered HTML, 13 pages (including `/models/{model_id:path}` model-info detail), Chart.js v4, grouped timeseries, CSS tooltips. See `architecture` skill § Dashboard.
- **Observability**: attempt analytics, routing analytics, latency phases, pending health, runtime metrics. See `architecture` skill § Runtime Observability.
- **Agent integrations**: `eggpool configsetup` generates configuration snippets for 11 coding agents (OpenCode, Claude Code, Aider, Codex, Qwen Code, Kilo, Continue, Cline, Roo Code, Goose, OpenHands). Target-specific generators live in `src/eggpool/integrations/`; shared utilities in `src/eggpool/config_utils.py` (`resolve_server_api_key`, `ServerKeyResolution`). Transcoder enablement is persisted to TOML when required. Secret output is controlled explicitly via `contains_secret`. See `architecture/README.md` § Package Structure.
- **Model information**: `model_info/` sidecar subsystem with persistent metadata, provider-native observations (`ProviderCatalogSource`), OpenRouter metadata source (`OpenRouterModelInfoSource` — TTL-cached, exact alias matching, uses shared outbound client), identity resolution (`model_info/identity.py` — exact/curated alias matching only, no fuzzy matching), status classification, lifecycle wiring (`ModelInfoService`), background refresh scheduler (`ModelInfoRefreshScheduler`), catalog-refresh-driven reconciliation (`CatalogRefreshResult`), and CLI inspection. `ModelInfoSourceFetchError` for source failures. Phase 4 adds JSON API endpoints (`GET /api/model-info`, `GET /api/model-info/{model_id}`, `GET /api/model-info/sources`, `POST /api/model-info/refresh`), compact `/v1/models` enrichment under `eggpool.model_info`, and dashboard model-info status pills with tooltips. Phase 5 adds Artificial Analysis and Hugging Face source adapters, manual overrides (`[model_info.overrides]`), alias expansion (`[model_info.aliases]`), source health hardening (`rate_limited_until`, `last_status_code`, `last_payload_count`, `last_success_duration_ms`), richer summary generation (benchmarks, HF metadata, conflicts), and `model_info_overrides` table (migration `0037`). See `architecture/README.md` § Model Information.

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No pre-commit hooks are configured in this repo; CI runs ruff, pyright, and pytest via GitHub Actions
- **`static_models` is the source of truth for provider-specific protocol** — `FAMILY_PROTOCOLS` (`src/eggpool/catalog/protocols.py`) is a global fallback. Providers like `minimax-cn` that serve MiniMax models on the OpenAI-compatible surface **must** ship `[[providers.<id>.static_models]]` rows with `protocol = "openai"`, otherwise the live `/v1/models` fetch resolves `MiniMax-M*` via the `minimax-` family prefix to `anthropic` and the protocol check clears it to `None`, producing `ModelUnavailableError` instead of `ProtocolMismatchError`. Static seeds survive via `ModelCatalogCache._preserve_static_fields` (`src/eggpool/catalog/cache.py:146-187`).
- **Upstream-authoritative suppression**: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing. Switch to `hard_cap` only as an opt-in escape hatch.
- **Same-tier fairness rotor**: EggPool is not purely lowest-score-wins for same-tier peer accounts. When accounts are effectively tied by priority, weight, health, protocol, and utilization score, same-tier fairness rotates candidates to avoid stable config-order bias and subscription starvation. When `fairness_mode = "round_robin"` (the default), effectively tied accounts within the same priority tier are rotated deterministically via `FairnessRotor` (`src/eggpool/routing/fairness.py`). Band membership requires same priority, same weight, same transcode status, and score within `fairness_epsilon` of the best. The server runtime honors all `[routing]` fairness config fields: `fairness_mode`, `fairness_epsilon`, and `fairness_scope`. The `fairness_scope` controls rotation group granularity: `provider_model_protocol` (default) includes the routed protocol in the key, `provider_model` intentionally collapses protocol groups, and `priority_model_protocol` intentionally co-balances same-priority providers serving the same model. The rotor's position map is capped at 4096 entries (`_ROTOR_HARD_CAP`); when reached the entire map is cleared and rotation restarts from 0. Restart also resets all rotor state — durable round-robin is explicitly out of scope.
- **Backoff persistence**: upstream-derived backoffs survive restarts via the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`). Local cost overruns must never be persisted as backoff rows.
- **Synthetic 503 vs 502**: `ModelUnavailableError` (503) is reserved for genuine pre-dispatch unavailability. `UpstreamExhaustedError` (502) is raised when every candidate account was attempted and exhausted mid-request.
- **Streaming finalizer shielding**: streaming `_build_stream_generator` finalization runs under `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task cancellation cannot kill the finalizer while it holds the DB lock. Leaks that escape this path are caught by the periodic `stale_request_finalizer` background task (`app._finalize_stale_requests`, runs every 60s).
- **Routing-decision score components** (`RoutingDecisionTrace.score_components`) carry the per-account score breakdown on every persisted `routing_decisions` row (`score_components_json`, migration `0035`). The dashboard and `eggpool accounts explain` consume this directly; do not re-score from quota tables when only the diagnostic breakdown is needed.
- **`_select_lock` publish ordering**: in `RequestCoordinator._select_and_persist_attempt()`, runtime publication (`Router.increment_active_request_count` + `QuotaEstimator.add_reservation`) must run INSIDE `_select_lock` AFTER the durable transaction commits but BEFORE the lock releases. The two contexts are explicit nested `async with` blocks (outer `_select_lock`, inner `_db.transaction()`). Key invariant: block placement (publication outside DB transaction body, inside `_select_lock`), not context-exit order.
- **`db.transaction()` nesting semantics** (`src/eggpool/db/connection.py`): nesting is detected via SQLite's per-connection `conn.in_transaction`, NOT task identity. A shielded or `create_task` child entering `db.transaction()` while the parent's `BEGIN IMMEDIATE` is still open piggybacks on the outer's commit boundary; the child never re-issues `BEGIN` and never acquires `_connection_lock` (the outer holds it). This fixes the AB/BA deadlock where a shielded child would block waiting for `_connection_lock` while the parent awaited `asyncio.shield()`. Two ContextVars back the semantics: `_in_transaction_context: ContextVar[bool]` is set/reset by both nested and outermost paths and is inherited by shielded/child tasks so they can `execute_write`; `_transaction_owner: ContextVar[Task]` is set only by the outermost path and is used by `vacuum()` to refuse running when the *current* task holds the lock (deadlock guard). `vacuum()` and `_require_transaction_owner()` consult `_in_transaction_context`, not `_transaction_owner`, so shielded/child task writes piggyback cleanly. Regression coverage lives in `tests/unit/test_database.py::TestTransactionNestingAcrossTaskBoundaries`.
- **`eggpool accounts explain` reads from SQLite, not an empty cache**: the command hydrates the catalog via `ModelCatalogCache.hydrate_from_db(db)` from `models`/`provider_model_metadata`/`account_models` rows. A thin `_CatalogShim` exposes the loaded cache as a `CatalogService`-compatible object. Output uses `click.echo` (no `rich` dependency).
- **Startup crash recovery**: `_crash_recovery` runs at every startup and recovers ALL pending requests and active reservations with no time threshold. A process restart is a definitive boundary.
- **Pricing pipeline**: prices flow TOML override → upstream metadata → external catalog (OpenRouter / OpenCode Zen via the alias registry). Cost precedence: `provider_reported > derived/partial/exact > estimated > unknown`.
- **`eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`**: recomputes cost from current price snapshots. Default `--dry-run`. Implemented in `src/eggpool/cost_recompute.py`.
- **Automatic backups**: in-process daily backups run by default under the `automatic_backup` supervised task (`src/eggpool/background/backup.py`). Controlled by `[backup]` config section.
- **DNS cache**: `OutboundClientManager` and `ProviderClientPool` both integrate a `DnsNetworkBackend` that caches resolved DNS entries. Controlled by `[network.dns_cache]` (enabled by default, TTL 1800s, max 50 entries). Exposes precise counters and derived rates for operator diagnostics.
- **Memory footprint caps**: every growth axis is bounded by hardcoded caps (`EWMA_HARD_CAP = 4096`, `GLOBAL_EWMA_HARD_CAP = 1024`, `MAX_TRACKED_HOSTS = 256`). Regression gate: `tests/integration/test_memory.py` (`pytest.mark.slow`). See `plans/memory.md` for the full design.
- **Transcoder body translation**: `select_transcoder()` in `src/eggpool/transcoder/protocol.py` is the single source of truth for translator dispatch. Loss-of-information warnings are accumulated on `TranscodeContext.loss_warnings` and logged at request completion.
- **Agent config generation**: `eggpool configsetup` dispatches to target-specific generators in `src/eggpool/integrations/`. Shared utilities (model ID formatting, base URL defaults, clipboard/write logic) live in `src/eggpool/config_utils.py`. When adding a new target, create a new module in `integrations/` and register it in the Click command; reuse the shared utilities rather than reimplementing them.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ModelInfoSourceFetchError`, `ContextLimitExceededError`

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (74 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__` — so `from eggpool.cli import cli` and existing test imports still work without loading the full graph
## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
