# Dashboard Milestone 014 — Operator surface presentation and accessibility corrective

Status: ready

Repository baseline: `d04ad6831c6e030b7f5fc8a56501fdfecab982bd`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for observed facts and derived projections
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. This milestone is the "intentional separately reviewed frontend
  change" the dashboard roadmap's asset-stability invariant requires, and it
  does not select a new frontend framework, protocol, authentication model, or
  runtime owner.
- Stop for architecture review if the corrections would change a published
  dashboard field's value or shape, because the embedded dashboard assets and
  the parity tooling are compatibility consumers.

Primary class: capability

## 1. Objective

Make the operator-facing surfaces render the way the product intends, and
remove the parts of the frontend that can never work.

The dashboard's theme, table chrome, chart loading, and status reporting are
part of what the product presents to an operator. At the current baseline the
on-prem pages that an administrator and a member actually use are rendered
without the theme at all, the administrative tables are rendered without the
table chrome, the status pills are painted with fixed colours that fail
contrast in the light themes, the chart surface can strand itself on a slow
asset load, and a cluster of retired frontend code is still carried in the
stylesheet and the script. This milestone corrects those, and states the
oracle-divergence bookkeeping the asset change requires.

## 2. Why this milestone is ready

No dependencies. Every defect is reproducible from the repository at the stated
baseline, and every correction is confined to the dashboard's own renderers,
its embedded assets, and the parity tooling's divergence record. No other
milestone's closed contract is reopened, and no hard dependency on another
subsystem's milestone exists.

On-prem user access M024 is a separate corrective plan in a separate
subsystem. It is independent of this one and the two may proceed in parallel;
their file ownerships are disjoint.

## 3. Current implementation evidence

**The stylesheet declares no custom properties, so a page that does not load
the theme stylesheet is unstyled.** `rust/assets/dashboard/static/dashboard.css`
has no `:root` block and no custom-property declaration of its own;
`rust/src/server/dashboard/assets.rs:31-36` says so explicitly. Every colour
in it resolves through `var()`, and the complete variable set comes from
`/static/theme.css`, which `rust/src/server/dashboard/assets.rs:37` serves and
which resolves an absent or unknown `theme` parameter to the default theme.

`/static/theme.css` is linked from exactly one place in the repository:
`rust/src/server/dashboard/render/layout.rs:164`. Every other page shell links
`dashboard.css` alone, so every `var()` in those pages is invalid — no page
background, no panel fill or border, no themed buttons, tables, or scrollbars:

- `rust/src/server/dashboard/render/admin.rs:159` — `on_prem_page`, the shell
  behind the administrator usage page and the member usage page
- `rust/src/server/dashboard/admin.rs:339` — the administrative audit page
- `rust/src/server/dashboard/admin.rs:365` — the my-account page
- `rust/src/server/dashboard/admin.rs:405` — the my-models page
- `rust/src/server/dashboard/admin.rs:182` — the one-time personal-token page

This does not reopen the accepted M011 correction. M011 fixed the member and
administrator usage pages rendering as bare fragments with no document, no
stylesheet, and no viewport declaration, and it closed that by giving them the
shared `on_prem_page` shell (`plans/closure/on-prem-user-access/011-status.md:73-77`).
That shell links the dashboard stylesheet, but the dashboard stylesheet carries
no custom properties of its own, so the variable-dependent part of the
presentation was never delivered and was not part of what M011 corrected. The
defect here is the layer beneath the shell, not the shell.

**The administrative tables are rendered without the table chrome.** The
stylesheet styles `table.data` (`rust/assets/dashboard/static/dashboard.css:520-538`)
and that is the only table selector it has. Seven tables are emitted as a bare
`<table>`, so they get no cell padding, no row borders, no header background,
and none of the `min-width: max-content` that makes the surrounding
`.table-scroll` region scroll horizontally on a narrow viewport:

- `rust/src/server/dashboard/render/admin.rs:100` — instance budget ceilings
- `rust/src/server/dashboard/render/admin.rs:104` — local accounts
- `rust/src/server/dashboard/render/admin.rs:183` — administrator usage
- `rust/src/server/dashboard/render/admin.rs:235` — member usage limits
- `rust/src/server/dashboard/render/admin.rs:241` — member settled usage
- `rust/src/server/dashboard/admin.rs:339` — administrative audit
- `rust/src/server/dashboard/admin.rs:365` — personal client credentials

