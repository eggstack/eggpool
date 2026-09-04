# D002 Closure — Account Registry and Catalog Cache/Hydration

Status: closed

Recommendation: closed; D003 is dependency-ready. D004 and D005 remain
queued behind D003, and no later plan is unblocked by D002 alone.

Implementation commits: [`966ca1b`](https://github.com/eggstack/eggpool/commit/966ca1b), [`4110d23`](https://github.com/eggstack/eggpool/commit/4110d23), and [`3916c84`](https://github.com/eggstack/eggpool/commit/3916c84)

Plan: [D002 — account registry and catalog cache/hydration](../../implementation/routing-domain/002-account-registry-and-catalog-cache.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md),
D001's [Python observation fixture](../../fixtures/routing-domain/d001-python-observations.json),
and [schema-54 routing-domain seed](../../fixtures/routing-domain/schema54-routing-domain-seed.sql).

Repository baseline for the handoff: `08597187d00660996ad14df6e5aeedce7dbd696e`.

## Outcome

D002 adds the request-independent Rust account and catalog state boundary. A
validated configuration plus stable durable account rows now produces ordered,
non-secret account identities with provider ownership, credential availability,
routing priority, positive finite weight, request-surface/protocol support, and
config-derived quota offsets. Credentials live in a separate `CredentialStore`
and never enter identity debug, equality, serialization, or snapshots.

The catalog cache owns typed global/provider model identity, support sets,
account/provider-model advertisement keys, protocol resolution, tool/vision and
thinking capability state, effective limits, advisory source metadata,
first/last-seen timestamps, per-account outcome, and durable-first freshness.
Its update primitives preserve uncertain support, gate withdrawal on both
authority flags, preserve sibling provider rows, protect static facts, and
report the same add/update/preserve/withdraw counts needed by D003.

`CatalogRepository` reads the existing schema-54 `models`,
`provider_model_metadata`, `account_models`, and `catalog_refresh_state` tables.
No migration 55, live catalog request, network client, scheduler, or inference
path was added. Config-derived quota offsets remain non-durable because the
current schema has no offset columns; they are materialized from validated
config for the future D004 scorer.

## Requirement-to-evidence matrix

| D002 requirement | Evidence | Result |
|---|---|---|
| Non-secret account identity and stable durable IDs | `AccountRegistry`; D001-shaped account test; identity redaction test | Pass |
| Credential availability without secret exposure | Separate `CredentialStore`; debug/JSON assertions and missing-credential failure test | Pass |
| Provider ownership, priority, weight, offsets, and surfaces | Account identity fields and request-surface assertions; finite-weight regression | Pass |
| Invalid account/provider relationships fail closed | Provider mismatch, missing durable account, unknown enabled durable account, and missing credential errors | Pass |
| Typed global/provider catalog identity | `ModelIdentity`/`ProviderModelIdentity`; schema-54 seed hydration assertions | Pass |
| Support sets and sibling provider rows | Non-destructive/authorized withdrawal test with shared provider support | Pass |
| Authority-gated withdrawal and update counts | `AccountCatalogUpdateResult` add/preserve/withdraw assertions | Pass |
| Static facts and resolved protocol preservation | Static protocol/capability preservation regression | Pass |
| Provider-qualified model IDs | Known-suffix, unknown-suffix, and empty-segment tests | Pass |
| Capability and limit subset | Typed tool/vision/thinking/limit records, conservative limit merge, and provider/global override test | Pass |
| Durable-first freshness and legacy fallback | Durable freshness, stale read, and deleted-refresh-row fallback tests | Pass |
| Corrupt advisory vs mandatory state | Malformed JSON is isolated; invalid protocol fails closed | Pass |
| No schema fork or live polling | Repository reads existing tables; no migration/dependency/network changes | Pass |

## Differential, restart, contention, and security evidence

The Rust hydration test applies the same D001 schema-54 seed used by the Python
migration harness and checks ordered model IDs, account support, provider
ownership, provider capabilities, and durable freshness source. The Rust
account test uses the D001 multi-provider account shape and verifies the same
enabled-account, protocol, surface, priority/weight, and disabled-account
semantics. Python's existing D001 oracle and catalog/provider compatibility
suites remained green.

Hydration is read-only and does not rewrite timestamps. Durable refresh rows are
preferred; when absent, the cache uses provider-specific last-seen timestamps
and then the global model timestamp, matching Python's fallback boundary. The
cache exposes cheap ordered snapshots and keeps routing reads in memory.

SQLite access remains behind the serialized `Database` and typed repository
boundary. Tests use current-thread Tokio fixtures, close databases in every
teardown path, and exercise successful seed hydration and invalid durable
state. No locks span database or network awaits because D002 owns no mutation
lock around those operations.

Credential values, proxy values, authorization fields, and raw request/session
content are absent from every D002 identity and cache record. Advisory JSON is
reduced to typed capability/limit fields for routing; malformed advisory JSON
becomes an empty advisory object, while invalid mandatory identity/protocol,
refresh outcome, timestamp, account linkage, or provider linkage returns a
construction error.

## Verification commands actually run

All commands below completed successfully:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain       # 12 passed
rtk cargo test --manifest-path rust/Cargo.toml                           # 65 passed
rtk uv sync --frozen --extra ci
rtk uv run ruff format --check src/ tests/ scripts/                    # 720 files formatted
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/                                        # 0 errors, 0 warnings
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1                # 14 passed
rtk uv run pytest tests/unit/test_routing_provider.py tests/unit/test_catalog_withdrawal_policy.py tests/unit/test_catalog.py -q --tb=short --maxfail=1  # 108 passed
rtk uv run pytest tests/integration/test_catalog_persistence_reconcile.py tests/integration/test_migration_compatibility.py -q --tb=short --maxfail=1  # 56 passed
rtk uv run pytest tests/integration/test_catalog_cache_reload.py tests/integration/test_catalog_unresolved_models.py tests/integration/test_provider_aware_catalog.py -q --tb=short --maxfail=1  # 15 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1          # 56 passed
rtk git diff --check
rtk git diff --cached --check
```

No live provider, inference request, external catalog, credential, or
performance test is part of this milestone.

## Known limitations and deferred boundaries

D002 intentionally does not own live catalog refresh, catalog persistence
writes, provider models-endpoint requests, pings, quota scoring, health,
quarantine, routing claims, model-router affinity, generation publication, or
background scheduling. D003 owns the refresh/normalization/persistence write
boundary and consumes these cache/repository types. Optional metadata remains
advisory and is not required for routing availability.

Unresolved mandatory findings by severity: none.

Supported differences are limited to Rust's typed ownership/container choices,
epoch-second freshness representation, and config-derived offsets not being
stored in schema 54 because Python also treats them as configuration policy.
No accepted-ADR exception or compatibility waiver is required.

## Planning follow-through and future-plan state

D002 is removed from the dependency-ready section of `migration-rs/registry.md`
and recorded in the completed-plan table with the implementation commits and
this closure record. Its plan status is closed.

D003 is now the sole dependency-ready M5 implementation plan because its hard
D002 predecessor is closed. D004 and D005 remain queued behind D003; the
default serial handoff is unchanged, so neither is marked ready. D006 remains
queued behind D004 and D005, D007 behind D006, and D008 behind D001-D007.

M6 planning may continue conceptually, but M6 implementation handoff remains
blocked on D008's integrated M5 closure. M7 remains additionally blocked on
M6; M8-M12 retain their existing roadmap sequencing. No future plan other than
D003 can be safely unblocked by D002 alone.
