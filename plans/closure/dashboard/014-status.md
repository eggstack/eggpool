# Dashboard Milestone 014 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/014-operator-surface-presentation-and-accessibility-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#7-milestones`

Repository baseline reviewed: `d04ad6831c6e030b7f5fc8a56501fdfecab982bd`

Implementation commits or pull requests:

- `2db8265af77d7a72cb9cc5f0a73dfc1248bb9fd1` — Implement the operator surface presentation and accessibility corrective

## 1. Executive finding

Milestone 014 is closed. The operator surface is now a complete, themed,
accessible page set, and the defects the milestone set out to correct are
corrected by guards that fail when the behavior is reverted.

The largest defect was that the entire on-prem browser surface rendered
unstyled. `rust/assets/dashboard/static/dashboard.css` declares no custom
property of its own, so every colour it uses resolves through `var()`, and
`/static/theme.css` was linked from exactly one place — the shared dashboard
layout. The five standalone shells (`/usage` and its administrator variant,
`/admin/audit`, `/my-account`, `/my-account/models`, and the one-shot personal
token page) therefore resolved no variables at all, and the sign-in, account,
and account-error pages loaded no stylesheet whatsoever. Every shell now links
both `/static/dashboard.css` and `/static/theme.css?theme=<name>`, resolving
the name through `selected_theme()`, and a guard proves that every variable the
stylesheet reads is published by every bundled theme, so no page can render an
invalid `var()`.

The status pills were the second defect, and the measurable one. The `.pill-*`
rules used hardcoded hexadecimal literals — `#22c55e`, `#eab308`, `#f97316`,
`#ef4444`, `#a855f7` — where the theme already publishes `--color-success`,
`--color-warning`, `--color-error`, and `--color-info`. Measured against the
surface the pill text actually sits on, the 20% tint of the pill colour over the
card background, **all fifty bundled themes failed**, and in thirteen themes
every one of the five pills failed; the worst case was **1.42:1** on Ferra
Light. The pill foregrounds are now derived in `theme_variables()` from the
theme's own semantic tokens, moved to a lightness that clears WCAG AA, and
placed on a lightness ladder so two roles never collapse onto one colour.
Measured on the values the server serves, **all fifty themes now pass, with a
worst case of 4.53:1** (IntelliJ Light and Macaw Light) and a best case of
4.59:1. The guard reads the served stylesheet rather than a separate table, so
a theme change cannot silently invalidate it, and a deliberately failing colour
is rejected.

The chart loading path used to be a dead end. `whenChartReady` polled for
Chart.js two hundred times at fifty milliseconds and, on give-up, only logged a
warning — while the recovery path was reachable only from inside that
callback, leaving the shell reading "Preparing chart…" indefinitely. The path
now ends in a rendered chart or an explicit failure: the payload is fetched
once and cached, the wait for the chart runtime is bounded, and a wait that
runs out states the failure. The fallback never re-fetches the payload and
never arms a timer that outlives the canvas it updates.

