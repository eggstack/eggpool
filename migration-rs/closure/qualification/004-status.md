# Q004 Closure — Dashboard SSR, DOM, Static Asset, and Visual Parity

Status: accepted; closed 2026-09-09

Implementation candidate: `73fad30f4eb79bcc75c38bd33a72c5b993f970cc`

Plan: [Q004 — dashboard SSR, DOM, static asset, and visual parity review](../../implementation/qualification/004-dashboard-dom-static-and-visual-parity.md)

## Outcome

Q004 is accepted. The Rust dashboard now exposes the complete 14-route page
matrix, preserves the shared SSR shell/navigation/form/theme semantics, and
passes deterministic Python/Rust comparisons for the empty first-run fixture.
The qualification remains qualification-only: Python is still the behavioral
oracle and no public cutover is made.

Machine evidence:

- [`004-run.json`](004-run.json), SHA-256
  `12c45e6b964bcef810a6bf0d3bfc90ba3ce822b312a1fc41b435afeb8859b612`;
  schema `m10-q004.v1`, 14 pages, 54 static/theme assets, 51 themes, and
  224 deterministic screenshot-coverage entries.
- [`004-run.md`](004-run.md), SHA-256
  `586f26de5a89b103225d2f4f617c02619e2f07c045c6b8a63a134d8dade3c2b6`.

## Page, state, theme, and viewport matrix

The route matrix is `/`, `/accounts`, `/models`, `/models/example-model`,
`/latency`, `/events`, `/timeseries`, `/bandwidth`, `/pings`, `/reliability`,
`/routing`, `/traces`, `/runtime`, and `/cache`. Every route was compared in
the deterministic empty/first-run state, with `period=24h` and `theme=Cyber
Red`; the model detail additionally used the escaped special-value fixture
`<model & "x">`.

The fixture contract reserves normal populated state, multiple
providers/accounts/models, recent success/failure/routing decisions,
long/unicode names, missing optional cost/model-info data, and public/private
mode. Public and private modes were both exercised for Python and Rust; the
private check asserted `401` without the bearer key and `200` with it.

The representative visual review set was `default`, `Cyber Red`,
`Catppuccin Latte`, and `Cyberpunk`, at `1440x900` desktop and `390x844`
mobile. The full theme inventory contains 51 names and is checked for exact
Python/Rust parity.

## DOM and semantic results

All 14 page comparisons passed. The projection compares title, stable page
IDs/classes, heading, active navigation, complete internal navigation path
set, form method/kind, expected static asset references, and duplicate-ID or
unsafe-link failures. It does not normalize semantic text, ordering,
escaping, missing links, or missing controls; only the requested period/theme
query values and route-specific dynamic detail text are treated as fixture
parameters.

Escaping passed for `<`, `>`, `&`, quotes, and the model-detail fixture. The
deliberate active-navigation mismatch test fails as expected, proving the
comparison does not over-normalize. Internal links are crawled against the
same route inventory and static references resolve.

## Static assets and visual review

The exact inventory is 4 dashboard-owned static assets plus 50 theme files,
for 54 files total. The manifest and copied Rust bytes match the Python source
hashes; served CSS/JS content types, bytes, and cache metadata match the
qualification contract. Asset inventory digest:
`e7d1e10c243dd699669b366fa9820d53a26a572308e26105b352861830d80904`.

Browser review used the isolated local Rust fixture and found no material
desktop/mobile overflow, navigation, theme, escaping, or empty-state
regression. Review artifacts were kept outside the repository:

| Artifact | View | SHA-256 |
|---|---|---|
| `q004/visual/rust-overview--desktop--cyber-red.png` | overview, 1440x900, Cyber Red | `3a350fc0715c395d0b22e9e1721f86bc75ae9fd1998d4ac800f0e0db842ac9c0` |
| `q004/visual/rust-overview--mobile--catppuccin-latte.png` | overview, 390x844, Catppuccin Latte | `800e36f4a28fd57401c9cdc386c77afc8ef4f0f4222aa44b228877dedfbe735d` |
| `q004/visual/rust-model-detail--mobile--cyberpunk.png` | model detail, 390x844, Cyberpunk | `957e563c20f834eafab22b46239918fa75551819c0c3c849d3d7b0566998a40b` |

The complete route/theme/viewport coverage remains reproducible from the
metadata manifest in `004-run.json`; binary captures are intentionally not
committed.

## Findings and corrections

The following mismatches were corrected and then re-run green:

1. Before: Rust exposed only the overview and summary endpoint. After: all 14
   current dashboard page routes render with the shared navigation, active
   state, period control, empty-state content, model-detail escaping, and
   static asset hooks.
2. Before: Rust served dashboard JavaScript as `text/javascript` with a
   five-minute cache. After: it matches Python's `application/javascript`
   content type and one-day cache policy.
3. Before: the Rust overview omitted most current navigation and the chart
   hooks. After: it carries the full nav/path inventory, chart preload/script,
   and stable `timeseries-chart`/`timeseries-initial-data` hooks.
4. Before: no qualification caught an active-page semantic mismatch. After:
   the deliberate mismatch test fails and the corrected candidate passes.

No intentional visual differences remain in the shared dashboard contract.
The data-rich populated fixtures remain owned by the Python oracle and later
qualification coverage; Q004 does not redesign or replace that oracle.

## Verification and transition

Passed commands:

- `uv run python scripts/qualification_dashboard.py --screenshots`
- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`
- `uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 14 passed
- `uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 131 passed, 3 skipped
- `git diff --check`

The full Rust all-target command was run with the required single-threaded
flag and completed with every test binary reporting `ok` and no failures.
Q004 has no unresolved findings. Per the planning process, Q004 is accepted
and only its direct successor is promoted: Q005 is now **ready**; Q006–Q010
remain queued, and M11 remains blocked on accepted Q010 plus its separate
planning review.
