# Phase 2: Account Registry and Model Discovery

## Overview

Account management, runtime health state, and dynamic model catalog discovery across multiple subscriptions.

## Components

### Account Registry (`accounts/registry.py`)
- Loads accounts from TOML configuration
- Validates API keys via environment variables
- Manages account lifecycle (enable/disable)
- Supports atomic SIGHUP reload

### Account State (`accounts/state.py`)
- In-memory runtime state per account
- Health state tracking (healthy, degraded, cooldown)
- Active request count and reserved microdollars
- Circuit breaker state and cooldown timers

### Model Catalog (`catalog/`)
- **fetcher.py**: Per-account model list retrieval via HTTPX
- **normalizer.py**: Protocol-specific response normalization
- **cache.py**: SQLite-backed persistent catalog cache
- **service.py**: Orchestration of refresh lifecycle
- **pricing.py**: Model price snapshot management
- **estimator.py**: EWMA-based cost estimation

### Protocol Resolution
Priority order:
1. Explicit TOML model override
2. Upstream model metadata
3. Known model-family rules
4. Last persisted protocol value

## Data Flow

```
Startup
    │
    ├── Load config accounts
    ├── Open database
    ├── Load cached catalog
    ├── Fetch /models per account (concurrent)
    ├── Normalize responses
    ├── Update models + account_models tables
    ├── Build in-memory registry
    └── Mark ready

Periodic Refresh
    │
    ├── Fetch per account (with jitter)
    ├── Update availability flags
    ├── Preserve historical models
    └── Trigger targeted refresh on 404
```

## Catalog Exposure Modes

- **union**: Model available if ≥1 healthy account supports it (default)
- **intersection**: Model available only if all enabled accounts support it
- **healthy_union**: Model available if ≥1 currently healthy account supports it

## Key Decisions

1. **Cached startup**: Allows degraded mode when upstream is temporarily unavailable
2. **Availability flags**: Model disappearance marks unavailable, never deletes history
3. **Per-account fetch**: One failed account doesn't block others
4. **Targeted refresh**: Upstream 404 triggers immediate catalog refresh for that account
