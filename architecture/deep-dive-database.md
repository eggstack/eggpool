# Deep Dive: Database Layer

Back to [Overview](overview.md)

## Purpose

SQLite via aiosqlite with WAL mode provides the durable storage layer. A single
connection is serialized by one lock, and each transaction has exactly one
asyncio-task owner. 50+ schema migrations track evolution.

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
    │   Lock + task owner   │
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
- Single-connection serialization via a lock and explicit asyncio-task ownership
- Transaction management: `async with db.transaction():`; same-task nesting is allowed, while inherited child tasks fail before SQL
- Readiness probes: `probe_writable()` with owned transactions
- `Database.vacuum()` — only sanctioned path for VACUUM
- `DatabaseLifecycleState` uses `disconnected`, `connecting`, `ready`,
  `failed_closed`, and `shutting_down`; `failed_closed` is terminal until a
  supervisor restart creates a fresh worker
- `writes_admitted` / `reads_admitted` are cached admission facts used by
  readiness and background writers
- `_safe_rollback()` is a private transaction-owned helper with bounded diagnostics for confirmed rollback and fail-closed rollback failure handling; there is no public rollback escape hatch or test-only injection state.
- Connection invalidation closes read/write admission and invokes the
  process-fatal worker boundary; it does not publish a replacement connection
  into a live request process.
- SQLite failures are classified from SQLite error codes first, then bounded
  message evidence. Busy/locked remains bounded local contention; corruption,
  disk failures, and indeterminate connection state fail closed for restart.

Startup owns the only durable repair path: migrations, `PRAGMA quick_check`,
pending-request/attempt/reservation reconciliation, and the initial writable
probe must all succeed before readiness. Runtime invalidation closes admission,
detaches the connection, invokes the fatal worker handler once, and never
reopens or replaces the connection in process.

### Connection shutdown ownership

`Database.disconnect()` is the final database-user boundary. Production lifespan
shutdown first stops admission and joins generation-owned request,
finalization, and background tasks; it then stops process-owned database users
such as readiness and routing-trace writers before disconnecting statistics and
primary connections. Direct-test fixtures must use `try/finally` to await their
own disconnect on the same canonical event loop. A closed-loop worker warning is
therefore a fixture or task-ownership failure to fix, not a warning to suppress.

### `db/repositories.py`

All repositories in one module:

| Repository | Purpose |
|------------|---------|
| `AccountRepository` | Account CRUD, config sync |
| `RequestRepository` | Request lifecycle (pending → selected → completed) |
| `ReservationRepository` | Quota reservations with release/reconciliation |
| `AttemptRepository` | Per-request attempt tracking |
| `UsageWindowRepository` | Exact timestamped 5h/7d/30d usage snapshots |
| `PriceSnapshotRepository` | Model price snapshots |
| `ProviderRepository` | Provider CRUD and config sync |
| `PingRepository` | Provider health ping results |
| `AccountBackoffRepository` | Upstream-derived backoff persistence |
| `AccountEventRepository` | Account event logging |
| `OperationalEventRepository` | Safety-net task event logging |
| `RoutingDecisionRepository` | Batched routing-decision persistence |

### `db/rollup_repository.py` — UsageRollupRepository

Buffered analytics rollups for performance.

### Direct dispatch persistence

`RequestCoordinator` creates the request, reservation, and attempt rows inside
one caller-owned transaction outside the selection claim lock. Runtime
ownership is published only after commit; statement, rollback, and ambiguous
commit failures raise and release provisional ownership. No placeholder
durable identity is returned.

### Write-path characterization (Plan 137)

The dispatch and finalization write paths are already compact. Each request
lifecycle involves exactly two SQLite transactions:

**Dispatch (before upstream):**

1. In-memory claim under `_selection_claim_lock` — no DB I/O.
2. Single `BEGIN IMMEDIATE` transaction outside the lock:
   `request INSERT or UPDATE` → `reservation INSERT` → `attempt INSERT` → `COMMIT`.
