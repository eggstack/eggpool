# Eggfetch Transport Consolidation Roadmap

Status: planned

Depends on: current Rust provider transport and provider transport qualification suite

Follow-up plans:
- `216-phase-1-eggfetch-direct-transport-foundation.md`
- `217-phase-2-eggfetch-egress-dialer-integration.md`
- `218-phase-3-eggfetch-transport-cutover-and-cleanup.md`
- `219-phase-4-eggfetch-qualification-and-footprint-closure.md`

## Objective

Replace Eggpool's bespoke generic HTTP/TLS connection machinery with `eggfetch-core` 0.1.5 while preserving Eggpool's existing provider-routing semantics, failure isolation, connection limits, proxy behavior, and public/internal transport error contract.

The purpose of this work is primarily maintenance consolidation. Eggpool should continue to own provider selection, account isolation, retries/failover, backoff/suppression, request shaping, and Eggress policy. Eggfetch should own the reusable HTTP/1.1 transport mechanics that Eggpool currently maintains itself.

## Research baseline

This plan is anchored to `eggfetch-core` 0.1.5 as prepared by Eggfetch commit `0f720b4fd9efea80d180ce5942fce5e3453d8003` (`chore(release): prepare 0.1.5`). Do not implement against assumptions from a later Eggfetch `main` without first checking that the required API is present in the pinned release.

Eggpool does not currently use `reqwest`. The migration is therefore not a `reqwest -> eggfetch` replacement. It is a consolidation from Eggpool's own Hyper/Rustls transport stack to the equivalent general-purpose facilities now exposed by Eggfetch.

The relevant Eggfetch 0.1.5 embedding surfaces are:

- `ClientBuilder::dialer(...)` and the raw-stream `Dialer` contract,
- `ClientBuilder::physical_connection_policy(...)`,
- `ClientBuilder::transport_io_timeout(...)`,
- `ClientBuilder::retry_canceled_requests(false)`,
- `ClientBuilder::pool_idle_timeout(...)`,
- `ClientBuilder::pool_max_idle_per_host(...)`,
- `HttpVersionPolicy::Http1Only`,
- `TlsConfigBuilder` with `TrustStore::WebPkiOnly` and additional CA support,
- `Client::execute_http_body(...)`, `NativeRequestOptions`, and `NativeResponseBody`,
- typed error inspection for physical-admission timeouts, transport I/O timeouts, connect timeouts, and custom `DialError` failures.

## Current ownership to retire

`rust/src/providers/transport.rs` currently owns generic mechanics that are no longer Eggpool-specific:

- Hyper legacy-client construction,
- Hyper connection pooling and idle eviction,
- disabling Hyper's transparent canceled-request retry,
- physical connection admission and permit lifetime,
- pool admission timeout,
- transport read/write inactivity timers,
- direct TCP connection establishment,
- destination TLS setup, SNI, trust roots, and certificate validation,
- wrapping Eggress raw streams in destination TLS,
- HTTP response-body frame lifecycle and connection reuse behavior.

Those responsibilities are well within Eggfetch's current general transport boundary and are the maintenance burden this roadmap removes.

## Target responsibility boundary

### Eggpool remains responsible for

- provider and account selection,
- coordinator retry/failover attempt accounting,
- provider suppression/backoff policy,
- request target construction and provider-specific request shaping,
- maximum request-body policy,
- stable `TransportError` classification presented to the rest of Eggpool,
- one-client-per-route/account pool identity through `ProviderClientPool`,
- Eggress configuration and selection,
- all policy about whether a request may use a direct or proxied route.

### Eggfetch becomes responsible for

- HTTP/1.1 client framing and response parsing,
- origin TLS negotiation, SNI, certificate verification, and configured trust roots,
- Hyper connection pooling and idle lifecycle,
- physical live-connection admission,
- physical admission timeout,
- established-transport read/write inactivity guards,
- direct connection establishment,
- frame-preserving native response bodies and pool-lease lifecycle.

### Eggress remains responsible for

- SOCKS, HTTP CONNECT, Shadowsocks, SSR, Trojan, SSH, and chained route establishment,
- route-level authentication and route-level TLS where the Eggress protocol requires it,
- returning an authenticated raw byte stream to the requested logical host/port.

