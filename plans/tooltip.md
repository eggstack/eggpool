# CSS Tooltip System Plan

## Objective

Replace the native `<title>` tooltip on the dashboard heatmap with a styled CSS tooltip that shows the date plus all available metrics (tokens or bytes, plus request count). Build this as a generalizable `[data-tooltip]` system so any HTML element in the dashboard can opt in with a single attribute.

The system must:

- Match the active theme via existing CSS custom properties (no new color values).
- Survive the auto-refresh `innerHTML` swap that runs every 15-60 seconds on the overview page (`src/eggpool/dashboard/render.py:202-235`).
- Keep the existing `<title>` element on SVG `<rect>` cells so `tests/unit/test_dashboard.py:888` (`test_renders_tooltip`) continues to pass.
- Add `aria-label` to every tooltip target so screen readers announce the same text that visual users see.
- Require no new JavaScript and no new dependencies.

## Current state

The heatmap is rendered entirely server-side from `_render_bandwidth_heatmap()` in `src/eggpool/dashboard/render.py:557-692`. It emits an inline SVG with one `<rect class="heatmap-cell">` per row in `daily_data`. Each rect carries an inline `fill="#…"` color and a native `<title>` child element holding the tooltip text:

```text
2026-03-05: 1,204 tokens
```

There is no other tooltip implementation anywhere in the codebase. No CSS framework is in use. The stylesheet is one file at `src/eggpool/dashboard/static/dashboard.css` (341 lines), already uses CSS custom properties and `color-mix()` for theming, and is the single place new CSS rules belong.

The data row feeding each cell already has everything we need but currently the renderer only formats one field per cell:

| Field | Source | Currently used |
|---|---|---|
| `day` | `stats/queries.py:380-409` | yes (date label only) |
| `total_tokens` | same query | yes when `value_field="total_tokens"` |
| `bytes_received`, `bytes_emitted` | same query | yes when `value_field="bytes"` |
| `request_count` | same query | **NO** |

The formatters `format_bytes()` (`render.py:580`) and `format_tokens()` (`render.py:586`) already exist and will be reused.

The dashboard theme is selected server-side and emitted as a stylesheet at `/static/theme.css?theme=<name>`. The current heatmap uses inline `fill="#…"` so the cells do not re-theme at the CSS layer; the SVG is regenerated whenever the theme changes. The new tooltip system uses theme CSS variables (`--card-bg`, `--card-border`, `--page-text`, `--tag-*-bg`) so it re-themes automatically.

## Non-goals

- Do not add any JavaScript tooltip libraries (Floating UI, Tippy.js, Popper, etc.).
- Do not change the heatmap SQL or the data shape returned by `StatsService.get_bandwidth_timeseries()`.
- Do not rewrite the Chart.js timeseries chart tooltip on the overview page (Chart.js manages its own tooltips natively and is out of scope).
- Do not remove the `<title>` element from SVG rects; it stays as a fallback and to satisfy the existing test.
- Do not migrate cells from inline `fill="#…"` to `var(--heatmap-N)` in this pass — that is a separate refactor and is not required to ship the tooltip.
- Do not implement auto-flip-on-edge positioning (top row flipping to bottom when clipped). Add it later if visual testing reveals clipping.
- Do not implement variant styles (success, warning, error tooltips). The position modifier hook is sufficient for the first pass.

## Design

### 1. Tooltip CSS rule

Append a single, reusable block to the bottom of `src/eggpool/dashboard/static/dashboard.css`. Everything is keyed off the `[data-tooltip]` attribute selector so it can be applied to any HTML element in the dashboard.

Behavior:

- `:hover` and `:focus` trigger the tooltip.
- The bubble is built with `::after`; a small triangle arrow uses `::before`.
- `content: attr(data-tooltip)` reads the text directly from the attribute.
- `white-space: pre` so multi-line content (using `\n`) renders naturally.
- Theme-aware colors via `var(--card-bg)`, `var(--card-border)`, `var(--page-text)`.
- Theme-aware shadow via `color-mix(in srgb, var(--page-text) 12%, transparent)` (matches existing usage at `dashboard.css:35`, `:70-71`).
- `pointer-events: none` on the bubble so it does not steal hover from neighbours.
- Short fade transition (120ms) and reduced-motion support.
- Default position: above the element. `[data-tooltip-pos="bottom"]` flips below for use cases where the element sits at the top of a clipped container.

Two stacked selectors keep the rule compact:

```text
[data-tooltip]                       -> sets position: relative on target
[data-tooltip]::after                -> bubble (content, theme, position)
[data-tooltip]::before               -> arrow triangle
[data-tooltip]:hover::after,
[data-tooltip]:focus::after,
[data-tooltip]:hover::before,
[data-tooltip]:focus::before         -> opacity: 1
[data-tooltip-pos="bottom"]::after   -> flipped Y position
[data-tooltip-pos="bottom"]::before  -> flipped arrow
@media (prefers-reduced-motion: reduce) -> transition: none
```

### 2. Heatmap overlay grid

Inside `<div class="heatmap">`, after the existing SVG, emit a sibling `<div class="heatmap-overlay">` that mirrors the SVG cell grid as HTML divs. The SVG cells keep rendering exactly as today; the overlay cells are transparent and exist only to receive hover and anchor the tooltip.

Geometry must match the SVG (`src/eggpool/dashboard/render.py:622-628`):

- `cell_size = 13`, `cell_gap = 3`, `step = 16`
- `top_margin = 20`, `left_margin = 36`
- 13 columns × 7 rows

CSS positions the overlay absolutely:

```text
.heatmap-overlay:  position: absolute; top: 20px; left: 36px;
                    display: grid; grid-template-columns: repeat(13, 13px);
                    grid-template-rows: repeat(7, 13px); gap: 3px;
.heatmap-hitbox:   width: 13px; height: 13px; border-radius: 2px;
                    (transparent; only used for hover detection)
```

The `.heatmap` container needs `position: relative` so the overlay's absolute positioning is anchored. `overflow: visible` is the default and must remain so the bubble can render above the container.

Add `pointer-events: none` to the SVG `<rect>` cells via a CSS rule (`.heatmap rect { pointer-events: none; }`) so the native browser tooltip (from the still-present `<title>`) does not double up with the CSS bubble. The `<title>` element stays in the markup for the existing test and as a fallback.

### 3. Tooltip content

Each hitbox carries three attributes:

- `data-tooltip` — the visible text. Multi-line via `\n`.
- `aria-label` — identical text for screen readers.
- `<title>` (still inside the SVG rect, not the hitbox) — fallback.

The strings, built from the data row already in scope:

- Overview (tokens view, `value_field="total_tokens"`):

  ```text
  Wed, Mar 5 2026
  1,204 tokens · 23 requests
  ```

- Bandwidth (`value_field="bytes"`):

  ```text
  Wed, Mar 5 2026
  2.4 MB in · 1.8 MB out · 23 requests
  ```

Reuse existing formatters `format_bytes()` (`render.py:580`) and `format_tokens()` (`render.py:586`) for the metric values. Add a small helper `_format_tooltip_date(day_str: str) -> str` at module level in `render.py` to reformat the existing `YYYY-MM-DD` `day_str` into the human-friendly `Wed, Mar 5 2026` format. Use `datetime.strptime()` and `%a, %b %-d %Y`.

`request_count` is currently never read by the renderer (`render.py:557-692`); this pass introduces it into the per-row tooltip text.

### 4. Other tooltip sites (first pass)

The CSS rule is generalizable, so a handful of markup-only additions demonstrate it is a system rather than a one-off. Apply `data-tooltip` to:

- **Topbar actions** in `_render_layout()` around `src/eggpool/dashboard/render.py:175-185`:
  - Refresh button: `"Reload this page"`
  - Theme selector: `"Switch dashboard theme"` (already a `<select>`, no markup change needed beyond the attribute)
