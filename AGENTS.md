# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — architecture index and quick reference; see `architecture/README.md` for full design details
- `deployment` — production deployment, systemd, operational scripts, configuration changes
- `development` — linting, testing, pre-commit checks, code style
- `documentation` — doc map, accuracy verification against code, and pruning rules for README/docs/architecture/AGENTS.md changes

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
- CI installs with `uv sync --frozen --extra ci` (locks match `uv.lock` exactly) — dependency changes must update `uv.lock`
- Entry point: `src/eggpool/cli.py` → `eggpool` console script
- Config resolution: `--config` flag > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`. API keys come from environment/`.env`
- Optional extras: `fast` (orjson JSON backend; stdlib fallback keeps SBC installs working), `proxy` (pproxy per-account outbound proxies)
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

One GitHub Actions job (`check`, Python 3.11): ruff format + ruff check + pyright + `pytest tests/smoke/`. CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

CI ignores paths-only changes to `docs/`, `architecture/`, `plans/`, `.opencode/skills/`, `CHANGELOG.md`, and `AGENTS.md` — docs-only PRs will show no CI run.

## Focused Verification

```bash
# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_window_expiry" -v

# Integration tests only
uv run pytest -m integration -v

# Network-dependent tests
uv run pytest -m network -v

# Lint auto-fix
uv run ruff check --fix src/
```

Markers registered in `pyproject.toml`: `unit`, `integration`, `network`, `live`, `performance`, `slow`.

## Code Style

- Python 3.11+ with `from __future__ import annotations` in ALL files
- Type hints on all function signatures and return types
- Ruff: E, F, W, I, N, UP, B, A, SIM, TCH rules; line length 88
- Pyright strict mode — covers `src/` AND `scripts/`, excludes `tests/` (ruff still covers `tests/`)
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- pytest with `asyncio_mode = "strict"`, `xfail_strict = true`, `--strict-markers`
- `asyncio_default_fixture_loop_scope = "function"` is set; do not override without understanding the implications
- respx for HTTPX upstream mocking; no real network in unit/integration suites
- Suites: `tests/unit/`, `tests/integration/`, `tests/smoke/`, `tests/contract/`, plus manual `tests/perf/` and `tests/live/` (not run in CI)
- Smoke suite (`tests/smoke/`) is the CI gate: package import, config parse/reject, check-config, DB migration, one non-stream + streaming request, upstream failure→recovery, premature EOF, Anthropic request, CLI help
- Provider contract tests: `uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v`
- Transcoder/proxy contract tests: `uv run pytest tests/contract/ -v`
- Database fixtures must disconnect on every teardown path; use `try/finally` on the canonical event loop

## Release

Manual release procedure — no automated release workflow. See `docs/releasing.md`.

## File Organization

- Source: `src/eggpool/`; Tests: `tests/`; Scripts: `scripts/` (type-checked by pyright); Deployment: `deploy/`
- Config references: `config.example.toml`, `config.sbc.example.toml`, `.env.example`
- DB migrations: numbered SQL files in `src/eggpool/db/schema/`
- Shared assets: `src/eggpool/_share/` (bundled config examples for pipx installs)
- Architecture docs: `architecture/` (deep dive per subsystem); Plans: `plans/` (historical record — consult only when the change falls within an active plan)

## Architecture

Start subsystem work at `architecture/README.md`, then the matching deep dive:

| Subsystem | Deep dive |
|-----------|-----------|
| CLI, config, errors, JSON backend | `architecture/deep-dive-core.md` |
| Request lifecycle, coordinator, proxy, finalization | `architecture/deep-dive-request-lifecycle.md` |
| Protocol transcoding (OpenAI ↔ Anthropic) | `architecture/deep-dive-transcoder.md` |
| Routing & quota | `architecture/deep-dive-routing.md` |
| Providers, contracts, outbound clients | `architecture/deep-dive-providers.md` |
| SQLite, migrations, repositories | `architecture/deep-dive-database.md` |
| Runtime generations & process management | `architecture/deep-dive-runtime.md` |
| Health, circuit breaker, quarantine | `architecture/deep-dive-health.md` |
| Background tasks & backups | `architecture/deep-dive-background.md` |
| Dashboard & stats API | `architecture/deep-dive-dashboard.md` |
| Model catalog & pricing | `architecture/deep-dive-catalog.md` |
| Model-info sidecar | `architecture/deep-dive-model-info.md` |
| Control plane (rehash) | `architecture/deep-dive-control.md` |
| Pydantic data models | `architecture/deep-dive-models.md` |
| Agent integrations (configsetup) | `architecture/deep-dive-integrations.md` |
| Security & redaction | `architecture/deep-dive-security.md` |
| Observability & routing traces | `architecture/deep-dive-observability.md` |
| Retry classification & backoff | `architecture/deep-dive-retry.md` |
| Metrics & telemetry | `architecture/deep-dive-metrics.md` |
| Backup/restore/uninstall lifecycle | `architecture/deep-dive-lifecycle.md` |
| Deployment & operations tooling | `architecture/deep-dive-deployment.md` |

Non-obvious wiring:

- **Request lifecycle**: `RequestCoordinator` in `src/eggpool/request/` orchestrates endpoint → routing → persistence → dispatch → finalization; HTTP layer in `src/eggpool/api/`
- **Runtime generations**: `RuntimeManager` (package root, `runtime_manager.py`) owns active/retiring slots and leases; generation-owned attributes mirrored on `app.state` are mirrors, not authority
- **Protocol transcoding**: `src/eggpool/transcoder/` converts OpenAI ↔ Anthropic; operator guide `docs/transcoding.md`
- **Responses passthrough**: `/v1/responses` is same-protocol only; surface selection is the `request_surface` field (`"chat_completions"` | `"responses"`), not a separate transcoder family → `architecture/deep-dive-request-lifecycle.md`
- **Control plane**: live config reload (rehash) over a Unix-domain socket, `src/eggpool/control/`
- **Routing**: load-based, never cost-based; tier-based via `routing_priority`; pieces split across `routing/`, `quota/`, `retry/`, `catalog/`, `health/`
- **Wire surfaces**: `WireSurfaceName`/`WireProfile` in `src/eggpool/wire/` are independent of `ProtocolName`; `ProviderConfig.wire_surfaces` is synthesized from legacy paths when absent, and `_wire_profiles.toml` accepts only closed Python-registered codec IDs
- **Process model**: supervisor + Granian worker, `workers=1` required (one process = one asyncio event loop); `[server].threads` maps to Granian `runtime_threads` (Rust I/O threads, safe above 1)

## Gotchas

- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound
- **Task-owned transactions**: SQLite access runs inside `db.transaction()` owned by the owning task; process transitions execute inside transactions with atomic rollback. Foreign-task access raises `DatabaseTransactionOwnershipError`
- **`/readyz` never performs a write**: reads a cached probe snapshot
- **`eggpool rehash` serializes reloads**: one reload at a time; concurrent attempts exit with code 4 (`EXIT_RELOAD_BUSY` from `cli_exit_codes.py` — use the constant). Disruptive changes (host, port, db path) require restart instead
- **`ReloadObserver` is inert in production**: observer protocol defaults to no-ops
- **`eggpool connect`/`logout` don't silently restart**: healthy server with missing control socket returns `(False, "control unavailable (server healthy)")`
- **`eggpool update` does a live PyPI lookup**: bare update uses freshness-aware latest check; explicit `VERSION` uses the exact release endpoint and permits deliberate downgrades
- **`static_models` is source of truth for provider-specific protocol**: providers serving non-default protocols must ship `[[providers.<id>.static_models]]` rows
- **No pre-commit hooks configured**: CI runs ruff, pyright, and pytest via GitHub Actions
- **When constructing `RequestCoordinator` in tests**: pass an explicit `transcoder_policy` or assert the desired default
- **`ProviderBoundRequest` dispatch-freeze**: `serialize_provider_payload()` freezes the body; `replace_provider_payload()` and `set_provider_payload(increment_generation=False)` reject when frozen. Only generation-incrementing methods (`set_provider_payload(increment_generation=True)`, `adopt_provider_payload(increment_generation=True)`) clear the freeze — the post-selection transcoder relies on this to replace a previously dispatched body on retry
- **Thinking rejection error class**: `CapabilityError` (400) only when the aggregated thinking status is genuinely `unknown` or `unsupported`. When all supporting accounts are quarantined but the provider entry reports `supported`/`mixed`, a transient 503/502 is raised instead. Aggregation iterates `cache.get_provider_model_entries()` (which applies overrides), not `cache.get_model()` (which does not). See `RequestCoordinator._determine_thinking_rejection_status` and `architecture/deep-dive-request-lifecycle.md`
- **Per-model quarantine suppresses account-wide circuit breaker**: when `effects.model_effect != "none"` (quarantine), `EffectsApplier._apply_account_effect` skips `HealthManager.record_failure()` for that account. The account-wide breaker advances only when the classifier sets `source="transport"` (genuine account-wide failure: DNS, TLS, persistent transport failure with no per-model cause). A per-model 5xx must quarantine the `(account, model)` pair, not the whole account. See `architecture/deep-dive-health.md`
- **Wire profile credentials stay out of metadata**: `resolve_provider_wire_profiles()` carries auth shape only; call `build_wire_profile_headers()` with the selected account key at dispatch time. Surface priorities and bundled hints are revocable preferences, not endpoint truth. See `architecture/deep-dive-providers.md`
- **Runtime wire learning is process-owned and bounded**: `ProcessRuntime.wire_profile_resolver` survives safe generation swaps, keys learned state by a structural candidate fingerprint, and learns only from completed ordinary success or an explicit failure-effects transition. It never probes in the background, stores secrets/raw bodies, or adds a retry budget. See `architecture/deep-dive-providers.md` and `architecture/deep-dive-runtime.md`
- **Canonical wire intent is source-owned**: `wire/ir.py` captures the original request, reasoning intent, normalized usage, response blocks, and bounded streaming events before provider adaptation. Alternate targets must encode from that canonical source; never chain a previously translated provider payload. `ReasoningIntent` keeps effort labels separate from numeric budgets and explicit disable. See `architecture/deep-dive-transcoder.md`
- **Negotiation-safe failure effects**: a bare/unknown 401 never disables credentials, advances health, or cascades; only explicit invalid/expired/revoked credential evidence disables the selected account. Deterministic auth/surface/schema mismatches may reject only the selected wire candidate and retry the same account before downstream handoff. All account and wire retries consume one shared `1 + max_retries_before_stream` upstream-submission budget. See `architecture/deep-dive-retry.md` and `architecture/deep-dive-request-lifecycle.md`

## Error Handling

Read `src/eggpool/errors.py` before adding exceptions (single self-documenting file). Chain with `raise ... from err` or `raise ... from None`. Non-obvious points:

- `ConfigValidationError` lives in `config_validation.py`, **not** `errors.py`, and inherits `AggregatorError` directly (not `ConfigError`). Its subclasses are raised by `eggpool.config_validation.validate_config_file()` and never raise `SystemExit`
- Status-code distinctions: `CapabilityError` (400) ≠ `ModelNotFoundError` (404, carries `model_id`) ≠ `ModelUnavailableError` (503); `RateLimitError` carries `retry_after`; `UpstreamError` carries `status_code`
- `PrematureStreamEOFError` extends `ProxyError`, not `UpstreamError`
- Cross-module errors: `BudgetResolutionError` (thinking budget, in `transcoder/budget_resolver.py`) extends `CapabilityError`; `TranscodeLossError` (`transcoder/errors.py`) maps to HTTP 400 when `loss_policy = "reject"`; `ProtocolMismatchError` (`catalog/protocols.py`) flags endpoint/model-protocol mismatch; `RuntimeManagerLeaseExhaustedError` maps to HTTP 503 in `api/proxy_request.py`

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (~73 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) dispatch without importing Click
- Everything else loads `eggpool.cli_full` lazily (heavy Click CLI); public symbols are forwarded via PEP 562 `__getattr__`

## Git Workflow

- Branch: `main`; commit messages concise, imperative mood
- Never commit secrets, API keys, or `.env` files

## Planning Policy

Planning is proportional to risk. Use the development skill's "Planning proportionality" section for guidance.
