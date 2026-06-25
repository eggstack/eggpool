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

## Multi-Provider Architecture

- Provider-suffixed model IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`)
- `ProviderClientPool` manages per-provider `httpx.AsyncClient` with independent connection pools
- Flat `[[accounts]]` configs auto-normalize to a default `opencode-go` provider
- `parse_model_provider()` and `format_model_provider()` in `routing/provider.py`

## Provider Contracts

Key design rules that are easy to get wrong:

- `compose_provider_url()` is the single source of truth for upstream URLs — catalog fetch, non-streaming chat, and streaming chat all call it
- `build_auth_headers()` reads from `ProviderConfig.auth` — never hardcode Bearer
- `base_url` ending `/v1` + path beginning `/v1/` is rejected (duplicate version prefix)
- `auth.mode = "none"` sends no upstream auth (Ollama local)
- All outbound dispatch paths use `compose_provider_url()` so providers cannot list at one host and dispatch to another
- **`models_endpoint.method = "DISABLED"`** skips live model listing. Providers using this must declare `[[providers.<id>.static_models]]` rows; otherwise `eggpool check-config` warns and the catalog will be empty for the provider
- **`static_models` lifecycle**: rows are seeded by `CatalogService._seed_static_models` BEFORE live fetch tasks run. Static-source fields (`protocol`, `protocol_source == "static_config"`, `supports_tools`, `supports_vision`) are preserved by `ModelCatalogCache._preserve_static_fields` when live rows arrive without them
- **`eggpool check-config`** runs `_check_stale_contracts` after a successful load and emits advisory warnings for: `DISABLED` without static seeds, `DISABLED` with `require_models = true`, declared path fields ignored by protocol mismatch, duplicate `/v1` segments, `Authorization` in static headers when `auth.mode != "none"`, legacy `models_method`/`models_path` fields, and Anthropic providers using `auth.header = "Authorization"` instead of `x-api-key`. Warnings do not change the exit code

## Bearer-prefix Guard

`AppConfig.validate_account_credentials()` rejects API keys beginning with `Bearer` for `auth.mode = "bearer"` providers. EggPool prepends `Bearer ` automatically; storing `Bearer <token>` produces `Authorization: Bearer Bearer <token>` and causes 401s. Providers using `auth.mode = "raw_authorization"` are unaffected.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ContextLimitExceededError`

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No CI workflows or pre-commit hooks are configured in this repo
- `Database.vacuum()` is the only sanctioned path for `VACUUM` in production code
- Every DML write must run inside `async with db.transaction():`
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- Requests must be persisted before upstream dispatch; pre-body failures can retry, but no retry after first downstream byte
- **Upstream-authoritative suppression**: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Above-capacity accounts stay eligible; only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing. Switch to `hard_cap` only as an opt-in escape hatch.
- **Backoff persistence**: upstream-derived backoffs survive restarts via the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`). Hydration runs at startup after account sync, best-effort (never blocks boot). Local cost overruns must never be persisted as backoff rows.
- **Synthetic 503 vs 502**: `ModelUnavailableError` (503) is reserved for genuine pre-dispatch unavailability. `UpstreamExhaustedError` (502) is raised when every candidate account was attempted and exhausted mid-request. Single-account upstream errors pass through to the client rather than becoming synthetic 503s.
- **Streaming finalizer shielding**: streaming `_build_stream_generator` finalization runs under `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task cancellation cannot kill the finalizer while it holds the DB lock. Leaks that escape this path are caught by the periodic `stale_request_finalizer` background task (`app._finalize_stale_requests`, runs every 60s) which force-finalizes any request that has been `pending` longer than `upstream.read_timeout_s`.
- **Startup crash recovery**: `_crash_recovery` runs at every startup and recovers ALL pending requests and ALL active reservations with no time threshold. A process restart is a definitive boundary, so leaked state from the previous process is unconditionally cleaned up. If `Crash recovery: marked N stale requests` appears in logs, the safety net caught leaks from the previous run.
- **Fast-path CLI imports**: fast-path commands (`croncheck`, `ensure-running`) import only `eggpool.runtime_paths` from the package; do not add transitive imports to `runtime_paths` or `fastcli` or you break the Raspberry Pi watchdog performance contract

## Process Model

- `eggpool serve` runs as a single supervisor process that invokes `Granian` with `workers=1`; Granian spawns one worker process, so exactly **two** processes appear under the canonical name
- The Granian worker is launched with `process_name="eggpool"`, so `ps` / `top` / `pgrep` show the canonical name for both supervisor and worker (not a generic `python` entry)
- `[server].threads` (int, default `1`, min `1`, max `64`) controls Granian `runtime_threads` — the number of worker event-loop threads. Default is `1` for SBC / Raspberry Pi; raise on capable hardware
- PID path resolution lives in `eggpool.runtime_paths` and is the single source of truth (`default_pid_file()`). Precedence: `$EGGPOOL_PID_FILE` → `$XDG_RUNTIME_DIR/eggpool.pid` → `~/.local/state/eggpool/eggpool.pid` → `/tmp/eggpool-<UID>.pid`. The PID file is owned by the **supervisor**, written before `Granian.serve()` and cleared in a `finally` block. The FastAPI lifespan no longer touches the PID file
- `eggpool serve` refuses to start a second instance: first checks `runtime.read_pid()` + `runtime.is_process_running()`; if no live PID, probes `GET /v1/healthz` via stdlib `urllib.request` (bind `0.0.0.0` / `::` is rewritten to `127.0.0.1`). A live PID or a 200 from the probe exits `1`. Stale PID files (PID not running) are cleared before starting
- `eggpool restart` no longer has inline subprocess logic; it delegates to `runtime.restart_server` which calls `runtime.send_sigterm` and `runtime.start_server` (which `subprocess.Popen`s a new supervisor)
- `eggpool ensure-running` is the canonical cron watchdog command — it atomically checks-and-starts without ever spawning a duplicate instance. Use it from `@reboot` and `*/5 * * * *` crontab lines, not `croncheck || eggpool serve &`

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool help` | Show help message and available commands |
| `eggpool version` | Print the installed version |
| `eggpool serve` | Start the aggregation proxy server (default command) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool onboard` | Run the interactive onboarding setup (connect providers, start server) |
| `eggpool connect` | Connect to a new provider interactively |
| `eggpool connect list` | List available providers |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Restart to apply config changes |
| `eggpool croncheck` | Lightweight check: exit 0 if server is running, exit 1 if not |
| `eggpool ensure-running` | Repair: start the server if it is not running; no-op when alive. Fast-path. |
| `eggpool models refresh` | Refresh model catalog from upstream |
| `eggpool configsetup opencode` | Print OpenCode provider config JSON with model limits |
| `eggpool db vacuum` | Reclaim SQLite space |
| `eggpool backup` | Create a timestamped `.zip` backup of config, `.env`, and database |
| `eggpool recover [path]` | Restore from a backup archive (interactive menu if no path) |
| `eggpool uninstall` | Remove binary, config, database, and shell PATH entries |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).
Running `eggpool` with no arguments prints the help message.

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (74 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__` — so `from eggpool.cli import cli` and existing test imports still work without loading the full graph
- See `plans/lightweight-cli-watchdog.md` for the full design

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
