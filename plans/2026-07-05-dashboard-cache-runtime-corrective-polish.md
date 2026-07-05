# Dashboard Cache / Runtime Corrective Polish Plan

## Context

The dashboard split is now largely complete:

- `/cache` owns detailed cache reporting, request segmentation, compression, synthetic cache, advisory tuning, routing guardrails, and optional transcoding diagnostics.
- `/runtime` no longer renders the old advanced cache/compression details block.
- The stale `advanced-request-shaping` CSS was removed.
- Cache anchor integrity and basic adversarial escaping tests were added.

The remaining issue is smaller but worth correcting before considering this line closed: `render_runtime` still renders `_render_request_shaping_summary_panel({}, period=period, guardrails_mode=guardrails_mode)`. That makes Runtime look like it has a request-shaping summary, but the data is intentionally empty. This is confusing for operators and creates a false maintenance contract for future contributors.

This pass should replace the empty summary component on Runtime with a deliberately lightweight navigation/relocation panel, ensure the Cache link preserves period/theme coherently, and add tests that pin the intended behavior.

## Goals

1. Remove the empty request-shaping summary rendering from `/runtime`.
2. Replace it with a purpose-built Runtime → Cache diagnostics panel.
3. Preserve `period` and `theme` in the Runtime Cache deep-link.
4. Keep detailed request-shaping summary rendering exclusively on `/cache`.
5. Add regression tests so Runtime does not reacquire fake/empty cache summary UI.
6. Run focused dashboard tests plus static checks.

## Non-goals

Do not change cache extraction, compression, synthetic cache, advisory tuning, route scoring, or database schema.

Do not move all transcoding out of Runtime in this pass unless it is trivial and clearly improves page semantics. Transcoding can remain on both Runtime and Cache for now if the current behavior is useful.

Do not add JavaScript state persistence or collapsible details to Runtime.

Do not remove the `/api/stats/request-shaping` endpoint or any per-surface JSON endpoint.

## Current suspected issue

In `src/eggpool/dashboard/render.py`, `render_runtime` currently builds:

```python
request_shaping_panel = _render_request_shaping_summary_panel(
    {}, period=period, guardrails_mode=guardrails_mode
)
```

and later renders it inside:

```html
<section class="panel" id="request-shaping-summary">
  <h3>Request shaping</h3>
  {request_shaping_panel}
  <p class="sub">... <a href="/cache?period={period}">Cache & request shaping</a> ...</p>
</section>
```

This is not ideal. The helper is meant to summarize actual cache/request-shaping state. Passing `{}` turns it into a pseudo-summary with default/off/unknown values. That may be technically harmless, but it is semantically misleading.

## Desired final behavior

Runtime should render a compact relocation panel, not the detailed summary helper.

Recommended wording:

```html
<section class="panel" id="cache-diagnostics-link">
  <h3>Cache & request shaping</h3>
  <p>
    Detailed cache reporting, request segmentation, cache-preservation,
    compression, synthetic-cache, advisory tuning, and routing guardrail
    diagnostics live on the Cache page.
  </p>
  <p><a class="button" href="/cache?period=24h&theme=..."><span>Open Cache diagnostics</span></a></p>
</section>
```

Use existing dashboard CSS/button/link classes if they already exist. Do not introduce a large new styling surface.

If no reusable button class exists, keep it as a normal link inside a panel.

## Phase 1: Add a small URL helper for dashboard links

Avoid hand-built query strings in `render_runtime`.

Add or reuse a small private helper in `src/eggpool/dashboard/render.py`:

```python
def _dashboard_query_href(path: str, *, period: str | None = None, theme: str | None = None) -> str:
    ...
```

Requirements:

- Return `path` unchanged when no query params are present.
- Include `period` when provided and meaningful.
- Include `theme` when non-empty.
- URL-encode values via `urllib.parse.urlencode`.
- Escape the final href with `escape_attr` at render call sites if the helper returns a raw string.
- Keep helper private unless an existing public helper already solves this.

Example outputs:

- `_dashboard_query_href("/cache", period="24h", theme="cyber-red")` → `/cache?period=24h&theme=cyber-red`
- `_dashboard_query_href("/cache", period="7d", theme="")` → `/cache?period=7d`
- `_dashboard_query_href("/cache", period=None, theme=None)` → `/cache`

Acceptance criteria:

- Runtime Cache link is not string-concatenated.
- Query params are encoded.
- Existing period selector behavior is not changed unless using the same helper is clearly safe.

## Phase 2: Replace Runtime empty summary with a dedicated panel

In `render_runtime`:

1. Delete the `request_shaping_panel = _render_request_shaping_summary_panel({}, ...)` call.
2. Delete any `id="request-shaping-summary"` Runtime wrapper if it implies a summary exists.
3. Add a dedicated panel renderer, either inline or as a small helper:

```python
def _render_runtime_cache_diagnostics_link_panel(*, period: str, current_theme: str) -> str:
    ...
```

Recommended content:

- Heading: `Cache & request shaping`
- Body copy: concise relocation explanation.
- Link text: `Open Cache diagnostics`
- Link href: `/cache?period={period}&theme={current_theme}` when theme is present.

The panel should explicitly say the Cache page contains:

- cache reporting;
- request segmentation;
- native cache preservation/cache stability;
- compression opportunities/runtime/policies;
- synthetic cache controls;
- advisory tuning;
- routing guardrails.

Do not render default status cards such as `Compression off`, `Synthetic cache off`, or `Guardrails reporting_only` on Runtime unless backed by actual data.

Acceptance criteria:

- Runtime no longer calls `_render_request_shaping_summary_panel`.
- Runtime no longer contains an empty/default request-shaping metric card set.
- Runtime still gives operators a clear path to Cache.