**Two usage pages are nav-less dead ends.** `on_prem_page`
(`rust/src/server/dashboard/render/admin.rs:157-163`) emits a bare `<main>` with
no header and no navigation, but the shared navigation links "Usage" →
`/usage` (`rust/src/server/dashboard/render/layout.rs:93`). An administrator
who follows that link leaves the navigation chrome and has no route back to
the overview from the page. The administrative audit page
(`rust/src/server/dashboard/admin.rs:339`) is the same shape and is reachable
only from the administrator usage page.

**The status pills are painted with fixed colours that ignore the theme.**
`rust/assets/dashboard/static/dashboard.css:1730-1769` hardcodes `#22c55e`,
`#eab308`, `#f97316`, `#ef4444`, and `#a855f7` for the fresh, partial, sparse,
stale, conflict, and source-unavailable pills. The theme already exposes
`--color-success`, `--color-warning`, `--color-error`, and `--color-info`
(`rust/src/server/dashboard/theme.rs:367-370`), and the same stylesheet uses
them correctly at `:348-351` and `:595-596`. The contrast for the fixed
values is computed analytically from the bundled theme TOMLs rather than
measured in a rendering engine, and lands between roughly 1.4:1 and 2.4:1
against a light theme's card background — far below the 4.5:1 WCAG AA
threshold for the 0.8em text the pills use. Six bundled light themes are
affected. The stylesheet's own comment at `:1771-1779` records the same
measurement method applied to `--text-muted` across all fifty themes, so the
method is established and reusable.

**A slow Chart.js load strands the charts.** `whenChartReady`
(`rust/assets/dashboard/static/dashboard.js:237-258`) polls for `window.Chart`
two hundred times at fifty milliseconds and, on giving up, only logs a warning.
The recovery path — `initChartLoadingShells`, which draws the payload already
fetched and cached at `dashboard.js:656` — is invoked only inside that
callback (`dashboard.js:817-821`). `renderIntoShell` caches the data and shows
"Preparing chart…" when `window.Chart` is undefined (`dashboard.js:655-659`),
so after ten seconds the shell spins on that message forever with the data
cached and never plotted. The script's own comment at `dashboard.js:239-241`
names the case: a low-bandwidth SBC deployment.

**The chart surface and the refresh status are not announced.** A
script-created canvas receives only an `id` (`dashboard.js:668-669`), and the
renderer-created canvases (`rust/src/server/dashboard/render/telemetry.rs:339`,
`rust/src/server/dashboard/render/overview.rs:589`) carry no `role`, no
`aria-label`, and no fallback text, so the chart's data is unavailable to a
screen reader. `showState` (`dashboard.js:636-640`) mutates the shell's
`textContent` inside a container with no `role="status"` and no `aria-live`,
while the renderers do use `role="status"` elsewhere
(`rust/src/server/dashboard/render/layout.rs:26`). The refresh indicator
`#dashboard-updated` (`rust/src/server/dashboard/render/layout.rs:159`) receives
`textContent` updates (`layout.rs:202`, `:227`, `:246`) with no live region,
and `#dashboard-content` is replaced wholesale (`layout.rs:226`) with no
announcement, so a screen-reader user cannot tell that data refreshed, paused,
or went stale.

**There is no skip link.** `layout.rs:164` places seventeen navigation links,
the theme picker, and the refresh button ahead of `<main id="dashboard-content">`,
and neither the layout nor the stylesheet provides a skip target.

**A cluster of retired frontend code is still carried.** The update-indicator
feature has no producer in the current Rust renderers. The stylesheet rules
`footer .footer-update-indicator`, `footer .update-indicator`,
`footer .update-indicator .update-versions`,
`footer .update-indicator .update-command`, and
`footer .update-indicator .update-copied`
(`rust/assets/dashboard/static/dashboard.css:193-267`) and the script function
`initUpdateCommandCopy` (`rust/assets/dashboard/static/dashboard.js:1279-1346`)
key off `data-update-command` and `data-update-copied`. A repository-wide
search finds those attributes only in the stylesheet, the script, and the
retired Python oracle (`scripts/dashboard_parity/oracle.py:106`). The Rust
footer (`rust/src/server/dashboard/render/layout.rs:158-162`) emits no
update-indicator markup at all.

