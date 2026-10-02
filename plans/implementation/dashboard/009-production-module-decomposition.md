# Dashboard Milestone 009 — Production module decomposition and ownership cleanup

Status: ready

Repository baseline: `30b8282a`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-009--production-module-decomposition-and-ownership-cleanup`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Applicable ADRs:

- None required. This is an internal module-boundary refactor preserving the already-closed dashboard compatibility/auth/runtime contracts.

Primary class: polish

## 1. Objective

Decompose the restored dashboard implementation out of the current monolithic
`rust/src/server/dashboard.rs` into explicit route, API, asset/theme,
view/projection, rendering, and formatting boundaries while preserving the
closed M001-M007 dashboard behavior and the M008 strict-CI invariant.

The milestone is successful only if the frozen Python-oracle comparator
continues to report exactly the already accepted source-backed differences and
no new DOM/API/theme/auth behavior appears.

## 2. Why this milestone is ready

Hard dependency: Dashboard M008 is closed at `plans/closure/dashboard/008-status.md`.

M008 restored strict Clippy and completed all hosted CI gates in run
`37049147155`. M009's pre-refactor strict qualification is recorded at
`/tmp/dashboard-m009-baseline.json` and reports only the nine accepted M006
differences. The frozen oracle worktree is available at
`/tmp/eggpool-dashboard-oracle-m002`.

No external dependency remains.

## 3. Current implementation evidence

At baseline `3270f4b71b2a7dd5115d9b8e1666a789ac292676`:

- `rust/src/server/dashboard.rs` is approximately 5,819 lines / 252 KiB.
- It contains more than one hundred top-level functions spanning:
  - page handlers for all 14 dashboard routes;
  - eight dashboard JSON/stat compatibility handlers and query normalization;
  - static CSS/JS/Chart.js/favicon/theme delivery;
  - data-page orchestration and dashboard-specific projection assembly;
  - every page renderer;
  - model metadata/benchmark/pricing helpers;
  - Runtime/Cache formatting;
  - theme/color/heatmap helpers;
  - shared layout, escaping, URL encoding, metric cards, and summary JSON;
  - unit tests.
- `rust/src/server/mod.rs` already provides the outer route/auth/server
  assembly, so dashboard decomposition can remain inside the dashboard
  subsystem without changing server ownership.
- `scripts/qualification_dashboard_parity.py` and the frozen oracle provide a
  strict full-DOM/API guard. M006 closed with nine explicitly accepted
  source-backed differences and no remaining dashboard-owned parity defect.
- `architecture/deep-dive-dashboard.md` still names one
  `rust/src/server/dashboard.rs` owner and will become stale if the module is
  decomposed.

The current file is maintainable enough to run, but it now concentrates too
many unrelated presentation/read-plane responsibilities in one compilation
unit. That raises review and merge-conflict cost and makes future bounded
dashboard changes harder to isolate.

## 4. Invariants that must not regress

- The dashboard remains observational and read-only.
- `rust/src/server/mod.rs` retains route assembly and authentication/public
  exemption ownership.
- Dashboard handlers remain thin adapters; no routing, retry, quota, provider,
  reload, generation, or persistence policy moves into rendering modules.
- `DashboardRepository`, `UsageRollupRepository`, runtime diagnostics,
  catalog/model metadata, health, and metrics owners remain authoritative.
- No new SQLite connection/pool, background task, provider probe, or runtime
  cache is introduced.
- Dashboard routes, methods, query semantics, status codes, auth classes, JSON
  schemas, DOM tree, classes/IDs/data attributes, text semantics, theme
  behavior, and static asset bytes remain unchanged.
- The sole intentional CSS difference accepted by M006 remains exactly the
  existing `.panel { min-width: 0; }` correction; M009 must not add another
  asset divergence.
- HTML escaping, JSON/script escaping, path/query encoding, result bounds, and
  secret/content redaction remain intact.
- The frozen oracle and comparator are not edited to accommodate the refactor.
- The nine M006 accepted source-backed differences remain the exact accepted
  set unless current repository evidence proves one has independently changed.
- Default and `--no-default-features` strict builds remain green.

## 5. Scope

### In scope

- Convert `rust/src/server/dashboard.rs` into a directory module or equivalent
  explicit internal module structure.
- Separate, at minimum, these concerns:
  - route/page handlers and query extraction/validation;
  - dashboard JSON compatibility APIs;
  - static asset/theme handlers;
  - shared response/degraded helpers;
  - shared HTML/layout/escaping/formatting primitives;
  - Overview renderer;
  - Accounts renderer;
  - Models/Model Detail renderer and model-info helpers;
  - telemetry renderers (Latency/Events/Timeseries/Bandwidth/Pings);
  - Reliability/Routing/Traces renderers;
  - Runtime/Cache renderers.
- Keep shared DTO/projection inputs explicit so renderer modules consume data
  rather than discover runtime state themselves.
- Re-export the same dashboard entry points expected by `server/mod.rs`.
- Move or split unit tests alongside their owning modules where that improves
  locality.
- Add a narrow structural guard if needed to prevent renderer modules from
  importing runtime policy owners directly.
- Update architecture/development documentation to the new stable module map.
- Run the strict oracle/API qualification against the refactored candidate.

### Explicitly out of scope

- Any new dashboard page, field, control, endpoint, metric, or telemetry.
- HTML/CSS/JS/theme changes.
- Changing the nine accepted M006 differences.
- Refactoring `DashboardRepository` or database schema solely for aesthetics.
- Changing public/private dashboard authentication.
- Moving dashboard code into a new crate.
- Introducing a template engine or frontend framework.
- Refactoring `scripts/qualification_dashboard_parity.py`; that is M010.
- Performance optimization unless the refactor itself causes a measured
  regression.
- Reworking non-dashboard server modules.

## 6. Required production changes

Use a stable internal module layout. A preferred shape is:

```text
rust/src/server/dashboard/
  mod.rs              # facade/re-exports; minimal shared constants
  routes.rs           # page handlers, query parsing, orchestration
  api.rs              # dashboard JSON compatibility handlers/projections
  assets.rs           # CSS/JS/chart/favicon/theme responses
  response.rs         # html/json/degraded response helpers
  format.rs           # escaping, formatting, URL/class helpers
  theme.rs            # theme variables/color/heatmap derivation
  render/
    mod.rs
    layout.rs
    overview.rs
    accounts.rs
    models.rs
    telemetry.rs
    diagnostics.rs    # reliability/routing/traces
    runtime.rs
    cache.rs
