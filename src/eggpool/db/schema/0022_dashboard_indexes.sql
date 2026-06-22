-- Indexes for dashboard hot paths.
--
-- The overview page groups `requests` by `client_ip` for the per-IP
-- panel and sorts streamed rows by `first_byte_ms` for TTFT
-- percentiles. Both queries used to full-scan the table because no
-- supporting index existed; the indexes below let SQLite range-scan
-- and sort in memory over a much smaller row set.

CREATE INDEX IF NOT EXISTS idx_requests_client_ip_started
    ON requests(client_ip, started_at);

CREATE INDEX IF NOT EXISTS idx_requests_streamed_started_ttft
    ON requests(started_at, first_byte_ms)
    WHERE streamed = 1 AND first_byte_ms IS NOT NULL;
