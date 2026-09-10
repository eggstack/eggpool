# Q012 — Dashboard State, Semantic Content, and Visual Requalification

Status: accepted; closed 2026-09-10

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: `e7d226aedc23740a3fe7c25210923d1703fca6c9` (historical Q011/Q010 M10 closure before post-close dashboard audit).

Primary class: invariant/corrective

Hard dependencies: accepted Q001-Q003 and Q005-Q011 evidence; historical Q004 and Q010 closure records remain append-only evidence but are superseded for the findings named here.

## Objective

Close the remaining M10 dashboard qualification gap without redesigning the product. Q012 must prove the mandatory Q001 dashboard contract against real deterministic populated/error states, strengthen semantic DOM comparison so meaningful data/content drift fails, produce actual bounded screenshot artifacts for the representative visual review, and rerun the aggregate M10 gate before M11 planning is eligible again.

Q012 may make narrowly scoped Rust dashboard parity corrections if the new fixtures expose real implementation defects. It must not weaken the Q001 contract or normalize away semantic differences merely to obtain closure.

## Why Q004/Q010 are not sufficient

Post-Q011 review found that the accepted Q004 evidence did not satisfy its own plan or the frozen Q001 manifest:

1. `q001.dashboard.states` requires representative empty, populated, unauthorized, and error states.
2. Q004 starts Python and Rust against fresh empty databases; its report labels populated/multi-provider states as `reserved deterministic fixture shape` rather than executing them.
3. `HtmlProjection` records page text, but `compare_dom_projection()` does not compare semantic text, table/card row content, or ordered data rows.
4. The focused deliberate-mismatch regression changes only active navigation. A missing or altered model/account/statistic row can therefore escape qualification.
5. Q004 emits a 224-entry screenshot **metadata** manifest, but the code does not capture those images. The closure records only three manually reviewed PNGs even though the Q004 plan requires every major page to be reviewed at least once across the representative theme/viewport set.
6. Q010 reran the same Q004 runner and inherited these gaps.
7. `migration-rs/registry.md` still carries completed Q002/Q003/Q005 rows in the dependency-ready table, so the active handoff surface is not cleanly authoritative.

These findings reopen M10 qualification only. Q005-Q009/Q011 environment, live-provider, SBC, database, and stability evidence remains accepted unless Q012 changes a surface that invalidates its freshness.

## Authoritative sources

Before editing code or fixtures, inspect current repository evidence including:

- `migration-rs/fixtures/qualification/m10-q001-manifest.json`;
- historical Q004 plan/closure and `004-run.json`/`004-run.md`;
- historical Q010 and Q011 closures;
- `scripts/qualification_dashboard.py`;
- `tests/migration_rs/test_q004_dashboard.py`;
- Python dashboard routes/renderers/repositories;
- Rust dashboard rendering/server/runtime sources;
- Q003 database compatibility fixtures and helpers;
- Q005-Q009/Q011 closure evidence for freshness review;
- current dashboard docs/themes/static assets.

Do not rewrite Q001 or historical closure records to narrow the frozen contract.

## Part A — deterministic shared dashboard state corpus

Create a versioned, bounded dashboard fixture corpus that can be presented identically to Python and Rust.

Prefer one canonical logical seed description plus a byte-identical or semantically identical database copy for each implementation. Because Q003 already accepts Python/Rust schema compatibility, it is acceptable to construct one representative SQLite fixture using the canonical schema/repository helpers and copy it before server startup, provided both implementations receive the same logical state and no implementation-specific hidden setup is required.

The corpus must include at least:

### Empty / first-run

Retain the existing empty-state fixture and current route/static/auth assertions.

### Populated multi-entity state

Seed enough current-schema data to exercise every dashboard page with meaningful content, including where applicable:

- at least two providers with structurally distinct names/surfaces;
- multiple accounts with enabled/disabled or healthy/degraded distinctions where the dashboard exposes them;
- multiple concrete models plus at least one virtual/router model if rendered;
- finite and streaming request records;
- success and failure outcomes;
- routing decisions / traces;
- latency/timeseries/bandwidth observations;
- provider pings/reliability facts;
- cache/model-info presence and missing optional values;
- usage/cost values including zero, present, and unavailable cases;
- runtime/task/reload facts where the page consumes them.

### Escaping / long / Unicode state

Include representative long names and values containing `<`, `>`, `&`, quotes, Unicode and whitespace-sensitive text in provider/account/model/error/detail fields that are expected to be escaped.

### Error / missing state

Exercise real dashboard error or missing-resource outcomes, at minimum:

- missing model detail;
- invalid or unsupported query parameter where Python defines behavior;
- absent optional model-info/cost/cache data;
- any route-level error state already present in the Python product contract.

