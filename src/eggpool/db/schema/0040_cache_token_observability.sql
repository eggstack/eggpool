-- Migration 0040: Cache and token observability (Phase 1).
--
-- Phase 1 of the cache-preserving deterministic compression plan
-- (plans/cache_compression_phase_01_cache_token_observability.md)
-- introduces a provider-neutral usage model.  Existing columns
-- (cache_read_tokens, cache_write_tokens, reasoning_tokens) are
-- already populated by the streaming/non-streaming extractors and
-- are kept as the canonical storage shape.  This migration adds
-- nullable columns that capture the new observability signals:
--
--   * The cache_counter_status (TEXT) records whether the upstream
--     reported cache counters (reported), parsed cleanly with no
--     cache fields (not_reported), or returned a shape EggPool could
--     not parse (unknown_format).  Default 'not_reported' so legacy
--     rows render correctly without a backfill.
--   * cached_input_tokens is the canonical "any cached input" figure
--     used by the dashboard when only OpenAI-shape counters are
--     available.  Distinct from cache_read_tokens because Anthropic
--     also splits cache creation.
--   * cache_read_input_tokens / cache_creation_input_tokens are the
--     Anthropic-shape split.  They mirror cache_read_tokens and
--     cache_write_tokens, which remain the historical storage shape
--     used by cost calculation.
--   * request_shape_hash and stable_prefix_hash are nullable
--     placeholders for Phase 2/3 (canonical request segmentation and
--     transcoder cache stability) and Phase 4 (observe-mode
--     compression accounting).  Adding them now means future phases
--     do not require additional migrations.
--   * transcoded is a boolean (0/1) recording whether the request
--     was transcoded between protocols.  The coordinator already
--     tracks this in process but does not persist it; Phase 1
--     observability surfaces "which path produced these counters"
--     explicitly.
--
-- All columns are nullable / default 0 so existing databases migrate
-- cleanly without a destructive backfill.  Rollback notes in
-- plans/cache_compression_phase_01_cache_token_observability.md
-- describe the disable-dashboard path.

ALTER TABLE requests ADD COLUMN cache_counter_status TEXT NOT NULL DEFAULT 'not_reported';
ALTER TABLE requests ADD COLUMN cached_input_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_read_input_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_creation_input_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_write_input_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_write_input_reported INTEGER;
ALTER TABLE requests ADD COLUMN input_tokens_reported INTEGER;
ALTER TABLE requests ADD COLUMN output_tokens_reported INTEGER;
ALTER TABLE requests ADD COLUMN total_tokens_reported INTEGER;
ALTER TABLE requests ADD COLUMN request_shape_hash TEXT;
ALTER TABLE requests ADD COLUMN stable_prefix_hash TEXT;
ALTER TABLE requests ADD COLUMN transcoded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN raw_usage_json TEXT;

-- Index on cache_counter_status so the stats layer can answer
-- "how many requests reported cache counters?" without a full scan.
CREATE INDEX IF NOT EXISTS idx_requests_cache_counter_status
    ON requests(cache_counter_status, started_at);

-- Index on transcoded so the dashboard can filter "non-transcoded
-- Anthropic-shape requests" vs "transcoded OpenAI-shape requests"
-- when comparing cache coverage across protocols.
CREATE INDEX IF NOT EXISTS idx_requests_transcoded
    ON requests(transcoded, started_at);