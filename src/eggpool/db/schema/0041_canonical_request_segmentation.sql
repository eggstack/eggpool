-- Migration 0041: Canonical request segmentation (Phase 2).
--
-- Phase 2 of the cache-preserving deterministic compression plan
-- (plans/cache_compression_phase_02_canonical_request_segmentation.md)
-- adds a structural segmentation layer that annotates canonical
-- requests into stable_prefix / semi_stable_context / volatile_suffix
-- regions.  The segmentation summary is computed observationally;
-- nothing in this migration changes request bodies, route scoring,
-- or eligibility.  The columns are all nullable / default to safe
-- fallbacks so the migration is non-destructive and legacy callers
-- that do not run the segmenter continue to work.
--
--   * segmentation_status records whether the segmenter produced
--     a normal result ('segmented'), saw a request with no
--     segmentable content ('empty_request'), or hit a parse
--     failure ('parse_failure').  Default 'empty_request' so
--     pre-Phase-2 rows render correctly without a backfill.
--   * stable_prefix_estimated_tokens / semi_stable_estimated_tokens /
--     volatile_estimated_tokens are coarse token estimates per
--     segment kind.  Used for thresholds and aggregate metrics
--     only; never for billing.
--   * stable_prefix_bytes / semi_stable_bytes / volatile_bytes
--     record the byte footprint of each segment kind for the
--     same reason.  Estimates are cheap and never raise.
--   * segmentation_summary_json is the compact JSON serialisation
--     of the full SegmentationResult for audit and dashboard
--     drill-down.  Raw request content is never persisted here.
--   * request_shape_hash and stable_prefix_hash were placeholders
--     added by migration 0040 for Phase 2/3.  Phase 2 now writes
--     them: stable_prefix_hash is a content-private SHA-256 digest
--     of the stable prefix structural descriptor, request_shape_hash
--     is a content-private SHA-256 digest of the request shape.
--   * All other columns are nullable / default 0 so existing
--     databases migrate cleanly.  Rollback notes in
--     plans/cache_compression_phase_02_canonical_request_segmentation.md
--     describe the disable-dashboard path.

ALTER TABLE requests ADD COLUMN segmentation_status TEXT NOT NULL DEFAULT 'empty_request';
ALTER TABLE requests ADD COLUMN stable_prefix_estimated_tokens INTEGER;
ALTER TABLE requests ADD COLUMN semi_stable_estimated_tokens INTEGER;
ALTER TABLE requests ADD COLUMN volatile_estimated_tokens INTEGER;
ALTER TABLE requests ADD COLUMN stable_prefix_bytes INTEGER;
ALTER TABLE requests ADD COLUMN semi_stable_bytes INTEGER;
ALTER TABLE requests ADD COLUMN volatile_bytes INTEGER;
ALTER TABLE requests ADD COLUMN segmentation_summary_json TEXT;

-- Index on segmentation_status so the stats layer can answer
-- "how many requests were segmented vs. had no segmentable
-- content?" without a full scan.  Mirrors the cache_counter_status
-- index added by migration 0040.
CREATE INDEX IF NOT EXISTS idx_requests_segmentation_status
    ON requests(segmentation_status, started_at);
