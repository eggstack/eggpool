# Q004 — Dashboard SSR, DOM, Static Asset, and Visual Parity Review

Status: ready for handoff (2026-09-09)

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q003.

Primary class: invariant/polish

Hard dependency: accepted Q003.

## Objective

Qualify the completed Rust dashboard as a rendered product. Earlier milestones established SSR/static route parity; Q004 must now verify representative page states, DOM semantics, escaping, navigation, assets, responsive layout, and visual consistency against Python without redesigning the dashboard.

## Required source audit

Inspect:

- F005 SSR/static implementation and closure;
- Python dashboard/page/rendering modules and tests;
- Rust server/dashboard renderer/static asset sources;
- `architecture/deep-dive-dashboard.md` and dashboard documentation;
- theme inventory and CSS/static assets;
- current dashboard-related migration tests and route fixtures;
- Q001 dashboard cells and Q002 deterministic route results.

## Page/state matrix

Freeze representative states for each current dashboard page/route, including where applicable:

- no data / first run;
- normal populated state;
- multiple providers/accounts/models;
- recent success/failure/routing decisions;
- long model/provider/account names;
- special HTML characters and Unicode;
- missing/partial optional cost or model-info data;
- public/private dashboard mode where behavior differs;
- update/status/backup/runtime facts exposed by completed M8/M9 surfaces.

Use deterministic DB/config fixtures, not screenshots of a live personal installation.

## DOM/semantic comparison

Compare Python and Rust output for:

- status code/content type/security headers relevant to dashboard;
- page title/head/meta facts;
- navigation links and active-page state;
- form actions/methods/inputs where present;
- table/card labels and ordered data rows;
- empty/error-state messages;
- escaped text versus intentionally trusted static markup;
- IDs/classes/data attributes used by existing scripts/styles;
- static asset URLs and cache/content metadata;
- theme selection classes/data attributes;
- external links and `rel`/target safety facts where applicable.

Normalize only ephemeral values such as CSRF-like/request IDs if the existing contract permits it. Do not normalize away missing text, reordered semantic rows, escaping differences, or broken links.

## Static asset integrity

Inventory every dashboard-owned CSS/JS/image/font-reference asset actually served by Python and Rust. Assert:

- expected asset route exists;
- content type is correct;
- no Python-only required asset is missing from Rust;
- referenced asset paths resolve;
- no absolute local filesystem path leaks;
- no secrets/config values are embedded in static output;
- bundled assets are deterministic across equivalent builds where expected.

Do not add or redistribute font files merely for qualification.

## Visual review

Create a qualification-only screenshot/review procedure for deterministic fixtures.

At minimum review:

- representative desktop viewport;
- representative narrow/mobile viewport;
- default theme;
- a small set of theme classes chosen to exercise light/dark/high-contrast or structurally distinct CSS, rather than manually screenshotting all 50 themes;
- every major page once across the representative theme/viewports;
- long-content fixture for overflow/wrapping.

Browser tooling must remain outside the Rust runtime dependency graph. Prefer an existing local/browser automation environment or a small dev-only script. If screenshots are too large for git, record hashes/filenames and concise review findings in closure rather than committing a binary archive.

## Accessibility/safety checks

Without turning M10 into an accessibility redesign, catch clear regressions:

- duplicate IDs in deterministic pages;
- missing labels for existing labelled controls;
- broken heading/navigation hierarchy relative to Python;
- obvious low-information image/link alt/title regressions where Python provides them;
- raw unescaped provider/model/error text;
- unsafe `javascript:`/unexpected external URLs generated from data.

## Required tests

Add focused tests for:

- DOM semantic projections for each page/state fixture;
- escaping of `<`, `>`, `&`, quotes and representative Unicode in user/provider/model/error fields;
- static asset route inventory;
- internal link crawl over deterministic rendered pages;
- theme selector/inventory parity;
- mobile/desktop screenshot procedure producing deterministic route coverage metadata;
- a deliberate DOM mismatch proving the comparison fails rather than over-normalizes.

## Verification

Run at minimum:

```text
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# focused Python dashboard/SSR tests
# Q004 DOM comparison command
# Q004 screenshot/review command in the documented browser environment
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

## Non-goals

Q004 does not redesign pages, replace CSS/theming, add a SPA, change dashboard information architecture, or add a browser engine/runtime dependency to Rust.

## Closure evidence

Write `migration-rs/closure/qualification/004-status.md` containing:

- page/state/theme/viewport matrix;
- DOM comparison results and normalization rules;
- static asset inventory/result;
- screenshot artifact names/hashes and manual review summary;
- any parity defects fixed with failing-before/passing-after evidence;
- explicit intentional visual differences, if any;
- unresolved findings and registry transition.

## Acceptance criteria

Q004 closes only when all mandatory Q001 dashboard cells pass, no material navigation/content/escaping/layout/static regression remains, and visual differences are either corrected or explicitly accepted without changing the migration contract.

Accepted Q004 promotes only Q005.
