-- Migration 0043: Safe-suffix compression fields (Phase 5).
--
-- Phase 5 of the cache-preserving deterministic compression roadmap
-- adds the first request-mutating deterministic compressor.  Given
-- the :class:`SegmentationResult` produced by Phase 2, the compressor
-- walks volatile-suffix segments, identifies eligible compressible
-- candidates (matching the analyzer's eligibility rules), applies
-- deterministic transforms in-place on a deep-copied payload, and
-- returns a :class:`CompressionResult` describing the outcome.
--
-- These columns persist the Phase 5 applier's per-request roll-up
-- alongside the Phase 4 observe-mode columns.  Both phases share
-- the ``compression_status`` column to distinguish disabled /
-- observed / applied outcomes.
--
--   * compression_applied records whether the applier actually
--     mutated the request body (1) or left it unchanged (0).
--   * compression_transform_count is the number of segments that
--     were successfully transformed.
--   * compression_transforms_by_reason_json is a compact JSON map
--     of reason codes to counts (e.g. ``{"repeated_line_run": 2,
--     "log_compaction": 1}``).
--   * compression_original_tokens / compression_compressed_tokens /
--     compression_savings_tokens are the token totals for segments
--     that were actually transformed (nullable; None when no
--     transforms occurred).
--   * compression_pre_stable_prefix_hash / compression_post_stable_prefix_hash
--     are SHA-256 hex digests of the stable-prefix segments before
--     and after compression.  The fail-closed guard uses these to
--     detect unexpected prefix drift.
--   * compression_stable_prefix_preserved is 1 when the two hashes
--     match and 0 when they diverge (always 1 under the safe-mode
--     fail-closed policy).
--   * compression_warnings_json is a JSON array of warning strings
--     emitted during compression (e.g. latency budget exceeded).
--   * compression_latency_ms is the wall-clock duration the
--     applier spent on this request.
--   * compression_failed_fallback is 1 when the fail-closed guard
--     triggered and the original payload was returned unchanged.
--   * compression_applied_summary_json is the full per-request
--     JSON summary produced by :func:`result_to_summary`.

ALTER TABLE requests ADD COLUMN compression_applied INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_transform_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_transforms_by_reason_json TEXT;
ALTER TABLE requests ADD COLUMN compression_original_tokens INTEGER;
ALTER TABLE requests ADD COLUMN compression_compressed_tokens INTEGER;
ALTER TABLE requests ADD COLUMN compression_savings_tokens INTEGER;
ALTER TABLE requests ADD COLUMN compression_pre_stable_prefix_hash TEXT;
ALTER TABLE requests ADD COLUMN compression_post_stable_prefix_hash TEXT;
ALTER TABLE requests ADD COLUMN compression_stable_prefix_preserved INTEGER NOT NULL DEFAULT 1;
ALTER TABLE requests ADD COLUMN compression_warnings_json TEXT;
ALTER TABLE requests ADD COLUMN compression_latency_ms REAL NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_failed_fallback INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_applied_summary_json TEXT;

-- Index on compression_applied for fast filtering of "requests that
-- actually received compression" in the dashboard stats layer.
CREATE INDEX IF NOT EXISTS idx_requests_compression_applied
    ON requests(compression_applied, started_at);
