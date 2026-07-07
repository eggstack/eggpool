# Dashboard cache-token card semantics fix plan

## Context

The dashboard index currently renders a `Total tokens` card next to a `Cache tokens` card. The implementation is internally consistent, but the labels invite the wrong comparison.

Current behavior:

- `Total tokens` is sourced from `summary["total_tokens"]` and means fresh provider tokens only: `input_tokens + output_tokens`.
- `Cache tokens` is sourced from `summary["total_cache_read_tokens"]` and means provider-reported cache-read input tokens.
- `Cache tokens` subtext uses the already-correct bounded denominator: `cache_read / (input + cache_read + cache_write)`, displayed as `of prompt`, with cache writes shown separately.
- The stats summary payload already exposes `fresh_tokens` and `accounted_tokens`, where `accounted_tokens = input + output + cache_read + cache_write`.

Observed operator problem:

Cache reads can legitimately exceed fresh tokens on cache-heavy workloads. That makes the dashboard look wrong because the visible `Cache tokens` metric can be greater than the visible `Total tokens` metric. It is not necessarily a persistence or arithmetic bug; it is a UI semantics bug. The index card labels conflate fresh token volume with all provider-accounted token activity.

The previous bounded-ratio bug appears already fixed. Do not revert the bounded ratio back to `cache_read / input`; retain `cache_read / (input + cache_read + cache_write)` everywhere.

## Goals

1. Make the overview cards impossible to misread when cache reads exceed fresh input/output tokens.
2. Preserve existing stats API compatibility for `total_tokens` as `input + output` unless an explicit API versioning decision is made later.
3. Surface both fresh-token volume and total accounted provider token activity on the index page.
4. Keep the cache-read percentage bounded and semantically tied to prompt/cache accounting, not output tokens.
5. Add regression coverage for cache-heavy cases where `cache_read_tokens > total_tokens`.

## Non-goals

- Do not change database schema.
- Do not rewrite historical request rows.
- Do not change routing, quota scoring, model pricing, or reservation logic.
- Do not reinterpret provider-reported `usage.total_tokens` as the canonical dashboard `total_tokens` field in this pass.
- Do not remove `fresh_tokens` or `accounted_tokens` from summary payloads.

## Target UX

Prefer this card set on the overview index:

- `Accounted tokens`
  - Metric: `accounted_tokens = input + output + cache_read + cache_write`.
  - Subtext: `fresh <fresh_tokens> · cache read <cache_read> · cache write <cache_write>`.

- `Fresh tokens`
  - Metric: `fresh_tokens = input + output`.
  - Subtext: `in <input_tokens> · out <output_tokens>`.

- `Cache reads`
  - Metric: `total_cache_read_tokens`.
  - Subtext: `<bounded_cache_read_pct> of prompt · write <cache_write>`.

This is slightly more verbose, but it makes the hierarchy explicit: cache reads are one component of accounted token activity, while fresh tokens remain the input/output subtotal operators are already used to seeing.

If the overview card grid becomes too crowded, acceptable fallback UX:

- Replace the current `Total tokens` card with `Accounted tokens`.
- Rename the current `Cache tokens` card to `Cache reads`.
- Put fresh tokens in the `Accounted tokens` subtext.

Avoid leaving the headline pair as `Total tokens` plus `Cache tokens`; that is the exact source of the operator confusion.

## Implementation plan

### 1. Normalize summary rendering variables in `src/eggpool/dashboard/render.py`

In `render_overview`, compute these values explicitly near the existing token formatting block:

```python
total_input_tokens = int(summary.get("total_input_tokens", 0))
total_output_tokens = int(summary.get("total_output_tokens", 0))
total_cache_read_tokens = int(summary.get("total_cache_read_tokens", 0))
total_cache_write_tokens = int(summary.get("total_cache_write_tokens", 0))

fresh_tokens = int(
    summary.get(
        "fresh_tokens",
        total_input_tokens + total_output_tokens,
    )
)
accounted_tokens = int(
    summary.get(
        "accounted_tokens",
        fresh_tokens + total_cache_read_tokens + total_cache_write_tokens,
    )
)
```

Then format:

```python
in_tok = format_tokens(total_input_tokens)
out_tok = format_tokens(total_output_tokens)
fresh_tok = format_tokens(fresh_tokens)
accounted_tok = format_tokens(accounted_tokens)
cache_read = format_tokens(total_cache_read_tokens)
cache_write = format_tokens(total_cache_write_tokens)
```

Keep `total_tok` only if other code still expects the variable. If it remains, assign it to `fresh_tok` and add a short inline comment that `summary["total_tokens"]` is legacy fresh-token volume.

### 2. Keep the bounded cache percentage formula

The renderer should continue using:

```python
cache_read_ratio = total_cache_read_tokens / (
    total_input_tokens + total_cache_read_tokens + total_cache_write_tokens
)
```

when the denominator is positive, otherwise fall back to `summary.get("cache_read_ratio")`.

Do not use output tokens in this denominator; the percentage is about prompt/cache input accounting, not completion output. Do not use `cache_read / total_tokens`; that would be bounded only accidentally and would carry the wrong semantics.

### 3. Update overview cards

Modify the second cards section in `render_overview`.

Recommended rendering order:

1. `Accounted tokens`
2. request shaping card, when present
3. `Fresh tokens`
4. `Cache reads`
5. `Reasoning tokens`
6. `Throughput`
7. `Streaming`
8. `Exactness`

Concrete card copy:

```python
_render_metric_card(
    title="Accounted tokens",
    metric=accounted_tok,
    sub=f"fresh {fresh_tok} · cache read {cache_read} · cache write {cache_write}",
)
```

```python
_render_metric_card(
    title="Fresh tokens",
    metric=fresh_tok,
    sub=f"in {in_tok} · out {out_tok}",
)
```

```python
_render_metric_card(
    title="Cache reads",
    metric=cache_read,
    sub=f"{cache_read_pct} of prompt · write {cache_write}",
)
```

If the renderer has tests that assert `Total tokens`, update them to assert `Fresh tokens` and `Accounted tokens` with the new semantics.

### 4. Update tooltip copy

In `_CARD_TOOLTIPS` in `src/eggpool/dashboard/render.py`:

- Replace `Total tokens` tooltip with `Fresh tokens` or leave `Total tokens` only if some other page still renders that title.
- Add tooltip for `Accounted tokens`.
- Replace `Cache tokens` tooltip with `Cache reads`.

Suggested tooltip text:

```python
"Accounted tokens": (
    "Input, output, cache-read, and cache-write tokens recorded for the selected period. "
    "This is the broad provider-accounting total and can exceed fresh input/output volume."
),
"Fresh tokens": (
    "Input plus output tokens recorded for the selected period, excluding cache-read and cache-write counters."
),
"Cache reads": (
    "Provider-reported prompt-cache read tokens. The subtext shows the bounded read share "
    "cache_read / (input + cache_read + cache_write) and cache writes."
),
```

If `_render_metric_card` relies on exact title lookup, make sure no stale `Cache tokens` or `Total tokens` card title remains unintentionally without tooltip coverage.

### 5. Update stats payload docs/comments, not schema

The stats payload already has the right fields. Add or adjust doc comments where helpful:

- `_build_summary` in `src/eggpool/stats/queries.py` should make it clear that `total_tokens` is legacy/fresh token volume and `accounted_tokens` is the broad accounting total.
- `StatsService.get_summary_from_rollups` should mirror the same wording.

Do not change the `total_tokens` API value in this pass. External consumers may already depend on `total_tokens = input + output`. The dashboard can use `accounted_tokens` without an API break.

### 6. Add regression tests

Add tests in `tests/unit/test_dashboard.py` or the current dashboard renderer test module.

Test: cache-heavy overview does not imply impossible totals.

Fixture summary:

```python
{
    "total_requests": 2,
    "successful_requests": 2,
    "error_requests": 0,
    "error_rate": 0.0,
    "total_input_tokens": 100,
    "total_output_tokens": 50,
    "total_tokens": 150,
    "fresh_tokens": 150,
    "accounted_tokens": 1150,
    "total_cache_read_tokens": 900,
    "total_cache_write_tokens": 100,
    "total_reasoning_tokens": 0,
    "total_cost_microdollars": 0,
    "avg_latency_ms": 50.0,
}
```

