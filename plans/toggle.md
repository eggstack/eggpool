# Account Breakdown Toggle Plan

## Context

Yesterday's merges `ce3acdc` ("fix: collapse account breakdown on index by
default, bump to v0.3.6") and `2683b4a` ("fix: show/hide disabled accounts
in overview account breakdown") landed the right *server-side* fix: the
Account breakdown panel on the overview page is no longer a collapsible
`<details>` block, and `show_disabled=False` is the default — disabled
accounts are filtered out at the SQL layer (`WHERE a.enabled = 1`).
Operators who want the historical view opt in with `?show_disabled=1`.

Two real problems remain, plus one UX gap that the operator mentioned
in the original ticket. This plan addresses all three in one pass.

1. **The overview toggle currently does not auto-submit.**
   `dashboard.js#initTimeseriesControls`
   (`src/eggpool/dashboard/static/dashboard.js:782-823`) wires
   `select[data-auto-submit]` only inside forms that also contain a
   `select[name="period"]`. The overview's `account-breakdown-filter`
   form (`src/eggpool/dashboard/render.py:1674-1683`) has
   `data-period-selector` but only `<input type="hidden" name="period">`,
   so the wiring short-circuits at `if (!select) continue;`. Result:
   flipping the dropdown reloads nothing; the operator must press Enter
   inside the form. `/accounts` works because its filter form pairs a
   period select with the show-disabled select.

2. **The toggle is a `<select>` dropdown, not a "toggle switch."** The
   operator phrased the request as a "small filter toggle." Two-option
   selects read like a dropdown, not a toggle. A button toggle reads
   better on the overview's panel header.

3. **There is no count indicator.** With 20 disabled accounts the chip
   should read "Show 20 disabled" so the operator learns the size of
   the filtered set without expanding the table.

## Goal

Replace the `<select>`-based filter in the overview's Account breakdown
panel with a button-style toggle that reads **"Show N disabled"** by
default, place a **"X enabled · Y disabled"** chip on the heading, and
fix the JS auto-submit wiring so the toggle (and any future
`data-period-selector` form without a period select) submits on change.

## Scope

In scope:

- `src/eggpool/dashboard/render.py` — convert
  `_render_account_breakdown_filter` from a form + select to an
  anchor-based button toggle; thread `disabled_count` into its label.
- `src/eggpool/dashboard/render.py` — add a heading chip to the panel
  header via a new `enabled_count` kw-arg on `render_overview`.
- `src/eggpool/dashboard/routes.py` — compute and pass `enabled_count`
  in `handle_overview`.
- `src/eggpool/dashboard/static/dashboard.css` — chip + toggle button
  styling, plus per-theme color tokens.
- `src/eggpool/dashboard/theme.py` — register the new CSS variables
  across all four themes (light, dark, midnight, solarized).
- `src/eggpool/dashboard/static/dashboard.js` — refactor
  `initTimeseriesControls` so `select[data-auto-submit]` wires
  independently of `select[name="period"]`.
- `tests/unit/test_dashboard.py` — update existing toggle tests;
  add count-badge, chip, and XSS-safety coverage.
- `tests/integration/test_dashboard_routes.py` — update the two
  overview-page tests to assert the new markup.

Out of scope:

- The `/accounts` page keeps its dropdown filter
  (`_render_account_filters`). That is a different ergonomics
  conversation (it pairs a period selector with the show-disabled
  toggle); revisit only if operators ask.
- A new theme. Reuse existing tokens wherever possible.
- Database schema changes. The disabled-count query is already cheap.

## Files to modify

```
src/eggpool/dashboard/render.py                # _render_account_breakdown_filter, render_overview
src/eggpool/dashboard/routes.py                # handle_overview threading
src/eggpool/dashboard/static/dashboard.css     # .panel-header-chip + .show-disabled-toggle styles
src/eggpool/dashboard/static/dashboard.js      # initTimeseriesControls refactor
src/eggpool/dashboard/theme.py                 # register new CSS tokens per theme
tests/unit/test_dashboard.py                   # TestRenderOverview
tests/integration/test_dashboard_routes.py     # test_overview_page_hides_disabled_by_default + show_disabled_query
```

## Files to create

None. No new modules; only existing files.

## Detailed design

