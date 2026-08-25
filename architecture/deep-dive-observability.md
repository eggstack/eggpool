# Deep Dive: Observability

Back to [Overview](overview.md)

## Purpose

The observability module provides routing trace persistence for debugging and dashboard drill-down.

## Module Structure

```
src/eggpool/observability/
├── __init__.py
└── routing_trace_writer.py   # Background trace writer (~410 lines)
```

## Key Components

### Routing Trace Writer (`routing_trace_writer.py`)

A process-owned, single-drain-task writer (~410 lines) that persists routing decision traces for debugging and dashboard drill-down.

**Design:**
- **Bounded queue**: `collections.deque(maxlen=queue_capacity)` drops the *newest* event when full (checked before append)
- **Thread-safe submission**: `submit()` uses `threading.Lock` so callers from any thread or event loop can safely enqueue
- **Single drain task**: one long-running coroutine pulls from the queue and writes bounded batches to the database
- **Silent failures**: every exception is swallowed and its counter incremented — the writer never raises

**Data flow:**
```
Router._select_account()
    → RoutingTraceEvent (immutable)
    → RoutingTraceWriter.submit()
    → bounded queue
    → drain task
    → RoutingDecisionRepository.create_many()
    → SQLite routing_decisions table
```

**Trace events capture:**
- Selected account and provider
- Score breakdown from `QuotaFairScorer`
- Eligibility gate results
- Tier and position within tier
- Timestamps and latency

**Integration points:**
- `routing/eligibility.py` emits gate-status events
- `routing/router.py` emits final selection events
- `request/coordinator.py` submits trace events after routing
- Dashboard `/routing` page displays trace data
- JSON API `GET /api/stats/routing` exposes trace summaries

## Configuration

```toml
[routing.trace]
mode = "off"             # Opt-in; default off ("off" | "sampled" | "all")
# queue_capacity = 1000  # Bounded queue size (default 1000)
```

Traces are opt-in — the writer is not constructed when `mode = "off"`. This is part of the lean default profile: no routing traces unless the operator explicitly enables them.

## Key Invariants

- Trace writing is fire-and-forget — never blocks the request path
- Bounded queue drops newest events when full — no memory pressure
- Silent failure mode — trace write failures never affect request handling
- Writer is process-owned — does not survive generation swaps
- Trace data is diagnostic only — never consumed by routing or scoring
- Thread-safe submission allows concurrent callers from different coroutines

## Database Schema

Trace events are persisted to the `routing_decisions` table:

```sql
CREATE TABLE routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    protocol TEXT,
    selected_account_id INTEGER,
    selected_account_name TEXT,
    selected_tier INTEGER,
    selected_score REAL,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    scored_count INTEGER NOT NULL DEFAULT 0,
    attempted_excluded_count INTEGER NOT NULL DEFAULT 0,
    top_score REAL,
    top_score_account_name TEXT,
    exclude_reasons_json TEXT NOT NULL DEFAULT '[]',
    decision_made_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    score_components_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY (selected_account_id) REFERENCES accounts(id)
);
```

## Related

- [deep-dive-routing.md](deep-dive-routing.md) — How routing decisions are made
- [deep-dive-dashboard.md](deep-dive-dashboard.md) — Dashboard routing trace display
- [deep-dive-database.md](deep-dive-database.md) — SQLite schema and migrations