Three further selectors are stale in the same way. The stylesheet targets
`.panel-header form.account-breakdown-filter` (`dashboard.css:453`), but the
live class is `account-filters`
(`rust/src/server/dashboard/render/accounts.rs:209`) and it has no rule of its
own. `td.yes` and `td.no` (`dashboard.css:595-596`) are never emitted — the
renderers put "yes" and "no" in the cell as text
(`rust/src/server/dashboard/render/accounts.rs:115`, `:169`). And
`.chart-container-compact` (`dashboard.css:629-634`) has no producer, although
the script still queries it (`dashboard.js:921`).

Three classes the renderers emit have no rule. `.num`
(`rust/src/server/dashboard/render/cache.rs:68`, `:236`) marks a numeric column
that is not right-aligned. `.empty-state`
(`rust/src/server/dashboard/render/runtime.rs:159`) renders at full body-text
weight, where the working `.empty` class (`dashboard.css:838-841`) renders a
muted italic placeholder. `.compact` is emitted on many pages
(`rust/src/server/dashboard/render/overview.rs:307`) and has no rule, so the
modifier does nothing.

Five script exports are never called: `fetchStats` (`dashboard.js:6`),
`formatAgeSeconds` (`:49`), `formatPercent` (`:82`), `formatBytes` (`:115`),
and `formatMicrodollars` (`:134`).

**The theme picker discards the page's own query.** The theme form is a GET
form with no action (`rust/src/server/dashboard/render/layout.rs:145`), so
submitting it replaces the whole query string with `period` and `theme`. On a
page whose state lives in the query — the timeseries model filter, the
bandwidth account filter — changing the theme silently resets that state.

**Usage tables truncate silently.** The member usage query is
`usage(user_id, "0000-01-01", 500)` (`rust/src/server/dashboard/admin.rs:289`),
the administrator usage query ends in `LIMIT 500`
(`rust/src/server/dashboard/admin.rs:304`), and the underlying query is
`LIMIT ?3` (`rust/src/user_budget.rs:362`). The rendered tables
(`rust/src/server/dashboard/render/admin.rs:219-243`) show no indication that
rows were dropped.

**The asset change has a recorded governance path.** The embedded assets
already diverge from the frozen Python oracle. `rust/assets/dashboard/manifest.json`
records the current SHA-256 of every asset,
`scripts/dashboard_parity/browser.py:11-21` records one correction reason per
diverging file, and `scripts/dashboard_parity/browser.py:37-40` fails the
qualification when the manifest and the working-tree bytes disagree.
`scripts/dashboard_parity/browser.py:61-67` fails when a recorded reason no
longer describes a real divergence, and `:47-52` fails when a divergence has
no recorded reason. The tooling test
`tests/tooling/test_dashboard_parity_projection.py:71-84` asserts the asset
count, the exact set of diverging paths, and the reason text.

`rust/src/server/dashboard/tests.rs:86` asserts the exact number of `--`
occurrences in a rendered theme's variable block, which is the count of theme
variables. Adding a theme variable changes that number deliberately.

## 4. Invariants that must not regress

- The dashboard remains observational. It never becomes a routing, quota,
  health, reload, generation, or persistence authority.
- Credentials, raw prompts, request or provider bodies, cache keys, and other
  secret-bearing content never enter dashboard persistence, rendered HTML, JSON
  APIs, logs, fixtures, screenshots, or closure evidence.
- Dynamic HTML remains escaped, and chart and bootstrap JSON remain safe
  against markup or script termination.
- The dashboard's server-rendered HTML and its JSON endpoints remain
  consistent with each other for every fact they both present.
- The published dashboard JSON field names and shapes do not change.
- The frozen Python oracle remains the compatibility reference for frontend
  structure and behavior. The current Rust implementation remains the
  authority for runtime semantics.
- Every divergence between the current assets and the frozen oracle remains
  recorded with a reason that describes that divergence, and an unrecorded
  divergence still fails the qualification.
- The theme system continues to serve a complete variable set for every
  selectable theme, including `default`.
- The auto-refresh loop continues to not stack intervals, not duplicate charts,
  and not keep detached DOM work alive.
- The dashboard's authentication posture is unchanged: the public/private
  exemption boundary and the browser-session boundary are untouched.
- No new dependency, and no secret-bearing content in any new artifact.

## 5. Scope

