-- Migration 0050: Index for bounded routing_decisions retention cleanup.
--
-- The retention cleanup task uses ``ORDER BY decision_made_at, id
-- LIMIT ?`` to select batches of old rows for deletion.  The existing
-- composite indexes on ``(model_id, decision_made_at)`` and
-- ``(provider_id, decision_made_at)`` do not cover this query well
-- because they sort by a leading non-timestamp column first.  A
-- dedicated ``(decision_made_at, id)`` index lets SQLite range-scan
-- directly to the oldest rows without a filesort.

CREATE INDEX IF NOT EXISTS idx_routing_decisions_retention
    ON routing_decisions(decision_made_at, id);
