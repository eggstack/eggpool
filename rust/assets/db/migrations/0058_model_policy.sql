CREATE TABLE policy_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
);

INSERT INTO policy_state(singleton, revision) VALUES (1, 1);

CREATE TABLE policy_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 64),
    default_effect TEXT NOT NULL CHECK (default_effect IN ('allow', 'deny')),
    preferred_model_id TEXT,
    quota_policy_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE profile_model_rules (
    profile_id TEXT NOT NULL REFERENCES policy_profiles(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL CHECK (length(model_id) BETWEEN 1 AND 512),
    effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    created_by TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (profile_id, model_id)
);

CREATE TABLE user_profile_assignments (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL REFERENCES policy_profiles(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    assigned_by TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_policy_overrides (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL CHECK (length(model_id) BETWEEN 1 AND 512),
    effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    updated_by TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, model_id)
);

CREATE TABLE instance_model_denials (
    model_id TEXT PRIMARY KEY CHECK (length(model_id) BETWEEN 1 AND 512),
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_profile_model_rules_profile ON profile_model_rules(profile_id, model_id);
CREATE INDEX idx_user_policy_overrides_user ON user_policy_overrides(user_id, model_id);

CREATE TRIGGER users_policy_revision_update AFTER UPDATE OF state, role, auth_epoch ON users
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;

CREATE TRIGGER policy_profiles_revision_insert AFTER INSERT ON policy_profiles
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER policy_profiles_revision_update AFTER UPDATE ON policy_profiles
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER policy_profiles_revision_delete AFTER DELETE ON policy_profiles
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER profile_model_rules_revision_insert AFTER INSERT ON profile_model_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER profile_model_rules_revision_update AFTER UPDATE ON profile_model_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER profile_model_rules_revision_delete AFTER DELETE ON profile_model_rules
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_profile_assignments_revision_insert AFTER INSERT ON user_profile_assignments
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_profile_assignments_revision_update AFTER UPDATE ON user_profile_assignments
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_profile_assignments_revision_delete AFTER DELETE ON user_profile_assignments
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_policy_overrides_revision_insert AFTER INSERT ON user_policy_overrides
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_policy_overrides_revision_update AFTER UPDATE ON user_policy_overrides
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER user_policy_overrides_revision_delete AFTER DELETE ON user_policy_overrides
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_model_denials_revision_insert AFTER INSERT ON instance_model_denials
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_model_denials_revision_update AFTER UPDATE ON instance_model_denials
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
CREATE TRIGGER instance_model_denials_revision_delete AFTER DELETE ON instance_model_denials
BEGIN UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1; END;