### In scope

- Theme delivery on the dashboard-owned standalone shells, so that every page
  an administrator or a member can reach resolves a complete variable set.
- The navigation chrome and a route back to the overview on the standalone
  shells that currently have neither.
- The table chrome on the seven administrative tables that lack it.
- The status pills' colours, derived from the theme's own semantic tokens,
  with a contrast guard across every bundled theme.
- The chart loading path, so that a slow or failed Chart.js load renders the
  data or an explicit failure instead of an indefinite spinner.
- An accessible name for every chart canvas, a live region for the chart
  loading state and the refresh indicator, and a skip link.
- The removal of the retired update-indicator, `account-breakdown-filter`,
  `td.yes`, `td.no`, and `.chart-container-compact` surface, and of the five
  unused script exports.
- A rule for `.num`, a rule for `.empty-state`, and either a rule for
  `.compact` or its removal from the renderers.
- Focus visibility for the navigation links, the refresh control, the theme
  and period selectors, and the filter-form controls.
- A print stylesheet.
- The theme picker preserving the page's own query parameters.
- Disclosure of the usage tables' row cap.
- The oracle-divergence bookkeeping for the asset change.
- Regression tests for each of the above.

### Explicitly out of scope

- Any change to a published dashboard field's value or shape.
- Any change to the dashboard's route surface, its JSON endpoints, its
  authentication posture, or its public/private exemption boundary.
- Any change to the inference path, the personal-credential path, the model
  policy, the budget service, or the durable schema.
- The sign-in, account, and member surfaces, which belong to On-prem user
  access M024.
- The CSRF cookie contract and the sign-in throttle.
- Any new dashboard page, panel, chart, or field.
- Any change to the frozen oracle's captures, manifest, or fixtures.
- A theme redesign, a frontend framework, a bundler, or an SPA conversion.
- Any performance work on the query or projection path beyond the usage row
  cap disclosure.

## 6. Required production changes

**Theme delivery on the standalone shells.** Every dashboard-owned shell must
link the theme stylesheet in addition to the dashboard stylesheet, resolving
the theme through the same `selected_theme` the shared layout uses so that a
theme carried in the request query applies and an unknown or absent theme
falls back to the default. The sign-in surface's own request for the theme
stylesheet is the other milestone's concern and does not interact with this
one.

The five shells named in §3 must all resolve a complete variable set. A page
that still resolves an invalid `var()` is a defect.

**Navigation chrome on the standalone shells.** The shells that are reachable
from the shared navigation must render the same navigation chrome as the
dashboard pages, so that following a navigation link does not drop the
operator out of the product and there is always a route back to the overview.
The one-time personal-token page is a deliberately one-shot surface reached
from a credential issuance, and it is not a navigation destination; the
milestone may leave it without the full chrome provided it resolves the theme.

**Table chrome on the administrative tables.** The seven bare tables must carry
the class the stylesheet's table rules target, so that cell padding, row
borders, the header background, and the horizontal scroll behaviour apply to
the administrative surfaces exactly as they apply to the operator pages. The
class must be applied through the shared table helpers where one exists, so
the next administrative table cannot miss it.

**Theme-derived status pill colours.** The pill foreground colours must come
from the theme's own semantic tokens rather than from fixed hexadecimal
literals. Where the existing token set does not distinguish two pills that
must remain distinguishable, the milestone may add a derived theme variable,
in which case the count asserted by `rust/src/server/dashboard/tests.rs:86`
changes deliberately and the change is recorded. The requirement is
measurable: every pill foreground must reach at least a 4.5:1 contrast ratio
against the card background of every bundled theme, and the pills must remain
mutually distinguishable.

The contrast guard must be computed from the same theme values the server
serves, not from a separately maintained table, so that a theme change cannot
silently invalidate it.

**Chart loading fallback.** The chart loading path must terminate in a
rendered chart or an explicit failure state, and must not depend on
Chart.js having loaded within a fixed window. The already-fetched payload must
be drawn when Chart.js becomes available, and a load that never completes must
produce a stated failure rather than an indefinite "Preparing chart…" message.

**Chart and status accessibility.** Every chart canvas must carry an
accessible name that identifies the chart and its subject. The chart loading
shell must announce its state changes, the refresh indicator must be a polite
live region, and the main content must be reachable by a skip link. The
announcement must not be so frequent that it is unusable.

