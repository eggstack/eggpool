# D001 Closure — Routing-Domain Contract and Deterministic Fixture Freeze

Status: closed

Recommendation: closed; D002 is dependency-ready. D003-D008 remain queued
behind their named predecessor closures.

Implementation commit: [`40be1bf`](https://github.com/eggstack/eggpool/commit/40be1bf)

Plan: [D001 — contract and fixture freeze](../../implementation/routing-domain/001-contract-and-fixture-freeze.md)

Contract: [M5 routing-domain contract](../../routing-domain-contract.md)

Repository baseline inspected: `08597187d00660996ad14df6e5aeedce7dbd696e`

## Outcome

D001 freezes the Python M5 state/policy boundary before Rust routing behavior
is added. It adds JSON-safe observation types for accounts, catalog, quota,
health/quarantine, routing/fairness/claims, and compiled model-router/affinity
state; reusable independent fake clocks and seeded RNG helpers; a representative
fixture matrix; a stable Python snapshot; and a copyable schema-54 durable seed.

No production Python routing/catalog/health/quota/model-router behavior and no
production Rust behavior changed. The Rust change is test-only schema-54
compatibility coverage proving the same durable seed opens through the existing
Rust database layer.

## Requirement-to-evidence matrix

| D001 requirement | Evidence | Result |
|---|---|---|
| M5-owned Python surfaces inventoried | [`routing-domain-contract.md`](../../routing-domain-contract.md) maps `accounts.registry/state`, catalog cache/normalization/protocol/capability/limit/service, quota estimation/scoring and SQLite repositories, health/backoff/circuit/manager, quarantine/repository, routing eligibility/fairness/router, and model-router config/registry/affinity | Pass |
| Structured account observations | `AccountObservation` records non-secret ID/name/provider, enabled and credential availability, weight/priority, protocol/surface support, quota offsets, and validation outcome | Pass |
| Structured catalog observations | `CatalogObservation` records global/provider rows, account support/provider maps, resolved protocol/source, capabilities/limits, relative freshness, outcomes, and preserve/withdraw decisions | Pass |
| Structured quota observations | `QuotaObservation` records persisted windows, capacities, offsets/weights, pending/reserved load, remaining capacity, score components, request/token counts, eligibility, and rank inputs | Pass |
| Structured health observations | `HealthObservation` records category classification, policy delay, account state, read-only health, model suppression, circuit probe state, and quarantine lifecycle | Pass |
| Structured routing observations | `RoutingObservation` records request facts, candidates/exclusions, tier, score/native-transcode facts, fairness key/mode/band, ranking, selection, and local claim ownership deltas | Pass |
| Structured model-router observations | `ModelRouterObservation` records sorted route IDs/labels/models/descriptions, selector/default models, exact policy bytes/digest/length, fingerprint, sticky limits, hashed identity, cache outcome, selected model, and stats | Pass |
| Deterministic clocks and randomness | `FakeClock` has independent wall/monotonic instances; `SeededRandom` and `seeded_python_random()` are reusable; the snapshot uses controlled values and zero-jitter formula assertions | Pass |
| Required representative fixture matrix | `fixtures/routing-domain/d001-fixture-matrix.json` enumerates direct/proxied and invalid account cases; catalog outcomes; quota boundaries/claims/malformed state; all health/circuit/quarantine edges; routing/fairness bounds; and model-router TTL/LRU/single-flight/identity cases | Pass |
| Schema-54 state seed | `schema54-routing-domain-seed.sql` seeds providers, accounts, models/support/metadata, refresh state, usage/reservations, backoffs, and quarantine rows without credentials | Pass |
| Exact/semantic/deferred parity classification | Snapshot `parity` map and the contract document classify identity/reasons/order/persisted semantics and policy bytes as exact, representation/time as semantic, and only M7 persistence/selector dispatch as deferred | Pass |
| M5/M6/M7 boundary freeze | Contract document defines `RoutingRequestFacts`, `SelectionClaim`, and `AffinityIdentityInput`; local claim ownership stops before durable inference dispatch; semantic selector inference is explicitly M7 | Pass |
| Deprecated reservation manager excluded | Contract and matrix identify `quota.reservation.ReservationManager` as non-contract production behavior; SQLite reservations plus estimator mirrors remain authoritative | Pass |
| Secret safety | Snapshot and seed scan tests reject API-key markers, proxy passwords, authorization values, and raw affinity content; the adapter stores only synthetic IDs/digests | Pass |
| Python and Rust schema-layer compatibility | Python test migrates and reopens a copy with the seed; Rust compatibility test migrates, applies, queries, and repository-reads the same seed | Pass |
| No production dependency/behavior expansion | Only migration test code, fixture/docs, and Rust test coverage changed; `rust/Cargo.toml`, migrations, and production `src/eggpool` are unchanged | Pass |

## Differential and deterministic results

The committed snapshot is generated twice in one test and compared as parsed
JSON to the committed observation. It includes the following representative
semantic evidence:

- three non-secret accounts across two providers, including a disabled account,
  a proxied account, different weights/priorities, and protocol/surface sets;
- authoritative catalog support, partial-refresh preservation, authoritative
  withdrawal, sibling provider metadata, and relative fresh state;
- persisted request/token/cost windows, request/token capacities, offsets,
  reserved/pending load, score components, and the explicit rule that cost is
  retained for audit but not used as the routing load signal;
- all normalized failure categories plus 402/408/409/422/429/5xx edge
  classification, exact auth handling, context-limit no-suppression, bounded
  backoff, rate-limit cooldown, circuit probe ownership/release, and
  suspected/quarantined/expired/terminal quarantine transitions;
- stable priority/fairness selection metadata and local claim deltas with
  durable persistence explicitly deferred to M7; and
- deterministic route sorting, normalized descriptions, exact compact policy
  bytes, fingerprint, hashed explicit session identity, and miss-then-hit
  affinity stats with no raw content retention.

No comparison uses raw monotonic timestamps, repr/error text, generated object
identity, live providers, inference dispatch, or timing-sensitive thresholds.

## Verification commands actually run

All commands below completed successfully:

```text
rtk uv run ruff format tests/migration_rs/routing_domain_fixtures.py tests/migration_rs/test_d001_routing_domain.py
rtk uv run ruff format --check tests/migration_rs/routing_domain_fixtures.py tests/migration_rs/test_d001_routing_domain.py
rtk uv run ruff check tests/migration_rs/routing_domain_fixtures.py tests/migration_rs/test_d001_routing_domain.py
rtk uv run pytest tests/migration_rs/test_d001_routing_domain.py -q --tb=short --maxfail=1  # 7 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1                    # 56 passed
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility     # 6 passed
rtk cargo test --manifest-path rust/Cargo.toml                                  # 53 passed
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1                       # 14 passed
rtk git diff --check
rtk git diff --cached --check
```

No live provider, inference request, external catalog, credential, broad
performance test, or full Python suite was required for this contract-only
milestone. The focused and smoke gates exercise the changed boundary and the
existing migration harness remains green.

## Database, restart, contention, and security evidence

The seed is applied only to fresh temporary schema-54 copies. The Python and
Rust tests query durable rows for account/provider/model identity, metadata,
refresh state, backoff, quarantine, and repository compatibility. Timestamp
values in the seed are deterministic persistence inputs; the contract requires
hydrators to convert expiry into monotonic remaining duration and cap
nonterminal suppression at 1,800 seconds.

The local claim shape records ownership token and pending/active deltas but
does not claim a durable transaction. Durable request/reservation/attempt
publication and cleanup remain M7. Affinity fixtures enforce the bounded
4,096-entry design and identity hashing; single-flight coordination state is
not persisted. The fixture tests run on the canonical asyncio loop and use
temporary databases with `finally` teardown.

Synthetic proxy URLs have no userinfo. Account API-key environment names are
identifiers only; no values are supplied or persisted. Raw session text is
used only as transient test input and is absent from the digest/observation.

## Unresolved findings and supported differences

Unresolved mandatory findings by severity: none.

There are no new supported runtime differences. The only deferred behavior is
the already-authorized M7 boundary: durable inference lifecycle and semantic
model-router selector dispatch. Internal Python container choices and
controlled time representation are semantic parity differences explicitly
documented in the contract, not implementation gaps.

## Planning follow-through and future-plan state

D001 is removed from the dependency-ready section of
`migration-rs/registry.md` and recorded in completed plans with implementation
commit `40be1bf` and this closure record. D002 is moved to the sole
dependency-ready M5 plan and its plan status is updated to `ready for handoff`.

D003 remains queued behind D002. D004 and D005 remain queued behind D003; the
default serial handoff is unchanged, so neither is prematurely marked ready.
D006 remains queued behind D004 and D005, D007 behind D006, and D008 behind
D001-D007. M6 planning may continue conceptually, but M6 implementation
handoff remains blocked on D008's stable integrated M5 closure. M7 remains
additionally blocked on M6; M8-M12 retain their existing roadmap sequencing.

No future plan other than D002 can be safely unblocked by D001 alone.
