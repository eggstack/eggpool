# Phase 7: Retry, Failover, and Health Management

## Overview

Error classification, pre-stream account failover, circuit breakers, rate-limit cooldowns, and health state management.

## Components

### Error Classification (`retry/classification.py`)
Normalized internal error taxonomy:
- `authentication` — Invalid credentials
- `quota_exhausted` — Balance/rate limit exceeded
- `rate_limited` — Too many requests (429)
- `model_unavailable` — Model not found (404)
- `invalid_request` — Client error (400)
- `upstream_server_error` — Server error (5xx)
- `connect_timeout` — Connection timeout
- `read_timeout` — Read timeout
- `connection_failure` — Network failure
- `midstream_failure` — Error during streaming
- `client_cancelled` — Client disconnect

### Failover (`retry/failover.py`)
Retry only when:
- No downstream bytes emitted
- Failure is retryable
- Another eligible account exists
- Retry budget not exhausted
- Request safe to replay

### Circuit Breakers (`health/circuit_breaker.py`)
- Failure threshold: 3 consecutive eligible failures
- Base cooldown: 30 seconds
- Maximum cooldown: 10 minutes
- Success reset: immediate

### Health Manager (`health/health_manager.py`)
- Tracks per-account health state
- Manages cooldown timers
- Handles rate-limit `Retry-After` headers
- Marks account/model-specific unavailability

## Health Transitions

| Error Type | State | Recovery |
|------------|-------|----------|
| Authentication | `authentication_failed` | Config reload or explicit probe |
| Quota exhausted | `quota_exhausted` | Cooldown timer |
| Rate limited | `cooldown` | Retry-After or exponential backoff |
| Server error | `degraded` | Success resets consecutive failures |
| Model unavailable | `model_unavailable` | Targeted catalog refresh |

## Key Decisions

1. **Pre-stream only**: Never retry after response bytes emitted
2. **Invalid client errors don't count**: 400/404 don't affect account health
3. **Targeted refresh**: Model-specific failures trigger catalog refresh for that account
4. **Observable attempts**: All retry attempts recorded in `request_attempts` table
