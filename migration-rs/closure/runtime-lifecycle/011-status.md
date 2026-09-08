# R011 Closure — Differential Qualification and M8 Closure

Status: closed

Recommendation: closed; M8 is closed and M9 is eligible for its own
planning/implementation review

Implementation commit: `31b32c4`

Plan: [R011 — differential qualification and M8 closure](../../implementation/runtime-lifecycle/011-differential-qualification-and-m8-closure.md)

## Outcome

R011 adds the integrated runtime qualification required to close M8. The new
`runtime_lifecycle_r011` Rust test binary composes the process runtime,
generation factory, active-generation manager, transactional reload service,
task supervisor, real Axum router, startup reconciler, and file-backed SQLite
boundary. It is anchored to the committed R001 Python observations rather than
normalizing away lifecycle differences.

The qualification proves that startup and reload construct the same immutable
generation graph, process-owned affinity and task state survive swaps, live
body limits are enforced from the leased generation before buffering, and
publication has one gate/epoch boundary. It also proves bounded retirement
backlog behavior, task singleton/reconfiguration behavior, startup recovery
before first request, same-database reopenability, and secret-free bounded
diagnostics after repeated reload cycles.

The existing R002-R010 and C009-C011 qualification suites remain part of the
aggregate evidence. In particular, their finite/streaming coordinator,
retained-finalization, server shutdown, cross-generation lease, wire/affinity,
and failure-isolation tests are not replaced by the aggregate test.

## Requirement-to-evidence matrix

| R011 requirement | Evidence | Result |
|---|---|---|
| R001 oracle anchor and exact/semantic parity | `qualification_is_anchored_to_the_committed_r001_oracle`; R005 policy and R001 fixture tests | Pass |
| Startup/reload shared factory and ownership split | `factory_reload_and_process_state_have_one_coherent_boundary`; R009 startup path; R002 factory tests | Pass |
| Candidate abort/failure isolation | R002 candidate abort/graph-failure tests; R006/R007 preflight and cancellation tests | Pass |
| Reload no-op/live/restart/mixed/invalid/stale/task/router cases | `reload_matrix_matches_r001_and_keeps_task_state_transactional`; R005/R007 policy and persistence tests | Pass |
| Lease/publication linearization and waiter cancellation | `publication_gate_has_no_old_generation_leases_after_commit`; `cancelled_gate_waiter_and_retirement_backlog_leave_bounded_state`; R003 stress/rollback tests | Pass |
| Finite/stream generation pinning and handoff | R003 cross-generation lease tests, R010 leased Axum admission, and C007-C009 finite/stream public-runtime qualification | Pass |
| Retirement, finalization drain, close-once, and backlog bound | R004 retained-finalization/close-order tests; `cancelled_gate_waiter_and_retirement_backlog_leave_bounded_state` | Pass |
| Singleton and generation-leased background ownership | `task_inventory_is_real_or_explicitly_deferred_and_never_duplicates`; R006/R008 callback and recovery tests | Pass |
| Startup recovery and same-DB restart | `startup_recovery_precedes_first_request_and_same_db_remains_readable`; R008/C010 recovery tests; R009 reopen test | Pass |
| Axum active-generation authority and live body limit | `axum_live_authority_uses_reloaded_body_limit_before_buffering`; R010 source and readiness tests | Pass |
| Graceful/forced shutdown and reload/shutdown safety | R009 shutdown matrix; R003/R007 staged shutdown acceptance and cancellation tests | Pass |
| Bounded, coherent, secret-free diagnostics | `diagnostics_and_debug_remain_bounded_and_secret_free_after_cycles`; R010 diagnostics tests; authority source audit | Pass |
| All R001 inventory callbacks accounted for | R008 explicit inventory: three real callbacks and three documented M9 deferrals; R011 task inventory assertion | Pass |
| Schema/dependency/scope safety | No migration or new dependency; existing `arc-swap` remains the only M8 runtime dependency; `git diff --check` | Pass |

## Parity rules and accepted differences

