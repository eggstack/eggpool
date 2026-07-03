# Dashboard Token and Cache Metrics Corrective Plan

## Context

The dashboard has shown two high-confidence accounting/display anomalies:

1. `Total tokens` appeared to stall around `615 M` even while traffic continued.
2. `Cache tokens` read share has rendered above `100%`.

The current implementation contains several plausible root causes. The most urgent is a real rollup timestamp comparison bug: `MetricsWriteCoalescer` writes `usage_rollups.bucket_start` with a `T...Z` timestamp shape, while the statistics service queries rollups with SQLite-style `YYYY-MM-DD HH:MM:SS` bounds. Because the rollup repository compares `bucket_start` lexicographically, same-day buckets with `T` can compare greater than an end bound containing a space and therefore disappear from `1h`, `24h`, `7d`, and `30d` summaries until the date boundary changes. Since `StatsService.get_summary()` prefers rollups whenever they return any requests, the dashboard can display a partial rollup snapshot rather than live `requests` rows.

The cache-percentage issue is a semantic/display bug. The overview card currently computes `cache_read_tokens / total_input_tokens`. That denominator is only safe for providers where cached prompt tokens are known to be a subset of prompt/input tokens. Anthropic-compatible providers can report cache reads and cache creations as separate counters outside fresh `input_tokens`, so the ratio can validly exceed `100%` even when the raw counters are correct. The label implies a bounded percentage; the denominator does not guarantee that invariant.

There is also broader semantic drift: dashboard `total_tokens` generally means `input_tokens + output_tokens`, while quota/routing records final usage as `input_tokens + output_tokens + cache_read_tokens + cache_write_tokens`. This makes cross-screen comparisons confusing and can mask cache-heavy usage.

## Goals

Fix the dashboard so token and cache metrics are monotonic, internally consistent, and explicit about what is being counted. The corrected implementation should:

- Ensure rollup-backed summaries include all relevant buckets for the selected time range.
- Normalize persisted rollup timestamps so string comparisons are safe and stable.
- Backfill existing `usage_rollups.bucket_start` values without losing historical aggregates.
- Prevent the dashboard from rendering misleading cache percentages above `100%` under a label that implies a bounded ratio.
- Expose distinct token concepts where necessary: fresh tokens, cache-read tokens, cache-write tokens, and accounted/billable-volume tokens.
- Keep live `requests` queries and rollup queries semantically aligned.
- Add regression tests for the specific failure modes.

## Non-goals

Do not change provider request/response forwarding behavior. Do not mutate upstream payloads. Do not attempt to infer provider billing rules beyond the counters already persisted. Do not remove existing request-level columns; this is an accounting/presentation correction, not a destructive schema redesign.

## Phase 1: Reproduce and pin the timestamp bug

Add focused tests around the rollup timestamp comparison behavior before changing code.

Suggested test targets:

- `tests/unit/test_metrics_buffer.py` or a new `tests/unit/test_usage_rollup_timestamps.py` for `_compute_bucket_start()`.
- `tests/unit/test_stats_rollup_summary.py` or equivalent for `UsageRollupRepository.query_summary()` via an in-memory or temp SQLite database.

Test cases:

1. Insert a rollup row with a current-day `bucket_start` shaped like `2026-07-03T04:00:00Z`; query with `start='2026-07-03 03:00:00'` and `end='2026-07-03 05:00:00'`; demonstrate the current query excludes it or behaves incorrectly.
2. Insert an equivalent row with `bucket_start='2026-07-03 04:00:00'`; confirm the query includes it.
3. Confirm `StatsService.get_summary()` prefers rollups only when the rollup result is complete enough for the requested range after the fix, not merely because it returns a nonzero historical partial.

Acceptance criteria:

- The failing pre-fix behavior is captured by a regression test or a narrowly documented expected-failure test that is flipped during Phase 2.
- The test suite can run without external provider credentials.

## Phase 2: Canonicalize rollup bucket timestamps

Change `src/eggpool/metrics/buffer.py::_compute_bucket_start()` to emit SQLite-compatible UTC timestamps using the same shape as `StatsService.format_dt()`: `YYYY-MM-DD HH:MM:SS`.

Implementation details:

- Replace `strftime("%Y-%m-%dT%H:%M:%SZ")` with `strftime("%Y-%m-%d %H:%M:%S")`.
- Add a short comment stating that `usage_rollups.bucket_start` is compared lexicographically and must therefore use the same sortable format as stats query bounds.
- Avoid local timezone formatting. Continue deriving the bucket from the event timestamp converted to UTC.

