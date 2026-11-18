CREATE TABLE security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start INTEGER NOT NULL CHECK(window_start >= 0 AND window_start % 60 = 0),
    event_code TEXT NOT NULL CHECK(event_code IN (
        'sign_in_succeeded',
        'sign_in_failed',
        'sign_in_throttled',
        'session_rejected',
        'agent_token_rejected',
        'agent_token_throttled',
        'csrf_rejected',
        'origin_rejected',
        'transport_rejected',
        'admin_access_denied',
        'scope_denied'
    )),
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'refused', 'throttled', 'denied')),
    principal_kind TEXT NOT NULL CHECK(principal_kind IN (
        'anonymous',
        'agent_token',
        'browser_session',
        'legacy_shared_key'
    )),
    user_id TEXT,
    event_count INTEGER NOT NULL CHECK(event_count > 0)
);

CREATE UNIQUE INDEX security_events_window_unique
    ON security_events(
        window_start,
        event_code,
        outcome,
        principal_kind,
        COALESCE(user_id, '')
    );

CREATE INDEX security_events_recent
    ON security_events(window_start DESC, id DESC);
