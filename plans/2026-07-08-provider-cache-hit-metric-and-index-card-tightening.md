# Provider cache hit metric cleanup and overview card tightening

Date: 2026-07-08
Status: handoff plan
Scope: cache/token stats semantics, dashboard overview cards, cache observability API, dashboard copy/tests

## Context

EggPool already records provider cache counters, but the public dashboard semantics are still easy to misread. The overview currently exposes `Accounted tokens`, `Fresh tokens`, and `Cache reads`, while the dedicated cache page exposes `Reported cache read share`. This is directionally better than the prior raw `cache tokens` card, but the underlying names and ratios are still not clean enough for an operator who expects a normal provider-style `cache hit` metric.

The provider norm is roughly:

- OpenAI-compatible usage reports prompt-cache hits as `usage.prompt_tokens_details.cached_tokens`.
- Anthropic usage splits cache accounting into `cache_read_input_tokens`, `cache_creation_input_tokens`, and fresh `input_tokens`.
- OpenRouter follows the OpenAI-compatible shape for `cached_tokens` and additionally exposes `cache_write_tokens` under prompt token details for cache writes/warmup.
- Gemini-style integrations may expose a cached-token total rather than the same OpenAI/Anthropic fields.

The important semantic distinction is reads versus writes. A cache read is a hit. A cache write/cache creation is a warmup or cache population event. Read and write tokens both matter for billing/accounting, but only reads should be counted as cache hits.

Current EggPool source shape that this plan must preserve:

- `src/eggpool/proxy/normalized_usage.py` already preserves `None` versus `0` and has provider-neutral fields for `cached_input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_write_input_tokens`, and `cache_counter_status`.
- `src/eggpool/stats/queries.py::fetch_cache_observability` already gates ratios on `cache_counter_status = 'reported'`, which is correct; missing cache counters must not become cache misses.
- `src/eggpool/dashboard/render.py::render_overview` already distinguishes `fresh_tokens` from broader `accounted_tokens` and computes a bounded cache-read share for the overview card.
- `src/eggpool/dashboard/render.py::_render_thinking_stats` currently returns its own full `section.cards`, so the single Thinking/Reasoning card can consume a full row and waste vertical space.
- `render_overview` currently renders bandwidth received, bandwidth emitted, and streamed TTFT in one row, then renders `_render_thinking_stats(thinking_stats)` as a separate row below it.

## Goals

Make cache metrics line up with common provider expectations:

1. Primary cache metric should be `Provider cache hit rate`, not raw cache tokens.
2. Cache hit rate must mean cache reads divided by provider-accounted prompt input volume.
3. Cache writes/cache creation must be separate and named as warmup/write volume, not hits.
4. Coverage/reporting status must stay visible, but as a submetric/badge rather than the headline operator metric.
5. Missing cache counters must remain distinct from zero cache hits.
6. Existing public API consumers of `total_tokens` and prior raw token fields should not break.

Tighten the overview layout:

1. Move Thinking/Reasoning into the same compact card row as Bandwidth received, Bandwidth emitted, and Avg TTFT.
2. Avoid rendering a full-width Thinking/Reasoning row for one card.
3. Keep the card hidden when there are no thinking counters, unless a future design intentionally renders an explicit zero state.
4. Keep mobile/tablet behavior stable with the existing `.cards` CSS.

## Non-goals

This is not a synthetic cache controls rollout. It should not change routing, request shaping, provider cache annotation behavior, compression behavior, or cost calculation.

This is not a database reset. Prefer additive derived fields and backward-compatible response keys. Only add migrations if the implementation truly needs durable per-request canonical fields that cannot be derived cheaply from existing columns.

This is not a full redesign of `/cache`; the cache page should be clarified, but the main requested frontend work is the index/overview cards.

## Proposed metric contract

Introduce these canonical derived terms and use them consistently in stats responses and dashboard copy:

