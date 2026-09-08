# R010 Closure — Active-Generation Authority Audit and Runtime/Reload Diagnostics

Status: closed

Recommendation: closed; R011 promoted to dependency-ready

Implementation commit: `e3a9a27`

Plan: [R010 — active-generation authority audit and runtime/reload diagnostics](../../implementation/runtime-lifecycle/010-active-generation-authority-and-diagnostics.md)

## Outcome

R010 completes the M8 authority conversion at the Rust Axum boundary. Production
`AppState` now contains only constructor-owned server route/auth settings,
process-owned database state, and the manager handle; generation-owned
configuration and M7 services remain behind `RuntimeManager`. The compatibility
`AppState::from_inference` constructor is explicitly test/embedding scoped and
still wraps its graph in a manager-owned generation.

Inference body admission acquires the active generation before collection and
uses `http_body_util::Limited` with that generation's live
`server.max_request_body_bytes`. The same lease is shared through request
extensions and the finite or streaming coordinator path. Non-inference routes
do not acquire or buffer a generation body. Authentication and dashboard route
topology remain constructor-owned because R005 classifies them as restart
required.

Readiness acquires one generation for its config/credential checks and keeps it
through the durable catalog probe, so a rehash cannot mix generation A config
with generation B request authority. Runtime diagnostics are a typed bounded
projection containing active/retiring generation metadata, publication/gate
state, reload last-result data, task snapshots, startup recovery, shutdown
state, and aggregate counters. It never serializes full config, bodies,
credentials, proxy URLs, or arbitrary upstream error text.

The authority inventory is recorded in
[`migration-rs/authority/runtime-lifecycle.md`](../../authority/runtime-lifecycle.md).

## Requirement-to-evidence matrix

| R010 requirement | Evidence | Result |
|---|---|---|
| No direct generation authority in production state | `AppState` stores `ServerState`, database, manager, and body-task tracking; the R010 source audit asserts no `pub config: Config` field | Pass |
| Authority classification is reviewable | `migration-rs/authority/runtime-lifecycle.md` covers middleware, inference, readiness, dashboard, tasks, reload, diagnostics, and shutdown | Pass |
| Live body limit before buffering | `admit_inference_body` acquires a lease, applies `Limited`, rejects oversized input before handler/M7 execution, and forwards the same lease | Pass |
| Body-limit rehash behavior | `dynamic_body_admission_rejects_before_m7_for_the_active_generation` publishes a 32-byte candidate and receives 413 for a 33-byte body | Pass |
| Readiness uses active generation | `readyz` leases before config/credential/catalog checks and holds the lease through DB reads | Pass |
| Restart-required auth/dashboard topology | `ServerState` is constructed once; R005 restart-required classification remains unchanged and live reload only replaces generation state | Pass |
| Coherent active/retiring metadata | `ProcessRuntime::diagnostics` samples the manager pointer, slot state, finalization supervisor, and routing catalog counts without caching service pointers | Pass |
| Reload diagnostics | `ReloadService::reload` records bounded attempts, category, changed sections, restart paths, digest prefix, duration, and reason; only the last record is retained | Pass |
| Task/recovery/shutdown diagnostics | Task snapshots, startup recovery, process shutdown state, and bounded aggregate counters are included in `RuntimeDiagnosticsSnapshot` | Pass |
| Retirement/failure visibility | Failed-close slots remain resident and are projected with bounded state/error category; successful closed slots are reaped | Pass |
| Secret/cardinality safety | `diagnostics_are_bounded_coherent_and_secret_free` recursively checks JSON/Debug-derived output and bounds task/path collections | Pass |
| No M9/schema/framework scope | No DB migration, dashboard page, control socket, CLI, metrics backend, or dependency was added | Pass |

## Supported differences

Rust uses a manager-owned `ArcSwap`/lease boundary and a single process task
supervisor rather than Python's manager/condition implementation. The Rust
diagnostic surface is a typed in-process API for M9 rather than an M9 control
transport. These are structural differences only; request consistency,
reload classification, bounded cleanup, and secret-redaction behavior remain
the accepted contracts.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 --test runtime_lifecycle_r007 --test runtime_lifecycle_r009 -- --test-threads=1
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

- R010 focused Rust: 4 passed.
- Existing Rust lifecycle/inference focus: 24 passed.
- Rust aggregate: 359 passed across 39 suites.
- Python migration oracle: 89 passed, 3 skipped.
- Python stale-state/readiness/diagnostics/runtime focus: 199 passed.
- Python smoke: 14 passed.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Ruff format/check, Rust format/Clippy, and diff checks passed.

## Unresolved findings

No unresolved R010 correctness, resource, security, compatibility, schema, or
scope finding remains. The deferred process callbacks remain the explicit M9
inventory recorded by R008; R010 does not promote them or create placeholders.

## Future-plan audit and registry transition

R010 is removed from the dependency-ready table and recorded as closed in the
completed implementation table. R011 is the only future implementation plan
directly unblocked by accepted R010 closure and is promoted to
`dependency-ready` in the registry, runtime-lifecycle README, and subsystem
roadmap. R011 alone may close M8.

M9 remains blocked on accepted R011 M8 closure and its separate planning and
implementation review. No M9, M10, or later plan is automatically unblocked by
R010. No other future plan has a satisfied direct dependency that requires a
status change from this closure.