3. Runtime publication under a second lock acquisition — no DB I/O.

**Finalization (after upstream):**

1. Single `BEGIN IMMEDIATE` transaction:
   `request UPDATE WHERE status='pending' RETURNING` →
   `attempt UPDATE WHERE completed_at IS NULL RETURNING` →
   `reservation UPDATE WHERE status='active' RETURNING` → `COMMIT`.

All three finalization mutations use `RETURNING` clauses, eliminating
read-after-write SELECTs. Conditional `WHERE` clauses make duplicate
finalization idempotent. Analytics emission, routing traces, and account
events are post-commit, buffered, or best-effort — they never participate
in the correctness transaction.

**SBC diagnostic write policy:**

- Routing traces: off (`mode = "off"`, `sample_rate = 0.0`).
- Dispatch spans: off (`sample_rate = 0.0`).
- Metrics: `low_wear` mode with 120 s flush; no inline writes.
- Readiness probe: disabled by default.
- Backup: disabled by default.

No redundant reads or writes were found during the audit. Diagnostic fields
are already disabled or sampled under the SBC profile.

### `db/migrations.py` — MigrationRunner

Schema migration execution. Ordered SQL files in `db/schema/`.

### `db/schema/`

53 SQL migration files (`0001_initial.sql` through `0053_remove_attempt_status_analytics_index.sql`), plus `checksums.json`.

## Key Tables

| Table | Purpose |
|-------|---------|
| `requests` | Request lifecycle, usage, cache/compression metrics |
| `request_attempts` | Per-request attempt tracking with provider/model/protocol |
| `routing_decisions` | Routing decisions with score components |
| `accounts` | Account configuration and state |
| `providers` | Provider configuration |
| `models` | Model catalog |
| `provider_model_metadata` | Provider-scoped semantic catalog metadata |
| `account_models` | Account/model support relationships |
| `catalog_refresh_state` | Compact per-account successful catalog freshness |
| `quotas` | Quota reservations |
| `account_backoffs` | Upstream-derived backoff state |
| `account_events` | Account event log |
| `operational_events` | Safety-net task events |
| `provider_pings` | Provider health ping observations |
| `prices` | Model price snapshots |
| `model_info_canonical` | Model metadata sidecar |
| `model_info_observations` | Source observations |
| `model_info_aliases` | Model aliases |
| `model_info_source_health` | Source health tracking |
| `model_info_overrides` | Operator overrides |
| `model_info_match_evidence` | Identity matching evidence |
Historical tuning tables from removed optional features are retained only by
their existing migrations; current runtime code does not write or expose them.

### Analytics index write policy

Request and attempt indexes are fixed schema assets; they are not created or
dropped based on whether the dashboard is enabled. Correctness, recovery,
identity, reservation-expiry, and retention indexes remain indexed. Optional
analytics indexes are kept only when their production query shape materially
benefits from the bounded lookup. The per-attempt `status_code/started_at`
index was removed in migration 0053 because stats aggregates inspect
`status_code` inside expressions rather than filtering on it; provider/model
filtered attempt views and retry time-window scans retain their supporting
indexes. The partial `retry_category IS NOT NULL` alternative was evaluated
but rejected because the production `COALESCE` grouping query does not
naturally use that predicate.

## Key Invariants

- Every DML write must run inside `async with db.transaction():`
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Child tasks cannot inherit transaction ownership
- Migrations are numbered SQL files with checksums
- `EXPECTED_SCHEMA_VERSION` tracked in `scripts/check_database.py`

## Schema Evolution

### Core request schema freeze

The existing `requests` table is frozen for optional diagnostics. New columns
are acceptable only for durable correctness/accounting facts required by
request lifecycle, billing/usage truth, routing repair, or externally visible
compatibility. Feature-specific diagnostics use an existing sparse
diagnostic/event table or a narrowly scoped sidecar keyed by request ID;
disabled features create no sidecar row. Sidecar data follows the existing
retention and redaction policy. Do not introduce a generic EAV/property store.

