# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — architecture index and quick reference; see `architecture/README.md` for full design details
- `deployment` — production deployment, systemd, operational scripts, configuration changes
- `development` — linting, testing, pre-commit checks, code style

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
- CI installs with `uv sync --frozen --extra dev` (locks match `uv.lock` exactly)
- Entry point: `src/eggpool/cli.py` → `eggpool` console script
- Config: `config.toml` + `.env` for API keys
- Optional `orjson` backend: `uv pip install 'eggpool[fast]'` (or `uv sync --extra fast`)

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
format. The high-concurrency reproducer in
`scripts/repro_high_concurrency_streams.py` remains available for diagnosing
stream-specific regressions.

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
- **Multi-provider architecture**: provider-suffixed model IDs (`model-id/provider-id`), `ProviderClientPool`, `OutboundClientManager`.
- **Provider contracts**: `compose_provider_url()` is the single source of truth for upstream URLs.
- **Protocol transcoding**: transparent request/response format conversion between OpenAI and Anthropic protocols in `src/eggpool/transcoder/`. Streaming hot path: one bounded `SSEDecoder` per upstream stream, synchronous `translate_frame()`/`finish()`, compact JSON separators `(",",":")`, lazy JSON-object parse cache. The transcoder's `usage` property returns a default; finalization must read usage from the coordinator's observer.
- **JSON backend (`eggpool.jsonx`)**: wire bodies, SSE frame helpers, and hot-path request body parsing. Preferred: `orjson` (install `eggpool[fast]`); falls back to stdlib. Override with `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`. Off the request path, stdlib `json` allowed for deterministic hashing.
- **Database invariants**: SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML.
- **Quota and routing**: tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor. Load-based (request count + token count + active count + health), never cost-based.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` (400) for thinking mismatches. `TranscodeLossError` (400) for loss-policy reject. `ProtocolMismatchError` for endpoint/model-protocol mismatches.
- **Process model**: supervisor + Granian worker (`workers=1`), daemon mode (`--verbose` for foreground). `runtime_threads=1` canonical (values > 1 emit startup warning), `database_worker_threads=2`. Readiness probe is process-owned.
- **Runtime generations**: `RuntimeManager` owns active/retiring generation slots. Lease acquisition is fail-closed: `RuntimeManagerLeaseExhaustedError` → HTTP 503. Staged reload swap: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`. `RuntimeGenerationCandidate` owns reload-created resources; `candidate.abort()` closes in reverse order.
- **Live rehash**: `eggpool rehash` applies provider/account/routing/model-override changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`. `ReloadTransaction` state machine executes process transitions inside SQLite transaction for atomic rollback.
- **Health management**: `src/eggpool/health/` — `HealthManager` circuit breaker, per-account health tracking, `DatabaseWritableProbe` (real SQLite write probes, cached for `/readyz`).
- **Request finalization**: `RequestFinalizationJob` keyed by `(proxy_request_id, attempt_id)`. `RequestFinalizationSupervisor` is the sole process-owned retry scheduler, using one bounded timer and capped retry age/backoff. `FinalizationResult` separates durable terminal/transition, reservation convergence, and runtime cleanup; retryable attempts use coordinator-retained cleanup with 128-entry capacity.
- **Stale runtime accounting**: the bounded stale sweep processes only rows it transitions, aggregates one active unit per accepted request, and applies one exact decrement per account. Reservation ownership is based on active identity/dimensions, so zero-cost request/token reservations are released; underflow logs an invariant warning and clamps to zero.
- **Terminal identities/statuses**: request and attempt ambiguity use distinct `request_finalization`/`attempt_finalization` strategies with explicit request, attempt, and reservation IDs. Recovery imports canonical terminal status sets and treats unknown or mismatched durable state as unresolved.
- **Streaming completion**: `classify_stream_eof()` uses provider-bound `stream_completion_policy` (`strict`/`compatible`/`permissive_observe`). Only canonical OpenAI `[DONE]` or Anthropic `message_stop` is strict. Incomplete EOF → `MIDSTREAM_ERROR`, never retried after handoff.
- **Thinking control normalization**: Provider-bound `ThinkingControlContract` validates/normalizes thinking controls. `ControlFieldAdaptation` provides per-field dispositions. Built-in contract resolution: specificity before priority.
- **Provider payload lifecycle**: `ProviderBoundRequest` is the sole provider-payload authority after client parsing. Copy-on-write generation-aware mutations, one final serialization cache, frozen before dispatch. `ProxyRequestContext.upstream_body` is a compatibility mirror only.
- **Dispatch persistence contract**: `persist_dispatch_bundles()` is binary: a validated result list or an exception; rollback never returns placeholder identities. `PersistedDispatchResult` requires non-empty request/reservation IDs and a positive attempt ID. `DispatchPersistenceWriter` fans batch failures to every waiter, keeps failed work out of persisted counters, and accepts submissions only from its owner event loop.
- **Background tasks**: `src/eggpool/background/` — `TaskSupervisor`, fixed-delay scheduler. Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation.
- **Database recovery**: `DatabaseRecoveryController` — single-flight, bounded recovery keeps public reads/writes closed until candidate schema verification, private writable probing, and ambiguity reconciliation succeed; unresolved work is retained and buffer overflow fails closed.
- **Failure effects and quarantine**: `classify_failure_effects()` centralizes consequences. `ModelQuarantine` — bounded state machine with corroboration before terminal withdrawal.

## Gotchas

- **`fastcli` and `runtime_paths` are stdlib-only**: do not add transitive imports. They must stay lightweight for the Raspberry Pi watchdog contract.
- **`eggpool rehash` serializes reload transactions**: only one reload in progress at a time. Concurrent rejections with `reload_in_progress`.
- **`ReloadObserver` is inert in production**: the observer protocol has no-op defaults.
- **`eggpool connect`/`logout` don't silently restart**: if the server is healthy but control socket is missing, they return `(False, "control unavailable (server healthy)")`.
- **`eggpool update` must make a live PyPI lookup**: use `is_newer_version()` for version comparison, never raw string equality.
- **No pre-commit hooks configured**: CI runs ruff, pyright, and pytest via GitHub Actions.
- **`static_models` is source of truth for provider-specific protocol**: providers serving non-default protocol must ship `[[providers.<id>.static_models]]` rows.
- **Upstream-authoritative suppression**: local quota estimates are advisory. Only upstream-observed failures suppress routing.
- **Routing is load-based, not cost-based**: `QuotaFairScorer` uses request count and token count, never `cost_microdollars`.
- **`app.state` generation-owned attributes are mirrors, not authority**: New code should use `get_active_generation(request)` or acquire a lease.
- **When constructing `RequestCoordinator` in tests**: pass an explicit `transcoder_policy` or assert the desired default.
- **`CapabilityError` (400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)**.
- **DB migrations are numbered SQL files** in `src/eggpool/db/schema/`.
- **`reload_in_progress` exits with code 4** (`EXIT_RELOAD_BUSY`). Use the constant from `cli_exit_codes.py`.
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound.
- **`/readyz` never performs a write**: reads a cached probe snapshot.
- **Readiness probe is process-owned**: survives generation swaps.
- **Process transitions execute inside `db.transaction()`**: atomic rollback on any failure.
- **Ambiguous-operation ownership**: pass `ambiguous_operation=` directly to `db.transaction()`; never use a shared pending descriptor. The descriptor is installed only after lock acquisition and is acknowledged only after durable convergence.
- **Terminal lifecycle**: streaming 4xx paths defer terminal work to `_handle_exhausted()`; they must not finalize and then raise into a second finalizer. Capability rejection is a client error with no provider penalty and uses the same retained terminal job as normal completion and cancellation.
- **SSE diagnostics**: `stream_diagnostics` exposes canonical/compatibility completion, premature EOF, HTTPX transport, and provider-bound first-byte/idle timeout outcomes. Historical lifetime fields remain bounded compatibility metadata but no lifetime timer runs. Each last-event record carries configured limits and bounded timing evidence; stream content and credentials are never persisted.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `ConfigError` → `ConfigValidationError` (with subclasses: `ConfigFileAccessError`, `ConfigParseError`, `ConfigSchemaError`, `ConfigStartupAuthError`, `ConfigAccountCredentialError`, `ConfigInternalError`)
- `DatabaseError` → `DatabaseCommitError`, `DatabaseConnectionInvalidatedError`, `DatabaseRollbackError`
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
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files

## Planning Policy

Completed implementation plans must not create permanent CI jobs, markers, evidence formats, or plan-numbered test suites. Regression tests must be merged into capability-based suites before a plan is closed.
