-- Request attempts table for failover observability

CREATE TABLE IF NOT EXISTS request_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    status_code INTEGER,
    error_class TEXT,
    upstream_request_id TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_request_attempts_request
    ON request_attempts(request_id);
CREATE INDEX IF NOT EXISTS idx_request_attempts_account
    ON request_attempts(account_id, started_at);
