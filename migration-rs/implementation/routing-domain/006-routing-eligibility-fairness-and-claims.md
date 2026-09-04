# D006 — Routing Eligibility, Fairness, and Local Selection Claims

Status: dependency-ready; D005 closed

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d006--eligibility-priority-tiers-fairness-and-local-selection-claims`

Primary class: capability/invariant

## 1. Objective

Port the deterministic router that combines D002/D003 catalog facts, D004 load scores, and D005 health state into stable candidates, exclusions, priority tiers, fairness decisions, and one locally owned selection claim.

D006 is the M5 concurrency boundary. It must close the stale-selection window by publishing provisional local ownership before another selector can score the same state, while deliberately stopping before durable inference request/reservation/attempt persistence.

## 2. Request-independent routing input

Define a typed `RoutingRequestFacts` (name may vary) that does not depend on M6's future canonical IR. Freeze fields from D001 such as:

- canonical model ID, including provider-qualified input before parsing;
- optional explicit provider constraint;
- requested upstream protocol when already known;
- client/request surface;
- set/order of provider protocols that later transcoding could support;
- thinking/reasoning capability requirement and configured unknown/conflict policy;
- projected token count and optional reservation cost estimate input;
- any stable model-router-resolved concrete model ID supplied by D007/M7 later.

D006 does not parse JSON, messages, tools, media, or reasoning payloads. M6 will construct these facts from a canonical request.

## 3. Stable eligibility reasons

Port Python eligibility with stable reason codes suitable for differential snapshots and later routing traces. Cover at least:

- disabled;
- auth_failed / unusable credentials;
- quota_exhausted when authoritative or hard-cap policy applies;
- cooldown / rate_limited;
- circuit_open / half-open probe unavailable at claim time;
- no_provider / wrong_provider;
- no_model / model_stale;
- no_protocol / protocol_mismatch;
- no_surface;
- model_quarantined / terminal model unavailable as appropriate;
- thinking_unsupported / thinking_unknown / thinking_conflicting and current capability-policy variants;
- malformed/non-finite score state.

Keep diagnostics deterministic. Do not expose secret configuration or raw upstream error text.

## 4. Eligibility ordering and authority

Preserve Python's policy authority:

1. configured/operator account enablement and usable credentials;
2. provider constraint/ownership;
3. request surface/protocol feasibility;
4. catalog support and freshness;
5. model protocol/capability/limit policy facts relevant before request-size parsing;
6. D005 health/cooldown/model/quarantine read-only state;
7. optional local quota hard-cap mode;
8. scoreability.

Default local quota remains score-only. A high local utilization score is not equivalent to authoritative provider quota exhaustion.

Read-only eligibility must use D005 `can_request`/equivalent and cannot acquire a half-open circuit probe.

## 5. Provider/model protocol and native preference

For each candidate determine whether the selected provider/account can serve the requested surface/protocol natively or requires a later M6 transcoder path. Set `requires_transcode` as a routing fact only; D006 must not transcode.

Honor `prefer_native` exactly as Python: score remains primary, and native-vs-transcode affects ordering/tie behavior only where the frozen scorer/router policy says it does. Do not allow native preference to leapfrog a strict higher routing-priority tier.

Provider-qualified model IDs must constrain provider selection using D002's parser rather than creating a separate naming rule.

## 6. Priority tiers

Group eligible candidates by configured `routing_priority` descending. Lower-priority tiers are fallback tiers and never compete in the fairness band while any candidate in a higher tier remains locally claimable.

Within a tier, use D004 deterministic score ranking. Preserve the tier on `RoutingScore`/decision trace.

Do not implement coordinator retry/failover loops. D006 may return the fully ranked plan/tier structure so M7 can ask for a later candidate after an attempt fails, but D006 itself selects only according to an explicit claim operation and exclusion set supplied by its caller.

## 7. Fairness band

Port `FairnessKey`, `FairnessRotor`, and router fairness policy.

Requirements:

- modes: off, round_robin, random where currently supported;
- scopes matching Python (`provider_model_protocol`, `provider_model`, `priority_model_protocol`, including client protocol only where the frozen key includes it);
- candidates sorted by account name before rotation so map insertion order is irrelevant;
- fairness applies only to candidates in the best-score epsilon band;
- fairness band must not mix different `requires_transcode` values when Python separates them;
- strict priority tier boundary is never crossed;
- default epsilon follows the scorer near-tie range unless explicitly configured;
- round-robin position map is LRU/ordered and hard-capped at 4,096 keys;
- restart resets in-memory positions exactly as Python; do not invent persistence;
- random mode uses injectable RNG in tests.

Return structured fairness diagnostics: mode, applied, key/scope, candidate count, anchor/best score, chosen index/account, and stable reason when not applied.

## 8. Routing plan

Implement a pure/read-only `build_routing_plan` equivalent that produces:

- eligible account names;
- ordered `RoutingScore` candidates;
- provider/model/protocol/tier facts;
- stable exclusion list;
- fairness decision/ordered band;
- sufficient catalog/health version markers for local revalidation if needed.

Building a plan must not increment active requests, publish pending quota load, consume circuit probes, persist routing decisions, or mutate fairness unless the Python fairness API specifically treats plan construction as selection. Prefer separating pure plan construction from selection mutation so diagnostics/readiness cannot alter future choices.

If preserving Python's exact fairness mutation requires the rotor to advance on selection, make that explicit in `select_and_claim`, not generic read-only planning.

## 9. Local selection-claim transaction

Implement a narrow `select_and_claim`/`claim_candidate` operation serialized by one Tokio mutex or equivalent. The critical section must contain no SQLite or network await.

Inside the local claim transaction:

1. build or revalidate the current in-memory candidate state;
2. select the highest priority/score/fairness candidate not excluded by caller;
3. attempt D005 mutating circuit-probe acquisition if the circuit requires it;
4. if probe acquisition fails due to a concurrent probe, exclude/reselect within the current plan/tier as Python does;
5. increment account active-request ownership;
6. publish one D004 pending claim for projected request/tokens/cost;
7. advance fairness state exactly once for the accepted selection;
8. return a `SelectionClaim` token containing non-secret identity and diagnostics.

The pending load and active count must be visible before the mutex is released. This closes the herd window where concurrent requests score the same pre-claim load.

## 10. Claim ownership API

`SelectionClaim` should identify at minimum:

- unique local claim ID/token;
- account durable ID/name;
- provider ID;
- canonical model ID/provider model identity where available;
- chosen protocol and `requires_transcode`;
- priority tier;
- projected tokens/cost mirrored into D004;
- whether a circuit half-open probe is owned;
- score/fairness snapshot references needed for tracing.

Expose explicit state transitions:

- `rollback_claim`: remove pending load, decrement active ownership, release circuit probe if owned;
- `convert_claim_after_durable_publication`: convert pending quota load into reserved mirrors while retaining active/probe ownership for M7;
- `release_active_claim`: final local release called by M7 when the request/attempt lifecycle no longer owns the account/probe.

Transition methods must be idempotent where Python coordinator compensation requires idempotency, or must return a typed already-transitioned result. Underflow/double-release cannot silently mutate another claim's counters.

No async work in `Drop`; M7's retained finalization/compensation owns guaranteed explicit cleanup.

## 11. Active request counts

Port account active-request increment/decrement with invariant diagnostics. A double decrement must not make the counter negative. Preserve Python's externally visible clamp/diagnostic semantics while using claim IDs internally to prevent one request from releasing another's ownership.

D004 scorer reads active counts as the inflight penalty signal.

## 12. Missing-account catalog recovery

Port the bounded missing-support recovery hook from Python router:

- only when model exists globally and an otherwise plausible enabled sibling account is missing support;
- call D003 one-account refresh through an injected callback;
- rate limit per account using monotonic time;
- bound the recovery-attempt map and prune/evict stale keys;
- ordinary routing does not synchronously spam model endpoints;
- failed recovery remains a normal no-support result and does not crash selection.

Do not create a background refresh loop; M8 owns periodic work.

## 13. Readiness/pairing checks

Implement read-only `has_eligible_pairing`/equivalent used by readiness. It may inspect account/catalog/protocol/health/quarantine state but must not:

- acquire circuit probes;
- advance fairness;
- increment active counts;
- publish quota claims;
- trigger live catalog refresh;
- write SQLite.

## 14. Routing trace boundary

Produce a typed decision trace suitable for the existing `routing_decisions` schema and dashboard later:

- requested model/provider/protocol/surface;
- candidates and stable exclusions;
- score components;
- tier/native-transcode facts;
- fairness metadata;
- selected account/provider;
- local claim ID.

D006 does not persist this trace as part of selection because M7 must coordinate durable routing/request/attempt persistence atomically. Provide deterministic serialization/value types for M7.

## 15. Concurrency/failure tests

Required tests include:

- two simultaneous claims cannot both select from the same unclaimed load snapshot when a peer account should become preferable after the first claim;
- claim mutex cancellation before commit publishes no partial ownership;
- accepted claim exposes pending load immediately;
- circuit HALF_OPEN permits only one claim;
- failed half-open acquisition reselects or reports no candidate without leaking fairness/pending state;
- rollback returns active/pending/probe state to baseline;
- duplicate rollback is typed/idempotent and cannot underflow;
- conversion replaces pending with reserved load exactly once;
- 4,096 fairness-key cap under adversarial model/provider keys;
- readiness causes zero mutation;
- missing-account recovery throttle remains bounded.

## 16. Differential matrix

Use D001 fixtures for all stable exclusions plus:

- provider-qualified models;
- strict priority tiers;
- score ordering and score ties;
- native/transcode preference;
- fairness modes/scopes/epsilon boundary;
- restart fairness reset;
- quota hard-cap vs score-only;
- stale/fresh catalog support;
- circuit/quarantine interactions;
- thinking capability policies;
- concurrent local claims and rollbacks.

Normalize only incidental float representation within D001 tolerance; candidate membership, exclusion code, tier, fairness choice, and selected account are not normalizable differences.

## 17. Acceptance criteria

D006 closes only if:

- candidate/exclusion/rank snapshots match Python;
- strict priority tiers are preserved;
- fairness is bounded and cannot cross tier/material-score/native bands;
- read-only routing/readiness has no state side effects;
- one local claim transaction atomically publishes active + pending + probe ownership before another selector runs;
- claim rollback/conversion ownership is explicit and underflow-safe;
- no SQLite/network work occurs under the claim lock;
- no inference retry/failover or routing-decision persistence is implemented here.

## 18. Stop conditions

Do not close if concurrent selectors can herd on stale load, fairness state grows unbounded, readiness consumes a probe/rotor position, claim rollback can underflow silently, lower priority competes with a healthy higher tier, or D006 starts persisting/submitting inference attempts.
