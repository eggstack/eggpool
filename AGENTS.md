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
- Optional `orjson` backend: `uv pip install 'eggpool[fast]'` (or `uv sync --extra fast`)

## Local Development Loop

Fast focused iteration:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest <affected test paths> -q --tb=short --maxfail=1
```

## Before-Push Check

Run the same checks as the primary CI job:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
```

## CI

Two GitHub Actions jobs on every PR:

| Job | Python | What it does |
|-----|--------|-------------|
| `check` | 3.12 | ruff format + ruff check + pyright + canonical test suite |
| `compat-311` | 3.11 | `pytest tests/smoke/` — package import, config, DB migration, one request through real Eggpool, CLI |

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

## Focused Verification

```bash
# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Integration tests only
uv run pytest -m integration -v

# Reload tests only
uv run pytest tests/integration/reload/ -v

# Request-path correctness (routing, transcoding, finalization)
uv run pytest -m request_path -v

# Dashboard tests
uv run pytest -m dashboard -v

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
- Tests in `tests/unit/`, `tests/integration/`, `tests/smoke/`, `tests/perf/`, `tests/soak/`, `tests/contract/`
- Smoke suite (`tests/smoke/`): package import, config parsing, invalid config rejection, check-config validation, DB migration, one non-stream request, one streaming request, CLI help
- Provider contract tests: `uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v`
- Performance and soak tests are manually invoked, not run in CI

## Release

Manual release procedure — no automated release workflow. See `docs/releasing.md`.

## Runtime Validation

Manual risk-based validation on target SBC hardware. See `docs/releasing.md` § Risk-Based SBC Validation.

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
- **Protocol transcoding**: transparent request/response format conversion between OpenAI and Anthropic protocols. Implemented in `src/eggpool/transcoder/` and `src/eggpool/request/coordinator.py`. The streaming hot path is tuned for high-concurrency coding-agent loads: the coordinator's `IncrementalSSEObserver` is the single observer, `StreamingTranscoder.feed`/`flush` are synchronous (no per-chunk `await`), and frame helpers use compact JSON separators `(",", ":")`. The transcoder's `usage` property returns a default `StreamUsageResult()`; finalization must read usage from the coordinator's observer.
- **JSON backend (`eggpool.jsonx`)**: wire bodies, SSE frame helpers, and hot-path request body parsing go through `eggpool.jsonx`. Preferred backend is `orjson`; falls back to stdlib when `fast` extra is not installed. Override at runtime with `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`. Off the request path, stdlib `json` is allowed for deterministic hashing and persisted diagnostic metadata.
- **Database invariants**: SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML.
- **Quota and routing**: tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor. Routing is load-based (request count + token count + active count + health), never cost-based.
- **Error hierarchy**: `AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning capability mismatches. `TranscodeLossError` (HTTP 400) for loss-policy reject. `ProtocolMismatchError` for endpoint/model-protocol mismatches.
- **Process model**: supervisor + Granian worker (`workers=1`), PID file lifecycle, daemon mode (default for `eggpool serve`; `--verbose` for foreground). Default `runtime_threads=1` (single event-loop thread is canonical; values > 1 emit a startup warning), `database_worker_threads=2` (separate read-only stats connection). Readiness probe is process-owned and started after database initialization, stopped before database close.
- **Background tasks**: `src/eggpool/background/` manages retention cleanup, periodic tasks, and startup crash recovery via `TaskSupervisor`. Fixed-delay scheduler: next interval begins after previous tick completes. `initial_delay_s` consumed exactly once per task lifecycle. Process-owned tasks (`checkpoint`, `metrics_flush`, `update_checker`, `automatic_backup`) survive generation swaps; generation-leased tasks are retired when their generation is retired.
- **Runtime generations**: `RuntimeManager` owns active/retiring generation slots. Request-path code obtains `GenerationLease` via `wrap_stream_with_lease` or `leased_runtime`. `ProcessRuntime` holds process-owned containers (database connections) that outlive any generation. Lease acquisition is fail-closed: `RuntimeManager.acquire()` raises `RuntimeManagerLeaseExhaustedError` (→ HTTP 503) when no generation slot is accepting leases, rather than falling back to a legacy path. The request handler catches this and returns `503 Service Unavailable` immediately. During a staged reload swap, `RuntimeManager` uses a predicate-based lease gate (`_lease_condition` with `_lease_admission_gated` boolean): `acquire()` callers wait on the condition rather than polling, and the gate is released (condition notified) on commit or rollback. The `PendingGenerationSwap` protocol splits publication into explicit `stage()` → `commit()`/`rollback()` → `finalize_retirement()` boundaries, ensuring no request can acquire the candidate generation until the SQLite commit and required process transitions succeed.
- **Active-generation state authority**: `RuntimeManager` is the sole authoritative source for active generation ID, config, digest, and generation-owned services. Request paths acquire a generation lease via `acquire()`. Production proxy request paths resolve generation-owned services from the leased generation, not from `app.state` mirrors. Readiness, dashboard, and stats routes read from the active generation via `get_active_generation(request)` helper.
- **Candidate resource ownership**: `RuntimeGenerationCandidate` makes ownership of reload-created resources explicit from construction. Every generation-owned closeable is registered immediately. Any failure before publication calls `candidate.abort()` which closes all registered resources in reverse registration order.
- **Shared runtime-generation factory**: `RuntimeGenerationFactory` (`src/eggpool/generation_factory.py`) eliminates behavior drift between startup and reload by constructing all generation-owned services through a single authoritative path.
- **Transactional rehash**: `ReloadTransaction` (`src/eggpool/reload_transaction.py`) introduces a monotonic state machine for the live-rehash transaction. Process transitions execute inside the SQLite transaction so they roll back atomically on failure.
- **Live configuration rehash**: `eggpool rehash` applies supported changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`. Field reload classification in `src/eggpool/config_reload_policy.py`.
- **Health management**: `src/eggpool/health/` implements `HealthManager` circuit breaker and per-account health tracking for routing eligibility.
- **Readiness probe**: `DatabaseWritableProbe` (`src/eggpool/health/writable_probe.py`) performs real SQLite write probes on a bounded cadence and caches the result. `/readyz` reads the cached snapshot.
- **Safe compression**: `src/eggpool/transcoder/compression/` implements observe/safe compression, policy resolution, advisory tuning, and deterministic markers.
- **Model info sources**: `src/eggpool/model_info/` enriches model metadata from multiple sources with tiered identity matching (6 tiers).
- **Performance hot path**: `Router.build_routing_plan()` is the authoritative selection path. `DispatchSpanRecorder` provides dispatch span telemetry.
- **Request finalization**: Process-owned finalization jobs ensure terminal cleanup independent of client request tasks. `RequestFinalizationSupervisor` manages bounded, deduplicated active jobs with shutdown drain/adopt.
- **Database recovery**: `DatabaseRecoveryController` handles connection invalidation with single-flight recovery, bounded retry, and transaction reconciliation.
- **Failure effects and quarantine**: `classify_failure_effects()` centralizes failure consequences. `ModelQuarantine` implements bounded quarantine state machine with corroboration before terminal withdrawal.
- **Thinking control normalization**: Provider-bound `ThinkingControlContract` validates and normalizes thinking/reasoning controls after provider/account selection.
- **Provider payload lifecycle**: `ProviderBoundRequest` manages decoded payload lifecycle with copy-on-write semantics. `TransformPipeline` runs ordered post-selection transforms.

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

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `DatabaseError` → `DatabaseCommitError`, `DatabaseConnectionInvalidatedError`, `DatabaseRollbackError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ModelInfoSourceFetchError`, `ContextLimitExceededError`, `CapabilityError`
- `RuntimeManagerLeaseExhaustedError` (RuntimeError) — mapped to HTTP 503 in `proxy_request.py`
- `ConfigValidationError(ConfigError)` and its subclasses are raised by `eggpool.config_validation.validate_config_file()`. They chain from the underlying failure and never raise `SystemExit`.

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

## Planning Policy

Completed implementation plans must not create permanent CI jobs, markers, evidence formats, or plan-numbered test suites. Regression tests must be merged into capability-based suites before a plan is closed.
