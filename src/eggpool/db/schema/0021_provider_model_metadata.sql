-- Preserve provider-specific catalog metadata for shared model IDs.

CREATE TABLE provider_model_metadata (
    model_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    display_name TEXT,
    protocol TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',
    source_metadata TEXT NOT NULL DEFAULT '{}',
    protocol_source TEXT,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolution_status TEXT NOT NULL DEFAULT 'resolved',
    PRIMARY KEY (model_id, provider_id),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);

CREATE INDEX idx_provider_model_metadata_provider
    ON provider_model_metadata(provider_id, model_id);
