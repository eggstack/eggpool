# Persistence Milestone 002 — Single-Gate Tenure and Metrics-Flush Allocation Cleanup

Status: active

Repository baseline: ef4749f2eb48c46a877c1d83d5bb313c194df1f0

Source roadmap:

- plans/subsystems/persistence-roadmap.md#milestone-002--single-gate-tenure-and-metrics-flush-allocation-cleanup

Long-term requirements:

- plans/000-long-term-specification.md — §2 invariant 4, §3 ownership boundaries, §5 performance posture
- plans/002-long-term-roadmap.md — Phase 3 persistence and publication bounds

Applicable ADRs:

- None required. This milestone is an internal ownership/statement-preparation cleanup that preserves the current schema, one SQLite gate/worker, transaction grouping, public APIs, and failure semantics.

Primary class: polish

## 1. Objective

Reduce avoidable CPU work, heap churn, and repeated SQLite statement preparation around EggPool's existing single serialized database gate.

This milestone has two tightly related targets:

1. publication should prepare deterministic routing-decision payload data before acquiring the database gate and move only the minimal owned row facts into the worker transaction;
2. metrics flush should consume owned buffered data without repeated deep String/batch clones and should prepare its repeated UPSERT once per transaction.

The point is shorter, cheaper occupancy of the existing gate — not a new persistence architecture.

## 2. Why this milestone is ready

There is no unresolved architecture dependency.

The current source makes the redundant work explicit:

- rust/src/coordinator/publication.rs::publish_transaction clones the complete SelectionSnapshot before moving into the transaction closure. insert_attempt_rows then allocates one serde_json::Value per exclusion, collects those into a Vec, serializes them, serializes selected RoutingScore, and formats the reservation TTL while the database transaction owns the one gate.
- rust/src/operations/metrics.rs::record_buffered receives UsageMetricEvent by value but clones bucket_start/provider_id/model_id/protocol/status to form the key.
- MetricsWriteCoalescer::flush takes the buffered BTreeMap, clones all row-key Strings into a Vec, then deep-clones that Vec again as transaction_rows so the original can be rebuffered on failure.
- The metrics transaction executes the same large INSERT ... ON CONFLICT statement through connection.execute once for every row, which repeats prepare/finalize work while the gate is held.

All of these can be removed behind private helpers without changing public behavior.

Legacy Plan 231 explicitly deferred the analogous routing transient-collection cleanup when it became broader than its original low-risk clone fix. This persistence work is separate and directly evidenced in the current owners.

## 3. Current implementation evidence

Authority paths:

- rust/src/coordinator/publication.rs
- rust/src/coordinator/finalization.rs
- rust/src/db/connection.rs
- rust/src/db/repositories.rs
- rust/src/operations/metrics.rs
- rust/tests/coordinator_publication.rs
- rust/tests/coordinator_finalization.rs
- rust/tests/database_compatibility.rs
- rust/tests/operations_o007.rs
- architecture/deep-dive-database.md
- architecture/deep-dive-metrics.md

### Publication path

publish_transaction currently copies:

- PublicationInput;
- account/provider/model/protocol Strings;
- full SelectionSnapshot, including candidate and exclusion collections.

Inside insert_attempt_rows, routing-decision persistence only needs bounded scalar fields plus:

- exclusion reasons JSON;
- selected score-components JSON;
- selected/top score facts;
- candidate/exclusion counts;
- selected account/tier/name facts.

The full runtime SelectionSnapshot is not itself a persistence requirement.

### Metrics path

The coalescer is intentionally bounded and failure-aware:

- BTreeMap ordering is deterministic;
- max rows and pending event caps are enforced;
- a failed flush merges the batch back into the live buffer, subject to the same caps;
- immediate mode uses the same flush boundary;
- Aggregate is Copy and contains only scalars.

Those semantics must survive exactly. The optimization is ownership movement and statement reuse only.

## 4. Invariants that must not regress

- One SQLite connection/gate/worker.
- Publication transaction still atomically owns request, reservation, attempt, and routing-decision persistence.
- Existing publication fault-injection stages and compensation semantics stay reachable in the same logical order.
- Duplicate publication, prior-attempt-finalization, and durable identity checks remain unchanged.
- Finalization is not delayed or reordered by metrics.
- Metrics write_mode immediate/low_wear behavior remains unchanged.
- Metrics capacity, dropped-event accounting, additive aggregation, flush-failure accounting, and rebuffer-on-failure semantics remain exact.
- BTreeMap deterministic ordering remains; do not switch to HashMap as a performance guess.
- No schema migration, SQL semantic change, public response shape, config key, or dependency.
- Credentials, raw bodies, cache keys, and arbitrary diagnostics remain absent.

## 5. Scope

### In scope

