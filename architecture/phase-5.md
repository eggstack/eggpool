# Phase 5: Usage Extraction and Price Accounting

## Overview

Protocol-specific usage adapters, price snapshot storage, cost calculation, and accounting quality tracking.

## Components

### Usage Adapters (`proxy/usage.py`)
- **OpenAI adapter**: Parses `usage` field from response/stream
- **Anthropic adapter**: Parses `usage` block from response/stream
- Shared `UsageResult` model for normalized output

### Price Snapshots (`catalog/pricing.py`)
- Immutable price records per model
- Sources: upstream metadata, config overrides, built-in fallbacks
- Historical price preservation for accurate cost calculation

### Cost Estimation (`catalog/estimator.py`)
- EWMA (Exponentially Weighted Moving Average) per model
- Separate estimates for streaming vs non-streaming
- Configurable alpha (decay factor)
- Exact/derived observations weighted higher than estimated

### Exactness Classification

| Level | Description |
|-------|-------------|
| `exact` | Upstream provided cost and usage |
| `derived` | Upstream provided tokens; cost calculated from price snapshot |
| `estimated` | Cost inferred from historical averages |
| `unknown` | Request couldn't be accounted for reliably |

## Cost Calculation

```
cost = input_tokens × input_rate
     + output_tokens × output_rate
     + cache_read_tokens × cache_read_rate
     + cache_write_tokens × cache_write_rate
```

All arithmetic uses integers with rates normalized per million tokens.

## Key Decisions

1. **Immutable snapshots**: Historical costs never recalculated
2. **Integer arithmetic**: Avoids floating-point rounding errors
3. **Conservative estimation**: Unknown models use fallback pricing
4. **Accounting visibility**: Dashboard shows exactness proportions