```text
cache_read_tokens_canonical
    Provider-reported tokens read from prompt cache. These are hits.

cache_write_tokens_canonical
    Provider-reported tokens written/created into prompt cache. These are warmup/write tokens, not hits.

cache_eligible_input_tokens
    Fresh input tokens + cache read tokens + cache write tokens for rows where cache counters are reported.

provider_cache_hit_rate
    cache_read_tokens_canonical / cache_eligible_input_tokens

cache_write_rate
    cache_write_tokens_canonical / cache_eligible_input_tokens

cache_benefited_request_rate
    count(reported finalized requests with cache_read_tokens_canonical > 0)
      / count(reported finalized requests with cache_eligible_input_tokens > 0)

cache_counter_coverage_rate
    reported rows / finalized non-pending rows in the selected window
```

Rules:

- `provider_cache_hit_rate` is token-volume weighted and should be the primary dashboard percentage.
- `cache_benefited_request_rate` is request weighted and should be a secondary/subtext metric.
- `cache_write_rate` should be displayed as `write/warmup`, not `hit`.
- Ratios return `None` when their denominator is zero or unavailable; render as `—`.
- Do not include `not_reported` rows in hit-rate denominators.
- Do not include `unknown_format` rows in hit-rate denominators.
- Do include reported zero-cache rows in denominators, because those are true misses/zero-hit rows.

## Provider mapping rules

### OpenAI-compatible payloads

Input payload shape:

```json
{
  "usage": {
    "prompt_tokens": 1234,
    "completion_tokens": 56,
    "prompt_tokens_details": {
      "cached_tokens": 768,
      "cache_write_tokens": 0
    }
  }
}
```

Mapping:

```text
cache_read_tokens_canonical = prompt_tokens_details.cached_tokens or 0 when reported
cache_write_tokens_canonical = prompt_tokens_details.cache_write_tokens or 0 when reported
cache_eligible_input_tokens = prompt_tokens
```

If `prompt_tokens` already includes cached tokens, which is the OpenAI-compatible convention, do not add cached tokens again for denominator purposes. Clamp any per-request derived fresh-input calculations to zero if `cached_tokens + cache_write_tokens > prompt_tokens` due to upstream inconsistency, but keep the raw provider numbers visible for diagnostics.

### OpenRouter OpenAI-compatible payloads

OpenRouter should follow the OpenAI-compatible logic above, but `normalized_usage.py` needs to recognize `prompt_tokens_details.cache_write_tokens` in addition to `cached_tokens`. Do not treat write tokens as hits.

### Anthropic payloads

Input payload shape:

```json
{
  "usage": {
    "input_tokens": 123,
    "output_tokens": 45,
    "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 300
  }
}
```

Mapping:

```text
cache_read_tokens_canonical = cache_read_input_tokens
cache_write_tokens_canonical = cache_creation_input_tokens
cache_eligible_input_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
```

Anthropic `input_tokens` is fresh/non-cached input in this accounting model, so adding read and creation tokens is required for the denominator.

### Gemini/future providers

If a provider exposes only `total_cached_tokens` and a trustworthy total input denominator, map cached tokens to reads and compute the ratio. If it exposes cached tokens without a reliable denominator, keep token totals but set hit-rate denominator-dependent fields to `None` and surface `cache_counter_status = 'reported'` plus a provider-specific diagnostic.

## Implementation steps

### 1. Normalize provider cache read/write semantics

File: `src/eggpool/proxy/normalized_usage.py`

- Extend OpenAI-compatible cache field detection so nested `prompt_tokens_details.cache_write_tokens` is recognized.
- Update `_extract_openai_cache_tokens` to return `cache_write_input_tokens` when the nested `cache_write_tokens` field exists.
- Keep `cached_input_tokens` backward-compatible, but document that it is a legacy aggregate/raw compatibility field and not necessarily the hit-only value.
- Prefer adding explicit helper names such as `_extract_openai_prompt_token_details` if that keeps parsing clear.
- Preserve `None` versus `0` exactly. Key presence with numeric zero means reported zero; missing key means unavailable.

Acceptance details:

- OpenAI payload with only `cached_tokens` reports `cache_counter_status = reported`, read tokens populated, write tokens `None` or zero only if explicitly present.
- OpenRouter-style payload with `cache_write_tokens` reports write tokens separately.
- A payload with neither cache key remains `not_reported`, not `reported zero`.
- Malformed nested details remains non-fatal.

