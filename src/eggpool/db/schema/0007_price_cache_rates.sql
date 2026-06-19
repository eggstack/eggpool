-- Add cache pricing support
ALTER TABLE model_price_snapshots
    ADD COLUMN cache_read_per_million_microdollars INTEGER;
ALTER TABLE model_price_snapshots
    ADD COLUMN cache_write_per_million_microdollars INTEGER;
