# Dispatch Stability Runbook

Diagnostic and mitigation guide for dispatch latency, database queues,
and resource pressure. This runbook covers the operational scenarios
validated by Milestone G soak testing.

## Diagnostic Sequence

When dispatch overhead rises or the system exhibits unstable behavior,
follow this sequence in order:

### 1. Confirm metric boundary

Compare local pre-upstream latency against upstream connect/TTFT to
determine whether the overhead is EggPool-side or upstream-side.

```bash
eggpool runtime-status --json | python3 -m json.tool
```

Look at:
- `dispatch_overhead.avg_ms`, `p95_ms`, `p99_ms` — EggPool-side only
- `local_pre_upstream` — full EggPool window from handler entry to dispatch
- `stream_diagnostics` — upstream connect/read/write/protocol errors

If `dispatch_overhead` is low but `local_pre_upstream` is high, the
bottleneck is in EggPool's routing, persistence, or claim path —
proceed to steps 2-4.

### 2. Inspect selection claim wait/held

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
sc = d.get('selection_claims', {})
print('claims_created:', sc.get('claims_created'))
print('claims_committed:', sc.get('claims_committed'))
print('claims_rolled_back:', sc.get('claims_rolled_back_before_persistence'))
wait = sc.get('claim_lock_wait_recent', {})
print('claim_lock_wait p50:', wait.get('p50_ms'), 'ms')
print('claim_lock_wait p95:', wait.get('p95_ms'), 'ms')
"
```

High claim-lock wait p95 (>5ms on general host, >15ms on SBC) indicates
SQLite write contention. Proceed to step 4.

### 3. Inspect dispatch writer queue

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
dw = d.get('dispatch_writer', {})
print('queue_depth:', dw.get('queue_depth'))
print('oldest_age_ms:', dw.get('oldest_age_ms'))
print('batches_total:', dw.get('batches_total'))
print('errors_total:', dw.get('errors_total'))
"
```

If `queue_depth` grows monotonically or `oldest_age_ms` exceeds the
enqueue timeout, the writer is saturated. Check SQLite pressure next.

### 4. Inspect DB lock-wait percentiles

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
db = d.get('db', {})
cw = db.get('contention', {})
print('lock_wait p50:', cw.get('lock_wait_p50_ms'), 'ms')
print('lock_wait p95:', cw.get('lock_wait_p95_ms'), 'ms')
print('lock_wait p99:', cw.get('lock_wait_p99_ms'), 'ms')
print('lock_wait max:', cw.get('lock_wait_max_ms'), 'ms')
print('write_ops:', cw.get('write_ops'))
print('read_ops:', cw.get('read_ops'))
"
```

If lock-wait p95 exceeds 10ms, write contention is significant. Check
maintenance and checkpoint activity next.

### 5. Inspect maintenance tick duration

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
m = d.get('maintenance', {})
for task in m.get('tasks', []):
    print(f\"{task['name']}: duration={task.get('duration_ms', '?')}ms stopped={task.get('stopped_reason', 'none')}\")
"
```

If maintenance ticks are taking >500ms or stopping due to budget
exhaustion, they may be contributing to write pressure. Check the
maintenance budget configuration.

### 6. Inspect finalization retry queue

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
fq = d.get('finalization_retry_queue', {})
print('queue_depth:', fq.get('queue_depth'))
print('oldest_age_ms:', fq.get('oldest_age_ms'))
print('enqueued_total:', fq.get('enqueued_total'))
print('drained_total:', fq.get('drained_total'))
print('dropped_overflow:', fq.get('dropped_overflow'))
"
```

A growing queue with increasing drops indicates finalization is
falling behind. Check event-loop lag next.

### 7. Inspect event-loop lag

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
el = d.get('event_loop_lag', {})
print('p50:', el.get('p50_ms'), 'ms')
print('p95:', el.get('p95_ms'), 'ms')
print('p99:', el.get('p99_ms'), 'ms')
print('max:', el.get('max_ms'), 'ms')
"
```

