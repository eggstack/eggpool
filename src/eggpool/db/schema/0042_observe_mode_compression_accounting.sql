-- Migration 0042: Observe-mode compression accounting (Phase 4).
--
-- Phase 4 of the cache-preserving deterministic compression plan
-- (plans/cache_compression_phase_04_observe_mode_compression_accounting.md)
-- adds a side-effect-free compression analyzer that runs over the
-- canonical request segments produced by Phase 2's segmenter.  The
-- analyzer is observational: it never mutates the request body,
-- never changes routing, and never synthesises provider cache
-- controls.  This migration adds the per-request storage columns
-- for the analyzer's roll-up; later phases (safe / balanced
-- compression) will read these rows without a new schema.
--
--   * compression_status records whether the analyzer ran.  The
--     safe default is 'disabled' so pre-Phase-4 rows and operators
--     who leave ``[compression] enabled = false`` render correctly
--     without a backfill.  'observed' is set by the analyzer in
--     observe mode; future phases may add 'safe' and 'balanced'.
--   * compression_mode mirrors the policy mode that produced the
--     observation; always 'observe' in Phase 4.
--   * compression_candidate_count /
--     compression_eligible_candidate_count /
--     compression_suppressed_candidate_count are integer tallies
--     for fast aggregation.  The split between eligible and
--     suppressed makes the cache-boundary suppression visible
--     separately from real opportunities.
--   * compression_estimated_original_tokens /
--     compression_estimated_compressed_tokens /
--     compression_estimated_savings_tokens are the eligible
--     candidate token totals.  All three are nullable so the
--     analyzer can record "no eligible candidates" without
--     faking a zero.
--   * compression_analyzer_latency_ms is the wall-clock duration
--     the analyzer spent on this request.  Used by the dashboard
--     to monitor overhead and by the latency-budget guard to
--     stop cleanly when the budget is exceeded.
--   * compression_warning_count is a small integer so the
--     dashboard can flag "this request had a latency budget
--     warning" without parsing the JSON.
--   * compression_reason_code_counts_json is a compact JSON map
--     of reason codes (e.g. ``{"repeated_line_run": 3,
--     "protected_cache_boundary": 1}``) so the top reason codes
--     are queryable without scanning the full summary.
--   * compression_summary_json is the full per-request roll-up
--     including the candidate breakdown, transform counts, and
--     warning list.  Raw request content is never persisted
--     here.
--
-- All numeric fields default to 0; all token / JSON / text fields
-- are nullable.  Rollback notes in
-- plans/cache_compression_phase_04_observe_mode_compression_accounting.md
-- describe the disable-policy path: setting ``[compression]
-- enabled = false`` short-circuits the analyzer so the new
-- columns stay at their defaults.

ALTER TABLE requests ADD COLUMN compression_status TEXT NOT NULL DEFAULT 'disabled';
ALTER TABLE requests ADD COLUMN compression_mode TEXT;
ALTER TABLE requests ADD COLUMN compression_candidate_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_eligible_candidate_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_suppressed_candidate_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_estimated_original_tokens INTEGER;
ALTER TABLE requests ADD COLUMN compression_estimated_compressed_tokens INTEGER;
ALTER TABLE requests ADD COLUMN compression_estimated_savings_tokens INTEGER;
ALTER TABLE requests ADD COLUMN compression_analyzer_latency_ms REAL;
ALTER TABLE requests ADD COLUMN compression_warning_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN compression_reason_code_counts_json TEXT;
ALTER TABLE requests ADD COLUMN compression_summary_json TEXT;

-- Index on compression_status so the stats layer can answer
-- "how many requests were observed vs. had compression disabled?"
-- without a full scan.  Mirrors the segmentation_status index
-- added by migration 0041.
CREATE INDEX IF NOT EXISTS idx_requests_compression_status
    ON requests(compression_status, started_at);

-- Index on compression_mode for breakdowns by analyzer mode.  In
-- Phase 4 every observed row carries 'observe' so the index is
-- degenerate today; future phases will use it to filter by mode.
CREATE INDEX IF NOT EXISTS idx_requests_compression_mode
    ON requests(compression_mode, started_at);
