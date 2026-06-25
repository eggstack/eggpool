-- Per-attempt observability extensions for failover analytics.
--
-- Phase 1 of the metrics-core-api plan.  Every attempt row now carries
-- the routing context (provider/model/protocol), the retry-classifier
-- verdict, and the byte counts that previously only existed at the
-- request level.  This lets the stats layer answer per-attempt
-- questions like "which provider is retrying the most?" and "what
-- category of error caused this failover?" without joining back to
-- the requests table.
--
-- ``retry_category`` mirrors ``eggpool.retry.classification.RetryCategory``
-- (never/bad_request/auth_failure/quota_exceeded/temporary/transient/
-- fatal/model_unavailable).  ``is_retry_outcome = 1`` flags attempts
-- that were finalized as "attempt_retryable" so the dashboard can
-- separate the retry distribution from the success distribution.
--
-- All columns are nullable / default 0 so existing rows from
-- pre-Phase-1 databases stay valid (rows written before this
-- migration just get NULL / 0 on the new fields).

ALTER TABLE request_attempts ADD COLUMN provider_id TEXT;
ALTER TABLE request_attempts ADD COLUMN model_id TEXT;
ALTER TABLE request_attempts ADD COLUMN protocol TEXT;
ALTER TABLE request_attempts ADD COLUMN retry_category TEXT;
ALTER TABLE request_attempts ADD COLUMN release_reason TEXT;
ALTER TABLE request_attempts ADD COLUMN bytes_received INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_attempts ADD COLUMN latency_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_attempts ADD COLUMN streamed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_attempts ADD COLUMN is_retry_outcome INTEGER NOT NULL DEFAULT 0;

-- Backfill ``model_id`` and ``provider_id`` from the parent request
-- so historical attempts are visible in per-model and per-provider
-- filters without re-running the upstream trace.
UPDATE request_attempts
SET provider_id = (
        SELECT r.provider_id FROM requests r
        WHERE r.id = request_attempts.request_id
    ),
    model_id = (
        SELECT COALESCE(r.original_model_id, r.model_id)
        FROM requests r WHERE r.id = request_attempts.request_id
    ),
    protocol = (
        SELECT r.protocol FROM requests r
        WHERE r.id = request_attempts.request_id
    )
WHERE provider_id IS NULL;

-- First-attempt timestamp on requests.  Set once when the first
-- attempt row is inserted; subsequent updates leave it alone.  Used
-- by the dashboard to compute coordinator overhead (time from
-- request open to first attempt dispatch).
ALTER TABLE requests ADD COLUMN first_attempt_at TIMESTAMP;

UPDATE requests
SET first_attempt_at = started_at
WHERE first_attempt_at IS NULL;

-- Backlink from a request to the final attempt that fulfilled it.
-- Lets the trace endpoint resolve "which attempt won this request?"
-- with a single JOIN rather than scanning attempts by attempt_number.
ALTER TABLE requests ADD COLUMN last_attempt_id INTEGER;

-- Hot-path indexes for the new per-attempt aggregates.  All scan the
-- ``request_attempts`` table by started_at with provider/model/error
-- filters; the (started_at) alone index from migration 0004 only
-- covers account_id, so we add covering indexes for the common
-- filter combinations.
CREATE INDEX IF NOT EXISTS idx_request_attempts_provider_started
    ON request_attempts(provider_id, started_at);
CREATE INDEX IF NOT EXISTS idx_request_attempts_model_started
    ON request_attempts(model_id, started_at);
CREATE INDEX IF NOT EXISTS idx_request_attempts_status_started
    ON request_attempts(status_code, started_at);
CREATE INDEX IF NOT EXISTS idx_request_attempts_retry_category_started
    ON request_attempts(retry_category, started_at);