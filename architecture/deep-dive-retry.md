# Deep Dive: Retry Classification

Back to [Overview](overview.md)

## Purpose

Upstream failure classification and retry decision logic. Determines whether a failed attempt should retry, which accounts to exclude, and how long to backoff.

## Module Structure

```
src/eggpool/retry/
├── __init__.py
└── classification.py    # Retry category and classification logic (~202 lines)
```

## Key Components

### Retry Categories (`RetryCategory`)

Eight categories of upstream failure outcomes:

| Category | Description | Retryable |
|----------|-------------|-----------|
| `NEVER` | Non-retryable client error | No |
| `BAD_REQUEST` | Client error, don't retry | No |
| `AUTH_FAILURE` | Authentication failure — disables the failing account; may retry on another account | Yes |
| `QUOTA_EXCEEDED` | Quota or rate-limit effect (`quota`/`rate_limit`) | Conditional |
| `TEMPORARY` | Retryable outcome outside {408, 502, 504} — backoff | Yes |
| `TRANSIENT` | Retryable transport-class status (408/502/504) | Yes |
| `FATAL` | Fatal error, don't retry | No |
| `MODEL_UNAVAILABLE` | Model-specific 404, retryable on another account | Yes |

### Classification Logic

`RetryClassifier.classify(status_code, headers, body)` adapts the canonical
`classify_failure_effects()` decision into a retry category:

- **Quota/rate-limit effects** (429, 402, and 403/409/422 with matching response signals) → `QUOTA_EXCEEDED`
- **Auth effect** (`disable_auth`, e.g. 401/403 with auth signal) → `AUTH_FAILURE`
- **Model effect** → `MODEL_UNAVAILABLE`
- **Retryable without those effects**: `TRANSIENT` when status ∈ {408, 502, 504}, otherwise `TEMPORARY`; transport failures classify as `TEMPORARY`
- **Remaining 4xx**: `BAD_REQUEST`; anything else: `NEVER`

**RetryableError dataclass:**
```python
@dataclass
class RetryableError:
    status_code: int
    category: RetryCategory
    retry_after: float | None = None
    message: str = ""
    account_name: str | None = None
    model_id: str | None = None
```

### Integration with Failure Effects

The retry module integrates with `failure/classifier.py` for typed failure effects:
- `classify_failure_effects()` returns the canonical retry/effects decision
- Effects include: retry scope, provider attribution, circuit transition, probe convergence
- `retry/classification.py` provides the HTTP-level categorization that feeds into the effects classifier

### Backoff Calculation

Retry-after durations are extracted from:
- `Retry-After` header (the only retry header parsed)
- Exponential backoff with jitter (default)

Bounded to 1,800 seconds maximum per the health management backoff cap.

## Data Flow

```
Upstream HTTP response
    → RetryClassifier().classify(status_code, headers, body)
    → RetryableError(category, retry_after)
    → RequestCoordinator._should_retry()
    → HealthManager.record_failure() / record_success()
    → Retry with excluded accounts
```

## Key Invariants

- Retry decisions are attempt-scoped — each attempt independently classified
- `AUTH_FAILURE` disables the failing account's credential state and retries on a different account
- `MODEL_UNAVAILABLE` retries across accounts — different accounts may have the model
- `QUOTA_EXCEEDED` respects `retry_after` — no premature retry
- Total retry attempts bounded by distinct eligible accounts and `1 + max_retries_before_stream`
- Retry cleanup converges before reselection — all owned resources released

## Configuration

Retry behavior is configured via the request lifecycle, not standalone:

```toml
[routing]
max_retries_before_stream = 3  # Default; total attempts = value + 1
```

## Related

- [deep-dive-health.md](deep-dive-health.md) — Circuit breaker and health tracking
- [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) — How retry integrates with the coordinator
- [deep-dive-providers.md](deep-dive-providers.md) — Provider-specific error handling
