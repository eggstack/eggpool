# Changelog

All notable changes to EggPool are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Model-info enrichment lifecycle correction.** Model-info startup now runs
  one bounded external pass, and the existing generation-leased
  `catalog_refresh` event now performs bounded due enrichment on every
  opportunity. Both runtime callback-construction paths share the same leased
  generation helper; source failures remain isolated from catalog discovery
  and routing. The legacy `model_info.refresh_interval_s` field is retained
  for compatibility but is deprecated and no longer represents a scheduler.
- **Plan 159 reasoning capability source precedence.** Removed static
  OpenCode Go reasoning seeds and model-family effort inference. Live
  provider metadata, verified provider-scoped model-info metadata, and
  operator overrides now merge by explicit fact with truthful provenance;
  missing reasoning controls remain unknown.
- **Plan 085 runtime closure.** The lean profile was compared with the
  planning-baseline SBC-shaped profile using the existing runtime snapshot and
  short manual process measurements. The comparison confirmed fewer worker
  threads, optional background work, DNS state, and outbound sockets on the
  final profile; workstation RSS was noisy and no Raspberry Pi claim is made.
  The manual performance harness now installs the canonical generation-owned
  finalization supervisor instead of relying on the removed direct fallback.
- **Plan 084 legacy-path and CI pruning.** Requests now require a published
  runtime generation and the canonical finalization supervisor. The provider
  bound request is the sole provider-payload authority; the unreleased
  database recovery compatibility surface and selection-claim state-machine
  module were removed. The Granian `pname` extra remains because the serve
  path uses process naming; the base package was tested and rejected. CI and
  development dependencies are split, and documentation-only workflow
  changes are filtered from CI.
- **Routing-failure isolation hardening.** A single request's failure path can
  no longer poison routing for unrelated models. `EffectsApplier.apply_once`
  marks each effect-step flag *before* invoking the side-effecting function
  and wraps every step in a bounded `except`, so an exception in one step
  cannot leave the applier in a half-applied state on retry and cannot
  prevent the remaining steps from running. `ModelCatalogCache.mark_model_unavailable`
  short-circuits when the (account, model) pair is not present, so a
  misclassified call cannot widen its blast radius. Tests:
  `tests/unit/test_routing_isolation.py` cover scoped model quarantine, the
  no-account-disable path for keyword false-positives in client-error bodies,
  5xx cooldown non-extension to siblings, and applier resilience under partial
  side-effect failure.
- **muse-spark-1.2-contributor routing fix.** Three coordinated changes
  restore the canonical ``OpenCode Go`` Muse Spark request path and stop
  one model's failure from black-holing sibling models on the same
  subscription:

  1. `ProviderBoundRequest.set_provider_payload` and
     `adopt_provider_payload` now clear the dispatch-freeze flag when a new
     generation begins. Pre-fix, the post-selection transcoder's
     `adopt_provider_payload` raised `RuntimeError: provider payload is
     frozen` after the first attempt's serialization, breaking every retry
     against a different selected provider.

  2. `RequestCoordinator._determine_thinking_rejection_status` now
     aggregates per-provider capability overrides when the collapsed
     entry reports `unknown`. Provider-scoped capability metadata (for
     example, the canonical OpenCode Go capability for
     `muse-spark-1.2-contributor`) lives in the provider-scoped cache row,
     so the previous bare-model lookup missed it and surfaced a misleading
     client-validation 400. The
     capability-error path now requires `rejected_status` to be truly
     `unknown`/`unsupported` before surfacing the 400; otherwise the
     router falls through to a transient `No accounts available` (503).

  3. `EffectsApplier._apply_account_effect` now skips
     `HealthManager.record_failure` when the classifier set
     `model_effect="quarantine"` — the per-model disable is the only
     shared-state penalty in that case. Pre-fix, a single muse-spark 5xx
     per account advanced the account-wide circuit breaker so five
     per-model 5xxes tripped the breaker for ALL models on the same
     subscription. Post-fix, sibling models on the same opencode-go
     account keep routing while only muse-spark-1.2-contributor is
     quarantined for the bounded TTL. Tests:
  `tests/integration/test_muse_spark_live_e2e.py` (3 tests covering the
  end-to-end live routing path with thinking controls and sibling-model
  isolation), `tests/integration/test_muse_spark_capability_status_attribution.py`
  (catalog + status attribution), and `tests/unit/test_provider_bound_request.py`
  + `tests/unit/test_effects_idempotency.py` (per-fix unit coverage).

### Added

- **PendingGenerationSwap staged-swap protocol for atomic reload publication.** `PendingGenerationSwap` (`src/eggpool/runtime_manager.py`) splits the coarse `install_candidate()` into explicit `stage()` → `commit()`/`rollback()` → `finalize_retirement()` boundaries. The staged swap gates lease admission so no request can acquire the candidate generation until the SQLite commit and required process transitions succeed. On rollback, the old generation is restored and lease admission reopens atomically. Diagnostics expose `staged`, `candidate_generation_id`, `old_generation_id`, and `stage_started_at`. Tests: `tests/unit/test_control_server.py`, `tests/unit/test_reload_security.py`.
- **Event-driven lease gate replacing polling in `RuntimeManager.acquire()`.** During a staged swap, `RuntimeManager._lease_gate_event` blocks new `acquire()` callers on an `asyncio.Event` rather than polling. The gate is released (event set) on commit or rollback. Shutdown wakes blocked acquisitions and returns the controlled 503 path.
- **Process transitions now execute inside the SQLite transaction.** The commit path in `ReloadManager` applies provider/account SQL, process transitions, and runtime publication all within a single `db.transaction()`. If any step fails, the entire transaction rolls back atomically — no partial state is visible. On commit failure, `TransitionApplyResult.rollback_applied()` undoes process transitions in reverse order before the DB layer rolls back.
- **`TransitionApplyResult` for proper rollback/finalization lifecycle.** `TransitionApplyResult` (`src/eggpool/reload_transaction.py`) tracks applied transitions, supports `apply_all()`, `rollback_applied()` (reverse-order rollback with error aggregation), and `finalize_all()` (post-commit cleanup). `preflight_all_transitions()` runs preflight on every transition in declared order without mutating state.
- **`EffectiveStateTransition` sentinel-based rollback.** `EffectiveStateTransition` uses a `_MISSING` sentinel to distinguish three rollback cases per attribute: attribute absent before preflight → delete it; attribute present with `None` → set to `None`; attribute present with a real value → restore it. Correctly handles `app.state` compatibility mirrors.
- **Expanded `TransactionState` enum with staged-swap states.** `TransactionState` adds `PROCESS_TRANSITIONS_PREFLIGHTED`, `RUNTIME_STAGED`, `RUNTIME_SWAP_COMMITTED`, and `OWNERSHIP_TRANSFERRED` to distinguish staged, committed, and externally visible publication boundaries. The state machine now tracks the full lifecycle of the staged-swap protocol.
- **Control socket hardening: SO_PEERCRED buffer validation, fail-closed stale socket classification, inode replacement protection, runtime directory permission verification.** `_reject_unmatched_peer_uid()` validates the complete `struct ucred` buffer and rejects mismatched UIDs on Linux; `_clean_stale_socket()` uses inode identity checks (device+inode captured before and after probe) to prevent pathname replacement races; `_restrict_socket_permissions()` verifies 0o600 mode after setting; `_verify_runtime_dir()` enforces 0o700 mode and correct UID ownership. Tests: `tests/unit/test_control_server.py`, `tests/unit/test_reload_security.py`.
- **Live configuration rehash** (`eggpool rehash`): control socket, reload
  manager, atomic generation swap, and non-disruptive old-generation retirement.
  All configuration fields remain `RESTART_REQUIRED` for now; the
  infrastructure is ready for future `LIVE` field classification.
