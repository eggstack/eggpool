# Deep Dive: Model-Info Enrichment

Back to [Architecture](README.md)

Model-info enrichment is part of the Rust catalog lifecycle. Startup may run
one bounded external pass when `startup_refresh` is enabled; later work is
attached to the generation-leased `catalog_refresh` task. There is no
standalone model-info scheduler in `rust/src/task_supervisor.rs`.

The catalog stores provider-scoped metadata with source, verification, TTL,
cooldown, and next-refresh state. Sources are the provider catalog plus
bounded `openrouter`/`artificial_analysis`/`huggingface` enrichment with
configured aliases and overrides (`rust/src/config.rs::ModelInfoConfig`).
External failures are isolated from core catalog discovery and routing.
Verified metadata can fill capability dimensions that the provider catalog
omits, while explicit operator overrides remain authoritative. Operator
`modelinfo list/show/refresh/repair` paths live in
`rust/src/operations/operator.rs`.

See `rust/src/catalog/cache.rs`, `rust/src/catalog/refresh.rs`, and
`rust/src/task_supervisor.rs`.
