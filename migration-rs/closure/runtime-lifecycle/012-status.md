# R012 Closure — Wire-Negotiation Runtime Authority and Reload-Diagnostics Re-Closure

Status: closed

Recommendation: closed; M8 is re-closed and M9 is eligible for its own
planning/implementation review

Implementation commit: `37ec54b`

Plan: [R012 — wire-negotiation runtime authority and reload-diagnostics re-closure](../../implementation/runtime-lifecycle/012-wire-negotiation-runtime-authority-and-reload-diagnostics-reclosure.md)

## Outcome

R012 corrects both post-R011 defects without reopening the M8 architecture.
Startup production paths now construct the single process-owned resolver from
the validated `routing.wire_negotiation` policy. Live reload stages the policy
and commits or rolls it back in the existing R007 acceptance window; rejected
and failed reloads cannot mutate resolver authority. Learned and rejected
observations retain bounded timestamps, so TTL/cooldown changes take effect
without flushing compatible state. A shared per-provider counter lets
concurrency limits converge while already-running negotiations finish.

Reload diagnostics now belong to the retained reload worker. A per-operation
owner token is acquired only after the reload lock, so a concurrent `Busy`
caller cannot clear the active operation. Guard drop records bounded terminal
abort evidence if the worker itself is aborted, while caller cancellation
leaves the retained transaction and diagnostics coherent.

## Requirement-to-evidence matrix

| R012 requirement | Evidence | Result |
|---|---|---|
| Startup non-default resolver authority | `startup_and_accepted_reload_install_non_default_wire_authority` | Pass |
| Enabled toggle and one process resolver | `policy_toggle_preserves_one_resolver_and_controls_negotiation`; R002 shared-state suite | Pass |
| Capacity, TTL, rejection cooldown, and compatible-state retention | `policy_capacity_and_time_changes_are_immediate_without_flushing_compatible_state` | Pass |
| Minimum interval authority | `minimum_interval_reconfiguration_changes_leader_eligibility` | Pass |
| Live concurrency transition | `concurrency_limit_changes_converge_without_killing_old_leaders` | Pass |
| Rejected reload isolation | `rejected_reload_categories_leave_wire_policy_unchanged`; R005/R007 suites | Pass |
| Repeated bounded policy transitions | `repeated_policy_reloads_keep_all_resolver_state_bounded` | Pass |
| Retained-worker caller cancellation and Busy ownership | `caller_cancellation_and_busy_do_not_corrupt_reload_diagnostics` | Pass |
| Shutdown cleanup and bounded Busy/cancellation storm | `shutdown_during_retained_reload_clears_diagnostics`; same cancellation test | Pass |
| Production request path after policy reload | Axum `/v1/healthz` request in `startup_and_accepted_reload_install_non_default_wire_authority` | Pass |
| R003/R005/R007/R009/R010/R011 compatibility | Focused suites below | Pass |
| Schema/dependency/M9 scope | No migration, Cargo dependency, second resolver, HTTP route, CLI, or control surface | Pass |

## Failing-before / passing-after evidence

The repository baseline `2220ad001066200e8240f2d597702458ab11ccc1` shows the
two defects directly: the process constructor installs
`WireResolverConfig::default()`, and `ReloadService::reload()` begins and
finishes diagnostics around the public `join.await`; `reload_owned()` returns
`Busy` after that global marker has already been set. The new R012 tests target
those exact seams rather than weakening R005's live classification. They pass
against the corrected implementation and exercise the baseline-failing
non-default startup, accepted policy transition, caller cancellation, and
concurrent Busy cases.

## Parity and supported differences

Exact parity is retained for the six live wire fields, accepted/rejected
reload categories, generation/publication changes, failed-reload isolation,
enabled behavior, cache bounds, diagnostic ownership, bounded vocabulary, and
secret redaction. Timing uses deterministic `Instant` values in resolver tests.

Rust uses a shared counter-based provider gate rather than replacing old
Tokio semaphores or introducing an actor; existing permits finish and new
leaders obey the accepted cap. Diagnostics use an implementation-specific
owner token that is not serialized. No provider/network calls occur during
policy staging or rollback.

## Verification commands and results

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r003 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r011 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/unit/test_config_reload_policy.py tests/integration/test_rehash_acceptance.py tests/integration/test_rehash_retirement_edge_cases.py tests/integration/test_rehash_streaming_swap.py tests/integration/reload/test_stale_app_state.py tests/integration/reload/test_diagnostics_contract.py tests/integration/reload/test_reload_diagnostics_assertions.py -q --tb=short --maxfail=1
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk uv run pyright src/ scripts/
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk git diff --check
```

Observed results:

- R012 focused Rust: 10 passed.
- R003/R005/R007/R009/R010/R011 focused Rust: 5, 6, 9, 6, 4, and 10 passed.
- Rust aggregate: 379 passed across 41 suites.
- Rust format and Clippy: passed with `-D warnings`.
- Python reload/config focus: 89 passed, 1 skipped.
- Python smoke: 14 passed.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Ruff format/check and diff checks: passed.
- The full migration-oracle command reached 18 passed tests before the
  pre-existing `tests/migration_rs/test_f003_config_cli.py::test_version_and_deferred_command_are_explicit`
  failure: the Python launcher emits `0.7.4` on stdout while the Rust
  candidate emits an empty version stream. This is an existing F003 CLI
  parity issue outside R012's runtime-lifecycle scope; it is recorded rather
  than masked or changed by this closure.

No paid/live provider was used.

## Resource and security review

The resolver remains one process-owned bounded instance. Cache/provider-state,
metric-label, gate, diagnostic, changed-path, and task bounds remain enforced;
no new task, scheduler, database table, provider HTTP path, or production
dependency was added. Resolver fingerprints and diagnostics contain no
credentials, proxy URLs, request/provider bodies, or unbounded exception text.
The only newly retained resolver data are bounded monotonic observation times,
and the only reload identity is an in-memory numeric token.

No unresolved high/medium M8 correctness, resource, security, compatibility,
schema, dependency, or scope finding remains. The existing F003 version
output mismatch is low-scope pre-existing CLI parity debt and does not affect
R012's runtime contracts.

## Future-plan audit and registry transition

R012 is removed from the dependency-ready table and recorded in the completed
implementation table. The runtime-lifecycle roadmap, implementation index,
handoff sequence, and registry now mark M8 closed after R012. M9 is newly
eligible for its own planning and implementation review, but no M9 plan is
created or auto-promoted. M10-M12 remain sequenced behind the long-term
roadmap; no other represented future plan has a newly satisfied direct
dependency requiring a status change.

R011 remains append-only historical aggregate evidence; it is not rewritten.
