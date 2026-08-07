# Deep Dive: Background Tasks

Back to [Overview](overview.md)

## Purpose

The `TaskSupervisor` manages periodic and startup tasks for maintenance, cleanup, monitoring, and operational housekeeping. Tasks are classified as process-owned (survive generation swaps) or generation-leased (retired with their generation).

## Architecture

```
┌─────────────────────────────────────┐
│         TaskSupervisor               │
│  Fixed-delay scheduler               │
│  Next interval after previous tick   │
└──────────────┬──────────────────────┘
               │
    ┌──────────▼──────────┐
    │ Process-Owned Tasks │ (survive generation swaps)
    │ • checkpoint        │ (default)
    │ • metrics_flush     │ (default)
    │ • update_checker    │ (opt-in)
    │ • automatic_backup  │
    └─────────────────────┘
               │
    ┌──────────▼──────────┐
    │ Generation-Leased   │ (retired with generation)
    │ Tasks               │
    │ • catalog_refresh   │
    │ • retention_cleanup │
    └─────────────────────┘
```

## Key Modules

### `background/maintenance.py` — TaskSupervisor

Fixed-delay scheduler:
- Next interval begins after previous tick completes
- `initial_delay_s` consumed exactly once per task lifecycle
- Tasks registered via `register_periodic()`
- Process-owned and generation-leased classifications

### `background/cleanup.py`

Retention cleanup and reservation reconciliation. Active requests are not
reclaimed by age; crash repair runs once during startup after a previous
process has exited.

### `background/backup.py`

Automatic backup task (zip archives).

## Task Classification

### Process-Owned Tasks

Survive generation swaps (live reload). The ordinary low-wear profile registers
the default rows below; update checking and automatic backups are explicit
opt-ins:
- **`checkpoint`** — Database WAL checkpoint
- **`metrics_flush`** — Metrics buffer flush
- **`update_checker`** — optional PyPI update check (conservative, no freshness bypass)
- **`automatic_backup`** — Scheduled backup

The task inventory is exposed through `eggpool runtime-status --json` and the
startup `Operational profile` log line. Those are bounded diagnostics for a
fixed short measurement window, not a benchmark framework or a steady-state
resource guarantee across hosts.

### Generation-Leased Tasks

Retired when their generation is retired:
- **`catalog_refresh`** — Upstream model catalog refresh
- **`retention_cleanup`** — Bounded daily retention and reservation reconciliation

Catalog refresh is also the event source for model-info reconciliation and
health/model recovery. Usage windows are hydrated while constructing a
generation, so neither concern needs a periodic reload task.

The five-minute default discovery cadence is a fetch cadence, not a full
SQLite catalog rewrite. Successful refresh freshness is written to compact
per-account state, unchanged semantic catalog rows are skipped, and steady
successful ping history is sampled internally at a coarse cadence. Failure
pings and success/failure transitions remain immediate diagnostics.

## Safety-Net Tasks

Recorded in `operational_events` table:
- **`_crash_recovery`** — Recover from unclean shutdown
- **`reconcile_expired_reservations`** — Release expired quota reservations

## Update Checker

`src/eggpool/update_checker.py` — two paths:
- **Background probe**: `UpdateChecker` via `TaskSupervisor.register_periodic()`. Conservative (no freshness bypass). Caches latest `UpdateInfo`.
- **CLI one-shot**: `async_check_for_update()`. Live PyPI lookup with freshness-aware double-fetch. Never reads `UpdateChecker.snapshot()`.
- **Exact CLI target**: `normalize_requested_version()` and `check_exact_release()` validate one requested release and query its PyPI metadata directly; this path is separate from the cached background snapshot.

## Key Invariants

- Fixed-delay: next interval begins after previous tick completes
- `initial_delay_s` consumed exactly once per task lifecycle
- Process-owned tasks survive generation swaps; the PyPI checker is only
  registered when `[update_checker].enabled = true`
- Generation-leased tasks retired with their generation
- Safety-net tasks record `operational_events` in same transaction as state mutation
- Update checker CLI never consults `UpdateChecker.snapshot()`
- Background update probe is conservative (minimal PyPI traffic)
