# Foundation Milestone F004 — SQLite Schema and Repository Compatibility Baseline

Status: closed; see [closure record](../../closure/foundation/004-status.md)

Repository baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

Source roadmap: `migration-rs/subsystems/foundation-roadmap.md#F004`

Primary class: invariant/infrastructure

## 1. Objective

Establish Rust access to the existing EggPool SQLite database without schema fork/reset: reuse migrations/checksums, preserve transaction/durability semantics, and port the minimal repository interfaces required by the next HTTP/SSR read-plane milestone.

## 2. Dependencies

Hard: F001/F002. Interface dependency from F003: database path/default config structure may be used once stable, but F004 can use explicit paths in tests.

## 3. Python oracle evidence

Primary sources: `src/eggpool/db/connection.py`, migrations runner/schema/checksums, repositories/rollup repository, architecture deep-dive database docs, startup integrity/crash reconciliation tests, and current SQL files.

## 4. Invariants

- existing SQL migration files remain canonical during this milestone;
- Rust does not renumber/rewrite historical migrations;
- Python-created DBs open in Rust;
- Rust writes use the same units/state/nullability semantics;
- WAL/busy timeout/synchronous behavior follows config/current contract;
- transactions retain `BEGIN IMMEDIATE` behavior where Python relies on it;
- serialized ownership prevents accidental concurrent use patterns that violate current SQLite assumptions;
- migration checksum mismatch fails closed;
- no ORM/schema generator becomes a second source of truth.

## 5. Scope

### In scope

`rusqlite` plus a minimal async serialization layer such as `tokio-rusqlite` unless repository evidence justifies a different approach; migration runner/checksum validation; connection pragmas; transaction helper; typed row/domain conversions needed by first read-plane; initial repositories for health/stats/dashboard bootstrap; DB observation fixtures.

### Out of scope

Porting every statistics query, finalization, quota reservation, background maintenance, backup/recovery, or runtime corruption policy. Those are later milestones unless a minimal startup invariant requires a small piece now.

## 6. Required production changes

Rust should reference/copy the existing migration SQL in a way that prevents silent drift. Prefer one canonical migration content source during side-by-side development; if packaging requires Rust-local embedded copies, add a hash/equality guard against the Python-era canonical files until cutover.

The async boundary should serialize work on one connection/worker consistent with current EggPool assumptions rather than opening many pooled SQLite writers for throughput that the product does not need.

## 7. Work packages

A. Inventory schema/migration/checksum contract.

B. Implement open/pragmas/serialized call/transaction primitives.

C. Implement migration discovery/application/checksum verification.

D. Port minimal typed repositories needed for F005 read surfaces.

E. Add Python→Rust fixture compatibility and safe Rust→Python rollback fixture.

F. Add corrupt/checksum/busy transaction negative cases.

## 8. Failure/restart/contention

A failed migration transaction must leave the DB in the prior valid state. Busy/locked outcomes must be bounded and typed. A process crash during a transaction relies on SQLite atomicity/WAL; startup validation must detect incompatible or indeterminate state according to existing policy. Do not create an in-memory repair mechanism as a substitute for durable recovery.

## 9. Compatibility/migration

No schema version increment is expected for this milestone. Any need for new schema means stop and create a separate migration plan/ADR if it changes rollback assumptions.

## 10. Tests

- open current empty/populated Python fixture;
- exact migration inventory/checksums;
- apply migrations from older representative fixture;
- idempotent current-schema startup;
- `BEGIN IMMEDIATE`/rollback behavior;
- WAL/pragmas;
- row type/unit/null parity for ported repositories;
- checksum corruption refusal;
- bounded busy behavior;
- Rust-created compatible rows readable by Python;
- no unintended schema diff after Rust open/close.

## 11. Verification commands

Rust fmt/clippy/test; migration DB differential suite; targeted Python DB migration/repository tests. Use temporary databases only.

## 12. Documentation

Document Rust DB ownership model, canonical migration source, rollback compatibility window, and which repositories remain unported.

## 13. Acceptance criteria

Rust can safely open a real current EggPool DB copy, validate schema, execute the ported read/write repository operations, and leave it consumable by Python without schema reset or semantic drift.

## 14. Stop conditions

Stop if a new schema is required, if current migration files cannot be reused safely, or if an async library forces connection semantics incompatible with EggPool's serialized ownership.

## 15. Closure evidence

Schema diff showing none, migration/checksum parity, Python→Rust→Python fixture results, contention/rollback outcomes, dependency delta, verification outputs.

## 16. Handoff notes

Favor explicit SQL and typed conversion over an ORM. Performance tuning belongs after correctness parity.
