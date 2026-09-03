# Deep Dive: Routing & Quota

Back to [Overview](overview.md)

## Purpose

Quota-aware routing selects the best upstream account for each request, balancing load across multiple accounts within the same provider and across providers with different priority tiers.

Semantic model-router selection is a separate pre-routing concern. When a
request names an exact virtual alias, `ModelRouterSelector` chooses a
configured concrete model with a bounded exact route-ID protocol; the
coordinator then routes that selector request and the eventual concrete target
through the ordinary account/quota path. Sticky routers may satisfy that
semantic decision from the process-owned affinity cache, but the cache never
pins an account/provider and never enters `QuotaFairScorer`. Selector policy,
prompt content, session identities, and route outcomes are not routing-score
inputs.

## Routing Architecture

```
Request arrives with model ID
    │
    ▼
┌────────────────────────────┐
│ Exact virtual alias?       │
│ affinity hit, or selector  │
│ → concrete model target    │
└──────────────┬─────────────┘
               │
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
- Enabled / authentication / quota / cooldown / rate-limit state
- Provider configuration
- Request surface support
- Protocol compatibility
- Health circuit breaker
- Quarantine
- Catalog/model support
- Thinking support (capability-aware routing)
- Local quota mode (`hard_cap` only)

### `routing/fairness.py` — FairnessRotor

Deterministic round-robin for same-tier peers:
- `fairness_mode`: `round_robin` (default), `random`, or `off`
- `fairness_epsilon`: score proximity threshold
- `fairness_scope`: rotation group granularity
  - `provider_model_protocol` (default)
  - `provider_model`
  - `priority_model_protocol`

Rotor position map capped at 4096 entries (`_ROTOR_HARD_CAP`). Eviction is LRU (oldest entry evicted on overflow).

### `routing/provider.py`

`parse_model_provider()` — canonical model/provider suffix parser. Input: `model-id/provider-id`. Output: `(model_id, provider_id)`.

### `model_router/affinity.py` — Semantic affinity

`ModelRouterAffinity` is a process-owned, event-loop-local TTL/LRU cache with a
hard cap of 4096 entries and bounded lazy cleanup. Keys are
`(virtual_model, router_fingerprint, session_digest)`. Explicit session headers
are SHA-256 digests; automatic identities use only bounded system/developer
text and the first user text on Chat/Messages surfaces. Responses requires the
explicit header for cross-request stickiness. Concurrent misses single-flight
one selector call per key; cancelled leaders release followers to retry. A
concrete target's downstream health or availability never changes the cached
semantic decision.

Semantic decisions also feed a process-local bounded metrics object. It counts
virtual requests, selector/default decisions, affinity hits/misses, fixed
fallback reasons, repair attempts/successes, resolution latency, and bounded
virtual-to-concrete selection pairs. It never records prompt text, selector
output, descriptions, raw session identities, or provider/account score data.

### `routing/config.py`

Routing configuration helpers.

`RoutingConfig.wire_negotiation` defines the bounded runtime negotiation
surface: enablement, per-provider concurrency, minimum interval, deterministic
rejection cooldown, learned-preference TTL, and cache size. The process-owned
`WireProfileResolver` uses these settings for in-memory ordering, candidate
cooldowns, provider/model single-flight, and provider-wide abnormal-dispatch
gating. It never probes in the background or adds a second retry budget.

The request coordinator owns one upstream-submission budget shared by normal
account failover and alternate-wire retries. A deterministic wire rejection
may retry the same account on another candidate; quota, rate-limit, model,
credential, transport, and server failures retain their explicit account or
model destinations and never trigger surface roulette.

Wire candidate preference is separate from account scoring. Provider surface
`priority` values and exact bundled/operator model preferences order possible
wire profiles; they do not override catalog protocol compatibility, health, or
later runtime evidence.

### `quota/scorer.py` — QuotaFairScorer

Load-based scoring with four inputs:
1. **Request count** — recent request volume
2. **Token count** — recent token volume
3. **Active count** — in-flight requests
4. **Health** — upstream health state

**Hardcoded invariant**: No cache or policy field ever enters the scorer. `RoutingScore` dataclass is audited by tests.

### `quota/estimation.py` — QuotaEstimator

Per-account quota window tracking (5h/7d/30d windows). Local estimates are advisory (`local_quota_mode = "score_only"` by default).

#### Pending claim publication

The coordinator uses the estimator's in-memory reservation mirror for one
provisional ownership path. Under `_selection_claim_lock`, a successful
health/account claim adds one pending request and its estimated tokens before
SQLite request/reservation/attempt persistence begins. `get_account_reserved_load`
includes pending and canonical load in one scorer snapshot, so a concurrent
selector cannot score the claimed account as idle.

After durable persistence, the second claim-lock section synchronously converts
the pending counters into the canonical reservation counters without a gap or
double count. Persistence failure, cancellation, or publication failure
releases the pending counters through the receipt/compensation owner. This is
process-local, single-loop accounting: there is no pending-claim table,
sweeper, background task, or cross-process reservation protocol.

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

When all eligible accounts are excluded (including quarantine) and the
request carries thinking controls, the error class depends on the
aggregated capability status and exact control match across every provider
entry for the model, not just the collapsed row. A 400 (`CapabilityError`)
is returned for an authoritative unsupported status or an exact unsupported
control dimension; unknown control metadata follows the configured unknown
policy.
When the aggregated status is `supported` or `mixed` but every
supporting account is quarantined, a transient 502/503 (`UpstreamError`,
`ModelUnavailableError`) is returned so the client can retry. This
prevents a misleading `thinking capability status: unknown` 400 from
being surfaced when the underlying provider does support the request
but is currently unhealthy across every account.

## Score Components

Every `routing_decisions` row carries `score_components_json`:
- Per-account score breakdown
- Per-window utilization ratios (`util_5h`, `util_7d`, `util_30d`)
- `tie_break` summary (decisive factor between chosen and runner-up)
- Fairness annotations (`applied`, `scope`, `key`, `candidate_count`)

## Key Invariants

- `QuotaFairScorer.score_accounts` accepts only 4 inputs: `account_names`, `model_name`, `active_requests`, `request_estimates`
- Cache metrics NEVER enter routing
- Semantic affinity and model-router metrics NEVER enter `QuotaFairScorer`
- Same-provider fairness preserved across adversarial cache profiles
- Priority tier boundaries are strict: lower-priority never advance ahead of higher-priority
- `weight` scales effective request/token capacity within a tier;
  `routing_priority` orders tiers
- A claimed account's pending request/token load is visible to later scoring
  before SQLite persistence, and conversion/release leaves no provisional
  residue
- Upstream-derived backoffs persist across restarts in `account_backoffs` table
- Durable backoffs are restart hints only; malformed, expired, unknown, or overlong rows have zero routing effect
- Local-estimate overage never produces a backoff row
