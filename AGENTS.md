# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — design principles, request lifecycle, invariants, error hierarchy
- `deployment` — production deployment, systemd, operational scripts
- `development` — linting, testing, pre-commit checks, code style

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
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
- **Quota and routing**: tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression. See `architecture/README.md` § Quota and Routing.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. See `architecture/README.md` § Error Hierarchy.
- **Process model**: supervisor + Granian worker, PID file lifecycle, daemon mode. See `architecture/README.md` § Daemon Mode.
- **Dashboard**: server-rendered HTML, 12 pages, Chart.js v4, grouped timeseries, CSS tooltips. See `architecture` skill § Dashboard.
- **Observability**: attempt analytics, routing analytics, latency phases, pending health, runtime metrics. See `architecture` skill § Runtime Observability.

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No pre-commit hooks are configured in this repo; CI runs ruff, pyright, and pytest via GitHub Actions
- **`static_models` is the source of truth for provider-specific protocol** — `FAMILY_PROTOCOLS` (`src/eggpool/catalog/protocols.py`) is a global fallback that applies when no explicit override exists. Providers like `minimax-cn` that serve MiniMax models on the OpenAI-compatible surface **must** ship `[[providers.<id>.static_models]]` rows with `protocol = "openai"`, otherwise the live `/v1/models` fetch resolves `MiniMax-M*` via the `minimax-` family prefix to `anthropic` and the provider protocol constraint check (`src/eggpool/catalog/service.py:533-545`) clears the protocol to `None`, producing `ModelUnavailableError("Model 'MiniMax-M3' has unresolved protocol")` instead of the more obvious 400 `ProtocolMismatchError`. Static seeds with `protocol_source = "static_config"` survive the live merge via `ModelCatalogCache._preserve_static_fields` (`src/eggpool/catalog/cache.py:146-187`). The same model ID may also be flipped on a per-provider basis via `[providers.<id>.model_overrides."<model-id>"].protocol = "openai"` for operator overrides that go beyond the bundled template.
- **Upstream-authoritative suppression**: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Above-capacity accounts stay eligible; only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing. Switch to `hard_cap` only as an opt-in escape hatch.
- **Backoff persistence**: upstream-derived backoffs survive restarts via the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`). Hydration runs at startup after account sync, best-effort (never blocks boot). Local cost overruns must never be persisted as backoff rows.
- **Synthetic 503 vs 502**: `ModelUnavailableError` (503) is reserved for genuine pre-dispatch unavailability. `UpstreamExhaustedError` (502) is raised when every candidate account was attempted and exhausted mid-request. Single-account upstream errors pass through to the client rather than becoming synthetic 503s.
- **Streaming finalizer shielding**: streaming `_build_stream_generator` finalization runs under `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task cancellation cannot kill the finalizer while it holds the DB lock. Leaks that escape this path are caught by the periodic `stale_request_finalizer` background task (`app._finalize_stale_requests`, runs every 60s) which force-finalizes any request that has been `pending` longer than `upstream.read_timeout_s`.
- **Startup crash recovery**: `_crash_recovery` runs at every startup and recovers ALL pending requests and ALL active reservations with no time threshold. A process restart is a definitive boundary, so leaked state from the previous process is unconditionally cleaned up. If `Crash recovery: marked N stale requests` appears in logs, the safety net caught leaks from the previous run.
- **Pricing pipeline**: prices flow TOML override → upstream metadata → external catalog (OpenRouter / OpenCode Zen via the alias registry). Cost precedence: `provider_reported > derived/partial/exact > estimated (reservation fallback) > unknown (zero)`. See the `architecture` skill for the full pricing resolution details.
- **`eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`**: walks the requests table in started_at DESC order, recomputes cost from the current price snapshots, and reports / applies the change. Default is `--dry-run`. Use after upgrading the resolver to fix inflated totals on cached-token-heavy models (e.g. MiMo 2.5). Implemented in `src/eggpool/cost_recompute.py` and reuses the live `CostCalculator` so the new values match what the finalizer would write today.
- **Automatic backups**: in-process daily backups run by default under the `automatic_backup` supervised task (`src/eggpool/background/backup.py`). Uses stdlib `sqlite3.Connection.backup()` for consistent snapshots, atomic archive publication (write-to-temp + rename), and count-based retention (default 14). Controlled by `[backup]` config section. The `eggpool deploy backup-cron` path remains available for operators who prefer external scheduling.
- **DNS cache**: `OutboundClientManager` and `ProviderClientPool` both integrate a `DnsNetworkBackend` that caches resolved DNS entries in memory. The cache reduces connection latency for repeated requests to the same upstream hosts. Controlled by `[network.dns_cache]` config. When a proxy is configured for an account, that account's client uses the proxy transport instead of the cached backend.
- **Transcoder body translation**: `select_transcoder()` in `src/eggpool/transcoder/protocol.py` is the single source of truth for translator dispatch. When `client_protocol != upstream_protocol`, the coordinator pre-translates the request body before dispatch, decodes the response body after success, and re-renders non-retryable errors in the client protocol. Loss-of-information warnings are accumulated on `TranscodeContext.loss_warnings` and logged at request completion.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ContextLimitExceededError`

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (74 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__` — so `from eggpool.cli import cli` and existing test imports still work without loading the full graph
- See `plans/lightweight-cli-watchdog.md` for the full design

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
