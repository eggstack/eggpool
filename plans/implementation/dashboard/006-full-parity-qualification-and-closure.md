# Dashboard Milestone 006 — Full Parity Qualification and Closure

Status: active

Repository baseline: 17e298f64fa21589f558c43592a24fa91b952ff7 plus conditionally closed Dashboard M001-M005

Source roadmap:

- plans/subsystems/dashboard-roadmap.md#milestone-006--full-dashboard-parity-qualification-and-closure

Long-term requirements:

- plans/000-long-term-specification.md
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md
- plans/003-planning-process.md

Applicable ADRs:

- None required unless M001-M005 exposed and accepted a new architecture decision; any such ADR must already be accepted before M006 begins.

Primary class: polish

## 1. Objective

Run a strict, like-for-like qualification of the restored native Rust dashboard against the frozen final Python oracle, close residual defects, remove migration-era dead/duplicated dashboard code, and reconcile architecture/operator documentation.

This milestone is a closure gate, not a venue for redesign.

## 2. Why this milestone is ready

M005 is conditionally closed with explicit source-truth dispositions; M003 and M004 are closed. M001 remains the unchanged oracle authority and M002 the common shell/API foundation. M006 is ready to qualify the remaining strict gaps, matched browser behavior, and shutdown/restart boundary.

When the prerequisite capability milestones close, M006 has no expected external runtime dependency. Browser tooling remains qualification-only.

## 3. Current implementation evidence

At roadmap creation the dashboard cannot qualify because the Rust DOM/API/view-model surface is materially reduced despite preserved asset bytes. M001-M005 are expected to restore that surface in bounded slices.

Historical Q012 cannot serve as final evidence because:

- its comparator selected a subset of semantic content and explicitly permitted richer Python diagnostics;
- its browser evidence used Python desktop/default and Rust mobile/Catppuccin combinations rather than matched pairs;
- screenshot artifacts were external and not a complete enduring parity contract.

M006's matched browser audit identified a narrow mobile overflow correction:
the frozen CSS allowed a panel's intrinsic minimum width to expand around its
max-content table despite the historical `.table-scroll` wrapper. Rust sets
`.panel { min-width: 0; }`, preserving the wrapper's horizontal scroll and
preventing root-page horizontal scrolling. This is a single, hashed CSS rule
change with an explicit oracle/candidate asset disposition; all other
dashboard CSS, JavaScript, and Chart.js bytes remain frozen.

M006 must use the stricter M001 oracle and matched-pair methodology.

## 4. Invariants that must not regress

- No oracle weakening to obtain green results.
- No CSS/JS redesign or broad asset change inside closure.
- No Python runtime fallback.
- Dashboard remains read-only, bounded, escaped, secret-free, and isolated from inference correctness.
- Public/private auth boundaries remain explicit.
- Full default and --no-default-features Rust build/lint gates remain green.
- Browser dependencies stay out of Cargo/release artifacts.

## 5. Scope

### In scope

- Full 14-route empty/populated/escaping/error/private contract matrix.
- Full restored dashboard JSON endpoint matrix.
- Asset/theme inventory/hash parity.
- Same-state, same-route, same-theme, same-viewport oracle/candidate browser captures.
- Desktop and mobile navigation/overflow interaction.
- Chart/static/grouped/progressive hydration and refresh.
- Console, failed fetch/static request, duplicate ID, invalid DOM, accessibility hook, and JS target checks.
- Residual renderer/view-model/API defects revealed by qualification.
- Removal of dead migration-only renderer paths/helpers that are no longer referenced.
- Final architecture/development/operator docs and planning closure record.

### Explicitly out of scope

- New UI features or visual redesign.
- Rebaselining the oracle to current Rust.
- Broad runtime/provider/routing changes.
- Performance optimization not required to fix a measured dashboard regression.
- New frontend dependency/framework.

## 6. Required production changes

Only bounded corrective production changes needed to satisfy the already-defined M001 contract and prior milestone acceptance criteria.

