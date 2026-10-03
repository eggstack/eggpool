# Runtime Efficiency Milestone 003 — Dashboard TTFT Percentile Query Qualification

Status: closed

Repository baseline: `5f028625` (M002 closure; original query baseline `6489aa75aa276a12ff14c35a07b46f2eba01e2b8`)

Source roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-003--dashboard-ttft-percentile-query-qualification-and-bounded-rewrite`

Long-term requirements:

- `plans/000-long-term-specification.md#2-runtime-invariants` — ordinary
  production keeps the current SQLite ownership until separately reviewed.
- `plans/000-long-term-specification.md#5-performance-posture`
- `plans/002-long-term-roadmap.md#phase-3--persistence-and-publication-bounds`
- `plans/003-planning-process.md`

Applicable ADRs:

- None for measurement or a query-only rewrite.
- If evidence requires a new SQLite connection/topology or durable schema/index
  migration, stop and create a separate reviewed plan; do not widen M003.

Primary class: polish

## 1. Objective

Characterize the current dashboard streamed-TTFT percentile query sequence on
realistic file-backed history and, only when the evidence demonstrates material
repeated sort/range work, replace it with a bounded semantically identical query
shape that reduces serialized SQLite worker/gate tenure.

A valid closure outcome is **keep current implementation** when evidence is
weak or the replacement is more complex than the measured cost.

## 2. Why this milestone is ready

No hard dependency is open. Dashboard feature/parity work is closed, the
dashboard JSON contract is stable, schema 54 and the bundled SQLite engine are
stable, and the previous runtime campaign already established a file-backed
query-plan qualification method.

Persistence M007 is independent. M003 must not change checkpoint behavior,
connection topology, or infer that a dashboard improvement fixes the known
Pi/MMC finite tail.

## 3. Current implementation evidence

At baseline,
`UsageRollupRepository::dashboard_summary_basic(period)` executes on one
serialized `Database::call`:

1. `COUNT(*)` over streamed rows with non-null `first_byte_ms` in the
   selected time window;
2. an ordered `first_byte_ms LIMIT 1 OFFSET ?` for the lower median row;
3. another ordered lookup for the upper median row;
4. another ordered lookup for p99;
5. the aggregate dashboard summary query with the computed percentile values
   passed as parameters.

For a non-empty window this is up to four TTFT-specific queries before/alongside
the aggregate read.

Migration 0022 provides the partial
`idx_requests_streamed_started_ttft (started_at, first_byte_ms)` index for
streamed rows. It usefully narrows time-window scans, but a range predicate on
the leading `started_at` column may still require temporary ordering by
`first_byte_ms`. That must be verified with the pinned SQLite query planner;
it is not assumed.

The previous Plan 228 dashboard fixture used 10,000 rows and did not justify a
second read connection. This milestone rechecks only the now-visible percentile
query sequence and remains query-local.

## 4. Invariants that must not regress

- `GET /api/stats/summary` and dashboard-page summary data retain identical
  field names/types and period normalization.
- TTFT population remains exactly: `streamed = 1 AND first_byte_ms IS NOT NULL`
  within the same inclusive/exclusive time bounds.
- Median definition remains the midpoint of indexes
  `(n - 1) / 2` and `n / 2`.
- p99 index remains `ceil(0.99 * n) - 1`, currently encoded as
  `(99 * n + 99) / 100 - 1`.
- Empty populations yield 0.0 for p50/p99 exactly as today.
- Integer/REAL conversion semantics remain compatible.
- Dashboard degradation/error behavior remains unchanged.
- Reads remain behind the existing database gate/worker.
- No request/provider/account identity or raw body is added to diagnostics.
- No new connection, background task, cache, summary table, or migration.

## 5. Scope

### In scope

- Deterministic file-backed SQLite fixtures at bounded history sizes.
- `EXPLAIN QUERY PLAN` for the current count/offset/aggregate sequence.
- Descriptive elapsed/gate-tenure measurements using the pinned release/test
  build and existing repository tooling.
- Semantic oracle cases for odd/even populations, duplicate TTFTs, nulls,
  period boundaries, and empty data.
- A query-only rewrite if evidence justifies it, preferably one that sorts the
  TTFT population once and returns only the needed percentile rows/scalars.
- Exact old/new result comparison.
- Dashboard/API regression qualification.

### Explicitly out of scope

- Dedicated dashboard read connection.
- SQLite pool or second general-purpose worker.
- New index/schema migration.
- Materialized/rollup percentile tables.
- Approximate percentiles.
- Changed percentile definition.
- Dashboard frontend redesign.
- Persistence M007/checkpoint changes.
- Permanent benchmark dependency or daemon.

## 6. Required production changes

### Phase 1 — Evidence before code

Seed file-backed schema-54 databases with deterministic request histories,
including at least:

