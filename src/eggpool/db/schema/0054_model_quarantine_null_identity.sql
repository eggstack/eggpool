-- Enforce the model-quarantine scope when upstream_model_id is NULL.
-- SQLite UNIQUE constraints treat NULL values as distinct, so the table's
-- composite constraint does not cover this valid scope.

DELETE FROM model_quarantine
WHERE id IN (
    SELECT id
    FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY provider_id, account_id, canonical_model_id,
                    upstream_protocol
                ORDER BY observation_count DESC, last_observed DESC, id
            ) AS duplicate_rank
        FROM model_quarantine
        WHERE upstream_model_id IS NULL
    )
    WHERE duplicate_rank > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_model_quarantine_null_upstream
    ON model_quarantine(
        provider_id,
        account_id,
        canonical_model_id,
        upstream_protocol
    )
    WHERE upstream_model_id IS NULL;