The remaining corrections are the ones the plan enumerated: the shared
navigation chrome on the standalone shells, the table chrome on every
administrative table, the accessibility surface, the retired frontend
removal, focus visibility, the print stylesheet, the theme picker's lost query
parameters, and the usage row-cap disclosure.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Every reachable page resolves a complete theme variable set, with no invalid `var()` | `rust/src/server/dashboard/render/layout.rs` — `dashboard_standalone_page`; test `the_standalone_shells_resolve_the_theme`; test `every_stylesheet_variable_resolves_in_every_bundled_theme` | pass | The guard extracts every `var()` without a fallback from the stylesheet and asserts every bundled theme publishes it. |
| The administrative tables carry the table chrome, and a future table cannot omit it | `DASHBOARD_TABLE_OPEN` and `DASHBOARD_TABLE_OPEN_COMPACT` in `render/layout.rs`; test `every_emitted_table_carries_the_table_class` | pass | A source-level guard fails if a table is opened any other way. |
| Every status pill reaches at least 4.5:1 against its theme's card background, in every bundled theme | `rust/src/server/dashboard/theme.rs` — `theme_pill_color`; test `every_bundled_theme_serves_contrast_safe_pill_foregrounds` | pass | Before: all 50 themes failed, worst 1.42:1. After: all 50 pass, worst 4.53:1. |
| The contrast guard fails on a deliberate regression | test `the_contrast_guard_rejects_a_deliberate_regression` | pass | A pill foreground set to the card background is rejected. |
| The pills stay mutually distinguishable | test `every_bundled_theme_keeps_its_pill_roles_distinguishable`; test `the_distinguishability_guard_rejects_a_collapsed_role` | pass | Several bundled themes give `text.success`, `text.warning`, and `text.error` the same colour, so the role ladder, not the token, keeps the roles apart. |
| The chart surface reaches a rendered chart or an explicit failure, never an indefinite preparing state | `rust/assets/dashboard/static/dashboard.js` — `waitForChartRuntime`, `markChartRuntimeUnavailable` | pass | The wait is bounded at the same 200 × 50 ms budget the old poll used, and a wait that runs out sets the shell's state to `error` with a stated message. |
| The chart surface never fetches the payload twice | `rust/assets/dashboard/static/dashboard.js` — `namespace.__chartHydrationData` | pass | The fallback draws the payload already in hand; it does not re-request it. |
| Every chart canvas carries an accessible name | `render/telemetry.rs`, `render/overview.rs`, and `dashboard.js` — `role="img"` plus `aria-label`; test `every_chart_canvas_carries_an_accessible_name` | pass | The script-created canvas and both renderer-created canvases are covered. |
| The chart shell and the refresh indicator are live regions | `render/layout.rs` — `role="status" aria-live="polite"` on the chart shell and on `#dashboard-updated`; test `the_dashboard_shell_carries_a_skip_link_and_live_regions` | pass | The refresh announcement is tied to the existing refresh note, not to a new timer. |
| A skip link is the first focusable element and targets the main content | `render/layout.rs` — `<a class="skip-link" href="#dashboard-content">`; test `the_dashboard_shell_carries_a_skip_link_and_live_regions` | pass | The main content element is `<main id="dashboard-content">`. |
| No retired stylesheet selector or unused script export remains, and a guard fails on reintroduction | `rust/src/server/dashboard/tests.rs` — `RETIRED_CSS_SELECTORS`, `RETIRED_SCRIPT_EXPORTS`; tests `retired_stylesheet_selectors_stay_retired`, `retired_script_exports_stay_retired`, `every_script_export_has_a_caller`, `every_stylesheet_class_has_a_producer` | pass | The retired-selector guard strips comments, so a comment may name a retired selector to explain why it went. It also asserts the script does not keep querying a retired class. |
| The navigation links, the refresh control, and the selectors have visible focus indicators | `rust/assets/dashboard/static/dashboard.css` | pass | |
| A print stylesheet drops the page chrome | `rust/assets/dashboard/static/dashboard.css` — `@media print` | pass | The page background, the sticky topbar, and the decorative overlay are dropped. |
| Changing the theme preserves the page's other query parameters | `rust/src/server/dashboard/routes.rs` — `dashboard_filter_query`; test `the_theme_picker_preserves_the_page_query_parameters` | pass | The picker carries the page's filters as hidden inputs. |
| A truncated usage table says so | `rust/src/server/dashboard/render/admin.rs` — `usage_truncation_notice`; test `a_truncated_usage_table_says_so` | pass | The disclosure is derived from the same cap the query applies. |
| The asset change is recorded with a reason per divergence, the asset manifest matches the working-tree bytes, and an unrecorded divergence still fails the qualification | `scripts/dashboard_parity/browser.py` — `ORACLE_CANDIDATE_CORRECTIONS`; `rust/assets/dashboard/manifest.json`; `rust/assets/runtime-manifest.json`; `tests/tooling/test_dashboard_parity_projection.py` | pass | See §3. |
| No published dashboard field name or shape changes, and no schema, configuration, or dependency change | `rust/Cargo.toml` and `rust/Cargo.lock` are unchanged; no migration was added | pass | No page route was added or removed. |

## 3. Production implementation evidence

- `rust/src/server/dashboard/theme.rs`
  - `theme_relative_luminance`, `theme_darken_color`, `theme_lighten_color`, and
    `theme_pill_color` derive a pill foreground that clears the AA floor
    against the theme's card background, with a margin above the floor so that
    rounding a channel to an integer cannot land a served value below it.
  - `theme_variables` publishes `--pill-neutral`, `--pill-success`,
    `--pill-info`, `--pill-warning`, `--pill-sparse`, and `--pill-error`
    alongside the existing token set. `sparse` has no token of its own, so it
    is mixed from the warning and error hues and then placed on its own rung
    of the ladder.
  - The theme variable count rises from 46 to 52. The assertion in
    `rust/src/server/dashboard/tests.rs` is updated, and a second assertion
    pins the count with the reason recorded.
