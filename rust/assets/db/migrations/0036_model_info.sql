-- Migration 0036: Model information sidecar tables.
--
-- Phase 1 foundation for persistent model metadata.  These tables
-- store provider-native observations, canonical summaries, aliases,
-- and source-health tracking alongside the existing catalog without
-- altering routing-critical tables.  All FKs cascade on model delete
-- so withdrawn models clean up automatically.

CREATE TABLE IF NOT EXISTS model_info_canonical (
    model_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    summary TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    conflicts_json TEXT NOT NULL DEFAULT '{}',
    sparse INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_refreshed_at TIMESTAMP,
    next_refresh_at TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_info_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT,
    provider_id TEXT,
    source TEXT NOT NULL,
    source_model_id TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    confidence REAL NOT NULL DEFAULT 0.5,
    raw_hash TEXT NOT NULL,
    normalized_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE,
    UNIQUE(source, source_model_id, raw_hash)
);

CREATE TABLE IF NOT EXISTS model_info_aliases (
    model_id TEXT NOT NULL,
    provider_id TEXT,
    alias TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, alias, source),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_info_source_health (
    source TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_success_at TIMESTAMP,
    last_error_at TIMESTAMP,
    last_error_class TEXT,
    last_error_message TEXT,
    cooldown_until TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_model_info_canonical_status
    ON model_info_canonical(status);

CREATE INDEX IF NOT EXISTS idx_model_info_canonical_next_refresh
    ON model_info_canonical(next_refresh_at);

CREATE INDEX IF NOT EXISTS idx_model_info_observations_model_source
    ON model_info_observations(model_id, source);

CREATE INDEX IF NOT EXISTS idx_model_info_observations_source_model
    ON model_info_observations(source, source_model_id);

CREATE INDEX IF NOT EXISTS idx_model_info_aliases_alias
    ON model_info_aliases(alias);