- Add a private prepared routing-decision persistence representation built from a borrowed SelectionSnapshot before database gate acquisition.
- Move JSON serialization and other deterministic formatting that does not require SQLite out of the transaction closure.
- Avoid deep-cloning the complete SelectionSnapshot solely to satisfy the 'static worker closure.
- Preserve the existing observable PublicationError category if precomputation fails.
- Change metrics enqueue to move owned event Strings into MetricKey when possible.
- Consume the taken metrics BTreeMap into flush rows rather than cloning each key field.
- Retain failure recovery with one shared owned row batch (for example Arc<[FlushRow]> or an equivalent no-deep-copy representation).
- Prepare the metrics UPSERT once inside the transaction and execute the prepared statement for each row.
- Add focused equivalence/failure tests and record structural before/after evidence.

### Explicitly out of scope

- Combining publication and finalization transactions.
- Removing durable routing_decisions rows or fields.
- Changing SQL conflict/update semantics.
- Bulk SQL string generation, unbounded VALUES construction, or a new ORM.
- Changing metrics bucket semantics, write cadence, row caps, or retention.
- Changing the database semaphore/gate implementation.
- Adding statement-cache dependencies or globally caching prepared statements.
- Changing checkpoint policy; M001 owns that independently.
- Altering dashboard query paths or schema indexes unless a separate measured query problem is found.

## 6. Required production changes

### 6.1 Prepare routing-decision data before the gate

Introduce a private value such as PreparedRoutingDecisionRow or the smallest equivalent. It should contain only facts insert_attempt_rows actually persists:

- serialized exclusion reasons;
- serialized selected score components or the existing empty-object representation;
- selected score;
- top score and top account;
- eligible/scored/excluded counts;
- selected account ID/name/tier;
- model/provider/protocol/attempt facts already needed by the insert.

Build this from borrowed inputs before Database::with_named_transaction is awaited.

Do not clone the complete SelectionSnapshot into the worker closure once this prepared row exists.

Serialization failures currently emerge from inside the transaction body as a database-transaction failure. Preserve that observable PublicationError::Database category rather than creating a new public error variant. A small crate-private mapping helper is preferable to widening the public enum.

### 6.2 Keep SQLite-dependent checks inside the transaction

Do not move these out merely to shorten the gate:

- existing request lookup;
- duplicate-attempt lookup;
- prior-attempt convergence check;
- account relationship check required for an observed duplicate;
- INSERT/UPDATE operations and last_insert_rowid reads;
- fault-injection points whose meaning is transaction-stage specific.

The optimization target is deterministic preparation, not weakening transactional validation.

### 6.3 Move metric key ownership

record_buffered receives UsageMetricEvent by value. Build the Aggregate from scalar fields, then destructure/move the owned key Strings into the MetricKey rather than cloning them.

Keep canonical bucket normalization behavior identical; if canonicalization replaces bucket_start, move the final owned String.

### 6.4 Consume the flush batch

After std::mem::take of the BTreeMap:

- consume batch.into_iter() to build ordered flush rows;
- move key Strings directly into each row;
- retain Aggregate by value;
- avoid a second deep Vec clone solely so failure recovery can see the rows after the worker closure.

A shared immutable batch such as Arc<[MetricFlushRow]> is acceptable because cloning the Arc is constant-size and the batch remains bounded by existing caps. Do not introduce reference cycles or an async queue.

### 6.5 Prepare the metrics UPSERT once per transaction

Define the SQL once and call connection.prepare (or the equivalent current rusqlite prepared-statement API) once inside the flush transaction. Execute that statement for each row.

Do not dynamically concatenate row values into SQL.

A per-flush prepared statement is sufficient. Do not enlarge scope into a global statement cache unless separate evidence requires it.

## 7. Ordered work packages

### Work package A — Publication prepared row

Intent:

Remove full SelectionSnapshot transfer and JSON/formatting work from the DB-gate-held transaction body.

Required changes:

- add private preparation helper/type;
- preserve serialization error category;
- update insert_attempt_rows to consume prepared facts;
- keep fault stages and SQL ordering stable.

Acceptance evidence:

- coordinator_publication fixtures remain exact;
- routing_decisions persisted JSON parses to the same values and scalar columns match existing expectations;
- duplicate/retry/fault-injection tests remain green;
- source inspection shows no full SelectionSnapshot clone solely for the transaction closure and no serde_json construction/serialization in the routing-decision insert body.

### Work package B — Metrics enqueue ownership

Intent:

Stop copying String key fields from an already-owned event.

Required changes:

- derive Aggregate scalar state before moving key fields;
- move final bucket/provider/model/protocol/status Strings into the map key;
- preserve canonical bucket behavior and caps.

Acceptance evidence:

- operations_o007 aggregation counts/rows remain identical;
- immediate mode and low_wear mode behave identically.

### Work package C — Metrics flush batch ownership

Intent:

Keep one bounded owned representation across DB execution and possible rebuffer.

Required changes:

- consume the BTreeMap into rows;
- share the immutable row batch with the worker closure without deep-cloning row Strings;
- on error, iterate the retained rows and merge exactly as today.

Acceptance evidence:

- forced database failure rebuffer test proves no event duplication/loss beyond existing capacity-drop rules;
- total_received/total_flushed/total_dropped/flush_failures/last_flush_rows remain equivalent.

