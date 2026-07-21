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
- Optional `orjson` backend: `uv pip install 'eggpool[fast]'` (or `uv sync --extra fast`); see the `eggpool.jsonx` architecture note in the `architecture` skill. Without this extra, EggPool falls back to a stdlib implementation with identical wire behaviour.

## Pre-commit Checks (run before every commit)

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

All four must pass with zero errors.

## Focused Verification

Run specific test subsets without waiting for the full suite:

```bash
# Request-path correctness only (routing, transcoding, finalization)
uv run pytest -m request_path -v

# Dashboard and cache-page tests only
uv run pytest -m dashboard -v

# Performance baseline tests only
uv run pytest -m performance -v

# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Hot-path request-path closure tests (Phases 1–5 + corrective polish + final polish)
uv run pytest tests/unit/test_proxy_request_hotpath_modes.py tests/unit/test_hotpath_corrective_polish.py tests/unit/test_runtime_dispatch_spans_dashboard.py -v

# Dispatch-stability baseline (Milestone A5)
uv run pytest tests/perf/test_dispatch_baseline.py -m performance -v

# Milestone F gap-fill tests (topology, hot-path equivalence, plateau, synchronization)
uv run pytest tests/unit/test_granian_topology.py tests/unit/test_hotpath_equivalence.py tests/unit/test_resource_plateau_extended.py tests/unit/test_synchronization_hardening.py -v

# Milestone F performance matrix (serial, concurrent, streaming, large body)
uv run pytest tests/perf/test_concurrent_workload_matrix.py -m performance -v

# Dispatch-stability selection-claim lock scope (Milestone B)
uv run pytest \
    tests/unit/test_selection_claim.py \
    tests/unit/test_selection_claim_diagnostics.py \
    tests/unit/test_coordinator_claim_lock_scope.py -v

# High-concurrency stream stability (OpenCode hardening) subset — stream
# diagnostics, finalization retry queue, routing trace guard, routing trace
# writer, and the 50-stream burst integration test.
uv run pytest \
    tests/unit/test_stream_diagnostics.py \
    tests/unit/test_stream_finalization_queue.py \
    tests/unit/test_routing_trace_guard.py \
    tests/unit/test_routing_trace_mode.py \
    tests/unit/test_routing_trace_writer.py \
    tests/integration/test_high_concurrency_streaming.py -v

# Bounded maintenance and SQLite hygiene (Milestone E) tests
uv run pytest tests/unit/test_maintenance_budget.py -v

# High-concurrency reproducer CLI (no real providers; mock SSE upstream).
uv run python scripts/repro_high_concurrency_streams.py \
    --concurrency 50 --cancel-rate 0.25 --cancel-offset 2

# Slow-writer burst fairness tests
uv run pytest tests/unit/test_slow_writer_burst_fairness.py -v

# Routing trace guard, mode, and writer tests
uv run pytest tests/unit/test_routing_trace_guard.py tests/unit/test_routing_trace_mode.py tests/unit/test_routing_trace_writer.py -v

# Phase 5 shared runtime-generation factory tests (parity, dispatch writer,
# span rate, recorder, diagnostics, backoffs, cleanup)
uv run pytest tests/unit/test_generation_factory.py -v

# Phase 6 transactional rehash tests (transaction state machine, commit
# protocol, compensation, cancellation shielding, fault injection)
uv run pytest tests/unit/test_reload_manager.py tests/unit/test_reload_failure_injection.py tests/unit/test_rehash_d3_failure_injection_closure.py -v

# Model-info identity subset (tiered matching, fresh-DB service, evidence API,
# safety, migration 0049, OpenRouter contract, deployment-suffix tier, source
# diagnostics, provenance consistency).  Use the repo-relative script when
# running from outside the repo root to avoid ModuleNotFoundError.
scripts/test_model_info_identity.sh
uv run pytest \
    tests/unit/test_model_info_fresh_db_service.py \
    tests/unit/test_model_info_match_evidence_api.py \
    tests/unit/test_model_info_matching_safety.py \
    tests/unit/test_model_info_migration_0049.py \
    tests/unit/test_model_info_tiered_matching.py \
    tests/unit/test_model_info_openrouter_contract.py \
    tests/unit/test_model_info_deployment_suffix.py \
    tests/unit/test_model_info_source_diagnostics.py \
    tests/unit/test_model_info_provenance_consistency.py -v

# Background task first-run subset (run_immediately, initial_delay_s,
# never_run_not_due vs never_run_overdue labels, source_diagnostics counters)
uv run pytest tests/unit/test_background_first_run.py -v

# Model-info FastAPI route registration order (suites /aliases and /matches
# are pinned before the greedy detail route)
uv run pytest tests/unit/test_model_info_route_registration.py -v

# Runtime manager and generation lifecycle tests
uv run pytest tests/unit/test_runtime_manager.py -v

# Control plane and live reload tests
uv run pytest tests/unit/test_control_server.py tests/unit/test_reload_manager.py tests/unit/test_cli_rehash_preflight.py -v

# Reload correctness baseline (Phase 1) — admission, atomicity,
# resources, retirement, and construction parity
uv run pytest tests/integration/reload/ -v

# Phase 3 — Asynchronous generation retirement tests
uv run pytest tests/unit/test_phase3_async_retirement.py -v

# Reload correctness baseline — single file
uv run pytest tests/integration/reload/test_reload_admission.py -v

# Reload correctness baseline — by concern
uv run pytest tests/integration/reload/test_reload_admission.py -v
uv run pytest tests/integration/reload/test_reload_atomicity.py -v
uv run pytest tests/integration/reload/test_reload_resources.py -v
uv run pytest tests/integration/reload/test_reload_retirement.py -v
uv run pytest tests/integration/reload/test_reload_parity.py -v
uv run pytest tests/integration/reload/test_persistence_publication_split.py -v
uv run pytest tests/integration/reload/test_process_mutation_timing.py -v
uv run pytest tests/integration/reload/test_stale_app_state.py -v
uv run pytest tests/integration/reload/test_lease_acquisition_fallback.py -v
uv run pytest tests/integration/reload/test_diagnostics_contract.py -v

# Reload admission race stress (100 runs) — for the Phase 1 acceptance
# criterion "concurrent admission coverage passes or fails consistently
# for at least 100 repeated runs".
uv run python scripts/admission_race_stress.py 100

# Soak validation and workload profiles (Milestone G)
uv run pytest tests/soak/ -v

# Soak workload profiles only
uv run pytest tests/soak/test_workload_profiles.py -v

# Stability assertions (early/late window comparison)
uv run pytest tests/soak/test_stability_assertions.py -v

# Resource plateau validation
uv run pytest tests/soak/test_resource_plateau.py -v

# Database consistency audit
uv run pytest tests/soak/test_db_consistency_audit.py -v

# Extended stability gates (extended-soak mode only)
uv run pytest tests/soak/test_extended_stability_gates.py -m extended_soak -v

# Dispatch stability soak runner
uv run python scripts/run_dispatch_stability_soak.py \
  --profile balanced-file-backed \
  --mode smoke \
  --output artifacts/dispatch-soak/smoke

# Lint auto-fix
uv run ruff check --fix src/

# Type check with errors only
uv run pyright src/ scripts/ 2>&1 | head -20
```

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.