### 1. `_render_account_breakdown_filter` — select → anchor toggle

Signature change:

```python
def _render_account_breakdown_filter(
    period: str,
    current_theme: str,
    show_disabled: bool,
    disabled_count: int,
) -> str:
```

Label logic:

```python
n = int(disabled_count or 0)
if show_disabled:
    label = "Hide disabled"
    next_value = "0"
elif n > 0:
    label = f"Show {n} disabled"
    next_value = "1"
else:
    label = "Show disabled"
    next_value = "1"
```

Render an anchor (no JS required, no form, no auto-submit):

```python
href = _build_href_with_state(
    period=period, theme=current_theme, show_disabled=next_value
)
return (
    f'<a class="show-disabled-toggle" '
    f'href="{escape_attr(href)}" '
    f'aria-pressed="{str(show_disabled).lower()}">'
    f'<span class="disabled-toggle-icon" aria-hidden="true">▾</span>'
    f"{escape(label)}"
    f"</a>"
)
```

`_build_href_with_state` is a small helper that emits
`?show_disabled=1&period=…&theme=…` (or an empty query when both args
are absent). Keeping the helper centralized avoids drift across the
overview and (later) any other panel that wants the same treatment.

Drop `data-period-selector` / `data-auto-submit` from this form — the
anchor navigates on its own. The Accounts page can keep its dropdown
markup; the JS refactor in §3 makes it work even with this removed.

### 2. `render_overview` — heading chip

Add a new keyword-only parameter:

```python
enabled_count: int = 0,
```

The panel header block changes from:

```python
<section class="panel">
  <div class="panel-header">
    <h3>Account breakdown</h3>
    {_render_account_breakdown_filter(period, current_theme, show_disabled)}
  </div>
  {_render_account_breakdown_body(accounts, show_disabled, disabled_count)}
</section>
```

to:

```python
def _account_count_chip(enabled_count: int, disabled_count: int) -> str:
    if disabled_count <= 0:
        return f'<span class="panel-header-chip">{enabled_count} enabled</span>'
    return (
        f'<span class="panel-header-chip">'
        f'{enabled_count} enabled · {disabled_count} disabled'
        f'</span>'
    )
...

<section class="panel">
  <div class="panel-header">
    <h3>Account breakdown {_account_count_chip(enabled_count, disabled_count)}</h3>
    {_render_account_breakdown_filter(period, current_theme, show_disabled, disabled_count)}
  </div>
  {_render_account_breakdown_body(accounts, show_disabled, disabled_count)}
</section>
```

The chip's enabled count is **whatever rows the current `accounts`
list contains that have `account_enabled == 1`** so it stays accurate
whether or not the operator has toggled `show_disabled`.

### 3. `handle_overview` — pass the enabled count

After the existing `accounts` query (`src/eggpool/dashboard/routes.py:230`):

```python
enabled_count = sum(1 for a in accounts if a.get("account_enabled"))
```

Then:

```python
html = render_overview(
    overview=overview,
    accounts=accounts,
    period=time_range.label,
    refresh_interval_s=refresh_s,
    bandwidth_daily=bandwidth_daily,
    ping_summary=ping_summary,
    models=models if models is not None else [],
    events=events,
    theme_css=theme_css,
    heatmap_colors=heatmap_colors,
    available_themes=available,
    current_theme=current_theme,
    ip_stats=ip_stats,
    timeseries=timeseries or [],
    pending_health=pending_health,
    attempt_stats=attempt_stats,
    operational_summary=operational_summary,
    update_info=_get_update_info(request),
    show_disabled=show_disabled,
    disabled_count=disabled_count,
    enabled_count=enabled_count,
)
```

Note: `disabled_count` is still only fetched when `not show_disabled`
(line 210), so the chip and toggle behave correctly but we don't pay
for the count on the "showing all" view.

### 4. `src/eggpool/dashboard/static/dashboard.css`

Append to the existing `.panel-header` block (current line 361):