- `rust/src/server/dashboard/render/layout.rs`
  - `DASHBOARD_TABLE_OPEN` and `DASHBOARD_TABLE_OPEN_COMPACT` are the only
    ways a table is opened.
  - `DASHBOARD_NAVIGATION` and `dashboard_navigation_markup` render the shared
    chrome.
  - `dashboard_standalone_page` renders a standalone shell with both
    stylesheets, the navigation, and a route back to the overview.
  - `dashboard_query_inputs` carries the page's query parameters as hidden
    inputs for the theme picker.
  - The shared layout gained the skip link, the live regions, and the
    `aria-label` on the chart canvases.
- `rust/src/server/dashboard/render/admin.rs` and the other render modules —
  the theme link, the table class, the canvas accessible names, the usage
  truncation disclosure, and the `yes`/`no` cell classes.
- `rust/src/server/dashboard/admin.rs` and `routes.rs` — the standalone shells
  and the query-parameter preservation.
- `rust/assets/dashboard/static/dashboard.css` — the pill rules now use the
  theme's variables; the retired update-indicator, account-breakdown-filter,
  `td.yes`/`td.no`, and compact-chart-container rules are gone; `.num`,
  `.empty-state`, and `table.data.compact` have rules; the skip link, the
  focus indicators, and the print stylesheet are added.
- `rust/assets/dashboard/static/dashboard.js` — the chart fallback, the canvas
  accessible name, the removal of the five unused exports and of
  `initUpdateCommandCopy`, and the removal of the query for the retired
  compact chart container. The script contains no `innerHTML`,
  `insertAdjacentHTML`, or `outerHTML`: every DOM write is `textContent`.
- `rust/assets/dashboard/manifest.json` — regenerated from the working-tree
  bytes.
- `rust/assets/runtime-manifest.json` — the digest of the dashboard manifest
  is updated to match.
- `scripts/dashboard_parity/browser.py` — `ORACLE_CANDIDATE_CORRECTIONS` now
  maps a path to the tuple of reasons for its divergences, one reason per
  divergence, and the inventory fails when a file records the same reason
  twice.
- `tests/tooling/test_dashboard_parity_projection.py` — updated for the
  per-divergence reason structure, and extended so an unrecorded divergence
  still fails.
- `architecture/deep-dive-dashboard.md` — the presentation and accessibility
  contract.

### The status-pill contrast measurement

Before, against the surface the pill text actually sits on — the 20% tint of
the pill colour over the card background — measured across all fifty bundled
themes:

| Theme | Card | Worst pill | Pills below 4.5:1 |
|---|---|---|---|
| Ferra Light | `#F1E6DE` | 1.42:1 | 5 of 5 |
| Catppuccin Latte | `#E6E9EF` | 1.43:1 | 5 of 5 |
| Rose Pine Dawn | `#F4EDE8` | 1.49:1 | 5 of 5 |
| base16-atelier-forest-light | `#F1EFEE` | 1.50:1 | 5 of 5 |
| Noctis Lilac | `#F2F1F8` | 1.53:1 | 5 of 5 |
| Macaw Light | `#FEFFFF` | 1.68:1 | 5 of 5 |
| Matrix | `#000000` | 4.45:1 | 1 of 5 |
| Nostromo Terminal | `#000000` | 4.45:1 | 1 of 5 |
| Cyber Red | `#090808` | 4.17:1 | 2 of 5 |

Themes with at least one failing pill: **50 of 50**. Themes with all five
failing: **13**. Worst case across every theme and every pill: **1.42:1**.

After, measured on the values the server serves:

| Theme | Card | Worst pill | Passes |
|---|---|---|---|
| IntelliJ Light | `#FFFFFF` | 4.53:1 | yes |
| Macaw Light | `#FEFFFF` | 4.53:1 | yes |
| Catppuccin Latte | `#E6E9EF` | 4.54:1 | yes |
| base16-atelier-forest-light | `#F1EFEE` | 4.54:1 | yes |
| Booberry | `#3A224C` | 4.55:1 | yes |
| Nord | `#3B4252` | 4.56:1 | yes |
| Matrix | `#000000` | 4.58:1 | yes |
| Cyber Red | `#090808` | 4.55:1 | yes |

