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
- Performance, live, and diagnostic reproducer tests are manually invoked, not run in CI

## Release

Manual release procedure — no automated release workflow. See `docs/releasing.md`.

## Runtime Validation

Target-device validation is optional and risk-based. For request lifecycle,
streaming, database, reload, or dependency changes, run the short manual
smoke described in `docs/releasing.md` when representative hardware is
available. It is a confidence check, not a CI gate or a retained evidence
format. For SBC resource checks, use a fixed short stabilization window and
the existing `eggpool runtime-status --json`, OS process/socket tools, and
startup operational-profile log. Record host, Python, config profile, and
whether optional features are enabled; never present workstation measurements
as Raspberry Pi results and never turn a numeric observation into a CI gate.
The high-concurrency reproducer in `scripts/repro_high_concurrency_streams.py`
remains available for diagnosing stream-specific regressions.

## File Organization

- Source: `src/eggpool/`
- Tests: `tests/` (mirrors src structure)
- Config: `config.example.toml`, `.env.example`
- DB schema: `src/eggpool/db/schema/`
- Scripts: `scripts/` (operational, also type-checked by pyright)
- Deployment: `deploy/`
- Shared assets: `src/eggpool/_share/` (bundled config examples for pipx installs)
- Architecture docs: `architecture/` (deep-dive per subsystem)
- Plans: `plans/` (completed implementation plans, historical reference)

## Architecture Index

> Full design details are in `architecture/README.md` and the `architecture` skill.

