-- Persistent record of upstream-observed backoffs.
--
-- Survives restarts so that a real upstream 429/402/5xx sequence
-- continues to suppress the offending account (or account/model pair)
-- after the process is killed. Local-estimate quota overage MUST NOT
-- be persisted here; only provider-observed failures drive these
-- rows.
--
-- ``model_id`` NULL means account-wide suppression; a non-NULL value
-- scopes the backoff to a specific (account, model) pair so the same
-- account can still serve other models.

CREATE TABLE IF NOT EXISTS account_backoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    model_id TEXT,
    reason TEXT NOT NULL,
    status_code INTEGER,
    error_class TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 1,
    backoff_until TEXT,
    last_failure_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, model_id, reason)
);

CREATE INDEX IF NOT EXISTS idx_account_backoffs_active
    ON account_backoffs(backoff_until);

CREATE INDEX IF NOT EXISTS idx_account_backoffs_account_model
    ON account_backoffs(account_id, model_id);