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
uv run python scripts/audit_xfail_skips.py
```

All five must pass with zero errors.

## CI Partitioning

CI runs 7 parallel jobs:

| Job | Python | Command |
|-----|--------|---------|
| lint | 3.12 | `ruff format --check` + `ruff check` |
| typecheck | 3.12 | `pyright src/ scripts/` |
| unit-integration | 3.11, 3.12 | `pytest -m "not slow and not performance and not soak and not extended_soak and not live"` |
| reload-control | 3.11, 3.12 | `pytest tests/integration/reload/` |
| plan-016-corrective | 3.11, 3.12 | Plan 016/017 focused test command (see below) |
| plan-018-reload-closure | 3.11, 3.12 | Plan 018/019/020 reload closure tests (see below) |
| plan-023-error-isolation | 3.11, 3.12 | Plan 023 error-isolation reproducer tests (see below) |
| performance | 3.12 | `pytest -m performance` |
| soak-audit | 3.12 | `pytest -m soak` + `audit_xfail_skips.py` |

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

# Phase 5 shared runtime-generation factory tests (parity, dispatch writer
# selection, span rate, recorder, diagnostics, backoffs, cleanup, no-op reload)
uv run pytest tests/unit/test_generation_factory.py -v

# Phase 6 transactional rehash tests (transaction state machine, commit
# protocol, compensation, cancellation shielding, fault injection)
uv run pytest tests/unit/test_reload_manager.py tests/unit/test_reload_failure_injection.py tests/unit/test_rehash_d3_failure_injection_closure.py tests/unit/test_phase6_fault_injection.py -v

# Plan 023 — Error-isolation reproducer and invariant baseline (mock upstream,
# state audit, cancellation seams, database faults, JSON counters, baselines)
uv run pytest \
    tests/unit/test_plan_023_state_audit.py \
    tests/unit/test_plan_023_cancellation_seams.py \
    tests/unit/test_plan_023_database_fault_matrix.py \
    tests/unit/test_plan_023_json_operation_counters.py \
    tests/integration/test_plan_023_minimax_thinking_reproducer.py \
    tests/integration/test_plan_023_error_isolation_matrix.py \
    tests/soak/test_plan_023_error_isolation_baseline.py \
    tests/perf/test_plan_023_request_path_baseline.py -v

# Plan 024 — Provider-bound thinking-control normalization (contract,
# adaptation, builtin contracts, native/transcoded normalization,
# opencode-minimax contract, compatibility retry, metrics, trace)
uv run pytest \
    tests/unit/test_plan_024_thinking_control_contract.py \
    tests/unit/test_plan_024_provider_request_adaptation.py \
    tests/unit/test_plan_024_builtin_contracts.py \
    tests/unit/test_plan_024_native_provider_normalization.py \
    tests/unit/test_plan_024_transcoded_provider_normalization.py \
    tests/unit/test_plan_024_thinking_trace.py \
    tests/unit/test_plan_024_thinking_metrics.py \
    tests/integration/test_plan_024_opencode_minimax_contract.py \
    tests/integration/test_plan_024_compatibility_retry.py -v

# Plan 025 — Typed failure effects and bounded model quarantine (effects
# matrix, signal extraction, quarantine state machine, hydration,
# idempotency, error isolation, cross-provider quarantine, operator CLI,
# closure evidence)
uv run pytest \
    tests/unit/test_plan_025_failure_effects_table.py \
    tests/unit/test_plan_025_failure_signal_extraction.py \
    tests/unit/test_plan_025_model_quarantine_state_machine.py \
    tests/unit/test_plan_025_effects_idempotency.py \
    tests/unit/test_plan_025_quarantine_hydration.py \
    tests/unit/test_plan_025_quarantine_cli.py \
    tests/integration/test_plan_025_error_isolation.py \
    tests/integration/test_plan_025_cross_provider_quarantine.py \
    tests/integration/test_plan_025_closure_evidence.py -v

# Plan 026 — Process-owned request finalization (runtime ownership token,
# finalization state machine, finalization supervisor, streaming
# cancellation hardening, startup reconciliation, shutdown drain)
uv run pytest \
    tests/unit/test_plan_026_runtime_ownership_token.py \
    tests/unit/test_plan_026_finalization_state_machine.py \
    tests/unit/test_plan_026_finalization_supervisor.py -v

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

# Phase 11 — Reload diagnostics outcome matrix (result categories, counters,
# stage accuracy, retirement status, snapshot, classify_result_category)
uv run pytest tests/unit/test_reload_diagnostics_matrix.py -v

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

# Integration tests (marker)
uv run pytest -m integration -v

# Reload tests (marker)
uv run pytest -m reload -v

# Network-dependent tests (marker)
uv run pytest -m network -v

# Soak tests (marker — short PR soak)
uv run pytest -m soak tests/soak/ -v

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

# Skip/xfail audit — must pass for CI soak-audit job
uv run python scripts/audit_xfail_skips.py

# Reload consistency audit (Plan 014 / Workstream G4)
uv run pytest tests/unit/test_audit_reload_consistency.py -v
uv run python scripts/audit_reload_consistency.py \
  --emit-snapshot-template

# Dispatch stability soak runner
uv run python scripts/run_dispatch_stability_soak.py \
  --profile balanced-file-backed \
  --mode smoke \
  --output artifacts/dispatch-soak/smoke

# Prepared-swap publication protocol tests (C1)
uv run pytest tests/unit/test_published_swap_protocol.py -v

# Plan 015 — Reload atomicity final closure (PendingGenerationSwap,
# lease gate, process-transition lifecycle, control socket hardening)
uv run pytest \
    tests/unit/test_control_server.py \
    tests/unit/test_reload_security.py \
    tests/unit/test_phase6_fault_injection.py \
    tests/unit/test_reload_diagnostics_matrix.py \
    tests/integration/reload/test_reload_fault_matrix.py -v

# Plan 016 — Reload atomicity corrective closure (pending-swap state
# machine, lease linearization, cancellation-shielded precommit abort,
# true SQLite COMMIT-bypass injection, peer-cred fail-closed, fact-based
# diagnostic flags)
uv run pytest \
    tests/unit/test_runtime_manager.py \
    tests/unit/test_process_transition_plan.py \
    tests/unit/test_control_server.py \
    tests/unit/test_reload_diagnostics_matrix.py \
    tests/unit/test_reload_manager.py \
    tests/unit/test_phase6_fault_injection.py \
    tests/unit/test_published_swap_protocol.py \
    tests/integration/reload/test_pending_swap_visibility.py \
    tests/integration/reload/test_sqlite_commit_failure.py \
    tests/integration/reload/test_plan_016_corrective_replacements.py \
    tests/integration/reload/test_reload_fault_matrix.py -v

# Plan 017 — Reload atomicity final corrective closure (lease condition,
# transition cleanup, acceptance finalization, commit-call recovery)
uv run pytest \
    tests/unit/test_runtime_manager.py \
    tests/unit/test_process_transition_plan.py \
    tests/unit/test_reload_manager.py \
    tests/unit/test_reload_diagnostics_matrix.py \
    tests/unit/test_db.py \
    tests/integration/reload/test_pending_swap_visibility.py \
    tests/integration/reload/test_sqlite_commit_failure.py \
    tests/integration/reload/test_reload_fault_matrix.py \
    tests/integration/reload/test_plan_017_lease_condition.py \
    tests/integration/reload/test_plan_017_transition_cleanup.py \
    tests/integration/reload/test_plan_017_acceptance_finalization.py \
    -v

# Plan 018/019/020 — Reload atomicity closure corrective pass + accepted-finalization lifecycle closure + control-flow evidence corrective pass
uv run pytest \
    tests/unit/test_runtime_manager.py \
    tests/unit/test_process_transition_plan.py \
    tests/unit/test_reload_manager.py \
    tests/unit/test_reload_diagnostics_matrix.py \
    tests/integration/reload/test_plan_017_lease_condition.py \
    tests/integration/reload/test_plan_018_transition_ownership.py \
    tests/integration/reload/test_plan_018_accepted_finalization.py \
    tests/integration/reload/test_plan_018_retirement_retry.py \
    tests/integration/reload/test_plan_018_database_commit_failure.py \
    tests/integration/reload/test_plan_018_gate_repair.py \
    tests/integration/reload/test_plan_019_finalization_retry.py \
    tests/integration/reload/test_plan_019_finalization_retention.py \
    tests/integration/reload/test_plan_019_shutdown_drain.py \
    tests/integration/reload/test_plan_019_acceptance_boundary.py \
    tests/integration/reload/test_plan_019_database_invalidation.py \
    tests/integration/reload/test_plan_019_transition_prefix.py \
    tests/integration/reload/test_plan_019_diagnostics_assertions.py \
    tests/integration/reload/test_plan_020_acceptance_window.py \
    tests/integration/reload/test_plan_020_single_flight.py \
    tests/integration/reload/test_plan_020_shutdown_transaction_ordering.py \
    tests/integration/reload/test_plan_020_production_transition_rollback.py \
    tests/integration/reload/test_plan_020_database_outcome_matrix.py \
    tests/integration/reload/test_plan_020_retention_close_counts.py \
    tests/integration/reload/test_plan_020_diagnostics_reconciliation.py \
    tests/integration/reload/test_pending_swap_visibility.py \
    tests/integration/reload/test_diagnostics_matrix.py \
    -v

# Control socket hardening tests (SO_PEERCRED, stale socket,
# inode protection, runtime dir permissions)
uv run pytest tests/unit/test_control_server.py tests/unit/test_reload_security.py -v

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
- **Process model**: supervisor + Granian worker (`workers=1`), PID file lifecycle, daemon mode (default for `eggpool serve`; `--verbose` for foreground). Default `runtime_threads=1` (single event-loop thread is canonical; values > 1 emit a startup warning), `database_worker_threads=2` (separate read-only stats connection). Readiness probe is process-owned and started after database initialization, stopped before database close.
- **Background tasks**: `src/eggpool/background/` manages retention cleanup, periodic tasks, and startup crash recovery via `TaskSupervisor`. Fixed-delay scheduler: next interval begins after previous tick completes. `initial_delay_s` consumed exactly once per task lifecycle. Process-owned tasks (`checkpoint`, `metrics_flush`, `update_checker`, `automatic_backup`) survive generation swaps; generation-leased tasks are retired when their generation is retired.
- **Runtime generations**: `RuntimeManager` owns active/retiring generation slots. Request-path code obtains `GenerationLease` via `wrap_stream_with_lease` or `leased_runtime`. `ProcessRuntime` holds process-owned containers (database connections) that outlive any generation. Lease acquisition is fail-closed: `RuntimeManager.acquire()` raises `RuntimeManagerLeaseExhaustedError` (→ HTTP 503) when no generation slot is accepting leases, rather than falling back to a legacy path. The request handler catches this and returns `503 Service Unavailable` immediately. During a staged reload swap, `RuntimeManager` uses a predicate-based lease gate (`_lease_condition` with `_lease_admission_gated` boolean): `acquire()` callers wait on the condition rather than polling, and the gate is released (condition notified) on commit or rollback. The `PendingGenerationSwap` protocol splits publication into explicit `stage()` → `commit()`/`rollback()` → `finalize_retirement()` boundaries, ensuring no request can acquire the candidate generation until the SQLite commit and required process transitions succeed.
- **Active-generation state authority (Phase 7)**: `RuntimeManager` is the sole authoritative source for active generation ID, config, digest, and generation-owned services. `active_metadata()` returns immutable generation identity for synchronous diagnostics. `snapshot_active_values()` returns a frozen `ActiveGenerationView` for short synchronous reads that need multiple generation-owned references. Request paths acquire a generation lease via `acquire()`. Production proxy request paths resolve generation-owned services from the leased generation, not from `app.state` mirrors. Readiness, dashboard, and stats routes read from the active generation via `get_active_generation(request)` helper. The compatibility mirror (`mirror_generation_on_app_state`) copies generation-owned services onto `app.state` for backward compatibility and is called at startup and after each reload publication.
- **Candidate resource ownership (Phase 4)**: `RuntimeGenerationCandidate` makes ownership of reload-created resources explicit from the moment each resource is constructed. Every generation-owned closeable is registered immediately after construction via `candidate.register_resource()`. Any failure before publication calls `candidate.abort()` which closes all registered resources in reverse registration order, collects close errors without masking the primary error, and emits `CleanupDiagnostics`. Pre-publication failures (reconcile, publish) also abort the candidate with `asyncio.shield()` so bounded cleanup completes even under task cancellation. `ReloadManager.snapshot()` surfaces `last_cleanup_diagnostics` for observability. Successful publication calls `candidate.transfer_to_runtime_manager()` to detach candidate cleanup. Ownership taxonomy: process-owned (database, coalescer, dispatch writer, routing trace writer, control server), generation-owned (client pool, outbound manager, DNS backend, supervisor), candidate-owned (resources during construction), request-owned (leases).
- **Shared runtime-generation factory (Phase 5)**: `RuntimeGenerationFactory` (`src/eggpool/generation_factory.py`) eliminates behavior drift between startup and reload by constructing all generation-owned services through a single authoritative path. Both startup and reload call `factory.prepare()`. The factory accepts process-owned dependencies as explicit inputs, constructs all generation-owned services with identical wiring, registers closeable resources on the candidate (reload case), hydrates persisted health/backoff state, and returns a `PreparedRuntimeGeneration`. Startup-only operations (migrations, crash recovery, catalog refresh, process workers) remain outside. **Dispatch writer enablement (Phase 8)**: the factory derives the coordinator's `use_dispatch_writer` from two conditions — the process-owned writer must be non-`None` *and* `config.dispatch_writer.enabled` must be `True`. Both must hold for the microbatch persistence path to be selected. Tests: `tests/unit/test_generation_factory.py`.
- **Transactional rehash (Phase 6)**: `ReloadTransaction` (`src/eggpool/reload_transaction.py`) introduces a monotonic state machine for the live-rehash transaction: `created → validated → diffed → candidate_prepared → persistence_prepared → process_transitions_prepared → commit_started → runtime_published → process_transitions_applied → persistence_committed → observable_state_updated → retirement_scheduled → completed`. The expanded `TransactionState` enum adds `PROCESS_TRANSITIONS_PREFLIGHTED`, `RUNTIME_STAGED`, `RUNTIME_SWAP_COMMITTED`, and `OWNERSHIP_TRANSFERRED` to distinguish staged, committed, and externally visible publication boundaries. Process transitions now execute inside the SQLite transaction (not after publication) so they roll back atomically on failure. `TransitionApplyResult` (`src/eggpool/reload_transaction.py`) tracks applied transitions for rollback and finalization lifecycle; `rollback_applied()` runs in reverse order and aggregates errors, while `finalize_all()` releases captured old-state snapshots after commit. `EffectiveStateTransition` uses a `_MISSING` sentinel to correctly distinguish absent attributes from `None` values during rollback. The `PendingGenerationSwap` protocol splits the coarse `install_candidate()` into explicit `stage()` → `commit()`/`rollback()` → `finalize_retirement()` boundaries. Commit ordering: The persistence delta, process transitions, and runtime publication all execute within a single SQLite transaction. The delta SQL is applied first, then process transitions, then the runtime pointer swap. If any step fails, the SQLite transaction rolls back automatically, leaving provider/account state identical to the pre-reload state. After the SQLite commit, observable state is updated and retirement is scheduled. Post-publication failures are compensated by accepting the new generation (the persistence delta is idempotent). Cancellation after publication is shielded to prevent mixed state. The `ReloadManager.active_transaction` property surfaces the current transaction for diagnostics.
- **Live configuration rehash**: `eggpool rehash` applies supported changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`. Field reload classification lives in `src/eggpool/config_reload_policy.py` (`_FIELD_DISPOSITION` map). LIVE fields include provider/account/routing/model-override families, `[transcoder]`, `[compression]`, `[cache]`, subset of `[models]`, and retention durations. Everything else is `RESTART_REQUIRED`. `eggpool rehash` JSON output is pinned at 9 keys. Reload admission uses an atomic claim primitive (`ReloadManager._claim_mutex` + `_reload_claimed`) that eliminates the TOCTOU race on concurrent reload attempts; the claim state is exposed via `ReloadManager.snapshot()` diagnostics. XDG_RUNTIME_DIR for socket path, strict JSON validation, fail-closed permissions, liveness probing before stale cleanup.
- **Health management**: `src/eggpool/health/` implements `HealthManager` circuit breaker and per-account health tracking for routing eligibility.
- **Readiness probe**: `DatabaseWritableProbe` (`src/eggpool/health/writable_probe.py`) is a process-owned background service that performs real SQLite write probes on a bounded cadence. The `/readyz` handler reads a cached snapshot instead of performing a write, eliminating routine write-lock contention from orchestrator polling. Configured via `[readiness_probe]` in config.toml.
- **Retry classification**: `src/eggpool/retry/` classifies upstream errors for failover and retry decisions.
- **Security**: `src/eggpool/security/` handles header redaction middleware and security utilities.
- **Integrations**: `src/eggpool/integrations/` generates external tool configs (OpenCode, Claude Code, Aider, Codex, etc.).
- **Safe compression**: `src/eggpool/transcoder/compression/` implements observe/safe compression, policy resolution, advisory tuning, and deterministic markers. Safe-mode `apply_safe_compression()` returns the original payload by identity on no-op.
- **Model info sources**: `src/eggpool/model_info/` enriches model metadata from multiple sources with tiered identity matching (6 tiers). Case-insensitive lookups at every layer.
- **Dispatch timing**: `LocalPreUpstreamRecorder` measures full EggPool-side window; `DispatchOverheadRecorder` covers coordinator-internal slice. Both use monotonic clocks.
- **Performance hot path**: `Router.build_routing_plan()` is the authoritative selection path (no fallback to legacy `select_accounts()`). `DispatchSpanRecorder` provides 200-sample dispatch span telemetry.
- **Plan 018 — Reload Atomicity Closure Corrective Pass**: correctness pass closing edge cases in transition ownership tracking, accepted-finalization idempotency, retirement retry safety, database commit failure recovery, and gate repair. New integration tests: `test_plan_018_transition_ownership.py`, `test_plan_018_accepted_finalization.py`, `test_plan_018_retirement_retry.py`, `test_plan_018_database_commit_failure.py`, `test_plan_018_gate_repair.py`.
- **Plan 019 — Accepted-Finalization Lifecycle Closure**: makes the Plan 018 finalization architecture truthful, retryable, bounded, and safe for long-running processes. Key changes: (1) progress/health separation — `AcceptedFinalizationStep` is the progress cursor; `AcceptedFinalizationHealth` records attempt outcome; only `COMPLETED` progress is terminal. (2) single-flight `run()` via `asyncio.Lock` — concurrent callers share one attempt. (3) transition-finalization outcome inspection — `TransitionFinalizationPendingError` blocks advancement when transitions remain. (4) retirement fault injection wired at the real production boundary. (5) bounded registry — active jobs dict + `deque(maxlen=32)` diagnostic history; completed jobs pruned and references released. (6) shutdown drain before `runtime_manager.shutdown()`. (7) defensive `_abort_precommit_reload` guard rejects accepted transactions. (8) `ReloadResult.finalization_status` field distinguishes accepted from fully finalized. (9) new counters: `accepted_reloads`, `fully_finalized_reloads`, `accepted_finalization_failures`, `accepted_finalization_retries`, `retirement_retry_count`.
- **Plan 020 — Accepted-Finalization Control-Flow Evidence Corrective Pass**: closes the structural seams the Plan 019 architecture left open, making accepted-finalization lifecycle bound, reconcilable, and faithfully observable. Key changes: (1) genuine single-flight via retained `asyncio.Task` — `run()` spawns one task under `_run_lock` and callers `await asyncio.shield(task)`; cancel/timeout cannot cancel the retained task. (2) `run()` returns `AcceptedFinalizationOutcome` (with `completed`, `next_step`, `error`, `failure_count`, `retry_attempt_count`, `retirement_retry_attempt_count`, `retirement_scheduled`, `adopted_for_shutdown`) rather than `AcceptedFinalizationStep`, decoupling the public contract from the internal progress cursor. (3) `FinalizationStatus` enum (`COMPLETED` / `RETRY_PENDING` / `RETIREMENT_SCHEDULE_FAILED` / `SHUTDOWN_ADOPTED` / `INVARIANT_FAILED`) supersedes step-as-status; `is_complete` only true for `COMPLETED`. (4) `AcceptedFinalizationInvariantError` raised when an unknown step appears in the cursor (no silent swallow). (5) active error fields cleared on success — once a job recovers, the previous error is no longer reported. (6) `adopt_for_shutdown()` lets the lifespan take ownership of unresolved jobs before shutdown; `drain_finalization_jobs` shields retained tasks so cancel does not cancel them. (7) Counter reconciliation via `_reconcile_finalization_job()` with delta tracking per job (`_reconciled_attempt_count`, `_retirement_retry_accounted`, `mark_reconciled()`) — `accepted_reloads`/`committed_reloads` increment inline at accept; `fully_finalized_reloads`/`accepted_finalization_failures_recovered`/`delayed_completion_count` advance only via reconcile (idempotent). (8) `ReloadDiagnosticResult` gains 12 finalization fields (`finalization_status`, `finalization_active_count`, `finalization_history_count`, `finalization_failure_count`, `finalization_retry_attempt_count`, `finalization_retirement_retry_attempt_count`, `finalization_last_error_step`, `finalization_last_error_message`, `finalization_pending_jobs`, `pending_swap_committed`, `accepted_generation_authoritative`, `ownership_diagnostics`) and `classify_result_category()` returns `POST_COMMIT_FINALIZATION_PENDING`/`RETIREMENT_SCHEDULE_FAILED` categories. (9) `ReloadResult` gains matching finalization fields and the control server response carries them. (10) Ownership fallback normalized to lowercase (`"transferred"`/`"aborted"`). (11) Shutdown sequence: `wait_for_transaction_completion` → `drain_finalization_jobs` (shielded) → `adopt_for_shutdown` for unresolved jobs (none are silently dropped). Tests: `tests/unit/test_accepted_finalization_state_machine.py` + 7 `tests/integration/reload/test_plan_020_*.py` files (40 tests).

