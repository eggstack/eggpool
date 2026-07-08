# OpenCode Stream Stability — Troubleshooting Workflow

OpenCode and other coding-agent clients keep long-lived SSE streams open
across many parallel edits. Under high concurrency, a small upstream
hiccup can quickly cascade into hundreds of pending requests with
locked-out reservations. This document is the operator playbook for
that failure mode.

## Symptom checklist

When users report "my agent just hung" or the dashboard shows a sudden
spike in pending requests, walk through this list in order:

1. **Dashboard runtime tab — stream diagnostics card.**
   The `outcomes` histogram should be dominated by `stream_completed`.
   A spike in `stream_finalizer_timeout`, `upstream_midstream_error`,
   or `downstream_send_cancelled` points at the failing layer.
2. **`/api/stats/runtime` JSON — `db_contention.lock_wait_p95_ms`.**
   Anything above `200 ms` means SQLite is the bottleneck. The routing
   trace guardrail should be skipping writes; confirm via
   `routing_trace_guard.skipped_db_pressure`.
3. **`/api/stats/runtime` JSON — `finalization_retry_queue.depth`.**
   Non-zero depth means the shielded finalizer timed out. The retry
   queue will drain on its own cadence; if depth keeps growing, the
   upstream is the problem, not the local finalizer.
4. **Per-account health** (`/api/health` or the health tab). Repeated
   `consecutive_failures` on a single account points at an upstream
   issue, not a code regression.

## Common root causes and fixes

### "Finalizer keeps timing out (status=cancelled but reservation still active)"

The 10 s shielded finalizer hit the SQLite lock. Two usual causes:

- **Lock contention from another writer.** Inspect
  `db_contention.lock_wait_count` and the rolling p95. If they spike,
  set `[routing.trace] mode = "off"` to drop the diagnostic writes
  and verify the contention drops.
- **Finalizer retry queue depth growing unbounded.** Confirm the
  periodic drain task is running (`/api/stats/runtime.background_tasks`
  should show `finalization_retry_drain.last_tick_status=ok`). If the
  task is missing, restart the service.

### "OpenCode streams drop mid-edit (status_code=502, error_class=PoolTimeout)"

The HTTPX pool exhausted. Open the relevant provider in
`config.toml` and raise `max_connections` and `max_keepalive`. See
`docs/providers.md` for the high-concurrency profile. Do not raise
`server.threads` — Granian already serializes the event loop.

### "Read timeouts spike during long model runs (status_code=504, error_class=ReadTimeout)"

The model upstream is slower than `read_timeout_s`. Increase it on
the affected provider. Coding agents that stream long completions
need at least `read_timeout_s = 900`; some slow tiers benefit from
`1800`. The retry queue will hold the request open during the wait.

### "Routing trace writes are getting dropped (routing_trace_guard.skipped_db_pressure > 0)"

This is the expected safety behavior. The guardrail is dropping the
diagnostic trace rows because the SQLite lock p95 exceeded
`skip_above_lock_wait_p95_ms`. If you want full traces anyway, raise
the threshold or set `mode = "off"` and accept the gap.

### "Active reservation count keeps climbing"

Reservations are released at finalization. If the count grows, the
finalizer is not running for some requests. Walk through:

1. Confirm `finalization_retry_queue` is not backed up.
2. Confirm `_crash_recovery` ran at startup (look for the
   "Recovered N pending requests" log line in supervisor output).
3. Run `eggpool stats repair-reservations` to manually reconcile.

## Recovery commands

```bash
# Inspect runtime telemetry without dashboard access
eggpool runtime show --db /var/lib/eggpool/eggpool.db

# Drain the finalization retry queue immediately (instead of waiting
# for the periodic supervisor tick)
eggpool admin drain-finalization-queue

# Reconcile active reservations whose request is no longer pending
eggpool stats repair-reservations --dry-run
eggpool stats repair-reservations

# Reset the routing trace guard threshold without a service restart
eggpool admin set-routing-trace-threshold --p95-ms 500
```

## Capacity planning

For OpenCode-style workloads, budget these limits:

- `max_connections`: 1 per concurrent stream + 25% headroom for
  retries and short-lived requests. 50 concurrent streams → 64
  minimum, 128 recommended.
- `read_timeout_s`: 900 s for default OpenCode; 1800 s for long
  completions or extended thinking.
- `pool_timeout_s`: 60 s. Anything shorter causes spurious 502s
  during burst startup.
- `database.worker_threads`: 2 is sufficient for dashboards; raise
  to 4 if the dashboard itself lags.

Do **not** raise `server.threads` or `workers`. Granian runs one
worker with a single asyncio loop. Adding threads does not improve
HTTPX concurrency, and adding workers multiplies the connection
budget per upstream IP.