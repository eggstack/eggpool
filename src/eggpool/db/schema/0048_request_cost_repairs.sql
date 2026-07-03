-- Migration 0048: add an audit table for historical request-cost repairs.

CREATE TABLE IF NOT EXISTS request_cost_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL
        REFERENCES requests(id) ON DELETE CASCADE,
    old_cost_microdollars INTEGER NOT NULL,
    new_cost_microdollars INTEGER NOT NULL,
    old_exactness TEXT,
    new_exactness TEXT NOT NULL,
    reason TEXT NOT NULL,
    provider_filter TEXT,
    since_date TEXT,
    repaired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_request_cost_repairs_request
    ON request_cost_repairs(request_id, repaired_at DESC);

CREATE INDEX IF NOT EXISTS idx_request_cost_repairs_repaired_at
    ON request_cost_repairs(repaired_at DESC);