Themes with a failing pill: **0 of 50**. Worst case: **4.53:1**.

### The asset digests

| Asset | Before | After |
|---|---|---|
| `static/dashboard.css` | `5242e07e…` (oracle baseline) | `de75fad3…` |
| `static/dashboard.js` | `1a7ff048…` (oracle baseline) | `e78f7ef6…` |
| `static/chart.umd.min.js` | `0bae5b84…` | unchanged |
| `static/favicon.svg` | `a075c45b…` | unchanged |

`rust/assets/dashboard/manifest.json` carries the SHA-256 of every asset in the
working tree, and `rust/assets/runtime-manifest.json` carries the digest of
that manifest. Both were regenerated after the final asset edit.

### The oracle divergence record

`scripts/dashboard_parity/browser.py` records one reason per divergence. The
pre-existing reasons are retained — the stylesheet's still names the table
scroll wrapper, the script's still names the timeseries filter form, and the
reasons stay distinct per file. The new divergences carry their own reasons:

- `static/dashboard.css` — derive the status pill colours from the theme's own
  semantic tokens so every pill clears 4.5:1 against its card background in
  every bundled theme, and give the pill text the card background as its
  surface; retire the update indicator, account breakdown filter and compact
  chart container rules that no renderer produces, and add the skip link, focus
  indicators and print rules.
- `static/dashboard.js` — end the chart loading path in a rendered chart or a
  stated failure instead of an indefinite preparing state, without fetching the
  payload a second time; retire the update command copy handler and the script
  exports that no caller uses, and name the charts the script creates.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
```

### Results

| Command | Result |
|---|---|
| `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | clean |
| `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` | clean, no warnings |
| `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` | passes |
| `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings` | clean, no warnings |
| `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` | **1208 passed, 0 failed** across every target |
| `uv run pytest tests/tooling/ -q --tb=short` | **247 passed, 1 skipped** (the skip is the release-packaging artifact inspection, which needs a release build) |
| `uv run ruff format --check scripts/ tests/tooling/` | 71 files already formatted |
| `uv run ruff check scripts/ tests/tooling/` | all checks passed |
| `uv run pyright scripts/` | 0 errors, 0 warnings, 0 informations |
| `uv run python scripts/validate_release_docs.py` | pass, 7 documents checked, 0.8.2 published |

## 5. Invariant review

- **The dashboard stays observational.** No routing, quota, health, reload,
  generation, or persistence authority moved into the dashboard. The theme
  variables are read from the bundled theme assets; nothing was added to a
  routing or selection path.
- **The published JSON contract is unchanged.** No dashboard field name or
  response shape changed. The theme's CSS variable *set* grew by six derived
  pill variables, which is a presentation contract and not a JSON API.
- **The asset change is a recorded, intentional divergence.** The dashboard
  roadmap's asset-stability invariant requires an intentional frontend change
  to carry a reason per divergence. The record carries one, and an unrecorded
  divergence still fails the qualification.
- **No secret reaches the surface.** No credential, prompt, raw body, or cache
  key entered the rendered markup, the JSON APIs, the logs, the fixtures, or
  the screenshots.
- **The script stays DOM-safe.** `dashboard.js` contains no `innerHTML`,
  `insertAdjacentHTML`, or `outerHTML`; every DOM write is `textContent`, so
  no dynamic value can terminate the markup or the script.
- **The chart bootstrap data stays safe.** The chart payload is carried in a
  `application/json` script element and is written back with `textContent`.
- **No timer outlives its canvas.** The runtime wait clears its handle when
  it resolves, when it fails, and when it is superseded, and the per-canvas
  guard prevents a second wait from being armed for the same canvas.
- **No configuration key and no dependency** was added. `rust/Cargo.toml` and
  `rust/Cargo.lock` are unchanged, and no migration was added.

## 6. Failure and recovery review

- The chart loading path now states its failure. A browser that never receives
  Chart.js gets a shell whose state is `error` and a message that says so,
  rather than an indefinite "Preparing chart…".
