-- Add provider_id to model_price_snapshots so pricing is keyed by
-- (model_id, provider_id) and two providers sharing the same model
-- cannot overwrite each other's price data.
ALTER TABLE model_price_snapshots ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'opencode-go';