Exact parity covers reload result classes, changed/restart path ordering,
generation publication increments, task names/spec ownership, acceptance and
retirement behavior, recovery status, and secret-redaction markers. Timing,
monotonic identifiers, and implementation-specific task scheduling are
compared semantically and remain bounded rather than being treated as exact
values.

The accepted structural differences are:

- Rust uses an `ArcSwap` active pointer and a narrow synchronous claim/publication
  section instead of Python's condition/slot implementation.
- Rust uses one process task supervisor with a fresh generation lease per
  generation-dependent tick instead of Python's split supervisor structure.
- Rust keeps a small unresolved-retirement cap and rejects new publication at
  the cap for SBC-safe bounded state.
- Rust exposes typed in-process lifecycle/diagnostic APIs; M9 will provide the
  control transport and CLI adapter.
- `metrics_flush`, `update_checker`, and `automatic_backup` remain explicit
  unregistered M9-owned capabilities, with no placeholder loops.

None of these differences changes request consistency, reload result
classification, shutdown/recovery behavior, or the live/restart policy.
No ADR is required because no canonical user-visible contract changed.

## Stable M9 handoff APIs

M9 may consume these server-side interfaces without reaching into generation
internals:

- `ReloadService::reload(ReloadRequest) -> ReloadResult`, plus
  `reload_path` and `reload_bytes`; `ReloadRequest::expected_digest` provides
  optimistic stale-config protection.
- `RuntimeManager::acquire() -> GenerationLease`, `active_slot`,
  `publication_epoch`, `retiring_slots`, `retirement_diagnostics`, `shutdown`,
  and `close_for_shutdown` for lifecycle ownership and status reporting.
- `ProcessRuntime::diagnostics(&RuntimeManager) -> RuntimeDiagnosticsSnapshot`
  and `task_capability_inventory()` for bounded status/control responses.
- `RuntimeTaskSupervisor::register_callback`, `prepare_diff`,
  `available_specs_for_config`, `snapshot`, `begin_shutdown`, and
  `shutdown_with_timeout`; future callbacks must register through this one
  process-owned supervisor.
- `ServerRuntimeHandle::request_shutdown`, `phase`, and the bounded
  `ServerRuntime` shutdown report for daemon/control integration.

Constructor-owned restart-required fields remain host/port, server auth and
logging, upstream transport, database constructor/path, dashboard route/auth
topology, security middleware, readiness constructor settings, and listener
state. M9 must hard-restart for those changes; only the R005 live policy may be
submitted to `ReloadService`.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r011 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/integration/test_readiness_probe.py tests/integration/test_readiness_transactions.py tests/integration/reload/test_stale_app_state.py tests/integration/reload/test_diagnostics_contract.py tests/integration/reload/test_reload_diagnostics_assertions.py tests/unit/test_runtime_metrics.py tests/unit/test_runtime_manager.py tests/unit/test_config_reload_policy.py -q --tb=short --maxfail=1
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk uv run pyright src/ scripts/
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk git diff --check
```

Observed results:

- R011 integrated Rust qualification: 10 passed.
- Rust aggregate: 369 passed across 40 suites.
- Rust format and Clippy: passed with `-D warnings`.
- Python migration oracle: 89 passed, 3 skipped.
- Python runtime/readiness/reload/diagnostics focus: 199 passed.
- Python smoke: 14 passed.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Ruff format/check and diff checks: passed.

No live or paid provider was used. No database migration, Cargo dependency,
M9 surface, or production runtime behavior changed in R011.

## Unresolved findings

No unresolved high/medium M8 correctness, resource, security, compatibility,
schema, dependency, or scope finding remains. The three deferred process
callbacks are explicit M9 inventory items carried forward from R008; they are
not silent no-ops and are not promoted by this closure.

## Future-plan audit and registry transition

R011 is removed from the dependency-ready table and recorded in the completed
implementation table. The runtime-lifecycle roadmap and registry now mark M8
closed after R011. M9 is eligible for its own planning and implementation
review, but no M9 plan is created or auto-promoted by this closure. M10-M12
remain sequenced behind their existing roadmap dependencies; no other
represented future plan has a newly satisfied direct dependency requiring a
status change.