**Retired frontend surface removal.** The update-indicator stylesheet rules
and the `initUpdateCommandCopy` function must be removed together with their
selectors, because the feature has no producer. The stale
`account-breakdown-filter`, `td.yes`, `td.no`, and `.chart-container-compact`
selectors must be removed or given their live producer. The five unused script
exports must be removed. A guard must fail if a retired selector or an unused
export is reintroduced.

**Focus visibility, print, and the theme picker.** The navigation links, the
refresh control, the theme selector, the period selector, and the filter-form
controls must have a visible focus indicator consistent with the ones the
stylesheet already defines. A print stylesheet must drop the page background,
the sticky topbar, and the decorative overlay, and keep the content readable
in black on white. The theme picker must preserve the page's own query
parameters so that changing the theme does not reset a filter.

**Usage row cap disclosure.** Where the usage query's row cap can drop rows,
the rendered page must say so, so that an operator reading a truncated table
is not misled about the total.

**Oracle divergence bookkeeping.** `rust/assets/dashboard/manifest.json` must
be updated with the new SHA-256 of the changed assets. The recorded correction
reasons in `scripts/dashboard_parity/browser.py` must describe the new
divergence set: the existing divergences remain true and must keep their
reasons, and each new divergence must be recorded with its own reason rather
than folded into an unrelated one. If one file now carries more than one
divergence, the recorded structure must be extended so that each divergence
keeps its own reason, and the tooling test must be updated to match.

## 7. Ordered work packages

### Work package A — Theme delivery on the standalone shells

Intent:

Make every dashboard-owned page resolve a complete variable set.

Required changes:

- Link the theme stylesheet on the five shells named in §3, resolving the
  theme through the shared resolution.
- Ensure the theme query parameter is read by the shells that can carry one.

Acceptance evidence:

- A test asserts each of the five shells links both stylesheets and that the
  theme parameter is resolved rather than defaulted unconditionally.
- A test asserts that a shell rendered with an unknown theme parameter still
  resolves to a complete variable set.

### Work package B — Navigation chrome on the standalone shells

Intent:

Give the operator a route back to the overview from every page the navigation
can reach.

Required changes:

- Render the shared navigation chrome on the usage and audit shells.
- Keep the one-time token page's one-shot character.

Acceptance evidence:

- A test asserts the usage and audit shells contain the navigation and a link
  to the overview.
- A test asserts the shared navigation's existing destinations are unchanged.

### Work package C — Table chrome on the administrative tables

Intent:

Give the administrative tables the same presentation as the operator pages.

Required changes:

- Apply the table class the stylesheet targets to the seven bare tables,
  through the shared helper where one exists.

Acceptance evidence:

- A test asserts every table the dashboard renderers emit carries the table
  class, so a future table cannot miss it.
- A test asserts the administrative tables are still inside a keyboard
  reachable scroll region.

### Work package D — Theme-derived pill colours with a contrast guard

Intent:

Make the status pills readable in every bundled theme.

Required changes:

- Replace the fixed hexadecimal literals with theme-derived values.
- Add the contrast guard, computed from the same theme values the server
  serves.

Acceptance evidence:

- A test asserts every pill foreground reaches at least 4.5:1 against its
  theme's card background, for every bundled theme.
- A test asserts the guard fails when a pill colour is replaced with a failing
  value, so the guard is real.
- A test asserts the pills remain mutually distinguishable.

### Work package E — Chart loading fallback

Intent:

Make the chart surface terminate.

Required changes:

- Render the cached payload whenever Chart.js becomes available, and produce
  an explicit failure state when it never does.

Acceptance evidence:

- A test asserts that with Chart.js unavailable the shell reaches a stated
  failure or a rendered chart, and never remains on the preparing message.
- A test asserts that the payload is not fetched twice.

### Work package F — Chart and status accessibility

Intent:

Make the chart surface and the refresh status perceivable without sight.

Required changes:

- Give every chart canvas an accessible name.
- Make the chart loading shell and the refresh indicator live regions.
- Add a skip link to the main content.

Acceptance evidence:

- A test asserts every canvas the renderers and the script emit carries an
  accessible name.
- A test asserts the refresh indicator and the chart shell carry the live
  region attributes.
- A test asserts the skip link is the first focusable element and targets the
  main content.

