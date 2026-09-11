-- Migration 0019: Add client_ip column to requests table
-- Tracks the IP address of the client making each request for LAN device stats.

ALTER TABLE requests ADD COLUMN client_ip TEXT;
