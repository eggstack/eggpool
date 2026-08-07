# Deep Dive: Routing & Quota

Back to [Overview](overview.md)

## Purpose

Quota-aware routing selects the best upstream account for each request, balancing load across multiple accounts within the same provider and across providers with different priority tiers.

## Routing Architecture

```
Request arrives with model ID
    │
    ▼
┌──────────────────────────┐
│ parse_model_provider()   │
│ → model_id, provider_id  │
└──────────────┬───────────┘
               │
    ┌──────────▼──────────┐
    │ Priority Grouping   │
    │ Group accounts by   │
    │ routing_priority    │
    │ (highest tier first)│
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Eligibility Filter  │
    │ health, protocol,   │
    │ catalog, backoff    │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ QuotaFairScorer     │
    │ 4-input scoring:    │
    │ • request count     │
    │ • token count       │
    │ • active count      │
    │ • health            │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Fairness Rotor      │
    │ Same-tier peers     │
    │ within fairness     │
    │ epsilon band        │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Circuit Breaker     │
    │ Accept/reject       │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Selected Account    │
    └─────────────────────┘
```

## Key Modules

### `routing/router.py` — Router

`build_routing_plan()` is the authoritative selection path (no fallback to legacy `select_accounts()`):

- `select_accounts_for_failover()` — returns ordered list of attempts
- `_score_eligible_accounts()` — runs `QuotaFairScorer` on eligible accounts
- `_collect_gate_status()` — per-account eligibility gate diagnostics
- `explain_account_eligibility()` — CLI diagnostic (reason codes, gate breakdown)

### `routing/eligibility.py`

`get_eligible_accounts()` — filters accounts by:
- Health circuit breaker
- Protocol compatibility
- Catalog/model support
- Backoff status
- Provider configuration
- Local quota mode

### `routing/fairness.py` — FairnessRotor

Deterministic round-robin for same-tier peers:
- `fairness_mode`: `round_robin` (default), `random`, or `off`
- `fairness_epsilon`: score proximity threshold
- `fairness_scope`: rotation group granularity
  - `provider_model_protocol` (default)
  - `provider_model`
  - `priority_model_protocol`

Rotor position map capped at 4096 entries (`_ROTOR_HARD_CAP`). Eviction is blunt (full clear).

### `routing/provider.py`

`parse_model_provider()` — canonical model/provider suffix parser. Input: `model-id/provider-id`. Output: `(model_id, provider_id)`.

### `routing/config.py`

Routing configuration helpers.

### `quota/scorer.py` — QuotaFairScorer

Load-based scoring with four inputs:
1. **Request count** — recent request volume
2. **Token count** — recent token volume
3. **Active count** — in-flight requests
4. **Health** — upstream health state

**Hardcoded invariant**: No cache, compression, segmentation, or policy field ever enters the scorer. `RoutingScore` dataclass is audited by tests.

### `quota/estimation.py` — QuotaEstimator

Per-account quota window tracking (5h/7d/30d windows). Local estimates are advisory (`local_quota_mode = "score_only"` by default).

### `quota/reservation.py`

Reservation management — tracks reserved cost/requests per account.

### `quota/audit.py`

Quota audit queries for diagnostics.

## Priority Tiers

`routing_priority` is per-provider (default `0`, must be `>= 0`). Higher values are preferred. The router selects the highest-priority tier with at least one eligible account. If every account in that tier fails, the request falls through to the next tier.

## Model Collapse

`[models].collapse_models` (default `false`):
- `false`: one provider-suffixed entry per `(model_id, provider_id)`
- `true`: same base model collapses to single unsuffixed ID, routed across all supporting providers

## Account Weight

`AccountConfig.weight` is a positive relative capacity/share hint within an
eligible priority tier. The scorer divides request-count and token-count
utilization pressure by the effective weight, so `2.0` is approximately twice
the effective capacity of `1.0` and `0.5` is approximately half. Persisted
load, reservations, offsets, and the projected request remain in the same
numerators. Cost remains diagnostic-only.

Priority tiers, health/circuit/quarantine, catalog, protocol, and native
preference retain precedence; weight only affects otherwise eligible accounts
in the selected tier. Equal weights preserve the baseline score semantics.

## Same-Tier Fairness

When accounts are effectively tied (same priority, health, protocol, score within `fairness_epsilon`):
- Round-robin rotor rotates candidates to avoid config-order bias
- Position tracked per fairness key (provider × model × protocol × priority × client_protocol)
- Fairness decisions recorded in `routing_decisions.score_components_json`

## Account Exclusion

Accounts excluded when:
- Upstream-observed failure inside backoff window
- Explicitly disabled/suspended by operator
- Model not supported (catalog/protocol incompatibility)
- Health circuit breaker open
- `local_quota_mode = "hard_cap"` AND local estimate exceeds capacity (opt-in legacy)

Transient upstream exclusions are self-healing: quota, rate-limit, server,
transport, protocol, and runtime model-absence suppression expires within at
most 1,800 seconds. A runtime model absence is scoped to the exact
account/model/protocol identity and cannot disable the whole account.
Authentication failure and authoritative catalog withdrawal are separate
terminal gates; they recover only through corrected credentials/operator action
or authoritative model reappearance.

## Score Components

Every `routing_decisions` row carries `score_components_json`:
- Per-account score breakdown
- Per-window utilization ratios (`util_5h`, `util_7d`, `util_30d`)
- `tie_break` summary (decisive factor between chosen and runner-up)
- Fairness annotations (`applied`, `scope`, `key`, `candidate_count`)

## Key Invariants

- `QuotaFairScorer.score_accounts` accepts only 4 inputs: `account_names`, `model_name`, `active_requests`, `request_estimates`
- Cache/compression metrics NEVER enter routing
- Same-provider fairness preserved across adversarial cache/compression profiles
- Priority tier boundaries are strict: lower-priority never advance ahead of higher-priority
- `weight` scales effective request/token capacity within a tier;
  `routing_priority` orders tiers
- Upstream-derived backoffs persist across restarts in `account_backoffs` table
- Durable backoffs are restart hints only; malformed, expired, unknown, or overlong rows have zero routing effect
- Local-estimate overage never produces a backoff row
