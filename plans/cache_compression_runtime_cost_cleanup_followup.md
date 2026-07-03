# Cache Compression Runtime and Cost Cleanup Follow-up Plan

Date: 2026-07-03
Repository: `eggstack/eggpool`
Status: handoff plan
Priority: high; cost finalizer fix is critical

Related plans:

- `plans/cache_compression_config_runtime_cleanup.md`
- `plans/cache_compression_phase_12_polish_pass.md`
- `plans/2026-07-03-reservation-fallback-floor-removal-followup.md`

## Summary

The last pass improved the operator-facing config and overview card substantially, but two lines of work remain:

1. **Critical accounting correctness:** the request finalizer still contains the old estimated-cost reservation floor, which can re-inflate canonical request cost after the new bounded-cost selector chooses a lower plausible local estimate.
2. **UI/config polish:** the generated config example is now schema-valid and simpler, but still leaks phase-shaped prose. The Runtime page now has a Request Shaping summary, but it still renders the full stack of detailed phase-era cards inline.

This follow-up plan should be executed in this order:

1. Fix the finalizer reservation-floor bug.
2. Add finalizer-level regression tests around the MiniMax inflation fixture.
3. Finish config text cleanup.
4. Collapse Runtime request-shaping detail into an operator-simple panel with advanced details hidden below it.
5. Verify overview/request-shaping metrics remain compact and correct.

## Current state

### Good

- `config.example.toml` and `src/eggpool/_share/config.example.toml` now validate against the schema.
- Known bad tuning keys were removed from examples.
- Tests now assert both config examples validate and reject the known invalid keys.
- The overview page now includes a compact Request Shaping card.
- Runtime aggregation now has a `_build_request_shaping_summary()` helper.
- The dashboard has enough aggregate data to present a coherent request-shaping view.

### Still wrong

- `src/eggpool/request/finalizer.py` still floors canonical estimated cost back to `reservation_microdollars` when the chosen cost is lower than the reservation.
- The config example still contains phase-shaped prose such as `Phase 12` and `Phase 5`.
- The Runtime page still renders detailed phase-era panels inline after the new summary:
  - cache counter observability;
  - request segmentation;
  - compression opportunities;
  - compression runtime;
  - policy overrides;
  - native cache preservation;
  - synthetic cache controls;
  - tuning;
  - routing guardrails.
- Runtime code comments still contain phase labels, even where UI headings were improved.

## Part 1: Fix the critical finalizer reservation-floor bug

### Problem

The cost-precedence ladder now correctly calls `choose_bounded_estimated_cost()` for local estimated costs and reservation estimates. That helper can choose the lower plausible local estimate when reservation is inflated.

Immediately after that, the finalizer still applies this old floor:

```python
if exactness == "estimated" and cost_microdollars < reservation_microdollars:
    cost_microdollars = reservation_microdollars
```

This defeats the new bounded selector and preserves the MiniMax cost inflation class.

### Required invariant

Reservation estimates are routing budgets, not bills.

Canonical request cost must follow this precedence:

1. provider-reported cost wins;
2. trusted local `exact`, `derived`, or `partial` wins;
3. local `estimated` goes through `choose_bounded_estimated_cost()`;
4. reservation-only cases go through `choose_bounded_estimated_cost()`;
5. no billable work remains zero/unknown.

Nothing after `choose_bounded_estimated_cost()` may unconditionally raise canonical cost back to the reservation.

### Implementation

Target: `src/eggpool/request/finalizer.py`.

Preferred fix:

- Delete the estimated-cost floor block entirely.
- Remove or rewrite comments claiming dashboard totals must reflect at least reservation.
- Keep `reservation_microdollars` as a separate audit/routing field.
- Keep `clamp_request_cost_microdollars()` after canonical selection as a last-ditch absolute safety cap.

Do not add provider-specific branches for MiniMax.

### Regression fixture

Use the live MiniMax-style fixture described in the prior follow-up plan:

- provider id: `minimax`
- model id: `MiniMax-M3`
- input tokens: `353`
- output tokens: `1386`
- cache read/write tokens: `0`
- local cost: `21848` microdollars
- local exactness: `estimated`
- reservation: `5411079` microdollars
- expected canonical cost: `21848` microdollars
- expected exactness: `estimated`

### Tests

Add finalizer-level tests, not only helper tests:

1. `test_estimated_local_cost_beats_higher_reservation_floor_regression`
   - The MiniMax fixture above.
   - Expected canonical cost is `21848`, not `5411079`.

2. `test_higher_reservation_does_not_floor_after_lower_local_selection`
   - Local estimate and reservation are both plausible, local lower.
   - Expected canonical cost remains local.

3. `test_reservation_lower_than_local_estimated_can_win_when_plausible`
   - Reservation lower than local, both plausible.
   - Expected canonical cost can be reservation.

4. `test_generic_estimate_not_floored_to_implausible_reservation`
   - Both local/reservation implausible.
   - Expected canonical cost is the helper’s generic bounded estimate, not raw reservation.