### Work package G — Retired frontend surface removal

Intent:

Stop carrying frontend that can never run.

Required changes:

- Remove the update-indicator rules and the `initUpdateCommandCopy` function
  together.
- Remove or repair the `account-breakdown-filter`, `td.yes`, `td.no`, and
  `.chart-container-compact` selectors.
- Remove the five unused script exports.
- Give `.num` and `.empty-state` rules, and either give `.compact` a rule or
  stop emitting it.
- Add the guard that fails on reintroduction.

Acceptance evidence:

- A test asserts no stylesheet selector and no script export has no producer
  in the current renderers and the current script.
- A test asserts the update-indicator selectors and the function are absent.

### Work package H — Focus visibility, print styles, and the theme picker

Intent:

Make the interface usable by keyboard, printable, and non-destructive to use.

Required changes:

- Add the missing focus indicators.
- Add a print stylesheet.
- Preserve the page's query parameters through the theme picker.

Acceptance evidence:

- A test asserts the interactive controls that lack a focus rule now have one.
- A test asserts the print stylesheet drops the page background and the
  sticky topbar.
- A test asserts that submitting the theme picker preserves the page's other
  query parameters.

### Work package I — Usage row cap disclosure

Intent:

Stop an operator reading a truncated table as a complete one.

Required changes:

- Report the cap, and the truncation, on the rendered usage pages.

Acceptance evidence:

- A test asserts a usage page at the cap states that the table is truncated.

### Work package J — Oracle divergence bookkeeping

Intent:

Keep the divergence record accurate when the assets change.

Required changes:

- Update `rust/assets/dashboard/manifest.json` with the new asset digests.
- Extend the recorded correction reasons so that each divergence keeps its own
  reason, and update the tooling test to match.
- Re-run the parity qualification and review the paired captures for the
  pages the asset change affects.

Acceptance evidence:

- The tooling test asserts the new diverging set and the per-divergence
  reasons.
- A test asserts that an unrecorded divergence still fails the inventory.
- The parity qualification runs and the browser dispositions are reviewed.

## 8. Failure, cancellation, restart, contention semantics

- No change adds a durable write, a second database round trip, or an await
  into a database call.
- The theme resolution stays a pure function of the request and the bundled
  theme bytes. It must not read the database.
- The chart fallback must not create a second fetch of the same payload, and
  must not leave an interval armed that outlives the canvas it updates. The
  existing handle-clearing discipline at `dashboard.js:534-538` and
  `:701-704` is the pattern.
- The live regions must not announce on every refresh tick in a way that makes
  a screen reader unusable. The refresh interval and the announcement cadence
  are the same concept here, so the announcement must be tied to the existing
  refresh note rather than to a new timer.
- The usage cap disclosure must be derived from the same query that applies
  the cap, so the page cannot claim a complete table when the query truncated.
- No cancellation path is added or changed.
- No restart behavior changes. The assets are embedded at compile time, so a
  deployed binary carries the corrected bytes.

## 9. Compatibility and migration

- No storage migration. No table, column, index, or schema version changes.
- No configuration key is added, removed, or reclassified in
  `rust/src/config_reload_policy.rs`.
- No published dashboard JSON field name or shape changes, and no page route
  is added or removed.
- The embedded dashboard assets change. This is the intentional frontend
  change the dashboard roadmap's asset-stability invariant requires, and it
  changes the divergence from the frozen oracle, which stays recorded with a
  reason per divergence.
- The rendered HTML of the administrative surfaces changes: the theme
  stylesheet link, the navigation chrome, and the table class. The operator
  pages' DOM contract is unchanged apart from the skip link and the
  accessibility attributes.
- If a theme variable is added, the number asserted by
  `rust/src/server/dashboard/tests.rs:86` changes deliberately and is
  recorded in the closure evidence.
- The scripts and stylesheet are served with the existing cache headers
  (`max-age=300` for the stylesheet, `max-age=86400` for the script). A
  deployment that changes the script therefore leaves operators on the
  previous script for up to a day. The deployment note must say so.

## 10. Required tests

- `rust/src/server/dashboard/tests.rs` — the in-module dashboard tests: the
  theme delivery on the standalone shells, the table class on every emitted
  table, the navigation chrome, the usage cap disclosure, and the theme
  variable count if it changes.
