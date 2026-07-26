-- Model quarantine state persistence.
--
-- Replaces indefinite model disable with a bounded quarantine state
-- machine that requires corroboration and auto-clears on recovery.
-- Legacy ``model_unavailable`` rows in ``account_backoffs`` are
-- preserved but hydrated as ``migration_legacy`` provenance on
-- startup.

CREATE TABLE IF NOT EXISTS model_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    canonical_model_id TEXT NOT NULL,
    upstream_model_id TEXT,
    upstream_protocol TEXT NOT NULL DEFAULT 'openai',
    state TEXT NOT NULL DEFAULT 'suspected',
    evidence_provenance TEXT NOT NULL DEFAULT 'runtime_http',
    reason TEXT NOT NULL DEFAULT '',
    first_observed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_observed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    observation_count INTEGER NOT NULL DEFAULT 1,
    expiry TEXT,
    cleared_at TEXT,
    clear_reason TEXT,
    last_status_code INTEGER,
    last_error_class TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider_id, account_id, canonical_model_id, upstream_model_id, upstream_protocol)
);

CREATE INDEX IF NOT EXISTS idx_model_quarantine_state
    ON model_quarantine(state);

CREATE INDEX IF NOT EXISTS idx_model_quarantine_expiry
    ON model_quarantine(expiry);

CREATE INDEX IF NOT EXISTS idx_model_quarantine_model
    ON model_quarantine(canonical_model_id);
