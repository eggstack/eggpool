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
| `NEVER` | Non-retryable client error (400, 401, 403) | No |
| `BAD_REQUEST` | Client error, don't retry | No |
| `AUTH_FAILURE` | Authentication failure | No |
| `QUOTA_EXCEEDED` | Rate limit or quota exceeded | Conditional |
| `TEMPORARY` | Temporary error, retry with backoff | Yes |
| `TRANSIENT` | Transient error, retry immediately | Yes |
| `FATAL` | Fatal error, don't retry | No |
| `MODEL_UNAVAILABLE` | Model-specific 404, retryable on another account | Yes |

### Classification Logic

`classify_retry()` maps HTTP status codes and error patterns to retry categories:

- **4xx errors**: mostly `NEVER` or `BAD_REQUEST` (except 429 → `QUOTA_EXCEEDED`)
- **5xx errors**: mostly `TEMPORARY` or `TRANSIENT`
- **Timeout/connection errors**: `TEMPORARY` with backoff
- **Model not found (404)**: `MODEL_UNAVAILABLE` — retryable on a different account

**RetryableError dataclass:**
```python
@dataclass
class RetryableError:
    status_code: int
    category: RetryCategory
    retry_after: float | None = None
    message: str = ""
```

### Integration with Failure Effects

The retry module integrates with `failure/classifier.py` for typed failure effects:
- `classify_failure_effects()` returns the canonical retry/effects decision
- Effects include: retry scope, provider attribution, circuit transition, probe convergence
- `retry/classification.py` provides the HTTP-level categorization that feeds into the effects classifier

### Backoff Calculation

Retry-after durations are extracted from:
- `Retry-After` header (HTTP standard)
- `X-RateLimit-Reset` header (provider-specific)
- Exponential backoff with jitter (default)

Bounded to 1,800 seconds maximum per the health management backoff cap.

## Data Flow

```
Upstream HTTP response
    → classify_retry(status_code, headers, body)
    → RetryableError(category, retry_after)
    → RequestCoordinator._should_retry()
    → HealthManager.record_failure() / record_success()
    → Retry with excluded accounts
```

## Key Invariants

- Retry decisions are attempt-scoped — each attempt independently classified
- `AUTH_FAILURE` never retries — credentials won't change mid-request
- `MODEL_UNAVAILABLE` retries across accounts — different accounts may have the model
- `QUOTA_EXCEEDED` respects `retry_after` — no premature retry
- Total retry attempts bounded by distinct eligible accounts and `1 + max_retries_before_stream`
- Retry cleanup converges before reselection — all owned resources released

## Configuration

Retry behavior is configured via the request lifecycle, not standalone:

```toml
[upstream]
max_retries_before_stream = 0  # Default: no pre-stream retry
```

## Related

- [deep-dive-health.md](deep-dive-health.md) — Circuit breaker and health tracking
- [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) — How retry integrates with the coordinator
- [deep-dive-providers.md](deep-dive-providers.md) — Provider-specific error handling