5. `test_provider_reported_cost_still_wins_over_all_estimates`
   - Provider cost set.
   - Expected exactness `provider_reported`.

6. `test_trusted_local_exactness_still_ignores_reservation`
   - `exact`, `derived`, `partial` local costs remain canonical.

### Acceptance criteria

- The estimated-cost floor block is gone or cannot affect lower plausible local estimated choices.
- The MiniMax fixture persists canonical `21848` microdollars, not `5411079`.
- Provider-reported precedence remains unchanged.
- Trusted local exact/derived/partial precedence remains unchanged.
- Historical repair tooling still detects/fixes old inflated rows.
- Dashboard reservation fallback warning remains useful for old rows but does not trigger for correctly finalized new rows.

## Part 2: Finish generated config example cleanup

### Problem

The config examples are now valid and shorter, but the request-shaping section still opens with phase-oriented prose. This is confusing for normal operators and implies the config is an implementation journal rather than a stable product surface.

### Target files

- `config.example.toml`
- `src/eggpool/_share/config.example.toml`
- `docs/cache-compression.md`
- `docs/cache-compression-profiles.md`
- `docs/cache-compression-troubleshooting.md`

### Required cleanup

Remove operator-facing references to:

- `Phase 5`
- `Phase 6`
- `Phase 7`
- `Phase 9`
- `Phase 10`
- `Phase 12`

from generated examples. Use product-oriented headings:

- `Request shaping`
- `Cache-preserving compression`
- `Synthetic cache controls`
- `Advanced scoped overrides`
- `Advisory tuning`

### Desired example shape

The main example should show only these normal request-shaping knobs:

```toml
# ----------------------------------------------------------------------
# Request shaping: cache-preserving compression
# ----------------------------------------------------------------------
# Disabled by default. "observe" records opportunities without changing
# requests. "safe" compresses only volatile suffix content and fails
# closed if a stable prefix would change. Routing never uses these metrics.
# [compression]
# enabled = false
# mode = "observe"
# min_candidate_tokens = 2048
# min_savings_tokens = 1024
# max_compression_latency_ms = 25.0
#
# [compression.transforms]
# fold_repeated_lines = true
# compact_logs = true
# compact_search_results = true
# elide_base64_blobs = true
# minify_machine_json = true
# compact_stack_traces = true
#
# [cache.synthetic_cache_controls]
# enabled = false
# dry_run = true
# min_stable_tokens = 1024
```

Keep advanced snippets below that, but make them explicitly optional and schema-valid.

### Test additions

Extend `tests/unit/test_config.py`:

- assert both examples validate;
- assert known-bad tuning keys are absent;
- assert generated example request-shaping section does not contain `Phase 5`, `Phase 6`, `Phase 7`, `Phase 9`, `Phase 10`, or `Phase 12`.

Do not assert that all docs are phase-free; plan files legitimately contain phase terms.

## Part 3: Collapse the Runtime page into a single Request Shaping section

### Problem

The Runtime page now includes a Request Shaping summary, but still renders all old detail cards inline. The result is better than before, but still too complex.

### Desired Runtime layout

Keep process/system diagnostics as they are. Replace the request-shaping part with:

1. **Request Shaping summary panel**
   - Compression mode: Off / Observe / Safe / Mixed.
   - Synthetic cache mode: Off / Dry-run / Apply / Mixed.
   - Advisory tuning: Off / Recommend.
   - Routing: Reporting-only.

2. **Four compact cards in the summary panel**
   - Compression: analyzed, compressed, saved/potential tokens, fallback count.
   - Cache reporting: cache counter reported rate, cache read/write tokens.
   - Synthetic cache: dry-run/applied/candidate/warning counts.
   - Guardrails: stable-prefix preserved rate, routing non-interference.

3. **Advanced details block**
   - Use `<details><summary>Advanced request-shaping details</summary>...</details>` or the repo’s existing equivalent pattern.
   - Move the existing detailed tables/cards inside this block:
     - segmentation totals;
     - compression opportunity table;
     - compression runtime transform/warning tables;
     - policy override rollup;
     - native cache preservation card;
     - synthetic cache status/warning/policy tables;
     - tuning recommendation table;
     - routing guardrails details.

### Files to update

Likely target:

- `src/eggpool/dashboard/render.py`
- `src/eggpool/dashboard/routes.py`
- `tests/unit/test_dashboard.py`
- `tests/unit/test_dashboard_phase7.py`
- `tests/unit/test_api_phase7.py`

Do not remove existing stats endpoints.

### UI text rules

Operator-facing Runtime text should not contain implementation phase labels.

Avoid:

- `Phase 1`
- `Phase 2`
- `Phase 4`
- `Phase 5`
- `Phase 6`
- `Phase 7`
- `Phase 9`
- `Phase 10`
- `canonical request segmentation`
- `closed-loop threshold tuning`

Prefer:

