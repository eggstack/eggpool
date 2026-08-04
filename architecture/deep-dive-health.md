# Deep Dive: Health Management

Back to [Overview](overview.md)

## Purpose

Per-account health tracking, circuit breaker, and cooldown management. Health state determines routing eligibility — unhealthy accounts are excluded from routing until they recover.

## Architecture

```
┌─────────────────────────────────────┐
│         HealthManager                │
│  Per-account health tracking         │
│  Circuit breaker per account         │
│  Cooldown management                 │
└──────────────┬──────────────────────┘
               │
    ┌──────────▼──────────┐
    │ AccountHealth       │
    │ • consecutive_failures│
    │ • disabled_models   │
    │ • disabled_until    │
    │ • disabled_reason   │
    │ • cooldown_until    │
    └─────────────────────┘
               │
    ┌──────────▼──────────┐
    │ CircuitBreaker      │
    │ • closed (healthy)  │
    │ • open (unhealthy)  │
    │ • half-open (probe) │
    └─────────────────────┘
```

## Key Modules

### `health/health_manager.py` — HealthManager

Per-account health tracking:
- `is_account_healthy()` — checks circuit breaker + cooldown
- `mark_success()` — records successful request
- `mark_failure()` — records failed request, may open circuit
- `mark_probe()` — half-open probe
- `get_cooldown_until()` — returns cooldown expiry

### `health/circuit_breaker.py`

Circuit breaker implementation:
- **Closed** (healthy): requests flow normally
- **Open** (unhealthy): requests blocked, cooldown timer running
- **Half-open** (probe): single probe request allowed

### `health/backoff.py`

Backoff tracking for upstream errors. Tracks per-account backoff state with bounded windows.

## Health Events

| Event | Effect |
|-------|--------|
| Successful request | `consecutive_failures` reset |
| Failed request | `consecutive_failures` increment |
| Circuit open | Account excluded from routing |
| Cooldown active | Account excluded until expiry |
| Operator disable | Account excluded until re-enabled |
| Model disable | Specific model excluded |

## Backoff Persistence

Upstream-derived backoffs (429, 402, model-unavailable) persist across restarts in `account_backoffs` table. Rehydrated into `HealthManager` at startup.

Local-estimate overage never produces a backoff row — only upstream-observed failures suppress routing.

## Attempt-Scoped Failure Decisions

`classify_failure_effects()` consumes one immutable `FailureObservation` and
returns the complete retry/effects decision. The coordinator carries that same
decision through account failover, retained attempt cleanup, and terminal
finalization; it does not reconstruct health effects from an exception class.
Response bodies are reduced to bounded `FailureSignal` values before
classification, and transport failures use `source="transport"` so they do
not fall through the upstream-HTTP path.

Effect identity is the durable `(proxy_request_id, attempt_id)` pair. Component
progress (`account`, `model`, `circuit`, `probe`, and durable backoff
persistence) is retained by the attempt cleanup or finalization owner and is
released with that owner after convergence. Separate attempts with identical
status/model/account facts therefore apply independently, while replaying one
attempt resumes only incomplete components. `HealthManager.record_failure()`
records the circuit failure; the effects applier never records that same
transition a second time. Request-local errors and cancellation release a
half-open probe without provider penalties.

## Key Invariants

- Health driven solely by upstream-observed failures, operator disablement, and catalog/protocol incompatibility
- Compression fallbacks do NOT increment provider error counters
- Compression fallbacks do NOT write `account_backoffs` rows
- Compression fallbacks do NOT call `HealthManager.mark_*`
- Backoff rows persist across restarts
- Cooldown timers are monotonic
- `AccountHealth` fields: `consecutive_failures`, `disabled_models`, `disabled_until`, `disabled_reason`, `cooldown_until`