Expected assertions:

- `Accounted tokens` appears before or near cache/fresh token cards.
- Accounted metric renders `1,150`.
- Fresh metric renders `150`.
- Cache reads metric renders `900`.
- Subtext renders `78.3% of prompt` because `900 / (100 + 900 + 100) = 0.81818` if using 900/1100, actually `81.8%`; verify exact fixture math before asserting. If using `input=100`, `read=900`, `write=100`, expected is `81.8%`.
- The old title `Cache tokens` should not appear on the overview card if renamed to `Cache reads`.

Test: summary fallback computes accounted tokens when field is absent.

Pass a summary that omits `fresh_tokens` and `accounted_tokens` but includes input/output/cache read/cache write. Assert the renderer still shows accounted total as `input + output + read + write`. This protects older callers/tests that construct minimal summary dicts.

Test: zero denominator still renders an em dash.

Input/cache read/cache write all zero should render `— of prompt` or the existing formatter's em dash behavior. Do not regress to `0.0%` unless the existing formatter already does that intentionally.

### 7. Add stats-level guard tests if not already present

If there is an existing `test_phase5_phase6.py`, `test_dashboard_rollups.py`, or stats query test covering `bounded_cache_ratio`, extend it with a cache-heavy case:

- `input=100`, `cache_read=900`, `cache_write=100` returns `900 / 1100`, not `9.0` and not `900 / 150`.
- `fetch_summary` returns:
  - `total_tokens = input + output`
  - `fresh_tokens = input + output`
  - `accounted_tokens = input + output + cache_read + cache_write`

This pins the intended semantic split so future code does not “fix” the dashboard by breaking API compatibility.

### 8. Optional diagnostics endpoint improvement

Optional, but useful: in any JSON stats endpoint that returns the overview summary, ensure both `fresh_tokens` and `accounted_tokens` are present for rollup and non-rollup paths. They already appear in the current query-builder paths; this step is a verification item, not expected schema work.

## Manual validation checklist

Run the focused test set first:

```bash
pytest tests/unit/test_dashboard.py -q
pytest tests/unit/test_dashboard_rollups.py -q
pytest tests/unit/test_phase5_phase6.py -q
```

Then run the broader suite used by the repo in prior passes:

```bash
pytest -q
```

If the repo has type/lint targets configured, run the documented local equivalents as well. Do not introduce new dependencies.

Manual dashboard validation:

1. Start eggpool with an existing database that has cache-heavy rows.
2. Open the overview dashboard.
3. Confirm the headline token card no longer reads as if `cache_read > total` is impossible.
4. Confirm `Accounted tokens >= Fresh tokens` and `Accounted tokens >= Cache reads` whenever cache reads are nonzero.
5. Confirm the cache percentage is bounded below or equal to 100%.
6. Confirm the Cache page, Runtime page, and API JSON still preserve the existing fields and do not lose cache observability.

## Acceptance criteria

- The overview dashboard no longer displays a misleading `Total tokens` vs `Cache tokens` pairing.
- Cache-heavy fixture with `cache_read_tokens > fresh_tokens` has explicit, correct display semantics.
- `accounted_tokens` is visible on the index page.
- `fresh_tokens` remains visible on the index page.
- Cache read percentage remains bounded by the existing prompt denominator.
- Existing stats API consumers are not broken by changing `summary["total_tokens"]` semantics.
- Regression tests cover both explicit `accounted_tokens` and renderer fallback when the field is absent.
- No database migration is required.

## Risk notes

The main risk is accidental semantic churn around `total_tokens`. Keep `total_tokens` as the legacy fresh-token value for now. Dashboard copy should solve the operator-facing problem without reinterpreting the API field.

The second risk is accidentally double-counting cache reads for providers whose reported `input_tokens` already includes cached tokens. Current EggPool code treats cache counters as separate accounting dimensions and already exposes `fresh_tokens` versus `accounted_tokens`; this plan preserves that convention rather than attempting provider-specific billing normalization. A later provider-normalization pass can refine billed-token semantics if needed, but the dashboard should first stop implying that cache reads must be bounded by fresh input/output volume.
