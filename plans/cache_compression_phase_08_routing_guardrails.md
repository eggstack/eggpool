# Phase 8 Plan: Routing Guardrails and Non-Interference Guarantees

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- Phase 1 cache/token observability
- Phase 4 observe-mode compression accounting
- Phase 5 safe suffix compression
- Phase 6 policy controls
- Phase 7 dashboard/runtime views

## Summary

EggPool's primary purpose is provider/account aggregation and routing, especially across many same-provider accounts such as multiple Opencode Go subscriptions. Cache and compression metrics are valuable for observability and request shaping, but they must not silently skew same-provider account distribution.

Phase 8 formalizes this as a routing invariant:

> Cache/compression metrics are reporting-only by default. They must not enter provider/account scoring, account removal, retry routing, or pool eligibility unless an explicit future routing mode is designed, documented, and tested.

This phase adds defensive code boundaries, tests, diagnostics, and documentation that prove cache/compression cannot accidentally influence routing.

## Non-goals

- Do not implement cache-optimized routing.
- Do not implement cost-optimized routing using cached-token economics.
- Do not change quota-fair scoring weights.
- Do not change provider health removal semantics.
- Do not add semantic routing based on request content.
- Do not reroute after compression unless the existing dispatch/retry path already does so for health/transport errors.

## Threat model

The likely failure modes are subtle:

1. A future stats field such as `cached_input_tokens` is passed into route scorer input and changes account selection.
2. A provider with high cache hit ratio gets favored even though the operator wanted even same-provider subscription usage.
3. A provider with low compression savings is treated as less eligible.
4. Compression failure/fallback marks a provider unhealthy.
5. Context-limit changes after compression trigger an implicit second routing pass.
6. Policy resolution after route selection causes a reroute or account switch.
7. Dashboard metrics are misread as routing inputs because docs are ambiguous.

Phase 8 should close these paths with code-level separation and regression tests.

## Current invariant

The active route scorer should depend only on existing routing inputs such as:

- provider/account enabled state,
- health status,
- active request count,
- request count/quota utilization,
- token count/quota utilization where already designed,
- model/protocol eligibility,
- configured weights/priorities,
- explicit provider/model routing policy.

It should not depend on:

- `cache_counter_status`,
- `cached_input_tokens`,
- `cache_read_input_tokens`,
- `cache_creation_input_tokens`,
- `stable_prefix_hash`,
- `stable_prefix_content_hash`,
- `request_shape_hash`,
- `compression_status`,
- `compression_mode`,
- candidate counts,
- estimated or actual compression savings,
- compression latency,
- compression policy name,
- transform reason counts.

## Implementation tasks

### 1. Identify routing input boundary

Audit route selection code and define the exact object/function boundary where route scoring inputs are built.

Add or update a small typed model if useful, for example:

```python
@dataclass(frozen=True, slots=True)
class RoutingDecisionInput:
    requested_model: str
    protocol: str
    eligible_providers: tuple[ProviderCandidate, ...]
    client_id: str | None
    # no cache/compression fields
```

The key is not the exact type. The key is that routing inputs are explicit and do not accept arbitrary request/finalizer metadata blobs.

### 2. Add forbidden-field tests

Add tests asserting route-scoring code does not reference cache/compression fields.

Options:

- Pure behavioral tests: vary cache/compression metrics while holding route inputs fixed; assert the same provider sequence/score ordering.
- Static-ish tests: construct route input from a request metadata object that includes cache/compression fields and assert those fields are not present in the scorer input.
- Snapshot tests: expected scorer inputs contain only approved keys.

Behavioral tests are mandatory; static tests are optional.

### 3. Same-provider account fairness regression

Build a focused regression suite for same-provider pools.

Scenarios:

1. Two Opencode Go-like accounts with equal usage and health.
2. Account A has high cache hit ratio; account B has low/unknown cache hit ratio.
3. Account A would produce higher compression savings; account B would produce none.
4. Account A has many stable-prefix cache hits; account B has none.
5. Account A previously had compression fallback; account B did not.

