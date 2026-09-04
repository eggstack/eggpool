# M5 Routing Domain and Catalog State Roadmap

Status: active; D001-D003 closed; D004 ready for handoff

Repository baseline: `08597187d00660996ad14df6e5aeedce7dbd696e`

Canonical source:

- `../000-long-term-specification.md`
- `../001-terminology-and-domain-model.md`
- `../002-long-term-roadmap.md#M5--catalog-account-registry-routing-quota-health-and-model-router-state`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

## 1. Purpose and ownership

M5 ports the deterministic state and policy layer that decides which concrete provider/account/model pair may be used before inference dispatch exists in Rust. It sits above the closed M4 provider transport and below M6 request codecs and M7 coordinator/finalization.

M5 owns:

- immutable account identity and credential-availability state;
- provider/model catalog identity, per-account support, freshness, protocol, capability, and limit metadata;
- routing-essential catalog refresh through the M4 provider clients;
- SQLite hydration/persistence for catalog freshness, model support, usage windows, backoffs, and model quarantine using the existing schema;
- quota estimates, bounded EWMA learned state, reservation mirrors, provisional selection claims, and fair-share scoring;
- account/model health, reason-specific bounded backoff, circuit-breaker probe ownership, and model quarantine;
- eligibility, stable exclusion reasons, priority tiers, native-vs-transcode preference, fairness bands, and deterministic candidate ranking;
- compilation of virtual model-router configuration and bounded session affinity state;
- differential fixtures that prove state snapshots, candidate sets, selections, exclusions, and local durable effects match Python.

M5 does **not** own:

- parsing public inference request bodies into the canonical IR;
- OpenAI/Anthropic/Gemini inference request/response codecs or SSE;
- semantic model-router selector calls, because Python's selector invokes `RequestCoordinator`;
- upstream inference authentication/header/body construction beyond the catalog models endpoint;
- retry/failover policy, alternate-wire negotiation, downstream handoff, or terminal finalization;
- creation/finalization of durable inference attempts;
- runtime-generation publication, rehash, periodic background scheduling, or generic outbound HTTP lifecycle;
- daemon/service lifecycle or release packaging.

## 2. Current Python ownership map

### Accounts

`accounts.registry.AccountRegistry` materializes immutable non-secret account identity, provider ownership, credential availability, routing priority, weight, protocol/request-surface support, and quota offsets. Secret API keys are stored separately and never appear in identity snapshots.

`accounts.state.AccountRuntimeState` tracks enabled/weight/priority, health/cooldown state, active requests, reserved cost mirrors, and per-model availability. Nonterminal suppression is capped at 1,800 seconds.

### Catalog

`catalog.cache.ModelCatalogCache` maintains global model identity, provider-specific model metadata, model-to-account support sets, account-to-provider ownership, per-account freshness, provider/model keys, capability overrides, and conservative protocol/limit metadata.

Catalog updates are deliberately non-destructive under uncertainty. Failed, skipped, partial, or empty refreshes preserve prior support. Withdrawal occurs only when the result is authoritative and the configured withdrawal policy permits it.

`catalog.service.CatalogService` hydrates durable state, seeds static models, fetches per-account model lists, normalizes protocol/capability/limit metadata, persists semantic changes, and supports one-account refresh for stale/missing support recovery. Routing-essential provider catalog requests use the provider account client pool.

Optional external pricing/model-info enrichment is advisory. M5 ports deterministic parsing, matching, trust gates, and persisted identity needed by routing/dashboard parity. Periodic external polling and the generic outbound-client lifecycle remain M8 concerns.

### Quota and claims

`quota.estimation.QuotaEstimator` owns bounded learned cost estimates, persisted 5h/7d/30d usage snapshots, operator offsets, request/token capacities, reservation mirrors, and provisional pending claims.

Routing load is based on **request count and token count**, not cost. Cost remains an audit/reservation-sizing signal. The canonical in-memory `ReservationManager` is deprecated and must not be ported as production architecture; SQLite `ReservationRepository` plus QuotaEstimator mirrors are authoritative.

A pending claim becomes visible before durable dispatch persistence so concurrent selection cannot choose from a stale load snapshot. The coordinator currently serializes selection/claim publication. Rust must retain that local ownership invariant without doing request persistence in M5.

### Health and quarantine

