-- Migration 0016: Add provider_id to requests table
ALTER TABLE requests ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'opencode-go';