- **Plan 023 — Error-Isolation Reproducer and Invariant Baseline**: Phase 1 of the upstream error isolation roadmap. Observational, test-infrastructure focused — no production behavior changes. Provides: (1) `MockUpstream` (`tests/helpers/mock_upstream.py`) — `respx`-backed mock upstream with declarative `MockResponseSpec`, `MockUpstreamRule` matching, `CapturedRequest` structured log, and 9 MiniMax-M3 scenario presets. (2) Canonical request fixtures (`tests/helpers/request_fixtures.py`) — 18+ immutable payloads covering every thinking-control variant. (3) `RequestStateAuditSnapshot` (`tests/support/state_audit.py`) — captures `DurableFacts` + `RuntimeFacts` before/after requests; `StateAuditDiff` with 5 categories; `is_clean` detects leaks. (4) `CancellationSeamRegistry` (`tests/support/cancellation_seams.py`) — 11 named `CancellationPoint` entries firing `CancelledError` exactly once. (5) Database fault seams via `Database` class-level injection hooks (`TEST_INJECT_BEFORE_COMMIT_CALL`, `set_test_inject_commit_call`, etc.). (6) `JSONOperationCounters` (`tests/support/json_counters.py`) — monkey-patches `jsonx.loads`/`dumps_bytes` with 6 category counters. (7) Performance baselines (`tests/perf/test_plan_023_request_path_baseline.py`, `tests/soak/test_plan_023_error_isolation_baseline.py`). Baseline artifact: `artifacts/plan-023-baseline.md`. Later phases (024–030) depend on this infrastructure and must keep Plan 023 tests green.