### 2. Add canonical cache metric helper logic

Recommended file: `src/eggpool/stats/cache_metrics.py` or a small helper section in `src/eggpool/stats/queries.py` if the project prefers fewer modules.

Implement a helper that can derive canonical per-row or aggregate terms from persisted columns:

```python
def derive_cache_metric_terms(
    *,
    upstream_protocol: str | None,
    input_tokens: int,
    cached_input_tokens: int | None,
    cache_read_input_tokens: int | None,
    cache_write_input_tokens: int | None,
    cache_creation_input_tokens: int | None,
    cache_counter_status: str,
) -> CacheMetricTerms:
    ...
```

Expected behavior:

- For Anthropic rows, read = `cache_read_input_tokens`, write = `cache_creation_input_tokens` or `cache_write_input_tokens`, denominator = `input_tokens + read + write`.
- For OpenAI rows, read = `cached_input_tokens` or `cache_read_input_tokens`, write = `cache_write_input_tokens`, denominator = `input_tokens` when `input_tokens > 0`; if a provider has persisted OpenAI-compatible values differently, fallback denominator can be `input_tokens + read + write` only behind a clearly named compatibility guard.
- For unknown protocol rows, use conservative denominator `input_tokens + read + write` only when the row has explicit granular read/write fields; otherwise return unknown denominator.
- Clamp denominator to at least read + write only for diagnostics, but do not hide inconsistencies. Include an `inconsistent_counter_rows` count in aggregates.

If a SQL-only approach is chosen instead, encode the same protocol-aware `CASE` logic in `fetch_cache_observability`, but keep the formula documented near the query.

### 3. Rework `fetch_cache_observability`

File: `src/eggpool/stats/queries.py`

Add new top-level response keys while preserving current keys for compatibility:

```text
provider_cache_hit_rate
cache_write_rate
cache_benefited_request_rate
cache_counter_coverage_rate
cache_read_tokens_canonical
cache_write_tokens_canonical
cache_eligible_input_tokens
cache_benefited_requests
cache_eligible_requests
cache_counter_reported_requests
cache_counter_not_reported_requests
cache_counter_unknown_requests
inconsistent_cache_counter_rows
```

Compatibility:

- Keep `cache_hit_ratio_known_only`, but make it an alias for `provider_cache_hit_rate` or mark it deprecated in code comments. Do not keep the old `cached_input_tokens / input_tokens` behavior if it miscalculates Anthropic rows.
- Keep `total_cached_input_tokens`, `total_cache_read_input_tokens`, `total_cache_creation_input_tokens`, and `total_cache_write_input_tokens` as raw totals.
- Update per-account and per-model breakdowns to include canonical read/write/eligible totals and hit rate where cheap enough.

SQL shape suggestion:

Use a CTE over finalized rows in the time window:

```sql
WITH cache_rows AS (
  SELECT
    *,
    CASE WHEN cache_counter_status = 'reported' THEN 1 ELSE 0 END AS reported_cache_row,
    CASE
      WHEN cache_counter_status = 'reported' AND upstream_protocol = 'anthropic'
        THEN COALESCE(cache_read_input_tokens, 0)
      WHEN cache_counter_status = 'reported'
        THEN COALESCE(cache_read_input_tokens, cached_input_tokens, 0)
      ELSE 0
    END AS cache_read_canonical,
    CASE
      WHEN cache_counter_status = 'reported' AND upstream_protocol = 'anthropic'
        THEN COALESCE(cache_creation_input_tokens, cache_write_input_tokens, 0)
      WHEN cache_counter_status = 'reported'
        THEN COALESCE(cache_write_input_tokens, 0)
      ELSE 0
    END AS cache_write_canonical,
    CASE
      WHEN cache_counter_status = 'reported' AND upstream_protocol = 'anthropic'
        THEN COALESCE(input_tokens, 0)
           + COALESCE(cache_read_input_tokens, 0)
           + COALESCE(cache_creation_input_tokens, cache_write_input_tokens, 0)
      WHEN cache_counter_status = 'reported'
        THEN COALESCE(input_tokens, 0)
      ELSE 0
    END AS cache_eligible_input_canonical
  FROM requests
  WHERE started_at >= ? AND started_at < ? AND status != 'pending'
)
```

