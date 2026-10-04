# Persistence Milestone 010 — Control/Projection Storage Boundary Architecture Investigation

Status: implemented

Repository baseline: `3efd650e63eef4999e450bdef738af9e54971d4b`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-010--controlprojection-storage-boundary-architecture-investigation`

Evidence lineage:

- `plans/closure/persistence/003-status.md` — same-gate event-assisted checkpointing rejected after foreground tail transfer
- `plans/closure/persistence/004-status.md` / `005-status.md` — timer-only checkpoint strategy rejected/corrected on Pi 5/MMC
- `plans/closure/persistence/007-pi5-qualification.md` — separate PASSIVE worker removed foreground latency but failed WAL convergence
- `plans/closure/persistence/008-status.md` — PERSIST/EXTRA rejected; candidate p95 155–160 ms in primary runs and worker-write proxy about 2.04× WAL/NORMAL control
- `artifacts/qualification/m008-pi5-2026-10-03/` — current target-class storage/write evidence

Long-term requirements:

- `plans/000-long-term-specification.md` — persistence integrity, bounded resources, compatibility, fail-closed ownership
- `plans/001-terminology-and-domain-model.md` — durable request/attempt/reservation ownership
- `plans/002-long-term-roadmap.md` — persistence and operations direction
- `plans/003-planning-process.md` — ADR threshold, evidence-gated architecture, bounded handoff

Applicable ADRs:

- No storage-split ADR is accepted at M010 start.
- A control database + outbox + analytics/history database is a new durable storage authority/protocol and crosses the ADR threshold.
- M010 may produce `plans/adrs/ADR-0002-persistence-control-projection-boundary.md` with **Status: proposed** only if the investigation converges on a concrete viable contract.
- M010 MUST NOT mark that ADR accepted or implement the production split. Acceptance and production implementation require explicit later direction and separate milestones.

Primary class: infrastructure

## 1. Objective

Determine whether EggPool should separate latency-critical correctness persistence from replayable observability/history persistence, and if so define a concrete, bounded control-database/outbox/analytics contract that is safe enough to propose as an ADR.

The investigation is driven by the closed physical evidence:

- keeping WAL/NORMAL preserves very fast common-case commits but retains rare foreground checkpoint stalls;
- moving PASSIVE checkpointing to another worker removes the foreground tail but cannot keep WAL progress bounded on the Pi/MMC target;
- replacing WAL with PERSIST/EXTRA removes checkpointing but makes ordinary publication/finalization commits tens of milliseconds and roughly doubles the measured worker-write proxy.

M010 therefore stops treating checkpoint placement or journal selection as the primary variable. It asks whether the critical transaction is writing too much state, too many indexes, or too much observational history synchronously.

M010 is architecture research and qualification only. Production remains the current monolithic schema and WAL/NORMAL runtime throughout this milestone.

## 2. Current repository facts that constrain the design

### 2.1 Publication is mixed correctness + observability

Current publication synchronously:

1. creates or validates the durable `requests` row;
2. inserts an active `reservations` row;
3. inserts a `request_attempts` row;
4. inserts a rich `routing_decisions` row with multiple secondary indexes.

The first three participate in identity, retry ordering, reservation ownership, compensation, finalization, and crash recovery. `routing_decisions` is observability/history, but its current contract intentionally commits beside the attempt so traces cannot disagree with the selected attempt.

A future split must preserve that consistency through a durable transactional projection record, not by making routing history best-effort.

### 2.2 Finalization is also mixed

Current finalization synchronously:

- reads and conditionally terminalizes the request;
- writes correctness status/identity convergence;
- writes a large set of request usage, latency, cache, cost, protocol, and observability fields;
- terminalizes the attempt with status/error/retry/byte/latency facts;
- releases the reservation.

`requests` and `request_attempts` therefore cannot be classified wholesale as “analytics tables.” M010 must classify at field/consumer level and derive the minimum durable correctness representation rather than assuming whole-table movement.

### 2.3 Crash recovery depends on control rows

Startup reconciliation uses pending `requests`, open `request_attempts`, and active `reservations` as authoritative crash evidence. A split that makes any of those transitions eventual would regress correctness.

### 2.4 The read plane is broad

Dashboard/stats repositories read:

- completed `requests`;
- `request_attempts`;
- `routing_decisions`;
- `usage_rollups`;
- active `reservations`;
- provider pings and operational events.

Some endpoints combine live correctness state (for example active reservations) with historical usage. The design must identify which reads stay on control storage and which may tolerate projection lag.

### 2.5 Backup currently assumes one database file

Backup format v1 snapshots one `usage.sqlite3` through SQLite's online backup API and restores it atomically with config/env state. A two-database design changes backup/restore authority and cannot be treated as an implementation detail.

### 2.6 M008 attribution is directional, not table-level

The accepted M008 attribution cohort shows publication dominates foreground worker writes and PERSIST/EXTRA worsens both publication and finalization write volume. It does not identify individual table/index cost and is not physical NAND-wear evidence.

M010 may add bounded test-only attribution needed to compare candidate transaction shapes, but it must not reinterpret the M008 proxy.

## 3. Candidate architecture to investigate

The primary candidate is:

~~~text
foreground request
      |
      v
+-------------------------+
| control SQLite database |
|-------------------------|
| request correctness     |
| attempt correctness     |
| reservation ownership   |
| config/catalog state    |
| durable projection      |
| outbox                  |
+-------------------------+
      |
      | same transaction:
      | control mutation + outbox append
      v
durable response boundary
      |
      | asynchronous bounded replay
      v
+-------------------------+
| analytics/history DB    |
|-------------------------|
| routing decisions       |
| rich completed history  |
| dashboard projections   |
| usage rollups           |
| operational history     |
+-------------------------+
~~~

This is a hypothesis, not a decision.

The investigation must compare at least:

### Option A — Retain current monolithic authority

Baseline. One database remains both correctness authority and analytics history.

### Option B — Same-database logical split only

Separate correctness and analytics tables/columns but keep one SQLite database and one foreground transaction.

This may reduce index fanout but does not isolate WAL/checkpoint traffic. Quantify whether it offers enough structural benefit to justify simpler follow-up work.

### Option C — Control DB + durable outbox + separate analytics/history DB

Correctness mutation and one bounded outbox record commit atomically in the control DB. One process-owned projector applies those events to a separate analytics database asynchronously.

This is the main candidate.

### Explicit non-candidates

Do not treat these as viable M010 solutions:

- synchronous writes to two independent SQLite databases from the request path;
- `ATTACH` as a substitute for a durable outbox/crash model;
- distributed transaction coordination;
- WAL2 or a SQLite fork;
- lossy in-memory-only analytics queue;
- “fire and forget” routing/attempt history that may silently disappear after a successful request;
- moving active reservation or retry-order state out of the synchronous control transaction.

## 4. Required table, field, index, and consumer inventory

Produce an authoritative matrix covering every current persisted table and every index that is maintained by request-path or frequent background writes.

For each table classify it as one of:

1. **control-authoritative** — required synchronously for request identity, admission, retry, finalization, recovery, catalog/config authority, or another correctness invariant;
2. **mixed** — contains both correctness and historical/observability fields; requires field-level decomposition before movement;
3. **projection candidate** — derivable from a durable event/control mutation and not needed synchronously for correctness;
4. **derived aggregate** — recomputable/buffered analytics such as rollups;
5. **independent operational state** — not request-path history and should not be pulled into the split without evidence.

At minimum inspect:

- `accounts`, `models`, `account_models`, provider/model metadata;
- `requests`;
- `request_attempts`;
- `reservations`;
- `routing_decisions`;
- `usage_rollups`;
- `provider_pings`;
- `operational_events`;
- `account_events`;
- model pricing/history/quarantine tables;
- retention/cleanup indexes.

For every mixed or candidate field identify all consumers:

- publication/retry;
- finalization;
- compensation;
- startup recovery;
- quota/usage hydration;
- routing;
- dashboard/stats/trace APIs;
- retention;
- backup/restore;
- migrations/tooling/tests.

Do not infer “analytics” from a column name. A field moves only when no correctness path depends on synchronous visibility.

## 5. Minimum control-state derivation

Derive the smallest control representation that preserves all existing correctness behavior.

The analysis must answer explicitly:

### Requests

Which fields are needed synchronously for:

- proxy-request idempotency;
- pending/terminal state;
- retry identity;
- startup reconciliation;
- finalization conflict detection;
- quota/account/model association?

Which terminal usage/cache/cost/latency fields can instead be emitted through projection events?

### Attempts

Which attempt fields are needed synchronously for:

- attempt-number uniqueness/order;
- “prior attempt finalized before retry” enforcement;
- finalization idempotency/conflict checks;
- startup recovery;
- reservation relationship?

Which rich error/latency/trace facts are projection candidates?

### Reservations

Assume active/released/expired ownership remains control-authoritative unless repository evidence disproves it.

### Routing decisions

Treat the existing row as a leading projection candidate, but preserve its atomic relationship with the attempt through the outbox event committed beside the attempt.

The selected candidate must not weaken the observable invariant merely because the physical table moves.

## 6. Durable outbox contract

If Option C remains viable, define a concrete outbox protocol.

At minimum specify:

- monotonic `event_id`;
- event schema/version;
- event kind;
- stable request/attempt keys;
- payload size bound;
- which payload fields are copied versus referenced;
- same-transaction insertion with control mutation;
- projector ordering rules;
- idempotent analytics application;
- analytics applied-cursor storage;
- at-least-once replay semantics;
- duplicate replay behavior;
- deletion/reclamation watermark;
- bounded batch size;
- retry/backoff;
- cancellation/shutdown ownership;
- observability counters;
- schema-version compatibility during upgrade/downgrade.

The projector's apply transaction should atomically update projection rows and its cursor so a crash after apply but before caller acknowledgement replays harmlessly.

Do not require exactly-once message delivery. Require exactly-once **effect** through idempotent apply and durable cursor semantics.

## 7. Bounded-resource and outage policy

A durable outbox creates a new failure mode: analytics storage may be unavailable while control writes continue.

M010 must model and decide among explicit policies rather than leaving the queue unbounded.

The decision matrix must cover:

- analytics DB unavailable for minutes/hours/days;
- projector repeatedly failing one poison event;
- disk approaching a hard limit;
- outbox count/bytes above soft and hard thresholds;
- control DB healthy while analytics DB is corrupt;
- retention cleanup racing projector progress.

No candidate is viable if it requires:

- unbounded outbox growth;
- unbounded in-memory queueing;
- silently dropping history required by current public APIs;
- blocking every request on analytics DB availability.

If preserving full historical parity and bounded disk cannot coexist without request-path blocking, record that as a design blocker rather than hiding it behind a large default cap.

The investigation may recommend bounded compaction/snapshot techniques only if replay and API equivalence are demonstrated.

## 8. Backup and restore model

A viable Option C must define recoverable two-file backup semantics before it can become a proposed ADR.

Evaluate at least:

### Strategy 1 — Analytics DB is fully rebuildable

Back up only control storage and retain enough durable projection history to rebuild analytics from zero.

Quantify retention/storage cost. Reject this strategy if indefinite event retention recreates the original storage problem.

### Strategy 2 — Coordinated control + analytics snapshots with outbox tail

Pause the projector/reclaimer at a durable applied cursor, snapshot analytics, snapshot control while the required outbox tail remains retained, record cursor/schema metadata in the archive, and on restore replay control events newer than the analytics cursor.

Specify:

- what is paused;
- whether request traffic may continue;
- required ordering;
- archive format/version implications;
- crash behavior during backup;
- restore validation;
- downgrade/rollback;
- what happens when one snapshot is corrupt/missing.

Do not claim cross-database atomicity from SQLite `ATTACH`/WAL.

Backup format v1 remains production authority during M010.

## 9. Dashboard/read consistency model

Inventory every endpoint currently backed by `DashboardRepository`, `UsageRollupRepository`, request/attempt repositories, and active reservation queries.

For each classify required consistency:

- **strong/live** — must read control state synchronously;
- **eventually consistent** — may read analytics projection with a bounded lag;
- **composed** — combines control and analytics values.

Define:

- maximum acceptable projection lag for eventually consistent views;
- behavior while analytics is unavailable;
- whether existing response schemas can remain unchanged;
- how trace endpoints avoid exposing an attempt before its projection appears;
- whether a bounded “projection lag” diagnostic is needed.

M010 must not change HTTP/dashboard contracts. Any user-visible degraded/freshness semantic that would change compatibility belongs in the ADR and later implementation plan.

## 10. Migration and rollback investigation

Define a production migration strategy without implementing it.

At minimum cover:

1. existing schema-54 monolithic database;
2. creation of control/outbox and analytics storage;
3. historical backfill;
4. shadow projection and parity comparison;
5. cutover of read paths;
6. removal of projection-only request-path writes from the control DB;
7. rollback before and after cutover;
8. upgrade/downgrade compatibility;
9. retention cleanup during migration.

Prefer a staged migration where new projection machinery can be verified before any old durable write is removed.

Do not plan a “flag day” destructive migration unless every safer staged option is disproven.

## 11. Test-only transaction-shape prototype

M010 should not rely on prose alone. Build a non-production prototype that compares the current transaction shape with the selected candidate shape.

Preferred location:

- `rust/tests/persistence_projection_architecture.rs`, or
- a dedicated test/qualification module with no default-runtime entry point.

Requirements:

- no production migration;
- no public config;
- no new crate dependency;
- temporary file-backed SQLite databases only;
- schema derived explicitly from current production columns/indexes relevant to the measured transaction;
- baseline models current publication/finalization write families;
- candidate models the proposed minimum control mutation + outbox append;
- analytics apply is measured separately, never counted as foreground latency;
- report statement/table/index mutation counts;
- record WAL/file-byte delta and, on Linux where already-supported instrumentation is reusable without unsafe, worker/process write-byte proxy;
- fixed deterministic workload;
- no claim that desktop/local timings predict Pi/MMC request latency.

The prototype must also exercise:

- duplicate event replay;
- projector crash after projection write but before cursor acknowledgement;
- restart and replay;
- out-of-date analytics cursor;
- poison-event handling policy;
- outbox reclamation only after durable apply;
- control correctness when analytics DB is absent.

If the candidate does not reduce foreground write/index fanout structurally, close the architecture path as not justified rather than scheduling physical performance work.

## 12. Failure-state matrix

Document deterministic outcomes for at least:

- crash before control transaction commit;
- crash after control commit before projector wake;
- crash while reading outbox;
- crash during analytics apply before commit;
- crash after analytics commit before the next loop;
- duplicate delivery;
- projector cancellation during shutdown;
- analytics DB locked/busy;
- analytics DB corrupt/unopenable;
- control DB recovery while analytics lags;
- backup during lag;
- restore with analytics cursor behind control;
- restore with invalid cursor/ahead-of-control state;
- schema upgrade with old outbox events pending.

For each identify authoritative state, retry action, data-loss risk, and whether inference may continue.

## 13. Security and privacy

The split must not increase retained sensitive content.

The inventory and proposed event schema must explicitly prove:

- no prompts/request bodies;
- no credentials/API keys;
- no raw provider bodies;
- no filesystem paths;
- no cache keys that are not already allowed durable metadata.

Preserve current bounded/sanitized error-detail policy.

Both database files, temporary files, backups, and restore staging must retain current private-file permissions and path validation.

Do not make the analytics DB remotely accessible.

## 14. Ordered work packages

### Work package A — Persistence ownership inventory

Produce:

`architecture/persistence-control-projection-investigation.md`

Include table/field/index/consumer matrix and current critical-path write graph.

### Work package B — Candidate contracts

Specify Options A/B/C, minimum control state, outbox event contract, projector ownership, read consistency, outage/backpressure, and backup/restore semantics.

Reject any candidate that cannot satisfy bounded-resource and recovery invariants.

### Work package C — Test-only prototype

Implement the deterministic current-vs-candidate transaction-shape prototype and replay/failure tests.

Keep it isolated from production code unless a small reusable pure data type is strictly necessary; if production code would need to change merely to run the experiment, stop and re-plan.

### Work package D — Evidence synthesis

Use:

- M007/M008 physical evidence;
- schema/index fanout inventory;
- prototype foreground write deltas;
- replay/recovery results;
- backup/read-plane analysis.

Produce a decision matrix with explicit advantages, costs, unresolved risks, and expected follow-up work.

### Work package E — ADR disposition

If one candidate is coherent enough for later implementation, write:

`plans/adrs/ADR-0002-persistence-control-projection-boundary.md`

with `Status: proposed`.

The proposed ADR must define:

- control authority;
- analytics authority;
- outbox/projector protocol;
- bounded backlog policy;
- backup/restore;
- consistency/freshness;
- migration/rollback;
- security;
- verification required for production adoption.

Do not mark it accepted in M010.

If no candidate satisfies the invariants, do not manufacture an ADR recommendation; close M010 with the rejection evidence.

### Work package F — Closure and successor audit

Write:

`plans/closure/persistence/010-status.md`

Valid dispositions:

1. **architecture candidate proposed** — ADR-0002 proposed; no production implementation registered automatically;
2. **more evidence required** — name the exact missing evidence and a narrower future experiment;
3. **split rejected** — structural/failure/backup cost does not justify the design.

The closure must audit the registry. No implementation milestone becomes ready merely because M010 proposes an ADR.

## 15. Non-regressing invariants

During M010:

- production remains one SQLite database/connection/gate/worker on WAL/NORMAL;
- current schema/migration ledger remains unchanged;
- no production second connection or projector task;
- publication/finalization transactions remain unchanged;
- no dashboard/API/config/wire behavior change;
- backup format v1 remains unchanged;
- no dependency change;
- no qualification feature is enabled in release/package workflows;
- M007/M008 evidence remains immutable;
- any prototype uses temporary isolated databases only.

## 16. Required verification

At minimum:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test persistence_projection_architecture -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
~~~

If the prototype uses an existing test target/module instead of the preferred target name, record the exact substitution in closure evidence.

Any optional physical Pi run must be labeled supplemental. M010 does not claim a target performance win; production implementation, if later authorized, must own physical target acceptance.

## 17. Acceptance criteria

M010 closes successfully as an architecture investigation only when all of the following exist:

- exhaustive persistence table/field/index/consumer classification;
- explicit minimum correctness/control-state definition;
- current-vs-candidate critical-path write graph;
- outbox ordering/idempotency/reclamation protocol;
- bounded backlog/outage policy;
- backup/restore contract;
- dashboard/read consistency contract;
- migration/rollback strategy;
- deterministic failure matrix;
- test-only prototype with current and candidate transaction shapes;
- quantified foreground statement/index/write-proxy deltas;
- duplicate/crash/replay tests;
- no production behavior/schema/config/API diff;
- explicit disposition: propose candidate, request narrower evidence, or reject split.

If a candidate is proposed, ADR-0002 must remain `proposed` at closure.

## 18. Stop conditions

Stop and report rather than silently widening if:

- correctness requires synchronous commit to both databases;
- the only workable design depends on unbounded outbox retention;
- current public history semantics require silently lossy projection to stay bounded;
- active reservation/retry/recovery state would become eventual;
- backup consistency cannot be defined without stopping request traffic for an unbounded interval;
- migration requires destructive flag-day replacement;
- prototype requires modifying production transaction code;
- a new external database/dependency is introduced;
- a public config/API change is required merely to evaluate the architecture;
- the investigation turns into implementation of the production split.

Those outcomes should close M010 with a blocker/rejection and inform a new plan.

## 19. Closure evidence required

`plans/closure/persistence/010-status.md` must contain:

- implementation/research commit(s);
- inventory coverage summary;
- selected/rejected table and field boundaries;
- index/write-fanout comparison;
- prototype design and results;
- replay/failure test matrix;
- bounded backlog decision;
- backup/restore decision;
- read consistency/freshness decision;
- migration/rollback decision;
- security/privacy review;
- default/no-default/test/Clippy/release results;
- local versus physical evidence labels;
- severity-tagged unresolved findings;
- ADR-0002 status if created;
- recommendation;
- unblock/successor audit;
- explicit statement that production remains monolithic WAL/NORMAL.

## 20. Handoff note

Do not start by creating a second production database.

Start by proving the ownership boundary. `requests` and `request_attempts` are mixed tables, while `reservations` is clearly correctness-sensitive and `routing_decisions` is the cleanest initial projection candidate. The design is only worthwhile if one atomic control transaction can replace several synchronous history/index mutations with a bounded outbox append while preserving retry, finalization, recovery, trace fidelity, backup, and bounded-resource behavior.

The output of M010 is a decision-quality architecture contract, not a storage migration.
