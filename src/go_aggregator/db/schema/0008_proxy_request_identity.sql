-- Store the proxy-generated request UUID in requests
ALTER TABLE requests ADD COLUMN proxy_request_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_proxy_request_id
    ON requests(proxy_request_id);

-- Backfill existing rows
UPDATE requests
SET proxy_request_id = 'legacy-' || id
WHERE proxy_request_id IS NULL;