```css
.panel-header-chip {
  display: inline-block;
  margin-left: 0.5rem;
  padding: 0.15rem 0.6rem;
  font-size: 0.78rem;
  font-weight: 500;
  color: var(--text-muted);
  background: var(--chip-bg);
  border: 1px solid var(--chip-border);
  border-radius: 999px;
  vertical-align: middle;
  white-space: nowrap;
}

.show-disabled-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0.75rem;
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--link-color);
  background: var(--button-bg);
  border: 1px solid var(--button-border);
  border-radius: 999px;
  text-decoration: none;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease,
    border-color 0.15s ease;
}

.show-disabled-toggle:hover {
  background: var(--button-bg-hover);
  color: var(--link-color-hover);
}

.show-disabled-toggle:focus-visible {
  outline: 2px solid var(--accent-color);
  outline-offset: 2px;
}

.show-disabled-toggle[aria-pressed="true"] {
  color: var(--text-color);
  background: var(--button-bg-active);
  border-color: var(--accent-color);
}

.disabled-toggle-icon {
  display: inline-block;
  transition: transform 0.15s ease;
}

.show-disabled-toggle[aria-pressed="true"] .disabled-toggle-icon {
  transform: rotate(180deg);
}
```

The chip and toggle already inherit `gap` from the
`.panel-header` `flex-wrap` layout — verify below 480px that they wrap
cleanly to their own row without horizontal overflow.

### 5. `src/eggpool/dashboard/theme.py`

Register six new tokens (`--chip-bg`, `--chip-border`, `--button-bg`,
`--button-border`, `--button-bg-hover`, `--button-bg-active`) in each
of the four themes. Reuse existing tokens where possible
(`--accent-color`, `--text-muted`, `--link-color`).

Light theme baseline (token-by-token):

| Token | Value |
|---|---|
| `--chip-bg` | `#eef2f7` |
| `--chip-border` | `#d8dee6` |
| `--button-bg` | `#ffffff` |
| `--button-border` | `#cbd5e1` |
| `--button-bg-hover` | `#f1f5f9` |
| `--button-bg-active` | `#e0f2fe` |

Dark / midnight / solarized themes: pick the matching inverted shade
so the chip and toggle feel native to each palette. Implementation
will mirror the existing token registration per-theme.

### 6. `src/eggpool/dashboard/static/dashboard.js`

In `initTimeseriesControls` (current line 782-823), refactor:

```js
const periodForms = document.querySelectorAll(
  "form[data-period-selector]"
);
const timeseriesForm = document.querySelector(
  "form[data-timeseries-controls]"
);
for (let p = 0; p < periodForms.length; p++) {
  const periodForm = periodForms[p];
  if (periodForm.__eggpoolPeriodWired) continue;
  periodForm.__eggpoolPeriodWired = true;
  const periodSelect = periodForm.querySelector('select[name="period"]');
  if (periodSelect) {
    periodSelect.addEventListener("change", function () {
      if (
        timeseriesForm
        && typeof namespace.refreshGroupedTimeseriesChart === "function"
      ) {
        syncTimeseriesPeriod(timeseriesForm);
        namespace.refreshGroupedTimeseriesChart(timeseriesForm);
      } else {
        periodForm.submit();
      }
    });
  }
  // Auto-submit any other `data-auto-submit` selects inside the same
  // form (e.g. the show-disabled toggle on /accounts). Wire
  // independently of whether periodSelect exists so filter-only forms
  // also get the auto-submit treatment.
  const autoSubmits = periodForm.querySelectorAll(
    "select[data-auto-submit]"
  );
  for (let s = 0; s < autoSubmits.length; s++) {
    const autoSelect = autoSubmits[s];
    if (autoSelect === periodSelect) continue;
    autoSelect.addEventListener("change", function () {
      if (
        timeseriesForm
        && typeof namespace.refreshGroupedTimeseriesChart === "function"
      ) {
        namespace.refreshGroupedTimeseriesChart(timeseriesForm);
      } else {
        periodForm.submit();
      }
    });
  }
}
```

Behavior change: `select[data-auto-submit]` now wires for any
`form[data-period-selector]`, regardless of whether
`select[name="period"]` exists. `select[name="period"]` no longer
short-circuits the wiring.

After this refactor the overview page does not need
`data-auto-submit` at all — the new anchor toggle submits via
navigation — but the wiring stays for the `/accounts` page and any
future page that mixes period + filter selects.

### 7. Tests — `tests/unit/test_dashboard.py`

Update existing tests in `TestRenderOverview`:

