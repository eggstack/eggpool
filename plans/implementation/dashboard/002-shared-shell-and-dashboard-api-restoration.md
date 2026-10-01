# Dashboard Milestone 002 — Shared Shell, Interaction, and Dashboard API Restoration

Status: closing

Repository baseline: 286b70ab3aef6b3979a033f8ee5d8f72006ffa56 plus closed Dashboard M001 oracle contract

Source roadmap:

- plans/subsystems/dashboard-roadmap.md#milestone-002--shared-shell-interaction-and-dashboard-json-contract-restoration

Long-term requirements:

- plans/000-long-term-specification.md
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md
- plans/003-planning-process.md

Applicable ADRs:

- None required. This milestone restores historical dashboard behavior under the existing native server/auth/runtime ownership.

Primary class: capability

## 1. Objective

Restore the final Python dashboard's common page shell and JavaScript-facing dashboard API/DOM contract in Rust before page-specific information-density work proceeds.

The unchanged embedded CSS/JS must once again receive the element types, IDs, classes, data hooks, forms, routes, and JSON shapes it was written against. Fix current invalid/repaired DOM rather than changing the frontend assets to accept the migration intermediate.

## 2. Why this milestone is ready

Dashboard M001 closed with the immutable Python oracle, complete DOM/API
projections, and strict comparator in `plans/closure/dashboard/001-status.md`.

There is no additional hard external dependency. The current EggServe/Axum
server boundary, dashboard auth policy, SQLite read path, and embedded assets
are already production-owned and stable.

## 3. Current implementation evidence

At the research baseline:

- rust/src/server/dashboard.rs contains both a shared render_dashboard_layout path and a separate full-document render_overview path, allowing shell drift.
- The overview account block can place a complete table or paragraph inside an outer tbody.
- The retained dashboard.js function reinitTimeseriesChart treats #timeseries-chart as a Chart.js canvas, but current render_timeseries_page emits that ID on a section.
- dashboard.js fetches /api/timeseries and /api/timeseries/grouped. Current rust/src/server/mod.rs does not register those endpoints.
- Historical Python registered both routes and rendered grouped/static/progressive chart hooks, top-nav data attributes/tooltips, theme/period controls, and auto-refresh behavior consumed by the same JS/CSS bytes.
- Current static assets are already the historical assets and should be held fixed.

## 4. Invariants that must not regress

