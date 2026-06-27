-- Migration 0033: Add provider-reported vs. local cost audit columns to requests.
--
-- Today the dashboard's ``Total cost`` field sums ``requests.cost_microdollars``,
-- but that single field conflates three different numbers:
--
--   * authoritative provider-reported actual API cost (when the upstream
--     exposes one in the response payload),
--   * EggPool-derived cost from trusted provider/model token rates, and
--   * conservative local reservation estimates used for routing and quota
--     pressure.
--
-- Phase 1 of the provider-reported cost dashboard fix keeps ``cost_microdollars``
-- as the canonical displayed value and adds four nullable audit columns so the
-- dashboard and diagnostics can distinguish the source of the recorded number
-- without rewriting historical rows:
--
--   * provider_cost_microdollars -- authoritative upstream-reported billed
--     cost, in microdollars.  NULL when the upstream did not report a value.
--   * provider_cost_source       -- short label for the field path that
--     produced the value (e.g. ``opencode_go:usage.cost_usd``).  NULL when
--     no provider cost was reported.
--   * local_cost_microdollars    -- EggPool-derived cost from the pricing
--     resolution pipeline.  Preserves the previous cost math for diagnostic
--     comparison even when the canonical value is overridden by a provider
--     report.
--   * local_cost_exactness       -- exactness label for the local cost
--     (``derived`` / ``partial`` / ``estimated`` / ``unknown``) so operators
--     can compare the local estimate with the canonical value.
--
-- All four columns are nullable; rows that pre-date this migration simply
-- store NULL.  No historical backfill is attempted: we cannot fabricate
-- provider-reported values for past requests, and rewriting local_cost from
-- cost_microdollars would conflate "value at the time of the request" with
-- "value under the current pricing snapshot".

ALTER TABLE requests ADD COLUMN provider_cost_microdollars INTEGER;
ALTER TABLE requests ADD COLUMN provider_cost_source TEXT;
ALTER TABLE requests ADD COLUMN local_cost_microdollars INTEGER;
ALTER TABLE requests ADD COLUMN local_cost_exactness TEXT;