- `test_account_breakdown_renders_show_disabled_filter` — assert
  presence of `.show-disabled-toggle` anchor with `href` containing
  `show_disabled=1`, default `aria-pressed="false"`, label
  `Show 3 disabled` when `disabled_count=3`.
- `test_account_breakdown_filter_reflects_state` — assert
  `aria-pressed="true"` and `href` containing `show_disabled=0` when
  `show_disabled=True`.
- `test_account_breakdown_filter_preserves_period_and_theme` — assert
  the constructed URL still contains `period=7d&theme=midnight`.

New tests in `TestRenderOverview`:

- `test_account_breakdown_chip_with_counts` — pass
  `enabled_count=12, disabled_count=3`, assert the chip text contains
  `12 enabled` and `3 disabled`.
- `test_account_breakdown_chip_no_disabled_zero` — pass
  `enabled_count=12, disabled_count=0`, assert the chip shows
  `12 enabled` and does not mention `disabled`.
- `test_account_breakdown_toggle_label_default_with_count` —
  `show_disabled=False, disabled_count=3` → label is `Show 3 disabled`.
- `test_account_breakdown_toggle_label_default_no_count` —
  `show_disabled=False, disabled_count=0` → label is `Show disabled`.
- `test_account_breakdown_toggle_label_active` — `show_disabled=True`
  → label is `Hide disabled`, `aria-pressed="true"`.
- `test_account_breakdown_toggle_xss_safe` — pass
  `disabled_count="<script>alert(1)</script>"` and assert the
  rendered HTML escapes the `<` / `>` (no `<script>` tag in the body).

### 8. Tests — `tests/integration/test_dashboard_routes.py`

- `test_overview_page_hides_disabled_by_default` (current line 340) —
  rewrite to assert `class="show-disabled-toggle"` present, default
  `aria-pressed="false"`, and the count appears in the label. Drop the
  `selected="selected"` assertions.
- `test_overview_page_show_disabled_query` (current line 360) —
  rewrite to assert `?show_disabled=1` flips the rendered toggle to
  `aria-pressed="true"` and the chip count is unchanged.

### 9. Manual smoke checklist

Before merging, run the dashboard locally with at least 12 enabled +
3 disabled accounts in the DB and confirm:

- [ ] `/` panel header shows heading + chip `12 enabled · 3 disabled`
      and a button reading `Show 3 disabled`. Table has 12 rows.
- [ ] Click `Show 3 disabled`: URL becomes
      `?show_disabled=1&period=24h`. Button reads `Hide disabled` with
      `aria-pressed="true"`. Table has 15 rows; disabled rows show
      `Enabled: no` in greyed text.
- [ ] Click `Hide disabled`: URL drops `show_disabled`, reverts to the
      12-row view.
- [ ] Theme switch (`?theme=midnight`): chip + toggle pick up the
      theme's dark tokens with adequate contrast.
- [ ] Mobile (≤480px viewport): chip wraps cleanly below the heading
      without overflow.
- [ ] Period flip while `?show_disabled=1` is active: `?period=7d`
      preserves `show_disabled=1`.

## Risk / caveats

- **Cache key:** `get_account_stats` already differentiates
  `cache_flag = "all" if include_disabled else "enabled"` so no
  dashboard-cache change is needed.
- **JS refactor blast radius:** the wiring change touches every
  `data-period-selector` form. `/accounts`, `/runtime`, and any other
  page with such forms must continue to work; covered indirectly by
  existing tests plus the new `test_account_breakdown_*` assertions.
  If a regression slips through, revert the JS change alone and the
  rest of the UI improvements stand on their own.
- **Anchor navigation vs. form submit:** the new toggle is a plain
  `<a href>`. There is no submit-button fallback if JS is disabled —
  the link itself navigates, which is the desired no-JS behavior. If
  you prefer a fallback submit button for keyboard-only operators
  navigating without a mouse, hold a follow-up.
- **Theme token roll-out:** four themes must add six new tokens each.
  Misnamed tokens silently fall back to defaults, so verify visual
  output in light, dark, midnight, and solarized before merge.

## Verification

After implementation, run the full pre-commit gate from `AGENTS.md`:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

All four must pass with zero errors. Plus the manual smoke checklist
above against a real dashboard, exercising the no-JS anchor navigation
and the dark themes.
