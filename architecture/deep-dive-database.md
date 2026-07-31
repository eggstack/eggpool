# Deep Dive: Database Layer

Back to [Overview](overview.md)

## Purpose

SQLite via aiosqlite with WAL mode provides the durable storage layer. Single-connection serialization via a lock + ContextVar ensures correctness. 50+ schema migrations track evolution.

## Architecture

```
┌─────────────────────────────────────┐
│           Application               │
│  repositories/ services/ coordinators│
└──────────────┬──────────────────────┘
               │
    ┌──────────▼──────────┐
    │   Database (aiosqlite)│
    │   WAL mode           │
    │   Single-conn serial  │
    │   Lock + ContextVar   │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │   SQLite File        │
    │   eggpool.db         │
    └─────────────────────┘
```

## Key Modules

### `db/connection.py` — Database

aiosqlite wrapper with:
- WAL mode enabled
- Single-connection serialization via lock + ContextVar
- Transaction management: `async with db.transaction(ambiguous_operation=...):`; ambiguity metadata belongs to the lock-owning transaction
- Readiness probes: `probe_writable()` with owned transactions
- `Database.vacuum()` — only sanctioned path for VACUUM
- Plan 027: `DatabaseLifecycleState` enum with explicit states
  (`disconnected → connecting → ready → invalidating → invalidated →
  recovering → reconciling → ready / failed_closed → shutting_down`)
- Plan 027: `connection_epoch` incremented on every successful `connect()`
  for epoch-tracking in long-lived components
- Plan 027: `writes_admitted` / `reads_admitted` cached admission facts
- Plan 027: `_safe_rollback()` helper with bounded diagnostics
- Plan 027: `AmbiguousDatabaseOperation` frozen dataclass for
  indeterminate commit outcomes
- Plan 060: connection opening and public admission are separate; recovery
  candidates remain closed to public reads/writes until schema verification,
  a private writable probe, and reconciliation complete
- Plan 060: ambiguity buffers acknowledge operations individually only after
  convergence; unresolved results remain queued and capacity overflow fails
  closed rather than evicting older work

### `db/recovery.py` — DatabaseRecoveryController

Plan 027: Process-owned single-flight recovery controller.

- Receives invalidation notifications from `Database`
- Stops admission of new correctness-critical writes
- Marks readiness false for the duration of recovery
- Detaches and closes the suspect connection with bounded timeout
- Opens an unadmitted fresh connection and re-runs migrations (in-memory DBs)
  or verifies schema compatibility (file-backed DBs)
- Runs a private writable probe to confirm the replacement connection is usable
- Reconciles ambiguous operations via built-in reconcilers; only durable
  convergence is acknowledged and unresolved operations remain queued
- Retries with bounded exponential backoff (`[database.recovery]` config)
- Single-flight: concurrent callers join the same recovery attempt
- `RecoverySnapshot` for diagnostics (state, attempts, waiters, reasons)

### `db/repositories.py`

All repositories in one module:

| Repository | Purpose |
|------------|---------|
| `AccountRepository` | Account CRUD, config sync |
| `RequestRepository` | Request lifecycle (pending → selected → completed) |
| `ReservationRepository` | Quota reservations with release/reconciliation |
| `AttemptRepository` | Per-request attempt tracking |
| `UsageWindowRepository` | Aggregated cost queries (5h/7d/30d) |
| `PriceSnapshotRepository` | Model price snapshots |
| `ProviderRepository` | Provider CRUD and config sync |
| `PingRepository` | Provider health ping results |
| `AccountBackoffRepository` | Upstream-derived backoff persistence |
| `AccountEventRepository` | Account event logging |
| `OperationalEventRepository` | Safety-net task event logging |
| `RoutingDecisionRepository` | Routing decision persistence |

### `db/rollup_repository.py` — UsageRollupRepository

Buffered analytics rollups for performance.

### `db/dispatch_repository.py`

`persist_dispatch_bundles()` — durable dispatch write pipeline (Milestone C). Batches multiple dispatch intents in a single transaction.

The dispatch repository validates every intent before opening the transaction.
Its contract is binary: successful calls return one fully populated
`PersistedDispatchResult` per input, in input order; statement, rollback, and
unknown-commit failures raise and never return placeholder rows. A persisted
result requires non-empty request and reservation IDs and a positive attempt ID.
The writer propagates one failed-batch exception to every waiting caller, so
failed intents do not increment persisted counters and later batches can
continue after deterministic failures.

### `db/migrations.py` — MigrationRunner

Schema migration execution. Ordered SQL files in `db/schema/`.

### `db/schema/`

51 SQL migration files (`0001_initial.sql` through `0051_model_quarantine.sql`), plus `checksums.json`.

## Key Tables

| Table | Purpose |
|-------|---------|
| `requests` | Request lifecycle, usage, cache/compression metrics |
| `request_attempts` | Per-request attempt tracking with provider/model/protocol |
| `routing_decisions` | Routing decisions with score components |
| `accounts` | Account configuration and state |
| `providers` | Provider configuration |
| `models` | Model catalog |
| `quotas` | Quota reservations |
| `account_backoffs` | Upstream-derived backoff state |
| `account_events` | Account event log |
| `operational_events` | Safety-net task events |
| `pings` | Provider health pings |
| `prices` | Model price snapshots |
| `model_info_canonical` | Model metadata sidecar |
| `model_info_observations` | Source observations |
| `model_info_aliases` | Model aliases |
| `model_info_source_health` | Source health tracking |
| `model_info_overrides` | Operator overrides |
| `model_info_match_evidence` | Identity matching evidence |
| `compression_tuning_recommendations` | Tuning recommendations |

## Key Invariants

- Every DML write must run inside `async with db.transaction():`
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Child tasks cannot inherit transaction ownership
- Migrations are numbered SQL files with checksums
- `EXPECTED_SCHEMA_VERSION` tracked in `scripts/check_database.py`

## Schema Evolution

Migrations are additive and non-destructive:
- New columns have sensible defaults
- Legacy callers continue to work
- Pre-migration rows render default values
- Checksums tracked in `checksums.json`

## Dispatch Persistence (Milestone C)

Replaces per-request correctness-critical dispatch transactions with process-owned microbatching:
1. Coordinator builds `DispatchIntent`
2. Enqueues to `DispatchPersistenceWriter`
3. Writer collects batches (bounded size + wait time)
4. Single `db.transaction()` per batch via `persist_dispatch_bundles()`
5. On failure, entire batch rolls back
6. Each intent's future resolves with `PersistedDispatchResult`

Key invariants:
- No upstream request before dispatch bundle commit
- Every intent receives exactly one outcome
- Queue saturation fails closed
- Isolated requests don't incur unconditional batching sleep
- A dispatch writer is bound to the event loop captured by `start()`; cross-loop
  submission is rejected rather than bridged.
