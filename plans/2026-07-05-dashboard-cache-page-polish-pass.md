# Dashboard Cache Page Polish Pass

## Context

Commit `d440166b76a8b5e6df9917d9adc4ad1d03f864ca` implemented the planned dashboard split by moving cache/compression/request-shaping diagnostics out of `/runtime` and into a dedicated `/cache` page. The broad architecture is now correct: Runtime is much closer to process/live operational diagnostics, while Cache owns the detailed provider-reported cache counters, request segmentation, compression, synthetic cache, advisory tuning, and routing guardrail panels.

This polish pass is intended to verify the migration, tighten small UX/API seams, and remove any remaining ambiguity introduced by the split. It should not redesign cache accounting, routing, scoring, compression, synthetic cache controls, migrations, or database schema.

## Goals

1. Verify `/cache` is fully route-safe, nav-safe, auth-safe, theme-safe, and period-safe.
2. Confirm `/runtime` no longer pays the heavy query cost for detailed cache/compression diagnostics.
3. Confirm the new extracted render helpers did not regress escaping, warning flags, empty states, or label semantics.
4. Clean up remaining docs/test naming that still implies detailed cache diagnostics live on Runtime.
5. Improve the operator experience around the Runtime → Cache deep-link and the Cache page local section index.

## Non-goals

Do not add JavaScript state persistence for the old `<details>` block. The details block should remain gone.

Do not alter routing behavior. `QuotaFairScorer` must remain independent of cache/compression/synthetic/tuning fields.

Do not change cache-counter extraction semantics or request finalization. This pass is dashboard/polish only.

Do not collapse the dedicated Cache page back into Runtime.

## Phase 1: Route, nav, and export verification

Inspect `src/eggpool/dashboard/routes.py` and `src/eggpool/dashboard/render.py` for consistency.

Required checks:

- `handle_cache` is registered in `register_dashboard_routes` at `/cache`.
- `handle_cache` is listed in `routes.py::__all__`.
- `render_cache` is imported where needed and listed in any relevant `__all__` export surface.
- `_render_nav` includes a Cache item with `active_nav="cache"` support.
- Cache nav link preserves `period` and `theme` using the same query conventions as other dashboard pages.
- Runtime nav link still marks Runtime active and does not accidentally mark Cache active.
- `/cache` and `/runtime` are both covered by dashboard-auth behavior when auth is enabled.

Suggested tests:

```bash
uv run pytest tests/unit/test_dashboard_cache_page.py tests/unit/test_dashboard_runtime.py -v
```

If `test_dashboard_runtime.py` does not exist, add focused tests under the existing dashboard test module rather than creating a broad fixture-heavy integration test.

Acceptance criteria:

- `/cache` is registered exactly once.
- Active nav class appears on Cache only when rendering Cache.
- `theme=` and `period=` survive navigation to Cache.
- `handle_cache` and `render_cache` imports do not rely on accidental unused imports.

## Phase 2: Runtime slimming verification

Review `handle_runtime` and `render_runtime` after the migration.

Required checks:

- `handle_runtime` must not call:
  - `get_cache_observability`
  - `get_canonical_request_segmentation`
  - `get_compression_observability`
  - `get_compression_runtime`
  - `get_compression_policy_stats`
  - `get_cache_stability`
  - `get_synthetic_cache_summary`
  - `get_compression_tuning_window_metrics`
  - `_build_request_shaping_summary`, unless intentionally keeping a cheap summary and clearly documented.
- Runtime should gather only live runtime snapshot and any intentionally retained runtime-adjacent stats.
- If `get_transcoding_stats(period)` remains on Runtime, confirm it is cheap enough for auto-refresh and that the visual payload still belongs there. Otherwise, move detailed transcoding direction/loss breakdowns to `/cache` and leave a compact Runtime link.
- `render_runtime` should no longer accept detailed cache/compression kwargs if it no longer renders those panels.
- Runtime body should not include `advanced-request-shaping`, `Advanced request-shaping details`, or a `<details>` wrapper for cache content.

Recommended regression tests:

- Monkeypatch/stub a fake stats service and assert `/runtime` does not call cache/compression stats methods.
- Render Runtime and assert the detailed Cache section headings do not appear:
  - `Cache reporting (`
  - `Request segmentation (`
  - `Compression opportunities (`
  - `Compression runtime (`
  - `Compression policies (`
  - `Synthetic cache controls (`
  - `Advisory tuning (`
