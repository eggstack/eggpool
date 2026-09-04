# M5 Routing-Domain Contract and D001 Fixture Freeze

Status: frozen by D001; see [D001 closure record](closure/routing-domain/001-status.md)

Oracle: the checked-out Python implementation under `src/eggpool/`, exercised
by `tests/migration_rs/test_d001_routing_domain.py`. The observation schema and
representative case matrix are intentionally independent of Python reprs,
private object identity, wall-clock timing, and live provider traffic.

## Ownership and boundary

D001 owns the observation adapters, deterministic clocks/RNG policy, fixtures,
normalization rules, and this contract. D001 does not add production Rust
routing, catalog, health, quota, or model-router behavior.

M5 owns account/catalog state, local quota and selection claims, health and
quarantine, eligibility/ranking/fairness, and compiled model-router policy and
affinity. Request parsing, request persistence, retry/failover, provider
submission, terminal finalization, and semantic selector inference are outside
M5. In particular, the deprecated in-memory
`eggpool.quota.reservation.ReservationManager` is not contract production
behavior; SQLite reservation rows and bounded estimator mirrors are the
authoritative durable boundary.

The committed artifacts are:

| Artifact | Purpose |
|---|---|
| `tests/migration_rs/routing_domain_fixtures.py` | JSON-safe observation types, fake clocks, seeded RNG helper |
| `tests/migration_rs/test_d001_routing_domain.py` | Python oracle construction and invariants |
| `fixtures/routing-domain/d001-fixture-matrix.json` | representative case inventory |
| `fixtures/routing-domain/d001-python-observations.json` | repeatable Python snapshot |
| `fixtures/routing-domain/schema54-routing-domain-seed.sql` | copyable schema-54 durable state seed |

## Python ownership inventory

| Domain | Python oracle | Frozen observations |
|---|---|---|
| Accounts | `accounts.registry.AccountRegistry`, `accounts.state.AccountRuntimeState` | stable identity, provider ownership, enabled/credential availability, weight/priority, protocol/request-surface support, offsets, validation result |
| Catalog | `catalog.cache.ModelCatalogCache`, `catalog.normalizer`, `catalog.protocols`, `catalog.capabilities`, `catalog.limits`, `catalog.service` | global and provider rows, account support, provider mapping, resolved protocol/source, capabilities/limits, relative freshness, refresh outcomes, non-destructive and authoritative support changes |
| Quota | `quota.estimation`, `quota.scorer`, SQLite usage/reservation repositories | persisted request/token/cost windows, capacities, offsets, weights, pending/reserved load, bounded EWMA counts, utilization, score components, eligibility, rank |
| Health | `health.backoff`, `health.circuit_breaker`, `health.health_manager`, failure classification | normalized category, state, failure/cooldown counts, bounded remaining durations, circuit state/probe facts, disabled model markers |
| Quarantine | `failure.quarantine`, `model_quarantine` repository | exact scope key, state transition, count, bounded expiry/provenance, terminal withdrawal and recovery evidence |
| Routing | `routing.eligibility`, `routing.fairness`, `routing.router` | request facts, eligible names, stable exclusion codes, tier, score/native-transcode facts, fairness key/mode/band/index, ranking, selected account, local claim deltas |
| Model router | `model_router.config`, `model_router.registry`, `model_router.affinity` | exact virtual alias, sorted route IDs/labels/models/descriptions, selector/default model, policy bytes/digest/length, fingerprint, sticky limits, hashed identity, cache result/stats |

The adapters observe Python's public semantic methods where possible. A small
number of read-only snapshot fields use private storage only to expose state
that is already a documented cache boundary; this is an oracle adapter, not an
invitation for Rust to copy Python's containers.

## Observation schema and parity classes

`RoutingDomainSnapshot` has `accounts`, `catalog`, `quota`, `health`,
`routing`, and `model_router` families, plus controlled `clocks` and a
`parity` map. JSON object keys and ordered arrays are sorted where ordering is
not itself a policy decision. The following classifications are frozen:

| Field/property | Classification |
|---|---|
| account/model/provider identity, validation category, exclusion/failure reason, candidate membership/order, selected account, persisted semantic values | exact |
| compiled policy bytes, policy length/digest, router fingerprint, affinity digest | exact |
| protocol/capability/limit values and catalog support transitions | exact |
| internal map/list/lock/container representation | semantic |
| equivalent floating-point representation | semantic only with an explicitly stated tolerance in the consuming plan |
| wall-clock timestamps and monotonic deadlines | semantic as controlled relative age/remaining duration; never raw monotonic values |
| request persistence/reservations/attempt publication | deferred to M7 |
| semantic model-router selector inference/dispatch | deferred to M7 |

No consumer may label a behavior deferred merely because it is inconvenient to
fixture. D002-D007 must implement every M5-owned exact or semantic contract;
only the named M7 boundaries remain deferred.

## Determinism and normalization

