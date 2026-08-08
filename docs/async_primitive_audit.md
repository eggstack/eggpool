# Async Primitive Audit — Dispatch Stability Milestone F

This document catalogs every long-lived async/threading primitive in
the EggPool codebase and evaluates cross-loop safety under the current
single-loop model (**Model 1**).

## Runtime Model Summary

EggPool runs as a **single Granian worker** (`workers=1`) with exactly one
event-loop thread (`runtime_threads=1`). Configuration validation rejects
multi-loop runtime settings.
All `asyncio.Lock` instances are bound to the event loop where they
are created.  Under Model 1, there is exactly **one event loop** per
process, so all asyncio locks are safe.

The `threading.Lock` primitives are process-wide OS-level locks that
are safe across any thread.

## Primitive Table

| Primitive | Module | Creating Loop | Consuming Loops | Ownership | Cross-Loop Safety | Shutdown |
|-----------|--------|---------------|-----------------|-----------|-------------------|----------|
| `_selection_claim_lock` | `coordinator.py:626` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `router._active_count_lock` | `router.py:224` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `router._missing_account_recovery_lock` | `router.py:242` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `quota_estimator._snapshot_lock` | `estimation.py:665` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `runtime_manager._lock` | `runtime_manager.py:459` | event loop | event loop | process | `asyncio.Lock` (loop-bound) | cleared on shutdown |
| `reload_manager._reload_lock` | `reload_manager.py:200` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `catalog_service._refresh_lock` | `service.py:199` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `dns_cache._lock` | `dns_cache.py:72` | event loop | event loop | process | `asyncio.Lock` (loop-bound) | cleared on shutdown |
| `db._connection_lock` | `connection.py:109` | event loop | event loop | process | `asyncio.Lock` (loop-bound) | cleared on shutdown |
| `outbound_manager._lock` | `outbound.py:103` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `thinking_counter._lock` | `thinking.py:85` | event loop | event loop | process | `asyncio.Lock` (loop-bound) | cleared on shutdown |
| `finalization_supervisor._lock` | `request/finalization_job.py:1276` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `fair_scorer._lock` | `fairness.py:82` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `catalog_resolvers._lock` | `catalog_resolvers.py:186` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `model_info_base._lock` | `base.py:38` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `update_checker._lock` | `update_checker.py:117` | event loop | event loop | process | `asyncio.Lock` (loop-bound) | cleared on shutdown |
| `metrics_coalescer._thread_lock` | `buffer.py:193` | any | any | process | `threading.Lock` (thread-safe) | cleared on shutdown |
| `metrics_coalescer._async_lock` | `buffer.py:194` | event loop | event loop | generation | `asyncio.Lock` (loop-bound) | cleared on generation swap |
| `db._connection_lock_guard` | `connection.py:110` | any | any | process | `threading.Lock` (thread-safe) | cleared on shutdown |
| `dispatch_overhead_recorder._lock` | `runtime_dispatch.py:44` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `local_pre_upstream_recorder._lock` | `runtime_dispatch.py:148` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `dispatch_span_recorder._lock` | `runtime_dispatch.py:271` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `stream_diagnostics._lock` | `stream_diagnostics.py:140` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `selection_claim_diagnostics._lock` | `selection_claim_diagnostics.py:57` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `routing_trace_writer._lock` | `routing_trace_writer.py:168` | any | any | process | `threading.Lock` (thread-safe) | cleared on shutdown |
| `routing_trace_guard._lock` | `routing_trace_guard.py:51` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `routing_trace_guard._global_lock` | `routing_trace_guard.py:216` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `event_loop_lag_monitor._lock` | `event_loop_lag.py:78` | any | any | process | `threading.Lock` (thread-safe) | never cleared |
| `compression_tuning._lock` | `tuning.py:757` | any | any | process | `threading.Lock` (thread-safe) | never cleared |

## Classification

### asyncio.Lock (loop-bound)

These locks are bound to the event loop where they are constructed.
They are safe under Model 1 (single loop) but would raise
`RuntimeError: ... is bound to a different event loop` if accessed
from a different loop.

All generation-owned locks are cleared when the generation is retired.
Process-owned locks live for the process lifetime.

### threading.Lock (thread-safe)

These are OS-level mutexes safe across any thread.  They are used for
hot-path recorders that may be accessed from Granian's runtime threads
outside the event loop (e.g., `record_usage()` in the metrics
coalescer, `record_ns()` in dispatch overhead recorders).

## Model 1 Invariants

1. **Single event loop**: all asyncio locks are bound to one loop.
2. **Generation ownership**: generation-owned locks are cleared on
   swap; process-owned locks outlive generations.
3. **SQLite serialization**: the single `db._connection_lock` serialises
   all SQL access; `threading.Lock` guards the connection for
   cross-thread safety at the aiosqlite boundary.
4. **Thread-safe recorders**: hot-path counters use `threading.Lock`
   because they may be called from any thread.

## Multi-Loop Risk Assessment

Granian multi-loop runtime settings are rejected before startup. This is a
fail-closed requirement: every long-lived `asyncio.Lock` is owned by the one
worker event loop, and no lock rebinding or experimental compatibility mode is
supported.