Eggfetch must not learn Eggress protocols. Eggress must not become responsible for origin HTTP/TLS. Eggpool should contain only the thin adapter between the two.

## Required behavioral invariants

The migration is accepted only if all of the following remain true:

1. Provider requests remain HTTP/1.1 for this migration. Do not opportunistically enable HTTP/2 or HTTP/3.
2. Eggpool's coordinator remains the sole owner of logical retry/failover attempts.
3. No redirect following is introduced by the transport layer.
4. Hyper's transparent canceled-request retry remains disabled through Eggfetch.
5. `config.max_connections` continues to limit live **physical connections**, including idle pooled connections.
6. `config.pool_timeout` continues to bound waiting for physical-connection admission.
7. Established transport read/write inactivity semantics remain equivalent to the current `TimedConnection` behavior.
8. Direct and each proxied account continue to have separate client/pool identities.
9. Proxy failures remain fail-closed. A failed custom route must never silently fall back to direct networking.
10. Destination/origin TLS remains Eggfetch's responsibility even when the underlying raw stream was established by Eggress.
11. Default trust semantics remain packaged WebPKI roots, with explicit additional CA roots used by tests/configuration. Do not silently switch to native OS roots.
12. Streaming responses remain streaming. Do not buffer the entire response to simplify adaptation.
13. Dropping, canceling, exhausting, or erroring a response body must release resources and leave the client usable according to current behavior.
14. Existing `TransportError` variants and coordinator-facing classifications remain stable unless a separately justified bug is found.
15. Maximum request-body enforcement and request-shape behavior (`Host`, `Content-Length`, target URI) remain unchanged.

## Critical semantic mapping

Do not map similarly named Eggfetch settings mechanically.

Eggpool's current `max_connections` is a physical-connection bound. Eggfetch's ordinary request/concurrency pool controls are logical request controls and are not the semantic replacement.

Use Eggfetch's physical lifecycle controls:

```rust
PhysicalConnectionPolicy {
    max_live: Some(config.max_connections),
    admission_timeout: Some(config.pool_timeout),
}
```

Use `TransportIoTimeout` for the established socket inactivity limits that currently live in Eggpool's `TimedConnection`.

Use Eggfetch's connection-establishment timeout for Eggpool's connect timeout.

Leave Eggfetch logical retry/redirect policy out of the request path. The native `execute_http_body(...)` path is the intended integration surface because it preserves HTTP frames without applying Eggfetch's higher-level redirects, retries, cookies, authentication, automatic decoding, or other convenience policy.

## Dependency policy

Start with an exact pre-1.0 pin and minimum features:

```toml
eggfetch-core = { version = "=0.1.5", default-features = false, features = [
    "http1",
    "tls-rustls",
] }
```

Do not enable Eggfetch's built-in proxy support, HTTP/2, HTTP/3, JSON, compression, cookies, or multipart support for this migration.

Eggfetch 0.1.5 requires Rust 1.89. Raise Eggpool's Rust MSRV from 1.88 to 1.89 as part of phase 1 and update any repository metadata/workflows that encode the old MSRV.

After cutover, direct dependencies that should become candidates for removal include:

- `hyper`,
- `hyper-rustls`,
- `hyper-util`,
- `tower-service`,
- `webpki-roots`,
- direct production use of `rustls` if no other Eggpool code still requires it.

These crates may remain transitively through Eggfetch. The win is ownership/consolidation, not necessarily elimination from the resolved graph.

## Error-boundary policy

Do not leak `eggfetch_core::Error` above the provider transport layer.

Add one explicit Eggfetch-to-Eggpool error translation boundary. Prefer typed Eggfetch inspection over source-string matching:

- physical connection admission timeout -> `TransportError::PoolTimeout`,
- connect establishment timeout -> `TransportError::ConnectTimeout`,
- transport read inactivity -> `TransportError::ReadTimeout`,
- transport write inactivity -> `TransportError::WriteTimeout`,
- custom `DialError` kinds -> existing Eggress/connect categories according to route and failure type,
- TLS -> existing `TransportError::Tls`,
- malformed/protocol/body failures -> the existing closest stable transport categories.

Preserve the diagnostic source chain where practical without including secrets in user-visible errors.

## Rollout model

