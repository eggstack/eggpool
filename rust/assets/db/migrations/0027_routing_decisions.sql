-- Routing decision observability.
--
-- Phase 2 of the metrics-core-api plan.  Each row captures one
-- routing decision made by the coordinator: which account was
-- chosen, which were considered, which were excluded, and what
-- scoring tier the chosen account sat in.  Persisted inside the
-- same transaction as the request_attempts INSERT so the trace and
-- the attempt can never disagree.
--
-- ``exclude_reasons_json`` is a JSON array of objects describing
-- accounts that were considered but excluded by the circuit breaker
-- or quota cap (e.g. ``{"account":"a","reason":"circuit_open"}``).
-- ``selected_score`` and ``top_score`` are stored as REAL so the
-- dashboard can chart the score gap that drove failover without
-- recomputing it.
--
-- The choice to keep ``selected_score`` nullable rather than
-- defaulting to 0 lets the dashboard distinguish "no scoring data
-- available" from "score was zero" (the former means the account
-- was the only eligible candidate; the latter means it scored
-- literally zero against its peers).

CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    protocol TEXT,
    selected_account_id INTEGER,
    selected_account_name TEXT,
    selected_tier INTEGER,
    selected_score REAL,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    scored_count INTEGER NOT NULL DEFAULT 0,
    attempted_excluded_count INTEGER NOT NULL DEFAULT 0,
    top_score REAL,
    top_score_account_name TEXT,
    exclude_reasons_json TEXT NOT NULL DEFAULT '[]',
    decision_made_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY (selected_account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_routing_decisions_request
    ON routing_decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_model_started
    ON routing_decisions(model_id, decision_made_at);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_provider_started
    ON routing_decisions(provider_id, decision_made_at);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_selected_account
    ON routing_decisions(selected_account_name, decision_made_at);