- 10k rows for direct comparison to prior Plan 228 scale;
- a larger bounded history such as 100k rows to expose repeated sort behavior;
- realistic streamed/non-streamed/null-TTFT ratios;
- rows both inside and outside 1h/24h/7d/30d windows.

Run `EXPLAIN QUERY PLAN` for the exact current TTFT count and ordered-offset
queries. Record whether a temporary B-tree/sort is used and whether that work is
repeated.

Measure dashboard-summary call duration separately from concurrent write
latency. Do not attribute generic filesystem noise to the query without
repeatability.

### Phase 2 — Conditional query-only rewrite

Proceed only when Phase 1 shows a repeatable meaningful cost attributable to
the repeated percentile queries.

Preferred properties of a replacement:

- one ordered TTFT relation/CTE/window calculation rather than three independent
  ordered OFFSET executions;
- returns at most the bounded percentile scalar/row set to Rust;
- does not collect the entire TTFT population into application memory;
- reuses the existing time predicate exactly;
- uses the pinned SQLite feature set only;
- keeps the aggregate summary semantics unchanged.

A window-function/CTE approach is acceptable if `EXPLAIN QUERY PLAN` confirms
it avoids repeated sorting and semantic tests prove exact parity. A different
query-only shape is acceptable when it is simpler and equally bounded.

Do not add an index in this milestone. If the only useful solution is a new
index, close M003 with that finding and open a migration-specific successor.

### Phase 3 — Re-measure

Against the same databases and build mode:

- record old/new query plans;
- record repeated summary durations;
- run concurrent lifecycle writes while refreshing summary at a bounded cadence;
- confirm no regression in publication/finalization correctness;
- avoid claiming target-class Pi/MMC latency improvement unless separately run
  there.

## 7. Ordered work packages

### Work package A — Semantic oracle

Create focused repository tests that compute/lock current p50/p99 output for:

- zero rows;
- one row;
- odd/even row counts;
- duplicate values;
- null TTFT exclusion;
- non-streamed exclusion;
- exact period-boundary rows;
- larger deterministic ordered/unordered insert sets.

These tests are authoritative for any rewrite.

### Work package B — File-backed query-plan evidence

Build the 10k and larger bounded fixtures and capture:

- exact `EXPLAIN QUERY PLAN` output for each TTFT query;
- summary elapsed distribution over repeated calls;
- database size and relevant index inventory;
- bundled SQLite version.

Keep evidence secret-free and repository-local where practical. A closure
record may summarize raw command output; no permanent large fixture file is
required.

### Work package C — Decision gate

If current cost is small or the planner already reuses work effectively, make
no production change and proceed directly to closure.

If repeated sorting/range work is clear and material, implement one query-only
rewrite under the invariants above.

Record the decision before broadening scope.

### Work package D — Exact result and contention qualification

For a rewritten candidate:

- compare old/new summary structs over the semantic oracle and randomized
  deterministic fixture seeds;
- run dashboard/API tests;
- run publication/finalization/database tests while summary reads repeat;
- compare query plans/timings using the same seeded databases.

## 8. Failure, cancellation, restart, contention semantics

Dashboard summary remains a read-only `Database::call`; errors degrade the
dashboard exactly as before.

Cancellation must not leave new tasks, caches, or temp state.

A long dashboard read still serializes on the existing DB gate. The goal is to
shorten its query work, not bypass ownership.

Backup/restart/recovery behavior is unchanged because no schema/topology state
changes.

## 9. Compatibility and migration

No migration is authorized.

No HTTP/JSON/CLI/config/Rust public API change.


A query rewrite is internal and must produce the same `DashboardSummary`
values for every fixture.

If evidence requires an index, second connection, materialized summary, or
schema change, stop M003 and register a separate plan with explicit
migration/lifecycle implications.

## 10. Required tests

Focused:

```bash
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
```

Also run the dashboard module/unit tests and tooling parity tests affected by
the summary endpoint.

