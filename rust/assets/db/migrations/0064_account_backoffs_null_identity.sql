-- Enforce the account-backoff scope when model_id is NULL.
--
-- SQLite UNIQUE constraints treat NULL values as distinct, so the table's
-- composite constraint does not cover this valid scope: two rows for the same
-- (account, reason) pair can both carry a NULL model_id. Migration 0054
-- applied the same repair to model_quarantine.
--
-- Migration 0051 promised that legacy account_backoffs rows with reason
-- 'model_unavailable' would be hydrated as 'migration_legacy' provenance on
-- startup. That promise is not implemented here and cannot be: such a row
-- carries no model identity, so there is no model scope to attribute it to,
-- and model_quarantine.canonical_model_id is NOT NULL. The rows are deleted
-- instead. Migration 0051 is immutable and is not edited. The runtime's
-- hydration path skips and reports any such row that appears later, from a
-- restored backup or from a writer this runtime does not own.

DELETE FROM account_backoffs
WHERE reason = 'model_unavailable'
  AND model_id IS NULL;

DELETE FROM account_backoffs
WHERE id IN (
    SELECT id
    FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY account_id, reason
                ORDER BY updated_at DESC, consecutive_failures DESC, id
            ) AS duplicate_rank
        FROM account_backoffs
        WHERE model_id IS NULL
    )
    WHERE duplicate_rank > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_backoffs_null_model
    ON account_backoffs(account_id, reason)
    WHERE model_id IS NULL;
