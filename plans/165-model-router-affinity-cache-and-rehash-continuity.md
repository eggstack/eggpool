# Plan 165 — Model-Router Affinity, Cache Locality, and Rehash Continuity

Date: 2026-09-03
Status: ready for implementation
Planning baseline: `525189763a3a6d506e9e8001e2426c9bd9a247fe`
Parent roadmap: `plans/162-optional-llm-model-router-selection-roadmap.md`
Depends on: `plans/163-model-router-config-registry-and-virtual-foundations.md`, `plans/164-model-router-selector-dispatch-and-minified-prompt.md`
Priority: P1 cache efficiency / concurrency correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Add bounded process-local semantic route affinity so a conversation that initially resolves a virtual model to a concrete model normally stays on that same concrete model. The objective is to preserve provider/model prompt-cache locality and avoid repeatedly paying selector latency or spraying one evolving transcript across unrelated models.

Affinity is deliberately above the existing provider/account router. It may pin a **concrete model**, but it must not pin a specific account/provider unless the route itself is explicitly provider-qualified. The existing `routing.Router` remains free to choose among eligible accounts/providers that serve the resolved concrete target.

---

## Core behavior

For a sticky router:

```text
first request for session
    |
    +-- affinity miss
    +-- selector/default resolves target A
    +-- store session -> target A

later request for same session
    |
    +-- affinity hit -> target A
    +-- selector is not invoked
    +-- normal provider/account routing for target A continues
```

Do not ask the selector on every turn and merely bias toward the previous result. Sticky routing is intentionally stronger because model-level prefix/KV caches are most valuable when the growing conversation remains with one model.

`sticky = false` disables this semantic affinity and allows each request to invoke the selector normally.

---

## 1. Runtime ownership

Add a process-owned bounded affinity service to `ProcessRuntime`, separate from the generation-owned `ModelRouterRegistry`.

Recommended location:

- new `src/eggpool/model_router/affinity.py`;
- `ProcessRuntime.model_router_affinity` (or similarly explicit name).

Why process-owned:

- staged `rehash` swaps generations while the process remains alive;
- an unrelated config change should not force every active conversation to reclassify;
- the router configuration fingerprint from Plan 163 provides a safe compatibility boundary;
- process ownership matches the existing precedent of process-owned learned runtime state without requiring persistence.

Do not store affinity in module globals. Tests and multiple application instances must remain isolated.

---

## 2. Bounded TTL/LRU cache

Implement a small standard-library in-memory cache with:

- hard maximum entry count appropriate to local/LAN deployments (for example 4096; choose one bounded default and document it);
- per-entry expiration derived from the router's `affinity_ttl_s`;
- LRU eviction when full;
- lazy expiry on access/insertion plus bounded cleanup, not a background sweeper;
- single-event-loop ownership consistent with EggPool's request execution model;
- no SQLite persistence;
- no external cache dependency.

An entry should contain only non-sensitive derived routing state:

```python
@dataclass(frozen=True, slots=True)
class AffinityDecision:
    virtual_model: str
    router_fingerprint: str
    session_digest: bytes | str
    route_id: str
    route_label: str
    concrete_model: str
    source: Literal["selector", "default"]
    expires_at_monotonic: float
```

Do not store original request text, selector prompt, raw header values, authentication data, or raw selector output.

Monotonic time should govern TTL expiration inside the process. Do not require wall-clock persistence semantics.

---

## 3. Explicit session identity

Support the client header:

```text
X-EggPool-Route-Session: <opaque client session id>
```

Contract:

- only meaningful when the requested model is a configured sticky virtual model;
- bounded header length (for example <= 512 bytes) with malformed/control-character values ignored or rejected according to existing request-header conventions;
- hash immediately with SHA-256 or equivalent standard-library cryptographic hash;
- never log the raw value;
- never persist the raw value;
- never include it in selector prompts;
- never forward it upstream;
- route affinity uses the digest, not the raw header.

Prefer treating an invalid/oversized affinity header as unavailable affinity rather than rejecting an otherwise valid inference request, unless existing EggPool security conventions strongly prefer a 400. Whichever behavior is chosen must be deterministic and documented.

