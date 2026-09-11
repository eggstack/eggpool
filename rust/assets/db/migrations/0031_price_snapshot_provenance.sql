-- Migration 0031: Add provenance detail columns to model_price_snapshots.
--
-- The pricing resolution pipeline now records a granular provenance
-- label alongside the existing top-level source bucket. These columns
-- let the dashboard surface *which* field (input / output / cache_read
-- / cache_write) came from which upstream/catalog, and the confidence
-- of the match (exact upstream metadata vs. curated alias vs. open
-- candidate).
--
-- Stored as TEXT (nullable) so older snapshots remain valid: rows from
-- migration 0030 and earlier simply have NULL detail/confidence and
-- are treated as the legacy ``source`` column only.

ALTER TABLE model_price_snapshots
    ADD COLUMN source_detail TEXT;

ALTER TABLE model_price_snapshots
    ADD COLUMN source_confidence TEXT;

ALTER TABLE model_price_snapshots
    ADD COLUMN catalog_source TEXT;