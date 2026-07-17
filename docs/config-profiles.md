# Configuration Profiles

Evidence-based configuration profiles for different deployment targets.
These profiles are validated by Milestone G soak testing.

## Balanced Default

Recommended for Raspberry Pi 4/5 and similar SBC hardware. Tuned for
dashboard responsiveness under request load with moderate write pressure.

```toml
[server]
threads = 4
access_log = false

[database]
worker_threads = 2
wal = true
synchronous = "NORMAL"
busy_timeout_ms = 5000

[upstream]
connect_timeout_s = 5
read_timeout_s = 300
max_connections = 100

[metrics]
write_mode = "balanced"
flush_interval_s = 30

[routing]
strategy = "quota_fair"

[routing.trace]
mode = "sampled"
sample_rate = 0.05
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
enabled = true
interval_s = 86400
retain_count = 14
```

**Characteristics:**
- One Granian worker with four runtime threads
- Two database connections (primary + read-only stats)
- WAL mode with NORMAL synchronous
- Sampled routing traces (5% sample rate)
- Balanced metrics flush (30s interval)
- Conservative maintenance budgets
- Automatic daily backups

## Minimum-Footprint SBC

For extremely constrained devices or when memory is at a premium.
Reduces database connections, metrics write frequency, and routing
trace overhead.

```toml
[server]
threads = 1
access_log = false

[database]
worker_threads = 1
busy_timeout_ms = 10000

[upstream]
max_connections = 50
max_keepalive = 10

[metrics]
write_mode = "low_wear"
flush_interval_s = 120
max_buffered_events = 250
timeseries_bucket_s = 300

[routing.trace]
mode = "sampled"
sample_rate = 0.05
include_score_components = false

[maintenance]
max_rows_per_batch = 250
max_batches_per_tick = 2
max_tick_duration_ms = 1000

[backup]
enabled = true
interval_s = 86400
retain_count = 7
```

**Characteristics:**
- Single runtime thread (minimum footprint)
- Single database connection (accepts dashboard contention)
- Low-wear metrics mode (120s flush, 5-minute buckets)
- Conservative maintenance budgets
- Reduced backup retention
- Longer busy timeout for slow storage

**Trade-offs:**
- Dashboard may be slow under request load
- Metrics are less fresh (2-minute vs 30-second granularity)
- Fewer maintenance batches per tick

## Full Diagnostics

For development, debugging, or high-power hosts where maximum
diagnostic visibility is needed. Not recommended for production or
SBC deployments.

```toml
[server]
threads = 4
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
and fast storage.

```toml
[server]
threads = 8
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
- Higher runtime thread count for concurrent stream handling
- Larger upstream connection pool
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
| Raspberry Pi 4/5, personal use | Balanced Default |
| Raspberry Pi, minimal resources | Minimum-Footprint SBC |
| Development/debugging | Full Diagnostics |
| High-concurrency coding agent | High-Concurrency General |
| Production, low traffic | Balanced Default |
| Production, high traffic | High-Concurrency General |

## Runtime Thread Guidance

The `[server].threads` setting controls Granian `runtime_threads` —
the number of event-loop threads in the worker process.

- **1 thread**: Minimum footprint. Dashboard may be slow under load.
  Suitable for very constrained devices.
- **4 threads** (default): Balanced. Handles concurrent streaming
  proxy traffic + dashboard requests without starvation.
- **8 threads**: High concurrency. For capable hosts with sustained
  multi-session workloads.

Values above the supported maximum emit a startup warning. All
`asyncio.Lock` objects are loop-bound; unsupported thread counts
may cause cross-loop affinity failures.

## Database Worker Threads

`[database].worker_threads` controls the number of database connections:

- **1 connection**: Minimal footprint. Dashboard queries contend with
  the dispatch write path. Acceptable for low-traffic deployments.
- **2 connections** (default): Separate read-only stats connection.
  Dashboard queries do not block dispatch persistence.

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
