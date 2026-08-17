# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — architecture index and quick reference; see `architecture/README.md` for full design details
- `deployment` — production deployment, systemd, operational scripts, configuration changes
- `development` — linting, testing, pre-commit checks, code style

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
- CI installs with `uv sync --frozen --extra ci` (locks match `uv.lock` exactly)
- Entry point: `src/eggpool/cli.py` → `eggpool` console script
- Config: `config.toml` + `.env` for API keys
- Optional `orjson` backend: `uv pip install 'eggpool[fast]'` (or `uv sync --extra fast`)
- **Do not** add transitive imports to `fastcli.py` or `runtime_paths.py` — they are stdlib-only for the Raspberry Pi watchdog contract

## Local Development Loop

Fast focused iteration:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest <affected test paths> -q --tb=short --maxfail=1
```

## Before-Push Check

Run the same checks as the CI job:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## CI

One GitHub Actions job on every PR:

| Job | Python | What it does |
|-----|--------|-------------|
| `check` | 3.11 | ruff format + ruff check + pyright + `pytest tests/smoke/` |

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

## Focused Verification

```bash
# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Integration tests only
uv run pytest -m integration -v

# Network-dependent tests
uv run pytest -m network -v

# Lint auto-fix
uv run ruff check --fix src/
```

## Code Style

- Python 3.11+ with `from __future__ import annotations` in ALL files
- Type hints on all function signatures and return types
- Ruff: E, F, W, I, N, UP, B, A, SIM, TCH rules
- Pyright strict mode — covers `src/` AND `scripts/` (not tests)
- Line length: 88 chars
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- pytest with `asyncio_mode = "strict"` and `xfail_strict = true` (from `pyproject.toml`)
- `asyncio_default_fixture_loop_scope = "function"` is set; do not override without understanding the implications
- `--strict-markers` enforced
- respx for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/smoke/`, `tests/perf/`, `tests/live/`, `tests/contract/`
- Smoke suite (`tests/smoke/`): package import, config parsing, invalid config rejection, check-config validation, DB migration, one non-stream request, one streaming request, one upstream failure followed by recovery, one premature EOF, one Anthropic request, and CLI help
- Provider contract tests: `uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v`
- Transcoder/proxy contract tests: `uv run pytest tests/contract/ -v`
- Multimodal transcoder tests: `uv run pytest tests/unit/test_transcoder/test_multimodal.py -v`
- Performance, live, and diagnostic reproducer tests are manually invoked, not run in CI
- Database fixtures must disconnect on every teardown path; use `try/finally` on the canonical event loop

## Release

Manual release procedure — no automated release workflow. See `docs/releasing.md`.

## File Organization

- Source: `src/eggpool/`
- Tests: `tests/` (mirrors src structure)
- Config: `config.example.toml`, `.env.example`
- DB schema: `src/eggpool/db/schema/`
- Scripts: `scripts/` (operational, also type-checked by pyright)
- Deployment: `deploy/`
- Shared assets: `src/eggpool/_share/` (bundled config examples for pipx installs)
- Architecture docs: `architecture/` (deep-dive per subsystem)
- Plans: `plans/` (active work and historical reference; not a required reading list)

## Architecture Index

> Full design details are in `architecture/README.md` and the `architecture` skill.

**Public protocol scope:** EggPool exposes OpenAI Chat Completions at
`/v1/chat/completions`, Anthropic Messages at `/v1/messages`, and OpenAI-style
model listing at `/v1/models`. It does not claim full OpenAI API or Responses
API parity.

Start subsystem work with the current architecture index and the relevant deep
dive. Read an active roadmap or focused plan only when the change is in its
scope.