Event-loop lag p95 >50ms indicates CPU or I/O starvation. On
Raspberry Pi, check thermal throttling (`vcgencmd measure_temp`).

### 8. Inspect DB/WAL size and checkpoint status

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
db = d.get('db', {})
print('db_size_kb:', db.get('db_size_kb'))
print('wal_size_kb:', db.get('wal_size_kb'))
print('shm_size_kb:', db.get('shm_size_kb'))
print('page_count:', db.get('page_count'))
print('page_size:', db.get('page_size'))
print('freelist_count:', db.get('freelist_count'))
"
```

A WAL file >10MB suggests checkpoints are not keeping up. A freelist
that grows without vacuum indicates page fragmentation.

### 9. Inspect runtime resources

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
mem = d.get('memory', {})
print('rss_bytes:', mem.get('rss_bytes'))
print('fd_count:', mem.get('fd_count'))
print('thread_count:', mem.get('thread_count'))
proc = d.get('processes', {})
print('process_count:', proc.get('eggpool_process_count'))
print('expected:', proc.get('expected_worker_process_count'))
"
```

Compare against baseline values. Unbounded growth in any metric
indicates a leak. Check generations next.

### 10. Inspect generations, DNS, and client state

```bash
eggpool runtime-status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
gen = d.get('runtime_manager', {})
print('active_generation:', gen.get('active_generation_id'))
print('retiring:', gen.get('retiring_generation_count'))
dns = d.get('dns_cache', {})
print('dns_entries:', dns.get('entry_count'))
pool = d.get('provider_client_pool', {})
print('pool_hosts:', pool.get('tracked_hosts'))
"
```

Retiring generations that persist beyond the drain timeout indicate
leaked streams. DNS or client pools that grow without bound indicate
connection leaks.

## Safe Mitigations

Apply these in priority order. Never disable correctness-critical
dispatch persistence or finalization.

### Priority 1: Reduce write pressure

```toml
[routing.trace]
mode = "sampled"
sample_rate = 0.01

[metrics]
flush_interval_s = 120
```

Lowering the trace sample rate and metrics flush frequency reduces
SQLite write volume with minimal diagnostic impact.

### Priority 2: Ensure separate stats connection

```toml
[database]
worker_threads = 2
```

The second connection handles dashboard and stats queries without
contending with the dispatch write path.

### Priority 3: Increase DB busy timeout

```toml
[database]
busy_timeout_ms = 10000
```

On Raspberry Pi or slow storage, a longer busy timeout prevents
transient lock-wait failures.

### Priority 4: Manual checkpoint during low traffic

```bash
sqlite3 ~/.local/share/eggpool/usage.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE);"
```

Reclaims WAL space during quiet periods.

### Priority 5: Reduce dashboard polling

Set longer cache TTLs on dashboard pages or reduce browser refresh
frequency during high-load periods.

### Priority 6: Move database to faster storage

If the database is on microSD, consider moving to USB SSD. The WAL
pattern benefits significantly from faster random write I/O.

### Priority 7: Restart as last resort

Collect diagnostics before restarting. The process logs an operational
profile at startup that confirms the effective configuration.

```bash
# Collect diagnostics first
eggpool runtime-status --json > /tmp/eggpool-diag-$(date +%s).json
journalctl -u eggpool --since "1 hour ago" > /tmp/eggpool-log-$(date +%s).txt

# Then restart
eggpool restart
```

## Warnings

- **Never disable `[database].wal`** — WAL mode is required for
  concurrent read/write access.
- **Never disable dispatch persistence** — this would lose request
  durability guarantees.
- **Never set `busy_timeout_ms = 0`** — this causes immediate failures
  under any write contention.
- **Never increase `[server].threads` beyond supported value** on
  systems that have not been validated for multi-loop affinity.