- **Live configuration rehash closure pass (Phases 1-5).** The first
  deliberately bounded set of configuration paths is now `LIVE` so
  `eggpool rehash` actually applies supported changes to a running process.
  The `[providers]`, `[accounts]`, `[model_overrides]`, `[model_capabilities]`,
  and all `[routing]` fields support live rehash; the closure-pass diff
  algorithm inherits the parent collection's `LIVE` disposition for expanded
  per-key paths (`providers.<id>`, `accounts.<provider>/<name>`,
  `model_overrides.<id>`, `model_capabilities.<id>`) so adding a new provider
  through `rehash` publishes a generation rather than rejecting with
  `restart_required`. Every other field stays `RESTART_REQUIRED` until a
  live replacement path is added in a separate diff; the unit test
  `test_live_field_inventory_matches_expected` pins the actual set of `LIVE`
  paths and fails closed on accidental expansion. Initial startup and
  candidate generation construction now share a single
  `register_runtime_tasks()` helper (`src/eggpool/runtime_tasks.py`) so the
  process-level E2E test
  (`tests/integration/test_rehash_streaming_swap.py`) can prove streaming
  generation swap end-to-end with a real `eggpool serve` subprocess, a
  mock upstream, and `eggpool --config X rehash` against the control socket.
  Stable exit codes (`src/eggpool/cli_exit_codes.py`) make the `rehash`
  command scriptable (0=ok, 1=validation, 2=restart-required,
  3=control-unavailable, 4=busy, 5=prep-failed, 6=digest-mismatch); `--json`
  output is supported. `eggpool accounts connect` and `eggpool accounts
  logout` route through the same helper via
  `providers.connect.apply_or_restart` so they apply `LIVE` changes when
  the server is running and fall back to `restart_server()` only when the
  control socket is unreachable. Documentation in
  `docs/live-config-rehash.md`, `docs/deployment.md`, `architecture/README.md`,
  `AGENTS.md`, and `.opencode/skills/architecture/SKILL.md` updated. See
  `plans/2026-07-13-live-config-rehash-closure-plan.md`.
- **Live config rehash polish pass (workstreams 1-5).** Observational
  E2E tests now prove config digest + credential fingerprint changes;
  provider removal is end-to-end tested; deterministic concurrency test
  seam (`preparation_event` hook on `ReloadManager`); safe `connect`/`logout`
  fallback via `resolve_apply_outcome()` that never silently restarts a
  healthy server; standardized 9-key `--json` contract
  (`cli_rehash_format.py`); busy stage (`reload_in_progress`) now exits 4;
  secret-safe diagnostics. The LIVE field inventory is unchanged.
- **Live config rehash closure pass D1 (request-policy expansion).**
  The request-path policy fields the candidate builder already constructs
  as generation-owned objects are now classified `LIVE` so `eggpool rehash`
  publishes the new policy without restarting. Concretely: the entire
  `[transcoder]` block, the entire `[compression]` block (including
  `[compression.synthetic_cache_controls]`), and the entire `[cache]`
  block (including `[cache.synthetic_cache_controls]`) are `LIVE`;
  the runtime-tunable subset of `[models]` (`expose_mode`,
  `collapse_models`, `refresh_interval_s`, `stale_after_s`,
  `allow_stale_catalog`) is `LIVE`; and
  `security.persist_redacted_error_detail` is `LIVE`. The
  `_disposition_for()` helper inherits `LIVE` from `transcoder`,
  `compression`, `cache`, and `models` for the expanded per-key paths
  so adding a new field through `rehash` publishes a generation
  rather than rejecting with `restart_required`. Startup-only fields
  in `[models]` (`startup_refresh`, `ping_retain_days`,
  `catalog_withdrawal_policy`), the entire `[upstream]` block, and the
  entire `[model_info]` block stay `RESTART_REQUIRED`. The new
  inventory is pinned by `test_live_field_inventory_matches_expected`
  and the new `test_request_policy_sub_paths_inherit_live` test in
  `tests/unit/test_config_reload_policy.py`. Identity separation
  between active and candidate policies is pinned by
  `TestMilestoneD1CandidateBuild` in `tests/unit/test_reload_manager.py`,
  and end-to-end behavioral reload for each new family is pinned by
  the seven `test_d1_*` tests in
  `tests/integration/test_rehash_streaming_swap.py` (transcoder loss
  policy, compression enabled/disabled, cache synthetic controls,
  models collapse/expose, prefer-native toggle, persist-redacted-
  error-detail, and old-stream/new-request split semantics). A new
  repeated-reload soak test (`TestMilestoneD1RepeatedReloadSoak`)
  exercises 25 alternating loss-policy reloads and asserts no
  retiring-slot leaks and per-generation identity separation.
Documentation in `architecture/README.md`, `docs/live-config-rehash.md`,
   and `AGENTS.md` updated. See
   `plans/2026-07-14-live-config-rehash-final-milestone-d1-request-policy-expansion.md`.
- **Dispatch stability milestone A** (`plans/2026-07-15-dispatch-stability-milestone-a-scheduler-and-baseline.md`).
  Four coordinated fixes: (A1) `SupervisedTask._run_periodic_loop()` now
  resolves the initial delay ONCE before the loop and switches to
  `interval_s` after the first tick — previously `initial_delay_s` was
  re-applied on every iteration, so tasks with a short `initial_delay_s`
  and longer `interval_s` ticked at the initial-delay cadence forever.
  (A3) `SupervisedTask.snapshot()` exposes new cadence diagnostics —
  `configured_interval_s`, `configured_initial_delay_s`,
  `initial_delay_consumed` (true after the one-time initial delay is
  waited out), `previous_tick_started_at`, `observed_last_interval_s`
  (time between the last two tick starts), `last_tick_drift_s` (actual
  tick start minus scheduled tick start), and `tick_in_progress` —
  surfaced under `/api/stats/runtime`. (A4) New `LocalPreUpstreamRecorder`
  in `src/eggpool/runtime_dispatch.py` measures the full EggPool-side
  window from ASGI handler entry (`request_received_monotonic_ns`,
  captured at the top of `handle_proxy_request`) to just before
  `httpx.AsyncClient.send()`; distinct from the existing
  `DispatchOverheadRecorder`, which covers only the coordinator-internal
  slice from `ProxyRequestContext.started_monotonic_ns`. Exposed via
  `runtime_metrics.local_pre_upstream`. (A5) A deterministic manual dispatch
  diagnostic exercises serial and
  concurrent native dispatches, cancellation under load, database
  contention snapshot shape, background-task cadence drift, and runtime
  metrics surface — pinned under the `performance` marker so it can be
  re-run without slowing the ordinary suite. (A6) New
  `_log_operational_profile()` in `src/eggpool/app.py` emits a single
  structured startup log (`Operational profile: ...`) with workers,
  runtime_threads, database_worker_threads, stats_db_separate,
  WAL/synchronous/busy_timeout, routing_trace_mode/sample_rate,
  metrics_write_mode/flush_interval_s, transcoder/compression/cache
  enabled flags, and background task counts split by ownership
  (`task_total`, `task_process_owned`, `task_generation_leased`). The
  scheduler is fixed-delay: the next interval begins after the previous
  tick completes, preventing overlapping ticks. A failing first tick
  does not reset the initial-delay state; only `stop()`/`start()`
  reapplies it. Cadence semantics and inventory are pinned by
  `tests/unit/test_periodic_cadence.py` (13 tests),
  `tests/unit/test_dispatch_timing_boundaries.py` (6 tests),
  `tests/unit/test_operational_profile.py` (3 tests), and the existing
  `tests/unit/test_runtime_task_inventory.py` (35 tests). Documentation
  in `architecture/README.md`, `AGENTS.md`, `.opencode/skills/architecture/SKILL.md`,
  `.opencode/skills/development/SKILL.md`, `docs/deployment.md`,
  `docs/raspberry-pi.md`, and `README.md` updated.
