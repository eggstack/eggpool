-- Migration 0044: Resolved compression policy audit fields (Phase 6).
--
-- Phase 6 of the cache-preserving deterministic compression roadmap
-- introduces ``[[compression.policies]]`` operator-controllable
-- overrides.  The resolver merges the global ``[compression]``
-- config with any matching override rows and produces a
-- :class:`ResolvedCompressionPolicy` per request.  These three
-- columns persist the audit metadata so dashboards can group
-- requests by the resolved policy name without re-running the
-- resolver.
--
--   * compression_policy_name is the stable name of the matched
--     override (the policy table's ``name`` field) or ``NULL``
--     when no override fired.  Never empty; legacy callers that
--     did not run the resolver leave the column at the migration
--     default (``NULL``).
--   * compression_policy_source is a short audit string describing
--     where the resolved policy came from (``"global"`` when no
--     override matched, ``"policy:<name>"`` when an override
--     fired).  Stable values for dashboard filtering.
--   * compression_policy_warnings_json is a JSON array of
--     resolution warnings (e.g. ``["policy:opencode-safe: overlay
--     validation failed: ..."]``).  Empty array ``'[]'`` when the
--     resolver ran cleanly; ``NULL`` when the resolver did not
--     run.  Mirrors the Phase 3 / Phase 4 / Phase 5 JSON-column
--     pattern (serialise via the finalizer, never inspect at the
--     SQL layer).
--
-- Index on compression_policy_name for fast filtering of "requests
-- resolved under this policy" in the dashboard stats layer.  Source
-- is implicit by name (always ``"policy:<name>"`` when name is set,
-- always ``"global"`` when name is NULL), so a dedicated index on
-- the source column is redundant.

ALTER TABLE requests ADD COLUMN compression_policy_name TEXT;
ALTER TABLE requests ADD COLUMN compression_policy_source TEXT;
ALTER TABLE requests ADD COLUMN compression_policy_warnings_json TEXT;

CREATE INDEX IF NOT EXISTS idx_requests_compression_policy_name
    ON requests(compression_policy_name, started_at);