Then aggregate from `cache_rows`. Add inconsistency counters where `cache_read_canonical + cache_write_canonical > cache_eligible_input_canonical` for OpenAI-compatible rows.

### 4. Rework summary/index cache fields

File: `src/eggpool/stats/queries.py`

`fetch_summary` and `_build_summary` currently expose `total_cache_read_tokens`, `total_cache_write_tokens`, `fresh_tokens`, and `accounted_tokens`. Add provider-cache headline fields to the summary result so the index does not need to recompute them incorrectly from raw totals:

```text
provider_cache_hit_rate
cache_write_rate
cache_counter_coverage_rate
cache_benefited_request_rate
cache_read_tokens_canonical
cache_write_tokens_canonical
cache_eligible_input_tokens
```

Implementation options:

- Preferred: have `StatsService.get_dashboard_overview` fetch `get_cache_observability(period, use_cache=True)` and inject a compact `cache` object into the overview payload. This avoids duplicating cache SQL in `fetch_summary` and keeps cache semantics in one query.
- Alternative: embed a small cache CTE in `fetch_summary`. This is less desirable because summary is already dense and the cache-specific logic will drift from `fetch_cache_observability`.

Recommended overview payload shape:

```python
return {
    "summary": summary,
    "imbalance": imbalance,
    "cache": {
        "provider_cache_hit_rate": ...,
        "cache_write_rate": ...,
        "cache_counter_coverage_rate": ...,
        "cache_benefited_request_rate": ...,
        "cache_read_tokens_canonical": ...,
        "cache_write_tokens_canonical": ...,
        "cache_eligible_input_tokens": ...,
    },
    ...
}
```

Update callers carefully. Existing routes that call `get_dashboard_overview` should not need new parameters beyond the period/time range they already have.

### 5. Update overview dashboard cache cards

File: `src/eggpool/dashboard/render.py`

Replace the current index cache card semantics:

Current likely row:

- `Accounted tokens`
- `Request shaping`
- `Fresh tokens`
- `Cache reads`
- `Reasoning tokens`
- `Throughput`
- `Streaming`
- `Exactness`

Target semantics:

- Keep `Accounted tokens`, but subtext should be explicit: `fresh X · cache read Y · cache write Z`.
- Keep `Fresh tokens` as input + output.
- Rename `Cache reads` to `Provider cache hit rate`.
- The metric should be the percentage, not the raw token count.
- The subtext should be `read <tokens> · write/warmup <tokens>` or `benefited <request-rate> · coverage <coverage-rate>` depending on available width.
- Do not call cache writes hits.
- Add tooltip text for `Provider cache hit rate`, `Cache write/warmup`, and `Cache counter coverage` if surfaced.

Suggested card:

```python
_render_metric_card(
    title="Provider cache hit rate",
    metric=provider_cache_hit_rate_str,
    sub=(
        f"read {cache_read} · write/warmup {cache_write}"
        if coverage is known else
        f"coverage {coverage_str} · counters unavailable"
    ),
)
```

Optional secondary card if the row has room:

```python
_render_metric_card(
    title="Cache counter coverage",
    metric=coverage_str,
    sub=f"{reported:,} reported · {not_reported:,} omitted",
)
```

Avoid too many cards on the index. If coverage is not promoted to its own card, it should appear in the hit-rate card subtext.

### 6. Tighten overview layout for Thinking/Reasoning, bandwidth, and TTFT

File: `src/eggpool/dashboard/render.py`

Current problem:

- `_render_thinking_stats(thinking_stats)` returns a complete `<section class="cards">` with one card.
- `render_overview` renders bandwidth received, bandwidth emitted, and Avg TTFT in their own `<section class="cards">`.
- Then the thinking card is rendered as a separate row, wasting vertical space.

Target:

