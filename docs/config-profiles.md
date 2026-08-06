# Configuration Profiles

Configuration profiles for different deployment targets. All profiles
use the supported single-event-loop default (`threads = 1`). High
concurrency is achieved through asyncio task concurrency, HTTP
connection-pool sizing, and bounded writers — not by multiplying event
loops. The `[server].threads` field is `RESTART_REQUIRED` for live
rehash.

## Lean Default

Recommended for ordinary installs, Raspberry Pi 4/5, and similar
hardware. It keeps the request path small and durable while making
analytics, diagnostics, backups, and external metadata probes opt-in.

```toml
[server]
threads = 1
access_log = false

[database]
worker_threads = 1
wal = true
synchronous = "NORMAL"
busy_timeout_ms = 5000

[upstream]
connect_timeout_s = 5
read_timeout_s = 300
max_connections = 16
max_keepalive = 4

[metrics]
write_mode = "low_wear"
flush_interval_s = 120

[routing]
strategy = "quota_fair"

[routing.trace]
mode = "off"
sample_rate = 0.0
include_score_components = false

[transcoder]
enabled = true

[compression]
enabled = false

[cache]
enabled = false

[maintenance]
max_rows_per_batch = 500
max_batches_per_tick = 4
max_tick_duration_ms = 500

[backup]
enabled = false
interval_s = 86400
retain_count = 14
```

**Characteristics:**
- Single Granian event-loop thread (supported default)
- One serialized SQLite connection; set `worker_threads = 2` for a separate stats connection
- WAL mode with NORMAL synchronous
- No routing trace persistence
- Low-wear metrics flush (120s interval)
- Optional model info, readiness, DNS, backup, and PyPI update probes

## Minimum-Footprint SBC

Use the repository's [copyable SBC configuration](../config.sbc.example.toml)
instead of merging a profile fragment into another file. It is a complete,
validated example with one SQLite worker, low-wear buffered analytics,
diagnostics disabled, and daily retention/backup cadence.

The trade-off is lower dashboard freshness and less diagnostic detail in
exchange for lower memory, connection, and microSD write pressure. Add only
the provider/account sections you need, then run `eggpool check-config`.

## Full Diagnostics

For development, debugging, or high-power hosts where maximum
diagnostic visibility is needed. Not recommended for production or
SBC deployments.

```toml
[server]
threads = 1
access_log = true

[database]
worker_threads = 2
busy_timeout_ms = 5000

[metrics]
write_mode = "balanced"
flush_interval_s = 15

[routing.trace]
mode = "all"
sample_rate = 1.0
include_score_components = true

[maintenance]
max_rows_per_batch = 1000
max_batches_per_tick = 8
max_tick_duration_ms = 2000
```

**Characteristics:**
- Full routing traces (every request)
- Score components in traces
- Faster metrics flush (15s)
- Higher maintenance budgets
- Access logging enabled

**Warnings:**
- Significantly increased write volume from full routing traces
- Higher CPU usage from trace persistence
- Not recommended for microSD storage
- Use faster storage (SSD/NVMe) for sustained operation

## High-Concurrency General Host

For capable general-purpose hosts handling sustained high-concurrency
workloads (e.g., coding agent traffic). Requires adequate CPU, memory,
and fast storage. Uses a single event-loop thread with increased
connection pool and maintenance budgets.

```toml
[server]
threads = 1
access_log = false

[database]
worker_threads = 2
busy_timeout_ms = 5000
wal = true
synchronous = "NORMAL"

[upstream]
max_connections = 200
max_keepalive = 20
connect_timeout_s = 5
read_timeout_s = 300

[metrics]
write_mode = "balanced"
flush_interval_s = 30

[routing.trace]
mode = "sampled"
sample_rate = 0.10
include_score_components = false

[maintenance]
max_rows_per_batch = 1000
max_batches_per_tick = 8
max_tick_duration_ms = 1000
```

**Characteristics:**
- Single event-loop thread with larger upstream connection pool
- Higher maintenance budgets
- 10% trace sample rate for better visibility
- Requires SSD or NVMe storage for sustained write performance

**Limits:**
- SQLite single-writer throughput saturates at ~500-1000 writes/second
  depending on storage latency
- Above this threshold, consider whether the workload is suitable for
  EggPool's SQLite-based architecture

## Profile Selection Guide

| Scenario | Recommended Profile |
|----------|-------------------|
| Raspberry Pi 4/5, personal use | Lean Default |
| Raspberry Pi, minimal resources | [copyable SBC config](../config.sbc.example.toml) |
| Development/debugging | Full Diagnostics |
| High-concurrency coding agent | High-Concurrency General |
| Production, low traffic | Lean Default |
| Production, high traffic | High-Concurrency General |

## Runtime Thread Guidance

The `[server].threads` setting controls Granian `runtime_threads` —
the number of event-loop threads in the worker process.

- **1 thread** (default, supported): Single event-loop thread. High
  concurrency is achieved through asyncio tasks, connection-pool
  sizing, and bounded writers. All `asyncio.Lock` objects are
  loop-bound and safe under single-loop execution.
- **Greater than 1**: Rejected during configuration validation. All
  process-owned `asyncio.Lock` objects are bound to the single supported
  worker event loop; use asyncio task concurrency and connection-pool sizing
  for higher concurrency.

## Database Worker Threads

`[database].worker_threads` controls the number of database connections:

- **1 connection**: Minimal footprint. Dashboard queries contend with
  the dispatch write path. Acceptable for low-traffic deployments.
- **2 connections** (opt-in): Separate read-only stats connection.
  Dashboard queries can avoid queuing behind most dispatch persistence.

## Maintenance Budgets

The `[maintenance]` section controls bounded maintenance task behavior:

- `max_rows_per_batch`: Rows processed per maintenance batch (default 500)
- `max_batches_per_tick`: Maximum batches per maintenance tick (default 4)
- `max_tick_duration_ms`: Maximum wall-clock time per tick (default 500ms)

Conservative budgets (lower values) reduce write pressure at the cost
of slower maintenance drain. Aggressive budgets (higher values) clear
backlogs faster but may contend with dispatch writes.

## Related Documentation

- [deployment.md](deployment.md) — installation and systemd setup
- [raspberry-pi.md](raspberry-pi.md) — SBC-specific guidance
- [operations/dispatch-stability.md](operations/dispatch-stability.md) — operator runbook
