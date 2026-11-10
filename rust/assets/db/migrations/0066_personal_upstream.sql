-- Secret-free personal upstream grants and bindings.
-- The plaintext provider secret stays in the config-adjacent environment file.
CREATE TABLE personal_upstream_grants (
    scope TEXT NOT NULL CHECK (scope IN ('instance', 'user')),
    user_id TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny', 'inherit')),
    updated_by TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, user_id),
    CHECK (
        (scope = 'instance' AND user_id = '' AND effect IN ('allow', 'deny'))
        OR (
            scope = 'user'
            AND length(user_id) BETWEEN 1 AND 64
            AND effect IN ('allow', 'deny', 'inherit')
        )
    )
);

INSERT INTO personal_upstream_grants(scope, user_id, effect)
VALUES ('instance', '', 'deny');

CREATE TABLE personal_upstream_bindings (
    provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 96),
    account_name TEXT NOT NULL CHECK (length(account_name) BETWEEN 1 AND 96),
    user_id TEXT NOT NULL CHECK (length(user_id) BETWEEN 1 AND 64),
    state TEXT NOT NULL CHECK (state IN ('active', 'withdrawn')),
    actor_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, account_name)
);

CREATE INDEX idx_personal_upstream_bindings_user
    ON personal_upstream_bindings(user_id, state);
