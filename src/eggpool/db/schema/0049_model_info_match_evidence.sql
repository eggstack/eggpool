-- Migration 0049: model identity normalization — match evidence and
-- discovery provenance.
--
-- Extends model_info_aliases with columns that record how each
-- alias was matched and who discovered it, and adds a companion
-- match_evidence table for full audit trails of resolution results.

ALTER TABLE model_info_aliases
    ADD COLUMN match_method TEXT;

ALTER TABLE model_info_aliases
    ADD COLUMN discovered_by TEXT;

ALTER TABLE model_info_aliases
    ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS model_info_match_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    source TEXT NOT NULL,
    alias TEXT NOT NULL,
    match_method TEXT NOT NULL,
    confidence REAL NOT NULL,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_info_match_evidence_model
    ON model_info_match_evidence(model_id, source);

CREATE INDEX IF NOT EXISTS idx_model_info_match_evidence_source
    ON model_info_match_evidence(source, last_seen_at DESC);
