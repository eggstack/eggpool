-- Migration 0038: Model info Phase 5 — source health hardening,
-- alias expansion, and manual overrides.
--
-- Adds columns to source_health for richer tracking, adds a
-- notes column to aliases, and creates the overrides table.

ALTER TABLE model_info_source_health
    ADD COLUMN last_status_code INTEGER;

ALTER TABLE model_info_source_health
    ADD COLUMN rate_limited_until TIMESTAMP;

ALTER TABLE model_info_source_health
    ADD COLUMN last_success_duration_ms INTEGER;

ALTER TABLE model_info_source_health
    ADD COLUMN last_payload_count INTEGER;

ALTER TABLE model_info_aliases
    ADD COLUMN notes TEXT;

CREATE TABLE IF NOT EXISTS model_info_overrides (
    model_id TEXT PRIMARY KEY,
    summary TEXT,
    family TEXT,
    display_name TEXT,
    notes TEXT,
    hide_benchmark_sources INTEGER NOT NULL DEFAULT 0,
    status_override TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