- Render Runtime and assert the Cache link panel exists.

Acceptance criteria:

- Runtime refresh path is materially cheaper than before the migration.
- The old non-persistent disclosure markup is absent.
- Operators still have an obvious one-click path from Runtime to Cache.

## Phase 3: Cache page period semantics

The Cache page is period-aware; Runtime may be live-state oriented. Tighten semantics so labels do not mislead operators.

Required checks:

- `/cache?period=1h`, `/cache?period=24h`, `/cache?period=7d`, and `/cache?period=30d` render the selected period in the page selector and section headings.
- Invalid/empty `period` resolves consistently to `24h` or the project-standard default.
- `handle_cache` should use a single normalized period variable, e.g. `resolved_period = period or "24h"`, and pass that same value to all stats calls and the renderer.
- Runtime should not display misleading historical-window language around live-only cards. If Runtime passes `period="runtime"` to `_render_layout`, ensure the footer reads acceptably. If it passes `period or "24h"`, ensure the retained `get_transcoding_stats(period)` heading has a legitimate time window.

Potential fix:

- Keep `/cache` period strictly historical (`1h`, `24h`, `7d`, `30d`).
- Keep `/runtime` footer as `runtime` if the page is mostly live-state. For any retained historical card such as Transcoding, label the panel heading with its own `period` rather than relying on the global footer.

Acceptance criteria:

- `/cache` period labels are coherent and bookmarkable.
- `/runtime` does not imply all live-state cards are scoped to `24h`.

## Phase 4: Cache page local index and anchor polish

The Cache page is long. The new local anchor index should be verified and made keyboard/screen-reader friendly without adding JavaScript.

Required checks:

- Each index link points to a unique `id` present in the rendered document.
- IDs are stable and descriptive:
  - `request-shaping-summary`
  - `cache-reporting`
  - `cache-stability`
  - `synthetic-cache-controls`
  - `request-segmentation`
  - `compression-opportunities`
  - `compression-runtime`
  - `compression-policies`
  - `advisory-tuning`
  - `routing-guardrails`
- Index labels match section headings exactly or nearly exactly.
- Anchor links preserve the current page and do not introduce duplicate query strings.
- Local index should be a normal `<nav>` or panel-like block with an accessible label.

Suggested test:

- Parse rendered Cache HTML and assert every `href="#..."` in the local index resolves to an element id.

Acceptance criteria:

- Operators can quickly jump to a long section.
- No broken local anchors.
- No hidden JS-only behavior.

## Phase 5: Render helper regression review

The helper extraction is the riskiest part because it can accidentally alter dynamic escaping or warning conditions.

Required checks:

- Every dynamic provider/account/model/policy string is still passed through `escape` or `escape_attr` before entering HTML.
- Dynamic values placed inside attributes use `escape_attr`, not only `escape`.
- Empty-state behavior is equivalent or better than before:
  - No data should produce an empty-state message, not a blank panel.
  - Zero counts should render as `0`, not `—`, where zero is a meaningful measurement.
  - Unknown/unavailable metrics should render as `—`, not `0`, where absence is semantically distinct.
- Warning flags are preserved:
  - failed compression fallbacks should mark the relevant card warning.
  - synthetic cache warnings should mark the warnings card warning.
  - routing guardrails should not warn unless a safety invariant is actually violated.
- Helper names should clearly separate provider-reported cache counters from synthetic cache controls.

Suggested adversarial render test data:

- provider id: `<provider&x>`
- account name: `acct"onclick="x`
- model id: `<script>alert(1)</script>`
- policy name: `global & policy`

Render Cache with those values and assert raw angle brackets/quotes do not escape into executable markup.

Acceptance criteria:

- No escaping regressions.
- No helper-level mutation of input dictionaries.
- Tests cover at least one adversarial identifier in Cache render output.

## Phase 6: Runtime → Cache link behavior

The Runtime page should not leave the operator guessing where the detailed panels went.

Required checks:

- Runtime has a compact panel or line that says detailed cache/request-shaping diagnostics are on Cache.
- The link includes the active theme. Example:
  - `/cache?period=24h&theme=cyber-red`