- `rust/src/server/dashboard/theme.rs`'s in-module tests, or the nearest
  existing target — the contrast guard, computed from the values the server
  serves.
- A focused new target for the stylesheet and script guards, if the in-module
  tests cannot host them.
- `tests/tooling/test_dashboard_parity_projection.py` — the asset inventory
  and divergence record, updated for the new divergence set.
- `scripts/qualification_dashboard_parity.py` — the parity qualification and
  the browser dispositions, re-run against the changed assets.
- `rust/tests/on_prem_admin_ui.rs` — the browser-driven on-prem surface, for
  the administrative pages' rendered appearance and the chart surface.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short
uv run python scripts/qualification_dashboard_parity.py
```

Plus the narrowest focused target for each work package. Do not claim commands
not actually run in the closure record.

## 12. Documentation updates

- `architecture/deep-dive-dashboard.md` — the theme delivery rule for every
  page shell, the table chrome rule, the accessibility contract for the chart
  surface and the refresh status, and the recorded oracle divergence.
- `architecture/overview.md` — only if the change alters the module ownership
  map. It is not expected to.
- `docs/` — only if an operator-facing document describes the dashboard's
  appearance or the chart behavior. Check before editing; do not add a new
  document.

## 13. Acceptance criteria

- Every dashboard page an administrator or a member can reach resolves a
  complete theme variable set, with no invalid `var()`.
- The administrative tables carry the table chrome, and a future table cannot
  be emitted without it.
- Every status pill reaches at least 4.5:1 against its theme's card
  background, in every bundled theme, and the guard fails on a deliberate
  regression.
- The chart surface reaches a rendered chart or an explicit failure, never an
  indefinite preparing state.
- Every chart canvas has an accessible name, the chart shell and the refresh
  indicator are live regions, and a skip link is the first focusable element.
- No retired stylesheet selector or unused script export remains, and a guard
  fails on reintroduction.
- The navigation links, the refresh control, and the selectors have visible
  focus indicators, and a print stylesheet drops the page chrome.
- Changing the theme preserves the page's other query parameters.
- A truncated usage table says so.
- The asset change is recorded with a reason per divergence, the asset
  manifest matches the working-tree bytes, and an unrecorded divergence still
  fails the qualification.
- The parity qualification passes and the browser dispositions are reviewed.
- No published dashboard field name or shape changes, and no schema,
  configuration, or dependency change.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- A correction would change a published dashboard field's value or shape.
- The theme correction would require a configuration key, or would change the
  server's behaviour rather than the rendered result.
- The chart fallback would add a second payload fetch, or would arm an
  interval that outlives the canvas it updates.
- The accessibility work would require a live region that announces on a
  cadence no operator could use.
- The oracle divergence record cannot be kept accurate for a changed asset, in
  which case the asset change must be split out and reviewed on its own.
- The parity qualification's browser dispositions cannot be reviewed for the
  changed pages.
- Evidence contradicts a dashboard invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- The contrast table for every pill against every bundled theme, before and
  after.
- The asset digest table before and after, and the per-divergence reason
  record.
- The parity qualification result and the reviewed browser dispositions.
- Any severity-tagged unresolved finding, with a recommendation.
- The registry update that closes this milestone, in its own commit.

## 16. Handoff notes

This is a corrective pass over a closed milestone family. M001–M013 remain
closed and immutable. This plan does not reopen any of them and does not
revisit the parity dispositions that M006 accepted, the module decomposition
that M009 performed, or the projection reconciliation that M013 performed.

The file ownership is disjoint from the on-prem corrective plan: this
milestone owns `rust/src/server/dashboard/**`, `rust/assets/dashboard/static/**`,
`rust/assets/dashboard/manifest.json`, and the divergence record in
`scripts/dashboard_parity/browser.py`. The on-prem plan owns
`rust/src/server/auth.rs`, `rust/src/web_session.rs`, and the member refusal
in `rust/src/server/middleware.rs`. The two may proceed in parallel.

The asset change is the highest-risk part of this milestone, because the
embedded assets are a compatibility surface with a frozen oracle and a
recorded divergence mechanism. Work package J is not a bookkeeping afterthought;
it is what makes the asset change reviewable. Do not change an asset and leave
the divergence record to be updated later.

Rust tests must run serially. Strict Clippy with `-D warnings` is an invariant
of this repository. Preserve unrelated user changes in the working tree.