No migration is required to state this policy, and historical request columns
are not split for cosmetic reasons.

Migrations are additive and non-destructive:
- New columns have sensible defaults
- Legacy callers continue to work
- Pre-migration rows render default values
- Checksums tracked in `checksums.json`

## Dispatch Persistence

Direct persistence is the canonical request-boundary write path:

1. The coordinator publishes provisional claim ownership.
2. It opens one `db.transaction()` outside the claim lock.
3. The transaction creates request, reservation, and attempt identities.
4. Commit completes before runtime quota/active ownership is published.
5. Failure rolls back durable rows and releases provisional ownership.

No process-owned batching writer, queue, or dispatch-writer configuration is
supported. SQLite connection serialization remains the database boundary.

Key invariants:
- No upstream request before dispatch bundle commit
- Every request receives one durable request/reservation/attempt outcome
- Transaction failure fails closed before upstream dispatch
- Claim compensation releases provisional ownership exactly once

## WAL Residue (Plan 137)

WAL files grow with write activity. SQLite checkpoints truncate them, but the
default WAL file size limit is unbounded. On storage-constrained SBCs, an
unbounded WAL can consume significant microSD space before a passive
checkpoint runs.

The `journal_size_limit` pragma bounds the WAL file size after each checkpoint.
When set, SQLite truncates the WAL to this size (or smaller) after a
`PRAGMA wal_checkpoint(PASSIVE)`. The pragma does not change checkpoint
cadence or synchronous mode; it only caps residual WAL size.

Default behavior: `None` (unbounded) for workstation installs. The SBC
profile sets `journal_size_limit = 67108864` (64 MiB), which provides
headroom for normal operation while bounding steady-state WAL consumption.

The pragma is safe with `synchronous=NORMAL` and WAL mode: it does not alter
durability semantics. The checkpoint itself is already passive and
non-blocking (runs in background cleanup).

## Database Lifecycle Clarity (Plan 137)

`DatabaseLifecycleState` tracks the connection lifecycle. The transitions
are:

```
DISCONNECTED → CONNECTING → READY → SHUTTING_DOWN
                                     FAILED_CLOSED (from any active state on error)
```

`_transition_state()` is a diagnostic setter — the caller's invariants
(locks, writes-admitted flag) are set independently. The only enforced
invariants are:

- `FAILED_CLOSED` is terminal for this instance (a supervisor restart
  creates a fresh `Database`).
- `SHUTTING_DOWN` is terminal-ish (no return to `READY`).

Error handling transitions to `FAILED_CLOSED` from `CONNECTING`, `READY`, or
`SHUTTING_DOWN`. This is intentional: a corrupted or invalidated connection
must close admission regardless of the current state.

The `_invalidate_connection()` method detaches the connection, records the
failure reason, and transitions to `FAILED_CLOSED`. It does not attempt
reconnection — the deployment contract is a worker restart.

## Schema Baseline Decision (Plan 137)

The project currently runs 53 numbered SQL migrations. A future 1.0 release
could establish a migration baseline: replace the full migration chain with a
baseline schema snapshot and a bridge strategy for pre-1.0 installs.

**Decision for this phase:** Document but do not execute. The baseline
policy should be:

1. Once the compatibility policy permits, replace indefinite pre-1.0
   migration archaeology with a baseline/bridge strategy.
2. Do not destructively squash migrations while current installs still
   need them.
3. The baseline snapshot should be the schema state at the 1.0 release
   tag.
4. A bridge migration applies the delta from the last pre-1.0 checksum
   to the baseline, allowing fresh installs to skip the full chain.

This is deferred until the 1.0 compatibility policy is finalized. No
destructive migration changes are made in this phase.
