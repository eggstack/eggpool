-- Add a single-column index on request_attempts(account_id) so
-- queries that filter only by account (e.g. get_active_for_account
-- style lookups) do not need to traverse the existing
-- (account_id, started_at) composite index. The composite index
-- remains in place for time-bounded queries.
CREATE INDEX IF NOT EXISTS idx_request_attempts_account_id
    ON request_attempts(account_id);
