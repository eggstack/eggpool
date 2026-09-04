# D005 — Health, Backoff, Circuit Breaker, and Quarantine

Status: queued behind D003 closure

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d005--health-backoff-circuit-breaker-and-model-quarantine`

Primary class: invariant/capability

## 1. Objective

Port the deterministic account/model suppression state used by routing: normalized failure categories, bounded reason-specific backoff, account/model health, one-probe circuit-breaker ownership, durable account backoff restart state, and exact-key bounded model quarantine.

D005 supplies state/effect primitives to D006 and later M7. It does not decide coordinator retryability or when an inference attempt is durably finalized.

## 2. Failure-category boundary

Port stable normalized categories and classification helpers that can operate on request-independent observations such as status code, transport category, explicit provider error class, and Retry-After value.

At minimum preserve Python's distinctions for:

- authentication failure;
- quota exhausted;
- rate limited;
- upstream 5xx;
- connect timeout;
- connection failure;
- protocol error;
- model unavailable;
- context limit;
- unknown/client-like failures.

Match the exact auth vocabulary rules so arbitrary substrings do not become terminal authentication failures. Preserve 402 quota, 408 connect timeout, 409/422 unknown absent explicit evidence, 5xx upstream, and D001 edge cases.

M7 will later map full provider/wire observations into this stable category/effect input. D005 must not infer retry policy from the category.

## 3. Backoff schedule

Port `compute_backoff_seconds` semantics with injected clock/RNG:

- authentication: terminal/indefinite until explicit reset; no exponential timer;
- quota: base 300s, exponential factor 2, cap 1,800s;
- rate limit: base 60s, factor 2, cap 1,800s;
- upstream 5xx: base 20s, factor 2, cap 1,800s;
- connect/connection/protocol: base 30s, factor 2, cap 1,800s;
- model unavailable: account/model scoped, base 300s, factor 2, cap 1,800s;
- context limit: no suppression.

Preserve current jitter policy (15% where configured). Retry-After for quota/rate limiting overrides the local exponential schedule and uses downward-only jitter so EggPool does not extend the provider's stated wait. Invalid/negative/non-finite Retry-After falls back to the bounded local schedule.

No nonterminal path may exceed 1,800 seconds after normalization/hydration.

## 4. Circuit breaker

Port the three-state circuit breaker with deterministic monotonic clock:

- CLOSED / OPEN / HALF_OPEN;
- failure threshold 5 by default;
- recovery timeout 300 seconds by default;
- one successful half-open probe closes by default;
- `can_request` is read-only and never mutates/probes;
- `allow_request` may transition OPEN->HALF_OPEN and acquires the single probe slot;
- concurrent HALF_OPEN callers are rejected while the probe is owned;
- `release_probe` clears ownership without recording success/failure;
- abandoned probe may be reclaimed after recovery timeout;
- success/failure reset/reopen counters exactly as Python.

Use a small synchronous or Tokio-aware lock whose critical sections contain no await. Readiness and candidate enumeration must use the read-only path.

## 5. Account health

Implement typed account health equivalent to the routing-visible Python state:

- healthy flag/state;
- last check/success/failure/category metadata needed by diagnostics;
- consecutive generic failures and consecutive cooldown counts;
- account disable reason/deadline;
- cooldown deadline;
- model disable deadlines;
- terminal model set;
- circuit breaker.

Rules:

- quota/rate cooldown does not increment/open the circuit breaker;
- generic transient upstream/transport/protocol failure may advance it;
- model unavailable suppresses the exact account/model rather than the entire account;
- successful traffic clears bounded transient suppression but does not override explicit operator disable, terminal authentication failure, or terminal model suppression;
- expired transient states become healthy lazily/explicitly according to Python;
- read-only health methods calculate effective expiry without mutating state.

Expose separate read-only eligibility and mutating claim APIs. D006 uses read-only enumeration then mutating circuit-probe acquisition inside the local claim transaction.

## 6. Durable account backoffs

Port typed repository operations for the existing `account_backoffs` schema (migration 0024 and later amendments if any). Preserve Python's semantic identity: account/provider/scope/reason/model where present, failure count, created/updated/expiry/terminal facts.

Required lifecycle:

- hydrate active durable backoffs on startup;
- ignore/prune expired nonterminal rows;
- terminal authentication/operator-like state remains until explicit clear where schema/policy records it;
- apply/update one bounded backoff idempotently;
- success may clear only the reasons Python permits;
- model-scoped success does not clear unrelated account/model backoffs;
- disabling/removing an account cannot make another account inherit its row.

Convert durable wall-clock expiry into a process-local remaining duration at hydration. Clamp nonterminal remaining duration to 1,800 seconds. Never store monotonic timestamps in SQLite.

Corrupt required backoff identity/reason/state must fail hydration or be explicitly quarantined as invalid state; it may not silently become healthy/eligible.

## 7. Model quarantine

Port `ModelQuarantine` with exact deterministic key over:

`provider_id + account_id + canonical_model_id + upstream_model_id-or-empty + upstream_protocol`.

Preserve states/provenance:

- healthy;
- suspected;
- quarantined;
- terminal_withdrawn;
- runtime HTTP / provider catalog / model info / manual override / operator action / migration legacy provenance.

Default bounded runtime lifecycle:

- first observation -> suspected, 120s TTL;
- second equivalent observation at current threshold -> quarantined, 300s TTL;
- expiry removes bounded suppression;
- exact-key successful request clears bounded suppression;
- authoritative catalog reappearance clears bounded/terminal state according to Python behavior;
- terminal withdrawal requires authoritative/operator/manual evidence;
- runtime-only evidence cannot directly create terminal withdrawal.

Hydration must not allow an older durable suspected/quarantined row to demote or resurrect a newer runtime-cleared/terminal state.

## 8. Quarantine persistence

Port `ModelQuarantineRepository` behavior against migrations 0051/0054 without new schema. Store/read epoch wall-clock fields exactly; convert to the in-memory state machine through validated typed parsing.

Invalid state/provenance/identity/count/timestamp must fail generation construction rather than being interpreted as healthy.

Persistence should be idempotent for repeated equivalent observations and clear operations. Exact-key isolation must be tested across providers, accounts, upstream model aliases, and protocols.

## 9. Effect application boundary

Define a small deterministic `HealthEffect`/`FailureEffect` input used to apply a normalized observation to health/backoff/quarantine state. It may contain category, scope, status, Retry-After, model identity, provenance, and whether evidence is authoritative.

Applying an effect may update D005 state and durable backoff/quarantine repositories. It must not:

- retry a request;
- select another account;
- create/finalize request/attempt rows;
- decide downstream response status;
- consume M4 network I/O.

M7 will decide *when* an attempt outcome warrants applying an effect and coordinate that with durable attempt/finalization ownership.

## 10. Concurrency and rollback

D005 state transitions must be atomic enough that D006 cannot observe impossible mixtures such as `healthy=true` with an unexpired terminal auth disable.

Circuit probe ownership participates in the D006 local claim transaction. Expose explicit probe release for local claim rollback and later coordinator paths such as client cancellation, client error, quota/rate cooldown, or model-disabled outcomes that should not count as breaker success/failure.

No async cleanup in `Drop`.

## 11. Differential tests

Use D001 fake clocks and seeded RNG to cover:

- every category/backoff sequence through several failure counts;
- 1,800-second cap;
- Retry-After lower/equal/higher/invalid/negative/zero;
- auth terminal reset;
- operator disable + later success;
- quota/rate cooldown vs circuit counters;
- generic transient circuit opening;
- OPEN->HALF_OPEN read-only vs mutating behavior;
- single concurrent probe and stale-probe recovery;
- explicit probe release;
- model unavailable exact account/model scope;
- model disable expiry and terminal marker;
- quarantine promotion/expiry/clear/reappearance/terminal withdrawal;
- stale durable quarantine hydration;
- schema-54 backoff/quarantine Python->Rust->Python compatibility;
- corrupt rows fail closed.

## 12. Security/resource requirements

Health/errors may include stable account/model/provider identifiers and reason enums but no API/proxy credentials or raw provider bodies. Bound model/backoff state by active configured accounts/catalog keys and prune stale model suppression when authoritative advertised models no longer include them.

Avoid per-request spawned timers. Store deadlines and evaluate against injected clocks; one optional maintenance pass later can prune in bulk.

## 13. Acceptance criteria

D005 closes only if:

- all nonterminal suppression is bounded to 30 minutes;
- Retry-After semantics match Python and never extend a provider-specified upper wait through jitter;
- auth/operator terminal state cannot be cleared by ordinary success;
- quota/rate cooldowns do not poison the circuit breaker;
- read-only checks cannot consume half-open probes;
- one and only one half-open probe is owned at a time;
- durable backoff/quarantine state survives restart with correct remaining-duration semantics;
- runtime quarantine cannot become terminal without authoritative evidence;
- corrupt mandatory durable state is fail-closed;
- no retry/finalization policy is embedded here.

## 14. Stop conditions

Do not close if any nonterminal backoff can exceed 1,800 seconds, a readiness call consumes a circuit probe, client cancellation has no way to release a claimed probe, model-unavailable disables an entire account, runtime suspicion becomes terminal withdrawal, or stale durable state can resurrect a cleared quarantine.