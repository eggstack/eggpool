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
   or `client_cancelled` points at the failing layer.
2. **`/api/stats/runtime` JSON — `db_contention.lock_wait_p95_ms`.**
   Anything above `200 ms` means SQLite is the bottleneck. The routing
   trace guardrail should be skipping writes; confirm via
   `routing_trace_guard.skipped_db_pressure`.
3. **`/api/stats/runtime` JSON — `finalization_supervisor`.**
   Inspect active jobs, retry-pending work, failed bounded diagnostics, and
   saturation counters. The snapshot also reports configured capacity and the
   absolute retry-age limit.
   The supervisor is the only automatic in-process terminal retry owner.
4. **Per-account health** (`/v1/healthz` or the health tab). Repeated
   `consecutive_failures` on a single account points at an upstream
   issue, not a code regression.

## Common root causes and fixes

### "Finalizer keeps timing out (status=cancelled but reservation still active)"

The 10 s shielded finalizer hit the SQLite lock. Two usual causes:

- **Lock contention from another writer.** Inspect
  `db_contention.lock_wait_count` and the rolling p95. If they spike,
  set `[routing.trace] mode = "off"` to drop the diagnostic writes
  and verify the contention drops.
- **Terminal finalization retry-pending work growing.** Confirm the
  supervisor diagnostics and SQLite contention counters. Jobs retire after
  the bounded retry age; durable stale/startup recovery remains the safety net.

### "OpenCode streams drop mid-edit (status_code=502, error_class=PoolTimeout)"

The HTTPX pool exhausted. Open the relevant provider in
`config.toml` and raise `max_connections` and `max_keepalive`. See
`docs/providers.md` for the high-concurrency profile. Do not raise
`server.threads` — Granian already serializes the event loop.

### "Read timeouts spike during long model runs (status_code=504, error_class=ReadTimeout)"

The model upstream may have a long first-token delay or a long gap between
tokens. Configure those intervals on the affected provider instead of raising
every provider's transport timeout:

```toml
[providers.example.stream_timeouts]
first_byte_timeout_s = 900
idle_timeout_s = 300
max_lifetime_s = 0
```

`read_timeout_s` remains the legacy HTTPX guardrail. Explicit stream values
raise that guardrail only for this provider. A first-byte timeout can retry
before downstream output; an idle timeout after output is a midstream failure.
If the upstream closes cleanly without its required terminal marker, inspect
the premature-EOF counters instead — increasing a timeout cannot repair EOF.

### "Routing trace writes are getting dropped (routing_trace_guard.skipped_db_pressure > 0)"

This is the expected safety behavior. The guardrail is dropping the
diagnostic trace rows because the SQLite lock p95 exceeded
`skip_above_lock_wait_p95_ms`. If you want full traces anyway, raise
the threshold or set `mode = "off"` and accept the gap.

### "Active reservation count keeps climbing"

Reservations are released at finalization. If the count grows, the
finalizer is not running for some requests. Walk through:

1. Confirm the finalization supervisor is running and not at capacity.
2. Confirm `_crash_recovery` ran at startup (look for the
   "Recovered N pending requests" log line in supervisor output).
3. Run `eggpool stats repair-costs` to reconcile any stale cost data.

## Recovery commands

```bash
# Inspect runtime health without dashboard access
eggpool runtime-status

# Reconcile active reservations whose request is no longer pending
# (crash recovery runs automatically at startup; for manual intervention
# restart the service: eggpool restart)
eggpool restart

# Reset the routing trace guard threshold — edit config and restart
# [routing.trace] skip_above_lock_wait_p95_ms = 500  in config.toml
# then: eggpool restart
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

Keep `server.threads = 1` and leave `workers=1`. The single event loop
multiplexes dashboard work and active streams; connection-pool sizing is
controlled by the provider HTTPX settings. Adding workers multiplies the
connection budget per upstream IP and is not supported.

## Closure validation

After deploying the stream-stability changes, run these commands to
verify the runtime diagnostics and harness are working correctly:

```bash
# Unit tests for stream diagnostics, finalization queue, and runtime metrics
uv run pytest tests/unit/test_runtime_metrics.py -q
uv run pytest tests/unit/test_stream_diagnostics.py -q
uv run pytest tests/unit/test_stream_finalization_queue.py -q

# Integration test: 50 concurrent streams, no cancellations
uv run pytest tests/integration/test_high_concurrency_streaming.py -q

# CLI reproducer: 50 streams with 25% cancellation
python scripts/repro_high_concurrency_streams.py \
    --concurrency 50 --cancel-rate 0.25 --scenario slow-stream

# CLI reproducer: 100 streams with 50% cancellation
python scripts/repro_high_concurrency_streams.py \
    --concurrency 100 --cancel-rate 0.50 --scenario slow-stream
```

### Expected summary values

A clean run should produce:

- `leaked_pending_rows == 0`
- `leaked_active_reservations == 0`
- `router_active_requests_after == 0`
- finalization supervisor `active_count` returns to zero
- `quota_reserved_cost_delta == 0`
- DB lock p95/max present when contention was observed (concurrency > 1)
- Stream diagnostics `stream_completed` delta matches the number of non-cancelled streams
- HTTPX first-class outcome labels (`upstream_read_timeout`, etc.) are zero for the happy path

### Runtime diagnostics sections

The `/api/stats/runtime` endpoint exposes these stream-stability sections:

- `stream_diagnostics` — outcome counters and histograms
- `finalization_supervisor` — active jobs, retry-pending work, bounded failures, saturation, and retry limits
- `routing_trace_guard` — skip rate and lock-pressure threshold
- `db.contention` — lock-wait p50/p95/p99/max and sample count
