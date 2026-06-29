-- Operational events table for safety-net and periodic cleanup metrics.
--
-- Phase 3 of the metrics-core-api plan.  Records rows when the
-- periodic safety-net tasks (``_crash_recovery``,
-- ``_finalize_stale_requests_once``, ``reconcile_expired_reservations``,
-- etc.) touch durable state.  Each row captures what happened, how
-- many rows it touched, and any associated account so the operator
-- dashboard can distinguish a quiet safety net from one that is
-- constantly cleaning up leaks.
--
-- ``event_type`` is one of:
--   - "crash_recovery"        : startup recovery sweep
--   - "stale_request_finalizer": periodic leaked-request sweep
--   - "reservation_reconcile" : background expired-reservation sweep
--
-- ``details_json`` is a JSON object carrying per-event metadata
-- (e.g. {"released_reservations": 3, "interrupted_requests": 1}).
-- The dashboard renders the JSON as a flat key/value list.

CREATE TABLE IF NOT EXISTS operational_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_operational_events_type_occurred
    ON operational_events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_operational_events_occurred
    ON operational_events(occurred_at);