-- Tool permissions are separate from model policy and default to deny.
CREATE TABLE profile_tool_rules (
    profile_id TEXT NOT NULL REFERENCES policy_profiles(id) ON DELETE CASCADE,
    backend_id TEXT NOT NULL CHECK (backend_id = 'eggsearch'),
    tool_name TEXT NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 128),
    effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    created_by TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (profile_id, backend_id, tool_name)
);

CREATE TABLE user_tool_overrides (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    backend_id TEXT NOT NULL CHECK (backend_id = 'eggsearch'),
    tool_name TEXT NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 128),
    effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    updated_by TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, backend_id, tool_name)
);

CREATE TABLE instance_tool_denials (
    backend_id TEXT NOT NULL CHECK (backend_id = 'eggsearch'),
    tool_name TEXT NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 128),
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (backend_id, tool_name)
);

CREATE INDEX idx_profile_tool_rules_profile ON profile_tool_rules(profile_id, backend_id, tool_name);
CREATE INDEX idx_user_tool_overrides_user ON user_tool_overrides(user_id, backend_id, tool_name);

CREATE TRIGGER profile_tool_rules_revision_insert AFTER INSERT ON profile_tool_rules
BEGIN
    UPDATE policy_profiles SET version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = NEW.profile_id;
    UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1;
END;
CREATE TRIGGER profile_tool_rules_revision_update AFTER UPDATE ON profile_tool_rules
BEGIN
    UPDATE policy_profiles SET version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = NEW.profile_id;
    UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1;
END;
CREATE TRIGGER profile_tool_rules_revision_delete AFTER DELETE ON profile_tool_rules
BEGIN
    UPDATE policy_profiles SET version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = OLD.profile_id;
    UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1;
END;
CREATE TRIGGER user_tool_overrides_revision_insert AFTER INSERT ON user_tool_overrides
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_tool_overrides_revision_update AFTER UPDATE ON user_tool_overrides
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_tool_overrides_revision_delete AFTER DELETE ON user_tool_overrides
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_tool_denials_revision_insert AFTER INSERT ON instance_tool_denials
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_tool_denials_revision_update AFTER UPDATE ON instance_tool_denials
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_tool_denials_revision_delete AFTER DELETE ON instance_tool_denials
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
