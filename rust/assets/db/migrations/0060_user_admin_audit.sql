CREATE TABLE user_admin_audit (
    id INTEGER PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 64),
    target_kind TEXT NOT NULL CHECK (length(target_kind) BETWEEN 1 AND 32),
    target_id TEXT NOT NULL CHECK (length(target_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_admin_audit_recent
    ON user_admin_audit(created_at DESC, id DESC);
CREATE INDEX idx_user_admin_audit_target
    ON user_admin_audit(target_kind, target_id, created_at DESC, id DESC);
