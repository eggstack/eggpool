# R008 Closure — Generation-Leased Maintenance, Recovery, and Background Integration

Status: closed

Recommendation: closed

Implementation commit: [`a814ea8`](https://github.com/eggstack/eggpool/commit/a814ea8)

Plan: [R008 — generation-leased maintenance, recovery, and background integration](../../implementation/runtime-lifecycle/008-generation-leased-maintenance-recovery-and-background.md)

## Outcome

R008 wires the closed C010 crash reconciler and R006 task supervisor into the
Rust process lifecycle. Startup now runs bounded reconciliation to convergence
after migrations/account synchronization and before generation construction or
request acceptance. The process retains only a scalar, secret-free recovery
report; reconciliation never creates or replays provider work and a recovery
failure closes startup.

The generation factory now exposes the existing M5 catalog service over the
same catalog cache used by routing. The `catalog_refresh` callback acquires a
fresh generation lease for every tick, isolates provider/account fetch failures
through the existing catalog service, and cannot retain a generation between
ticks. `retention_cleanup` performs bounded historical deletion through the
existing SQLite schema, using the leased generation's live retention and
maintenance budget. Each database batch is a separate transaction; pending
requests are preserved and active request traffic is not held across sleeps.

The process-owned checkpoint callback remains generation-independent. Initial
task installation is capability-filtered, so deferred inventory rows remain
visible in an explicit capability diagnostic but never create placeholder
loops. Process task shutdown is joined during the current foreground server
cleanup path, while signal/drain ownership remains R009 scope.

## Requirement-to-evidence matrix

| R008 requirement | Evidence | Result |
|---|---|---|
| Startup C010 recovery before serving | `ProcessRuntime::reconcile_startup()` is called after migrations/account sync and before `RuntimeGenerationFactory::prepare()` in `server::run_with_digest` and `serve_listener` | Pass |
| Multi-batch convergence and idempotence | `startup_recovery_converges_multiple_bounded_passes_without_provider_work` seeds 501 pending rows, proves multiple bounded passes, scalar reporting, and a no-op second pass | Pass |
| Recovery failure is startup-fatal | `ServerError::StartupRecovery` propagates reconciliation failure before listener serving/generation publication | Pass |
| No provider replay | C010 reconciler remains durable-only; the R008 startup test uses an invalid fixture endpoint and asserts only durable convergence | Pass |
| Generation-leased catalog refresh | `TaskCallbackRegistry::with_generation_maintenance` invokes `InferenceState`'s generation-owned `CatalogService` only from `TaskTickContext::Generation`; shared cache keeps routing and refresh coherent | Pass |
| Bounded retention maintenance | `Database::cleanup_retention` uses per-batch transactions, row/batch/time budgets, and terminal/historical predicates; the R008 test proves pending requests survive | Pass |
| Process-owned checkpoint | Existing `Database::checkpoint` callback remains registered once through `ProcessRuntime` | Pass |
| Singleton task installation/reconfiguration boundary | `ProcessRuntime::install_initial_tasks` attaches the one supervisor to the active manager and installs only available capabilities; R006/R007 staged diff tests remain green | Pass |
| Deferred inventory is explicit and loop-free | `task_capability_inventory` marks `metrics_flush`, `update_checker`, and `automatic_backup` deferred with owners/reasons; R008 asserts only catalog/retention/checkpoint loops start | Pass |
| Failure isolation and bounded diagnostics | Existing supervisor outcome handling plus bounded snapshots/history; catalog/retention errors return `TaskCallbackError` and do not close the runtime | Pass |
| Finalization ownership | No periodic finalization polling loop was added; `FinalizationSupervisor` remains self-owned and is drained by generation retirement/shutdown as required by the plan boundary | Pass |
| Schema/dependency/scope safety | No schema migration, new scheduler, provider retry path, M9 CLI/control surface, or Cargo dependency was added | Pass |

## Deferred callback inventory

| Task | Status | Future owner/reason |
|---|---|---|
| `metrics_flush` | deferred/unregistered | M9 metrics/background integration; no Rust metrics coalescer/writer capability exists yet |
| `update_checker` | deferred/unregistered | M9 operational update capability; no Rust PyPI checker is implemented |
| `automatic_backup` | deferred/unregistered | M9 backup/recovery capability; no Rust archive/backup workflow is implemented |

No other R001 inventory entry is deferred. `catalog_refresh`,
`retention_cleanup`, and `checkpoint` are registered real callbacks. The
deferred rows are not passed to the supervisor and therefore do not own a
running loop.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/unit/test_runtime_task_inventory.py tests/unit/test_runtime_tasks.py tests/integration/test_startup_lifecycle.py tests/integration/test_application_startup.py -q --tb=short --maxfail=1
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk git diff --check
```

Observed results:

- R008 focused Rust: 3 passed.
- R006 focused Rust: 10 passed.
- R007 focused Rust: 6 passed.
- Rust aggregate: 350 passed across 37 suites.
- Python migration oracle: 89 passed, 3 skipped.
- Python runtime/startup bundle: 68 passed.
- Python smoke: 14 passed.
- formatting, Clippy, and diff checks passed.

No live provider or paid external service was required.

## Unresolved findings

No unresolved R008 correctness, resource, security, compatibility, schema, or
dependency finding remains. The three deferred process callbacks are explicit
M9-owned capabilities, not incomplete running tasks. R009 remains responsible
for signal handling, graceful/forced shutdown semantics, and reload/shutdown
race qualification; those concerns were not pulled into R008.

## Future-plan audit and registry transition

R008 is removed from the dependency-ready table and recorded in the completed
implementation table with implementation commit `a814ea8` and this accepted
closure record. R009 is promoted to the sole dependency-ready M8 plan because
its hard dependency, accepted R008 closure, is satisfied.

R010 remains queued behind R009, and R011 remains queued behind R010 and is
still the only plan allowed to close M8. M9 operational CLI/control/daemon
work remains blocked on accepted R011 M8 closure and its separate planning and
implementation review. No later plan is unblocked by R008 beyond R009.