- **Request lifecycle**: `RequestCoordinator` orchestrates endpoint → routing → persistence → dispatch → finalization.
- **Pending claim publication**: after a routing plan selects an account, `_selection_claim_lock` publishes one provisional request/token unit through `QuotaEstimator` before SQLite persistence. Persistence remains outside the lock; success converts that ownership to the canonical reservation, while failure or cancellation releases it exactly once.
- **Dispatch isolation**: local preparation and response adaptation failures are terminal local errors with no provider retry. Only typed HTTPX transport failures may retry, only across distinct accounts before `downstream_started`.
- **Multi-provider architecture**: provider-suffixed model IDs (`model-id/provider-id`), `ProviderClientPool`, `OutboundClientManager`.
- **Provider contracts**: `compose_provider_url()` is the single source of truth for upstream URLs.
- **Protocol transcoding**: `src/eggpool/transcoder/` — OpenAI ↔ Anthropic conversion. The transcoder's `usage` property returns a default; finalization reads usage from the coordinator's observer.
- **JSON backend (`eggpool.jsonx`)**: preferred `orjson` (`eggpool[fast]`), falls back to stdlib. Override with `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`. Off the request path, stdlib `json` allowed for deterministic hashing.
- **Database invariants**: SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML.
- **Request schema freeze**: the historical `requests` table is frozen for optional diagnostics. New columns require durable lifecycle/accounting or externally visible compatibility justification; feature-specific diagnostics use sparse/event or narrowly scoped sidecar storage with existing retention/redaction rules. Do not add cosmetic migrations or a generic EAV store.
- **Quota and routing**: tier-based via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor. Load-based (request count + token count + active count + health), never cost-based. Positive account `weight` scales effective request/token capacity within an eligible tier (`1.0` baseline, `2.0` approximately double, `0.5` approximately half); priority and health eligibility remain authoritative.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. See `errors.py`.
- **Process model**: supervisor + Granian worker (`workers=1`), daemon mode (`--verbose` for foreground). `runtime_threads=1` is required. Readiness probe is process-owned and disabled by default.
- **Lean defaults**: loopback binding, low-wear analytics, provider pools of 16/4, background outbound pools of 8/2. Model-info, routing traces, readiness writes, automatic backups, dispatch writing are opt-in. Disabled features are `None` and construct no clients/tasks.
- **Runtime generations**: `RuntimeManager` owns active/retiring generation slots. `RuntimeGenerationFactory.prepare()` is the shared startup/rehash construction boundary. `RequestFinalizationSupervisor` is generation-owned.
- **Live rehash**: `eggpool rehash` applies changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`.
- **Health management**: `src/eggpool/health/` — circuit breaker, per-account tracking, bounded 1,800s backoff, scoped model quarantine, `DatabaseWritableProbe` for `/readyz`.
- **Background tasks**: `src/eggpool/background/` — `TaskSupervisor`, fixed-delay scheduler. Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation.
- **Database recovery**: startup integrity is fail-closed. Indeterminate outcomes exit the worker; systemd restarts, then startup integrity and crash reconciliation run before readiness.

## Gotchas

- **`eggpool rehash` serializes reload transactions**: only one reload in progress at a time. Concurrent rejections with `reload_in_progress`.
- **`ReloadObserver` is inert in production**: the observer protocol has no-op defaults.
- **`eggpool connect`/`logout` don't silently restart**: if the server is healthy but control socket is missing, they return `(False, "control unavailable (server healthy)")`.
- **`eggpool update` must make a live PyPI lookup**: bare updates use `is_newer_version()` and retain the freshness-aware latest path; an explicit `VERSION` uses the exact PyPI release endpoint, permits deliberate downgrades, and verifies the installed version before restart.
- **No pre-commit hooks configured**: CI runs ruff, pyright, and pytest via GitHub Actions.
- **`static_models` is source of truth for provider-specific protocol**: providers serving non-default protocol must ship `[[providers.<id>.static_models]]` rows.
- **Upstream-authoritative suppression**: local quota estimates are advisory. Only upstream-observed failures suppress routing.
- **Routing is load-based, not cost-based**: `QuotaFairScorer` uses request count and token count, never `cost_microdollars`.
- **Selection claim visibility**: `QuotaEstimator` folds pending request/token claims into the same scorer reservation-load snapshot. There is no pending-claim table, sweeper, background task, or cross-process coordination.
- **Routing trace persistence**: `RoutingDecisionRepository.create_many()` uses one `executemany` operation inside the caller-owned transaction and propagates database failures; trace-off and unsampled paths do not call it.
- **Catalog write reduction**: the default discovery cadence is not a full catalog rewrite. `_persist_catalog()` compares stable semantic model/provider fields outside the write transaction, persists only deltas, stores successful freshness in `catalog_refresh_state`, and coarsens steady successful pings to an internal 30-minute sample while failures and state transitions remain immediate.
- **Account weight semantics**: weight is a relative capacity/share hint only among otherwise eligible accounts in the selected priority tier. It affects request/token utilization, not cost, and does not promise exact request ratios across different request sizes or provider histories.
- **`app.state` generation-owned attributes are mirrors, not authority**: New code should use `get_active_generation(request)` or acquire a lease.
- **When constructing `RequestCoordinator` in tests**: pass an explicit `transcoder_policy` or assert the desired default.
- **`CapabilityError` (400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)**.
- **DB migrations are numbered SQL files** in `src/eggpool/db/schema/`.
- **`reload_in_progress` exits with code 4** (`EXIT_RELOAD_BUSY`). Use the constant from `cli_exit_codes.py`.
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound.
- **`/readyz` never performs a write**: reads a cached probe snapshot.
- **Readiness probe is process-owned**: survives generation swaps.
- **Process transitions execute inside `db.transaction()`**: atomic rollback on any failure.
- **Database ambiguity ownership**: durable request, attempt, and reservation identities are created before the commit boundary; an indeterminate outcome fails the worker closed and startup reconciliation is the only repair boundary.
- **Finalization round trips**: first request/attempt/reservation terminal transitions use SQLite `RETURNING` results inside one correctness transaction; no-transition/idempotent components still perform focused durable reads.
- **Terminal lifecycle**: streaming 4xx paths defer terminal work to `_handle_exhausted()`; they must not finalize and then raise into a second finalizer. Capability rejection is a client error with no provider penalty and uses the same retained terminal job as normal completion and cancellation.
- **Finalization saturation**: supervisor capacity rejection is a local overload invariant. Before downstream handoff it raises a typed local terminal-invariant error; after handoff it records a bounded diagnostic and leaves durable pending state for startup repair. It never spawns detached cleanup or penalizes a provider.
- **SSE diagnostics**: `stream_diagnostics` exposes canonical/compatibility completion, premature EOF, HTTPX transport, and provider-bound first-byte/idle timeout outcomes. Historical lifetime fields remain bounded compatibility metadata but no lifetime timer runs. Each last-event record carries configured limits and bounded timing evidence; stream content and credentials are never persisted.
- **Router self-healing**: temporary quota, rate, server, transport, protocol, and runtime model suppression is capped at 1,800 seconds. Success clears only matching transient state; authentication and authoritative model withdrawal require explicit credential/operator or catalog recovery. Every acquired half-open probe must end in success, provider failure, or idempotent release.
- **Client attribution**: `security.trusted_proxies` is an exact peer-IP allowlist. Forwarded client-IP headers are ignored unless the immediate ASGI peer is listed; an empty list is the safe default.

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
- `ConfigValidationError(ConfigError)` and its subclasses are raised by `eggpool.config_validation.validate_config_file()`. They chain from the underlying failure and never raise `SystemExit`.

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

Completed implementation plans must not create permanent CI jobs, markers, evidence formats, or plan-numbered test suites. Regression tests must be merged into capability-based suites before a plan is closed.