Audit renderer organization after parity restoration. Remove duplicate full-document builders, unreachable placeholder code, stale hard-coded migration values, and dead helpers/assets only when the M001 JS/CSS/DOM inventory proves they are not part of the historical contract.

Do not delete preserved frontend CSS/JS rules merely because a particular deterministic fixture does not exercise them.

## 7. Ordered work packages

### Work package A — Complete machine parity matrix

Intent: prove every mandatory contract cell.

Required changes:

- Run all 14 pages across empty/populated/escaping/long/error/missing/private states where applicable.
- Run every dashboard-only JSON route across success/error/auth/query/bounds cases.
- Compare full M001 DOM/API projections, not selected cards/tables.
- Verify assets/themes and JS selector/API producer coverage.

Acceptance evidence:

- Zero unexplained mandatory mismatch.
- Any intentional semantic divergence is documented, source-justified, and separately approved rather than normalized away.

### Work package B — Matched browser qualification

Intent: establish visual/interactive equivalence rather than smoke-test each implementation differently.

Required changes:

- Capture oracle and Rust with identical route, state, theme, viewport, device scale factor, font/runtime assumptions, and deterministic data.
- Cover every major page at least once in both desktop and mobile across the bounded set; include dense tables, charts, model detail, Runtime, Cache, long/Unicode, and empty/error states.
- Record image hashes/dimensions and manual/automated disposition.
- Fail on console exception, failed same-origin fetch/static resource, duplicate ID, invalid chart target, or unexpected horizontal/body overflow.

Acceptance evidence:

- No unexplained material visual regression.
- Intended dense-table horizontal overflow remains contained to its historical scroll region.

### Work package C — Residual corrective pass

Intent: fix qualification findings without moving the goalposts.

Required changes:

- Classify every mismatch by severity and owner.
- Correct dashboard-owned defects.
- If a mismatch belongs to another subsystem or requires new architecture, stop/record blocker rather than weakening the oracle.
- Re-run the smallest focused matrix after each correction, then the full matrix.

Acceptance evidence:

- No medium+ unresolved dashboard-owned defect.
- No oracle or normalization change made solely to obtain pass.

### Work package D — Cleanup and documentation reconciliation

Intent: leave the repository easier to maintain than the migration intermediate.

Required changes:

- Remove obsolete duplicated renderer paths/placeholders and stale migration comments/tests superseded by the new guard.
- Keep historical migration/closure records immutable.
- Update architecture/deep-dive-dashboard.md, architecture/overview.md if needed, operator/API docs, and development skill focused-target index to current truth.
- Ensure the new dashboard module split and test ownership are documented without copying implementation detail into AGENTS.md.

Acceptance evidence:

- Current docs match shipped route/API/data ownership.
- docs-only references do not claim historical Python is a runtime dependency.

### Work package E — Closure and unblock audit

Intent: formally close the roadmap only on evidence.

Required changes:

- Write plans/closure/dashboard/006-status.md with implementation commits, requirement-to-evidence matrix, exact commands/results, security/auth/privacy review, visual evidence, unresolved findings, and disposition.
- Audit registry for any dashboard corrective work revealed by closure.
- Close the roadmap only if all milestone closures are accepted and no successor remains required.

Acceptance evidence:

- registry/roadmap/closure agree on status.
- No blocked dashboard item is silently dropped.

## 8. Failure, cancellation, restart, contention semantics

Qualification tooling must clean up browser/server/temp worktree processes on interruption. A failed capture/test must remain a failure; do not silently retry until a flaky visual passes without recording the instability.

Production dashboard failure semantics remain those established in M002-M005. M006 must include at least one shutdown/restart run proving richer dashboard/browser activity does not delay bounded server/process closure.

## 9. Compatibility and migration

No new migration. This milestone validates compatibility achieved by prior milestones.

The checked-in M001 oracle remains frozen. Future intentional dashboard redesign after roadmap closure requires a new plan that explicitly supersedes selected oracle facts and updates the compatibility baseline; do not mutate historical evidence in place.

## 10. Required tests

Machine matrix:

- every page route;
- every dashboard JSON route;
- static/theme assets;
- public/private auth;
- query validation/bounds;
- escaping/Unicode/long values;
- empty/populated/error/missing states;
- complete DOM structure/classes/IDs/data/form/link/content order;
- JS selector/API producer coverage;
- secret/raw-content sentinel scan.

Browser matrix:

- matched oracle/candidate pairs;
- desktop/mobile;
- representative themes including default/Cyber Red, a light theme, and a structurally distinct dark theme;
- nav/theme/period/filter/number-stepper/copy/refresh interactions;
- Chart.js static/grouped/progressive/timeseries rendering;
- zero unexplained console/network errors;
- no duplicate chart/timer behavior after repeated refresh.

Runtime safety:

- dashboard reads during shutdown/reload remain bounded;
- no inference/provider behavior regression.

## 11. Required verification commands

Focused dashboard/tooling:

    cargo test --manifest-path rust/Cargo.toml --test dashboard_parity -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
    uv sync --frozen
    uv run ruff format --check scripts/ tests/tooling/
    uv run ruff check scripts/ tests/tooling/
    uv run pyright scripts/
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1

Run the M001 parity script in its strict full-matrix mode and browser mode with an explicit output directory. Record exact commands and browser/runtime versions in closure.

Full repository:

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    cargo build --manifest-path rust/Cargo.toml --locked --release
    git diff --check

Cargo deny is required only if this line unexpectedly changes Cargo.toml/Cargo.lock; adding a production browser/frontend dependency is a stop condition, not a routine dependency change.

## 12. Documentation updates

- architecture/deep-dive-dashboard.md: exact route/API/render/view-model/auth/static ownership and verification.
- architecture/overview.md: module map if dashboard files were decomposed.
- docs/: dashboard/API/operator behavior and qualification notes where appropriate.
- .opencode/skills/development/SKILL.md: stable focused dashboard test target/tooling command once it exists.
- plans/subsystems/dashboard-roadmap.md and plans/registry.md: close only with accepted closure record.

## 13. Acceptance criteria

- All 14 page routes match the frozen oracle's meaningful complete DOM contract.
- All historical dashboard-only JSON routes required by the retained frontend are restored or have an explicitly approved semantically equivalent compatibility result.
- Static frontend assets remain the frozen bytes unless a separately justified corrective proves otherwise.
- Matched browser pairs show no unexplained material desktop/mobile/theme regression.
- No console exception, failed dashboard fetch/static resource, duplicate ID, invalid chart target, or timer/chart duplication occurs.
- Runtime/cache/model/telemetry values are authoritative or explicitly unavailable, never fabricated.
- Security/auth/privacy/bounds and default/no-default repository gates pass.
- No medium+ dashboard-owned finding remains.

## 14. Stop conditions

Stop and report rather than improvise when:

- any prerequisite milestone is not closed;
- passing requires weakening the M001 oracle/comparator;
- a mismatch requires changing runtime/provider/routing ownership outside prior plan scope;
- historical behavior is demonstrably unsafe and needs a new product decision;
- visual evidence is non-deterministic because fixture/fonts/browser cannot be stabilized enough for useful comparison;
- production browser/frontend dependencies are proposed.

## 15. Closure evidence required

plans/closure/dashboard/006-status.md must contain:

- implementation commits for M001-M006;
- complete requirement-to-evidence matrix;
- oracle commit/hash/projection version;
- route/API/static/theme matrix results;
- browser pair manifest with route/state/theme/viewport/hash/disposition;
- console/network/DOM/JS-hook results;
- security/auth/privacy/bounds review;
- exact focused/full/no-default/tooling commands and results, with CI versus local truth;
- performance/resource note for dashboard query/render impact;
- severity-tagged unresolved findings;
- disposition and explicit roadmap/registry unblock audit.

## 16. Handoff notes

Do not repeat the Q012 mistake of using different viewport/theme combinations for Python and Rust and calling the result visual parity.

The final question is simple: given the same sanitized state and the same browser conditions, does the Rust dashboard present and behave like the final Python dashboard while using current Rust authorities underneath?