`health.backoff` uses reason-specific bounded schedules with a 30-minute nonterminal cap. Provider Retry-After for rate/quota suppression takes precedence and receives downward-only jitter so EggPool never extends the provider's explicit upper bound.

`health.health_manager` separates account health, account/model disable state, cooldowns, terminal authentication failure, and circuit-breaker state. Read-only eligibility checks never consume a half-open probe. Actual dispatch claim acquisition does consume the single half-open probe and must release it on non-health terminal paths.

`failure.quarantine.ModelQuarantine` is a bounded model-specific state machine keyed by provider/account/canonical-model/upstream-model/protocol. Runtime evidence progresses healthy -> suspected -> quarantined; expiry or success can recover; terminal withdrawal requires authoritative/operator evidence.

### Routing

`routing.router.Router` combines eligibility, quota scoring, strict priority tiers, native-protocol preference, bounded fairness, and stable diagnostics. Fairness rotation is bounded to 4,096 keys and applies only within the best-score band, not across priority tiers or materially different scores.

Eligibility is authoritative for operator disable, provider/model/catalog/protocol/capability/health/quarantine constraints. Local quota estimates are advisory by default (`score_only`) and become a hard exclusion only when explicitly configured as `hard_cap`.

### Model routers

`model_router.registry` compiles immutable virtual router configuration into deterministic route IDs, a length-delimited SHA-256 fingerprint, and a bounded static selector policy.

`model_router.affinity` stores only derived decisions keyed by virtual model, router fingerprint, and a SHA-256 session digest. The cache is TTL/LRU bounded to 4,096 entries and single-flights concurrent misses.

`model_router.selector`, however, performs an internal request through `RequestCoordinator`. That network/dispatch behavior is therefore deferred to M7. M5 supplies the compiled policy/affinity boundary that M7 will call.

## 3. Rust target architecture

Keep one Cargo package. Add modules under `rust/src/` rather than internal crates:

```text
accounts/
  identity.rs
  registry.rs
catalog/
  cache.rs
  normalize.rs
  capabilities.rs
  limits.rs
  refresh.rs
quota/
  state.rs
  estimator.rs
  scorer.rs
health/
  backoff.rs
  circuit.rs
  manager.rs
failure/
  quarantine.rs
routing/
  eligibility.rs
  fairness.rs
  router.rs
  claim.rs
model_router/
  registry.rs
  affinity.rs
```

Names may be adjusted to fit the existing module tree, but ownership boundaries should remain clear. Do not recreate the Python file graph mechanically and do not create multiple Cargo packages.

Long-lived routing reads should operate on in-memory state after hydration. SQLite is the durable source for restart recovery and accounting, not a per-candidate hot-path query engine.

## 4. Core invariants

- Python remains production until cutover.
- M4 transport is consumed as a stable dependency and is not redesigned in M5.
- no account secret is stored in routing/catalog snapshots, logs, errors, fairness keys, affinity keys, or fixture observations;
- account name/provider ownership is deterministic and unknown/malformed account state fails closed;
- catalog failure/partial/empty uncertainty does not silently remove previously valid support;
- authoritative withdrawal and model reappearance semantics remain explicit;
- catalog freshness uses durable per-account refresh evidence and never treats an unrelated catalog write as a successful refresh for another account;
- request/token utilization drives routing; cost does not become a hidden routing signal;
- local quota is score-only by default and hard-gates only when configured;
- pending selection load is published before the selection critical section is released;
- claim rollback detects ownership underflow instead of silently clamping inconsistent ownership;
- nonterminal backoff never exceeds 1,800 seconds;
- authentication failure is terminal until explicit reset;
- model-unavailable suppression remains account/model scoped rather than account-wide;
- read-only health/readiness/candidate enumeration cannot consume a half-open circuit probe;
- at most one half-open probe is owned per circuit at a time;
- fairness does not cross strict priority tiers or the configured near-score/native-protocol band;
- fairness, EWMA, affinity, recovery-attempt, and other learned maps are explicitly bounded;
- model quarantine is exact-key and bounded; runtime-only model-like failures cannot create terminal withdrawal without authoritative evidence;
- model-router affinity stores hashes/derived decisions, never raw session content;
- semantic model-router selector execution is deferred to M7;
- no inference retry, attempt persistence, or terminal finalization is implemented by M5.

## 5. Concurrency and ownership model

