-- Correct pricing conversion from dollars/1K tokens to microdollars/1M tokens
-- Previous migration 0005 multiplied by 1000 (wrong), correct factor is 1,000,000,000

UPDATE model_price_snapshots
SET input_per_million_microdollars =
        CAST(input_price_per_1k * 1000000000 AS INTEGER)
WHERE input_price_per_1k IS NOT NULL;

UPDATE model_price_snapshots
SET output_per_million_microdollars =
        CAST(output_price_per_1k * 1000000000 AS INTEGER)
WHERE output_price_per_1k IS NOT NULL;