- **Dispatch stability milestone B (selection-claim lock deconvoying).**
  `RequestCoordinator._select_and_persist_attempt` now splits selection
  into three phases around a new `RequestCoordinator._selection_claim_lock`:
  Phase A claims an account and resolves identity under the lock, Phase B
  persists the durable rows (`_persist_dispatch_bundle`) OUTSIDE the lock
  so a SQLite waiter can no longer convoy other selectors, and Phase C
  re-acquires the lock for runtime publication
  (`_publish_runtime_state`). Compensation on Phase C failure goes
  through `_compensate_or_rollback_claim`, which decrements the active
  count, finalizes the attempt as `PostCommitInterrupted`, releases the
  circuit-breaker health slot, and tags the context with
  `post_commit_interrupted`. The plan's acceptance criterion #1 (no DB op
  under the lock) is pinned by
  `tests/unit/test_coordinator_claim_lock_scope.py::test_db_io_runs_outside_selection_claim_lock`,
  which instruments `Database.transaction()` and asserts the transaction
  window never overlaps the lock-held window. The plan's acceptance
  criterion #6 (release exactly once) is pinned by
  `tests/unit/test_selection_claim.py::test_release_health_slot_only_once`,
  `test_release_health_slot_with_no_holder_is_no_op`, and
  `test_release_health_slot_with_none_health_manager_is_safe`. A new
  frozen `SelectionClaim` dataclass and `SelectionClaimTracker` state
  machine (`PLAN -> CLAIM -> PERSIST -> COMMITTED -> PUBLISHED`, with
  `ROLLED_BACK` as a terminal divert) live in
  `src/eggpool/request/selection_claim.py`; process-local counters live on
  `SelectionClaimDiagnostics` and surface under `/api/stats/runtime`
  `selection_claims` (`claims_created`, `claims_committed`,
  `claims_published`, `claims_rolled_back_before_persistence`,
  `ambiguous_commit_reconciliations`,
  `post_commit_publication_failures`,
  `compensation_successes` / `compensation_failures`,
  `max_concurrent_claims`, and `claim_lock_wait_overflows` /
  `claim_lock_wait_recent`). Nine new span keys are registered
  (`SPAN_SELECTION_CLAIM_WAIT`, `SPAN_SELECTION_CLAIM_HELD`,
  `SPAN_SELECTION_REVALIDATION`, `SPAN_DISPATCH_PERSISTENCE_WAIT`,
  `SPAN_DISPATCH_PERSISTENCE_TRANSACTION`,
  `SPAN_DISPATCH_PERSISTENCE_COMMIT`, `SPAN_POST_COMMIT_PUBLICATION`,
  `SPAN_CLAIM_ROLLBACK`, `SPAN_POST_COMMIT_COMPENSATION`). The legacy
  `_select_lock` is retained as a back-stop and is unused by the
  post-milestone-B hot path. Test coverage spans
  `tests/unit/test_selection_claim.py` (16 unit tests),
  `tests/unit/test_selection_claim_diagnostics.py` (10 unit tests), and
  `tests/unit/test_coordinator_claim_lock_scope.py` (4 integration
  tests). Documentation in `architecture/README.md` § Lock scope and
  publish ordering and `AGENTS.md` updated.

### Dispatch Stability Milestone G — Soak Validation, Rollout, and Operational Closure

- Added `ConsistencyAuditor` (`src/eggpool/db/consistency_audit.py`) for read-only database lifecycle invariant checks
- Added soak test suite (`tests/soak/`) with eight canonical workload profiles
- Added early/late window stability ratio gates (dispatch p95 ≤ 1.20x, p99 ≤ 1.50x)
- Added resource plateau validation (RSS, thread count, reservation cleanup)
- Added configuration profiles documentation (`docs/config-profiles.md`)
- Added operator runbook for dispatch stability diagnostics (`docs/operations/dispatch-stability.md`)
- **Runtime-thread policy correction.** `server.threads` defaults to `1`
  (single event-loop thread is canonical). Values > 1 emit a startup
  warning; the supported default is `threads=1`. Documentation across
  `architecture/README.md`, `AGENTS.md`, and operator runbooks updated to
  reflect the corrected default. The `threads=4` guidance was
  retired — `asyncio.Lock` primitives are loop-bound and multi-loop
  access is unsupported without operator verification.
- **Slow-writer burst fairness tests** (`tests/unit/test_slow_writer_burst_fairness.py`).
  Validates that dispatch latency remains bounded when concurrent slow
  upstream writers share the dispatch pipeline. Part of the dispatch
  stability closure pass.
- **Extended soak runner** (`scripts/run_dispatch_stability_soak.py`).
  Canonical long-running dispatch stability validation with `smoke`,
  `extended`, and `ci` modes, configurable workload profiles, and
  artifact output (stability gates, resource plateau, DB consistency).
- **Extended stability gates** (`tests/soak/test_extended_stability_gates.py`).
  Long-duration validation of dispatch latency stability, resource
  plateau, and database consistency under sustained load. Gated behind
  the `extended_soak` pytest marker.
- **Dispatch stability closure pass.** Documentation and operational
  claims corrected across `AGENTS.md`, `architecture/README.md`,
  `docs/operations/dispatch-stability.md`, and `CHANGELOG.md`. Test
  command inventory updated with slow-writer, extended soak, and soak
  runner commands.

### Fixed

- **Provider-suffixed model ids no longer leak the ``/provider-id``
  suffix to upstream.** ``/v1/models`` exposes provider-scoped entries in
  the form ``<model-id>/<provider-id>``. When a client used one of those
  suffixed ids against an endpoint whose body is transcoded or rewritten
  -- ``/v1/chat/completions`` for an Anthropic model, or any cross-
  protocol retry -- the suffix leaked into the JSON body the upstream
  received, which rejected the request with ``Model <model-id>/<provider-id>
  is not supported``. The proxy layer now normalizes the in-memory
  payload's ``model`` field to the parsed base id at every point where a
  body is rebuilt for upstream dispatch: the transcode preflight cache
  (`handle_proxy_request` in `src/eggpool/api/proxy_request.py`), the
  coordinator's re-translation reset
  (`_apply_selected_provider_transcode` in
  `src/eggpool/request/coordinator.py`), and the legacy provider-request
  adapter (`RequestCoordinator._legacy_provider_request`). The
  immutable ``client_payload`` contract is preserved -- only the
  provider-bound snapshot loses the suffix. Regression coverage is
  parameterized across multiple model ids in
  `tests/unit/test_prepared_transcode.py` (`TestCoordinatorResetStripsProviderSuffix`)
  so the fix is model-agnostic and future model names are covered by
  default.

## [0.6.0] - 2026-07-09

### Fixed

