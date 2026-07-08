# Provider cache hit metric polish and correctness closure

Date: 2026-07-08
Status: handoff plan
Parent plan: `plans/2026-07-08-provider-cache-hit-metric-and-index-card-tightening.md`
Follow-up target: commit `606ee5555f25e4600608314d8c86060dfdd22f2e` (`Add provider cache hit rate metric and tighten overview layout`)

## Context

The implementation pass landed most of the requested dashboard/frontend work and introduced a canonical cache metric layer. The overview layout work is in good shape: `Thinking/Reasoning` was refactored into `_render_thinking_stats_card()` and is now rendered in the same card row as `Bandwidth received`, `Bandwidth emitted`, and `Avg TTFT (streamed)`.

The remaining work is a correctness/polish pass around the canonical cache metric semantics. The primary issue is not broad architecture; it is the exact provider-specific denominator and clamping behavior.

Current important files:

- `src/eggpool/stats/cache_metrics.py`
- `src/eggpool/stats/queries.py`
- `src/eggpool/proxy/normalized_usage.py`
- `src/eggpool/dashboard/render.py`
- Tests touching normalized usage, stats query cache observability, and dashboard overview rendering.

## Problems to fix

### 1. Anthropic cache reads/writes are incorrectly clamped against fresh input

`derive_cache_metric_terms()` currently computes:

```python
read_clamped = has_input and read > inp
write_clamped = has_input and write > inp
if read_clamped:
    read = inp
if write_clamped:
    write = inp
if shape == ProtocolShape.OPENAI:
    eligible = inp
else:
    eligible = inp + read + write if has_input else read + write
```

This clamp happens before protocol-specific denominator handling. That is wrong for Anthropic. Anthropic `input_tokens` is fresh input; `cache_read_input_tokens` and `cache_creation_input_tokens` are expected to exceed fresh input on cache-heavy requests. A normal row like:

```text
input_tokens=100
cache_read_input_tokens=700
cache_creation_input_tokens=200
upstream_protocol='anthropic'
```

should yield:

```text
cache_read_tokens_canonical = 700
cache_write_tokens_canonical = 200
cache_eligible_input_tokens = 1000
provider_cache_hit_rate = 70%
cache_write_rate = 20%
```

Current logic clamps read/write to `100`, yielding `100 / 300 = 33.3%`, which invalidates the metric.

### 2. Inconsistency accounting ignores clamp flags

`CacheMetricTerms` stores `cache_read_clamped` and `cache_write_clamped`, but `aggregate_cache_terms()` only increments `inconsistent_cache_counter_rows` when `terms.cache_read_tokens > terms.cache_eligible_input_tokens`. Because clamping has already occurred, many impossible provider rows will never be counted as inconsistent.

This should be explicit:

```python
if terms.cache_read_clamped or terms.cache_write_clamped:
    inconsistent += 1
```

For OpenAI-compatible rows, a read/write counter exceeding `prompt_tokens` is suspicious and should be clamped or otherwise bounded so the displayed rate cannot exceed 100%. For Anthropic rows, read/write exceeding fresh input is normal and should not be flagged.

### 3. `cache_hit_ratio_known_only` still uses the old formula

`fetch_cache_observability()` still computes:

```python
total_cached / total_input_reported
```

and returns that as `cache_hit_ratio_known_only`. This is the old semantics and remains wrong for Anthropic rows. The field may need to stay for compatibility, but it should become a deprecated alias of `provider_cache_hit_rate`.

The docstring also still says `cache_hit_ratio_known_only` is `cached_input_tokens / input_tokens`, which is now stale and should be corrected.

### 4. OpenAI cache field detection/comment drift

`normalized_usage.py` comments say nested `prompt_tokens_details.cache_write_tokens` is an OpenRouter/OpenAI-compatible warmup counter, but `_OPENAI_CACHE_FIELDS` currently does not include `cache_write_tokens` because `_has_any_field()` only looks at the top-level usage object. The implementation later still reports write-only rows because extracted cache tokens are non-`None`, but the comment/constant mismatch is confusing and invites future regressions.

Fix either by:

- removing the misleading claim that `_OPENAI_CACHE_FIELDS` covers nested write fields, and documenting that nested detection is handled by `_extract_openai_cache_tokens()` plus the `any(v is not None ...)` check; or
- adding a helper such as `_has_openai_cache_field(usage)` that checks both top-level aliases and nested `prompt_tokens_details.cached_tokens` / `cache_write_tokens`.

The second option is cleaner.

### 5. Overview fallback card still uses legacy title when cache observability is absent

`render_overview()` still renders the old `Cache reads` card when `cache_observability is None`. In normal routing this may not matter because `handle_overview()` now passes `cache_observability`, but tests or future callers can still hit the old path.

Prefer making the fallback card title `Provider cache hit rate` too, with fallback subtext clearly indicating legacy summary math is being used. This avoids reintroducing the old mental model through alternate call paths.

## Required implementation changes

### A. Make cache derivation protocol-specific before clamping

File: `src/eggpool/stats/cache_metrics.py`