> All `uv run pytest` commands above assume the Eggpool repo root as the
> working directory.  When invoking from a sibling project root, use the
> repo-relative script instead, which always ``cd``s into the repo root
> before running pytest:
>
> ```bash
> EGGPOOL_REPO=/path/to/eggpool "$EGGPOOL_REPO/scripts/test_model_info_identity.sh"
> ```

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
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`, `tests/perf/`, `tests/soak/`, `tests/live/`
- Provider contract tests: `uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v`
- Soak tests in `tests/soak/` for long-running stability validation (Milestone G)

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
- **Process model**: supervisor + Granian worker (`workers=1`), PID file lifecycle, daemon mode (default for `eggpool serve`; `--verbose` for foreground). Default `runtime_threads=1` (single event-loop thread is canonical; values > 1 emit a startup warning), `database_worker_threads=2` (separate read-only stats connection).
- **Background tasks**: `src/eggpool/background/` manages retention cleanup, periodic tasks, and startup crash recovery via `TaskSupervisor`. Fixed-delay scheduler: next interval begins after previous tick completes. `initial_delay_s` consumed exactly once per task lifecycle. Process-owned tasks (`checkpoint`, `metrics_flush`, `update_checker`, `automatic_backup`) survive generation swaps; generation-leased tasks are retired when their generation is retired.
- **Runtime generations**: `RuntimeManager` owns active/retiring generation slots. Request-path code obtains `GenerationLease` via `wrap_stream_with_lease` or `leased_runtime`. `ProcessRuntime` holds process-owned containers (database connections) that outlive any generation. Lease acquisition is fail-closed: `RuntimeManager.acquire()` raises `RuntimeManagerLeaseExhaustedError` (→ HTTP 503) when no generation slot is accepting leases, rather than falling back to a legacy path. The request handler catches this and returns `503 Service Unavailable` immediately.
- **Candidate resource ownership (Phase 4)**: `RuntimeGenerationCandidate` makes ownership of reload-created resources explicit from the moment each resource is constructed. Every generation-owned closeable is registered immediately after construction via `candidate.register_resource()`. Any failure before publication calls `candidate.abort()` which closes all registered resources in reverse registration order, collects close errors without masking the primary error, and emits `CleanupDiagnostics`. Pre-publication failures (reconcile, publish) also abort the candidate with `asyncio.shield()` so bounded cleanup completes even under task cancellation. `ReloadManager.snapshot()` surfaces `last_cleanup_diagnostics` for observability. Successful publication calls `candidate.transfer_to_runtime_manager()` to detach candidate cleanup. Ownership taxonomy: process-owned (database, coalescer, dispatch writer, routing trace writer, control server), generation-owned (client pool, outbound manager, DNS backend, supervisor), candidate-owned (resources during construction), request-owned (leases).
- **Shared runtime-generation factory (Phase 5)**: `RuntimeGenerationFactory` (`src/eggpool/generation_factory.py`) eliminates behavior drift between startup and reload by constructing all generation-owned services through a single authoritative path. Both startup and reload call `factory.prepare()`. The factory accepts process-owned dependencies as explicit inputs, constructs all generation-owned services with identical wiring, registers closeable resources on the candidate (reload case), hydrates persisted health/backoff state, and returns a `PreparedRuntimeGeneration`. Startup-only operations (migrations, crash recovery, catalog refresh, process workers) remain outside. Tests: `tests/unit/test_generation_factory.py`.
- **Transactional rehash (Phase 6)**: `ReloadTransaction` (`src/eggpool/reload_transaction.py`) introduces a monotonic state machine for the live-rehash transaction: `created → validated → diffed → candidate_prepared → persistence_prepared → process_transitions_prepared → commit_started → runtime_published → process_transitions_applied → persistence_committed → observable_state_updated → retirement_scheduled → completed`. Process-supervisor task reconfiguration (`apply_spec_diff`) is deferred to the commit phase (after publication) to avoid leaving the process supervisor in a partially-reconfigured state. Commit ordering: SQLite persistence delta → candidate publication → process transitions → completion. Post-publication failures are compensated by accepting the new generation (the persistence delta is idempotent). Cancellation after publication is shielded to prevent mixed state. The `ReloadManager.active_transaction` property surfaces the current transaction for diagnostics.
- **Live configuration rehash**: `eggpool rehash` applies supported changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`. Field reload classification lives in `src/eggpool/config_reload_policy.py` (`_FIELD_DISPOSITION` map). LIVE fields include provider/account/routing/model-override families, `[transcoder]`, `[compression]`, `[cache]`, subset of `[models]`, and retention durations. Everything else is `RESTART_REQUIRED`. `eggpool rehash` JSON output is pinned at 9 keys. Reload admission uses an atomic claim primitive (`ReloadManager._claim_mutex` + `_reload_claimed`) that eliminates the TOCTOU race on concurrent reload attempts; the claim state is exposed via `ReloadManager.snapshot()` diagnostics.
- **Health management**: `src/eggpool/health/` implements `HealthManager` circuit breaker and per-account health tracking for routing eligibility.
- **Retry classification**: `src/eggpool/retry/` classifies upstream errors for failover and retry decisions.
- **Security**: `src/eggpool/security/` handles header redaction middleware and security utilities.
- **Integrations**: `src/eggpool/integrations/` generates external tool configs (OpenCode, Claude Code, Aider, Codex, etc.).
- **Safe compression**: `src/eggpool/transcoder/compression/` implements observe/safe compression, policy resolution, advisory tuning, and deterministic markers. Safe-mode `apply_safe_compression()` returns the original payload by identity on no-op.
- **Model info sources**: `src/eggpool/model_info/` enriches model metadata from multiple sources with tiered identity matching (6 tiers). Case-insensitive lookups at every layer.
- **Dispatch timing**: `LocalPreUpstreamRecorder` measures full EggPool-side window; `DispatchOverheadRecorder` covers coordinator-internal slice. Both use monotonic clocks.
- **Performance hot path**: `Router.build_routing_plan()` is the authoritative selection path (no fallback to legacy `select_accounts()`). `DispatchSpanRecorder` provides 200-sample dispatch span telemetry.

