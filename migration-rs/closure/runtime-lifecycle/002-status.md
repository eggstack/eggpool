# R002 Closure — Process Runtime, Generation Factory, and Candidate Ownership

Status: closed

Implementation commit: [`ded541f4a576928015ecf5f1be1b1a96b0b1539c`](https://github.com/eggstack/eggpool/commit/ded541f4a576928015ecf5f1be1b1a96b0b1539c)

Plan: [R002 — process runtime, generation factory, and candidate ownership](../../implementation/runtime-lifecycle/002-process-runtime-generation-factory-and-candidate-ownership.md)

Repository baseline: `3f9a537f8f41bf66756aeb492f5c3b4fadf160a3` before the closure-aligned test/resource refinement

## Outcome

R002 is complete. Rust now has one `ProcessRuntime` for the process-owned
database, model-router affinity cache, wire-learning resolver, and optional
config-path metadata; one immutable `RuntimeGeneration` for the request-visible
M7 graph; one `RuntimeGenerationFactory` used by the static server startup
path; and one `PreparedGeneration` candidate owner with explicit transfer or
asynchronous abort.

The factory compiles model-router and wire-profile structure before building a
provider pool, creates one generation finalization supervisor, passes one
process-owned wire resolver to both finite and streaming coordinators, and
injects the process-owned affinity handle. Candidate-owned provider clients
close idempotently and are dropped from the pool on generation close. Process
database, affinity, and wire state are never closed by candidate abort.

R002 does not add `arc-swap`, active publication, request leases, retirement
policy, reload diffs, background tasks, signal changes, or control/CLI work.

## Requirement-to-evidence matrix

| R002 requirement | Evidence | Result |
|---|---|---|
| Shared process runtime | `ProcessRuntime` owns `Database`, one `Arc<ModelRouterAffinity>`, one `WireResolver`, and optional config path; custom `Debug` is secret-free | Pass |
| Immutable generation metadata and M7 graph | `RuntimeGeneration` exposes monotonic id, digest, immutable config snapshot, `Arc<InferenceState>`, router metadata, and narrow close/drain handles | Pass |
| One startup/candidate factory | `server::run_with_digest` and `server::serve_listener` call `RuntimeGenerationFactory::prepare`; no startup call bypasses the shared graph builder | Pass |
| Pre-pool validation/compilation | `structural_failure_happens_before_client_pool_construction` rejects an invalid model-router mapping before pool construction; model-router and wire profiles compile before `ProviderClientPool::from_config` | Pass |
| Shared affinity and wire learning | `factory_builds_one_shared_m7_graph_for_finite_and_streaming` and `process_wire_learning_survives_candidates_but_fingerprint_changes_partition_it` verify process identity, learned preference reuse, and structural fingerprint partitioning | Pass |
| Shared finalization boundary | The finite and streaming coordinator handles compare equal with `FinalizationSupervisor::same_as` in the focused R002 suite | Pass |
| Candidate ownership state machine | `PreparedGeneration` implements prepared/transferred/aborting/aborted states; transfer is one-shot, abort is explicit and idempotent, and `Drop` only reports forgotten ownership | Pass |
| Reverse close and typed cleanup evidence | `GenerationResources::close` drains finalization before closing the provider pool and returns `GenerationCloseReport`; `candidate_abort_is_idempotent_and_does_not_close_process_state` verifies one close count and process DB survival | Pass |
| Construction failure cleanup | `failed_graph_build_closes_candidate_pool_but_not_process_database` proves post-pool graph failure returns a typed close report with exactly one pool close; pre-pool structural failure has no candidate resource | Pass |
| Transfer isolation | `transferred_candidate_is_not_abortable_by_candidate_owner` proves candidate abort cannot close transferred resources | Pass |
| Secret-safe diagnostics | `lifecycle_debug_is_secret_free` checks process, generation, candidate, pool, and provider-config debug surfaces do not expose a static header secret or proxy marker | Pass |
| M7 behavior preserved | Full Rust target regression and Python M7/runtime oracle suites remain green | Pass |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r002       # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets                        # 312 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1                    # 89 passed, 3 skipped
rtk uv run pytest tests/unit/test_runtime_manager.py tests/unit/test_runtime_generation_retirement.py tests/unit/test_runtime_task_inventory.py tests/unit/test_runtime_tasks.py tests/unit/test_config_reload_policy.py tests/unit/test_reload_diagnostics_matrix.py tests/unit/test_reload_inventory_audit.py tests/unit/test_reload_failure_injection.py tests/unit/test_reload_resource_failure_paths.py tests/unit/test_reload_manager_task_tracking.py tests/unit/test_reload_post_publication_failures.py -q --tb=short --maxfail=1  # 313 passed
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1                         # 14 passed
rtk git diff --check
```

No Rust dependency, SQLite schema, live provider, network fixture, active
publication primitive, or recurring task was added. `Cargo.toml` and
`Cargo.lock` remain unchanged.

## Future-plan audit and registry transition

R002 is removed from the dependency-ready table and recorded in the completed
implementation table with the implementation commit above and this accepted
closure record. R003 is promoted to the sole dependency-ready M8 plan because
its hard dependency, accepted R002 closure, is now satisfied. R004-R011 remain
serially blocked behind their immediate predecessors. M9 remains blocked on
accepted R011 M8 closure and its own planning/implementation review.

No other future plan can be safely unblocked by R002 alone. No unresolved
mandatory R002 requirement or high/medium correctness/security finding remains.
