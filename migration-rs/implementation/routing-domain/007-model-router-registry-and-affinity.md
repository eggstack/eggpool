# D007 — Model-Router Registry and Affinity State

Status: queued behind D006 closure for serial handoff

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d007--model-router-compiled-registry-and-affinity-state`

Primary class: capability/invariant

## 1. Objective

Port the deterministic, bounded state of EggPool virtual model routers without pulling coordinator-backed semantic selector execution into M5.

D007 owns immutable virtual-router compilation, deterministic fingerprints/static policy, exact virtual-model lookup, bounded session identity derivation, TTL/LRU affinity, keyed single-flight, and diagnostic counters. M7 will later own selector requests that invoke `RequestCoordinator` and will use D007 to remember/reuse concrete decisions.

## 2. Boundary with Python selector

Python `model_router.selector.ModelRouterSelector` compiles a selector prompt and calls `RequestCoordinator.execute`. That behavior is intentionally **not** implemented in D007.

D007 should expose interfaces equivalent to:

- lookup compiled virtual router by requested virtual model ID;
- derive an affinity/session key from explicit header or bounded normalized conversation-prefix input;
- get a still-valid concrete-model affinity decision;
- run a caller-supplied async selection closure under keyed single-flight and remember the returned concrete model if valid;
- invalidate naturally when router config fingerprint changes.

The caller-supplied selection closure is test-only/generic in M5. M7 will later provide the real semantic selector/coordinator implementation.

## 3. Compiled registry

Port immutable generation-owned structures equivalent to `CompiledModelRoute`, `CompiledModelRouter`, and `ModelRouterRegistry`.

Preserve:

- exact virtual router ID/alias lookup;
- ordered/sorted route compilation as frozen by D001;
- stable route IDs, labels, concrete model targets, and descriptions;
- default/fallback route/model identity;
- sticky/affinity enablement and TTL;
- selector timeout/max-input/repair-attempt limits as configuration facts;
- static selector policy bytes;
- deterministic config fingerprint;
- duplicate/invalid route/alias validation;
- no mutation of compiled registry after publication.

The registry may be `Arc`-shared. Do not add a dynamic plugin/DI registry.

## 4. Fingerprinting

Reproduce Python's deterministic length-delimited SHA-256 fingerprint input exactly where D001 classifies it as exact parity. Do not rely on debug/JSON map ordering.

The fingerprint must change when any selector/route/static-policy fact that could change a decision changes, and remain stable when unrelated config ordering changes.

Keep concrete target model names out of any static selector policy field where Python deliberately separates them for cache safety, while including them in the config fingerprint/compiled route identity where required.

Add golden fixtures for route-order permutations and all fingerprint-sensitive fields.

## 5. Static selector policy

Port the static policy text/bytes compilation used to guide the semantic selector. Treat bytes and length as exact contract where D001 freezes them.

Do not add model-specific prompt optimizations, templates, or prose changes during migration. The real selector request construction remains M7; D007 only returns the exact immutable policy fragment and compiled route data it will need.

## 6. Explicit session identity

Preserve the `x-eggpool-route-session` contract:

- bounded at 512 bytes;
- empty/invalid/control-character/oversized values do not become raw cache keys;
- valid value is hashed with SHA-256 before entering affinity storage;
- raw header value is never logged, persisted, or returned by diagnostics.

Use explicit domain-separated framing for the digest if and only if the Python fixture does; otherwise preserve exact current digest behavior for parity.

## 7. Automatic session identity without M6 coupling

Python automatic identity derives from a bounded normalized system/developer + first-user prefix and is disabled for Responses requests. D007 must preserve the algorithm without defining the full future `CanonicalRequest`.

Introduce a minimal `AffinityIdentityInput`/`ConversationPrefix` value containing only already-normalized text-role fragments and surface identity. M6 will later adapt canonical requests into this value.

Preserve bounds:

- automatic prefix maximum 4,096 bytes;
- first-user reserve minimum 1,536 bytes;
- only the frozen early-role/text fields participate;
- tools/media/later turns/generation parameters are excluded where Python excludes them;
- Responses surface automatic identity remains disabled where Python requires an explicit session key.

No raw prefix text is retained after digest derivation.

## 8. Affinity key and entry

Affinity key is scoped by at least:

- virtual model/router ID;
- compiled config fingerprint;
- session digest.

Entry stores only the derived concrete model decision and timestamps/expiry metadata required for TTL/LRU operation. Before returning a hit, validate that the concrete model still exists in the current compiled route set; an obsolete target becomes a miss and is removed.

Do not pin provider/account identity in the semantic affinity. The concrete model is sticky; D006/M7 still choose the healthy account/provider for that model.

## 9. TTL/LRU bounds

Match Python's process-local bounded cache behavior:

- maximum 4,096 entries;
- TTL from compiled router policy;
- access/touch ordering as frozen by D001;
- expired entries removed lazily/boundedly;
- insertion at cap evicts least-recently-used state;
- config fingerprint change naturally produces a new key space and old entries become unreachable/evictable;
- restart clears in-memory affinity unless Python currently persists it (it does not).

Avoid one Tokio task/timer per entry. Store deadlines and prune on access/in bounded maintenance.

## 10. Keyed single-flight

Port the per-key concurrent miss behavior so two equivalent concurrent requests do not trigger two selector calls.

Required semantics:

- first miss becomes leader;
- later equal-key misses join the leader;
- success is stored once and all joiners receive the same concrete model;
- selector error/cancellation releases the key so a later caller can become leader;
- a cancelled joiner does not cancel the leader unless Python's frozen semantics require it;
- no unbounded per-key lock map remains after completion;
- single-flight bookkeeping is capped/pruned consistently with the affinity cache.

Use a small async synchronization primitive; no actor framework.

## 11. Diagnostics

Expose aggregate bounded statistics equivalent to Python where public/operator parity needs them:

- hits;
- misses;
- expirations;
- evictions;
- leaders;
- joins;
- current entry count;
- current inflight key count.

Diagnostics may expose router ID/fingerprint and hashed key prefixes only if already permitted; never expose raw session headers or conversation prefix text.

## 12. Validation of selected model

The generic `get_or_select` boundary must validate that a caller-supplied selected concrete model is one of the compiled routes for this router before caching it. Unknown model results fail closed as selector-policy errors and are not stored.

D007 does not test whether the concrete model is currently routable/healthy; D006/M7 owns current routing eligibility and may discard/reselect an affinity decision when no eligible account remains.

## 13. Differential tests

Cover:

- disabled/no routers;
- exact alias lookup;
- route ordering permutations;
- fingerprint golden cases;
- static policy exact bytes;
- config fingerprint invalidation;
- explicit session valid/invalid/control/oversized cases;
- automatic identity bounds/role inclusion/exclusion;
- Responses automatic-identity disabled behavior;
- TTL hit/expiry;
- LRU touch/eviction at 4,096;
- obsolete selected route invalidation;
- concurrent leader/joiner success;
- selector error/cancellation and later recovery;
- selected model not in route set;
- secret/raw-content absence from Debug/snapshots.

Use a deterministic fake selector closure returning known concrete models. Do not call an LLM/provider.

## 14. Resource posture

Affinity/flight state is intentionally process-local and bounded. Avoid storing full prompts, canonical requests, headers, tool schemas, or response objects. Hash input immediately after bounded normalization.

Characterize memory with 4,096 entries and many transient single-flight keys; no hard benchmark threshold is required, but growth must converge at configured caps.

## 15. Acceptance criteria

D007 closes only if:

- compiled router identity/fingerprint/static policy match the oracle;
- no real selector inference request exists in Rust M5;
- explicit and automatic session identities match frozen behavior;
- raw session/request content is not retained;
- affinity and single-flight maps are bounded;
- concurrent misses single-flight correctly and recover from cancellation/errors;
- cache only stores models from the compiled route set;
- account/provider selection remains outside semantic affinity.

## 16. Stop conditions

Do not close if D007 imports/duplicates the future M6 canonical request model, calls `ProviderHttpClient` to choose a model, stores raw conversation/session content, makes affinity account-specific, leaves an unbounded single-flight map, or changes selector policy wording/fingerprint semantics without an ADR-supported contract change.