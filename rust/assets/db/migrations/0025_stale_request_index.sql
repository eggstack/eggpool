-- Stale-request index for the periodic finalizer.
--
-- The crash_recovery, reconcile_expired_reservations, and the new
-- stale-request finalizer all scan requests by status + started_at.
-- Without an index this is a full table scan; with many leaked rows
-- the scan holds the connection lock long enough to starve new
-- requests.
--
-- The ``idx_requests_status_started`` index was added by migration 0004
-- for the same reason.  This migration is intentionally idempotent
-- (``CREATE INDEX IF NOT EXISTS``) so the safety-net background task
-- introduced for the 503 leak fix has a documented, versioned schema
-- anchor.  Re-running the statement on a database that already has the
-- index is a no-op; on a fresh database the 0004 migration has already
-- created it before this file runs.

CREATE INDEX IF NOT EXISTS idx_requests_status_started
    ON requests(status, started_at);