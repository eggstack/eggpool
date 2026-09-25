# Deep Dive: Catalog and Pricing

Back to [Architecture](README.md)

`rust/src/catalog/` owns provider model discovery, normalization, capability
metadata, pricing, limits, withdrawal, and refresh scheduling. The catalog is
provider-scoped first; collapsed public model entries are conservative
summaries and never invent provider capabilities.

`rust/src/catalog/cache.rs` is the in-memory read authority; routing reads never
query SQLite. `refresh.rs` fetches and normalizes provider data, applies
explicit operator overrides, and persists bounded rows through
`rust/src/db/repositories.rs` (`catalog_refresh_state`). Refreshes mutate the
generation router's catalog so they become visible to routing atomically.
Model-info enrichment is bounded startup/tick work attached to the
generation-leased `catalog_refresh` task (`TaskOwnership::ActiveGenerationLeased`,
`models.refresh_interval_s`); it is not a separate scheduler. See
[Model info](deep-dive-model-info.md) for enrichment sources and operator paths.

Thinking/reasoning metadata keeps support, toggle, effort, and budget controls
independent. Unknown remains distinct from unsupported. Routing and wire
adaptation use the exact provider/model contract selected by the catalog.

## Invariants

- refresh failures do not erase usable catalog state;
- provider/model identity is preserved through normalization and persistence;
- stale or withdrawn rows are bounded and explicit;
- pricing is for accounting/observability and never affects routing;
- external sources cannot override a deliberate operator capability override.