- `Cache reporting`
- `Request segmentation`
- `Compression opportunities`
- `Safe compression`
- `Policy overrides`
- `Native cache preservation`
- `Synthetic cache controls`
- `Advisory tuning`
- `Routing guardrails`

Code comments may reference historical phase plans if useful, but renderer-visible strings should not.

### Acceptance criteria

- Runtime page has one visible Request Shaping panel.
- Detailed request-shaping tables are collapsed behind an advanced details block.
- Runtime page is readable without scrolling through all phase-era cards by default.
- Existing detailed data remains available for diagnostics.
- Operator-facing Runtime HTML does not render phase labels.
- Existing endpoint tests still pass.

## Part 4: Preserve and tighten overview Request Shaping card

### Current state

The overview Request Shaping card is useful. It displays:

- configured compression mode;
- actual or potential token savings;
- cache reported rate;
- synthetic-cache mode.

### Follow-up tasks

- Keep the card compact.
- Ensure it handles no-data windows cleanly.
- Ensure `cache reported` uses reported rows divided by known rows, not total requests where cache counters were never parsed.
- Ensure safe mode shows actual savings and observe mode shows potential savings.
- Ensure disabled mode does not misleadingly show zero savings as a success metric.

### Tests

Update dashboard tests to assert:

- Overview renders `Request shaping`.
- Disabled mode subtext says compression disabled or equivalent.
- Observe mode shows potential savings.
- Safe mode shows saved tokens.
- Cache reported value renders as percent or em dash with no rows.
- Synthetic mode renders Off/Dry Run/Apply without raw internal enum leakage.

## Part 5: Cost repair/dashboard warning verification

The cost hardening pass added reservation fallback detection and repair tooling. After removing the finalizer floor, verify the dashboard warning remains correct for historical rows but not new rows.

### Tasks

- Inspect `src/eggpool/stats/queries.py` reservation fallback detection.
- Inspect dashboard warning render path in `src/eggpool/dashboard/render.py`.
- Add or update tests so:
  - old inflated rows are counted;
  - repair apply reduces the count/excess;
  - newly finalized MiniMax fixture rows are not counted as fallback because canonical cost is already local;
  - provider-reported rows are never flagged as suspicious reservation fallback.

### Acceptance criteria

- Dashboard warning remains visible for unrepaired historical inflated rows.
- Dashboard warning disappears after repair or for clean new data.
- Dashboard total cost no longer rebounds to reservation totals after new requests.

## Part 6: Docs alignment

Update docs to match the final behavior.

### Required language

Use this invariant consistently:

> Reservation estimates are routing budgets and audit fields. They do not floor canonical request cost. Provider-reported cost wins; trusted local exact/derived/partial costs win; local estimated and reservation estimates are bounded by `choose_bounded_estimated_cost()`.

Update at least:

- `README.md`
- `architecture/README.md`
- `AGENTS.md`
- `.opencode/skills/architecture/SKILL.md`
- `.opencode/skills/development/SKILL.md`
- `docs/cache-compression.md`
- `docs/cache-compression-profiles.md`
- `docs/cache-compression-troubleshooting.md`

Also remove or demote phase-shaped request-shaping language in operator docs.

## Part 7: Validation plan

Run focused tests first:

```bash
uv run pytest tests/unit/test_config.py -q
uv run pytest tests/unit/test_dashboard.py -q
uv run pytest tests/unit/test_dashboard_phase7.py -q
uv run pytest tests/unit/test_api_phase7.py -q
uv run pytest tests/unit/test_cost_inflation_guards.py -q
uv run pytest tests/unit/test_pricing.py -q
uv run pytest tests/unit/test_quota.py -q
```

If finalizer tests live elsewhere, run them explicitly:

```bash
uv run pytest tests/unit/test_request_finalizer.py -q
uv run pytest tests/unit/test_cost_repair.py -q
```

Then run full checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

## Final acceptance criteria

- Finalizer no longer floors canonical estimated cost to reservation.
- MiniMax regression fixture persists canonical local estimated cost, not reservation.
- Provider-reported and trusted local cost precedence remain intact.
- Config examples validate and contain no known-bad tuning keys.
- Generated examples no longer present request shaping as a phase timeline.
- Runtime page shows a single Request Shaping summary by default.
- Detailed request-shaping diagnostics are still available under an advanced details block.
- Overview Request Shaping card remains compact and no-data safe.
- Dashboard reservation fallback warning still detects old inflated rows but not clean new rows.
- Stats endpoints remain backward-compatible.
- Full tests, ruff, and pyright pass.

## Non-goals

- Do not remove schema fields in this pass; keep backward compatibility.
- Do not add cache-aware routing.
- Do not implement tuning apply mode.
- Do not change default request mutation behavior.
- Do not hide historical cost-repair diagnostics.
- Do not add provider-specific MiniMax hacks.

## Rollback guidance

If UI consolidation causes regressions, revert only the Runtime rendering changes and keep the finalizer/config-example fixes. The finalizer floor removal and config example schema fix should not be rolled back, because they address correctness defects.