Do not maintain two permanent transports or introduce a runtime feature switch. Use a staged source migration:

1. establish direct-path parity on Eggfetch while retaining a narrow temporary boundary if needed,
2. add the Eggress `Dialer` adapter and qualify all proxy paths,
3. make Eggfetch the only `ProviderHttpClient` transport implementation,
4. delete the old generic connector/TLS/timer machinery and direct dependencies,
5. perform full qualification and footprint measurement.

Keeping `ProviderHttpClient` and `ProviderClientPool` surfaces stable should contain the blast radius and make rollback a normal source-control revert rather than an additional runtime architecture.

## Phase map

### Phase 1 — Direct transport foundation

Plan: `216-phase-1-eggfetch-direct-transport-foundation.md`

Add the pinned dependency, raise MSRV, reproduce current direct HTTP/TLS/pooling/timeouts through Eggfetch's native API, and prove direct-path behavioral parity before touching Eggress routes.

### Phase 2 — Eggress dialer integration

Plan: `217-phase-2-eggfetch-egress-dialer-integration.md`

Implement a thin custom `Dialer` over Eggress's existing raw-stream connector, preserve account pool isolation and fail-closed routing, and run the existing proxy/chaining/cancellation matrix.

### Phase 3 — Transport cutover and cleanup

Plan: `218-phase-3-eggfetch-transport-cutover-and-cleanup.md`

Make Eggfetch the sole provider HTTP transport, centralize typed error translation, delete the bespoke Hyper/Rustls lifecycle implementation, and remove now-unowned direct dependencies.

### Phase 4 — Qualification and footprint closure

Plan: `219-phase-4-eggfetch-qualification-and-footprint-closure.md`

Run the complete transport/full-suite qualification and measure the actual Eggpool release artifact/dependency impact under controlled before/after conditions.

## Footprint expectations

Do not make binary-size reduction an acceptance assumption.

Eggfetch's own qualification data shows that its minimal fixture can be somewhat larger than a minimal `reqwest` fixture even while resolving fewer unique packages. That comparison is not directly predictive for Eggpool because Eggpool already embeds Hyper/Rustls and substantial custom transport code.

For Eggpool, measure the actual release artifact before and after under the same target, toolchain, profile, LTO/strip settings, and feature set. Record:

- stripped binary bytes,
- direct dependency count/change,
- resolved package count/change,
- transport source/code ownership removed,
- any material release-build regression.

The migration succeeds on maintenance consolidation and behavioral parity even if binary size is approximately flat. Investigate any large unexplained regression rather than imposing an arbitrary byte threshold.

## Verification strategy

Treat `rust/tests/provider_transport.rs` as the primary acceptance suite rather than rewriting it around Eggfetch internals. It already exercises the important externally observable behavior: request shape, connection reuse, idle expiry, streaming/chunking, timeouts, connection failures, physical admission/cancellation, explicit CA roots, account pool isolation, Eggress transports, authentication failure, chaining, fail-closed behavior, and post-cancellation recovery.

Add only focused tests needed to pin newly important adapter/error-boundary behavior. Delete tests only when they test a private implementation that no longer exists and an externally observable acceptance test already covers the behavior.

## Completion criteria

This roadmap is complete when:

- all four phase plans are complete,
- all provider transport paths use Eggfetch 0.1.5 or an explicitly reviewed successor,
- no generic bespoke Hyper/Rustls connection lifecycle remains in Eggpool,
- Eggress integration is a thin raw-stream dialer rather than a duplicate HTTP client,
- coordinator retry/failover ownership is unchanged,
- physical admission and timeout behavior is demonstrably preserved,
- all current transport acceptance tests and the full Rust test suite pass,
- direct dependency cleanup is complete,
- before/after artifact and dependency measurements are recorded,
- no Eggpool-specific feature has been added to Eggfetch merely to complete this migration.

## Handoff notes

Favor deletion and consolidation over creating a new abstraction hierarchy. `ProviderHttpClient` is already the correct containment boundary; keep it unless a concrete blocker requires otherwise.

Do not refactor provider routing, coordinator retry logic, or Eggress protocol configuration while performing this work. Those changes would make transport parity harder to prove and would obscure the maintenance benefit this roadmap is intended to deliver.