Add a provider-request regression test proving the EggPool-only header is absent from upstream headers. Current provider header construction should already avoid blindly forwarding client headers; preserve that contract.

---

## 4. Automatic affinity key when no header exists

Many Chat Completions/Messages clients resend a growing transcript but do not provide a stable conversation ID. Derive a best-effort automatic session fingerprint only when a stable semantic prefix is available cheaply.

Requirements:

1. Use request semantic structure, not the entire evolving JSON body.
2. Prefer stable initial context such as system/developer instruction plus the first user turn.
3. Ignore later assistant/user/tool turns when computing the session identity so the key remains stable as history grows.
4. Exclude tool schemas, tool outputs, binary/base64/image/PDF bodies, timestamps, request IDs, generation options, and provider metadata.
5. Hash the bounded canonical prefix immediately; store only the digest.
6. If there is no trustworthy stable prefix, return "no automatic affinity key" and invoke the selector for that request rather than inventing a collision-prone global key.

For `/v1/responses`, remember EggPool's public contract is stateless. A request may contain no repeated stable history. Documentation must recommend `X-EggPool-Route-Session` when callers want model stickiness across independent Responses requests.

Do not use client IP as a session key. Multiple conversations behind one host/proxy would collide and users could be pinned to unrelated choices.

Do not use API key identity as a session key for the same reason.

---

## 5. Affinity key and router fingerprint

The lookup key must include the semantic router's configuration fingerprint:

```text
(virtual_model_id, router_config_fingerprint, session_digest)
```

This makes stale decisions naturally unreachable after meaningful configuration changes.

Fingerprint changes from Plan 163 should include selector model, default, routes/descriptions/targets, sticky/TTL/input/repair settings, and selector protocol version. If an unrelated EggPool setting changes, the fingerprint remains stable and current affinity continues.

Do not manually walk the cache and rewrite entries during rehash. Let the key/fingerprint establish compatibility. A bounded cache may lazily evict stale entries later.

When a virtual router is removed, no new request can reference its generation-owned compiled entry; old affinity rows become unreachable and are eventually evicted.

---

## 6. Rehash continuity

Required staged-reload behavior:

### Unrelated configuration change

```text
router fingerprint unchanged
=> existing affinity hit remains valid
=> selector not called again
```

### Router policy change

Changing route descriptions, selector, default, target set, or other semantic fingerprint input:

```text
new router fingerprint
=> old entry cannot match
=> next request reclassifies
```

### Router removal

The public alias ceases to be virtual after the new generation is published. Existing affinity does not override the new generation's registry. If a concrete catalog model has the same ID, normal concrete resolution rules apply under Plan 163's collision contract.

### Invalid rehash candidate

Candidate generation fails before publication; current generation/fingerprint/affinity behavior remains intact.

Add integration tests around the actual staged generation lease/publication path, not only direct cache unit tests.

---

## 7. Single-flight for first selection

Concurrent requests for the same sticky `(virtual model, fingerprint, session digest)` must not all launch selector inference before the first decision is stored.

Implement a bounded per-key single-flight mechanism:

```text
request A -> miss -> becomes leader -> selector
request B -> miss -> joins same key
request C -> miss -> joins same key
leader publishes target
B/C receive same target
```

Constraints:

- no unbounded lock map;
- lock/future state is removed after completion;
- if the leader is externally cancelled, followers must not wait forever;
- if the leader resolves via configured default after selector failure, followers receive that same decision;
- one failed leader must not poison unrelated session keys;
- do not hold a global lock while waiting on model inference;
- use process-local asyncio primitives only; no DB lease/table.

Because EggPool normally executes Python coroutines on one event loop per worker, a simple keyed-future/single-flight map is sufficient. Do not build cross-process distributed locking for a single-worker/local deployment feature.

---

## 8. Which decisions are sticky

When `sticky = true` and a session identity exists, cache any successfully resolved configured route, including a resolution that used `default_model` because the selector failed.

Rationale: the default is explicitly the operator's safe model for selector failure, and keeping that session on the default preserves the same cache-locality objective. New sessions still attempt the selector normally, so a transient selector outage does not globally disable routing after recovery.