Do not invent a new UI error page solely for qualification.

### Private/unauthorized state

Retain the public/private dashboard distinction and assert unauthenticated/authorized behavior for both implementations.

All seeded timestamps must be deterministic enough for semantic comparison. Use the existing approved relative-time normalization only where the contract permits it; do not normalize event ordering, data presence, values, or row membership.

## Part B — semantic dashboard projection

Replace the shell-only comparison with a page-aware semantic projection that preserves meaningful dashboard information.

The projection should extract, as applicable:

- page title and heading hierarchy;
- active navigation and internal links;
- form methods/actions/controls;
- stable IDs/data attributes used by scripts;
- page-level empty/error/status messages;
- metric/card labels and displayed values;
- table headers;
- ordered table rows and cell text;
- model/provider/account identifiers and displayed statuses;
- chart bootstrap/data-hook presence and bounded semantic data facts where Python exposes equivalent content;
- static asset references;
- escaping/safety observations.

Do not require raw HTML byte equality. HTML attribute order and implementation-specific nonsemantic formatting may differ, but semantic text/data/ordering must not disappear behind normalization.

### Required deliberate-negative regressions

Focused tests must prove the comparison fails for at least:

- changed metric/card value;
- missing data row;
- changed row value;
- reordered rows when Python ordering is contractual;
- changed page status/error text;
- missing control/form input;
- escaping regression;
- active-navigation mismatch.

A comparator that can pass after meaningful populated data has been changed is not acceptable.

## Part C — page/state comparison matrix

Run Python and Rust side-by-side across the same fixture roots and compare the real routes.

At minimum qualify all existing 14 dashboard page routes under:

- empty state;
- populated state;
- escaping/long-name state where relevant;
- missing/error state where relevant;
- public and private auth behavior.

Not every page needs a separate database if a bounded shared populated fixture exercises all of them. The closure must enumerate which state(s) apply to each route and which semantic projection fields are compared.

The populated fixture must materially exercise page content. A report entry such as `reserved`, `future fixture`, or metadata-only coverage cannot satisfy a mandatory cell.

## Part D — real visual review artifacts

The qualification procedure must distinguish **coverage metadata** from **actual captured images**.

Use browser tooling outside the Rust runtime dependency graph. Existing local browser tooling, Playwright as qualification-only tooling, or a documented equivalent is acceptable. Do not add a browser engine or screenshot dependency to Rust production code.

### Minimum visual review set

Capture actual Python and Rust screenshots such that:

- every major dashboard page is visually reviewed at least once;
- both desktop and narrow/mobile layouts are represented across the set;
- the default theme plus the existing structurally distinct review themes are represented across the set;
- populated content appears in the visual set, not only empty pages;
- at least one long/Unicode/overflow-sensitive page is captured;
- model detail and a dense table/chart page are included.

A reasonable bounded implementation is one Python/Rust pair per each of the 14 routes using a deterministic assignment of viewport/theme, plus a few additional mobile/long-content captures where needed. Do not create hundreds of PNGs merely because the metadata cross-product contains hundreds of theoretical cells.

### Artifact evidence

For each actual capture record:

- implementation;
- route/state/theme/viewport;
- dimensions;
- relative artifact name;
- SHA-256;
- capture result;
- concise manual-review disposition.

Binary screenshots may remain outside git if repository size is undesirable, but closure must distinguish actual captures from planned filenames and retain reproducible metadata/hashes. The capture command must fail if an expected screenshot was not actually created.

Manual review must explicitly check navigation, clipping/overflow, empty/populated tables/cards, chart containers, theme application, long text wrapping, and mobile usability. Pixel-perfect equality is not required unless an existing contract says so.

## Part E — bounded dashboard parity corrections

If the populated/error corpus exposes a real Rust/Python mismatch, fix only the smallest dashboard implementation surface needed for parity.

Allowed examples:

- missing route data query;
- wrong row ordering;
- missing label/value/empty-state text;
- incorrect escaping;
- missing optional-field handling;
- incorrect status/content metadata;
- broken layout hook/static reference.

Not allowed:

- dashboard redesign;
- new SPA/frontend framework;
- theme rewrite;
- new product metrics unrelated to Python parity;
- broad runtime or DB architecture changes.

Every product correction needs a failing-before/passing-after regression tied to the fixture that exposed it.

## Part F — aggregate freshness and M10 re-closure

After Q012 dashboard qualification passes:

1. rerun the current Q001 manifest validator;
2. rerun Q002 deterministic aggregate;
3. rerun Q003 if Q012 touches database seed/schema/repository behavior beyond qualification-only fixtures;
4. rerun the full Q012 dashboard runner and focused tests;
5. rerun Q005 portability if Rust dashboard/server production source changed in a way that could be target-sensitive; otherwise record a source-freshness justification;
6. retain Q006 rootful evidence unless deployment sources changed;
7. retain Q011 live-provider evidence unless coordinator/provider/wire sources changed;
8. retain Q008 physical SBC evidence unless runtime/server/dashboard production changes materially affect its qualified paths; if affected, run the bounded applicable SBC follow-up rather than silently carrying stale evidence;
9. rerun Q009 stability if runtime/server/dashboard changes affect its exercised request/runtime path; otherwise record freshness justification;
10. run the full Rust/migration/smoke/lint gates required by Q010.

Q012 is the current corrective M10 closure authority. Historical Q004/Q010/Q011 records remain unchanged; Q012 closure may re-close M10 only after the aggregate freshness review proves no accepted evidence was invalidated.

## Part G — registry/control-surface repair

As part of Q012 implementation/closure:

- remove already-completed Q002/Q003/Q005 entries from the registry's dependency-ready table;
- while Q012 is active, list only Q012 as dependency-ready;
- describe Q004 and Q010 as historical accepted closure evidence superseded for the named dashboard gap;
- keep Q011 as accepted live-provider corrective evidence;
- keep M11 blocked until Q012 closes;
- after accepted Q012 closure, leave the dependency-ready table empty and mark M11 merely eligible for its own planning review.

Do not delete completed-plan rows or rewrite historical closure records.

## Required focused tests

Create or extend focused tests (for example `tests/migration_rs/test_q012_dashboard_requalification.py`) covering at least:

- deterministic fixture construction and clone identity;
- populated state actually contains the required provider/account/model/request/routing/statistic classes;
- all 14 routes execute against populated state for Python and Rust;
- missing/error/private state coverage;
- semantic projection of cards/tables/status text;
- every deliberate-negative comparator case in Part B;
- screenshot command proves actual output files exist before recording hashes;
- visual coverage includes all page routes and both implementations;
- artifact manifest is bounded and secret-free;
- no credential, raw provider body, hostname, physical identifier, or arbitrary environment dump is retained.

## Verification

Run at minimum:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs/test_q004_dashboard.py tests/migration_rs/test_q012_dashboard_requalification.py -q --tb=short --maxfail=1
uv run python scripts/qualification_dashboard.py --screenshots
uv run python scripts/qualification_runner.py --skip-build
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

If Q012 adds a separate screenshot/capture command or renamed runner, record and run that exact command in closure. The closure must also document any environment-specific Q005/Q008/Q009 reruns required by the freshness audit.

## Closure evidence

Write `migration-rs/closure/qualification/012-status.md` containing:

- implementation commit(s);
- exact populated/error/private fixture inventory;
- route × state matrix;
- semantic projection fields and normalization rules;
- failing-before/passing-after comparator regressions;
- any Rust dashboard defects found and corrected;
- actual screenshot artifact count, route/theme/viewport mapping and SHA-256 values or bounded manifest hash;
- manual visual-review summary;
- Q001 dashboard-cell disposition;
- Q002/Q010-equivalent aggregate rerun results;
- environment evidence freshness decisions for Q005-Q009/Q011;
- full Rust/Python/lint test counts;
- dependency/schema/security/resource review;
- unresolved findings;
- registry transition.

## Acceptance criteria

Q012 closes only when all of the following are true:

- `q001.dashboard.states` is actually exercised for representative empty, populated, unauthorized and error/missing states;
- every current dashboard page receives populated semantic qualification where meaningful;
- semantic text, metric/card values, table headers/rows and contractual ordering are compared rather than ignored;
- deliberate data-content mismatches fail the comparator;
- escaping/long/Unicode and missing optional values are qualified;
- actual screenshots—not only expected filenames—cover every major page at least once across a bounded Python/Rust visual matrix;
- desktop/mobile and representative theme/layout variants are reviewed;
- any discovered parity bug has failing-before/passing-after evidence;
- Q001/Q002 and the Q010-equivalent aggregate gates remain green on the final candidate;
- accepted Q005-Q009/Q011 evidence is either fresh or deliberately rerun where Q012 changes require it;
- the registry has one unambiguous active handoff and no stale completed rows in the dependency-ready table;
- no unresolved high/medium dashboard, compatibility, security, data-loss, lifecycle, target, provider, or resource finding remains;
- no M11 cutover, installer switch, release publication, updater-authority change or M12 retirement work is included.

Only accepted Q012 closure may re-close M10 and restore M11 eligibility for a separate planning review.

## Handoff

Q012 is accepted and is the append-only closure authority for the dashboard findings. M10 is re-closed. The dependency-ready implementation-plan table is empty; M11 is eligible for its own separate planning review, but no M11 implementation plan is promoted by this closure.