Rewrite `derive_cache_metric_terms()` so protocol shape is selected first.

Suggested behavior:

```python
shape = _map_protocol(protocol)
inp = max(0, input_tokens or 0)
raw_read = max(0, cache_read_tokens or 0)
raw_write = max(0, cache_write_tokens or 0)

if shape == ProtocolShape.OPENAI:
    eligible = inp
    read_clamped = eligible > 0 and raw_read > eligible
    write_clamped = eligible > 0 and raw_write > eligible
    read = min(raw_read, eligible) if eligible > 0 else raw_read
    write = min(raw_write, eligible) if eligible > 0 else raw_write
elif shape == ProtocolShape.ANTHROPIC:
    read = raw_read
    write = raw_write
    eligible = inp + read + write
    read_clamped = False
    write_clamped = False
else:
    read = raw_read
    write = raw_write
    eligible = inp + read + write if (inp > 0 or read > 0 or write > 0) else 0
    read_clamped = False
    write_clamped = False
```

Notes:

- Do not clamp Anthropic read/write to fresh `input_tokens`.
- Keep OpenAI rate bounded by using `prompt_tokens` as denominator and clamping read/write to eligible only when eligible is positive.
- Unknown protocol should be conservative but should not invent provider-specific clamp behavior. Use `input + read + write` denominator if granular terms exist.
- Consider whether OpenAI `read + write` together can exceed `eligible`. Current separate `min(raw_read, eligible)` and `min(raw_write, eligible)` can still make `read + write > eligible`, making read+write rates collectively exceed 100%. This may be acceptable if each rate is independently bounded, but a stricter implementation can scale or clamp write after read. Document the chosen behavior.

### B. Count clamp events as inconsistent rows

File: `src/eggpool/stats/cache_metrics.py`

Update aggregation:

```python
if terms.cache_read_clamped or terms.cache_write_clamped:
    inconsistent += 1
```

Keep any additional sanity check if desired, but do not rely only on post-clamp comparisons.

### C. Alias `cache_hit_ratio_known_only` to canonical hit rate

File: `src/eggpool/stats/queries.py`

After `agg = aggregate_cache_terms(...)`, return:

```python
"cache_hit_ratio_known_only": agg.provider_cache_hit_rate,
```

Remove or rename the old `cache_hit_ratio_known_only = total_cached / total_input_reported` local. If keeping the local for backwards diagnostics, expose it under a clearly deprecated/raw name such as:

```text
legacy_cache_hit_ratio_known_only_raw
```

But avoid adding new public fields unless needed. The cleanest pass is to keep only the compatibility alias and update the docstring.

Update the `fetch_cache_observability()` docstring:

```text
cache_hit_ratio_known_only: deprecated compatibility alias for provider_cache_hit_rate.
provider_cache_hit_rate: cache_read_tokens_canonical / cache_eligible_input_tokens, restricted to reported rows.
```

### D. Tighten OpenAI cache field detection

File: `src/eggpool/proxy/normalized_usage.py`

Replace or supplement `_OPENAI_CACHE_FIELDS` with a helper:

```python
def _has_openai_cache_field(usage: dict[str, Any]) -> bool:
    if _has_any_field(usage, ("cache_read_input_tokens", "cached_tokens")):
        return True
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        return "cached_tokens" in prompt_details or "cache_write_tokens" in prompt_details
    return False
```

Then in `_extract_openai()` use:

```python
cache_status = (
    CacheCounterStatus.REPORTED
    if _has_openai_cache_field(usage)
    or any(v is not None for v in cache_tokens.values())
    else CacheCounterStatus.NOT_REPORTED
)
```

This ensures a write-only OpenRouter-style payload is explicitly classified as reported even if future refactors change token extraction details.

### E. Normalize overview fallback title/copy

File: `src/eggpool/dashboard/render.py`

Change the fallback when `cache_observability is None` from `Cache reads` to `Provider cache hit rate`.

Suggested fallback:

```python
_render_metric_card(
    title="Provider cache hit rate",
    metric=cache_read_pct,
    sub=f"legacy summary estimate · read {cache_read} · write/warmup {cache_write}",
)
```

This keeps the concept consistent even for tests or unusual direct render calls.

Also guard `cache_counter_coverage_rate` formatting. Current subtext uses:

```python
f" · {cache_counter_coverage_rate:.0%} reported"
```

If `cache_counter_coverage_rate` is `None` while eligible tokens are present due to a future malformed payload, this can throw. Use `_format_percent_unit(cache_counter_coverage_rate, digits=0)` or a small local guard.

## Tests to add or update

### Unit tests for `stats.cache_metrics`

Add explicit tests for `derive_cache_metric_terms()` and `aggregate_cache_terms()`.

Required cases:

1. Anthropic normal cache-heavy row:

```text
input=100, read=700, write=200, protocol='anthropic'
=> read=700, write=200, eligible=1000, no clamp
```

2. Anthropic read/write exceed fresh input and are not inconsistent:

```text
aggregate reported row above
=> provider_cache_hit_rate=0.7, cache_write_rate=0.2, inconsistent_cache_counter_rows=0
```