- Refactor `_render_thinking_stats` into a card renderer, not a section renderer.
- Keep a thin wrapper only if other pages/tests expect the old function.

Suggested refactor:

```python
def _render_thinking_stats_card(thinking_stats: dict[str, Any] | None) -> str:
    if not thinking_stats:
        return ""
    total = int(thinking_stats.get("total", 0) or 0)
    if total == 0:
        return ""
    ...
    return _render_metric_card(
        title="Thinking/Reasoning",
        metric=f"{total:,}",
        sub=sub_text,
        tooltip="...",
    )


def _render_thinking_stats(thinking_stats: dict[str, Any] | None) -> str:
    card = _render_thinking_stats_card(thinking_stats)
    if not card:
        return ""
    return f'<section class="cards">{card}</section>'
```

Then update `render_overview`:

```python
<section class="cards">
  {Bandwidth received card}
  {Bandwidth emitted card}
  {Avg TTFT card}
  {_render_thinking_stats_card(thinking_stats)}
</section>
```

Remove the later standalone `{_render_thinking_stats(thinking_stats)}` from the overview body.

Ensure empty thinking stats produce no extra whitespace/card but do not collapse the bandwidth/TTFT row.

### 7. Update dedicated cache page copy

File: `src/eggpool/dashboard/render.py`

In `_render_cache_reporting_panel`:

- Rename `Reported cache read share` to `Provider cache hit rate`.
- Use `provider_cache_hit_rate` from the stats payload.
- Add a separate card for `Cache write/warmup rate` when available.
- Keep `Rows with cache counters`, `Rows without cache counters`, and `Unrecognized payload shape`, but place them after the primary hit/write metrics or leave them as diagnostics.
- Update the table labels:
  - `Provider-reported cached input tokens` -> `Provider-reported cache-accounted input tokens` or keep raw label with explicit raw wording.
  - `Anthropic cache creation` -> `Cache write / creation`.
  - Add canonical rows: `Canonical cache reads`, `Canonical cache writes`, `Cache-eligible input denominator`.

Dedicated cache page should explain:

```text
Cache hit rate counts provider-reported cache reads only. Cache writes/creation are shown separately because they populate cache entries and are not hits.
```

### 8. Update tooltips and docs

Files:

- `src/eggpool/dashboard/render.py`
- `README.md`
- `docs/cache-compression.md`
- `docs/cache-compression-troubleshooting.md` if it mentions cache ratios
- Any dashboard snapshot or inline docs under `architecture/`

Tooltip changes:

- Replace `Cache reads` tooltip with `Provider cache hit rate` tooltip.
- Keep `Accounted tokens` tooltip clear that accounted tokens can exceed fresh input/output because provider accounting includes cache reads and writes.
- Add `Cache counter coverage` tooltip if surfaced.
- Update `Thinking/Reasoning` tooltip only if the new card function changes title or context.

Docs should state:

- Raw `total_tokens` remains legacy fresh `input + output` for API compatibility.
- `accounted_tokens` is the broad accounting total.
- Provider cache hit rate is `cache_read / cache_eligible_input`, known/reported rows only.
- Cache writes are warmup and not hits.
- Missing provider counters are unknown/unsupported reporting, not cache misses.

### 9. Tests

Add or update tests in the relevant existing dashboard/stats suites.

Likely files to inspect/update:

- `tests/test_normalized_usage.py`
- `tests/test_stats_queries.py`
- `tests/test_dashboard_render.py`
- Any existing `cache_observability` tests
- Any snapshot/string tests for overview dashboard cards

Required cases:

1. OpenAI-compatible cache read only:
   - `prompt_tokens=1000`, `cached_tokens=600`
   - hit rate = `600 / 1000 = 60%`
   - write rate = `0%` or `None` depending on explicit reported write availability; prefer `0%` if denominator is known.

2. OpenRouter-style read + write:
   - `prompt_tokens=1000`, `cached_tokens=600`, `cache_write_tokens=200`
   - hit rate = `600 / 1000 = 60%`
   - write rate = `200 / 1000 = 20%`
   - write tokens do not increase hit numerator.