Expected result:

- Selection order follows existing fairness tie-break behavior.
- Cache/compression deltas do not alter score/order.
- Compression fallback does not mark account unhealthy.

### 4. Provider health separation

Confirm provider health is affected only by upstream/transport/HTTP health signals currently defined by the router.

Add tests:

- Safe compression fail-closed fallback before dispatch does not increment provider error counters.
- Observe-mode analyzer exception, if one is ever caught, does not mark provider unhealthy.
- Compression latency-budget warning does not mark provider unhealthy.
- Provider can still be marked unhealthy by actual dispatch failures as before.

### 5. No post-compression reroute by default

Safe compression can change estimated token counts. This must not cause an implicit second route selection pass by default.

Add tests:

- Route is selected.
- Safe compression applies.
- Request dispatch uses the originally selected provider/account.
- Compression result does not call route selection again.

If existing code routes after compression, document that and assert compression metadata is still not scorer input. The key invariant is no cache/compression-based scoring.

### 6. Policy resolver non-interference

From Phase 6, provider-specific compression policy may resolve after route selection. That is acceptable only if it does not change the selected route.

Add tests:

- Provider-specific policy disables compression after a route is selected; route remains selected.
- Provider-specific policy enables safe compression after a route is selected; route remains selected.
- Policy warning/fallback does not cause route removal.

### 7. Add runtime diagnostic surface

Add a small diagnostic field to runtime stats or route debug output:

```json
{
  "routing_cache_compression_mode": "reporting_only",
  "routing_uses_cache_metrics": false,
  "routing_uses_compression_metrics": false,
  "route_scorer_inputs": ["health", "quota", "active_requests", "model_eligibility"]
}
```

This should be hard-coded or derived from scorer configuration, not from request content.

### 8. Documentation

Update README/architecture docs:

- State that cache/compression metrics are not routing inputs by default.
- Explain why: same-provider account fairness and subscription balancing.
- Explain future work would need an explicit opt-in routing mode.
- Explain provider health is separate from compression fallback.

## Future opt-in mode boundary

Do not implement this now, but document the future boundary:

A future cache-aware routing mode would need:

- an explicit config flag such as `routing.cache_aware = true`,
- same-provider fairness controls,
- per-provider support detection,
- cost model using cached-read/write token prices,
- backtesting metrics,
- per-client opt-in,
- clear dashboard warnings.

The existence of this note should prevent accidental partial implementation in Phase 8.

## Test plan

### Unit tests: scorer inputs

- Route scorer ignores cache fields.
- Route scorer ignores compression fields.
- Route scorer ignores stable/request shape hashes.
- Route scorer ignores compression policy names.

### Unit tests: provider health

- Compression fallback does not increment provider error count.
- Analyzer warnings do not increment provider error count.
- Latency-budget warnings do not increment provider error count.
- Actual upstream failure still increments provider error count.

### Integration tests: request path

- Route once, compress, dispatch same provider.
- Provider-specific policy post-route does not reroute.
- Same-provider account distribution remains unchanged with varied cache/compression metrics.

### Dashboard/API tests

- Runtime diagnostic says cache/compression routing mode is reporting-only.
- Dashboard note renders.
- No endpoint implies cache/compression is active in scoring.

## Acceptance criteria

- There is a clear code boundary between routing inputs and cache/compression metadata.
- Same-provider fairness tests pass with adversarial cache/compression values.
- Compression fallback does not affect provider health.
- Safe compression does not cause implicit route reselection.
- Policy resolution does not cause route reselection.
- Runtime/dashboard diagnostics state reporting-only behavior.
- Documentation explicitly describes non-interference.
- Full tests, ruff, and pyright pass.

## Rollback notes

This phase should be mostly tests/docs/diagnostics. If any runtime diagnostic causes trouble, remove the diagnostic endpoint/card without touching routing. If a behavioral test fails, treat that as a real routing regression rather than weakening the test.