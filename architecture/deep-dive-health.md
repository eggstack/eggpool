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
- Synchronous state transitions use a short-held `threading.Lock` so the
  breaker remains safe at thread/diagnostic boundaries.
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

The classifier is deliberately conservative about authentication. A bare or
unknown HTTP 401 is not credential evidence and does not disable an account,
advance its circuit, or trigger account failover. Only explicit invalid,
expired, or revoked credential evidence produces `disable_auth`. A missing
credential-header or endpoint/surface/schema mismatch can produce a resolver-
only wire rejection when an alternate candidate exists and the failure was
observed before downstream response handoff; it never changes account health.

Weak model wording is also negotiation-aware. When the selected provider's
scoped catalog/config entry knows the model and the pre-handoff response says
the model is unsupported on the selected endpoint, the observation carries
`MODEL_UNSUPPORTED_ON_SURFACE` and may reject only that wire candidate. Strong
absence wording (`model not found`, `unknown model`, `does not exist`, and
equivalent authoritative withdrawal) remains model quarantine/withdrawal
evidence and never enumerates alternate surfaces merely because they exist.

Effect identity is the durable `(proxy_request_id, attempt_id)` pair. Component
progress (`account`, `model`, `circuit`, `probe`, and durable backoff
persistence) is retained by the attempt cleanup or finalization owner and is
released with that owner after convergence. Separate attempts with identical
status/model/account facts therefore apply independently, while replaying one
attempt resumes only incomplete components. `HealthManager.record_failure()`
records the circuit failure; the effects applier never records that same
transition a second time. Request-local errors and cancellation release a
half-open probe without provider penalties.

### Per-model quarantine suppresses account-wide circuit advance

When the classifier sets `model_effect = "quarantine"` on a 5xx, the
effects applier (`EffectsApplier._apply_account_effect`) skips the
`HealthManager.record_failure()` call for that account. The per-model
disable (`disable_model`) is the **sole** shared-state penalty in that
case; the account-wide circuit breaker stays closed. The breaker
advances only when the classifier sets `source = "transport"`
(genuine account-wide failure: DNS failure, TLS error, persistent 503
with no model-specific cause) **and** `model_effect = "none"`.

This isolation prevents N per-model 5xx failures from tripping the
breaker for all models on the same subscription. A single model's
upstream anomaly must not black-hole sibling models on the same
account. The `effects.account_effect` is still `"failure"` in the
quarantined case; the `model_effect` flag gates whether the breaker
call fires.

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
- Per-model quarantine (`model_effect != "none"`) is the SOLE shared-state penalty; the account-wide circuit breaker advances only when `source="transport"` AND `model_effect="none"` (genuine account-wide failure). A single model's 5xx must not black-hole sibling models on the same account
