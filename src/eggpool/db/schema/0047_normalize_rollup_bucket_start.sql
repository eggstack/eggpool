-- Migration 0047: Normalize usage_rollups bucket_start timestamp format.
--
-- ``MetricsWriteCoalescer._compute_bucket_start`` historically emitted
-- ``YYYY-MM-DDTHH:MM:SSZ`` (ISO-8601 with ``T`` and ``Z`` separators)
-- while ``StatsService.format_dt`` emits ``YYYY-MM-DD HH:MM:SS``
-- (space separator, no trailing ``Z``).  Rollup queries compare
-- ``bucket_start`` lexicographically against ``format_dt`` bounds, so
-- same-day buckets written with ``T`` always compare greater than
-- the end bound (``'T'`` > ``' '``) and were silently excluded from
-- rollup-backed summaries.  Net effect: dashboards under-reported
-- total tokens for the in-flight hour.
--
-- The writer now emits the space-separated form.  This migration
-- rewrites the legacy rows in place so historical data is queryable
-- again without forcing operators to flush.  Rows already in the new
-- shape are unaffected.  The conversion is idempotent: when no
-- ``T...Z`` rows remain the WHERE clause matches nothing and the
-- entire statement becomes a no-op.

UPDATE usage_rollups
SET bucket_start = replace(replace(bucket_start, 'T', ' '), 'Z', '')
WHERE bucket_start LIKE '____-__-__T__:__:__Z';