## Performance Profiles

See [config-profiles.md](../config-profiles.md) for evidence-based
configuration profiles for different deployment targets.

## Runtime Validation Runner

The runtime validation runner (`scripts/run_dispatch_stability_soak.py`)
validates long-running dispatch stability across canonical workload profiles.

### Supported options

```
--profile PROFILE          Soak profile (default: balanced-file-backed)
--duration-seconds INT     Total duration in seconds (default: 300, minimum: 30)
--output FILE              Output JSON file path (default: /tmp/eggpool-runtime-validation.json)
--seed INT                 Deterministic random seed (default: 42)
-v / --verbose             Enable verbose logging
```

`--output` names a single JSON file, not a directory. The output is written
atomically (tmp → replace). No manifest, Markdown summary, JSONL series, or
checksum bundle is generated.

### Running validation

```bash
# Short validation (30 seconds — proves public CLI/output contract
# without burdening ordinary CI with a 5-minute runtime)
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 30 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json

# Standard SBC validation (5 minutes) on representative hardware
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 300 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json
```

The requested `--duration-seconds` covers warm-up, the two measurement
windows, and the inter-window drain. A bounded final quiescence poll adds
a small amount of wall-clock time after late load stops and is reported
separately under `quiescence_duration_seconds`. Quiescence is the
correctness drain allowance, not part of the requested measurement
duration.

### Interpreting output

The JSON output contains:

- `passed` / `failure_reasons` — pass/fail gating with reasons
- `process` — Eggpool child PID and RSS (bytes, measured from child process)
- `early` / `late` — window metrics (throughput, latency percentiles,
  success/error counts, observed error rate)
- `gates` — structured per-criterion pass/fail blocks:
  - `workload` — useful-work gate (per-window attempts, successes, errors,
    configured-vs-observed error rate, dual-shape coverage for
    `sbc-reference` at ≥60 seconds)
  - `throughput` — late/early RPS ratio against
    `throughput_decline_limit`
  - `dispatch_p95` / `dispatch_p99` — direct late/early ratio caps
    (early_ms, late_ms, ratio, ratio_limit, passed, failure_reason)
  - `quiescence` — post-load bounded drain observation
    (drained, attempts, elapsed_seconds, pending_requests,
    active_reservations, failure_reason)
  - `rss` — RSS availability / required gate
  - `database_audit` — offline SQLite lifecycle invariant check
- `database_audit` — SQLite lifecycle invariants (independent from runtime)
- `polling` — bounded dashboard polling diagnostics

`dispatch_p95_ratio_limit` and `dispatch_p99_ratio_limit` are direct ratio
caps: `late_value / early_value <= limit` must hold. Recommended values
are `1.50` (p95) and `2.00` (p99) for short functional validation, and
`1.30` / `1.80` for longer-duration runs. Empty latency windows,
non-positive early baselines, missing runtime data, zero attempts, and
zero successes all fail closed.

Final drain state is collected from a bounded post-load quiescence poll,
not from `metrics[-1]`. Unavailable metrics are reported as `null`, never
zero. Required metrics that are unavailable cause the run to fail with a
descriptive reason.

## Extended Stability Gates

Extended stability gates (`tests/soak/test_extended_stability_gates.py`)
validate long-duration behavior that cannot be caught in short tests:

- Dispatch latency stability over extended windows
- Resource plateau validation (memory, threads, file descriptors)
- Database consistency under sustained load
- Background task cadence drift under load

Run directly with pytest:

```bash
uv run pytest tests/soak/test_extended_stability_gates.py -m extended_soak -v
```

## Related Documentation

- [deployment.md](../deployment.md) — installation and systemd setup
- [raspberry-pi.md](../raspberry-pi.md) — SBC-specific guidance
- [config-profiles.md](../config-profiles.md) — configuration profiles
- [architecture/README.md](../../architecture/README.md) — design details
