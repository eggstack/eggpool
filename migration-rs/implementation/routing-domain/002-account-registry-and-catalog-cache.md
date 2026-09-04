# D002 — Account Registry and Catalog Cache/Hydration

Status: queued behind D001 closure

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d002--account-registry-and-catalog-cachehydration`

Primary class: capability/invariant

## 1. Objective

Port the non-networked account and catalog identity layer to Rust. Given the same validated config and schema-54 database snapshot, Rust must construct the same non-secret account identities, provider ownership, catalog support sets, protocol/capability/limit metadata, provider-specific model identity, and freshness state that Python uses for routing.

D002 intentionally stops before live catalog refresh. D003 will mutate this state from deterministic provider model-list responses.

## 2. Account registry

Add a Rust account registry owned by the migration runtime generation. It should consume already-validated `Config` plus stable account IDs from `AccountRepository` and materialize immutable routing identity equivalent to Python `AccountRuntimeIdentity`.

Required fields/behavior:

- durable account ID and configured account name;
- provider ID;
- enabled state;
- `has_usable_credentials` boolean;
- routing priority and positive finite weight;
- provider/request-surface protocol support;
- quota/cost/request/token offset policy needed by D004;
- lookup by account name and provider;
- deterministic enabled-state ordering for snapshots/tests.

Secret API key values must remain in a separate credential store/API. `Debug`, diagnostics, serialization, equality snapshots, and routing identity must expose only presence/availability, never the value.

Preserve validation behavior for enabled providers that require auth but have no usable key. Unknown providers and structurally invalid account/provider relationships fail generation construction rather than disappearing from the routing pool silently.

## 3. Request-surface/protocol support

Port the request-independent account/provider compatibility helpers from `accounts.registry` without importing M6 canonical request types.

Represent supported public/request surfaces as stable enum/string values sufficient for D006. Preserve the Python rule set for chat-completions/responses/other currently routable surfaces and provider protocol lists. Where a surface can later be transcoded, D002 only exposes provider protocol facts; D006 receives the later-compatible protocol set through `RoutingRequestFacts`.

Do not implement transcoding here.

## 4. Catalog cache state

Implement an in-memory catalog cache with explicit typed records for:

- global model identity;
- `(model_id, provider_id)` provider-specific identity;
- model -> account support set;
- account -> provider mapping;
- account -> provider/model keys last known to be advertised;
- per-account successful refresh timestamp/freshness source;
- protocol/protocol source/resolution status;
- capabilities relevant to eligibility;
- discovered/effective context/input/output limits;
- source metadata required by deterministic model-info/pricing/capability resolution;
- first/last-seen facts where public/dashboard parity needs them.

Prefer immutable/shared records and ordered collections only where stable snapshot order is needed. Do not copy arbitrary JSON metadata per routing decision.

## 5. Catalog update primitives

Port the pure cache mutation semantics needed later by D003:

- `AccountCatalogOutcome` equivalent;
- account/provider association always learned when a valid update occurs;
- supplied models add/update support;
- destructive withdrawal only when `authoritative && allow_withdrawals`;
- failed/partial/empty/uncertain refresh paths preserve prior support;
- provider-specific rows survive when another account on the provider still advertises them;
- global first-seen-wins metadata/protocol resolution behavior;
- static-config protocol/capability preservation against weaker live metadata;
- resolved protocol preservation on non-destructive refresh;
- unused-model pruning only when no account/provider support remains;
- exact support removal/reappearance semantics used by quarantine recovery later.

Expose mutation result counts equivalent to `AccountCatalogUpdateResult` so D003 can persist/diagnose without diffing maps a second time.

## 6. Provider-qualified model IDs

Port `parse_model_provider`/catalog model-ID suffix semantics used by Python. A suffix is a provider qualifier only when it matches a known provider; otherwise the entire model ID remains the base ID. Preserve exact escaping/edge behavior captured by D001.

Do not introduce a new public model naming scheme during migration.

## 7. Capabilities and limits

Port the typed subset of catalog capability/limit representation required for eligibility and public model identity. This includes at minimum:

- tool/vision support where currently represented;
- thinking/reasoning capability states and source precedence required by D006;
- max context/input/output limits;
- provider/global override precedence;
- conservative aggregation across provider rows when Python does so;
- protocol-source/resolution status.

D002 may split capability/limit code into focused modules. Avoid a monolithic Rust translation of Python's large metadata dictionaries.

Pure model-info/pricing parsing helpers may be introduced only where needed to hydrate persisted catalog identity. Network enrichment is D003/M8-boundary work.

## 8. SQLite repository expansion

Extend `rust/src/db/repositories.rs` or split it into focused modules while remaining one Cargo package. Add typed repository methods for existing schema tables needed to hydrate:

- global models and provider-model metadata;
- model/account support relationships;
- catalog refresh state;
- any persisted protocol/capability/source metadata fields required by the cache.

Use the exact current schema and SQL semantics. Do not add migration 55 for repository convenience.

Malformed advisory JSON metadata may be ignored with bounded diagnostics only where Python does the same. Invalid mandatory identity/state (unknown required enum, broken account/provider linkage, impossible required IDs) must fail the candidate generation rather than becoming eligible with defaults.

## 9. Freshness

Reproduce per-account successful-refresh freshness. Durable refresh rows are authoritative where present; legacy model timestamps are only the same fallback Python allows for accounts lacking durable freshness evidence.

Expose read-only methods used by D006 such as:

- account supports model;
- provider-specific protocol/capabilities/limits;
- account/model is fresh under configured TTL;
- all exposed models/provider variants for `/v1/models` and diagnostics later.

Freshness reads must not mutate state.

## 10. Hydration and side-by-side compatibility

Add `hydrate_from_db` or an equivalent constructor that reads the existing Python database without performing a live refresh. Qualify:

1. Python seed -> Rust hydrate snapshot;
2. Rust cache semantic write where D002 owns one -> Python read;
3. corrupt advisory metadata isolation;
4. corrupt mandatory identity fail-closed behavior;
5. stale vs fresh durable refresh rows.

Hydration must not change freshness timestamps merely because Rust opened the DB.

## 11. Memory/resource requirements

Catalog state is long-lived on SBCs. Preserve logical identity without unnecessary duplication:

- store account support as compact sets;
- share global/provider metadata where practical;
- cache config-derived capability override maps once;
- avoid serializing JSON on every eligibility check;
- provide cheap snapshot/count diagnostics rather than cloning the entire cache in hot paths.

No new general caching framework is needed.

## 12. Tests

Add Rust unit/integration and Python/Rust differential cases from D001 for:

- account credential presence without secret exposure;
- provider ownership/priority/weight;
- request-surface protocol support;
- model provider-suffix parsing;
- first-seen/provider-specific metadata;
- sibling account shared support;
- authoritative withdrawal vs uncertainty preservation;
- static protocol/capability preservation;
- provider/global override precedence;
- fresh/stale durable state;
- corrupt JSON vs corrupt required identity;
- prune-unused behavior;
- deterministic snapshot ordering.

Run `cargo fmt`, Clippy `-D warnings`, Rust tests, targeted Python account/catalog tests, migration harness, and `git diff --check`.

## 13. Acceptance criteria

D002 closes only if:

- account registry snapshots match the D001 oracle and contain no secrets;
- schema-54 catalog/account state hydrates without a migration fork;
- catalog support/freshness/protocol/capability/limit reads match Python;
- destructive updates require authoritative withdrawal permission;
- uncertain refresh primitives preserve prior support;
- malformed mandatory identity fails closed;
- no live HTTP/catalog polling is required to build the state;
- routing reads can be satisfied from in-memory state without SQLite per candidate.

## 14. Stop conditions

Do not close if Rust silently drops an enabled account, treats a failed/empty catalog as authoritative withdrawal, marks all accounts fresh after hydration, stores credentials inside identity objects, or requires a new schema merely to express Python's existing catalog state.