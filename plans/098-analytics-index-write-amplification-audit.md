# Plan 098 — Analytics Index Write-Amplification Audit

Date: 2026-08-10
Status: planned
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

## Purpose

Audit analytics-oriented SQLite indexes on frequently written request/attempt tables and remove or narrow only those whose read benefit does not justify continuous SBC write amplification.

This is an evidence-gathering and selective-deletion plan, not a generic database tuning exercise. The default outcome may legitimately be “keep the current indexes” if representative query plans show they materially bound dashboard/maintenance reads.

## Relevant schema and code

Inspect at minimum:

- `src/eggpool/db/schema/0002_indexes.sql`
- `src/eggpool/db/schema/0020_performance_indexes.sql`
- `src/eggpool/db/schema/0022_dashboard_indexes.sql`
- `src/eggpool/db/schema/0026_attempt_observability.sql`
- `src/eggpool/db/schema/0027_routing_decisions.sql`
- `src/eggpool/db/schema/0050_routing_decisions_retention_index.sql`
- later migrations that add/drop indexes on `requests`, `request_attempts`, `reservations`, `routing_decisions`, or rollup tables
- dashboard/stats repositories and SQL queries consuming these indexes
- maintenance/retention queries and startup reconciliation queries.

High-interest candidates include analytics indexes on `request_attempts`, especially:

- `(provider_id, started_at)`
- `(model_id, started_at)`
- `(status_code, started_at)`
- `(retry_category, started_at)`

and dashboard-oriented request indexes such as:

- `(client_ip, started_at)`
- partial streamed TTFT indexes.

These are candidates only, not predetermined removals.

## Governing constraints

1. Do not remove primary keys, unique constraints, foreign-key-supporting lookup paths, startup recovery indexes, or bounded retention indexes without direct proof that another existing index covers the exact query.
2. Do not add dynamic index creation/drop based on `[dashboard] enabled`.
3. Do not add an index advisor or runtime query planner.
4. Do not add automatic VACUUM/REINDEX.
5. Do not introduce database-specific extensions.
6. Prefer fewer/narrower indexes only when representative `EXPLAIN QUERY PLAN` evidence supports the trade.
7. Do not optimize the dashboard as if it were a high-QPS public analytics service; occasional modest scan cost may be acceptable on a local appliance.
8. Do not knowingly turn daily retention/reconciliation into unbounded full-table scans.

## Workstream A — Build an index-to-query inventory

For each non-primary index on the high-write tables, record:

- index name and columns/partial predicate;
- table written frequency/class (per request, per attempt, periodic, rare);
- all production SQL queries that can use the index;
- whether the query is correctness/recovery, bounded maintenance, dashboard/stats, or optional diagnostics;
- whether another existing index has a useful left-prefix/covering relationship;
- whether indexed columns are sparse/null-heavy.

Do not create a permanent registry. A concise table in this plan's completion record is sufficient.

Classify indexes into:

- **must keep** — correctness/recovery/retention or clearly dominant hot read;
- **candidate narrow** — sparse analytics field where a partial index can reduce footprint/write cost;
- **candidate remove** — optional dashboard query with acceptable bounded scan and no correctness role;
- **keep pending evidence** — uncertain benefit/trade.

## Workstream B — Representative local dataset

Use an existing local test fixture/database generator if available. Otherwise create a temporary one-off SQLite database in tests or `/tmp` with representative cardinality; do not commit a large fixture.

Suggested scale for query-plan inspection:

- enough requests/attempts to make planner choices meaningful, e.g. tens of thousands of rows;
- multiple providers/accounts/models;
- mostly successful attempts;
- retry/error categories sparse relative to total attempts;
- streamed and non-streamed requests;
- several client IP values.

Exact row count is not a product benchmark and must not become a CI requirement.

Use `ANALYZE` on the temporary database if needed so SQLite has statistics. Do not alter production startup to run `ANALYZE` automatically as part of this plan.

## Workstream C — `EXPLAIN QUERY PLAN` evidence

For each candidate index:

1. record the production query;
2. capture `EXPLAIN QUERY PLAN` with current schema;
3. in a temporary disposable DB only, drop/narrow the candidate;
4. capture the new plan;
5. assess whether the resulting scan/sort remains bounded and acceptable for an occasional local dashboard request;
6. inspect whether a different existing index now covers the query.

Do not rely solely on elapsed milliseconds from one workstation. Query shape and row visitation are more portable evidence than noisy timing.