## Gotchas

- **`fastcli` and `runtime_paths` are stdlib-only**: do not add transitive imports. They must stay lightweight for the Raspberry Pi watchdog contract.
- **`eggpool rehash` serializes reload transactions**: only one reload in progress at a time. Concurrent rejections with `reload_in_progress`. The admission claim is atomic under `ReloadManager._claim_mutex` — no check-then-lock window.
- **`ReloadObserver` is inert in production**: the observer protocol has no-op defaults; attaching an observer with no overrides has zero cost. Tests use it for deterministic stage barriers.
- **`eggpool connect`/`logout` don't silently restart**: if the server is healthy but control socket is missing, they return `(False, "control unavailable (server healthy)")`.
- **`eggpool update` must make a live PyPI lookup**: the CLI helper MUST NOT consult `UpdateChecker.snapshot()`. Use `is_newer_version()` for version comparison, never raw string equality.
- **No pre-commit hooks configured**: CI runs ruff, pyright, and pytest via GitHub Actions.
- **`static_models` is source of truth for provider-specific protocol**: providers serving non-default protocol must ship `[[providers.<id>.static_models]]` rows.
- **Upstream-authoritative suppression**: local quota estimates are advisory (`local_quota_mode = "score_only"`). Only upstream-observed failures suppress routing.
- **Routing is load-based, not cost-based**: `QuotaFairScorer` uses request count and token count, never `cost_microdollars`.
- **Pricing safeguard**: ambiguous bare upstream pricing defaults to dollars-per-million. `RequestFinalizer` never floors canonical cost to reservation. Use `eggpool stats repair-costs` for historical cleanup (dry-run first).
- **When constructing `RequestCoordinator` in tests**: pass an explicit `transcoder_policy` or assert the desired default; never rely on implicit `None`.
- **`CapabilityError` (400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)**: `BudgetResolutionError` is a subclass of `CapabilityError`.
- **DB migrations are numbered SQL files** in `src/eggpool/db/schema/`. The `model_info_*` sidecar tables carry FKs to `models.model_id`; repository writes seed a placeholder `models` row in the same transaction.
- **`reload_in_progress` exits with code 4** (`EXIT_RELOAD_BUSY`). Use the constant from `cli_exit_codes.py`, not the literal string.
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound. `MetricsWriteCoalescer` is the only component using `threading.Lock` for cross-thread safety.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ModelInfoSourceFetchError`, `ContextLimitExceededError`, `CapabilityError`
- `RuntimeManagerLeaseExhaustedError` (RuntimeError) — raised by `RuntimeManager.acquire()` when no generation slot is accepting leases or the manager is shutting down; mapped to HTTP 503 in `proxy_request.py`
- `ConfigValidationError(ConfigError)` and its subclasses (`ConfigFileAccessError`, `ConfigParseError`, `ConfigSchemaError`, `ConfigStartupAuthError`, `ConfigAccountCredentialError`, `ConfigInternalError`) are raised by `eggpool.config_validation.validate_config_file()`. They chain from the underlying failure and never raise `SystemExit`.

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