- **Python hot-path corrective polish.** Closure pass over the Phase 1-5 landing: selection lock wait/held spans (`selection_lock_wait`, `selection_locked`) now record exactly one sample per attempt via explicit `record_ns()` calls after the lock exits — the placeholder `_maybe_span` blocks inside `_select_and_persist_attempt()` were double-sampling these metrics, depressing p50/p95 and masking real concurrency contention. `CompressionResult` now carries applier-derived `candidate_count`, `eligible_candidate_count`, `suppressed_candidate_count`, and `applied_transform_count` so safe-mode observation counts meaningful candidates without a second observe pass. Safe-mode `apply_safe_compression()` remains a single pass: no-op returns the original payload by identity, applied runs return a path-level copy-on-write payload. `_copy_with_replacements()` receives direct unit coverage for no-op identity, single dict/list paths, multiple shared-prefix and disjoint-branch replacements, duplicate-path last-wins, invalid-path fail-closed, chained transforms, and strict structural-sharing identity of untouched branches. `/api/stats/runtime` `dispatch_spans` is rendered on the runtime dashboard as a `Dispatch spans` panel showing actionable spans (`coordinator_pre_upstream`, `segmentation`, `compression_analyze`, `compression_apply`, `selection_lock_wait`, `selection_locked`, `routing_trace_write`) with p50/p95/max plus `sample_count`; absent spans render as "not observed in recent window" instead of `0 ms`. Behavioral regression guards land in `tests/unit/test_hotpath_corrective_polish.py` and `tests/unit/test_runtime_dispatch_spans_dashboard.py` so an accidental reintroduction of duplicate safe-mode analyze, unconditional deep-copy, double lock-span sampling, or zero-filled dashboard spans is caught at unit-test time. Documentation in `architecture/README.md`, `AGENTS.md`, `.opencode/skills/architecture/SKILL.md`, and `README.md` updated to reflect the corrected copy-on-write semantics. Migration 0043 checksum refreshed for the stale-comment edit.

### Changed

- **Low-power dashboard performance optimization (Raspberry Pi / SBC).** Default install now targets dashboard responsiveness on Raspberry Pi 4/5 and similar SBC hardware while keeping the single Granian worker process model. `DatabaseConfig.worker_threads` defaults to `2` so `app.py:_lifespan_runtime` opens a separate read-only `stats_db` connection on file-backed SQLite — dashboard analytics no longer queue behind request-path writes on the shared connection lock. `ServerConfig.threads` defaults to `2` so the single worker can multiplex streaming proxy traffic + dashboard requests without single-event-loop starvation. Granian `workers=1` is unchanged. `RoutingTraceConfig.mode` defaults to `"sampled"` with `sample_rate = 0.05` and `include_score_components = False`, reducing routing-decision insert volume by ~20x on default installs. The CLI startup log line `Granian profile: workers=1 runtime_threads=N database_worker_threads=M access_log=...` makes the effective profile visible at every `eggpool serve` start.
- Dashboard HTML and JSON handlers (`handle_runtime`, `handle_cache`, request-shaping/compression/transcoding JSON routes) now use the lifespan-wired `app.state.stats` and pass `use_cache=True` for the heavy aggregate methods (`get_transcoding_stats`, `get_cache_observability`, `get_canonical_request_segmentation`, `get_compression_observability`, `get_compression_runtime`, `get_compression_policy_stats`, `get_cache_stability`, `get_synthetic_cache_summary`, `get_compression_tuning_window_metrics`). The 30s in-memory dashboard cache keeps repeated renders off the database; API endpoints that are documented as exact remain exact. `DashboardTelemetry` (`src/eggpool/dashboard/telemetry.py`) records per-route render durations in a 100-sample rolling buffer; `RuntimeMetricsService._snapshot_dashboard_telemetry` exposes `{recent_render_ms_p50, recent_render_ms_p95, slowest_recent_route, separate_stats_db, runtime_threads, database_worker_threads, routing_trace_mode}` under `/api/stats/runtime`.
- Background tasks register with explicit `initial_delay_s` offsets (`metrics_flush=5s`, `usage_window_refresh=15s`, `stale_request_finalizer=25s`, `health_disabled_models_prune=40s`, `model_info_canonical_backfill=10s`) so 30s/60s-cadence ticks do not cluster on the same wall-clock second. `background/periodic_initial_offset(name, interval_s, *, max_fraction=0.5)` is the deterministic-from-name helper for future additions. Startup crash recovery is unchanged.
- `docs/deployment.md` adds a Performance Profiles section (balanced / minimum-footprint / full-diagnostics) with a symptom-to-knob troubleshooting table. `docs/raspberry-pi.md` documents the recommended profile for Pi-class installs.

## [0.5.3] - 2026-07-05

### Fixed

- Installer Python selection now applies the documented 3.11-3.14 support
  window to both version-suffixed probes and the bare `python3` fallback, so
  Python 3.15+ is rejected until the Granian/PyO3 dependency stack supports it.

## [0.5.0] - 2026-07-01

### Changed

- **Mobile dashboard topbar lays burger, brand, and refresh on one row.** Hoisted the burger button out of `<nav class="topnav">` and rendered it as a sibling before the `<h1>` so the mobile flex layout puts [burger][EggPool][↻] inline instead of stacking the brand title on its own row beneath the icons. The `<nav>` keeps `margin-left: auto` on wide viewports (so the menu and refresh stay right-aligned) but drops to inline `width: auto` on mobile so the three icons stay grouped on one row. The brand title shrinks to `flex: 0 0 auto` (was `flex: 1 1 100%` with centered text) to match its intrinsic content width.
- **Burger button no longer carries a tooltip.** The hamburger glyph is self-explanatory on a phone, so `data-tooltip="Open page menu"` and the companion `data-tooltip-open-label="Close page menu"` swap attributes were retired. The `aria-label` still swaps between the open and close copy so assistive tech announces the state. The expanded-state tooltip suppression CSS rule and the JS swap path were dropped with it.

## [0.4.8] - 2026-07-01

### Changed

- Refined model-info status from enriched detail and normalized provider-suffixed lookups.
- Centralized Anthropic usage token mapping into usage module.
- Added model capabilities normalizer and routing eligibility enhancements.
- Fixed burger icon transform-box to fill-box for correct rotation pivots.

## [0.4.7] - 2026-07-01

### Changed

- **Pruned stale skills, plans, and documentation.** Removed duplicate `.agents/skills/` directory (identical to `.opencode/skills/`). Removed 35 orphaned completed plans from `plans/` (retained 10 plans still referenced from architecture docs). Updated `AGENTS.md` file organization with `_share` directory. Updated `architecture/README.md` package structure to include missing modules (`config.py`, `config_utils.py`, `deploy_user.py`, `onboard.py`, `toml_edit.py`).

## [0.4.6] - 2026-06-30

### Fixed

- **SQLite integer overflow protection for accounting values.** Added `clamp_sqlite_integer()` to prevent overflow when persisting microdollar costs, token counts, and quota reservations to SQLite's 64-bit INTEGER columns. Applied across pricing, cost reporting, quota estimation, and cost calculation paths so extreme upstream metadata or attacker-crafted payloads cannot produce unpersistable values.

## [0.4.5] - 2026-06-30

### Fixed

- **Transcoder preserves `stream` flag in OpenAI-to-Anthropic request encoding.** The `stream` field is now carried through from the incoming OpenAI request to the upstream Anthropic request instead of being dropped, ensuring streaming requests are forwarded correctly.

## [0.4.4] - 2026-06-30

### Fixed