- **Plan 024 — Provider-Bound Thinking-Control Normalization**: adds an explicit provider-bound request-contract layer so thinking/reasoning controls are validated and normalized after provider/account selection, regardless of whether protocol transcoding is required. Key changes: (1) `ThinkingControlContract` (`src/eggpool/catalog/capabilities.py`) — structured contract with `mode` (`unknown`/`none`/`fixed`/`effort`/`budget`/`effort_or_budget`), `request_fields`, `accepted_efforts`, `effort_aliases`, `effort_to_budget_tokens`, `explicit_budget_min`/`max`, `historical_reasoning_content`, and `source`. (2) `ThinkingRequestIntent` — immutable normalized record of original client thinking intent stored in `ProxyRequestContext.thinking_intent`, preventing intermediate translations from becoming falsely authoritative. (3) `ProviderRequestAdaptation` (`src/eggpool/transcoder/provider_adaptation.py`) — typed pure result with `payload`, `changed`, `decision` (`passthrough`/`mapped`/`dropped`/`rejected`), `requested_controls`, `emitted_controls`, `warnings`. (4) `adapt_thinking_controls()` — pure adaptation function that validates/normalizes controls against the provider contract. (5) `_adapt_provider_thinking_controls()` in coordinator — post-selection normalization stage that runs for both native and transcoded paths, before upstream dispatch. (6) Built-in contracts (`src/eggpool/transcoder/builtin_contracts.py`) — manually curated contracts for OpenCode Go MiniMax-M3 (fixed), MiniMax native (effort), Anthropic native (effort_or_budget), and OpenAI native (effort). (7) `ProviderControlPolicyConfig` (`src/eggpool/transcoder/policy.py`) — `[transcoder.provider_control_policy]` config section with `unsupported_control`, `unknown_contract`, and `allow_compatibility_retry`. (8) Provider control adaptation counters (`provider_mapped`/`provider_dropped`/`provider_rejected`) in `ThinkingMetricsCounter`. (9) Thinking trace extended with `provider_control_decision` and `provider_control_warnings`. Tests: `tests/unit/test_plan_024_*.py` (51 tests).