- The focus indicators make the keyboard path through the navigation, the
  refresh control, the theme picker, and the filters visible, and the skip
  link makes the main content reachable in one keystroke.
- The print stylesheet gives an operator a readable black-on-white rendering
  of any page that carries data they need to keep.
- The theme picker no longer silently discards the operator's filters, so
  changing the theme is no longer a way to lose a scoped view.
- A truncated usage table says it is truncated, so the operator is not misled
  into reading a partial table as a complete one.

## 7. Migration and compatibility review

- No schema change and no migration.
- No configuration key was added, removed, or reinterpreted.
- No dependency was added; `rust/Cargo.toml` and `rust/Cargo.lock` are
  unchanged.
- No published dashboard field name or response shape changed, and no page
  route was added or removed. The theme picker's query parameters are now
  carried through, which is a behaviour change in the operator's favour and not
  a contract change.
- The theme stylesheet publishes six additional custom properties. A client
  that consumed the theme stylesheet directly sees six new variables and no
  changed ones.
- The retired stylesheet selectors and script exports were never part of a
  published contract; they had no producer.

## 8. Security review

- **No new attack surface.** The change is presentation only. No route, no
  authentication decision, no authorization decision, and no data path
  changed.
- **The member and administrator surfaces stay disjoint.** The standalone
  shells render the same navigation the shared layout renders, and the
  navigation is the same fixed set of destinations. The on-prem member
  surfaces keep their own routes and their own authorization.
- **No secret reaches the surface.** The dashboard renders no credential, and
  the change adds no field that could carry one.
- **The markup stays escaped.** Dynamic values remain escaped, and the script
  writes to the DOM only through `textContent`.
- **The contrast correction is a security-adjacent improvement.** A status
  pill that reads at 1.42:1 against its card cannot be distinguished from the
  card, so a degraded provider state and a healthy one can look the same. The
  correction restores a distinguishable foreground in every bundled theme.
- **The retirement removes dead code, not live paths.** Each retired selector
  and export was checked to have no producer, and the guard fails if one
  returns.

## 9. Documentation and operations

- `architecture/deep-dive-dashboard.md` — the presentation and accessibility
  contract: theme delivery, navigation chrome, table chrome, the derived
  status pills, the chart surface, accessibility, query parameters, the usage
  cap, and the retirement guard.
- `scripts/dashboard_parity/browser.py` — the per-divergence reason record.
- `tests/tooling/test_dashboard_parity_projection.py` — the matching
  inventory assertion.
- `rust/assets/dashboard/manifest.json` and `rust/assets/runtime-manifest.json`
  — the asset digests.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | The dashboard parity qualification could not be executed in this environment: it needs the frozen Python oracle worktree and a browser, neither of which is present here. The divergence inventory was verified by the tooling test instead, which asserts the manifest agrees with the working-tree bytes, that every divergence carries a reason, and that a recorded reason still describes a real divergence. | The paired visual comparison against the frozen oracle is unproven for this revision. | Run `scripts/qualification_dashboard_parity.py` with the pinned oracle worktree and a browser, and record the paired review. The tooling gate that stands in for it is green. |
| Low | `tests/fixtures/dashboard-on-prem-browser/reference.json` records the member-refusal body as the bare string `Administrator access required.`, which the On-prem M024 milestone changed to an HTML page. | The on-prem browser reference is stale for the member-denied observations. It is a physical-SBC artifact and is not consumed by CI. | Regenerate the reference on hardware with the on-prem browser qualification. |

## 11. Roadmap disposition

`plans/subsystems/dashboard-roadmap.md` is updated: M014 moves from `ready` to
`closed`, the milestone section records the closure, and the status table
records it. M001–M013 stay closed and immutable.

The subsystem's status returns to `closed`. The two findings in §10 are
recorded against the subsystem rather than carried by a new milestone,
because neither is a defect in the corrected behaviour: one is a qualification
that needs hardware and a browser, and the other is a stale physical-SBC
reference artifact.

## 12. Registry updates

- `plans/registry.md` — the Dashboard row moves from `active` to `closed`; the
  M014 entry moves from the dependency-ready table to the recently-closed
  table; the narrative paragraph records the closure.
- `plans/subsystems/dashboard-roadmap.md` — M014 is closed.
- No other subsystem's rows changed.
