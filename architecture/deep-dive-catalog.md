# Deep Dive: Catalog and Pricing

Back to [Architecture](README.md)

`rust/src/catalog/` owns provider model discovery, normalization, capability
metadata, pricing, limits, withdrawal, and refresh scheduling. The catalog is
provider-scoped first; collapsed public model entries are conservative
summaries and never invent provider capabilities.

`rust/src/catalog/cache.rs` is the in-memory read authority. `refresh.rs`
fetches and normalizes provider data, applies explicit operator overrides, and
persists bounded rows through `rust/src/db/repositories.rs`. Model-info
enrichment is bounded startup/tick work attached to the generation-leased
catalog refresh; it is not a separate scheduler.

Thinking/reasoning metadata keeps support, toggle, effort, and budget controls
independent. Unknown remains distinct from unsupported. Routing and wire
adaptation use the exact provider/model contract selected by the catalog.

## Invariants

- refresh failures do not erase usable catalog state;
- provider/model identity is preserved through normalization and persistence;
- stale or withdrawn rows are bounded and explicit;
- pricing is for accounting/observability and never affects routing;
- external sources cannot override a deliberate operator capability override.
