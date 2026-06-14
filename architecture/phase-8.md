# Phase 8: Statistics API and Dashboard

## Overview

Statistics query layer, JSON API endpoints, and server-rendered HTML dashboard for usage visualization.

## Components

### Statistics Service (`stats/`)
- **queries.py**: SQL query builders for all statistics
- **service.py**: Business logic and aggregation methods

### API Endpoints (`api/stats.py`)
| Endpoint | Description |
|----------|-------------|
| `GET /api/stats/summary` | Aggregate request/token/cost totals |
| `GET /api/stats/accounts` | Per-account utilization and health |
| `GET /api/stats/models` | Per-model usage and performance |
| `GET /api/stats/timeseries` | Time-bucketed metrics |
| `GET /api/stats/errors` | Error breakdown by class |
| `GET /api/events` | Recent operational events |

### Dashboard (`dashboard/`)
- **routes.py**: FastAPI route handlers
- **render.py**: Jinja2 template rendering
- **escape.py**: HTML escaping utilities
- **templates/**: Server-rendered HTML pages
  - `base.html` — Layout and navigation
  - `overview.html` — Summary metrics
  - `accounts.html` — Account utilization
  - `models.html` — Model statistics
  - `events.html` — Operational events

### Dashboard Pages

**Overview**:
- Total requests, tokens, cost
- Exact/derived/estimated proportions
- Active streams, error rate
- Average/p95 latency and time-to-first-byte

**Accounts**:
- Health state, cooldown, active requests
- Reserved estimated cost
- 5-hour/7-day/30-day projected utilization
- Request count, error count, last success/failure

**Models**:
- Protocol, supporting accounts
- Request count, observed cost, average cost
- Token counts, cache utilization
- Average/p95 latency, error rate

**Events**:
- Account cooldown entries
- Authentication failures
- Model appearance/disappearance
- Circuit breaker state changes

## Security Constraints

- No prompt or completion content
- No API keys or auth headers
- No account secret references
- All free-text fields bounded
- HTML escape all displayed data
- Parameterized SQL queries

## Key Decisions

1. **Server-rendered HTML**: No large frontend framework
2. **Same service layer**: Dashboard and JSON API share statistics queries
3. **Period selection**: Configurable time ranges with filters
4. **Accounting quality**: Dashboard shows exactness proportions
