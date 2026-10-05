# Deep Dive: Catalog and Pricing

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

`rust/src/catalog/` owns provider model discovery, normalization, capability metadata,
limits, withdrawal, and refresh scheduling. Authority files are `mod.rs`, `cache.rs`, and
`refresh.rs`; durability goes through `db::CatalogRepository` (`list_models`,
`list_provider_models`, `list_account_model_support`, `list_refresh_state`,
`apply_persistence_batch`). `cache.rs` is the in-memory read authority: routing reads never
query SQLite. `refresh.rs::CatalogService` fetches, normalizes, applies operator overrides,
mutates the generation router's shared cache, and persists bounded rows.

## Cache identity and reads

`ModelCatalogCache` keys collapsed public entries by model ID (`ModelIdentity`), exact
provider contracts by `(model_id, provider_id)` (`ProviderModelIdentity`), support by
model-to-accounts, and freshness/outcomes per account; all maps are `BTreeMap`-ordered for
stable snapshots. `hydrate_from_db` validates mandatory IDs, supported protocols
(`openai`, `anthropic`), resolution status, provider match, and timestamps, skipping the
`__deprecated__` sentinel and stale cross-provider support rows.

Reads used by routing and surfaces: `account_supports_model`, `account_model_is_fresh`,
`get_provider_model`, `supporting_accounts`, `models_for_account`,
`provider_keys_for_account`, `get_effective_limits`, `get_provider_capabilities`,
`get_effective_capabilities`, `exposed_model_ids`, and `snapshot` (`CacheSnapshot`).
Collapsed views are conservative: `get_effective_capabilities` reports `Mixed`/`Unknown`
on disagreement and min/max intersections on budgets, while provider-exact accessors keep
the host contract. `parse_model_provider` implements final-slash provider qualification
against known providers.

## Refresh lifecycle

`CatalogService` is built with `new`, `with_credentials`, or `with_shared_cache`; the last
is the production path, so refreshes mutate the router's live cache under one lock rather
than a disconnected copy. `refresh` serializes on a refresh lock, hydrates once
(`ensure_hydrated`), seeds static models, fetches enabled credentialed accounts
concurrently over `ProviderClientPool`, classifies each `AccountCatalogFetch`, applies it
via `update_from_account`, prunes unreferenced globals, emits `CatalogModelEvent`
(`Reappeared`/`Withdrawn` with `ModelReappearance`), and persists one batch plus bounded
transport pings. `refresh_one_account` refreshes a single eligible account and returns
`None` for unknown/disabled/uncredentialed names.

Refresh-result diffing captures only ordered model IDs and provider/model keys. Persistence
projects owned schema-54 rows directly from the cache while holding its mutex, then drops
that guard before SQLite reads/writes. Pending refresh/ping facts are copied under the
service-state mutex and that guard is also dropped before database awaits; successful
persistence clears them, while failure retains them for retry. Startup hydration clones the
cache before its database await and publishes the hydrated value only after the await.
`CacheSnapshot` remains available for diagnostics and callers that need its broader view.

Outcomes are `RefreshOutcome` (`SuccessAuthoritative`, `SuccessEmpty`, `SuccessPartial`,
`Failed`, `Skipped`). Only fully protocol-resolved observations are destructive:
`authoritative && catalog_withdrawal_policy != "preserve_until_health"` permits withdrawal;
empty/partial/failed/skipped and malformed inputs preserve support. A durable withdrawal
deletes the withdrawn model's dependent rows (account links, provider metadata, price
snapshots) in the same transaction, because `model_price_snapshots.model_id` is NO ACTION
and a stranded snapshot would fail the whole persist batch. `seed_from_account`
adds durable knowledge without claiming freshness; `seed_static_models` and
`set_account_provider` register configuration before first provider contact.
`ModelsConfig` controls `refresh_interval_s`, `startup_refresh`, `stale_after_s`,
`allow_stale_catalog`, `ping_retain_days`, `collapse_models`, `expose_mode`, and
`catalog_withdrawal_policy`. The `catalog_refresh` generation-leased task
(`task_supervisor.rs::runtime_task_inventory`) drives periodic refresh; there is no second
scheduler.

## Normalization and overrides

`normalize_models` handles OpenAI-style `data` arrays and Anthropic list shapes, keeps only
rows with non-empty `id`, caps bodies at 10 MiB with JSON depth validation, and derives
`supports_tools`/`supports_vision` plus provider-catalog thinking signals. `resolve_models`
then applies, in precedence order, per-provider `model_overrides`, global `model_overrides`,
upstream metadata, family mapping, and persisted protocol, clearing protocols the provider
does not speak. Limits resolve per dimension with `provider_override`, `global_override`,
and `upstream_metadata` sources; capability overrides can force thinking dimensions and
vision. Operator capability overrides in cache reads remain authoritative over external
sources and never invent provider support.

## Pricing

`PricingConfig` (`catalogs.openrouter`, `opencode_zen`, `aliases`, `fallback =
"generic_estimate"`) and `QuotaEstimator` cost facts exist for accounting and observability
only. Pricing never affects eligibility, scoring, fairness, or claim acquisition.

## Public surface

`GET /v1/models` (`server/mod.rs`, `server/health.rs::models_api`) projects
`RoutingRouter::catalog_model_ids` into `{object: list, data: [{id, object: model,
owned_by: eggpool, name}]}` with no refresh, probe, or health mutation.

## Discovery bootstrap

Model discovery starts from the bundled provider templates
(`rust/assets/providers/_templates.toml`), whose review authority is current
first-party provider documentation — see "Bundled provider-template
authority" in [Providers and Outbound Clients](deep-dive-providers.md). The
template's base URL plus its model-discovery path is the initial discovery
target; live responses remain the source of truth afterward and refresh
failures never erase usable catalog state.

## Invariants

- Refresh failures never erase usable catalog state; malformed input is non-destructive.
- Provider/model identity survives normalization, merge, hydration, and persistence.
- Stale and withdrawn rows are bounded, explicit, and evented.
- Pricing is observability only and never steers routing.
- External sources cannot override deliberate operator capability overrides.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
```