- **Plan 025 — Typed Failure Effects and Bounded Model Quarantine**: centralizes the consequences of request and upstream failures into one typed, test-pinned decision. Replaces first-observation indefinite model withdrawal with bounded, provider/account/model/protocol-scoped quarantine that requires corroboration before becoming terminal and automatically clears on recovery. Key changes: (1) `FailureObservation` (`src/eggpool/failure/observation.py`) — immutable input record with source, status_code, error_class, provider/account/model scope, response_signal, retry_after, and response_started. (2) `FailureEffects` (`src/eggpool/failure/effects.py`) — immutable decision output with retry, retry_scope, client_outcome, account_effect, model_effect, circuit_penalty, persist_backoff, backoff_reason/until, release_probe_only, and evidence_class. (3) `classify_failure_effects()` (`src/eggpool/failure/classifier.py`) — single pure classifier with table-driven decision logic covering every status/body/error-class matrix row. Unknown validation defaults to zero shared-state effects. (4) `ModelQuarantine` (`src/eggpool/failure/quarantine.py`) — state machine (`healthy → suspected → quarantined → terminal_withdrawn`) keyed by (provider_id, account_id, canonical_model_id, upstream_model_id, upstream_protocol). First observation creates bounded suspected TTL; repeated equivalent evidence promotes; expiry restores eligibility; success clears exact key; catalog reappearance clears; terminal withdrawal only from authoritative sources. (5) `EffectsApplier` (`src/eggpool/failure/applier.py`) — applies effects exactly once per attempt via idempotency key; guards against double-penalization from retried finalization. (6) `extract_failure_signal()` (`src/eggpool/failure/signal_extract.py`) — bounded conservative signal extraction from response bodies (quota, rate-limit, auth, model-absent, context-limit, unsupported-control). (7) Durable schema: `0051_model_quarantine.sql` with scope key, state, provenance, observation count, expiry, and clear tracking. Tests: 9 test files covering the full effects matrix, signal extraction, state machine, hydration, idempotency, error isolation, cross-provider quarantine, operator diagnostics, and closure evidence (end-to-end pipeline verification).

