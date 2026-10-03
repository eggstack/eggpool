# Dashboard Milestone 011 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/011-hosted-oracle-history-qualification.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-011--hosted-oracle-history-qualification`

Repository baseline reviewed: `3c40e34f`

Implementation commits or pull requests:

- `30b8282a` — fetch full history in hosted CI so the pinned oracle commit is available to the existing identity test.

## 1. Executive finding

M011 is closed. Hosted CI now has the Git object needed by the frozen dashboard manifest test; the existing test, oracle, and comparator remain unchanged. Full CI run `37049147155` completed successfully with every Rust and tooling gate passing.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Hosted checkout contains the pinned oracle source commit | `.github/workflows/ci.yml` sets `actions/checkout@v4` `fetch-depth: 0`; manifest blob test passed in run `37049147155` | pass | The test reads the pinned `c23a70961f4b7858fdb0264cfb27b7ea26a8a334` CSS blob through `git show`. |
| Preserve strict oracle and blob assertions | `tests/tooling/test_dashboard_parity_projection.py` unchanged; focused projection suite | pass | 26 passed. |
| Full hosted qualification | GitHub Actions CI run `37049147155` | pass | Every configured workflow step completed successfully; no gate skipped. |
| No production/parity behavior changes | Commit diff contains only CI checkout depth and plan/registry/closure updates | pass | No Rust, fixture, comparator, or asset changes. |

## 3. Production implementation evidence

No production implementation changed. The sole functional change is to CI checkout history depth; it allows an already required qualification test to inspect the pinned historical source blob.

## 4. Verification executed

### Commands run

```bash
uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short --maxfail=1
git diff --check
```

Hosted run `37049147155` executed the complete workflow sequence from `.github/workflows/ci.yml`: formatting, default strict Clippy, no-default check and strict Clippy, full serial Rust workspace tests, uv sync, Ruff format/check, Pyright, and tooling pytest.

### Results

- Focused dashboard projection tests: 26 passed.
- Hosted Rust and tooling gates: all passed; run `37049147155` concluded success.
- `git diff --check`: passed.
- The prior run `37047638579` failed only because its depth-one checkout did not include the pinned historical commit; this is the direct reason for M011.

## 5. Invariant review

- The oracle commit, manifest, captures, comparator strictness, and accepted M006 mismatch set are unchanged.
- The historic source blob test remains enabled and passed in hosted CI.
- No dashboard runtime, API, DOM, asset, authentication, or data-ownership behavior changed.

## 6. Failure and recovery review

No runtime failure, cancellation, restart, persistence, or contention semantics changed. CI fails closed if the pinned source object or its asset blob is unavailable.

## 7. Migration and compatibility review

No product migration or compatibility effect. The workflow fetches full Git history for CI qualification only.

## 8. Security review

No credentials or request data are added to CI artifacts. The pinned oracle is public repository history and the same secret-free test fixture remains in use.

## 9. Documentation and operations

The dashboard roadmap, active registry, implementation plan, and this closure record describe the hosted-history dependency and its resolution. No operator documentation changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M011 finding | None | None |

## 11. Roadmap disposition

Milestone closed. M008's hosted qualification blocker is satisfied; reactivate M008 in its own lifecycle status commit, then complete its closure and unblock M009 if all M008 evidence remains green.

## 12. Registry updates

`plans/registry.md` removes M011 from dependency-ready work, records M011 as recently closed, and audits M008 as eligible to resume. `plans/subsystems/dashboard-roadmap.md` marks M011 closed and links this closure record. Applied in the same commit.