Add exact percentile semantic cases described in Work package A.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv run pytest tests/tooling/test_dashboard_parity_projection.py tests/tooling/test_dashboard_parity_modules.py -q
```

If no production change is justified, closure still records the focused
semantic tests and Phase-1 measurement evidence; do not create churn merely to
satisfy the plan.

## 12. Documentation updates

If a rewrite lands, update:

- `architecture/deep-dive-dashboard.md`;
- `architecture/deep-dive-database.md` only if its summary-query description
  becomes inaccurate.

If the result is keep-current, record the decision in the closure record and
roadmap status without changing architecture docs unnecessarily.

## 13. Acceptance criteria

One of two valid outcomes:

**Keep outcome**

- seeded file-backed query plans/timings are recorded;
- current query cost does not justify additional production complexity;
- no production Rust/schema change;
- semantic and broad qualification remain green.

**Rewrite outcome**

- evidence shows repeated current query work worth removing;
- replacement returns exact p50/p99/summary values;
- TTFT population/period semantics are unchanged;
- replacement is bounded and does not load the full population into Rust;
- repeated sort/range work is reduced in query-plan evidence;
- dashboard read duration improves repeatably on the seeded fixture without
  write correctness regression;
- no migration/topology/public API change;
- full default/no-default qualification is green.

## 14. Stop conditions

Stop and record a follow-up rather than widening scope if:

- useful improvement requires a new index/migration;
- useful improvement requires a second read connection or connection pool;
- only approximate percentile algorithms appear practical;
- the rewrite would collect an unbounded TTFT vector in application memory;
- query-plan evidence is ambiguous and timing is dominated by unrelated
  filesystem/checkpoint behavior;
- persistence M007 changes concurrently in a way that invalidates the
  measurement baseline.

## 15. Closure evidence required

Record:

- implementation/measurement commit(s);
- exact seeded row counts and SQLite version;
- relevant existing indexes;
- before query plans and timing summaries;
- decision: keep or rewrite;
- if rewritten, after query plans/timings and exact result-parity evidence;
- concurrent dashboard-read/write correctness evidence;
- focused + full default/no-default suites;
- dashboard tooling/parity results;
- strict fmt/Clippy + locked release build;
- explicit statement that no migration, second connection, checkpoint change,
  or target-class performance claim was introduced.

## 16. Execution findings — 2026-10-03

The opt-in file-backed qualification is implemented in
`rust/tests/dashboard_ttft_qualification.rs` and was run twice with the pinned
bundled SQLite 3.53.2 engine. Each run seeds independent schema-54 files with
10,000 and 100,000 deterministic request rows (80% streamed, 68.6% of all
rows with TTFT, 90% timestamped 12 hours back inside the 24-hour query, and
10% outside 30 days). The printed file size is the main database file only;
the WAL sidecar is not included.

Both runs selected `idx_requests_streamed_started_ttft` for the count and
ordered TTFT queries. COUNT is a covering-index range scan. Each
`ORDER BY first_byte_ms` plan uses a temporary B-tree; the implementation has
three such ordered statements (lower median, upper median, and p99). Despite
that repeated sort, complete `dashboard_summary_basic("24h")` timings were:

The first measurement attempt used rows timestamped exactly 24 hours earlier.
Because seeding and query execution occur at different times, those rows could
fall outside the 24-hour predicate. Those initial timing samples are discarded.
The fixture was corrected to put 90% of rows 12 hours inside the 24-hour query
window before collecting the accepted runs below.

| Rows | Run | p50 ms | p95 ms | max ms | Writer p50 ms | Writer max ms | Main DB bytes |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 1 | 52.959 | 53.133 | 56.819 | 53.756 | 58.867 | 9,052,160 |
| 10,000 | 2 | 53.078 | 53.893 | 57.152 | 54.036 | 69.346 | 9,052,160 |
| 100,000 | 1 | 755.840 | 759.517 | 761.531 | 756.729 | 792.494 | 86,970,368 |
| 100,000 | 2 | 754.329 | 756.802 | 757.766 | 756.218 | 757.354 | 86,982,656 |

The write attempts are queued alongside each summary call on the same serialized
database worker; their elapsed time tracks the summary duration, with some
additional host scheduling noise. These are host-local descriptive measurements,
not an SBC latency claim.

Decision: **implement an adaptive query-only bounded rewrite**. A grouped
frequency/window query alone regressed the 10k fixture (~68 ms vs the original
~53 ms), while reducing the 100k fixture to ~454–456 ms from ~754–756 ms. The
grouped path therefore runs only for at least 50,000 eligible rows; other
populations retain the original offset queries. A first adaptive selector
counted distinct TTFT values, which added a temporary B-tree and measured
56–68 ms at 10k and 510–518 ms at 100k, so it was removed. The existing
covering-index count is reused for the threshold. The final adaptive query
plans keep that count as an index range scan and use the frequency GROUP BY
plus bounded window ranking for the large history. Final p50 at 10k was
54.719 ms (writer p50 55.605 ms), close to the 52.959–53.078 ms baseline;
final p50 at 100k was 472.781 ms (writer p50 471.905 ms), about 37% below
the 754.329–755.840 ms baseline. Exact parity was asserted against the old
offset implementation on both deterministic large fixtures and the semantic
oracle. The grouped query uses temporary B-trees for grouping and window
ordering, but does not sort each of three full-row offset queries. No new
index, schema, connection, or persistence-topology change is authorized.

The semantic test locks empty/single/odd/even/duplicate/null/non-streamed and
out-of-window behavior. The expensive seeded harness is ignored by normal
workspace test runs and can be explicitly invoked with the command recorded
in the closure record. Accepted adaptive qualification runs used the bundled
SQLite 3.53.2 engine and checked exact p50/p99 equality against the old query
for each seeded 10k/100k database before timing.

## 17. Handoff notes

This is deliberately a measurement-gated plan. A zero-production-diff closure
is successful when the current implementation is already cheap enough.