- **Plan 026 — Process-Owned Request Finalization**: makes selected-attempt cleanup independent of the client request task. Once EggPool has durably created a request, attempt, or reservation and claimed runtime ownership, one retained process-owned finalization job must own terminal reconciliation until every durable and in-memory obligation has either completed or entered a bounded, observable retry state. Key changes: (1) `FinalizationIdentity` (`src/eggpool/request/finalization_job.py`) — immutable frozen dataclass containing all data needed to finalize without querying mutable request context. (2) `FinalizationProgress` — progress state machine (`created → durable_finalization_pending → durable_finalized → runtime_release_pending → runtime_released → analytics_pending → completed`); only `completed` is terminal. (3) `AttemptRuntimeLease` — idempotent runtime ownership token tracking active-count, quota-reservation, and health-probe acquisition/release facts. (4) `RequestFinalizationJob` — process-owned job with retained `asyncio.Task`, single-flight `run()` via `asyncio.shield`, concurrent-caller sharing, and completion callback. (5) `RequestFinalizationSupervisor` — bounded, deduplicated registry of active jobs with process-owned completion reconciliation, bounded history deque (scalar-only records), startup stale-state reconciliation, and shutdown drain/adopt. (6) Streaming cancellation integration — the coordinator registers a finalization job before the inner stream generator; on `CancelledError`, the retained task owns finalization even when every request waiter is cancelled, replacing the fragile `asyncio.wait_for(asyncio.shield(...), timeout=10)` pattern. (7) `RuntimeGeneration` gains `finalization_supervisor` field wired through `RuntimeGenerationFactory`. Tests: `tests/unit/test_plan_026_runtime_ownership_token.py`, `tests/unit/test_plan_026_finalization_state_machine.py`, `tests/unit/test_plan_026_finalization_supervisor.py`.

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
- **`app.state` generation-owned attributes are mirrors, not authority**: The active generation (via `RuntimeManager.active_snapshot()`) is the authoritative source. `app.state` mirrors are updated at startup and after each reload publication via `mirror_generation_on_app_state()`. New code should use `get_active_generation(request)` or acquire a lease instead of reading generation-owned `app.state` attributes directly.
- **Pricing safeguard**: ambiguous bare upstream pricing defaults to dollars-per-million. `RequestFinalizer` never floors canonical cost to reservation. Use `eggpool stats repair-costs` for historical cleanup (dry-run first).
- **When constructing `RequestCoordinator` in tests**: pass an explicit `transcoder_policy` or assert the desired default; never rely on implicit `None`.
- **`CapabilityError` (400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)**: `BudgetResolutionError` is a subclass of `CapabilityError`.
- **DB migrations are numbered SQL files** in `src/eggpool/db/schema/`. The `model_info_*` sidecar tables carry FKs to `models.model_id`; repository writes seed a placeholder `models` row in the same transaction.
- **`reload_in_progress` exits with code 4** (`EXIT_RELOAD_BUSY`). Use the constant from `cli_exit_codes.py`, not the literal string.
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound. `MetricsWriteCoalescer` is the only component using `threading.Lock` for cross-thread safety.
- **`/readyz` never performs a write**: The readiness endpoint reads a cached probe snapshot from the process-owned `DatabaseWritableProbe`. The probe executes real write transactions on a bounded cadence (default 10s) and caches the result. This avoids SQLite write-lock contention from frequent orchestrator polling.
- **Readiness probe is process-owned**: The probe survives generation swaps (rehash). It must not be recreated or interrupted during reload.
- **Process transitions execute inside `db.transaction()`**: The commit path applies provider/account SQL, process transitions, and runtime publication all within a single SQLite transaction. If any step fails, the entire transaction rolls back atomically — no partial state is visible. On commit failure, `TransitionApplyResult.rollback_applied()` undoes process transitions in reverse order before the DB layer rolls back.

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
