-- Migration 0037: Add failure_count to model_info_source_health.
--
-- Tracks consecutive failures for exponential backoff calculation.
-- Reset to 0 on success, incremented on each error.

ALTER TABLE model_info_source_health
    ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