Acceptance criteria:

- `_compute_bucket_start(datetime(2026, 7, 3, 4, 37, tzinfo=UTC), 3600)` returns `2026-07-03 04:00:00`.
- Existing rollup repository range queries include same-day buckets correctly.
- No dashboard/API call path receives mixed `T...Z` timestamps for newly written rollups.

## Phase 3: Add a migration/backfill for existing rollups

Add a new migration after the current latest migration that normalizes existing `usage_rollups.bucket_start` rows.

Backfill strategy:

- Update rows matching the legacy shape: `bucket_start LIKE '____-__-__T__:__:__Z'`.
- Convert by replacing `T` with a space and trimming the trailing `Z`.
- Use an idempotent SQL statement so re-running migrations is safe.

Example SQL shape:

```sql
UPDATE usage_rollups
SET bucket_start = replace(substr(bucket_start, 1, 19), 'T', ' ')
WHERE bucket_start LIKE '____-__-__T__:__:__Z';
```

Before adding the migration, inspect the migration runner conventions in `src/eggpool/db/migrations.py` and the existing migration files/inline migration definitions. Match the repository's established style exactly.

Important edge cases:

- If a legacy row normalizes onto the same unique key as an already-normalized row, the migration must merge counters rather than violate a unique constraint. Check the `usage_rollups` unique constraint: it appears to key on `(bucket_start, bucket_size_s, provider_id, model_id, account_id, protocol, streamed, status)`. If collisions are possible, implement a two-step migration:
  1. Create a temporary normalized aggregate table or CTE that groups by the normalized key and sums counters/min/maxes latency fields appropriately.
  2. Replace affected rows with merged rows.
- If the migration framework does not support complex SQL easily, use a Python migration hook if the project has that convention. Otherwise add a dedicated repository maintenance helper invoked from the migration.

Acceptance criteria:

- Existing `T...Z` rows are normalized.
- Duplicate normalized keys are merged without data loss.
- Request counts, token sums, cache sums, cost sums, byte sums, retry counts, and thinking character sums are preserved.
- Latency min/max and first-byte sums/counts remain mathematically correct.
- The migration is idempotent.

## Phase 4: Align rollup and live summary semantics

Audit and align these paths:

- `StatsService.get_summary()` and `get_summary_from_rollups()`.
- `stats.queries.fetch_summary()`.
- `UsageRollupRepository.query_summary()`.
- `fetch_account_stats()` and `fetch_model_stats()` where dashboard pages show token totals.
- `fetch_timeseries()` and rollup-backed timeseries methods.

Current inconsistency to resolve:

- Live summary `total_tokens` is `input + output` for non-pending rows in some places.
- Account/model/timeseries totals generally include `input + output` for all rows in the period.
- Quota estimator usage volume includes `input + output + cache_read + cache_write`.
- Normalized provider usage sometimes has a provider-reported `total_tokens` that does not match either display total or quota-accounted total.

Recommended schema/API semantics:

- Keep `total_tokens` as backward-compatible `input_tokens + output_tokens` for now, but document it internally as `fresh_tokens` semantics.
- Add derived fields to summary/account/model/timeseries payloads:
  - `fresh_tokens = input_tokens + output_tokens`.
  - `cache_tokens = cache_read_tokens + cache_write_tokens`.
  - `accounted_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens`.
- For overview display, use `accounted_tokens` where the intent is total provider-accounted traffic volume. Keep `fresh_tokens` visible in subtext.
- Do not blindly sum `total_tokens_reported` across providers until provider semantics are normalized; keep it diagnostic only unless a later pass defines provider-specific billing semantics.

Pending-row policy:

- Use terminal rows only for completed dashboard usage totals unless a page is explicitly showing in-flight/pending health.
- Add the same `status != 'pending'` filter to rollup-derived summaries if live summaries exclude pending rows.
- Because rollups are emitted only after request finalization, rollups should naturally be terminal-only. Still keep status filtering explicit in rollup SQL to make the invariant obvious and future-safe.

Acceptance criteria:

- Summary from live `requests` and summary from `usage_rollups` agree on request count, fresh tokens, cache tokens, accounted tokens, and cost for the same fixture dataset.
- Existing consumers of `total_tokens` are not broken.
- New fields are present in JSON stats payloads and dashboard renderer inputs.

## Phase 5: Correct cache ratio semantics and display

Replace the current overview calculation of `cache_read_tokens / total_input_tokens` with a bounded, protocol-neutral ratio or relabel the unbounded one. Preferred implementation is bounded:

- `cache_addressable_input_tokens = total_input_tokens + total_cache_read_tokens + total_cache_write_tokens`.
- `cache_read_ratio = total_cache_read_tokens / cache_addressable_input_tokens` when denominator > 0.
- `cache_write_ratio = total_cache_write_tokens / cache_addressable_input_tokens` when denominator > 0.

Dashboard copy:

- Change the card subtext from `X% of input · write Y` to something like `X% of input volume · write Y` or `X% read share · write Y`.
- If retaining the current unbounded denominator for diagnostic reasons, label it explicitly as `reads / fresh input` and do not imply it is a hit rate. In that case, values above `100%` are valid and should not be treated as a UI bug. The preferred path remains bounded input-volume share.

Account/model table handling:

- Any per-account or per-model `cache_read_ratio` currently computed as `cache_read_tokens / input_tokens` should either:
  - switch to the bounded denominator above, or
  - be renamed to `cache_read_to_fresh_input_ratio` and rendered with explicit labeling.
- Prefer switching to bounded denominator for dashboard-facing fields, and optionally add diagnostic raw ratios under separate names only if useful.

Acceptance criteria:

- Cache read percentage on overview cannot exceed `100%` when using the bounded denominator.
- Cache-heavy Anthropic-compatible fixtures render sensible values.
- Zero-denominator cases render `—` or equivalent, not `0%` unless the upstream explicitly reported zero cache activity on nonzero input volume.

## Phase 6: Guard rollup preference against partial/stale data

The summary path currently prefers rollups whenever rollups return `total_requests > 0`. This is unsafe if rollups are partial, stale, or have mixed timestamp formats.

Implement a safer fallback policy:

- For fixed recent ranges (`1h`, `24h`, `7d`, `30d`), compare rollup coverage against live request coverage when feasible.
- At minimum, if the rollup maximum `bucket_start` is older than the latest terminal request in the selected range by more than one flush interval plus one bucket size, fall back to live `requests` for the summary.
- Expose a diagnostic field in the summary payload, e.g. `summary_source: 'rollup' | 'requests'` and optionally `rollup_lag_seconds`.
- Render this only in a debug tooltip or runtime/stats diagnostics page, not necessarily on the main dashboard.

Implementation notes:

- Add a lightweight `UsageRollupRepository.query_coverage()` returning min/max bucket, request count, and updated_at max for a range.
- Add a lightweight live latest-terminal-request query.
- Keep this cheap; do not add expensive full live-vs-rollup comparisons on every dashboard refresh unless guarded by cache.

Acceptance criteria:

- If rollups stop flushing, the dashboard does not remain permanently stuck on stale rollup totals.
- A runtime/stats diagnostic can show whether the summary came from rollups or live requests.
- Normal operation still uses rollups for large windows when coverage is current.

## Phase 7: Tests and fixtures

Add fixture-driven tests for the exact anomalies.

Required tests:

1. `test_rollup_bucket_start_format_matches_stats_bounds`
   - Verifies `_compute_bucket_start()` emits `YYYY-MM-DD HH:MM:SS`.

2. `test_rollup_summary_includes_current_day_bucket`
   - Inserts a row at a current-day bucket and queries a same-day window.

3. `test_rollup_migration_normalizes_legacy_tz_buckets`
   - Seeds `usage_rollups` with `T...Z` rows and runs migrations.
   - Confirms all are normalized and sums preserved.

4. `test_rollup_migration_merges_normalized_key_collisions`
   - Seeds one legacy and one already-normalized row that normalize to the same key.
   - Confirms the merged row preserves additive counters and min/max latency fields.

5. `test_overview_cache_ratio_bounded_for_cache_heavy_anthropic_usage`
   - Fixture: input=5_000, output=1_000, cache_read=50_000, cache_write=10_000.
   - Expected bounded read share: `50_000 / 66_000`, approximately `75.8%`, not `1000%`.

6. `test_summary_live_and_rollup_token_fields_agree`
   - Same fixture represented in `requests` and `usage_rollups`.
   - Confirms fresh/cache/accounted token fields match.

7. `test_dashboard_total_tokens_does_not_stall_when_rollup_lag_detected`
   - Seed stale rollups plus newer terminal request rows.
   - Confirm summary falls back to live rows or reports live-compatible totals.

Run:

```bash
pytest tests/unit/test_metrics_buffer.py tests/unit/test_stats_rollup_summary.py tests/unit/test_dashboard_render.py
pytest
ruff check src tests
pyright
```

