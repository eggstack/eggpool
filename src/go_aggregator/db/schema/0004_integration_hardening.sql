-- Integration hardening: align schema with canonical request lifecycle

-- Extend requests with lifecycle fields
ALTER TABLE requests ADD COLUMN protocol TEXT NOT NULL DEFAULT 'openai';
ALTER TABLE requests ADD COLUMN streamed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN exactness TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE requests ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_write_tokens INTEGER;
ALTER TABLE requests ADD COLUMN reasoning_tokens INTEGER;
ALTER TABLE requests ADD COLUMN thinking_characters INTEGER;
ALTER TABLE requests ADD COLUMN reserved_microdollars INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN first_byte_ms INTEGER;
ALTER TABLE requests ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN upstream_request_id TEXT;
ALTER TABLE requests ADD COLUMN error_class TEXT;
ALTER TABLE requests ADD COLUMN error_detail TEXT;
ALTER TABLE requests ADD COLUMN status_code INTEGER;

-- Extend reservations with lifecycle fields
ALTER TABLE reservations ADD COLUMN estimated_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reservations ADD COLUMN estimated_microdollars INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reservations ADD COLUMN expires_at TIMESTAMP;
ALTER TABLE reservations ADD COLUMN release_reason TEXT;

-- Extend request_attempts with bytes_emitted
ALTER TABLE request_attempts ADD COLUMN bytes_emitted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_attempts ADD COLUMN error_detail TEXT;

-- Indexes for new query patterns
CREATE INDEX IF NOT EXISTS idx_requests_exactness ON requests(exactness);
CREATE INDEX IF NOT EXISTS idx_requests_completed ON requests(completed_at);
CREATE INDEX IF NOT EXISTS idx_requests_protocol ON requests(protocol);
CREATE INDEX IF NOT EXISTS idx_reservations_expires ON reservations(expires_at);
CREATE INDEX IF NOT EXISTS idx_requests_status_started ON requests(status, started_at);
