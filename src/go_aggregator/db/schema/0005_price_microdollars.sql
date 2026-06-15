-- Align price snapshots with integer microdollar arithmetic

ALTER TABLE model_price_snapshots ADD COLUMN input_per_million_microdollars INTEGER;
ALTER TABLE model_price_snapshots ADD COLUMN output_per_million_microdollars INTEGER;
ALTER TABLE model_price_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'upstream';
ALTER TABLE model_price_snapshots ADD COLUMN metadata_json TEXT;

-- Backfill from existing float prices (dollars/1K -> microdollars/million)
UPDATE model_price_snapshots SET
    input_per_million_microdollars = CAST(input_price_per_1k * 1000 AS INTEGER),
    output_per_million_microdollars = CAST(output_price_per_1k * 1000 AS INTEGER)
WHERE input_price_per_1k IS NOT NULL OR output_price_per_1k IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_price_snapshots_model_source
    ON model_price_snapshots(model_id, source);