3. OpenAI normal row:

```text
input=1000, read=600, write=200, protocol='openai'
=> eligible=1000, hit_rate=0.6, write_rate=0.2
```

4. OpenAI impossible row:

```text
input=100, read=700, write=0, protocol='openai'
=> read is clamped or otherwise bounded, hit_rate <= 1.0, inconsistent_cache_counter_rows=1
```

5. Missing/not reported rows:

```text
status=NOT_REPORTED
=> excluded from numerator and denominator, counted in coverage denominator only
```

6. Unknown protocol:

```text
input=100, read=50, write=25, protocol=None or 'unknown'
=> eligible=175, hit_rate=50/175 when reported
```

### Query-level tests for `fetch_cache_observability()`

Use fixture rows if existing tests already write `requests` rows directly.

Required cases:

1. Mixed OpenAI + Anthropic aggregate:

```text
OpenAI: input=1000, read=600, write=200 => eligible 1000
Anthropic: input=100, read=700, write=200 => eligible 1000
Aggregate: read=1300, write=400, eligible=2000, hit_rate=65%, write_rate=20%
```

2. `cache_hit_ratio_known_only` equals `provider_cache_hit_rate`.

3. `inconsistent_cache_counter_rows` increments for OpenAI impossible rows, not Anthropic normal cache-heavy rows.

4. `cache_counter_coverage_rate` includes reported + not_reported + unknown rows and excludes none from coverage denominator.

### Normalized usage tests

Add or adjust tests for OpenAI-compatible nested prompt details:

1. `prompt_tokens_details.cached_tokens` read-only payload -> reported, read populated, write absent/None.
2. `prompt_tokens_details.cache_write_tokens` write-only payload -> reported, write populated, read absent/None or zero according to existing convention.
3. both fields -> reported, read/write separately populated.
4. no nested prompt cache fields -> not_reported.

### Dashboard render tests

Add or update direct render tests:

1. `render_overview(..., cache_observability=canonical_payload)` renders `Provider cache hit rate`, `write/warmup`, and does not render a cache headline titled `Cache reads`.
2. `render_overview(..., cache_observability=None)` still renders `Provider cache hit rate` fallback, not `Cache reads`.
3. `Thinking/Reasoning`, `Bandwidth received`, `Bandwidth emitted`, and `Avg TTFT (streamed)` appear in the same cards section. A robust string test can inspect the substring from `Bandwidth received` through `Thinking/Reasoning` and assert there is no intervening `</section>` before the thinking card.
4. `cache_counter_coverage_rate=None` does not raise when rendering a canonical cache card.

## Documentation cleanup

Update any text that still says:

```text
cache_hit_ratio_known_only = cached_input_tokens / input_tokens
```

Replace with:

```text
cache_hit_ratio_known_only is a deprecated compatibility alias for provider_cache_hit_rate.
provider_cache_hit_rate = cache_read_tokens_canonical / cache_eligible_input_tokens on rows where cache counters were provider-reported.
```

Update docs/comments that imply writes are part of hits. Wording should consistently use:

- `cache read` / `hit`
- `cache write` / `cache creation` / `warmup`
- `cache-accounted tokens` for read + write raw accounting totals

## Validation commands

Run focused tests first:

```bash
pytest -q tests -k "cache_metrics or cache_observability or normalized_usage or overview or dashboard"
```

Then run the specific suites that own dashboard/stat behavior if names differ:

```bash
pytest -q tests/unit tests/integration -k "cache or dashboard"
```

Then static checks:

```bash
ruff check src tests
pyright
```

If the full suite is feasible, run it after the focused pass:

```bash
pytest -q
```

## Acceptance criteria

- Anthropic cache-heavy rows are not clamped against fresh input.
- Anthropic example `input=100/read=700/write=200` yields hit rate `70%` and write rate `20%`.
- OpenAI-compatible rows still use prompt/input tokens as the denominator and cannot display hit rates above 100%.
- `inconsistent_cache_counter_rows` counts OpenAI-compatible clamp events.
- `cache_hit_ratio_known_only` is a compatibility alias of `provider_cache_hit_rate`, not the old raw formula.
- OpenAI-compatible write-only payloads are explicitly classified as cache-counter reported.
- Overview never reintroduces `Cache reads` as the headline card title, even on fallback rendering.
- Dashboard rendering handles `cache_counter_coverage_rate=None` without exception.
- Tests cover the above cases and pass with ruff/pyright clean.

## Suggested patch order

1. Fix `derive_cache_metric_terms()` protocol-specific clamp behavior.
2. Fix `aggregate_cache_terms()` inconsistency counting.
3. Add `stats.cache_metrics` unit tests for Anthropic/OpenAI/unknown rows.
4. Change `cache_hit_ratio_known_only` alias behavior and update `fetch_cache_observability()` docstring.
5. Add query-level regression tests for mixed OpenAI/Anthropic aggregation.
6. Add `_has_openai_cache_field()` and normalized usage tests.
7. Tighten overview fallback card title and coverage formatting guard.
8. Add dashboard render regression tests.
9. Update docs/comments and run validation.
