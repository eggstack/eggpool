# Plan 150 — Runtime Wire Negotiation and Single-Flight Governor

Date: 2026-09-02
Status: ready after Plans 148–149
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Depends on: Plans 148–149
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f`
Priority: P0 routing resilience
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Add bounded, evidence-driven runtime learning of the upstream wire profile used by a provider/model. A previously successful profile is a preference, not permanent truth. When the upstream deterministically rejects that profile before inference begins, EggPool may try another configured compatible profile, learn the successful result, and use it for subsequent requests without restart or configuration edits.

Negotiation must be reactive, single-flight, rate-limited, memory-bounded and subordinate to the request's existing total retry budget.

This phase owns **selection state and concurrency control**. Plan 151 owns the failure signals/effects that authorize a negotiation transition. Plan 152 supplies all concrete surface codecs.

---

# Governing invariants

1. No background probe loop.
2. No periodic synthetic inference requests.
3. No retry to another surface on ambiguous failures that may have started inference.
4. No one-request Cartesian product of all accounts and all surfaces.
5. Concurrent requests encountering the same stale provider/model profile must not independently probe alternates.
6. A successful ordinary request is sufficient to learn/refresh a profile.
7. Learned state is a performance/correctness hint and must be easy to invalidate.
8. Learned state must never permanently mark an account credential invalid.
9. Surface negotiation occurs before downstream handoff only.
10. Rehash/config changes must not keep learned profiles whose candidate definition changed.

---

# Process-owned resolver

Add one process-owned service, for example:

```text
WireProfileResolver
```

owned at the same lifecycle level as other process-level services that should survive safe generation swaps.

Do not instantiate independent resolvers per request or per worker helper.

Suggested cache key:

```text
(provider_id, canonical_model_id, candidate_fingerprint)
```

The candidate fingerprint must change when relevant provider configuration changes, including at least:

- candidate surface set;
- path templates;
- selected auth shape;
- relevant static headers;
- explicit fixed/preferred model override.

It must not include the secret/API key value.

A simple deterministic hash of normalized structural config is sufficient. Do not add cryptographic signing machinery.

---

# Learned entry

Use a small slotted/dataclass structure approximately:

```text
WirePreferenceEntry
  preferred_surface
  last_success_monotonic / wall-clock metadata as appropriate
  source
  confidence timestamp
  candidate rejection records
```

Per candidate retain only:

```text
last_deterministic_rejection_at
suppress_until
last_rejection_class
```

Do not store raw upstream response bodies.

### Source precedence

Resolve the first candidate in this order:

1. explicit operator `fixed=true` model surface, if implemented by Plan 148;
2. recent learned runtime success;
3. explicit operator non-fixed preference;
4. current model metadata hint from a verified upstream/catalog source, if present;
5. bundled `_wire_profiles.toml` hint;
6. provider candidate priority/order.

If an operator fixed a profile, do not negotiate away from it. Return the normal failure to the client and leave account health decisions to Plan 151.

### Learned TTL

`learned_preference_ttl_s` is an aging/eviction mechanism, not a forced-probe timer.

If the learned profile is older than TTL but remains configured and has not been rejected, it should remain the first candidate or fall back to the strongest current static hint according to one documented deterministic rule. In neither case should EggPool proactively try another surface before the preferred attempt fails.

Ordinary success refreshes the entry.

---

# No SQLite persistence in first implementation

Do not add a database table for learned wire profiles.

Reasons:

- a restart naturally clears possibly stale runtime assumptions;
- current provider/config hints are enough to seed the first request;
- a single relearning event after restart is acceptable;
- avoiding writes matters for small SBC deployments;
- persistent protocol history creates migration and stale-truth problems that are not needed for correctness.

If future operational evidence shows repeated restart negotiation is material, persistence can be separately evaluated after the in-memory state machine is stable.

---

# Negotiation state machine

The normal attempt sequence should be conceptually:

```text
resolve ordered candidate list
        |
        v
try preferred candidate
        |
        +-- accepted -> record success -> normal response lifecycle
        |
        +-- ordinary failure -> normal failure policy; no surface search
        |
        +-- explicit NEGOTIATE_WIRE -> acquire negotiation ownership
                                      |
                                      v
                               choose next unsuppressed candidate
                                      |
                                      v
                                same account dispatch
                                      |
                    +-----------------+------------------+
                    |                                    |
                 accepted                        safe rejection
                    |                                    |
             record preferred                      suppress candidate
             release ownership                    next candidate if budget
