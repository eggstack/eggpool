-- Indexes aligned with request accounting and analytics hot paths.

CREATE INDEX IF NOT EXISTS idx_requests_account_started
    ON requests(account_id, started_at);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_model_provider_captured
    ON model_price_snapshots(model_id, provider_id, captured_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_requests_streamed_provider_model_started_ttft
    ON requests(provider_id, model_id, started_at, first_byte_ms)
    WHERE streamed = 1 AND first_byte_ms IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reservations_status_expires
    ON reservations(status, expires_at);

-- These are left-prefix duplicates of existing unique/composite indexes.
DROP INDEX IF EXISTS idx_accounts_name;
DROP INDEX IF EXISTS idx_requests_status;
DROP INDEX IF EXISTS idx_request_attempts_account_id;