- **Provider-scoped transcodable protocol lookup**. `get_transcodable_protocols` and `count_eligible_accounts_for_protocol` on `ModelCatalogCache` now accept an optional `provider_id` filter so protocol inference and eligibility counting only consider accounts that belong to a specific provider. `_infer_upstream_protocol` in `proxy_request.py` and `RequestCoordinator._resolve_upstream_protocol` pass the selected account's `provider_id`, preventing cross-provider protocol leakage when the same model is served by providers with different protocol surfaces.
- **MiniMax token-plan contract warning**. `check-config` now emits a warning when a MiniMax provider entry targets `api.minimax.io` with `openai` protocol but does not use the `/anthropic` path — token-plan keys hitting the OpenAI `/v1/chat/completions` surface can return upstream `insufficient balance (1008)` errors.
- **`build_upstream_headers` now receives the upstream protocol**. The coordinator passes `context.upstream_protocol` to `build_upstream_headers` so protocol-specific auth header construction (e.g. `x-api-key` for Anthropic vs `Authorization: Bearer` for OpenAI) uses the correct upstream protocol instead of the client's protocol.

## [0.4.3] - 2026-06-30

### Added

- **Phase 6.1: Tool-Use Transcoding**. Bidirectional OpenAI ↔ Anthropic tool calling translation for both streaming and non-streaming requests. The transcoder now lifts `tools`, `tool_choice`, assistant `tool_calls` history, `role: "tool"` history, and `tool_use` / `tool_result` content blocks across protocols instead of dropping them with a warning.
  - **Body translation**: `OpenAIToAnthropic.encode_request` / `decode_response` and `AnthropicToOpenAI.encode_request` / `decode_response` translate `tools[]`, `tool_choice` (string ↔ object shapes), `parallel_tool_calls` (`true` omitted; `false` dropped with `parallel_tool_calls_collapsed` warning since Anthropic has no parallel-disable knob), assistant `tool_calls[]` ↔ `content[].tool_use`, and `role: "tool"` ↔ `content[].tool_result` blocks. `tools[].function.strict` is dropped (no Anthropic equivalent); `tools[].cache_control` is dropped on the Anthropic → OpenAI path.
  - **Streaming tool_call delta translation**: `OpenAIToAnthropicStreaming` and `AnthropicToOpenAIStreaming` extend their state machines to track `content_block_start` / `input_json_delta` / `content_block_stop` triples and emit OpenAI `tool_calls` deltas in insertion order; the reverse direction buffers `tool_calls[*].function.arguments` chunks keyed on `tool_calls[*].index` and flushes Anthropic `tool_use` blocks on `finish_reason: "tool_calls"`. `flush()` emits a final delta for any slot whose `content_block_stop` never arrived so the client never hangs on a missing terminal.
  - **Tool-call ID translation map**: a per-request `ToolCallIdMap` (on `TranscodeContext.id_map`) mints `call_<24 hex>` and `toolu_<24 hex>` ids so the two namespaces never collide. Both `generate_openai_id()` and `generate_anthropic_id()` produce 24 hex characters after the prefix. The map is per-`TranscodeContext`, so concurrent requests cannot collide.
  - **`pause_turn` sentinel handling**: Anthropic's `pause_turn` `stop_reason` maps to `finish_reason: "tool_calls"` plus a synthetic tool_call entry `{"id": "call_pause_turn_<request_id>", "type": "function", "function": {"name": "__eggpool_pause_turn__", "arguments": "{}"}}`. OpenAI clients detect the sentinel by name and resume the turn with the same `tool_use_id`. A `pause_turn` loss warning is appended whenever the sentinel is synthesized.
  - **`stream_options.include_usage` lifting**: `OpenAIToAnthropic.encode_request` lifts `stream_options.include_usage` onto `TranscodeContext.request_include_usage` before the streaming transcoder runs. The streaming transcoder reads the flag to decide whether to forward upstream usage chunks to the OpenAI client. The wrapper object itself is dropped with a `dropped_field` warning.
  - **New loss-warning kinds**: `tool_call_id_translated`, `tool_call_id_changed`, `parallel_tool_calls_collapsed`, `malformed_tool_arguments`, `invalid_tool_choice`, `unsupported_tool_type`, `empty_tool_use_block`, `tool_result_image_dropped`, `tool_result_error_passthrough`, `cache_control_dropped`, `pause_turn`, `non_text_content_dropped`, `tool_result_inferred`. All are registered in `eggpool.transcoder.LOSS_WARNING_KINDS`. See `docs/transcoding.md` § Tool-Use Transcoding for the full catalogue.
- **Catalog non-destructive refresh contract.** `ModelCatalogCache.update_from_account()` now accepts `authoritative: bool = False, allow_withdrawals: bool = False`; both flags default to `False` so a failed, empty, or partial refresh cannot silently de-pool a healthy account. Operators opt into the old "refresh is the source of truth" behavior with `ModelsConfig.catalog_withdrawal_policy` (`preserve_until_health` default, `confirmed_once`, `confirmed_twice`). Under the default policy, health is the only de-pooling mechanism. `CatalogService._fetch_and_process_account()` returns an `AccountCatalogOutcome` enum (`SUCCESS_AUTHORITATIVE`, `SUCCESS_PARTIAL`, `SUCCESS_EMPTY`, `FAILED`, `SKIPPED`) plus an `AccountCatalogUpdateResult` summary row so the cache layer can stay non-destructive while operators still get full audit trail. `_log_refresh_summary` emits a single INFO line enumerating per-outcome counts after every cycle so catalog uncertainty shows up without enabling debug logging.
- **`eggpool accounts explain --gates`** (`src/eggpool/cli_full.py`) renders the per-account gate breakdown (config, credentials, health, circuit, provider id registry/catalog match, provider-supports-protocol, model support row/availability/freshness, provider-metadata-exists, protocol_match, local_quota_gate, final_eligible) as a compact text table. Backed by `Router.explain_account_eligibility(include_gates=True)` and `Router._collect_gate_status(...)`; the breakdown is informational — the canonical decision still comes from `_classify_eligibility`.
- **Same-tier account fairness rotor**: deterministic round-robin rotation for effectively tied same-tier accounts prevents routing starvation. Configurable via `[routing] fairness_mode`, `fairness_epsilon`, and `fairness_scope`. Defaults to `round_robin` with `provider_model_protocol` scope. Fairness decisions are recorded in `routing_decisions.score_components_json` under the `fairness` key for operator diagnostics. The `fairness` payload now includes the `scope` field, and each `top_candidates` entry carries `rank_before_fairness`, `rank_after_fairness`, and `fairness_band_member` for full traceability. The `FairnessRotor` position map is capped at 4096 entries (`_ROTOR_HARD_CAP`); when reached the entire map is cleared and rotation restarts from 0.
- **Bidirectional OpenAI ↔ Anthropic protocol transcoding.** When `[transcoder] enabled = true`, requests from clients using one protocol can be forwarded to upstream accounts that speak only the other. Initial scope is text-only requests and responses, plus streaming SSE. Tool calls, vision, and extended thinking land in a follow-up release. See `docs/transcoding.md` for the full translation table.
- New `eggpool stats transcoding [--period 1d|7d|30d]` subcommand for transcoding observability.
- New "Transcoding" card on the `/runtime` dashboard page.
- Structured INFO log per transcoded request with `request_id`, protocol direction, account, and loss-warning count.
- Boot-time INFO line when `[transcoder] enabled = true` so operators see the configuration at startup.
- **`routing_decisions.score_components_json` column (migration `0035`)** carries the per-account score breakdown captured by `QuotaFairScorer` at the moment the coordinator chose the selected account. The dashboard can now answer "why account A over account B?" without rescoring from quota tables. Includes `quota_score`, `inflight_penalty`, `health_penalty`, `final_score`, `weight`, `active_request_count`, `reserved_microdollars`, per-window `cost_*` and `capacity_*` microdollar values, `tier`, `requires_transcode`, and the top 5 near-tie candidates.
- **`eggpool accounts explain --model <id> [--provider P] [--protocol P] [--scores]`** subcommand (`src/eggpool/cli_full.py`) renders a Rich table listing every registered account with its live eligibility verdict and a stable `reason_code` (`disabled`, `auth_failed`, `quota_exhausted`, `cooldown`, `rate_limited`, `circuit_open`, `wrong_provider`, `no_protocol`, `protocol_mismatch`, `no_model`, `model_stale`, `ok`). With `--scores`, eligible accounts are also scored and the output includes priority, weight, active request count, reserved microdollars, and routing score. Re-evaluated on every invocation against the live registry + catalog so operators can diagnose routing skew without restarting the service.
- **`GET /api/stats/routing/eligibility`** JSON counterpart (auth-gated via the existing stats-route dependency list, `src/eggpool/api/stats.py`) returns the same per-account verdict list as a JSON document for programmatic dashboards and alerting.

