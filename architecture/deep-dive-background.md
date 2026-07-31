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
    │ • checkpoint        │
    │ • metrics_flush     │
    │ • update_checker    │
    │ • automatic_backup  │
    └─────────────────────┘
               │
    ┌──────────▼──────────┐
    │ Generation-Leased   │ (retired with generation)
    │ Tasks               │
    │ • catalog_refresh   │
    │ • model_info_refresh│
    │ • stale_finalization│
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

Retention cleanup, stale request finalization, reservation reconciliation.

### `background/backup.py`

Automatic backup task (zip archives).

## Task Classification

### Process-Owned Tasks

Survive generation swaps (live reload):
- **`checkpoint`** — Database WAL checkpoint
- **`metrics_flush`** — Metrics buffer flush
- **`update_checker`** — PyPI update check (conservative, no freshness bypass)
- **`automatic_backup`** — Scheduled backup

### Generation-Leased Tasks

Retired when their generation is retired:
- **`catalog_refresh`** — Upstream model catalog refresh
- **`model_info_refresh`** — Model info sidecar refresh
- **`stale_finalization`** — Finalize escaped requests

## Safety-Net Tasks

Recorded in `operational_events` table:
- **`_crash_recovery`** — Recover from unclean shutdown
- **`_finalize_stale_requests_once`** — Finalize requests stuck in pending and
  reconcile exact per-request runtime ownership in a bounded pass
- **`reconcile_expired_reservations`** — Release expired quota reservations

## Update Checker

`src/eggpool/update_checker.py` — two paths:
- **Background probe**: `UpdateChecker` via `TaskSupervisor.register_periodic()`. Conservative (no freshness bypass). Caches latest `UpdateInfo`.
- **CLI one-shot**: `async_check_for_update()`. Live PyPI lookup with freshness-aware double-fetch. Never reads `UpdateChecker.snapshot()`.

## Key Invariants

- Fixed-delay: next interval begins after previous tick completes
- `initial_delay_s` consumed exactly once per task lifecycle
- Process-owned tasks survive generation swaps
- Generation-leased tasks retired with their generation
- Safety-net tasks record `operational_events` in same transaction as state mutation
- Update checker CLI never consults `UpdateChecker.snapshot()`
- Background update probe is conservative (minimal PyPI traffic)
