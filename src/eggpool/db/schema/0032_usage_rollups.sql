-- Migration 0032: Add usage_rollups table for buffered analytics rollups.
--
-- The rollup table stores pre-aggregated usage counters bucketed by
-- time, provider, model, account, protocol, streaming flag, and
-- status.  The coordinator periodically flushes in-memory counters
-- into these rows so the dashboard can render timeseries charts and
-- summary statistics without scanning the full requests table.
--
-- Counter fields (request_count, error_count, retry_count, token
-- buckets, cost, bytes, latency) are designed for additive upserts:
-- ``INSERT ... ON CONFLICT DO UPDATE SET col = col + excluded.col``.
-- Latency min/max are maintained via CASE/WHEN so they converge
-- monotonically within each bucket.
--
-- Stored as TEXT for bucket_start (ISO 8601) and INTEGER for
-- bucket_size_s so the composite primary key is fully deterministic.

CREATE TABLE IF NOT EXISTS usage_rollups (
    bucket_start TEXT NOT NULL,
    bucket_size_s INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    account_id INTEGER NOT NULL DEFAULT 0,
    protocol TEXT NOT NULL,
    streamed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_characters INTEGER NOT NULL DEFAULT 0,
    cost_microdollars INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    bytes_emitted INTEGER NOT NULL DEFAULT 0,
    latency_ms_sum INTEGER NOT NULL DEFAULT 0,
    latency_ms_min INTEGER,
    latency_ms_max INTEGER,
    first_byte_ms_sum INTEGER NOT NULL DEFAULT 0,
    first_byte_ms_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        bucket_start,
        bucket_size_s,
        provider_id,
        model_id,
        account_id,
        protocol,
        streamed,
        status
    )
);

CREATE INDEX IF NOT EXISTS idx_usage_rollups_bucket ON usage_rollups(bucket_start, bucket_size_s);
CREATE INDEX IF NOT EXISTS idx_usage_rollups_provider_model ON usage_rollups(provider_id, model_id, bucket_start);
CREATE INDEX IF NOT EXISTS idx_usage_rollups_account ON usage_rollups(account_id, bucket_start);