### Changed

- `RequestCoordinator` now carries `upstream_protocol` alongside `protocol` on `ProxyRequestContext`. Behaviour is identical when `[transcoder] enabled = false`.
- **`ModelCatalogCache.update_from_account()` semantic shift — now non-destructive by default.** Both `authoritative` and `allow_withdrawals` keyword arguments default to `False`, so a per-account catalog update that omits previously-known support rows no longer silently de-pools those rows. Updates that already pass `authoritative=True, allow_withdrawals=True` continue to behave as before (and are still the only way to remove support via a refresh). All in-tree callers that intentionally destroy support — the destructive `prune_unused()` cleanup step and the explicit `update_from_account(...)` calls in `tests/unit/test_catalog.py` / `tests/integration/test_catalog_unresolved_models.py` — now pass both flags explicitly so the test suite continues to assert legacy destructive behavior. See `architecture/README.md` § Catalog Refresh Semantics.
- **`RequestCoordinator._select_and_persist_attempt()` lock scope.** The runtime publication step (`Router.increment_active_request_count` + `QuotaEstimator.add_reservation`) now runs INSIDE `_select_lock` AFTER the durable transaction commits but BEFORE the lock releases. The two contexts are written as explicit nested `async with` blocks (outer `_select_lock`, inner `_db.transaction()`). Note: collapsing them back into the previous compound `async with self._select_lock, self._db.transaction():` form would NOT by itself re-introduce the stale-score race on context-exit-order grounds — Python exits context managers right-to-left, so the transaction would still commit before the lock released. The actual bug was that the runtime publication block lived INSIDE the transaction body, so active-count and reserved-cost state were published before the durable transaction committed. The explicit nested form makes it hard to accidentally place publication inside the transaction while still keeping publication under `_select_lock`. The key invariant is block placement (publication must be outside the DB transaction body but still inside `_select_lock`), not context-exit order. The compensation chain (decrement → finalize-as-cancelled → release health slot → set `client_metadata["post_commit_interrupted"]` → re-raise) is preserved and still catches `BaseException` (including `CancelledError` / `SystemExit` / `KeyboardInterrupt`, all re-raised without being swallowed).
- `RoutingScore` gains diagnostic fields (`reserved_microdollars`, `cost_5h_microdollars`, `cost_7d_microdollars`, `cost_30d_microdollars`, `capacity_5h_microdollars`, `capacity_7d_microdollars`, `capacity_30d_microdollars`, `active_request_count`) so the scorer can return enough state to populate `score_components_json` without a second pass over the quota tables.
- `RoutingDecisionTrace` gains `score_components: Mapping[str, Any] | None` plus `to_score_components_json()`; `RoutingDecisionRepository.create()` accepts an optional `score_components_json` argument (defaults to `'{}'` for backward compatibility with rows inserted by code paths that have not yet been migrated).
- **`score_components_json` payload adds per-window utilization ratios and a tie-break summary.** The diagnostic JSON now carries `util_5h`, `util_7d`, `util_30d` (None when capacity is unconfigured) plus a `tie_break` dict naming the decisive factor between the chosen account and the runner-up (`tier`, `quota`, `inflight`, `transcode`, `near_tie`, `exact_tie`, `no_runner_up`) so the dashboard can surface a concrete cause without re-scoring.
- **`eggpool accounts explain` hydrates the catalog from SQLite.** The command now opens the database, runs migrations on a fresh install, and calls `ModelCatalogCache.hydrate_from_db(db)` (a new read-only helper on the cache module) to populate the in-memory model / provider / account-support tables from `models`, `provider_model_metadata`, and `account_models` rows before classification. The previous implementation constructed an empty cache and would have reported every account as ineligible even if the catalog-service shape had been right.
- **`eggpool accounts explain` no longer imports `rich`.** The undeclared `rich` dependency was replaced with plain `click.echo` columnar output. `reason_detail` strings now embed the account name, provider id, configured protocols, requested model id, and stale-window seconds so operators can act directly on the diagnosis.
- **`eggpool accounts status` now prints `routing_priority`.** The per-line output gained a `priority=N` field derived from the account's provider, alongside `provider`, `enabled`, `weight`, and the api-key-env set state.
- **`eggpool accounts explain` runs migrations on fresh installs.** The inner `_run_explain` coroutine now calls `MigrationRunner(db).run()` before hydrating `ModelCatalogCache.hydrate_from_db(db)`, so a brand-new (unmigrated) database path no longer crashes with `sqlite3.OperationalError: no such table: models` / `provider_model_metadata` / `account_models`. With no catalog rows yet, accounts surface a `no_model` verdict instead of the SQL error. The command still performs no outbound provider refresh.

### Fixed

- **Dashboard rollup filters now match request-table filters.** When buffered analytics are enabled, account-filtered summaries, unknown-account summaries, flat model-filtered timeseries, and grouped model-filtered timeseries now push filters into `usage_rollups` queries instead of accidentally returning global rollup data or falling back because the display series key did not contain the filtered model id.
- **Cross-account protocol poisoning from a partial sibling refresh.** `_provider_models` is keyed by `(model_id, provider_id)` and shared by every account that lists that provider (e.g. all `opencode-go-0001`/`-0002`/`-0003` accounts share one row per model on the `opencode-go` provider). Prior to this fix a single sibling's partial refresh — transient upstream parse error, unresolved family prefix, or a model whose protocol cannot be re-derived this cycle — produced a model entry with `protocol=None` and clobbered the previously-resolved protocol on the shared row, silently dropping every sibling account from routing even though `_account_support` still listed them. `ModelCatalogCache._preserve_resolved_protocol()` now applies a sibling-wins guard: a non-destructive `update_from_account` will keep the existing resolved protocol when the new entry's `protocol` is `None` and the prior row carried a resolved value. The destructive path (`authoritative=True AND allow_withdrawals=True`) intentionally skips the guard so operator-initiated withdrawals remain effective. `tests/unit/test_catalog_withdrawal_policy.py::test_partial_refresh_does_not_clobber_shared_provider_protocol{,multiple}` and `test_explicit_destructive_update_can_still_clear_protocol` pin the new contract.

## [0.3.5] - 2026-06-27

### Changed

- **README rewrite**: condensed the README from 870 lines to a concise, user-focused overview with quick start, CLI reference, configuration summary, API endpoints, and a documentation table linking to dedicated docs. Detailed content (deployment, providers, proxy, backup, model limits, Raspberry Pi, firewall, filesystem layout, network diagnostics) now lives in `docs/` and is linked from the README where it makes sense.

