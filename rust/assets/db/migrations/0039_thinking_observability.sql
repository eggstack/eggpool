-- Migration 0039: Thinking/reasoning observability.
--
-- Adds a thinking_trace_json column to the requests table to store
-- per-request thinking decision metadata for diagnostics.

ALTER TABLE requests ADD COLUMN thinking_trace_json TEXT;