```

The exact filenames may differ if current type dependencies support a cleaner
split, but the ownership rules are binding:

- render modules accept already-gathered data and formatting/theme context;
- render modules do not register routes or make async DB/runtime calls;
- routes/API modules may gather bounded current-owner snapshots but do not own
  presentation policy beyond selecting the correct renderer/JSON projection;
- asset/theme delivery remains independent from database/runtime state;
- shared escaping/format helpers do not import server/runtime state.

Avoid an artificial "utils.rs" dumping ground. If a helper has one page-family
consumer, keep it with that renderer.

Keep the existing `dashboard` module path so `server/mod.rs` should require
only import/re-export adjustments, not route behavior changes.

## 7. Ordered work packages

### Work package A — Freeze behavior and dependency graph

Intent: establish a current-candidate baseline before moving code.

Required changes/evidence:

- Confirm M008 is closed and current strict CI is green.
- Record current `dashboard.rs` top-level responsibility inventory.
- Run the strict dashboard oracle/API qualification and preserve the exact
  current difference classification.
- Record current static asset hashes.

Acceptance evidence:

- Baseline strict report has no unexplained dashboard-owned mismatch.
- Nine accepted source-backed differences remain explicitly identified.
- Asset manifest is recorded before movement.

### Work package B — Extract shared non-rendering boundaries

Intent: create stable seams before splitting page families.

Required changes:

- Extract route/query orchestration, JSON APIs, static/theme delivery,
  response helpers, escaping/formatting, and theme/color helpers.
- Keep all existing function semantics and visibility as narrow as practical.
- Re-export only the facade functions actually used outside the dashboard
  module.

Acceptance evidence:

- `server/mod.rs` route topology/auth behavior is unchanged.
- Focused dashboard unit and server-transport tests pass.
- No new public Rust API is introduced.

### Work package C — Extract render families

Intent: localize page-specific markup and helpers.

Required changes:

- Move shared layout first, then page families in small reviewable steps.
- Keep model-info helpers with Models/Model Detail unless truly shared.
- Keep reliability/routing/trace diagnostic helpers together only where they
  share inputs; do not recreate a second monolith.
- Keep Runtime/Cache separated because their source DTOs and security semantics
  are distinct.

Acceptance evidence:

- Compilation and focused tests remain green after each family move.
- No renderer imports the database connection/server runtime directly.
- Shared layout remains single-source.

### Work package D — Re-home tests and add structural guard

Intent: make module boundaries maintainable, not merely cosmetic.

Required changes:

- Move unit tests next to the implementation they exercise where practical.
- Add one narrow guard/test documenting the intended dependency direction if
  ordinary Rust visibility alone cannot enforce it.
- Do not add brittle line-count/file-size tests.

Acceptance evidence:

- Tests can identify the owning module for layout, escaping, theme, JSON API,
  and page-render behavior.
- There is no broad `pub` exposure solely to make tests compile.

### Work package E — Full parity and documentation reconciliation

Intent: prove a pure refactor.

Required changes/evidence:

- Run full strict oracle/API qualification without changing oracle fixtures or
  normalization.
- Compare resulting difference groups with the pre-refactor baseline.
- Verify static assets are unchanged.
- Update `architecture/deep-dive-dashboard.md`,
  `architecture/overview.md`, and the development skill if focused test/module
  instructions changed.

Acceptance evidence:

- Exact same accepted difference set as pre-refactor.
- No new route/API/DOM/theme/auth mismatch.
- Architecture docs describe the directory module and ownership accurately.

## 8. Failure, cancellation, restart, contention semantics

No runtime concurrency or failure policy may change.

Module extraction must not alter when snapshots are taken, DB queries are
awaited, or runtime diagnostics are borrowed/cloned. Keep async boundaries in
the route/API layer. Renderer functions should remain synchronous/pure with
respect to process state.

Dashboard DB/read failures retain the same bounded degraded response. Shutdown,
restart, and cancellation behavior remain owned by existing server/runtime
lifecycle code.

If moving a helper changes borrow/lifetime behavior enough to require new
shared mutable state, stop; that would no longer be a pure decomposition.

## 9. Compatibility and migration

No config, schema, storage, endpoint, DOM, asset, or data migration.

Internal Rust module paths are not a public compatibility surface. External
HTTP/browser behavior is.

Do not modify frozen oracle captures, accepted difference dispositions, or the
dashboard asset manifest unless a byte change actually occurs; any byte change
is a stop condition for this plan.

## 10. Required tests

Focused:

```bash
cargo test --manifest-path rust/Cargo.toml --lib server::dashboard -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short --maxfail=1
```

Strict compatibility qualification:

```bash
uv run python scripts/qualification_dashboard_parity.py --output /tmp/dashboard-m009.json --markdown /tmp/dashboard-m009.md
```

If the qualification environment supports the already-established frozen
oracle checkout, use it exactly as documented by M006/M007. Browser screenshot
recapture is optional only if the full DOM/API projection and static asset
hashes are byte/semantically unchanged; if any rendered projection changes,
run the matched screenshot/interaction gate and treat the change as a defect
unless separately justified.

Full repository gates:

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

Hosted CI must pass on the final implementation/closure head.

## 11. Documentation updates

- `architecture/deep-dive-dashboard.md`: replace the single-file owner
  description with the new module map and dependency direction.
- `architecture/overview.md`: update the HTTP-adapter/observability module map.
- `.opencode/skills/development/SKILL.md`: update focused dashboard test/module
  pointers if the stable test path changes.
- Do not rewrite M001-M008 closure records.

## 12. Acceptance criteria

1. M008 is closed before implementation begins.
2. The production dashboard is decomposed into explicit internal modules with
   a thin facade and localized render families.
3. `server/mod.rs` retains the same route/auth behavior.
4. Renderers do not acquire runtime/database authority.
5. No dashboard HTTP/DOM/API/theme/static behavior changes.
6. Frozen oracle/normalization is unchanged.
7. Strict qualification reports the exact same accepted difference set as the
   pre-refactor baseline and no new mismatch.
8. Static asset hashes remain unchanged.
9. Focused, full default/no-default, tooling, and hosted-CI gates pass.
10. Architecture/development docs reflect the new module structure.
11. No medium-or-higher unresolved dashboard maintenance defect remains from
    the decomposition.

## 13. Stop conditions

Stop and report rather than broadening scope if:

- M008 is not closed/current CI is not green;
- moving code requires changing rendered output, endpoint schema, auth, query
  semantics, or theme/static bytes;
- a module split requires new shared mutable state or a new runtime owner;
- database/query behavior must change to make interfaces convenient;
- the strict oracle or accepted M006 difference list would need modification;
- the refactor grows into `DashboardRepository` redesign, a new crate, or a
  frontend/template framework;
- unrelated CI failures appear.

## 14. Closure evidence required

The closure record must include:

- implementation commit(s);
- pre/post module inventory and ownership map;
- statement of external facade/re-export changes;
- pre/post strict oracle/API difference summary proving no new mismatch;
- static asset hash comparison;
- focused dashboard/server/status results;
- default/no-default Clippy/check and full serial Rust results;
- tooling/Ruff/Pyright results;
- hosted CI run ID/conclusion;
- architecture/development doc updates;
- security/auth/content-redaction review;
- failure/concurrency semantics review;
- severity-tagged unresolved findings;
- unblock audit promoting M010 if M009 closes.

## 15. Handoff notes

This is intentionally a structural cleanup after parity restoration. Resist the
temptation to "improve" labels, markup, APIs, or old renderer semantics while
moving code.

The frozen oracle is the strongest available refactor guard. Preserve it.