## [0.3.4] - 2026-06-27

### Fixed

- **Exclusion taxonomy empty-state**: the Routing page's doughnut chart now shows `<p class="empty">No exclusion data in this period.</p>` instead of an invisible Chart.js ring when no exclusions have been recorded in the selected period. The previous behaviour rendered a zero-data doughnut whose legend was visible but the chart itself was not, producing a "key but no graph" artefact.
- **`circuit_breaker` classification**: `SUPPRESSIVE_EXCLUSION_REASONS` now includes `circuit_breaker`, the only exclusion reason the coordinator actually writes to `exclude_reasons_json`. Previously every real-world exclusion landed in the `unknown` bucket because the frozenset only contained the legacy `circuit_open` name.
- **Catalog empty-data list is now classified `SUCCESS_EMPTY`, not `SUCCESS_AUTHORITATIVE`.** `CatalogService._fetch_and_process_account()` now distinguishes between "no model list in the response payload" (the existing `SUCCESS_EMPTY` branch on `result.response == {}`) and "model list returned but zero normalizable items after filter" (a new branch that fires when `normalize_models(...) == []` with `result.error is None`). Prior to this fix a fully empty but healthy upstream response was reported as authoritative and could mask a regression in the upstream `/v1/models` payload; the per-cycle summary line in operators' logs now correctly enumerates these as `empty=N`.

### Added

- **Sticky dashboard topbar**: `header.topbar` is `position: sticky; top: 0; z-index: 5` with a subtle backdrop blur, so the page navigation stays visible while scrolling on desktop. Mobile layout is unchanged (the topnav disclosure still wraps cleanly under 480px).
- **Footer update indicator**: periodic PyPI check (default 24h interval, 15s timeout) drives a footer pill that appears only when a newer `eggpool` release is available. The pill shows the current and latest versions side-by-side and the one-liner command (`eggpool update`) in an inline-code block. Clicking the command copies it to the clipboard via the bundled `dashboard.js` (Clipboard API with `execCommand("copy")` fallback); a transient "copied!" indicator confirms success. The new `src/eggpool/update_checker.py` module is the single source of truth for PyPI lookups — both the dashboard background task and the `eggpool update` CLI share `async_check_for_update()` so the two paths cannot drift.
- **`/api/stats/update` endpoint**: auth-gated JSON snapshot of the latest `UpdateChecker` state (`current_version`, `latest_version`, `update_available`, `last_checked_at`, `last_error`). Returns an empty payload if the checker has not yet produced a snapshot. Always auth-gated regardless of `dashboard.public`.
- **Runtime dispatch overhead and load metrics**: `DispatchOverheadRecorder` (`src/eggpool/runtime_dispatch.py`) records `time.perf_counter_ns() - context.started_monotonic_ns` immediately before `client.send(...)` in both `_execute_non_streaming` and `_execute_streaming`, on every upstream attempt (retries included). Bounded `deque(maxlen=100)`, thread-safe, integer-nanosecond storage — no body, model ID, account name, auth header, or client IP ever enters the buffer. `RuntimeMetricsService.snapshot()` gains two top-level sections: `dispatch_overhead` (avg/min/max/p50/p95 over the last 100 attempts) and `load` (`os.getloadavg` 1m/5m/15m + normalized per-core; `available: false` on platforms without it). The Runtime dashboard drops the configured-thread and process-count cards in favor of `Active threads`, `Load average`, and `Dispatch overhead`; process-count anomalies surface as a warning-only panel. `eggpool runtime-status` and `docs/deployment.md` document the new metrics.

## [0.3.0] - 2026-06-25

### Added

- **Daemon mode**: `eggpool serve --daemon` spawns a detached supervisor
  and returns the shell promptly. The child runs the normal foreground
  `serve` command (Granian supervisor + worker); `--daemon` is never
  forwarded. Flags: `--log-file PATH`, `--quiet`, `--as-root`. Default
  log destination is `~/.local/state/eggpool/eggpool.log`.
- **Fast-path CLI**: `eggpool ensure-running` and `eggpool croncheck`
  are dispatched without importing Click via `eggpool.fastcli`. Both
  modules are stdlib-only, keeping cron watchdog ticks cheap on
  Raspberry Pi-class hardware.
- **`eggpool runtime-status`**: compact terminal health summary from
  the running server (process topology, memory, background tasks,
  database health, in-flight requests). Supports `--json` for
  scripting.
- **Grouped timeseries dashboard**: stacked-bar chart on `/timeseries`
  with groupable dimensions (`provider_model`, `provider`, `model`,
  `account`), top-N + Other folding, per-bucket detail table, and
  interactive controls for period, bucket, group_by, metric, and
  limit. Backed by `/api/timeseries/grouped`.
- **Metrics dashboard**: reliability page (attempt success/retry
  breakdown, `retry_category` distribution, pending health, operational
  events), routing page (per-`(model, provider)` decision aggregates,
  account selection counts, exclusion taxonomy), traces page
  (auth-gated recent request metadata), latency phase decomposition
  (`upstream_connect_ms`, `upstream_read_ms`, `coordinator_overhead_ms`).
- **CSS tooltip system**: pure-CSS `[data-tooltip]` bubbles on heatmap
  cells, column headers, topbar controls, and status badges. No
  JavaScript; survives overview auto-refresh `innerHTML` swap.
- **Upstream-authoritative suppression**: local quota estimates are
  advisory by default (`local_quota_mode = "score_only"`). Above-capacity
  accounts stay eligible; only upstream-observed failures (429/402/5xx/auth)
  and explicit operator disablement suppress routing. Opt in to legacy
  behavior via `local_quota_mode = "hard_cap"`.
- **Runtime/ops metrics**: `/api/stats/runtime` endpoint exposes
  process topology, memory, background task state, database health,
  and in-flight request counts. `/runtime` dashboard page renders
  these metrics.
- **Attempt analytics**: per-attempt aggregates including latency
  percentiles, byte totals, retry rate, and `retry_category`
  distribution. Every `request_attempts` row carries
  `provider_id/model_id/protocol/retry_category/release_reason/bytes_received/latency_ms/streamed/is_retry_outcome`.
- **Routing analytics**: per-`(model, provider)` decision aggregates,
  account-level selection counts, and per-`(account, reason)` exclusion
  counts. Every routing decision persisted to `routing_decisions` in
  the same transaction as the `request_attempts` INSERT.
- **Operational health**: `crash_recovery`, `stale_request_finalizer`,
  and `reservation_reconcile` safety-net events recorded as
  `operational_events` rows.
- **Pricing provenance**: `source_detail` and `source_confidence`
  columns on `model_price_snapshots` for dashboard attribution.
  Migration `0031_price_snapshot_provenance.sql`.
- **Pricing alias registry**: maps upstream model IDs to external
  catalog IDs with `exact`/`curated_alias`/`ambiguous_skip` confidence.
  Migration `0030_model_pricing_aliases.sql`. Seeded idempotently at
  startup.
- **Install/deploy simplification**: `eggpool deploy systemd --install`
  personal mode auto-detects user, binary, and config paths.
  `eggpool deploy backup-cron` and `eggpool deploy all` for complete
  lifecycle management.
- `eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`
  operator escape hatch for fixing inflated cost totals after resolver
  upgrades.
- `eggpool init-config` writes bundled `config.example.toml` to
  current directory or target path.

### Fixed