## Phase 3: Theme and period preservation

The current Runtime Cache link includes `period` but not `theme`. Tighten this.

Required behavior:

- `render_runtime(..., period="7d", current_theme="cyber-red")` includes a Cache link containing both `period=7d` and `theme=cyber-red`.
- `render_runtime(..., period="24h", current_theme="")` includes `/cache?period=24h` and no dangling `&theme=`.
- `current_theme` must be escaped/encoded in the URL.
- The user-visible link text should not include raw period/theme values.

Potential implementation detail:

- `render_runtime` already receives `current_theme`; pass it to the helper.
- Keep `_render_layout(period="runtime")` as-is so the Runtime footer remains live-state oriented.
- Use the `period` argument only for the Cache deep-link and retained transcoding panel, not for the Runtime layout footer.

Acceptance criteria:

- Theme continuity works when navigating Runtime → Cache.
- Runtime remains semantically live-state.

## Phase 4: Tests for Runtime corrective behavior

Update `tests/unit/test_dashboard_runtime.py`.

Add/adjust tests:

1. Runtime does not render detailed Cache page headings:
   - already mostly covered; keep.
2. Runtime does not render the old advanced details block:
   - already covered; keep.
3. Runtime does not render fake/default request-shaping summary cards.
   - Assert absence of text that only comes from `_render_request_shaping_summary_panel` defaults if stable.
   - Prefer a structural assertion if the helper output has a distinct class/heading.
4. Runtime renders the dedicated relocation panel:
   - heading: `Cache & request shaping`
   - link text: `Open Cache diagnostics`
   - href includes `/cache?period=24h` by default.
5. Runtime Cache link preserves custom period:
   - `render_runtime(snapshot, period="7d")` includes `/cache?period=7d`.
6. Runtime Cache link preserves theme:
   - `render_runtime(snapshot, period="7d", current_theme="cyber-red")` includes `period=7d` and `theme=cyber-red`.
7. URL escaping:
   - `render_runtime(snapshot, current_theme='x" onclick="bad')` must not include raw attribute-breaking text.

Acceptance criteria:

- Tests fail before the corrective change and pass after it.
- Tests do not depend on incidental whitespace ordering.
- Tests avoid brittle full-HTML snapshots.

## Phase 5: Cache page summary remains intact

Ensure the Cache page still uses actual `request_shaping_summary` when supplied and still falls back from detailed stats when not supplied.

Required checks:

- `render_cache(request_shaping_summary={...})` uses the supplied summary.
- `render_cache(request_shaping_summary=None, cache_stability=..., compression_observability=..., compression_runtime=..., synthetic_cache_summary=..., routing_runtime=...)` still generates a useful fallback summary.
- The Cache page local index still points to the summary section.
- No Runtime-specific helper names leak into Cache code.

Suggested tests:

- Existing Cache page tests probably cover most of this. Add only one focused assertion if missing:
  - Cache page contains `Summary` link and a summary panel when `request_shaping_summary` is supplied.

Acceptance criteria:

- Runtime becomes simpler without weakening Cache.

## Phase 6: Docs/comments cleanup

Search for language that now becomes stale after replacing Runtime’s pseudo-summary.

Commands:

```bash
rg -n "Request shaping.*Runtime|Runtime.*request shaping|request-shaping summary.*Runtime|request_shaping_summary.*render_runtime|cache-diagnostics-link|Cache & request shaping" src tests README.md AGENTS.md architecture docs plans
```

Update current docs only. Historical plan files can remain unless they are active handoff docs and now misleading.

Recommended doc wording:

- Runtime: live operational health plus a link to Cache diagnostics.
- Cache: detailed cache/request-shaping summary and drill-downs.

Acceptance criteria:

- Current documentation no longer says Runtime renders a request-shaping summary.
- Code comments around `render_runtime` do not imply detailed request-shaping data is available there.

## Phase 7: Verification commands

Run focused tests first:

```bash
uv run pytest tests/unit/test_dashboard_runtime.py tests/unit/test_dashboard_cache_page.py -v
```

Run API/cache compatibility tests:

```bash
uv run pytest tests/unit/test_api_phase7.py tests/unit/test_compression_stats_phase7.py -v
```

Run static checks:

```bash
uv run ruff check src tests
uv run pyright
```

If feasible, run the full suite:

```bash
uv run pytest
```

Document any failures exactly, including command and failure summary.

## Acceptance criteria for this corrective polish pass

- `/runtime` does not call or render `_render_request_shaping_summary_panel`.
- `/runtime` has a dedicated, unambiguous Cache diagnostics link panel.
- Runtime → Cache link preserves period and theme with proper URL encoding/HTML escaping.
- `/cache` remains the only dashboard page rendering detailed request-shaping summary and drill-down panels.
- Existing Cache anchor and escaping tests continue to pass.
- Runtime tests pin absence of old details block, absence of detailed Cache headings, and presence of the dedicated relocation panel.
- Focused tests, `ruff`, and `pyright` pass or failures are documented for follow-up.

## Suggested commit message

```text
Correct Runtime cache diagnostics link panel

- Replace Runtime's empty request-shaping summary render with a dedicated Cache diagnostics link panel
- Preserve period/theme in Runtime -> Cache link using encoded query params
- Add Runtime tests for the relocation panel, theme propagation, and absence of fake summary UI
- Keep detailed request-shaping summary rendering exclusive to /cache
```

## Future follow-up, not in this pass

If Runtime remains too broad, consider moving detailed transcoding breakdowns fully to `/cache` or a separate `/transcoding` page and leaving Runtime with only a compact total/health indicator. Do not include that change in this corrective polish unless the existing code makes it nearly free and test coverage is straightforward.
