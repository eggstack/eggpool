-- Migration 0046: Closed-loop threshold tuning (Phase 10).
--
-- Phase 10 of the cache-preserving deterministic compression roadmap
-- adds an optional tuning engine that analyses recent compression
-- observations and suggests bounded adjustments to conservative
-- thresholds (``min_candidate_tokens``, ``min_savings_tokens``,
-- ``max_compression_latency_ms``).  Tuning is disabled by default;
-- the first supported mode is ``recommend`` (advisory) and
-- ``apply`` is reserved for operators who explicitly opt in.
--
-- The new ``compression_tuning_recommendations`` table persists the
-- latest recommendation per policy so dashboards survive a process
-- restart.  Each row is keyed by ``policy_name`` (``<global>``
-- sentinel for requests without a Phase 6 override); the
-- ``recommendation_json`` column carries the full immutable
-- recommendation payload (status, current/recommended thresholds,
-- reason codes, window metrics, safety blockers, generation
-- timestamp).  The tuning engine never persists raw request content.
--
-- A second table, ``compression_tuning_overrides``, records every
-- runtime override the resolver applied when ``mode = "apply"`` is
-- active.  Overrides expire after ``cooldown_s`` and are written
-- transactionally so the audit trail cannot diverge from the
-- in-memory registry.  No raw prompts, tool outputs, system
-- messages, or auth headers are ever stored.
--
-- Both tables are additive and non-destructive.  Existing rows in
-- the ``requests`` table are not modified.

CREATE TABLE IF NOT EXISTS compression_tuning_recommendations (
    policy_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    recommendation_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_compression_tuning_recommendations_status
    ON compression_tuning_recommendations(status, generated_at);

CREATE TABLE IF NOT EXISTS compression_tuning_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_name TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_compression_tuning_overrides_policy
    ON compression_tuning_overrides(policy_name, generated_at);