Python currently gains much of its determinism from one canonical asyncio loop plus narrow locks. Rust may remain current-thread during this milestone, but M5 state should be correct if M8 later moves work across Tokio threads.

Use narrow synchronization:

- immutable/config/catalog snapshots where practical;
- `Mutex` only around mutations that must be atomic relative to selection;
- read-only snapshots for diagnostics and readiness;
- one local selection-claim critical section for candidate selection plus provisional ownership publication;
- no SQLite await while holding the selection-claim lock;
- no lock held across M4 network I/O.

The M5 claim transaction consists only of local state: select/revalidate candidate, consume a circuit probe if needed, increment active ownership, and publish pending request/token/cost load. M7 later converts that claim into durable request/reservation/attempt state. M5 exposes explicit publish/rollback/release operations so M7 can prove cleanup; it does not rely on async work from `Drop`.

## 6. Database policy

Reuse the existing 54 migrations and tables. M5 extends the Rust repository layer to the already-existing account backoff, routing/catalog/model metadata, usage/reservation, catalog refresh, and model quarantine tables where required.

Do not add a migration merely because the Rust repository layer is incomplete. A new migration requires evidence that the Python schema itself cannot represent required parity behavior.

Hydration rules must preserve Python's wall-clock/duration distinction. Durable expiry timestamps are restart hints; convert remaining duration into the process monotonic domain and clamp nonterminal residual suppression to the 1,800-second policy cap. Corrupt mandatory durable identity/state fails generation construction rather than silently becoming eligible.

## 7. Network and catalog policy

Routing-essential provider model discovery may use M4's `ProviderClientPool`/`ProviderHttpClient`. D003 owns only the models-endpoint contract: provider auth/static headers needed for discovery, method/path/query/body, finite JSON response parsing, static seeds, and per-account isolation.

M5 must not introduce Reqwest, a second TLS stack, or another connection pool for optional catalogs. Deterministic external pricing/model-info resolvers may be ported behind an injected response/lookup interface and qualified from fixtures/database snapshots. M8 may later attach periodic generic outbound polling using the eventual process-level outbound manager.

## 8. Dependency graph

```text
F001-F006 closed
M4 T001-T006 closed
       |
       v
D001 contract + deterministic fixture freeze
       |
       v
D002 account registry + catalog cache/hydration
       |
       v
D003 catalog refresh + normalization + persistence
       |
       +-------------------+
       |                   |
       v                   v
D004 quota/claims/scoring  D005 health/backoff/quarantine
       |                   |
       +---------+---------+
                 v
D006 eligibility/routing/fairness/local claims
                 |
                 v
D007 model-router compilation + bounded affinity
                 |
                 v
D008 differential qualification + M5 closure
                 |
                 v
M6 implementation planning may become dependency-ready
```

D004 and D005 may be implemented in either order once D003 is closed, but only one plan should be marked dependency-ready at a time unless the registry explicitly approves parallel handoff. D006 requires both.

## 9. Milestones

### D001 — Routing-domain contract and deterministic fixture freeze

Freeze exact/semantic parity classes, stable observations, fake-clock/randomness policy, representative account/catalog/quota/health/routing/model-router state fixtures, and concurrency cases before Rust behavior is added.

Exit: an implementation agent can compare Python and Rust domain decisions without live providers or inference dispatch.

### D002 — Account registry and catalog cache/hydration

Port immutable account identity/credential availability and the routing-facing model catalog cache, provider/account support maps, freshness, protocol/capability/limit metadata, provider-suffix parsing, static override semantics, and SQLite hydration.

Exit: the same config/database snapshot produces parity-equivalent non-secret account and catalog snapshots.

### D003 — Catalog refresh, normalization, and persistence

Port provider model-list request construction on M4, static model seeding, response validation, normalization, protocol/capability/limit resolution, non-destructive refresh outcomes, authoritative withdrawal, semantic persistence, pings/freshness, and one-account recovery hooks.

Exit: deterministic provider catalog fixtures produce the same live/new/withdrawn/support/freshness outcomes and persisted rows.

### D004 — Quota, reservation mirrors, claims, and fair-share scoring

Port persisted usage snapshots, bounded EWMA/reservation estimates, pending/reserved ownership mirrors, claim conversion/underflow checks, local-cap modes, request/token utilization scoring, weights, offsets, inflight penalties, native preference inputs, and deterministic ranking.

