# R005 Closure — Config Diff, Reload Policy, and Redacted Change Model

Status: closed

Recommendation: closed

Implementation commit: [`c9ee3656`](https://github.com/eggstack/eggpool/commit/c9ee3656a097addaec0982ec0d0128b1d3d2ad7d)

Plan: [R005 — config diff, reload policy, and redacted change model](../../implementation/runtime-lifecycle/005-config-diff-reload-policy-and-redaction.md)

Repository baseline: `dbd74bdfcec0e2fa87b5724e3e36c5c3d0c62007`

## Outcome

R005 adds the pure Rust reload-policy boundary in
`rust/src/config_reload_policy.rs`. It ports the complete R001 field table as
an explicit fail-closed rule set, exposes typed `ReloadDisposition`,
`ConfigChange`, and `ConfigDiff` values, and keeps live/restart-required
classification separate from candidate construction, publication, task
changes, persistence, and file I/O.

All Rust configuration structs now have a stable serde projection. The
policy's schema-coverage guard compares that projection with the 153 exported
R001 rules, so adding a Rust config field without a classification fails the
R005 test. Dynamic provider/account paths use sorted keys and semantic account
identity; model-router and other dynamic configuration remains collapsed at
the same operator-facing paths as Python.

The diff is lexicographically ordered, sections are stable and deduplicated,
mixed changes retain both live and restart-required sets, and the serialized
`ConfigDiff` includes all three projections (`changes`, `live`, and
`restart_required`). Secret fields render only `<changed>`. Recursive
collection rendering and free-text sanitization also cover credential-shaped
token/password/secret fields and proxy URI userinfo. Semantic SHA-256 digests
are independent of TOML comments/formatting, while expected-digest checking
returns a typed mismatch before any reload mutation.

## Requirement-to-evidence matrix

| R005 requirement | Evidence | Result |
|---|---|---|
| Complete R001 path/disposition parity | `runtime_lifecycle_r005::exact_r001_field_dispositions_are_ported` compares all 153 fixture rows and counts 59 LIVE / 94 RESTART_REQUIRED | Pass |
| Rust schema coverage | `serialized_schema_projection_is_the_coverage_guard` compares serde-derived leaves to the frozen table | Pass |
| Fail-closed unknown paths | `unknown_paths_and_dynamic_rules_fail_closed_or_match_oracle` | Pass |
| Live/restart/mixed policy | `live_restart_and_mixed_mutations_are_classified_without_partial_semantics` | Pass |
| R001 diff parity | `diff_projections_match_the_frozen_r001_cases` covers identical, live-only, restart-only, mixed, and secret cases | Pass |
| Deterministic dynamic maps | `provider_account_and_router_paths_are_stable`; BTreeMap/BTreeSet traversal and lexicographic final ordering | Pass |
| Secret-safe diagnostics | `secret_values_are_absent_from_all_change_projections` searches Debug, Display, direct change JSON, and ConfigDiff JSON for seeded sentinels | Pass |
| Digest/no-op semantics | `semantic_digest_and_noop_are_formatting_independent` | Pass |
| Pure policy boundary | Module contains no generation, task, DB, publication, network, or config-file mutation calls | Pass |
| Dependency scope | No Cargo dependency or database/schema change; serde derives use existing dependencies | Pass |

## Failing-before and passing-after evidence

At the R005 baseline there was no Rust policy module or R005 test target, so
the R005-specific behavior had no executable Rust implementation to qualify.
The implementation commit adds that missing boundary and its differential
tests. The passing-after commands below provide the complete qualification
evidence; existing Rust and Python suites remained green.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check       # passed
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings  # passed
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005  # 9 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets          # 331 passed, 34 suites
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1      # 89 passed, 3 skipped
rtk uv run pytest tests/unit/test_config_reload_policy.py tests/unit/test_reload_security.py -q --tb=short --maxfail=1  # 74 passed
rtk git diff --check                                               # passed
```

## Supported differences and unresolved findings

Rust uses serde's fixed struct-field order plus `BTreeMap` for the semantic
projection, rather than Python's Pydantic model walk. This is an internal
normalization: comments/formatting are ignored, dynamic paths and output order
are stable, and the R001 projections match exactly. No supported runtime
difference is introduced.

No unresolved R005 correctness, security, resource, or compatibility finding
remains. R007 owns file reading, validation transactions, expected-digest
orchestration, candidate publication, and mutation boundaries. R006 owns
process task-spec staging; neither responsibility is pulled into R005.

## Future-plan audit and registry transition

R005 is removed from the dependency-ready table and recorded in the completed
implementation table with implementation commit `c9ee3656` and this accepted
closure record. R006 is promoted to the sole dependency-ready M8 plan because
its hard dependency, accepted R005 closure, is satisfied. R007-R011 remain
queued behind their immediate predecessors. M9 remains blocked on accepted
R011 M8 closure and its separate planning/implementation review.

No other future plan can be safely unblocked by R005 alone. In particular,
R007 still requires R006's task-supervisor contract, and R008-R011 depend on
the transactional reload, background, shutdown, authority, and aggregate
qualification slices that follow. No M8 roadmap closure, M9 eligibility,
operational CLI, control socket, signal, or process-shutdown status is
promoted by this record.