Do not cache:

- a resolution attempt interrupted by parent/client cancellation;
- an invalid target not present in the compiled route map;
- a request that never reached a semantic decision;
- downstream target success/failure state. Affinity records the chosen model, not the health of a particular account.

When `sticky = false`, skip lookup/store/single-flight entirely.

---

## 9. Interaction with target availability and failover

Affinity is a semantic preference boundary, not health state.

On an affinity hit for target A:

- continue through existing model-specific capability/context checks;
- let the existing provider/account router choose eligible accounts/providers for A;
- if A is temporarily unavailable, return the existing concrete-model error after ordinary failover;
- do **not** silently run the selector again and switch this same session to B after ambiguous target failure;
- do not delete affinity because one account failed; another account/provider serving A may recover or remain eligible.

A future operator-controlled "reselect on hard deterministic absence" policy is out of scope. Initial behavior should favor correctness/cache stability over opportunistic semantic switching.

---

## 10. Memory/resource discipline

This feature is targeted at Raspberry Pi/SBC and local deployments.

Keep the cache representation compact:

- slotted/frozen dataclasses where useful;
- digests rather than raw prompts/session IDs;
- bounded strings already present in config;
- hard entry cap;
- no background cleanup loop;
- no statistics object per session beyond the decision itself;
- no persistence serialization.

Expose aggregate counters, not per-session diagnostic history.

The cache should exist only if at least one configured router has `sticky = true`, or be a near-zero-cost process object with no entries/tasks. Prefer lazy construction if it makes runtime wiring simpler without complicating tests.

---

## 11. Observability

Plan 165 should produce decision source information for later Plan 166 metrics:

- `affinity_hit` / `affinity_miss`;
- `explicit_session` vs `automatic_session` vs `no_session` as non-sensitive categories;
- cache entries/evictions/expirations aggregate gauges/counters if existing metrics architecture supports them cheaply;
- single-flight leader/join counts if useful.

Never include session digests in normal logs unless an explicit debug-only bounded prefix is already consistent with EggPool's privacy conventions; default should avoid logging even digests.

---

## Tests

### Unit tests

- explicit session header produces stable digest without storing raw value;
- different raw session values produce different keys;
- automatic prefix fingerprint remains stable as later transcript turns are appended;
- two distinct first-user/system prefixes do not collide in deterministic test vectors;
- Responses-like request with no stable prefix returns no automatic key;
- TTL hit before expiry and miss after expiry;
- LRU eviction at hard cap;
- no background cleanup task;
- router fingerprint partitions old/new decisions;
- sticky false bypasses cache entirely;
- selector/default decisions cache; cancellation/non-decision does not;
- raw prompt/session text absent from cache objects/repr where applicable.

### Concurrency tests

- N simultaneous first requests for one key trigger one selector call;
- followers receive exactly the leader's decision;
- separate keys classify independently;
- leader internal selector failure/default still releases followers;
- leader external cancellation does not deadlock followers and cleans keyed state;
- single-flight map returns to baseline size after completion.

### Integration/rehash tests

- first virtual request selects target, second same session hits affinity;
- unrelated live config rehash retains affinity;
- route-description/target/default/selector change invalidates affinity by fingerprint;
- router removal makes old affinity unreachable;
- failed candidate rehash leaves active affinity behavior unchanged;
- explicit session header is not forwarded upstream;
- normal provider/account failover for the affinity target remains active;
- target failure does not reselect semantic model;
- no DB rows/tables contain raw affinity identity.

---

## Acceptance criteria

Plan 165 is complete when:

1. Sticky routers reuse one concrete model per stable session without re-invoking the selector.
2. Explicit and safe automatic session identities are supported without persisting/logging raw identifiers.
3. Cache size and TTL are bounded with no sweeper/background task.
4. Concurrent first requests single-flight correctly.
5. Rehash retains decisions only when the semantic router fingerprint is unchanged.
6. Affinity never pins an account or modifies quota/fairness/provider health logic.
7. Downstream target failures do not trigger semantic model spray.
8. The EggPool-only session header cannot leak upstream.
9. No DB migration, dependency, cross-process protocol, or production-style distributed cache has been added.
