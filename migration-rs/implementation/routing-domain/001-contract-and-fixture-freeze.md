# D001 — Routing-Domain Contract and Deterministic Fixture Freeze

Status: closed; see [closure record](../../closure/routing-domain/001-status.md)

Frozen contract: [routing-domain contract](../../routing-domain-contract.md)

Repository baseline: `08597187d00660996ad14df6e5aeedce7dbd696e`

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d001--routing-domain-contract-and-deterministic-fixture-freeze`

Primary class: invariant/infrastructure

## 1. Objective

Freeze the Python M5 behavioral contract before adding Rust routing-domain behavior. Produce machine-readable deterministic observations for account identity, catalog state, quota/scoring, health/backoff/circuit/quarantine, routing/fairness/claims, and model-router compilation/affinity.

D001 must make future Rust decisions objectively comparable without live providers, inference dispatch, or timing-sensitive tests.

## 2. Scope

D001 owns only fixtures, oracle adapters, normalization rules, and contract documentation. It may add small Python test helpers and migration harness observation types. It must not add production Rust routing/catalog/health logic.

Inventory at minimum these Python surfaces:

- `accounts.registry`, `accounts.state`;
- `catalog.cache`, routing-relevant normalization/protocol/capability/limit logic, catalog freshness/persistence semantics;
- `quota.estimation`, `quota.scorer`, canonical SQLite reservation/usage paths;
- `health.backoff`, `health.circuit_breaker`, `health.health_manager`;
- `failure.quarantine` and its durable repository representation;
- `routing.eligibility`, `routing.fairness`, `routing.router`;
- `model_router.registry`, `model_router.affinity`;
- existing SQLite tables/migrations used by those surfaces.

Explicitly mark the deprecated in-memory `quota.reservation.ReservationManager` as non-contract production behavior.

## 3. Stable observation schemas

Add structured observations rather than comparing repr/error text. Required observation families:

### Account observation

Record only non-secret fields: account name/id, provider ID, enabled, usable-credential boolean, weight, priority, supported protocols/request surfaces, configured quota offsets, and stable validation outcome.

Never record API-key values or proxy URI credentials.

### Catalog observation

Capture global model IDs, provider/model rows, account support sets, account/provider mapping, protocol and protocol source, capability/limit fields relevant to eligibility, freshness timestamps normalized to relative age/status, refresh outcome, and support add/preserve/withdraw decisions.

### Quota observation

Capture persisted request/token/cost windows, capacities/offsets/weight, pending/reserved request/token/cost mirrors, bounded EWMA/cache counts, per-window utilization, score components, eligibility flag, and stable rank.

### Health observation

Capture normalized failure category, health state, consecutive failure/cooldown counts, bounded remaining cooldown/disable duration, circuit state/failure/success counts/probe availability, disabled models/terminal markers, and quarantine state/count/remaining TTL/provenance.

Do not compare raw monotonic timestamps.

### Routing observation

Capture requested model/provider/protocol/surface facts, eligible candidate names, stable exclusion codes, tier, quota score components, native/transcode flag, fairness key/mode/index/band, ordered ranking, selected account, and local claim ownership deltas.

### Model-router observation

Capture virtual ID, ordered route IDs/labels/models/descriptions, selector/default model IDs, compiled policy bytes as base64 or digest plus exact length, config fingerprint, sticky/TTL/limits, affinity key digest, cache outcome, selected concrete model, and bounded cache statistics.

## 4. Deterministic time/randomness policy

Build reusable fake clocks for wall-clock and monotonic domains. Contract tests must be able to advance them independently so restart hydration and duration semantics can be verified.

Backoff comparisons must disable jitter or use an explicitly seeded RNG. Fairness random mode must use a seeded/injected random source. Round-robin must not depend on map insertion accident.

Normalize time as one of:

- exact configured duration;
- remaining duration at a controlled fake `now`;
- expired/not-expired boolean;
- deterministic epoch only when SQLite persistence itself is under test.

Never normalize away a difference that changes eligibility, suppression length, fairness choice, catalog freshness, or claim ownership.

## 5. Required fixture matrix

Create representative small fixtures rather than a combinatorial suite.

### Accounts/config

Include direct and proxied accounts across at least two providers, enabled/disabled accounts, missing optional credentials, invalid enabled required-auth credentials, different weights/priorities, and multiple provider protocols.

### Catalog

Include static-only provider, authoritative non-empty refresh, partial/invalid rows, empty response, failed response, stale durable refresh state, fresh state, support shared by sibling accounts, provider-specific protocol differences, capability overrides, model withdrawal, and model reappearance.

### Quota

Include empty/default capacities, explicit request/token capacities, weights, positive/negative offsets where permitted, exact-capacity boundary, above-capacity score-only mode, hard-cap mode, pending claim, reserved claim, pending->reserved conversion, ownership underflow, and non-finite/malformed persisted state.

### Health

Cover every `FailureCategory`, 402/408/409/422/429/5xx edge classifications, exact auth vocabulary, context-limit no-suppression, quota/rate cooldowns that do not advance the breaker, generic transient failures that do, model-unavailable scope, Retry-After zero/finite/too-large/invalid values, 1,800-second cap, operator disable, auth terminal reset, and success that must not undo operator/auth state.

### Circuit/quarantine

Cover closed->open->half-open->closed, one half-open probe, explicit release, stale probe recovery, quarantine suspected->quarantined, expiry, exact-key success clear, catalog reappearance, terminal withdrawal, and stale durable hydration that must not demote newer runtime state.

### Routing

Cover disabled/no-provider/no-model/stale/no-protocol/protocol-mismatch/no-surface/thinking/quarantine/health exclusions; strict priority tiers; score ordering; native-vs-transcode; fairness off/round-robin/random; fairness key scopes; epsilon-band boundary; 4,096-key eviction; missing-account refresh throttle; and local claim/rollback.

### Model-router

Include disabled/empty registry, deterministic route sorting/fingerprinting, description normalization, explicit session header valid/invalid/oversized/control characters, TTL hit/miss/expiry, LRU eviction, concurrent single-flight, config fingerprint invalidation, and automatic identity framing from bounded role/text inputs without retaining raw content.

## 6. SQLite snapshots

Seed a Python-created database at schema 54 with representative:

- accounts;
- model/provider metadata and account support/freshness rows;
- usage windows/reservations needed for routing snapshots;
- account backoffs;
- model quarantine rows.

The fixture must be copyable so Python and Rust can run sequentially against identical initial state. Never point differential tests at the live production database.

Record which durable fields are exact parity and which convert into monotonic remaining-duration state after hydration.

## 7. Contract classifications

For every observation field classify parity as:

- **exact** — identity, stable reason code, candidate membership/order, persisted semantic field, fingerprint/policy bytes;
- **semantic** — internal container choice, lock type, generated object identity, equivalent float representation within an explicitly documented tolerance;
- **deferred** — only when ownership belongs to M6/M7/M8 and the deferred boundary is named.

Do not use `deferred` for behavior that M5's long-term roadmap requires at closure.

## 8. M5/M6/M7 boundary freeze

Document the request-independent DTOs later plans may consume. At minimum freeze concepts equivalent to:

- `RoutingRequestFacts`: canonical model ID, optional provider constraint, client/request surface, requested protocol, protocols acceptable through later transcoding, thinking/capability requirements, projected token count, and optional model-router session identity inputs;
- `SelectionClaim`: selected account/provider/model/protocol/tier plus local ownership token and score/fairness diagnostics;
- `AffinityIdentityInput`: bounded already-normalized role/text prefix data or explicit session header.

D001 does not implement these in production Rust; it freezes their observable fields so D006/D007 can avoid depending on M6's future `CanonicalRequest` type.

## 9. Verification

Required gates:

- existing migration harness tests remain green;
- new Python oracle fixtures pass with fixed clocks/RNG;
- snapshot serialization is stable across repeated runs;
- secret-marker scan proves fixtures/observations contain no API/proxy credentials;
- schema-54 fixture opens under current Python and Rust DB layers;
- `git diff --check`.

No Cargo dependency should be added for D001 unless a migration-test-only serialization helper is clearly justified.

## 10. Acceptance criteria

D001 closes only if:

- all M5-owned Python surfaces are inventoried;
- structured observations exist for accounts, catalog, quota, health, routing, and model-router state;
- fake clock and deterministic RNG policies are reusable;
- schema-54 state fixtures cover backoff/quarantine/catalog/usage data;
- stable exclusion/failure/fairness reason vocabularies are frozen;
- local claim ownership fields and M5/M7 handoff are explicit;
- semantic model-router selection is explicitly deferred to M7, not accidentally omitted;
- no production behavior is changed merely to make the oracle easier to compare.

## 11. Stop conditions

Do not close if a future implementation agent would still have to infer a routing rule from Python source without a fixture, if timestamps/randomness can make the same fixture produce different decisions, if any snapshot contains credentials, or if the contract ambiguously assigns request persistence/retry/model-selector dispatch to M5.