### Work package D — One prepared UPSERT per flush

Intent:

Reduce SQLite parser/statement setup time while the one gate is held.

Required changes:

- prepare the existing UPSERT once inside the transaction;
- execute it repeatedly with the same params and ordering.

Acceptance evidence:

- all rollup columns and ON CONFLICT additive/min/max behavior are unchanged;
- no schema/index change;
- the transaction count remains one per flush.

## 8. Failure, cancellation, restart, contention semantics

Publication precomputation occurs before durable mutation. If it fails, no SQLite transaction starts and no local claim may be converted as published. Map the failure to the existing publication database-transaction error category so caller retry/compensation behavior does not silently change.

Once publication enters the transaction, cancellation semantics remain governed by the existing retained spawned publication worker and post-commit compensation boundary.

Metrics flush still removes the live batch under the synchronous Mutex only long enough to transfer ownership. The async DB operation must not hold that Mutex. On failure, rebuffer under the same caps and merge rules. A concurrent record_usage call during the flush goes into the new live buffer and must merge correctly when the failed batch returns.

Restart behavior is unchanged: buffered analytics are process memory and durability-critical request state is not deferred into the coalescer.

## 9. Compatibility and migration

No migration.

No public HTTP, CLI, config, Rust API, task name, SQL schema, or persisted JSON schema change.

The exact textual JSON ordering produced by serde_json with preserve_order should remain deterministic for the current input ordering; tests should compare parsed semantic value unless an existing compatibility fixture requires byte equality.

## 10. Required tests

### Publication

- existing coordinator_publication suite;
- existing coordinator_finalization suite as a non-regression owner;
- add/extend a test that exercises multiple exclusions and a selected score, reads routing_decisions, and verifies all persisted scalar/JSON facts;
- retain fault-injection coverage around request/reservation/attempt/routing-decision/before-commit stages;
- verify serialization-preparation failure, if it can be deterministically injected, maps to the existing public error category without a transaction.

### Metrics

Extend rust/tests/operations_o007.rs or focused unit tests to cover:

- multiple events coalescing to one key;
- distinct keys/rows;
- immediate mode;
- max-row/max-pending capacity behavior;
- database failure with concurrent/new buffered events and exact rebuffer merge;
- additive cost/token/byte/latency/min/max/first-byte fields;
- one flush transaction containing many rows.

Timing assertions are not required. Structural removal of the deep clone and repeated prepare is the primary evidence.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
git diff --check
~~~

No dependency change is expected. If Cargo files change unexpectedly, also run cargo deny, cargo tree -e features, cargo tree --duplicates, and the locked release build, then explain why the scope changed.

## 12. Documentation updates

- architecture/deep-dive-database.md — note that deterministic publication-row preparation occurs before gate acquisition if the ownership description benefits from it.
- architecture/deep-dive-metrics.md — record move-based batch ownership and one prepared statement per flush.
- Do not rewrite legacy Plans 230–242.

## 13. Acceptance criteria

- No full SelectionSnapshot clone exists solely to enter the publication transaction.
- Routing exclusion/score JSON serialization no longer runs while the DB gate is held.
- Durable publication rows and fault/retry semantics are unchanged.
- Metrics enqueue moves owned key Strings where possible instead of cloning them.
- Metrics flush owns one bounded row batch; no second deep batch clone remains for failure recovery.
- The metrics UPSERT is prepared once per transaction rather than once per row.
- Rebuffer-on-failure and capacity/drop counters are exact.
- One SQLite gate/worker, schema, config, APIs, and task model remain unchanged.
- Focused/default/no-default validation is green.

## 14. Stop conditions

Stop and report rather than improvise if:

- moving serialization outside the transaction changes caller-visible error classification or claim compensation in a way that cannot be preserved cleanly;
- routing-decision persistence would need to drop fields or weaken duplicate checks;
- metrics failure recovery needs an unbounded copy/queue;
- a new database dependency or schema migration appears necessary;
- statement reuse requires a global cache or connection abstraction change;
- the optimization expands into checkpoint scheduling or dashboard query redesign.

## 15. Closure evidence required

The closure record must include:

- implementation commit(s);
- structural before/after inventory of publication clones/serialization placement;
- structural before/after inventory of metrics key/batch clones;
- confirmation that the metrics SQL statement is prepared once per flush;
- focused publication/finalization/database/operations_o007 results;
- full default/no-default suite results;
- persisted-row equivalence evidence;
- failure/rebuffer equivalence evidence;
- docs changed;
- severity-tagged residual findings.

## 16. Handoff notes

Keep the gate simple. The preferred shape is:

request/routing state
    -> deterministic prepared persistence row
    -> acquire DB gate
    -> only SQLite-dependent validation and writes
    -> commit

and:

bounded metrics BTreeMap
    -> move into one immutable flush batch
    -> acquire DB gate
    -> prepare one UPSERT
    -> execute each row
    -> success drop batch / failure merge batch back

Do not trade a few allocations for a new persistence abstraction.