- **Sortable column headers** in `render_accounts()`, `render_models()`, `render_latency()`, and `render_bandwidth()` (where `<th>` already has a sort indicator). One short sentence per header explaining sort direction (e.g., `"Sort by name"`, `"Sort by request count"`).
- **Status badges** in the accounts and models tables. Add a small Python mapping near the badge renderers:

  ```text
  disabled            -> "Account disabled by operator"
  auth_error          -> "Upstream rejected the credentials"
  rate_limited        -> "Upstream returned 429 recently"
  quota_exhausted     -> "Upstream reported the quota is exhausted"
  cooldown_active     -> "Account is in cooldown after recent failures"
  ```

  Badge vocabulary is whatever the existing renderers already emit; the mapping is added alongside them.

These other sites require no CSS changes — the existing `[data-tooltip]` rule applies automatically.

## Files to change

| File | Change |
|---|---|
| `src/eggpool/dashboard/static/dashboard.css` | Append the `[data-tooltip]` system (~55 lines). Add `.heatmap` `position: relative`, `.heatmap-overlay`, `.heatmap-hitbox`, and `.heatmap rect { pointer-events: none; }` (~20 lines). |
| `src/eggpool/dashboard/render.py` | `_render_bandwidth_heatmap()` at `557-692`: emit the overlay grid alongside the unchanged SVG. Add `_format_tooltip_date()` helper at module level. Per-row tooltip string with all metrics. Add `pointer-events="none"` to each rect. Topbar, table headers, and status badges: add `data-tooltip="…"` attributes only. |
| `tests/unit/test_dashboard.py` | Existing `test_renders_tooltip` (line 888) continues to pass. Add `test_renders_data_tooltip`, `test_tooltip_text_includes_request_count`, `test_overlay_grid_geometry`, `test_topbar_actions_have_tooltips`, `test_status_badges_have_tooltips`, and a snapshot-style test that asserts the formatted date string for a known `day_str`. |

## Edge cases & mitigations

| Risk | Mitigation |
|---|---|
| Tooltip clipped by an `overflow: hidden` ancestor | Verify `.heatmap`, the card containing it, and `<main>` during implementation. If any clips, set `overflow: visible` or use `[data-tooltip-pos="bottom"]` for top-row cells. |
| Top-row bubble overflows above the page top | `pointer-events: none` keeps the bubble non-interactive. Acceptable for a 90-day heatmap where most cells have plenty of headroom. |
| Tooltip overlaps neighbour on narrow viewports | `pointer-events: none` keeps adjacent cells hoverable. |
| Auto-refresh `innerHTML` swap kills event listeners | Pure CSS has no listeners to kill. New `data-tooltip` attributes arrive fresh on every refresh. |
| Screen readers ignore `::after` content | `aria-label` is set on each hitbox with the identical text, so the accessible name matches. |
| `test_renders_tooltip` asserts `<title>` is present | We keep `<title>`; the test passes unchanged. |
| Theme switching (`/static/theme.css?theme=…`) | Tooltip uses CSS vars (`--card-bg`, `--card-border`, `--page-text`), so it re-themes automatically without re-render. |
| Heatmap resizes on mobile (currently does not) | Overlay grid uses the same `cell_size`, `cell_gap`, and `step` constants as the SVG. If the heatmap becomes responsive later, the overlay follows the same constants. |
| `data-tooltip` attribute contains characters that need HTML escaping (quotes, `<`, `>`) | Use `html.escape(..., quote=True)` when interpolating into `data-tooltip="…"` and `aria-label="…"`. |

## Implementation phases

### Phase 1 — CSS rule

File: `src/eggpool/dashboard/static/dashboard.css`

Append the `[data-tooltip]` block to the bottom of the file. No markup changes yet. Visually, nothing changes because no element has `data-tooltip` yet. This phase proves the rule parses and matches nothing.