- **Request lifecycle**: `RequestCoordinator` orchestrates endpoint → routing → persistence → dispatch → finalization. Deep dive: `architecture/deep-dive-request-lifecycle.md`
- **Dispatch isolation**: local preparation and response adaptation failures are terminal local errors with no provider retry. Only typed HTTPX transport failures may retry, only across distinct accounts before `downstream_started`
- **Multi-provider architecture**: provider-suffixed model IDs (`model-id/provider-id`), `ProviderClientPool`, `OutboundClientManager`. Deep dive: `architecture/deep-dive-providers.md`
- **Provider contracts**: `compose_provider_url()` is the single source of truth for upstream URLs
- **Protocol transcoding**: `src/eggpool/transcoder/` — OpenAI ↔ Anthropic conversion. Deep dive: `architecture/deep-dive-transcoder.md`. Operator guide: `docs/transcoding.md`
- **Content IR**: `src/eggpool/transcoder/content.py` — narrow content-block representation for cross-protocol translation. `MultimodalCapabilities` in `catalog/capabilities.py` provides granular media support per provider/model/protocol
- **Local provider templates**: `_templates.toml` includes presets for Ollama, LM Studio, llama.cpp, vLLM, LocalAI, and a generic custom-compatible endpoint. Local providers use discovery-based verification (no fixed probe models)
- **JSON backend (`eggpool.jsonx`)**: preferred `orjson` (`eggpool[fast]`), falls back to stdlib. Override with `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`. Off the request path, stdlib `json` allowed for deterministic hashing
- **Database invariants**: SQLite WAL, single-connection serialization, and task-owned `async with db.transaction():` boundaries for all DML; rollback is private to the owning transaction path and ambiguous outcomes fail closed. Deep dive: `architecture/deep-dive-database.md`
- **Request schema freeze**: the historical `requests` table is frozen for optional diagnostics. New columns require durable lifecycle/accounting or externally visible compatibility justification; feature-specific diagnostics use sparse/event or narrowly scoped sidecar storage. Do not add cosmetic migrations or a generic EAV store
- **Quota and routing**: tier-based via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor. Load-based (request count + token count + active count + health), never cost-based. Positive account `weight` scales effective request/token capacity within an eligible tier (`1.0` baseline, `2.0` approximately double, `0.5` approximately half). Deep dive: `architecture/deep-dive-routing.md`
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. See `errors.py`
- **Process model**: supervisor + Granian worker (`workers=1`), daemon mode (`--verbose` for foreground). `runtime_threads=1` is required. Readiness probe is process-owned and disabled by default. Deep dive: `architecture/deep-dive-deployment.md`
- **Lean defaults**: loopback binding, low-wear analytics, provider pools of 16/4, background outbound pools of 8/2. Model-info, routing traces, readiness writes, automatic backups, dispatch writing are opt-in. The copyable SBC profile also leaves full-database backups off by default
- **Runtime generations**: `RuntimeManager` owns active/retiring generation slots. `RuntimeGenerationFactory.prepare()` is the shared startup/rehash construction boundary. `RequestFinalizationSupervisor` is generation-owned. Deep dive: `architecture/deep-dive-runtime.md`
- **Live rehash**: `eggpool rehash` applies changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`. Operator guide: `docs/live-config-rehash.md`
- **Health management**: `src/eggpool/health/` — circuit breaker, per-account tracking, bounded 1,800s backoff, scoped model quarantine, `DatabaseWritableProbe` for `/readyz`. Deep dive: `architecture/deep-dive-health.md`
- **Background tasks**: `src/eggpool/background/` — `TaskSupervisor`, fixed-delay scheduler. Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation. Deep dive: `architecture/deep-dive-background.md`
- **Database teardown**: generation-owned request/finalization/background tasks are joined before process- or fixture-owned databases disconnect; database fixtures must use `try/finally` cleanup on the canonical event loop
- **Database recovery**: startup integrity is fail-closed. Indeterminate outcomes exit the worker; systemd restarts, then startup integrity and crash reconciliation run before readiness. Runbook: `docs/runbooks/database-recovery.md`

## Gotchas

- **`fastcli` and `runtime_paths` are stdlib-only**: no transitive imports. Raspberry Pi watchdog contract
- **`eggpool rehash` serializes reload transactions**: only one reload in progress at a time. Concurrent rejections with `reload_in_progress`
- **`ReloadObserver` is inert in production**: the observer protocol has no-op defaults
- **`eggpool connect`/`logout` don't silently restart**: if the server is healthy but control socket is missing, they return `(False, "control unavailable (server healthy)")`
- **`eggpool update` must make a live PyPI lookup**: bare updates use `is_newer_version()` and retain the freshness-aware latest path; an explicit `VERSION` uses the exact PyPI release endpoint, permits deliberate downgrades, and verifies the installed version before restart
- **No pre-commit hooks configured**: CI runs ruff, pyright, and pytest via GitHub Actions
- **`static_models` is source of truth for provider-specific protocol**: providers serving non-default protocol must ship `[[providers.<id>.static_models]]` rows
- **`CapabilityError` (400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)**
- **DB migrations are numbered SQL files** in `src/eggpool/db/schema/`
- **`reload_in_progress` exits with code 4** (`EXIT_RELOAD_BUSY`). Use the constant from `cli_exit_codes.py`
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound
- **`/readyz` never performs a write**: reads a cached probe snapshot
- **Process transitions execute inside `db.transaction()`**: atomic rollback on any failure
- **When constructing `RequestCoordinator` in tests**: pass an explicit `transcoder_policy` or assert the desired default
- **`app.state` generation-owned attributes are mirrors, not authority**: new code should use `get_active_generation(request)` or acquire a lease

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `ConfigError` → `ConfigValidationError` (with subclasses: `ConfigFileAccessError`, `ConfigParseError`, `ConfigSchemaError`, `ConfigStartupAuthError`, `ConfigAccountCredentialError`, `ConfigInternalError`)
- `DatabaseError` → `DatabaseCommitError`, `DatabaseConnectionInvalidatedError`, `DatabaseRollbackError`, `DatabaseTransactionOwnershipError`, `ModelQuarantineHydrationError`, `ModelQuarantineRecoveryError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ProxyError` → `PrematureStreamEOFError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ModelInfoSourceFetchError`, `ContextLimitExceededError`, `CapabilityError`
- `CapabilityError` → `BudgetResolutionError` (thinking budget rejection)
- `AcceptedFinalizationInvariantError` (reload invariant violation)
- `RuntimeManagerLeaseExhaustedError` (RuntimeError) — mapped to HTTP 503 in `proxy_request.py`
- `TranscodeLossError` (from `transcoder.errors`) — HTTP 400 when `loss_policy = "reject"`
- `ProtocolMismatchError` (from `catalog.protocols`) — endpoint/model-protocol mismatch
- `ConfigValidationError(ConfigError)` and its subclasses are raised by `eggpool.config_validation.validate_config_file()`. They chain from the underlying failure and never raise `SystemExit`

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (~73 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files

## Planning Policy

Planning is proportional to risk. Use the development skill's "Planning proportionality" section for guidance.