- If Runtime has a meaningful period selector, the link should include that period. If Runtime is live-state only, default the link to `24h` and say so in subtext.
- The link text should be unambiguous: `Open Cache diagnostics` or `View Cache details`.
- Do not render the full Cache summary on Runtime unless it is cheap and clearly useful.

Acceptance criteria:

- Runtime remains short.
- Cache is discoverable from Runtime and from top nav.
- Theme continuity is preserved.

## Phase 7: JSON endpoint backward-compatibility smoke tests

The migration should not affect API consumers.

Required checks:

Confirm the following endpoints still exist and return stable JSON shapes:

- `/api/stats/cache-observability`
- `/api/stats/canonical-request-segmentation`
- `/api/stats/compression-observability`
- `/api/stats/compression-runtime`
- `/api/stats/compression-policies`
- `/api/stats/cache-stability`
- `/api/stats/synthetic-cache-observability`
- `/api/stats/compression-tuning`
- `/api/stats/request-shaping`

For each endpoint, test:

- default period works;
- `?period=1h` works;
- empty database returns a stable zero/empty shape;
- auth behavior is unchanged from before migration.

Acceptance criteria:

- No endpoint removed, renamed, or shape-broken by the dashboard split.

## Phase 8: Documentation cleanup

Search docs and comments for stale phrasing that implies the detailed cache/compression panels live on Runtime.

Search terms:

```bash
rg -n "Runtime.*cache|runtime.*cache|Advanced request-shaping|request-shaping runtime|cache/compression runtime|/runtime.*compression|/runtime.*cache" README.md AGENTS.md docs architecture src tests plans
```

Expected cleanup:

- README should say `/cache` owns detailed request-shaping diagnostics.
- `AGENTS.md` should direct implementers to `/cache` for dashboard cache/compression visibility.
- Architecture docs should reserve `/runtime` for live operational health.
- Tests should be named around Cache if they assert detailed cache/compression panels.
- Historical plan files can remain historical, but any active handoff doc should point to `/cache`.

Acceptance criteria:

- No current docs instruct operators to open Runtime for detailed cache/compression panels.
- Historical plans are not rewritten unless they are misleading as active docs.

## Phase 9: Verification commands

Run the focused dashboard/cache set first:

```bash
uv run pytest tests/unit/test_dashboard_cache_page.py tests/unit/test_api_phase7.py tests/unit/test_compression_stats_phase7.py -v
```

Then run route/render smoke tests if present:

```bash
uv run pytest tests/unit/test_dashboard_routes.py tests/unit/test_dashboard_render.py -v
```

Then run static checks:

```bash
uv run ruff check src tests
uv run pyright
```

Finally, run the full test suite if feasible:

```bash
uv run pytest
```

If full suite cannot be run, document exactly which subsets were run and why.

## Acceptance criteria for this polish pass

- `/cache` is route/nav/export complete.
- `/cache` renders all detailed cache/request-shaping/compression panels with valid anchors.
- `/runtime` no longer renders the old advanced details block and no longer gathers detailed cache/compression aggregates.
- Runtime has a clear Cache deep-link preserving theme and using a coherent period.
- Cache render helpers preserve escaping, warning behavior, and empty states.
- JSON endpoints remain backward compatible.
- Docs point detailed cache/compression diagnostics to `/cache`.
- Focused tests and static checks pass or any remaining failures are documented with exact failing commands.

## Suggested implementation order

1. Inspect route/nav/export plumbing.
2. Add or tighten tests for `/cache` route rendering and Runtime absence of old details block.
3. Fix period/theme propagation and Runtime deep-link if needed.
4. Add anchor integrity test for the Cache local index.
5. Add adversarial escaping render test for Cache panels.
6. Search and clean stale docs/comments.
7. Run focused tests and static checks.
8. Record verification results in the final commit message or a short note in the plan if a follow-up is needed.

## Follow-up candidates after polish

If the page remains too long after this pass, consider a later second-level split:

- `/cache` for provider-reported cache counters, cache stability, synthetic cache controls.
- `/compression` for compression opportunities, runtime outcomes, policy rollups, and advisory tuning.

Do not do that split in this polish pass. The current dedicated Cache page is the correct first consolidation point and should be stabilized before further decomposition.
