# Architecture

This directory is the current design index. Historical implementation plans
remain under `plans/`; this index describes the runtime that is shipped today.

EggPool's public protocol scope is OpenAI Chat Completions at
`POST /v1/chat/completions`, Anthropic Messages at `POST /v1/messages`, and
OpenAI-style model listing at `GET /v1/models`. The service does not claim full
OpenAI API or `/v1/responses` parity; upstream provider protocol labels do not
expand this public surface.

For repository work, start here and follow the relevant deep dive. Active
plans provide scope and sequencing when needed; completed plans are historical
records and should not be traversed as a chain for ordinary changes. Regression
tests follow capability contracts, while historical phase matrices and manual
performance diagnostics are intentionally outside the routine CI surface.

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

Shutdown ownership is ordered: request work and generation-owned finalization /
background tasks are joined first, process-owned database users are stopped
next, and only then does `Database.disconnect()` close the aiosqlite connection.
The event loop is the final owner to tear down. Tests that create databases
directly must mirror this with `try/finally` fixture cleanup; no warning filter
is used to hide a worker thread publishing after loop teardown.

## Providers and network

Provider/model contracts define URLs, protocols, capabilities, authentication,
and prompt-cache dialects. `compose_provider_url()` is the URL authority.
`ProviderClientPool` and `OutboundClientManager` use bounded HTTPX connection
pools. Per-account pproxy routing remains supported. Host resolution is
delegated to the operating system; there is no EggPool process-local DNS cache.

See [deep-dive-providers.md](deep-dive-providers.md).

## Protocol transcoding

OpenAI Chat Completions and Anthropic Messages requests/responses are converted
through the transcoder
package. Request encoders receive the provider-bound payload as a read-only
`Mapping`, build a fresh target graph, and hand that graph across the trusted
`adopt_provider_payload()` boundary. A narrow content-block IR
(`src/eggpool/transcoder/content.py`) provides shared types for cross-protocol
content translation; `MultimodalCapabilities` in `catalog/capabilities.py`
gives granular per-model media support. Native prompt-cache fields are
capability-gated by provider/model contract. TTLs are never silently converted,
tool-definition boundaries are not moved to message boundaries, and cache keys
are never synthesized or logged. Loss policy determines whether unsupported
fields warn or reject. Strict image/PDF base64 validation rejects obvious
encoded-size overflow before decoding and releases the temporary validation
buffer before translated output is built.

See [deep-dive-transcoder.md](deep-dive-transcoder.md).

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

## Manual SBC characterization

Resource characterization is an operational confidence check, not a product
benchmark. On a representative SBC, use a fixed short stabilization window,
`eggpool runtime-status --json`, the startup operational-profile line, and
standard process/socket tools. Provider-backed requests must use real
configured accounts and synthetic, non-sensitive request shapes. Keep
provider/network latency separate from EggPool-local preparation and dispatch
timings. If hardware, safe credentials, or a request dimension is unavailable,
record it as `not measured`; do not extrapolate from a workstation or create a
load/soak harness, performance threshold, or hardware CI gate. See
[Plan 126](../plans/126-provider-backed-sbc-characterization.md) for the
completed evidence record.

## Schema policy

The historical `requests` table is frozen. New persistence must justify durable
lifecycle/accounting or externally visible compatibility value. Feature-specific
diagnostics use existing bounded fields or narrowly scoped sidecars; cosmetic
migrations and generic EAV storage are prohibited. Historical synthetic-cache
columns from earlier migrations remain for compatibility but are no longer
written or exposed.
