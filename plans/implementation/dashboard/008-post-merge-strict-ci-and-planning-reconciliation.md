# Dashboard Milestone 008 — Post-merge strict-CI and planning reconciliation corrective

Status: closed

Repository baseline: `299a0b3657667af509742a184e658c14df22d406`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-008--post-merge-strict-ci-and-planning-reconciliation-corrective`

Corrects / follows:

- `plans/implementation/dashboard/006-full-parity-qualification-and-closure.md`
- `plans/closure/dashboard/006-status.md`
- `plans/implementation/dashboard/007-empty-recovery-summary-correction.md`
- `plans/closure/dashboard/007-status.md`

Cross-subsystem planning reconciliation:

- `plans/registry.md`
- `plans/closure/deployment-packaging/003-status.md`

Long-term references:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Related ADRs:

- None. This pass changes no dashboard protocol, auth semantics, durable
  dependency, runtime owner, or public compatibility contract.

Primary class: invariant

## 1. Objective

Restore strict CI on the merged dashboard-parity head and reconcile the compact
planning registry after the merge without reopening completed dashboard parity
or deployment-packaging capability work.

At baseline `299a0b3657667af509742a184e658c14df22d406`, hosted CI run
`37040025250` fails at the default-feature workspace Clippy gate because
`rust/src/server/dashboard.rs` contains two nested-`if` shapes rejected by
current stable Clippy's `collapsible_if` lint under `-D warnings`.

Because that step fails, CI skips the no-default checks, Rust tests, Python
tooling setup, Ruff, Pyright, and tooling pytest. The current merge therefore
cannot be treated as fully qualified even though the dashboard parity work and
Deployment/Packaging M003 each had prior green evidence.

The same review also found stale planning-control text:

- the authoritative Deployment/Packaging registry row correctly says M003 is
  closed;
- a historical unblock-audit paragraph still says M003 is registered
  `ready`;
- the dependency-ready section is an empty table instead of representing the
  actual ready work.

M008 must correct both classes of drift and produce a green current-head CI
without changing dashboard behavior.

## 2. Why this milestone is ready

All hard/interface dependencies are already closed:

- Dashboard M001-M007 are closed.
- The parity merge is on `main`.
- The CI failure is deterministic and points to two concrete Clippy findings in
  `rust/src/server/dashboard.rs`.
- No architecture or product decision is required.
- Deployment/Packaging M003 is already closed and its planning correction is
  documentation-only.

The corrective is therefore dependency-ready and bounded to one agent pass.

## 3. Current evidence

Hosted CI run `37040025250` for baseline
`299a0b3657667af509742a184e658c14df22d406` reports:

- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`: pass;
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings`: fail;
- all later CI gates: skipped.

The two failures are both `clippy::collapsible_if` in
`rust/src/server/dashboard.rs` around the Linux load-average projection near
the current `/proc/loadavg` read:

1. nested `if let Ok(loadavg) = std::fs::read_to_string(...)` followed by
   `if let Some(load) = ...`;
2. nested parsed-load branch followed by
   `if let Ok(cpu_count) = std::thread::available_parallelism()`.

The code is observational dashboard formatting. The corrective must preserve
the existing Linux-only best-effort behavior and unavailable fallback exactly.

Planning evidence at the same baseline:

- Dashboard roadmap is marked `closed` with M001-M007 closed.
- Deployment/Packaging row correctly records M003 closed.
- The registry's earlier deployment-packaging unblock-audit paragraph still
  records M003 as `ready`, while a later audit records it closed.
- Dependency-ready table is empty.

## 4. Non-regressing invariants

- Dashboard remains observational; no routing/runtime/persistence authority
  moves into rendering code.
- Linux load-average display remains best-effort and never makes dashboard
  rendering fail when `/proc/loadavg` or CPU-count discovery is unavailable.
- Non-Linux behavior remains unchanged.
- No dashboard DOM/API/theme/oracle acceptance changes are introduced.
- No accepted dashboard parity disposition is reopened.
- Dashboard auth, escaping, redaction, bounds, and public/private semantics are
  unchanged.
- Strict Clippy with `-D warnings` remains a repository invariant; do not add
  a broad lint allowance.
- Default and `--no-default-features` builds remain equivalent to the current
  supported feature boundary.
- Deployment/Packaging M001-M003 remain closed; registry cleanup must not
  rewrite their closure records.
- Provider Transport M002 is currently ready after its upstream Eggfetch
  interface was published; Routing Selection M002 remains evidence-gated
  because no affinity workload measurement is available.

## 5. Scope

### In scope

- Rewrite the two Clippy-rejected conditional shapes in
  `rust/src/server/dashboard.rs` in the smallest semantics-preserving form.
- Add or tighten a focused regression only if existing dashboard tests do not
  already pin load-average fallback behavior.
- Run every CI gate that was skipped after Clippy failed.
- Run focused dashboard qualification appropriate to the touched code.
- Reconcile `plans/registry.md`:
  - remove/supersede the stale Deployment/Packaging M003-ready audit text;
  - represent M008 as the current dependency-ready plan while active;
  - on closure, remove M008 and restore the canonical no-ready-work form if no
    other plan is promoted.
- Reopen the dashboard roadmap only for M008 status tracking, then close it
  again when evidence passes.

### Out of scope

- Dashboard visual/DOM/API parity changes.
- New dashboard telemetry.
- Refactoring `dashboard.rs` solely for module size.
- Changing Linux load-average semantics or adding a new cross-platform system
  metrics dependency.
- Lint suppression or lowering CI strictness.
- Installer/deployment production changes.
- Provider/routing/persistence work.
- Dependency upgrades.
- Release publication changes.

## 6. Required production change

Use the current stable-Rust idiom to collapse the nested conditional chain
without changing behavior.

Expected shape is equivalent to:

```rust
if let Ok(loadavg) = std::fs::read_to_string("/proc/loadavg")
    && let Some(load) = loadavg
        .split_whitespace()
        .next()
        .and_then(|value| value.parse::<f64>().ok())
    && let Ok(cpu_count) = std::thread::available_parallelism()
{
    // existing formatting/return
}
```

The exact expression may differ if it improves readability, but it must:

- read `/proc/loadavg` at most once;
- use the first load-average field exactly as today;
- parse to the same numeric type;
- use the same CPU-count source;
- return the same formatted value when all observations succeed;
- use the same unavailable/fallback result on any failed observation.

Do not add `#[allow(clippy::collapsible_if)]` unless a concrete semantic or
readability reason proves the collapsed form is materially worse; current
evidence does not justify an allowance.

