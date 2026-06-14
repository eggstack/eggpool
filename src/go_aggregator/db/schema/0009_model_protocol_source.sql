-- Add per-model protocol resolution columns
ALTER TABLE models ADD COLUMN protocol_source TEXT;
ALTER TABLE models ADD COLUMN endpoint_path TEXT;
