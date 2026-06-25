-- Deterministic alias registry for mapping provider-native model IDs
-- to external catalog model IDs (e.g. OpenRouter, OpenCode Zen).
--
-- This is the single source of truth for non-fuzzy catalog lookups:
-- the resolver never falls back to substring or edit-distance matching
-- when multiple candidates could fit (e.g. MiMo 2.5 vs MiMo 2.5 Pro).
-- Each row says: for this (provider_id, upstream_model_id) pair, the
-- catalog_source catalog should be consulted at catalog_model_id with
-- the noted confidence.
--
-- Confidence values:
--   exact            — provider-native ID equals catalog ID exactly
--   curated_alias    — operator-maintained manual mapping
--   ambiguous_skip   — present for diagnostic visibility only; resolver
--                      never uses this row to fetch a price
CREATE TABLE IF NOT EXISTS model_pricing_aliases (
    provider_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    catalog_source TEXT NOT NULL,
    catalog_model_id TEXT NOT NULL,
    confidence TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, upstream_model_id, catalog_source)
);

CREATE INDEX IF NOT EXISTS idx_pricing_aliases_catalog
    ON model_pricing_aliases(catalog_source, catalog_model_id);

CREATE INDEX IF NOT EXISTS idx_pricing_aliases_provider
    ON model_pricing_aliases(provider_id, upstream_model_id);