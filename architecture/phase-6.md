# Phase 6: Quota-Aware Routing and Reservations

## Overview

Rolling window quota tracking, cost estimation hierarchy, atomic reservations, and quota-fair account selection.

## Components

### Quota Windows (`quota/`)
- **estimation.py**: Rolling window queries (5-hour, weekly, monthly)
- **reservation.py**: In-flight reservation management
- **scorer.py**: Quota-fair account scoring

### Routing (`routing/`)
- **eligibility.py**: Account eligibility evaluation
- **router.py**: Account selection with atomic reservation

### Reservation Lifecycle

```
Creation (before dispatch)
    │
    ├── Generate proxy request ID
    ├── Calculate estimated cost
    ├── Select account via scoring
    ├── Insert reservation record
    ├── Increment in-memory reserved usage
    └── Insert initial request row

Success (on completion)
    │
    ├── Parse exact/derived usage
    ├── Calculate final cost
    ├── Remove reservation
    ├── Update request row
    ├── Decrement in-flight state
    ├── Update EWMA estimator
    └── Mark account success

Failure (pre-stream)
    │
    ├── Release reservation
    ├── Apply health consequences
    ├── Reserve against replacement account
    └── Increment retry count

Cancellation
    │
    ├── Cancel upstream reading
    ├── Release reservation
    ├── Persist captured usage
    └── Mark error class: client_cancelled
```

### Scoring Formula

For account `i`:

```
p5_i = (observed_5h + offset_5h + reserved + estimate) / capacity_5h
pw_i = (observed_7d + offset_week + reserved + estimate) / capacity_week
pm_i = (observed_30d + offset_month + reserved + estimate) / capacity_month

score_i = max(p5, pw, pm)
        + 0.15 × mean(p5, pw, pm)
        + inflight_count × 0.01
        + health_penalty
```

### Near-Tie Handling
- Accounts within `near_tie_epsilon` of best score
- Random selection weighted inversely by active request count
- Deterministic seeded RNG in tests

## Key Decisions

1. **Atomic selection+reservation**: Prevents race conditions under concurrency
2. **Conservative reservation**: Safety factor of 1.15× prevents chronic under-reservation
3. **Manual offsets**: Support for usage outside the proxy
4. **Crash recovery**: Stale reservations cleaned on startup