- rust/src/server remains an HTTP/read-plane adapter; no coordinator/routing/retry/finalization logic moves into dashboard code.
- Dashboard public/private auth semantics stay unchanged.
- /v1/*, /api/integrations/*, /api/status, update/runtime control endpoints do not inherit dashboard-public access.
- Dynamic markup and JSON remain escaped and secret-free.
- No second SQLite pool/gate or unbounded dashboard query is added.
- dashboard.css, dashboard.js, chart.umd.min.js, favicon.svg, and theme assets remain byte-identical unless M001 proves the historical asset copy itself is corrupt.
- Frontend routes are restored to the JS contract; JS is not edited merely to hide missing Rust behavior.

## 5. Scope

### In scope

- One canonical shared HTML layout used by every dashboard page, including Overview.
- Historical topbar/burger/nav/theme/period/refresh/footer/update indicator/autorefresh DOM and behavior.
- Valid page/body/table markup that does not rely on browser repair.
- Historical lazy Chart.js inclusion/preload decisions.
- Restoration of GET /api/timeseries and GET /api/timeseries/grouped with the historical dashboard JSON field names, query validation, bounds, and dashboard auth class.
- Correct chart element types and bootstrap JSON/data attributes required by dashboard.js.
- Common shell/render helper decomposition under rust/src/server/dashboard/ if needed to prevent re-growth of one monolithic file.
- Focused browser assertions for no console error, missing static resource, failed same-origin dashboard fetch, duplicate ID, or chart initialization type error.

### Explicitly out of scope

- Full Overview/Accounts/Models content parity beyond shell/structural corrections required to make the common layout valid.
- Reliability/routing/traces rich diagnostic content.
- Runtime/cache rich telemetry and their additional stats APIs.
- CSS/visual redesign.
- Changes to inference or provider APIs.

## 6. Required production changes

Establish one shared dashboard renderer boundary. A directory split is preferred if it makes ownership clearer, for example rust/src/server/dashboard/{mod,routes,assets,view_model,render/...}. Exact filenames may vary; do not use refactoring itself as the acceptance criterion.

Restore shell semantics from the oracle:

- document/head/static/theme links;
- egg background;
- burger/topnav/menu classes and aria controls;
- active navigation;
- theme selector attributes and persisted period;
- page period selector behavior;
- update indicator/copy hooks where historically part of the shared layout;
- footer/updated state;
- auto-refresh script behavior;
- conditional Chart.js preload/script.

Restore dashboard-only JSON handlers required by the unchanged JS. Query parsing must keep the current bounded period set and add the historical grouped parameters only with explicit validation/bounds. Reuse current DB/read models or add bounded dashboard projection functions; do not expose raw request content.

Correct #timeseries-chart and related chart hooks to the exact expected element types. Ensure all rendered tables/panels are valid HTML.

## 7. Ordered work packages

### Work package A — Canonical shell and renderer boundary

Intent: remove shell drift and invalid duplicated full-document builders.

Required changes:

- Port historical shared layout/nav/period/theme/update/autorefresh semantics.
- Route every page through the common layout, including overview.
- Preserve active nav/query propagation and accessibility hooks.
- Decompose renderer helpers enough that page milestones can reuse them without duplicating full documents.

Acceptance evidence:

- M001 full-DOM comparator passes the common shell projection on all page routes.
- No duplicate IDs or invalid table nesting in browser-parsed DOM.
- Static assets remain hash-identical.

### Work package B — Timeseries dashboard JSON compatibility

Intent: satisfy the frontend fetch contract rather than rewriting JS.

Required changes:

- Register /api/timeseries and /api/timeseries/grouped in the dashboard route set.
- Match historical success/error/query/auth shapes and required field names such as bucket, request_count, error_count and grouped series/bucket/point totals.
- Apply explicit bounded row/bucket/group limits.
- Keep responses dashboard-observational and secret-free.

Acceptance evidence:

- Oracle API projection passes for empty/populated/invalid-query/private/public cases.
- Browser network log shows no failed dashboard timeseries request.

### Work package C — Chart/bootstrap hook restoration

Intent: make the retained JS operate on the intended DOM.

Required changes:

- Restore canvas/script/shell types, IDs, classes, data-chart-* attributes, period metadata, and grouped/static chart bootstrap structures required by dashboard.js.
- Ensure lazy Chart.js loading remains correct and no interval is multiplied by content refresh/replacement.
- Preserve no-chart-page avoidance of the ~200 KB Chart.js asset where historical behavior did so.

Acceptance evidence:

- Browser fixture initializes the chart without type exception.
- Repeated refresh/reinit does not create duplicate canvases or additional refresh intervals.
- M001 JS-selector-to-producer map has no missing producer for the restored common/timeseries surface.

### Work package D — Focused interaction/browser gate

Intent: qualify behavior that a string/DOM comparator cannot prove.

Required changes:

- Add qualification-only browser coverage for desktop/mobile nav toggle, theme submit, period change, manual refresh, dashboard auto-refresh, timeseries hydration/refresh, and grouped control updates where part of the restored shared surface.
- Fail on console exception, failed same-origin fetch/static load, duplicate ID, or missing Chart.js target.

Acceptance evidence:

- Same-state oracle/candidate interaction facts match.
- No frontend error is suppressed merely because the visible page looks acceptable.

## 8. Failure, cancellation, restart, contention semantics

Dashboard JSON/read failures remain local bounded 4xx/5xx responses and never alter inference/runtime state. Auto-refresh or chart refresh failure may show a bounded unavailable state but must not spin/retry aggressively, stack intervals, or retain detached-DOM tasks.

Server shutdown/cancellation semantics remain owned by existing EggServe/Axum lifecycle. This milestone must not add background Rust dashboard tasks.

Concurrent dashboard requests may share immutable/static assets and authoritative read owners only; renderer state remains request-local.

## 9. Compatibility and migration

No database schema/config migration is expected.

Restore historical dashboard endpoints as compatibility reads. If a route name now collides with a non-dashboard operational endpoint or would broaden auth unexpectedly, stop and resolve the ownership conflict rather than silently aliasing it.

Do not remove current /api/stats/summary behavior; page/API restoration is additive to current supported surfaces unless the oracle proves exact historical aliasing is required.

## 10. Required tests

Focused Rust coverage should include a dedicated dashboard parity/integration target or focused additions to server_transport for:

- all dashboard pages use the common shell;
- public/private auth;
- valid period and invalid period;
- theme fallback/selection;
- shell link/control/aria/data hooks;
- static/theme content types and asset hashes;
- /api/timeseries empty/populated/invalid/auth;
- /api/timeseries/grouped group_by/metric/filter/bounds cases;
- timeseries canvas element type and bootstrap payload safety;
- HTML escaping and JSON script escaping;
- valid table structure and duplicate-ID guard;
- degraded DB read response does not expose error internals.

Tooling/browser coverage from M001 must add the interaction/network/console gates above.

## 11. Required verification commands

Focused:

    cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test dashboard_parity -- --test-threads=1
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1

If dashboard_parity is implemented under a differently named target, record the exact target in closure and update the development skill only when it becomes a stable repository convention.

Repository gates:

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

## 12. Documentation updates

- Update architecture/deep-dive-dashboard.md only for shared shell/API behavior actually restored.
- Document restored dashboard-only JSON endpoints and auth classification in the appropriate operator/API docs.
- Update the dashboard asset/runtime manifest if route/asset contract guards require it; do not change asset bytes without separate evidence.

## 13. Acceptance criteria

- Every dashboard page uses one shared historical-compatible shell.
- Browser-parsed DOM is valid and no longer relies on nested-table repair.
- /api/timeseries and /api/timeseries/grouped are available with the expected bounded dashboard contract.
- dashboard.js has no missing producer for common/timeseries selectors or fetches.
- The chart target has the correct element type and initializes.
- Common interactions work on matched desktop/mobile fixtures with zero unexplained console/network errors.
- Static frontend asset bytes remain unchanged.

## 14. Stop conditions

Stop and report rather than improvise when:

- M001 oracle is not closed/stable;
- satisfying the old JS would require weakening auth or exposing secret/request content;
- a historical endpoint conflicts with a current non-dashboard authority;
- an asset edit is proposed only because Rust markup/API is incomplete;
- restoring grouped data would require an unbounded query or second DB owner;
- the work expands into page-specific rich view models assigned to M003-M005.

## 15. Closure evidence required

- implementation commits;
- M001 shell/API/JS-hook parity report before/after;
- endpoint matrix with auth/query/bounds;
- asset hash proof;
- valid-DOM/duplicate-ID/escaping evidence;
- matched browser interaction run with console/network disposition;
- focused and full default/no-default/tooling command results;
- medium+ unresolved finding review;
- unblock audit promoting M003 and M004 if M002 closes.

## 16. Handoff notes

Keep the frontend fixed and move Rust back toward it. The current server is the migration variable; dashboard.js/dashboard.css are not.

M003 and M004 should not start until this plan closes because otherwise each page group is likely to reproduce shell/API work independently.
