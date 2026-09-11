-- Provider ping probes: records GET /models latency and health per account.
CREATE TABLE provider_pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    probed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    latency_ms INTEGER,
    status_code INTEGER,
    error TEXT,
    model_count INTEGER DEFAULT 0
);

CREATE INDEX idx_provider_pings_provider ON provider_pings(provider_id);
CREATE INDEX idx_provider_pings_probed ON provider_pings(probed_at);
CREATE INDEX idx_provider_pings_provider_probed
    ON provider_pings(provider_id, probed_at);
