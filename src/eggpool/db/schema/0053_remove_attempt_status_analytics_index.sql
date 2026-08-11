-- Migration 0053: remove an unused per-attempt analytics index.
--
-- The stats layer evaluates status_code inside aggregate expressions but
-- never filters request_attempts by status_code. Its time-window scans are
-- still bounded by the retained retry-category/time index (or the account
-- and provider/model indexes for filtered views), so maintaining a separate
-- status_code/time B-tree on every attempt write is not justified.

DROP INDEX IF EXISTS idx_request_attempts_status_started;