Exit: quota snapshots and ranking inputs match Python, including concurrent provisional claims.

### D005 — Health, backoff, circuit breaker, and model quarantine

Port normalized health categories, 30-minute bounded reason policies, Retry-After semantics, circuit state/probe ownership, account/model disable state, durable backoff hydration/persistence, exact-key quarantine lifecycle, and quarantine persistence/recovery.

Exit: fake-clock state-machine traces and restart snapshots match Python and cannot become fail-open under corrupt/expired state.

### D006 — Eligibility, priority tiers, fairness, and local selection claims

Port the router's stable exclusion codes, provider/model/surface/protocol/capability/quarantine/health gates, strict priority tiers, score integration, native preference, 4,096-key bounded fairness, missing-account refresh throttling, readiness checks, and the local selection-claim transaction.

Exit: the same state/request-facts fixture yields parity-equivalent candidates, ranking, fairness decision, selected claim, and rollback ownership under concurrency.

### D007 — Model-router compiled registry and affinity state

Port deterministic virtual-router compilation/fingerprinting/static policy, exact virtual lookup, explicit/automatic session identity framing, TTL/LRU affinity, keyed single-flight, and aggregate stats without retaining raw content.

Actual selector calls through the coordinator remain deferred to M7.

Exit: compiled router bytes/fingerprints and affinity cache traces match Python fixtures without importing M6/M7 behavior.

### D008 — M5 differential qualification and closure

Run integrated account/catalog/quota/health/router/model-router state scenarios, concurrency/claim tests, restart hydration, corrupt-state fail-closed tests, bounded-memory checks, dependency review, and SBC-oriented local characterization.

Exit: no unresolved high/medium M5 correctness gap remains and M6 may rely on stable routing-domain interfaces.

## 10. Verification strategy

Prefer deterministic Python/Rust observations over live providers. Build fixture matrices around:

- multi-provider and multi-account identity;
- missing/disabled/misconfigured credentials;
- static, authoritative, partial, empty, failed, stale, and reappearing catalogs;
- provider-specific protocols/capabilities/limits;
- usage windows, offsets, weights, reservations, and pending claims;
- all backoff categories and boundary values;
- circuit open/half-open/single-probe/release traces;
- quarantine promotion/expiry/success/reappearance/terminal evidence;
- exact stable routing exclusions;
- priority tiers, transcode/native preference, fairness rotation/random mode, and bounded-key eviction;
- concurrent claim selection with no herd/stale-load window;
- virtual-router compilation, affinity TTL/LRU/single-flight, and no raw session retention;
- Python-created DB -> Rust hydrate -> Rust write -> Python read where the schema window permits.

No live inference provider, broad CI matrix, load farm, or paid external catalog is required for M5 closure.

## 11. Resource posture

This milestone is especially important for SBC deployments. The Rust design should improve resource behavior without changing policy:

- keep selection DB-free after snapshot hydration;
- avoid per-candidate allocation where a reusable vector/map snapshot suffices;
- keep fairness/affinity/EWMA/recovery maps bounded at the same or tighter safe limits;
- avoid polling/background task proliferation in M5;
- avoid duplicate catalog metadata copies when immutable sharing is practical;
- characterize selection and refresh memory/CPU locally, but do not invent unsupported hard performance gates.

## 12. Non-goals

- no public inference endpoint dispatch;
- no canonical request/SSE implementation;
- no semantic selector LLM call;
- no coordinator retry/failover;
- no durable request/attempt finalization lifecycle;
- no generic outbound HTTP manager;
- no periodic catalog/model-info scheduler;
- no rehash/generation publication;
- no daemon/service lifecycle;
- no new database schema unless parity proves the current schema insufficient;
- no ORM, actor framework, DI framework, or second HTTP/TLS stack.

## 13. Closure condition

M5 closes only after D001-D008 have accepted closure evidence and an integrated deterministic snapshot can prove parity-equivalent account/catalog identity, candidate eligibility, priority/fairness ranking, local claim ownership, quota pressure, health/backoff/quarantine state, and bounded virtual-router affinity.

M5 closure does not mean a client inference request can be dispatched. It means M6 can supply canonical request facts and M7 can consume the selected claim/transport without having to redesign the routing-domain state machine.
