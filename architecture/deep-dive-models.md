# Deep Dive: Data Models

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

Typed configuration and runtime models live in `rust/src/config.rs`,
`rust/src/accounts/registry.rs`, `rust/crates/eggpool-model-routing/` (`policy.rs`,
`identity.rs`, `lib.rs`), `rust/src/model_router.rs`, `rust/src/routing/`,
`rust/src/quota/`, `rust/src/catalog/`, and the typed `db` repositories. Structural config
is validated before generation publication; generation-owned models are immutable after
construction and process-owned caches stay separate. Repositories never store credentials,
prompts, raw bodies, or cache keys.

## Configuration models

`Config` aggregates `ServerConfig`, `UpstreamConfig`, `DatabaseConfig`, `ModelsConfig`,
`RoutingConfig`, `ProviderConfig`/`AccountConfig`, `ModelRouterConfig`, `ModelInfoConfig`,
transcoder/model-override policies, and related sections. `ModelsConfig` holds catalog
tuning (`refresh_interval_s`, `expose_mode`, `startup_refresh`, `stale_after_s`,
`allow_stale_catalog`, `ping_retain_days`, `collapse_models`,
`catalog_withdrawal_policy`). `RoutingConfig` holds `strategy`, `near_tie_epsilon`,
`local_quota_mode`, `fairness_mode`, `fairness_epsilon`, and `fairness_scope`.

`ModelRouterConfig` declares one virtual model: `selector_model`, `default_model`,
`routes: BTreeMap<String, ModelRouteConfig>`, `sticky`, `affinity_ttl_s`,
`selector_timeout_s`, `max_input_bytes`, and `repair_attempts`
(`ModelRouteConfig` is `model` plus `description`). `config.rs::model_router_policy`
adapts each TOML router into the neutral `ModelRouterPolicy` without retaining file I/O,
provider, or transport types in the shared crate.

## Account identity boundary

`accounts/registry.rs::AccountRegistry` is built per validated generation via `from_config`
or `hydrate_from_db` over stable `db::Account` rows plus `CredentialStore::from_config`.
`AccountIdentity` is immutable, credential-free routing identity (`account_id`,
`account_name`, `provider_id`, `enabled`, `has_usable_credentials`, `routing_priority`,
`weight`, `supported_protocols`, `supported_request_surfaces`, `quota_offsets`);
`CredentialStore` keeps raw keys outside identity and renders them only at dispatch
(`get`, `has_usable`). `RequestSurface` is `ChatCompletions`, `Responses`, or `Messages`;
`QuotaOffsets` carries configured cost offsets as policy, not durable identity. Accessors
(`get`, `get_by_provider`, `enabled_snapshot`, `all`, `supports_protocol`,
`supports_request_surface`) expose only copies or references, never secrets.

## Neutral semantic policy vs. app affinity

`eggpool-model-routing` is neutral validation/compilation only: `validate_model_router_mapping`
(structural checks, no catalog availability requirement), `compile_model_router`
(deterministic label-sorted `route_id`s, normalized descriptions, `model-router/v1` static
policy capped by `COMPILED_POLICY_MAX_BYTES` at 64 KiB, SHA-256 `config_fingerprint`),
`ModelRouterRegistry::from_policies`/`get`/`is_virtual`, `CompiledModelRouter`
(`virtual_model`, `selector_model`, `default_model`, `routes`, `route_by_id`,
`static_policy`, fingerprint, TTL/timeout budgets), `resolve_route_id` (exact compiled IDs
only), and hashed session identities (`SessionIdentity`, `SessionSource`,
`AffinityIdentityInput`, `ConversationPrefix`, `ConversationTextFragment`,
`session_identity_from_header` capped at `AFFINITY_SESSION_HEADER_MAX_BYTES`,
`automatic_session_identity` bounded by `AUTOMATIC_PREFIX_MAX_BYTES`). Debug output carries
digests and byte counts, never raw session or conversation text.

`model_router.rs::ModelRouterAffinity` retains the EggPool-owned Tokio TTL/LRU/single-flight
cache (cap `AFFINITY_CACHE_MAX_ENTRIES` 4,096; TTL 1–604,800 s): `get`, `resolve`, and
`stats` with `AffinityDecision`, `AffinityResolution` (`cache_hit`,
`single_flight_join`), `AffinitySelection`, `AffinityStats`, and `AffinityError`.
Keys bind virtual model, config fingerprint, and session digest; stale-fingerprint and
expired entries miss instead of serving cross-policy affinity. A selector chooses one
concrete model before provider routing and cannot pin an account, bypass health/quota, or
reselect after submission.

## Capability and quota shapes

`catalog::CapabilityStatus` keeps `Supported`, `Unsupported`, `Unknown`, `Mixed`, and
`Conflicting` distinct; `ThinkingCapability` separates status, toggle, effort, budget, and
bounds. `routing::RoutingCandidate`/`RoutingScore` and `quota::AccountQuota`/`QuotaPolicy`
carry only bounded numeric/diagnostic facts into selection traces.

## Invariants

- Identity is credential-free; credentials render only at dispatch.
- Unknown capability data stays distinct from unsupported data.
- Compiled policy bytes and fingerprints change only through explicit compatibility review.
- Affinity stores digests and decisions only, never raw requests or secrets.
- Public `coordinator/finite.rs::FiniteRequest` and `request/admission.rs::CompactAdmittedRequest`
  shapes remain compatibility surfaces; private execution inputs avoid duplicating preserved trees.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
```