- 503 saturation after several minutes of streaming load. Streaming
  request finalization is now wrapped in
  `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task
  cancellation cannot kill the finalizer while it holds the SQLite
  connection lock. A periodic `stale_request_finalizer` background
  task force-finalizes any request that has been `pending` longer than
  `upstream.read_timeout_s` and reconciles the in-memory active-count
  and quota-reservation caches. Startup `_crash_recovery` no longer
  time-gates its sweep — a process restart is treated as a definitive
  boundary, so every leaked pending request and every active
  reservation from the previous process is recovered on boot.
- MiMo-style cost inflation via provider-aware pricing resolution.
  The resolver now correctly handles cached-token-heavy models where
  upstream metadata reports different pricing than the external catalog.
- Stale `format_tokens` assertion after unit-scaling rewrite.
- MiniMax and GeneralCompute provider contract alignment: auth headers,
  URL composition, and static model seeding now match the documented
  contracts.

### Changed

- `eggpool serve` runs as a single supervisor process invoking Granian
  with `workers=1`. The supervisor owns the PID file; the FastAPI
  lifespan no longer touches it.
- `eggpool restart` delegates to `runtime.restart_server` instead of
  inlining subprocess logic.
- AGENTS.md trimmed to point to skills for details; CLI commands
  table expanded to cover all 35+ commands.

## [0.2.2] - 2026-06-25

### Fixed

- Catalog no longer accumulates orphans when an upstream provider
  withdraws a model. `ModelCatalogCache.update_from_account` now
  records the per-account `(model_id, provider_id)` keys it
  advertises and clears stale per-provider rows on the next
  refresh; `prune_unused()` drops entries from `_models` and
  `_account_support` that have no remaining reference, and
  `CatalogService.refresh()` calls it after every per-account
  gather. The "Skipping unresolved model during catalog
  persistence" warning now fires once per model id per process
  and is demoted to DEBUG on subsequent cycles, so a persistent
  unresolved upstream name no longer spams the log.
- A new reconciliation pass runs at the end of
  `_persist_catalog` to align the durable catalog with the live
  cache. Models that are no longer advertised by any account are
  deleted; rows with historical request or reservation history
  are relinked to a shared `__deprecated__` placeholder while
  the original id is preserved in the new
  `requests.original_model_id` and `reservations.original_model_id`
  columns. Orphan `provider_model_metadata` rows and disabled
  `account_models` rows with no request history are also
  removed. Migration `0023_deprecated_model_placeholder.sql`
  inserts the placeholder and adds the two new columns.
  Stats queries use `COALESCE(original_model_id, model_id)` so
  dashboard widgets continue to attribute historical usage to
  the real model name.

## [0.2.1] - 2026-06-24

### Fixed

- `eggpool serve` returning 500 with `TypeError: 'NoneType' object is not
  callable` on Python 3.14 / spawn-based multiprocessing start methods.
  Granian workers re-import `eggpool.cli` in a fresh interpreter, so the
  module-level `_app` set by the parent process was `None` in the worker
  and the `target_loader` returned `None` as the ASGI callback. `_app_loader`
  now rebuilds the `FastAPI` app from the config path inside each worker.
  Follow-up to the [0.1.4] module-level loader fix.

## [0.2.0] - 2026-06-24

### Added

- `eggpool backup` CLI command that bundles `config.toml`, `.env`, and the
  SQLite database (with `-wal`/`-shm`) into a timestamped `.zip` archive.
  Default location is `~/backups/eggpool/`; override with `--output-dir`.
  Honor `XDG_BACKUP_HOME` for the default.
- `eggpool recover [path]` CLI command that restores a backup archive. With
  no path, opens an interactive `TerminalMenu` selector over the default
  backup directory. Stages restored files alongside the current ones and
  rolls back on failure.
- `eggpool uninstall` CLI command that detects the install method
  (`pipx` / `uv tool` / `source` / `manual`) and removes the binary,
  active config, `.env`, database, and shell-rc entries. Supports
  `--yes`, `--keep-config`, `--keep-data`, and `--keep-path`. Prints
  instructions for manual removal of systemd, logrotate, and cron
  artifacts (these are never removed automatically).
- New `eggpool.lifecycle` module (`backup`, `uninstall`, `__init__`)
  housing the lifecycle helpers.

## [0.1.7] - 2026-06-24

### Changed

- Install script fallback (no pipx) now uses `uv tool install .` instead
  of `uv sync`, so `eggpool` works as a bare command from any directory
  after install — matching the pipx experience. Adds `uv tool update-shell`
  to persist `~/.local/bin` on PATH.
- Post-install prompt (`install_prompt.py`) uses bare `eggpool --config`
  when the command is on PATH; falls back to `uv run --directory` when
  not yet available. Prints an actionable error when neither is present.
- Install instructions (README, deployment docs) updated to reflect bare
  `eggpool --config <path>` invocation pattern.

## [0.1.6] - 2026-06-23

### Fixed

- `eggpool serve` and `eggpool check-config` now suggest running
  `eggpool onboard` or `eggpool connect` when the config file is missing,
  instead of showing a bare error.

## [0.1.5] - 2026-06-23

### Fixed

- Install script now caps Python version at 3.14. Pyo3 (used by Granian)
  does not yet support Python 3.15.

## [0.1.4] - 2026-06-23

### Fixed

- Fix `eggpool serve` crash on Linux/macOS: Granian worker processes
  failed to start due to unpicklable local closure in `target_loader`.
  Moved `_app_loader` to module level for multiprocessing compatibility.
- Install script now invokes pipx through the detected Python version
  (`python3.x -m pipx`) to avoid using the wrong interpreter when
  system Python differs from the detected version.

## [0.1.3] - 2026-06-23

### Changed

- `eggpool onboard` now creates a minimal config and generates a server
  API key on fresh installs, eliminating the need for `init-config`.
- Install script recommends `eggpool onboard` instead of `init-config`.
- `init-config` shows a helpful warning when config exists, recommending
  `eggpool onboard` for provider setup.

### Fixed

- Onboard flow now works deterministically on fresh installs without
  requiring manual config creation first.

## [0.1.2] - 2026-06-23

### Fixed

- Create minimal config when `config.toml` is missing during `eggpool
  onboard`, so fresh installs no longer fail with "Failed to update config".
- Fix `update` command misidentifying source installs as pipx (causing
  wrong upgrade method).

### Changed

- Add `--install` flag to `deploy` subcommands for automated setup.
- Rewrite deployment docs with personal-use and production sections.

## [0.1.1] - 2026-06-23

### Added

- `eggpool deploy` subcommands: `systemd`, `logrotate`, `cron`, `all`.
- Dynamic deploy snippets based on detected install paths.

## [0.1.0] - 2026-06-23

### Added

- Multi-provider aggregation across OpenAI- and Anthropic-compatible
  upstreams with quota-aware routing.
- SQLite-backed request, token, latency, error, and cost statistics.
- Multi-page HTML dashboard (overview, accounts, models, latency, pings,
  events, timeseries, bandwidth) with 50+ Halloy themes.
- CLI commands: `serve`, `check-config`, `migrate`, `onboard`,
  `connect`, `connect list`, `logout`, `accounts list`,
  `accounts status`, `models refresh`, `db vacuum`, `dashboard public`,
  `rehash`, `restart`, `stop`, `update`, `getkey`, `newkey`, `edit`,
  `configsetup opencode`, `configsetup claude-code`, `set`,
  `init-config`, and the `deploy` group (`systemd`, `logrotate`,
  `cron`, `all`).
- Operational scripts: `install.sh`, `install_prompt.py`,
  `check_database.py`, `smoke_test.py`, `verify_upstream_auth.py`.

### Notes

- See the README and `docs/deployment.md` for install, configuration,
  and deployment.
