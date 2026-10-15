CREATE TABLE user_budget_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id TEXT,
    window TEXT NOT NULL CHECK (window IN ('day', 'month')),
    max_requests INTEGER CHECK (max_requests IS NULL OR max_requests > 0),
    max_tokens INTEGER CHECK (max_tokens IS NULL OR max_tokens > 0),
    max_microdollars INTEGER CHECK (max_microdollars IS NULL OR max_microdollars > 0),
    max_concurrent INTEGER CHECK (max_concurrent IS NULL OR max_concurrent > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (max_requests IS NOT NULL OR max_tokens IS NOT NULL OR
           max_microdollars IS NOT NULL OR max_concurrent IS NOT NULL)
);

CREATE UNIQUE INDEX idx_user_budget_rules_scope
    ON user_budget_rules(user_id, ifnull(model_id, ''), window);
CREATE INDEX idx_user_budget_rules_user ON user_budget_rules(user_id, window);

CREATE TABLE user_budget_reservations (
    request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    model_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL CHECK (policy_version > 0),
    day_start TEXT NOT NULL,
    month_start TEXT NOT NULL,
    held_requests INTEGER NOT NULL DEFAULT 1 CHECK (held_requests = 1),
    held_tokens INTEGER NOT NULL CHECK (held_tokens >= 0),
    held_microdollars INTEGER NOT NULL CHECK (held_microdollars >= 0),
    observed_tokens INTEGER NOT NULL DEFAULT 0 CHECK (observed_tokens >= 0),
    observed_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (observed_microdollars >= 0),
    token_usage_complete INTEGER NOT NULL DEFAULT 0 CHECK (token_usage_complete IN (0, 1)),
    cost_usage_complete INTEGER NOT NULL DEFAULT 0 CHECK (cost_usage_complete IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('active', 'settled', 'released')),
    expires_at TEXT NOT NULL,
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_budget_reservations_active
    ON user_budget_reservations(user_id, status, model_id, day_start, month_start);
CREATE INDEX idx_user_budget_reservations_expiry
    ON user_budget_reservations(status, expires_at);

CREATE TABLE user_budget_attempt_usage (
    attempt_id INTEGER PRIMARY KEY REFERENCES request_attempts(id) ON DELETE CASCADE,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_microdollars INTEGER NOT NULL CHECK (cost_microdollars >= 0),
    provider_reported_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (provider_reported_microdollars >= 0),
    local_estimated_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (local_estimated_microdollars >= 0),
    cost_source TEXT NOT NULL CHECK (cost_source IN ('provider_reported', 'local_estimated', 'unknown')),
    tokens_known INTEGER NOT NULL CHECK (tokens_known IN (0, 1)),
    cost_known INTEGER NOT NULL CHECK (cost_known IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_budget_attempt_usage_request
    ON user_budget_attempt_usage(request_id, attempt_id);

CREATE TABLE user_budget_usage (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    model_id TEXT NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('day', 'month')),
    window_start TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    token_count INTEGER NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    cost_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (cost_microdollars >= 0),
    provider_reported_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (provider_reported_microdollars >= 0),
    local_estimated_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (local_estimated_microdollars >= 0),
    reservation_fallback_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (reservation_fallback_microdollars >= 0),
    unknown_token_requests INTEGER NOT NULL DEFAULT 0 CHECK (unknown_token_requests >= 0),
    unknown_cost_requests INTEGER NOT NULL DEFAULT 0 CHECK (unknown_cost_requests >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, model_id, window, window_start)
);

CREATE INDEX idx_user_budget_usage_window
    ON user_budget_usage(user_id, window, window_start, model_id);

CREATE TABLE user_budget_rule_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    model_id TEXT,
    window TEXT NOT NULL CHECK (window IN ('day', 'month')),
    action TEXT NOT NULL CHECK (action IN ('set', 'remove')),
    max_requests INTEGER,
    max_tokens INTEGER,
    max_microdollars INTEGER,
    max_concurrent INTEGER,
    rule_version INTEGER NOT NULL CHECK (rule_version > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_budget_rule_audit_user
    ON user_budget_rule_audit(user_id, created_at DESC, id DESC);

CREATE TRIGGER user_budget_rules_revision_insert AFTER INSERT ON user_budget_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_budget_rules_revision_update AFTER UPDATE ON user_budget_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_budget_rules_revision_delete AFTER DELETE ON user_budget_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