Adjust exact file names to match existing test organization.

## Phase 8: Operator diagnostics and documentation

Update dashboard/tooltips/docs so operators can understand the corrected metrics.

Documentation updates:

- Update the dashboard metric tooltip for `Total tokens` to clarify whether it is fresh tokens or accounted tokens after the implementation decision.
- Update the `Cache tokens` tooltip to distinguish cache reads, cache writes, and read share denominator.
- Add a short note in dashboard or stats docs explaining that provider-reported total token fields are preserved separately and not blindly summed unless normalized.

Optional runtime diagnostic:

- Add a small stat to the runtime page or stats API showing metrics coalescer health:
  - write mode
  - buffered events
  - total events received/flushed/dropped
  - last flush age
  - last flush error
- The coalescer already exposes a `snapshot()` method with these values, so the work is mostly route/render wiring.

Acceptance criteria:

- Operators can distinguish stale rollup behavior from true no-traffic behavior.
- Dashboard labels no longer imply invalid invariants.

## Suggested implementation order

1. Add tests that pin the timestamp bug and cache-ratio bug.
2. Change `_compute_bucket_start()` to SQLite format.
3. Add migration/backfill for existing rollup rows.
4. Align summary fields and add `fresh_tokens`, `cache_tokens`, `accounted_tokens`.
5. Update dashboard renderer ratio/display semantics.
6. Add rollup coverage guard/fallback.
7. Update docs/tooltips.
8. Run full tests and type/lint checks.

## Manual verification checklist

After deploying the fix against a real EggPool database:

1. Run a pre-migration diagnostic query:

```sql
SELECT COUNT(*) AS legacy_rows
FROM usage_rollups
WHERE bucket_start LIKE '____-__-__T__:__:__Z';
```

2. Run migrations.

3. Confirm no legacy bucket timestamps remain:

```sql
SELECT COUNT(*) AS legacy_rows
FROM usage_rollups
WHERE bucket_start LIKE '____-__-__T__:__:__Z';
```

4. Compare live request totals and rollup totals over `24h`:

```sql
SELECT
  COALESCE(SUM(input_tokens), 0) AS input_tokens,
  COALESCE(SUM(output_tokens), 0) AS output_tokens,
  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
  COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
  COALESCE(SUM(input_tokens + output_tokens), 0) AS fresh_tokens,
  COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) AS accounted_tokens
FROM requests
WHERE started_at >= datetime('now', '-24 hours')
  AND status != 'pending';
```

```sql
SELECT
  COALESCE(SUM(input_tokens), 0) AS input_tokens,
  COALESCE(SUM(output_tokens), 0) AS output_tokens,
  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
  COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
  COALESCE(SUM(input_tokens + output_tokens), 0) AS fresh_tokens,
  COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) AS accounted_tokens
FROM usage_rollups
WHERE bucket_start >= datetime('now', '-24 hours')
  AND bucket_start < datetime('now')
  AND status != 'pending';
```

The numbers may differ slightly if the metrics buffer has not flushed yet; after one flush interval they should converge.

5. Generate a cache-heavy request and confirm `Cache tokens` no longer renders an apparent impossible hit rate above `100%` unless deliberately labeled as an unbounded diagnostic ratio.

## Risks

- Migration collision handling is the main risk. If legacy and normalized rows coexist for the same unique key, a naive `UPDATE` can fail. Implement merge logic rather than assuming no collisions.
- Changing `total_tokens` semantics directly could break downstream consumers. Prefer adding explicit new fields and then adjusting dashboard display, rather than silently redefining `total_tokens`.
- Rollup coverage checks must remain cheap. Avoid full live-vs-rollup aggregation on every dashboard refresh unless cached.
- Cache token semantics differ by provider. Keep the labels precise and avoid presenting cache share as a universal provider billing metric.

## Definition of done

- Newly written rollup buckets use the same timestamp format as stats query bounds.
- Existing legacy rollup buckets are normalized and merged safely.
- Rollup-backed summaries include current-day traffic and do not stall on partial stale rollups.
- Overview cache percentage is bounded or explicitly labeled as an unbounded diagnostic ratio.
- Dashboard and API payloads distinguish fresh/cache/accounted token totals.
- Live request and rollup summaries agree on token semantics for fixture data.
- Regression tests cover timestamp normalization, rollup range inclusion, migration collision merging, cache-heavy percentage rendering, and stale-rollup fallback.
- `pytest`, `ruff check`, and `pyright` pass.
