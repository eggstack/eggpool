-- Historical schema fixture: state immediately after applying migrations 1-11.
--
-- This fixture represents the production GoRouter schema as it would look
-- after the v1 through v11 migrations have all been applied. The SQL
-- reproduces the v11 state without depending on the migration runner
-- itself, so upgrade-compatibility tests can apply it directly to a
-- file-backed SQLite database, reopen the file through ``Database``,
-- and verify that running the current migration set on top is a no-op.
--
-- All rows use synthetic, non-secret values. Do not edit without also
-- updating tests/fixtures/schema/checksums.json.

-- ----- Schema (rebuilt from migrations 1-11) -----

-- _migrations: bookkeeping table.
CREATE TABLE _migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- accounts (migration 0001)
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    api_key_env TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- models (migration 0001, extended by 0009 and 0011)
CREATE TABLE models (
    model_id TEXT PRIMARY KEY,
    display_name TEXT,
    protocol TEXT NOT NULL DEFAULT 'openai',
    capabilities TEXT NOT NULL DEFAULT '{}',
    source_metadata TEXT NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    protocol_source TEXT,
    endpoint_path TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'resolved'
);

-- account_models (migration 0001)
CREATE TABLE account_models (
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, model_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);

-- requests (migration 0001, extended by 0004 and 0008)
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_microdollars INTEGER DEFAULT 0,
    upstream_latency_ms REAL DEFAULT 0,
    error_message TEXT,
    protocol TEXT NOT NULL DEFAULT 'openai',
    streamed INTEGER NOT NULL DEFAULT 0,
    exactness TEXT NOT NULL DEFAULT 'unknown',
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    thinking_characters INTEGER,
    reserved_microdollars INTEGER NOT NULL DEFAULT 0,
    first_byte_ms INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    upstream_request_id TEXT,
    error_class TEXT,
    error_detail TEXT,
    status_code INTEGER,
    proxy_request_id TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);

-- reservations (migration 0001, extended by 0004)
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    reserved_microdollars INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'active',
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_microdollars INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMP,
    release_reason TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);

-- model_price_snapshots (migration 0001, extended by 0005 and 0007)
CREATE TABLE model_price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    input_price_per_1k REAL,
    output_price_per_1k REAL,
    captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    input_per_million_microdollars INTEGER,
    output_per_million_microdollars INTEGER,
    source TEXT NOT NULL DEFAULT 'config',
    metadata_json TEXT,
    cache_read_per_million_microdollars INTEGER,
    cache_write_per_million_microdollars INTEGER,
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);

-- account_events (migration 0001)
CREATE TABLE account_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

-- request_attempts (migration 0003, extended by 0004)
CREATE TABLE request_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    status_code INTEGER,
    error_class TEXT,
    upstream_request_id TEXT,
    bytes_emitted INTEGER NOT NULL DEFAULT 0,
    error_detail TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

