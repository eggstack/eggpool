-- Migration 0034: Protocol transcoding support columns and daily aggregation.
--
-- 1. Add ``upstream_protocol`` to ``requests`` so we can identify
--    transcoded requests (where protocol != upstream_protocol).
-- 2. Pre-aggregated daily transcoding counters for fast dashboard queries
--    on large deployments.

ALTER TABLE requests ADD COLUMN upstream_protocol TEXT;

CREATE TABLE IF NOT EXISTS transcoding_daily (
    day TEXT NOT NULL,                          -- YYYY-MM-DD UTC
    client_protocol TEXT NOT NULL,
    upstream_protocol TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    loss_warning_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, client_protocol, upstream_protocol)
) WITHOUT ROWID;
