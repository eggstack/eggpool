ALTER TABLE users
    ADD COLUMN auth_epoch INTEGER NOT NULL DEFAULT 1 CHECK (auth_epoch > 0);

ALTER TABLE requests
    ADD COLUMN user_id TEXT;
ALTER TABLE requests
    ADD COLUMN credential_id TEXT;
ALTER TABLE requests
    ADD COLUMN auth_epoch INTEGER;
ALTER TABLE requests
    ADD COLUMN principal_kind TEXT NOT NULL DEFAULT 'legacy_shared_key'
        CHECK (principal_kind IN ('agent_token', 'browser_session', 'legacy_shared_key'));
ALTER TABLE requests
    ADD COLUMN request_kind TEXT NOT NULL DEFAULT 'logical'
        CHECK (request_kind IN ('logical', 'internal_selector'));
ALTER TABLE requests
    ADD COLUMN parent_proxy_request_id TEXT;

CREATE INDEX idx_requests_user_recent
    ON requests(user_id, started_at DESC, id DESC);
CREATE INDEX idx_requests_parent_proxy_request
    ON requests(parent_proxy_request_id)
    WHERE parent_proxy_request_id IS NOT NULL;
