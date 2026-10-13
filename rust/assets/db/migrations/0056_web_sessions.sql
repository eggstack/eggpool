CREATE TABLE web_sessions (
    id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    selector TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL,
    csrf_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX web_sessions_user_active
    ON web_sessions(user_id, revoked_at, expires_at);

CREATE INDEX web_sessions_expiry
    ON web_sessions(expires_at) WHERE revoked_at IS NULL;
