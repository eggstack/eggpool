# Dashboard Milestone 011 — Hosted oracle history qualification

Status: active

Repository baseline: `3c40e34f`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-011--hosted-oracle-history-qualification`

Corrects / follows:

- `plans/implementation/dashboard/008-post-merge-strict-ci-and-planning-reconciliation.md`
- `plans/closure/dashboard/006-status.md`
- `tests/tooling/test_dashboard_parity_projection.py`

Long-term references:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Related ADRs:

- None. This changes only CI checkout history availability for a test that verifies a pinned, public source commit.

Primary class: invariant

## 1. Objective

Make the hosted CI checkout retain the pinned dashboard-oracle commit needed by `test_frozen_manifest_identity_inventory_and_asset_blobs`, then rerun the complete CI workflow so M008 can close on green hosted evidence.

## 2. Why this milestone is ready

The M008 implementation is committed and its local default/no-default Rust gates and tooling suite pass. Hosted CI run `37047638579` passed the Rust gates but failed when `git show c23a70961f4b7858fdb0264cfb27b7ea26a8a334:src/eggpool/dashboard/static/dashboard.css` could not find the pinned commit in the default depth-one checkout. The focused test succeeds locally where repository history is available. The required change is bounded to CI history availability; no dashboard behavior or oracle data needs to change.

## 3. Current implementation evidence

- `.github/workflows/ci.yml` uses `actions/checkout@v4` with the default shallow history.
- `scripts/qualification_dashboard_parity.py::build_oracle_manifest` verifies historical source asset blobs using `git show`.
- `tests/tooling/test_dashboard_parity_projection.py::test_frozen_manifest_identity_inventory_and_asset_blobs` exercises that verification.
- The oracle source remains pinned at `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`.

## 4. Invariants that must not regress

- Hosted CI must exercise the same frozen oracle manifest and blob identities as local qualification.
- Do not weaken or skip the historical blob test.
- Do not modify the oracle manifest, captures, or comparator to hide missing history.
- This change must not alter production code, dashboard routes, DOM, assets, auth, or parity dispositions.

## 5. Scope

### In scope

- Configure the CI checkout to fetch history containing the pinned oracle commit.
- Run the focused manifest test and the complete hosted CI suite.
- Record the new hosted run as evidence for M008.

### Explicitly out of scope

- Rewriting qualification to use a different source of oracle bytes.
- Changing oracle fixtures or accepted M006 differences.
- Changing production dashboard behavior or assets.
- General CI performance or caching work.

## 6. Required production changes

No production changes. Adjust only `.github/workflows/ci.yml` checkout history depth so the pinned Git object is available to the required test.

## 7. Ordered work packages

### Work package A — Make oracle history available

Intent: allow the existing test to inspect the pinned source tree in hosted CI.

Required changes: configure the checkout action to fetch the repository history needed to include the M001 oracle commit.

Acceptance evidence: `git cat-file -e c23a70961f4b7858fdb0264cfb27b7ea26a8a334^{commit}` succeeds in the workflow environment.

### Work package B — Requalify CI

Intent: prove the prior test failure is resolved and all subsequent gates execute.

Required changes: run the focused manifest test locally and trigger full hosted CI on the corrective head.

Acceptance evidence: the focused test passes; hosted CI completes with every Rust and tooling gate green.

## 8. Failure, cancellation, restart, and contention semantics

No runtime semantics change. A missing oracle object remains a failing qualification condition.

## 9. Compatibility and migration

No product compatibility or migration effect. CI downloads additional Git history only.

## 10. Required tests

```bash
uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short --maxfail=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Hosted CI must also pass the complete command sequence in `.github/workflows/ci.yml`.

## 11. Required verification commands

```bash
git diff --check
```

The hosted workflow is the CI checkout-history verification environment.

## 12. Documentation updates

- `plans/subsystems/dashboard-roadmap.md`.
- `plans/registry.md`.
- `plans/closure/dashboard/011-status.md`.

## 13. Acceptance criteria

1. Hosted checkout contains the pinned oracle commit.
2. The frozen-manifest blob identity test passes without changes to its assertion or fixture.
3. All hosted CI gates complete successfully.
4. No runtime, oracle, asset, API, DOM, auth, or parity behavior changes.
5. M008 remains blocked until this evidence is green, then is closed in its own closure commit.

## 14. Stop conditions

Stop and reassess if the pinned commit is unavailable from the configured origin, the manifest test requires weakening, or fetching history causes a material CI reliability/performance problem that needs a separate decision.

## 15. Closure evidence required

- Implementation commit.
- Exact workflow change and the reason the default checkout omitted the object.
- Focused test result.
- Hosted run ID and full conclusion, showing no skipped gates.
- `git diff --check` result.
- Explicit statement that oracle data/comparator and production behavior are unchanged.
- Registry/roadmap reconciliation and M008 unblock audit.
- Severity-tagged unresolved findings and disposition.

## 16. Handoff notes

Do not alter the frozen oracle to accommodate checkout depth. Keep M008 blocked until the complete hosted rerun succeeds; then promote M009 only in the M008 closure commit.