## Workstream D — Partial-index opportunity for sparse retry/error fields

Specifically evaluate whether a sparse field such as `retry_category` benefits from a partial index such as:

```sql
CREATE INDEX ...
ON request_attempts(retry_category, started_at)
WHERE retry_category IS NOT NULL;
```

Only adopt this if:

- production queries filtering retry category naturally satisfy the predicate;
- NULL/no-retry rows dominate representative data;
- query plans still use the partial index;
- migration complexity remains small.

Do not create multiple overlapping partial indexes to micro-optimize every error type.

## Workstream E — Migration strategy

If indexes are changed, add one normal forward migration with explicit `DROP INDEX IF EXISTS` / `CREATE INDEX IF NOT EXISTS` operations.

Requirements:

- update `checksums.json` through the repository's normal migration process;
- do not edit historical migration files;
- keep migration transactional/compatible with supported SQLite version;
- do not rebuild request tables;
- avoid migration-time full-table data rewrites.

If no index change is justified, this plan may complete with documentation/evidence only and no schema migration.

## Workstream F — Flash/write consideration

Use application-level reasoning plus optional temporary SQLite page/WAL observations to compare candidate index cost. A full storage benchmark is not required.

At minimum, record for each removed/narrowed per-attempt/per-request index:

- it is maintained on every INSERT/UPDATE affecting indexed columns;
- the query benefit it provides;
- why the optional read cost is acceptable after the change.

Do not claim exact microSD endurance improvement unless actually measured on comparable hardware.

## Workstream G — Regression tests

Tests should prove:

- migrations create the intended final index set;
- correctness/recovery/retention queries still return identical results;
- dashboard/stats queries still return identical results;
- query-plan assertions are used sparingly and only for the critical retained/new partial index shape. Avoid pinning exact planner wording for every query;
- migration works on an existing pre-plan database.

Do not add runtime tests that fail because an optional dashboard query takes a specific number of milliseconds.

## Verification

Run migration/database/dashboard/stats focused tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Retain the query-plan comparison in this plan's closure notes, not as a new CI artifact.

## Acceptance criteria

- [ ] Every candidate request/attempt analytics index is mapped to its actual production consumers before removal/narrowing.
- [ ] Correctness, startup recovery, foreign-key/identity lookup, reservation expiry, and bounded retention indexes are explicitly separated from optional analytics indexes.
- [ ] Representative `EXPLAIN QUERY PLAN` evidence is captured for each index actually changed.
- [ ] No index is removed solely because the schema “has too many indexes.”
- [ ] Sparse retry/error indexes are evaluated for partial-index narrowing where representative data supports it.
- [ ] Any adopted partial index is used by its production query and materially excludes ordinary NULL/no-retry rows.
- [ ] Dashboard/stats result semantics are unchanged after index changes.
- [ ] Startup reconciliation and maintenance/retention queries remain bounded and appropriately indexed.
- [ ] Historical migrations are not edited; any schema change uses one new forward migration and updates migration checksums normally.
- [ ] No automatic VACUUM, REINDEX, ANALYZE-at-startup, dynamic index feature, or database tuning service is introduced.
- [ ] If evidence does not justify an index change, the plan explicitly records `keep` rather than forcing a modification.
- [ ] Focused migration/query tests and ordinary smoke gate pass.

## Rejection conditions

Reject the implementation if:

- an index is dropped without locating its production consumers;
- elapsed workstation timing alone is used as the reason for removal;
- a correctness/recovery/retention query becomes an unbounded table scan;
- historical migrations are rewritten;
- dynamic index toggling is introduced for dashboard enable/disable;
- the plan expands into schema redesign, partitioning, another DB engine, automatic maintenance, or generalized benchmarking;
- exact flash-endurance improvements are claimed without measurement.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, this plan, all relevant index migrations, database architecture docs, and dashboard/stats SQL consumers.
2. Build the temporary index-to-query inventory.
3. Create/reuse a disposable representative database and collect baseline query plans.
4. Evaluate candidate drops/partial indexes one at a time in the disposable DB.
5. Implement only changes supported by query-plan evidence.
6. Add one forward migration if required and update migration checksums through the normal repository mechanism.
7. Run focused migration/dashboard/stats tests.
8. Run ordinary lint/type/smoke gate.
9. Record the final keep/narrow/remove decisions and evidence in this plan.
10. Stop; do not tune unrelated SQLite pragmas or add performance infrastructure.
