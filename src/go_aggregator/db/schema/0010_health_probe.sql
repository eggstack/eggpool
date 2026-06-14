-- Create health_probe table for real writeability checks
CREATE TABLE IF NOT EXISTS health_probe (
    id INTEGER PRIMARY KEY,
    probe_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