3. Anthropic read + creation:
   - `input_tokens=100`, `cache_read_input_tokens=700`, `cache_creation_input_tokens=200`
   - eligible denominator = `1000`
   - hit rate = `70%`
   - write rate = `20%`

4. Reported zero cache:
   - reported fields present with zero values
   - included in denominator
   - hit rate can be `0%`.

5. Not reported:
   - no cache fields
   - coverage counts omitted/non-reported
   - not included in hit denominator.

6. Unknown format:
   - malformed usage payload
   - counted as unknown
   - not included in hit denominator.

7. Mixed provider aggregate:
   - OpenAI and Anthropic rows in same window
   - aggregate hit rate uses sum(read) / sum(provider-appropriate denominators), not average of percentages.

8. Overview layout:
   - When thinking stats total > 0, rendered HTML contains one `Thinking/Reasoning` card in the same cards section as `Bandwidth received`, `Bandwidth emitted`, and `Avg TTFT (streamed)`.
   - There is no standalone one-card `<section class="cards">` after that row.
   - When thinking stats are empty, bandwidth/TTFT row still renders normally.

9. Cache card copy:
   - Overview contains `Provider cache hit rate` and does not use `Cache reads` as the headline card title.
   - Subtext contains `write/warmup` or equivalent explicit non-hit wording.

### 10. Validation commands

Run the smallest targeted set first:

```bash
pytest tests -q -k "cache_observability or normalized_usage or dashboard"
```

Then run the dashboard marker if available:

```bash
pytest -m dashboard -q
```

Then run the request-path/stat safety subset if runtime permits:

```bash
pytest -m request_path -q
```

Finally:

```bash
ruff check src tests
pyright
```

If pyright is too slow/noisy on the deployment target, at minimum run the touched modules through pyright if the project has a module-level invocation pattern.

## Migration/backward compatibility guidance

Prefer no schema migration for the first pass. Existing columns can derive the canonical metrics:

- `input_tokens`
- `cached_input_tokens`
- `cache_read_input_tokens`
- `cache_creation_input_tokens`
- `cache_write_input_tokens`
- `cache_counter_status`
- `upstream_protocol`

If later performance indicates repeated derivation is expensive, add a follow-up migration with durable canonical columns. That should be a separate plan because it touches request finalization and rollups.

Do not remove existing API fields in this pass. Mark aliases in comments and docs:

- `cache_hit_ratio_known_only` -> deprecated alias for `provider_cache_hit_rate`
- `total_cached_input_tokens` -> raw/provider compatibility aggregate
- `total_tokens` -> legacy fresh `input + output`

## Acceptance criteria

- `/api/stats/cache-observability` returns canonical provider cache hit/read/write metrics with protocol-aware denominators.
- The dashboard overview headline says `Provider cache hit rate`, not `Cache reads`, for the cache-hit concept.
- Cache writes/cache creation are never described as hits.
- Missing cache counters are displayed as coverage/reporting gaps, not as misses.
- Anthropic rows cannot produce an inflated/misleading hit rate from `cached_input_tokens / input_tokens`.
- OpenAI/OpenRouter rows use `prompt_tokens` as the denominator and do not double-count cached tokens.
- Overview Thinking/Reasoning card renders in the same row/section as Bandwidth received, Bandwidth emitted, and Avg TTFT.
- No standalone full-width Thinking/Reasoning card row remains on the overview page.
- Existing `total_tokens`, raw cache totals, and compatibility fields remain available.
- Targeted dashboard/stats/normalization tests pass.

## Suggested implementation order

1. Add/adjust normalized OpenAI-compatible parsing for `cache_write_tokens`.
2. Add canonical cache metric derivation in stats query/helper layer.
3. Update `/api/stats/cache-observability` response keys and tests.
4. Feed compact cache metrics into overview rendering.
5. Rename/rework overview cache card title/subtext/tooltips.
6. Refactor `_render_thinking_stats` into a reusable card renderer and move it into the bandwidth/TTFT row.
7. Update dedicated cache panel wording and docs.
8. Run targeted tests and tighten any snapshots/string assertions.
