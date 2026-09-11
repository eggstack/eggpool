-- Migration 0015: Add multi-provider support
-- Creates providers table and adds provider_id to accounts and models.

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    protocols TEXT NOT NULL DEFAULT '["openai"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE accounts ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'opencode-go';

ALTER TABLE models ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'opencode-go';

-- Backfill existing data to the default provider
UPDATE accounts SET provider_id = 'opencode-go' WHERE provider_id = 'update';
UPDATE models SET provider_id = 'opencode-go' WHERE provider_id = 'update';

-- Insert the default provider
INSERT OR IGNORE INTO providers (provider_id, base_url, protocols)
VALUES ('opencode-go', 'https://opencode.ai/zen/go/v1', '["openai", "anthropic"]');