-- health_probe (migration 0010)
CREATE TABLE health_probe (
    id INTEGER PRIMARY KEY,
    probe_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ----- Indexes (migrations 0002 and 0004) -----

CREATE INDEX idx_accounts_name ON accounts(name);
CREATE INDEX idx_accounts_enabled ON accounts(enabled);

CREATE INDEX idx_account_models_account ON account_models(account_id);
CREATE INDEX idx_account_models_model ON account_models(model_id);

CREATE INDEX idx_requests_account ON requests(account_id);
CREATE INDEX idx_requests_model ON requests(model_id);
CREATE INDEX idx_requests_started ON requests(started_at);
CREATE INDEX idx_requests_status ON requests(status);

CREATE INDEX idx_reservations_account ON reservations(account_id);
CREATE INDEX idx_reservations_model ON reservations(model_id);
CREATE INDEX idx_reservations_status ON reservations(status);
CREATE INDEX idx_reservations_request ON reservations(request_id);

CREATE INDEX idx_model_price_snapshots_model ON model_price_snapshots(model_id);
CREATE INDEX idx_model_price_snapshots_captured ON model_price_snapshots(captured_at);

CREATE INDEX idx_account_events_account ON account_events(account_id);
CREATE INDEX idx_account_events_type ON account_events(event_type);
CREATE INDEX idx_account_events_created ON account_events(created_at);

CREATE INDEX idx_requests_exactness ON requests(exactness);
CREATE INDEX idx_requests_completed ON requests(completed_at);
CREATE INDEX idx_requests_protocol ON requests(protocol);
CREATE INDEX idx_reservations_expires ON reservations(expires_at);
CREATE INDEX idx_requests_status_started ON requests(status, started_at);

CREATE INDEX idx_request_attempts_request ON request_attempts(request_id);
CREATE INDEX idx_request_attempts_account
    ON request_attempts(account_id, started_at);

CREATE INDEX idx_price_snapshots_model_source
    ON model_price_snapshots(model_id, source);

CREATE UNIQUE INDEX idx_requests_proxy_request_id
    ON requests(proxy_request_id);

-- ----- Migration bookkeeping (versions 1-11) -----

INSERT INTO _migrations (version, name, applied_at) VALUES
    (1,  '0001_initial',                    '2024-01-01 00:00:00'),
    (2,  '0002_indexes',                    '2024-01-01 00:00:01'),
    (3,  '0003_request_attempts',           '2024-01-01 00:00:02'),
    (4,  '0004_integration_hardening',      '2024-01-01 00:00:03'),
    (5,  '0005_price_microdollars',         '2024-01-01 00:00:04'),
    (6,  '0006_correct_price_microdollars', '2024-01-01 00:00:05'),
    (7,  '0007_price_cache_rates',          '2024-01-01 00:00:06'),
    (8,  '0008_proxy_request_identity',     '2024-01-01 00:00:07'),
    (9,  '0009_model_protocol_source',      '2024-01-01 00:00:08'),
    (10, '0010_health_probe',               '2024-01-01 00:00:09'),
    (11, '0011_model_resolution_status',    '2024-01-01 00:00:10');

-- ----- Representative rows (synthetic, non-secret) -----

-- accounts: one representative configured account.
INSERT INTO accounts (id, name, api_key_env, enabled, weight) VALUES
    (1, 'historical-account', 'GOROUTER_TEST_KEY_1', 1, 1.0);

-- models: a resolved model with full protocol metadata from v11.
INSERT INTO models (
    model_id, display_name, protocol, capabilities, source_metadata,
    protocol_source, endpoint_path, resolution_status
) VALUES
    ('historical-model', 'Historical Model', 'openai', '{}', '{}',
     'config', '/v1/chat/completions', 'resolved');

-- account_models: the relationship between the two.
INSERT INTO account_models (account_id, model_id, enabled) VALUES
    (1, 'historical-model', 1);

-- model_price_snapshots: a representative snapshot, with the
-- historically correct 'config' default for the source column.
INSERT INTO model_price_snapshots (
    model_id, input_price_per_1k, output_price_per_1k,
    input_per_million_microdollars, output_per_million_microdollars,
    source, metadata_json
) VALUES
    ('historical-model', 0.00001, 0.00002, 10000, 20000, 'config', '{}');

-- requests: a representative completed request.
INSERT INTO requests (
    account_id, model_id, started_at, completed_at, status,
    input_tokens, output_tokens, cost_microdollars,
    protocol, streamed, exactness, proxy_request_id
) VALUES
    (1, 'historical-model', '2024-01-01 00:01:00', '2024-01-01 00:01:05', 'success',
     100, 50, 1500,
     'openai', 0, 'exact', 'legacy-historical-1');

-- request_attempts: a representative completed attempt.
INSERT INTO request_attempts (
    request_id, attempt_number, account_id,
    started_at, completed_at, status_code
) VALUES
    (1, 1, 1, '2024-01-01 00:01:00', '2024-01-01 00:01:05', 200);

-- reservations: a representative released reservation.
INSERT INTO reservations (
    request_id, account_id, model_id,
    reserved_microdollars, estimated_tokens, estimated_microdollars,
    status, released_at, release_reason
) VALUES
    (1, 1, 'historical-model', 1500, 200, 1500,
     'released', '2024-01-01 00:01:06', 'completed');
