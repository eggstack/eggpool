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
- `is_account_healthy()` — checks disabled/cooldown state
- `record_success()` — records successful request
- `record_failure()` — records failed request, may open circuit
- `try_acquire_request()` — half-open probe slot acquisition
- `release_request()` — releases probe slot without penalty

### `health/circuit_breaker.py`

Circuit breaker implementation:
- **Closed** (healthy): requests flow normally
- **Open** (unhealthy): requests blocked, cooldown timer running
- **Half-open** (probe): single probe request allowed

### `health/backoff.py`

Backoff tracking for upstream errors. Every nonterminal policy is capped at
1,800 seconds (30 minutes), including final jitter. Provider `Retry-After`
is honored for rate-limited and quota-exhausted reasons and capped to the
same bound. Authentication is terminal and has no timed expiry; runtime
model absence is account/model scoped and bounded.

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

The table is a restart hint, not the sole process-local authority. Hydration
ignores disabled-account, unknown-reason, contradictory-scope, malformed, and
expired rows; expired rows are deleted on a best-effort basis. Legacy future
deadlines are clamped in memory and opportunistically rewritten to `now +
1800s`. Persistence failures are logged without failing proxy traffic.

Recovery is scoped. A successful account/model request clears matching
transient account rows and that model's bounded quarantine, but never clears an
authentication failure or unrelated model. An authoritative catalog
reappearance or explicit operator enable/reset clears terminal model state.
Validated live rehash compares the old and candidate account identity and
resolved credentials; a changed credential/provider/key binding re-enables
the candidate account in-memory and clears that account's terminal
authentication hint in a SQLite transaction. Unchanged accounts retain
their authentication state.

Model-quarantine hydration is a generation-publication prerequisite. The
repository distinguishes a successful empty result from a failed read, and
strict row conversion rejects malformed identities, timestamps, states, or
provenance. Startup fails closed and a rehash candidate is rejected; the active
generation is not replaced by an empty or partial quarantine. When an
authoritative catalog reports a model again, the exact durable quarantine row
is marked healthy first. Only after durable convergence does the callback clear
the matching in-memory entry and transient model backoff. A database failure
therefore preserves routing suppression and is not recorded as provider
health evidence. Recovery is deterministic per identity; a later identity may
fail without undoing earlier durable-first convergence.

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
- Compression fallbacks do NOT call `HealthManager.record_*`
- Backoff rows persist across restarts
- Nonterminal backoff, including `Retry-After` and jitter, never exceeds 1,800 seconds
- Successful requests do not clear authentication or authoritative model withdrawal
- Half-open probe acquisition always converges through success, failure, cancellation, or local release
- Cooldown timers are monotonic
- `AccountHealth` fields: `consecutive_failures`, `disabled_models`, `disabled_until`, `disabled_reason`, `cooldown_until`