## 7. Ordered work packages

### Work package A — Freeze the CI failure

Intent:

Confirm the corrective addresses the observed failure, not an unrelated local
state.

Required evidence:

- record baseline CI run `37040025250`;
- retain the exact two `collapsible_if` diagnostics in closure evidence;
- run focused default-feature Clippy before editing if practical, or use hosted
  CI logs as the baseline reproduction.

### Work package B — Apply the minimal dashboard code correction

Intent:

Make current stable Clippy green without behavior change.

Required changes:

- collapse/restructure only the offending load-average conditional chain;
- preserve all return/fallback semantics;
- do not alter public dashboard rendering or assets.

Acceptance evidence:

- focused dashboard tests pass;
- default workspace Clippy passes with `-D warnings`;
- no new warnings are hidden.

### Work package C — Execute the gates skipped by CI

Intent:

Prove the merge head, not merely the two changed lines, is healthy.

Required commands:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Focused dashboard command:

```bash
cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
```

If existing tests do not exercise the load-average helper/fallback, add the
smallest deterministic unit seam possible. Do not introduce host-dependent
tests that assume `/proc/loadavg` exists.

Dashboard browser/oracle parity need not be rerun for a proven formatting-only
conditional rewrite unless production output changes. Closure must state why it
was or was not run.

### Work package D — Reconcile planning control state

Intent:

Make the registry describe current reality once.

Required changes:

- remove or rewrite the stale historical paragraph that says
  Deployment/Packaging M003 is registered `ready`;
- retain the later M003 closed audit as the authoritative disposition;
- register Dashboard M008 as `ready` before handoff and `active`/closed
  through normal lifecycle commits;
- update dashboard roadmap status from `closed` to `active` while M008 is
  open;
- add M008 to the dashboard milestone status section;
- at closure, return dashboard roadmap to `closed` if no successor exists;
- leave Deployment/Packaging M001-M003 closure records untouched.

Do not duplicate implementation requirements into the registry.

### Work package E — Closure and unblock audit

Closure must not occur until:

- local/hosted strict Clippy is green;
- the previously skipped CI gates have completed successfully on the
  corrective head;
- no new medium-or-higher dashboard finding is introduced;
- registry and dashboard roadmap statuses agree;
- blocked/eligible work is audited.

## 8. Failure, cancellation, restart, and contention semantics

