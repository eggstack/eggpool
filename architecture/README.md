# Architecture

This directory is the current design index. Historical implementation plans
remain under `plans/`; this index describes the runtime that is shipped today.

EggPool's public protocol scope is OpenAI Chat Completions at
`POST /v1/chat/completions`, the stateless OpenAI Responses surface at
`POST /v1/responses`, Anthropic Messages at `POST /v1/messages`, and
OpenAI-style model listing at `GET /v1/models`. Responses rejects stateful
fields before provider selection, but its canonical payload and stream
grammar can be adapted to eligible upstream Chat, Messages, Responses, or
native Gemini profiles. `response.completed` is the only successful Responses
terminal; native Gemini terminals are `interaction.completed` or a candidate
`finishReason`. Transport EOF never manufactures a terminal event.

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
The optional `ModelRouterRegistry` is also generation-owned. It contains only
compiled configuration for exact virtual model aliases, so changing
`[model_routers.<id>]` publishes a fresh registry atomically without adding
catalog, health, quota, database, network, or background-task work. The
generation-independent `ModelRouterSelector` consumes one compiled router
through a child `ProxyRequestContext` and the same `RequestCoordinator`; it
does not create a loopback HTTP request or selector-specific client.

## Request lifecycle

`RequestCoordinator` orchestrates endpoint detection, request parsing, model and
account routing, durable request/attempt/reservation state, provider dispatch,
response adaptation, and terminal finalization. Local preparation and response
adaptation failures are terminal local errors. Only typed HTTPX transport
failures may retry, and only across distinct accounts before downstream handoff.
The optional Plan 164 selector is a pre-routing semantic helper: its bounded
non-streaming selector and optional repair requests are concrete child requests
with independent IDs and ordinary accounting. Invalid output, unavailable
selector models, and selector errors return the compiled router's default route;
parent cancellation remains cancellation.

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

Provider/model contracts define URLs, protocol families, wire surfaces,
capabilities, authentication, and prompt-cache dialects. `compose_provider_url()`
is the URL authority. `WireSurfaceName` and `WireProfile` keep concrete
upstream endpoint/codec/auth facts independent from the compatibility
`ProtocolName` values.
`ProviderClientPool` and `OutboundClientManager` use bounded HTTPX connection
pools. Per-account pproxy routing remains supported. Host resolution is
delegated to the operating system; there is no EggPool process-local DNS cache.

See [deep-dive-providers.md](deep-dive-providers.md).

## Protocol transcoding

OpenAI Chat Completions and Anthropic Messages requests/responses are converted
through the transcoder
package. Request encoders receive the provider-bound payload as a read-only
`Mapping`, build a fresh target graph, and hand that graph across the trusted
`adopt_provider_payload()` boundary. `MultimodalCapabilities` in
`catalog/capabilities.py` gives granular per-model media support; provider-
sensitive media forces a final recompute against the selected provider's row.
Native prompt-cache fields are capability-gated by provider/model contract.
TTLs are never silently converted, tool-definition boundaries are not moved
to message boundaries, and cache keys are never synthesized or logged. Loss
policy determines whether unsupported
fields warn or reject. Strict image/PDF base64 validation rejects obvious
encoded-size overflow before decoding and releases the temporary validation
buffer before translated output is built.

The Responses surface is a stateless client surface, not a byte-only
passthrough. `request_surface` identifies the client grammar while the
selected `WireProfile.surface` identifies the concrete upstream endpoint and
codec. The canonical request is captured before provider adaptation; concrete
codecs in `wire/codecs/defaults.py` encode alternate requests and translate
typed response/stream events back to the client grammar. Chat-specific
transforms remain scoped to Chat, and native terminal evidence is required for
successful streaming completion. `responses_path` remains a legacy shorthand
for an `openai_responses` candidate.

`wire/ir.py` defines the deliberately small canonical request, response,
content, tool, usage, reasoning-intent, and streaming-event vocabulary.
`wire/codecs/base.py` defines the codec contract; `compat.py` covers Chat and
Messages, and `defaults.py` implements Responses, Gemini Interactions, and
Gemini `generateContent`. `wire/codecs/runtime.py` adapts selected upstream
responses and streams back to the public client surface. Alternate targets
always encode from the original canonical request, never from a prior target
payload.

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

Model-info external enrichment piggybacks on the generation-leased
`catalog_refresh` event. Startup performs one bounded pass when enabled;
subsequent ticks select due canonical rows by `next_refresh_at` and status/source
TTL state. No standalone `model_info_refresh` scheduler exists, and a source
failure cannot fail catalog discovery or routing.

## Configuration and deployment

`config.toml` plus `.env` configure the service. The copyable profiles bind to
loopback by default; LAN or wildcard binds require the existing server API key.
Live-reloadable settings are explicitly listed in
`src/eggpool/config_reload_policy.py`; unknown fields are rejected.
`[model_routers.<id>]` is an optional live-reloadable configuration surface;
its structural validation does not check current catalog availability.

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
