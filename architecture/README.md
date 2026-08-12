# Architecture

This directory is the current design index. Historical implementation plans
remain under `plans/`; this index describes the runtime that is shipped today.

## Runtime shape

`src/eggpool/cli.py` bootstraps the fast stdlib-only commands and delegates
ordinary commands to the full Click CLI. The production process is a supervisor
and one Granian worker (`workers = 1`, one asyncio event-loop thread). The
worker owns a process-level database, readiness probe, task supervisor, and
`RuntimeManager`.

`RuntimeManager` publishes immutable generation slots. A generation contains
the provider pool, outbound clients, catalog, router, coordinator, health
manager, statistics service, and generation-leased background tasks. Rehash
prepares a complete candidate through `RuntimeGenerationFactory`, then swaps
it atomically; leases keep in-flight requests on the generation they acquired.

## Request lifecycle

`RequestCoordinator` orchestrates endpoint detection, request parsing, model and
account routing, durable request/attempt/reservation state, provider dispatch,
response adaptation, and terminal finalization. Local preparation and response
adaptation failures are terminal local errors. Only typed HTTPX transport
failures may retry, and only across distinct accounts before downstream handoff.

The database uses SQLite WAL, one serialized primary connection, and caller-owned
`async with db.transaction()` boundaries for DML. Durable identities are created
before ambiguous commit boundaries; indeterminate outcomes fail closed and are
repaired on startup.

## Providers and network

Provider/model contracts define URLs, protocols, capabilities, authentication,
and prompt-cache dialects. `compose_provider_url()` is the URL authority.
`ProviderClientPool` and `OutboundClientManager` use bounded HTTPX connection
pools. Per-account pproxy routing remains supported. Host resolution is
delegated to the operating system; there is no EggPool process-local DNS cache.

See [deep-dive-providers.md](deep-dive-providers.md).

## Protocol transcoding

OpenAI and Anthropic requests/responses are converted through the transcoder
package. Native prompt-cache fields are capability-gated by provider/model
contract. TTLs are never silently converted, tool-definition boundaries are
not moved to message boundaries, and cache keys are never synthesized or
logged. Loss policy determines whether unsupported fields warn or reject.

See [deep-dive-transcoder.md](deep-dive-transcoder.md).

## Request shaping

The supported request-shaping surface is deliberately small:

- provider-reported cache-counter observability;
- canonical request segmentation and cache-boundary tracking;
- optional compression in `observe` or `safe` mode;
- policy-scoped compression overrides;
- bounded dashboard/runtime diagnostics.

Compression is `suffix_only`. Safe transforms operate only on eligible volatile
suffix segments, preserve stable prefixes and protected cache boundaries, and
fail closed on integrity changes. Metrics are reporting-only and never enter
`QuotaFairScorer`. Synthetic cache insertion, compression recommendation
tuning, static-prefix placement, and custom DNS caching are not runtime
features. Removed configuration blocks fail through Pydantic `extra = "forbid"`.

See [deep-dive-cache-compression.md](deep-dive-cache-compression.md) and
`docs/cache-compression.md`.

## Routing and health

Routing is tier-based and load-based. `QuotaFairScorer` uses request/token load,
active requests, health, priority, and account weight; it never uses monetary
cost. Suppression is upstream-authoritative and transient backoff is bounded.
Circuit breakers, per-account health, scoped model quarantine, and readiness
database probes live under `src/eggpool/health/`.

## Background work and observability

`TaskSupervisor` owns process and generation task lifetimes. Optional features
construct no clients/tasks when disabled. Runtime diagnostics are bounded and
redacted; request bodies, credential values, cache keys, and raw tool content
are not persisted. `/readyz` reads a process-owned cached probe snapshot and
never writes.

## Configuration and deployment

`config.toml` plus `.env` configure the service. The copyable profiles bind to
loopback by default; LAN or wildcard binds require the existing server API key.
Live-reloadable settings are explicitly listed in
`src/eggpool/config_reload_policy.py`; unknown fields are rejected.

See [deep-dive-deployment.md](deep-dive-deployment.md), `docs/deployment.md`,
and `docs/live-config-rehash.md`.

## Schema policy

The historical `requests` table is frozen. New persistence must justify durable
lifecycle/accounting or externally visible compatibility value. Feature-specific
diagnostics use existing bounded fields or narrowly scoped sidecars; cosmetic
migrations and generic EAV storage are prohibited. Historical synthetic-cache
columns from earlier migrations remain for compatibility but are no longer
written or exposed.

