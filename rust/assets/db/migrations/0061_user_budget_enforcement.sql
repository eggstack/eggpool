-- M008 enforceable token/cost admission: per-rule enforcement mode plus
-- auditable worst-case reservation facts. Existing rows keep the estimated
-- mode explicitly; promotion to hard mode is an admin operation, never an
-- automatic upgrade.
ALTER TABLE user_budget_rules
    ADD COLUMN enforcement TEXT NOT NULL DEFAULT 'estimated'
    CHECK (enforcement IN ('hard', 'estimated'));

ALTER TABLE user_budget_rule_audit
    ADD COLUMN enforcement TEXT NOT NULL DEFAULT 'estimated'
    CHECK (enforcement IN ('hard', 'estimated'));

-- Worst-case permit facts recorded with each held reservation so hard-mode
-- holds stay auditable after settlement. Estimated-mode rows keep zero
-- bounds with a NULL price source.
ALTER TABLE user_budget_reservations
    ADD COLUMN enforcement TEXT NOT NULL DEFAULT 'estimated'
    CHECK (enforcement IN ('hard', 'estimated'));
ALTER TABLE user_budget_reservations
    ADD COLUMN bound_tokens INTEGER NOT NULL DEFAULT 0
    CHECK (bound_tokens >= 0);
ALTER TABLE user_budget_reservations
    ADD COLUMN bound_microdollars INTEGER NOT NULL DEFAULT 0
    CHECK (bound_microdollars >= 0);
ALTER TABLE user_budget_reservations
    ADD COLUMN price_source TEXT CHECK (price_source IN ('operator_tariff', 'estimate'));

CREATE INDEX idx_user_budget_rules_enforcement
    ON user_budget_rules(user_id, enforcement);
