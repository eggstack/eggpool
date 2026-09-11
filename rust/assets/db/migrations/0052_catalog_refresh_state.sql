-- Keep refresh freshness separate from semantic catalog metadata.
--
-- One row per account is enough because an account has exactly one provider.
-- The legacy catalog timestamps are used only to seed existing support; new
-- refreshes update this compact row instead of every model/provider row.
CREATE TABLE catalog_refresh_state (
    account_id INTEGER PRIMARY KEY,
    provider_id TEXT NOT NULL,
    last_successful_refresh_at TIMESTAMP NOT NULL,
    last_outcome TEXT NOT NULL DEFAULT 'legacy_hydrated',
    model_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX idx_catalog_refresh_state_provider
    ON catalog_refresh_state(provider_id);

INSERT INTO catalog_refresh_state (
    account_id,
    provider_id,
    last_successful_refresh_at,
    last_outcome,
    model_count
)
SELECT
    a.id,
    a.provider_id,
    COALESCE(MAX(pm.last_seen_at), MAX(m.last_seen_at)),
    'legacy_hydrated',
    COUNT(DISTINCT am.model_id)
FROM accounts AS a
JOIN account_models AS am
  ON am.account_id = a.id AND am.enabled = 1
JOIN models AS m
  ON m.model_id = am.model_id
LEFT JOIN provider_model_metadata AS pm
  ON pm.model_id = am.model_id AND pm.provider_id = a.provider_id
GROUP BY a.id, a.provider_id
HAVING COALESCE(MAX(pm.last_seen_at), MAX(m.last_seen_at)) IS NOT NULL;