No runtime state-machine behavior changes.

The touched dashboard path is read-only observational formatting. Failure to
read Linux load average or CPU parallelism continues to degrade to the existing
fallback and must not affect request serving, process lifecycle, or dashboard
availability beyond that field.

No new async tasks, locks, persistence, filesystem mutation, or cancellation
paths are introduced.

## 9. Compatibility and migration

No API, DOM, config, schema, storage, protocol, or migration change.

The Rust source shape changes only to satisfy current strict Clippy. Operator
output must remain byte/semantically equivalent for the affected field.

Planning-only registry edits have no runtime effect.

## 10. Required tests and guards

At minimum:

- existing dashboard unit tests;
- full default workspace Clippy;
- no-default check + Clippy;
- full serial workspace tests;
- full Python tooling suite;
- formatting/type/static checks;
- hosted CI on the corrective/closure head.

Add a focused dashboard test only if there is no existing guard for:

- valid loadavg + CPU count -> expected formatted utilization/load text;
- unavailable/invalid observation -> existing fallback.

Do not make such a test depend on the CI host's actual load average.

## 11. Documentation updates

Required:

- `plans/subsystems/dashboard-roadmap.md`;
- `plans/registry.md`;
- M008 closure record when implemented.

No operator/architecture documentation change is expected because behavior is
unchanged. If implementation changes observable output, stop and reassess
scope rather than silently updating parity documentation.

## 12. Acceptance criteria

1. Current default-feature workspace Clippy passes with `-D warnings`.
2. Both baseline `collapsible_if` findings are gone without lint suppression.
3. Dashboard load-average success/fallback semantics are unchanged.
4. Focused dashboard Rust tests pass.
5. No-default check and strict Clippy pass.
6. Full serial Rust workspace tests pass.
7. Ruff format/check, Pyright, and tooling pytest pass.
8. Hosted CI for the M008 implementation/closure head completes successfully;
   no gate remains skipped due to an earlier failure.
9. No dashboard DOM/API/theme/oracle contract changes occur.
10. Registry no longer contains an operative stale statement that
    Deployment/Packaging M003 is `ready`.
11. Dashboard roadmap and registry consistently identify M008 while open and
    return to closed/no-successor state on closure if no new work is promoted.
12. Existing blocked/evidence-gated work remains correctly classified after
    unblock audit.

## 13. Stop conditions

Stop and report rather than broadening scope if:

- satisfying Clippy changes dashboard output or platform semantics;
- additional compiler/Clippy failures reveal a materially larger production
  problem than localized merge cleanup;
- full tests expose dashboard parity/runtime regressions requiring behavior
  changes;
- current repository state has already fixed the two Clippy diagnostics;
- registry reconciliation exposes contradictory closure records rather than
  stale control-surface prose.

If unrelated CI failures occur after the Clippy fix, record them separately and
decide whether they are a new corrective rather than folding them into M008.

## 14. Closure evidence required

The M008 closure record must include:

- implementation commit(s);
- baseline CI run `37040025250` and the two exact Clippy diagnostics;
- code-diff statement demonstrating behavior-preserving scope;
- focused dashboard test result;
- default and no-default Clippy/check results;
- full serial Rust test result;
- tooling/Ruff/Pyright results;
- hosted CI run ID and final conclusion;
- statement on whether browser/oracle qualification was rerun and why;
- registry/roadmap reconciliation evidence;
- migration/security/failure-semantics review;
- severity-tagged unresolved findings;
- unblock audit and final disposition.

## 15. Handoff notes

Treat this as post-merge qualification debt, not a dashboard feature pass.

The desired code change should be tiny. The larger requirement is evidence:
the baseline CI failure caused every gate after default Clippy to be skipped,
so M008 cannot close merely because Clippy compiles locally.

Do not reopen Dashboard M001-M007 or Deployment/Packaging M001-M003. Preserve
their closure records as historical evidence and add M008 as the corrective
layer.

### Status update — hosted oracle history

Local M008 gates passed, and hosted CI run `37047638579` passed the Rust
qualification gates. Its tooling pytest gate then failed because the default
depth-one checkout did not contain the pinned oracle commit required by
`test_frozen_manifest_identity_inventory_and_asset_blobs`. This is outside
the original source correction. Dashboard M011 is registered to make the
pinned commit available without changing the test or oracle. M011 is now
closed, and hosted run `37049147155` completed all gates successfully; M008 is
active again for its final closure pass.