### Phase 2 — Heatmap overlay

File: `src/eggpool/dashboard/render.py` (`_render_bandwidth_heatmap`, lines 557-692)

- Add `_format_tooltip_date()` helper.
- Extend the per-row loop to emit a `<div class="heatmap-hitbox" data-tooltip="…" aria-label="…">` alongside each existing `<rect>`.
- Emit the `<div class="heatmap-overlay">…</div>` after the closing `</svg>` but inside `<div class="heatmap">`.
- Add `pointer-events="none"` attribute to each rect.
- Preserve the existing `<title>` element.

Run the existing dashboard tests; they must pass unchanged.

### Phase 3 — Tests

File: `tests/unit/test_dashboard.py`

- `test_renders_data_tooltip` — asserts `data-tooltip` and `aria-label` attributes on heatmap hitboxes.
- `test_tooltip_text_includes_request_count` — feeds a known data row and asserts the rendered tooltip contains the formatted token count and the request count.
- `test_overlay_grid_geometry` — asserts the overlay contains exactly the same number of hitboxes as data rows.
- `test_tooltip_date_format` — asserts `_format_tooltip_date("2026-03-05") == "Wed, Mar 5 2026"`.
- `test_topbar_actions_have_tooltips` — asserts `data-tooltip` on the refresh button and theme selector.
- `test_status_badges_have_tooltips` — asserts `data-tooltip` on representative badge types.

### Phase 4 — Other tooltip sites

Files: `src/eggpool/dashboard/render.py` (topbar in `_render_layout`, sortable headers, status badges)

Mark up only. No CSS changes. No new tests beyond those in phase 3.

### Phase 5 — Verification

- Manual smoke test on `/`, `/accounts`, `/models`, `/latency`, `/bandwidth` with at least two contrasting themes (e.g., GitHub Light and Catppuccin Mocha). Confirm:
  - Heatmap cells show the new styled tooltip with date + metrics + request count.
  - Tooltip colors update when the theme is switched.
  - Tooltip survives the 15-60s auto-refresh on the overview page.
  - Tooltip appears on a sortable column header.
  - Tooltip appears on a status badge.
- Run the four pre-commit checks from `AGENTS.md`:
  - `uv run ruff format --check src/ tests/ scripts/`
  - `uv run ruff check src/ tests/ scripts/`
  - `uv run pyright src/ scripts/`
  - `uv run pytest`

## Definition of done

- Heatmap cells show a styled CSS tooltip on hover containing the human-formatted date, primary metric, and request count.
- The `[data-tooltip]` CSS rule works on every other dashboard element that opts in, with no per-site CSS additions.
- Tooltip is themed via existing CSS custom properties and updates with theme changes without a server re-render.
- No JavaScript added; no new dependencies; no Python imports beyond what `render.py` already has (`datetime` is already a stdlib import in the module per typical patterns; verify and add only if missing).
- `aria-label` is set on every tooltip target with the same text as the visible tooltip.
- `<title>` element remains on SVG `<rect>` cells so the existing test continues to pass.
- `tests/unit/test_dashboard.py` covers the new markup, content shape, and overlay geometry.
- All four `AGENTS.md` pre-commit checks pass.

## Invariants while implementing

- Never put the tooltip text in `<title>` only; the visible bubble must be driven by `data-tooltip` and the accessible name must be `aria-label` so screen readers get the same content as sighted users.
- Never let an overlay hitbox paint a visible color; its sole purpose is hover detection. Cell color stays in the SVG `<rect>`.
- Never rely on inline `style` for the tooltip — all rules belong in `dashboard.css`.
- Never change the SQL row shape or add a new query — every field needed (`day`, `bytes_received`, `bytes_emitted`, `total_tokens`, `request_count`) is already in `daily_data`.
- Never use JavaScript for positioning or rendering — pure CSS only.
- HTML-escape every interpolated value inside `data-tooltip="…"` and `aria-label="…"`.