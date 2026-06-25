-- Migration: 0029_latency_phases
--
-- Phase 4 of the metrics-core-api plan: break latency down into phases so the
-- dashboard can show whether slowness is network connect time, upstream
-- stream read time, or eggpool's own coordinator overhead.
--
-- The existing ``first_byte_ms`` column captures total time-to-first-byte
-- end-to-end.  The new columns are additive sub-phases:
--
--   * upstream_connect_ms   — time spent in ``client.aclose`` / DNS / TCP / TLS
--                             handshake before the upstream request was even
--                             sent.  Measured around ``async with client.stream(...)``.
--   * upstream_read_ms      — time from "request sent" to "first byte read"
--                             (overlaps with first_byte_ms but counts only the
--                             upstream portion, not the eggpool-side decode).
--   * coordinator_overhead_ms — total request elapsed minus (connect + read +
--                              body decode).  Captures eligibility scoring,
--                              reservation math, retry logic, JSON encoding,
--                              and FastAPI plumbing.
--
-- All three are nullable — older rows leave them NULL and the dashboard
-- simply omits the breakdown for those windows.
ALTER TABLE requests ADD COLUMN upstream_connect_ms INTEGER;
ALTER TABLE requests ADD COLUMN upstream_read_ms INTEGER;
ALTER TABLE requests ADD COLUMN coordinator_overhead_ms INTEGER;

CREATE INDEX IF NOT EXISTS idx_requests_ttfb_ms
    ON requests(first_byte_ms)
    WHERE first_byte_ms IS NOT NULL;