```

The negotiator must not itself interpret raw HTTP status/body. Plan 151's canonical `FailureEffects`/signal must say whether the failure authorizes a wire transition.

---

# Same-account first for surface mismatch

A deterministic surface rejection is not evidence that another API key will help.

Therefore a wire transition should normally use the **same selected account** first.

This is critical to avoid:

```text
3 surfaces × N accounts
```

behavior.

Only an independent account-scoped failure effect such as confirmed invalid credential or 429 should cause normal account failover, and that account failover should preserve the best-known surface.

Plan 151 defines the retry scopes exactly.

---

# Single-flight per provider/model

Use one in-flight negotiation owner per `(provider_id, canonical_model_id)`.

The owner is the first request that receives an authorized wire-transition failure.

Other concurrent requests must not enumerate candidates independently.

Preferred follower behavior:

1. if another healthy provider/account route is readily available under existing routing, it may use that route rather than block;
2. otherwise wait only for the **wire acceptance decision**, not for the leader's full model generation;
3. after the leader has an accepted alternate response status/stream start, consume the newly learned surface and dispatch normally;
4. if the leader concludes no candidate is usable, followers receive/use that bounded negative result and do not immediately repeat the same probes.

Do not hold a negotiation lock through the entire generated response body.

The outbound client should already be capable of obtaining response status/headers before consuming a streaming body; use that boundary as the acceptance point where practical.

---

# Provider-wide negotiation semaphore

Add a second small guard keyed only by provider:

```text
max concurrent negotiation dispatches per provider = 1 by default
```

This is distinct from normal inference concurrency. It controls only requests that are trying a candidate because the preferred surface was just rejected.

A provider hosting many models can therefore continue serving normal known-good requests while EggPool performs one abnormal contract-discovery transition at a time.

Do not serialize all provider inference requests.

---

# Minimum negotiation interval / upstream pressure

Use the Plan 148 `min_negotiation_interval_s` to prevent rapid repeated control-plane attempts against a provider.

Implementation may use a monotonic timestamp per provider:

```text
next_negotiation_allowed_at
```

No token-bucket dependency/library is necessary.

### 429 / Retry-After

If any negotiation candidate attempt returns a rate-limit/quota effect:

- stop candidate enumeration immediately;
- do not mark the surface rejected;
- set `next_negotiation_allowed_at` from bounded `Retry-After` when available, otherwise the existing rate-limit fallback;
- release negotiation ownership;
- allow the normal account-rate-limit routing policy to decide whether a different account/provider can serve the request using the same preferred surface.

A 429 says nothing about which endpoint grammar is correct.

### Provider-wide vs account-wide rate limit

Do not infer a new durable provider-global rate-limit state merely because one account gets 429. The provider-wide timestamp in this plan governs **negotiation attempts only** so multiple account retries cannot hammer endpoint discovery while the upstream is applying pressure.

---

# Deterministic rejection cooldown

When Plan 151 authorizes a wire transition because candidate A is structurally incompatible, suppress candidate A for the configured `rejection_cooldown_s`.

If candidate B succeeds, B becomes preferred and A remains temporarily suppressed.

When the cooldown expires, A is merely eligible again if B later fails. EggPool must not probe A just because the cooldown ended.

If every candidate is suppressed/rejected, return one deterministic upstream-unavailable/capability failure according to the coordinator's existing error rendering. Do not loop.

---

# Candidate enumeration

The resolver returns an ordered list with:

- fixed preference if any;
- learned/static preference;
- remaining configured candidates in stable priority order;
- currently suppressed candidates omitted unless all alternatives are unavailable and a deliberate cooldown-expiry rule permits them.

Do not attempt a candidate whose codec is unavailable or whose config validation failed; such configuration should normally fail earlier at startup/check-config.

Do not synthesize arbitrary paths such as `/responses` for a provider that did not declare that candidate.

---

# Interaction with model/account eligibility

Wire selection occurs after EggPool has a candidate account/provider/model route but before the outbound body is finalized.

The router should not permanently exclude an account merely because its provider exposes several surfaces.

Surface capability/codec representability may affect route eligibility when the canonical request contains a feature that a candidate surface cannot encode. Preferred design:

- route selection establishes provider/account/model candidates;
- wire resolver filters that provider's candidate surfaces by codec/capability representability;
- if no surface can represent the canonical request, produce a local capability error with no account penalty.

Do not send requests to discover a fact EggPool already knows locally from the codec/capability system.

---

# Rehash/generation behavior

The resolver should be process-owned but generation-aware through structural candidate fingerprints.

After rehash:

- unchanged provider/model candidate fingerprint may reuse learned preference;
- changed path/auth/surface set generates a new key and therefore cannot use stale learned state;
- removed providers/models become naturally unreachable and their old cache entries are evicted later;
- no explicit database cleanup is required.

Keep cache cleanup lazy/bounded rather than adding a maintenance thread.

A simple LRU/ordered eviction when `cache_max_entries` is exceeded is sufficient.

---

# Observability

Add only low-cardinality existing metrics/log fields needed to diagnose negotiation:

```text
wire_surface_selected
wire_selection_source
wire_negotiation_attempted
wire_negotiation_result
wire_candidate_rejected
wire_singleflight_follower
```

Prefer counters and bounded structured logs over new persisted tables.

Never log:

- API keys/auth headers;
- request text;
- tool arguments;
- raw upstream error body;
- complete path/query if it can contain secrets.

A compact debug log may include provider ID, canonical model ID, surface IDs, sanitized status/failure class and attempt number.

---

# Expected code surfaces

Likely files:

- new `src/eggpool/wire/resolver.py`;
- new `src/eggpool/wire/state.py` if needed;
- app/generation lifecycle service construction;
- `src/eggpool/request/coordinator.py`;
- `src/eggpool/request/upstream_helpers.py` (replace direct protocol/surface URL selection with resolved profile consumption);
- request context fields;
- routing attempt state;
- existing metrics/logging utilities;
- focused concurrency/routing tests.

Do not implement provider codecs here beyond test doubles/minimal existing adapters.

---

# Required deterministic tests

Use a synthetic provider with three candidate profiles A/B/C.

## Learned success

- no cached entry -> configured preferred A;
- A accepted -> A recorded preferred;
- next request uses A directly with one upstream submission.

## Surface migration

- cache A as preferred;
- synthetic provider changes: A returns explicit safe surface rejection, B accepts;
- one request tries A then B within total attempt budget;
- B becomes preferred;
- next request uses B directly;
- no restart/rehash required.

## No proactive TTL probe

- age learned B past preference TTL;
- if B is still chosen under the implementation's documented stale-hint rule, only B is sent;
- if static hint becomes preferred after TTL, only that preferred request is sent first;
- no alternate is sent until a safe rejection occurs.

## Ambiguous failure

For timeout-after-send, 5xx, connection reset after write or midstream EOF:

- no alternate surface request occurs;
- learned surface is not invalidated;
- normal failure/account handling applies.

## Single-flight stampede

Issue e.g. 20 concurrent requests with stale A:

- only one request owns A->B negotiation at a time;
- followers do not independently probe B/C;
- after B is accepted followers converge on B;
- bound total negotiation-only submissions independent of follower count.

Do not require a 20×provider matrix; one concurrency test is enough.

## Provider semaphore

Two models simultaneously require negotiation on one provider:

- at most configured negotiation concurrency is observed;
- known-good normal requests remain able to run.

## 429

During negotiation B returns 429 + Retry-After:

- C is not tried;
- B is not marked structurally incompatible;
- provider negotiation timestamp is delayed;
- normal account failover may occur separately under Plan 151 semantics.

## Rehash

- unchanged candidate fingerprint retains preference;
- changing B's path/auth produces new fingerprint and does not reuse old learned B entry.

## Cache bound

- inserting more than `cache_max_entries` evicts old entries without unbounded lock/map growth.

---

# Acceptance criteria

- [ ] Learned wire preference is runtime-revisable without restart.
- [ ] No background/scheduled surface probing exists.
- [ ] Runtime state is in-memory and bounded; no DB migration is introduced.
- [ ] Config changes invalidate stale state through a structural candidate fingerprint.
- [ ] Only Plan 151-authorized failures can trigger alternate-surface enumeration.
- [ ] Alternate-surface transition normally stays on the same account.
- [ ] Negotiation is single-flight per provider/model.
- [ ] Negotiation concurrency is independently bounded per provider without serializing normal inference.
- [ ] Followers wait only for wire-profile acceptance/convergence, not entire model generation, when they wait at all.
- [ ] 429/Retry-After stops negotiation and delays further provider negotiation attempts.
- [ ] Deterministically rejected profiles receive temporary negative-cache cooldown rather than permanent blacklisting.
- [ ] Expired cooldown/TTL never causes unsolicited probe traffic.
- [ ] All surface/account submissions consume the request's shared attempt budget implemented in Plan 151.
- [ ] Same-surface/known-good steady state adds only a bounded in-memory lookup and no network round trip.
- [ ] No new dependency, worker process, background task or persistence table is added.

---

# Rejection conditions

Reject implementation if it:

- probes all configured endpoints at startup;
- periodically probes models in the background;
- retries another surface after an ambiguous timeout/5xx/midstream failure;
- performs independent surface scans from every concurrent request;
- stores raw response bodies or secrets in learned state;
- adds DB writes on request success to persist the learned profile;
- scopes learned semantic surface state solely to account so every account has to rediscover the same provider/model endpoint;
- treats an account auth failure as proof the provider/model surface is wrong;
- creates a second unlimited retry counter separate from normal routing attempts;
- blocks all normal provider traffic behind the negotiation semaphore.

---

# Verification

Run focused resolver/state/concurrency tests and ordinary project gates. Use deterministic fake upstreams only in this phase; real-key convergence is reserved for Plan 153.

Record peak negotiation submission counts in the stampede test so regressions are obvious without adding a performance benchmark gate.

---

# Handoff

1. Read Plans 147–149 and current coordinator/generation lifecycle.
2. Implement bounded process-owned resolver/cache.
3. Add candidate fingerprinting and lazy eviction.
4. Add per-model single-flight and provider negotiation semaphore/interval.
5. Wire coordinator to ask for an ordered profile but gate transitions on a placeholder/Plan-151 effect.
6. Add synthetic migration, ambiguity, 429 and concurrency tests.
7. Run ordinary gate and record implementation SHA/results.
