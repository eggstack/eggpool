-- M009 instance-global budget ceilings intersected with per-user rules.
-- Exactly one instance-wide scope: model_id NULL means all models. No
-- user/tenant identifier and no user foreign key: global held and settled
-- counters survive user disable/delete and cover unattributed requests too.
CREATE TABLE instance_budget_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT,
    window TEXT NOT NULL CHECK (window IN ('day', 'month')),
    max_requests INTEGER CHECK (max_requests IS NULL OR max_requests > 0),
    max_tokens INTEGER CHECK (max_tokens IS NULL OR max_tokens > 0),
    max_microdollars INTEGER CHECK (max_microdollars IS NULL OR max_microdollars > 0),
    max_concurrent INTEGER CHECK (max_concurrent IS NULL OR max_concurrent > 0),
    enforcement TEXT NOT NULL DEFAULT 'estimated'
        CHECK (enforcement IN ('hard', 'estimated')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (max_requests IS NOT NULL OR max_tokens IS NOT NULL OR
           max_microdollars IS NOT NULL OR max_concurrent IS NOT NULL)
);

CREATE UNIQUE INDEX idx_instance_budget_rules_scope
    ON instance_budget_rules(ifnull(model_id, ''), window);
CREATE INDEX idx_instance_budget_rules_enforcement
    ON instance_budget_rules(enforcement);

-- One row per logical request that matched an instance rule at admission.
-- request_id references the global request row; intentionally no user
-- reference so history never depends on a user still existing.
CREATE TABLE instance_budget_reservations (
    request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
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
    enforcement TEXT NOT NULL DEFAULT 'estimated'
        CHECK (enforcement IN ('hard', 'estimated')),
    bound_tokens INTEGER NOT NULL DEFAULT 0 CHECK (bound_tokens >= 0),
    bound_microdollars INTEGER NOT NULL DEFAULT 0 CHECK (bound_microdollars >= 0),
    price_source TEXT CHECK (price_source IN ('operator_tariff')),
    status TEXT NOT NULL CHECK (status IN ('active', 'settled', 'released')),
    expires_at TEXT NOT NULL,
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_instance_budget_reservations_active
    ON instance_budget_reservations(status, model_id, day_start, month_start);
CREATE INDEX idx_instance_budget_reservations_expiry
    ON instance_budget_reservations(status, expires_at);

-- Per-model settled buckets. Global (model-agnostic) rules aggregate across
-- these rows at check time with a bounded indexed sum; no full history scan
-- runs under the writer gate.
CREATE TABLE instance_budget_usage (
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
    PRIMARY KEY (model_id, window, window_start)
);

CREATE INDEX idx_instance_budget_usage_window
    ON instance_budget_usage(window, window_start, model_id);

CREATE TABLE instance_budget_rule_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    model_id TEXT,
    window TEXT NOT NULL CHECK (window IN ('day', 'month')),
    action TEXT NOT NULL CHECK (action IN ('set', 'remove')),
    max_requests INTEGER,
    max_tokens INTEGER,
    max_microdollars INTEGER,
    max_concurrent INTEGER,
    enforcement TEXT NOT NULL DEFAULT 'estimated'
        CHECK (enforcement IN ('hard', 'estimated')),
    rule_version INTEGER NOT NULL CHECK (rule_version > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_instance_budget_rule_audit_recent
    ON instance_budget_rule_audit(created_at DESC, id DESC);

CREATE TRIGGER instance_budget_rules_revision_insert AFTER INSERT ON instance_budget_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_budget_rules_revision_update AFTER UPDATE ON instance_budget_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_budget_rules_revision_delete AFTER DELETE ON instance_budget_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
