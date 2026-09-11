-- Migration 0045: Synthetic provider cache controls (Phase 9).
--
-- Phase 9 of the cache-preserving deterministic compression roadmap
-- adds an opt-in layer that synthesises provider cache_control
-- annotations around stable-prefix regions for providers that
-- support explicit cache boundary hints (initially Anthropic).  The
-- feature is disabled by default and dry-run by default; the
-- ``synthetic_cache_*`` columns persist the audit metadata so
-- operators can verify the selector before enabling apply mode.
--
--   * synthetic_cache_status is a stable status string with one of:
--       - "disabled": synthetic cache controls are off (global or
--         policy resolved config disabled).
--       - "policy_required": require_policy blocked the run because
--         no policy override matched.
--       - "provider_unsupported": target provider protocol / kind is
--         not in the configured supported set.
--       - "no_candidates": selector ran but found no eligible
--         stable-prefix placement.
--       - "dry_run": selector ran, candidates were recorded, but no
--         mutation occurred.
--       - "applied": at least one synthetic cache_control was
--         written to the provider-bound body.
--   * synthetic_cache_dry_run mirrors the resolved dry_run flag.
--   * synthetic_cache_candidate_count is the number of placements
--     the selector surfaced (0 in dry_run/applied/no_candidates
--     paths where the selector found nothing).
--   * synthetic_cache_applied_count is the number of placements the
--     mutator actually wrote (0 in dry_run / disabled / no_candidates).
--   * synthetic_cache_warning_count is the count of warning codes
--     emitted (e.g. dry_run, below_min_tokens, limit_reached).
--   * synthetic_cache_warnings_json is a JSON array of stable
--     warning codes so dashboards can group by reason without
--     re-parsing upstream payloads.
--   * synthetic_cache_policy_name is the resolved compression policy
--     name that gated the run (or ``<global>`` when no policy
--     override matched).  Re-uses the Phase 6 audit pattern.
--   * synthetic_cache_policy_source mirrors Phase 6
--     compression_policy_source semantics.
--   * synthetic_cache_summary_json is a JSON blob with the full
--     plan summary (status, dry_run, candidate/applied/warning
--     counts, sorted placements, sorted reasons, policy name).
--     Kept JSON-shaped for forward-compatibility: later phases can
--     add fields without a schema bump.
--
-- Indexes support the dashboard / stats roll-ups in
-- fetch_synthetic_cache_summary.  status+started_at is the most
-- common filter ("show me applied/dry_run requests in the last
-- 24h"); policy_name+started_at lets operators drill into a
-- specific override.

ALTER TABLE requests ADD COLUMN synthetic_cache_status TEXT;
ALTER TABLE requests ADD COLUMN synthetic_cache_dry_run INTEGER NOT NULL DEFAULT 1;
ALTER TABLE requests ADD COLUMN synthetic_cache_candidate_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN synthetic_cache_applied_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN synthetic_cache_warning_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN synthetic_cache_warnings_json TEXT;
ALTER TABLE requests ADD COLUMN synthetic_cache_policy_name TEXT;
ALTER TABLE requests ADD COLUMN synthetic_cache_policy_source TEXT;
ALTER TABLE requests ADD COLUMN synthetic_cache_summary_json TEXT;

CREATE INDEX IF NOT EXISTS idx_requests_synthetic_cache_status
    ON requests(synthetic_cache_status, started_at);

CREATE INDEX IF NOT EXISTS idx_requests_synthetic_cache_policy_name
    ON requests(synthetic_cache_policy_name, started_at);