`FakeClock` instances are callable clocks with explicit `advance()` and are
created independently for wall and monotonic domains. Contract cases pass
controlled times to duration-aware methods or patch the Python module's wall
clock at the cache boundary. A restart comparison records exact configured
durations, controlled remaining durations, or expired/not-expired; it never
compares raw monotonic timestamps.

`SeededRandom` is the injectable RNG surface for future consumers. The
`seeded_python_random()` context manager snapshots/restores Python's global RNG
state for current modules that still import the module-level RNG. Backoff
jitter is disabled for formula assertions and seeded for trace observations;
fairness random mode must use an injected/seeded source. Round-robin sorts
candidate account names before rotating and therefore cannot depend on mapping
insertion order.

Allowed normalization is limited to sorted JSON object keys, explicitly
documented stable ordering, relative time/expiry representation, and derived
SHA-256/base64 values where raw session content is forbidden. Do not normalize
away a change to eligibility, suppression duration, catalog freshness/support,
fairness choice, claim ownership, policy bytes, or fingerprint.

## Frozen vocabularies

The routing exclusion vocabulary is:

`disabled`, `auth_failed`, `quota_exhausted`, `cooldown`, `rate_limited`,
`circuit_open`, `no_provider`, `wrong_provider`, `no_model`, `model_stale`,
`no_protocol`, `protocol_mismatch`, `thinking_unsupported`,
`thinking_unknown`, `thinking_conflicting`, `no_surface`, and
`model_quarantined`.

The normalized failure vocabulary is:

`authentication_failed`, `quota_exhausted`, `rate_limited`,
`model_unavailable`, `connect_timeout`, `connection_failure`,
`upstream_server_error`, `protocol_error`, `context_limit_exceeded`, and
`unknown`.

Fairness modes are `off`, `round_robin`, and `random`; fairness scopes are
`provider_model_protocol`, `provider_model`, and `priority_model_protocol`.
Catalog outcomes are `success_authoritative`, `success_empty`,
`success_partial`, `failed`, and `skipped`. Quarantine states are `healthy`,
`suspected`, `quarantined`, and `terminal_withdrawn`.

## Durable schema-54 fixture

`schema54-routing-domain-seed.sql` is a seed applied after migrations `0001`
through `0054`. It contains representative providers, direct/proxied account
identity (the proxy URL is credential-free), shared and withdrawn model rows,
provider metadata, account support, catalog refresh state, successful/pending
requests, released/active reservations, account-wide/model-scoped backoffs,
and bounded model-quarantine rows.

The fixture is copyable and is never a production database. It records exact
durable values such as account/provider/model identity, row state, counts, and
semantic JSON. Timestamp values are deterministic fixture epochs only; runtime
hydration must convert durable expiry into the process monotonic domain and
cap nonterminal suppression at 1,800 seconds. Python and Rust both open a
fresh migrated copy and read the same rows; no live production database is
used.

## M5/M6/M7 handoff DTOs

These are request-independent conceptual DTOs. D001 freezes their observable
fields without implementing them in Rust or coupling them to M6's future
`CanonicalRequest`.

`RoutingRequestFacts`:

- canonical model ID;
- optional provider constraint;
- client/request surface (`chat_completions`, `responses`, or the later
  Messages adapter identity);
- requested protocol;
- protocols acceptable through later transcoding;
- thinking/capability requirements;
- projected token count;
- optional model-router session identity inputs, already bounded or represented
  by an explicit header digest.

`SelectionClaim`:

- selected account ID/name;
- provider ID;
- canonical and upstream model IDs when known;
- resolved protocol and client surface;
- priority tier, quota score components, native/transcode flag, and fairness
  diagnostics;
- local ownership token;
- active request, pending request/token/cost, and circuit-probe ownership
  deltas;
- explicit publish, rollback, and release outcome.

`AffinityIdentityInput`:

- virtual model ID and router config fingerprint;
- either an explicit session-header digest or bounded already-normalized
  system/developer prefix plus first-user text framing;
- client surface and source (`explicit_session` or `automatic_session`);
- no raw session content after digesting.

M5 ends after local claim ownership is published/rolled back. Durable request,
reservation, attempt, retry, failover, and finalization are M7. M5 model-router
work ends at deterministic compiled policy and bounded affinity state; Python's
semantic selector call remains explicitly M7.

## Verification contract

The D001-focused gate is:

```text
uv run ruff format --check tests/migration_rs/routing_domain_fixtures.py tests/migration_rs/test_d001_routing_domain.py
uv run ruff check tests/migration_rs/routing_domain_fixtures.py tests/migration_rs/test_d001_routing_domain.py
uv run pytest tests/migration_rs/test_d001_routing_domain.py -q --tb=short --maxfail=1
```

The full migration harness, Rust database compatibility suite, and repository
smoke gate are required before closure. A secret-marker scan covers the
snapshot, matrix, seed, and test adapter; fixture observations must not contain
API keys, proxy credentials, raw session text, or authorization values.
