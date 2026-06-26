# Outbound Client Lifecycle Cleanup Plan

## Context

Eggpool is intended to run well on small SBC-class deployments, including Raspberry Pi systems where unnecessary process, network, DNS, TLS, and filesystem churn are more visible than on larger servers. A Pi-hole deployment has shown a high volume of DNS requests from eggpool. Before adding a dedicated DNS cache, the first implementation step is to verify that eggpool is not accidentally defeating HTTP connection reuse by constructing fresh clients, sessions, connectors, or transports on hot paths.

DNS caching is useful, but it should not be used to mask a deeper transport lifecycle problem. If provider forwarding, model discovery, update checks, quota checks, or health probes create a fresh outbound client for each operation, the system will repeatedly pay DNS lookup, TCP handshake, TLS handshake, and connection-pool warmup costs. The desired architecture is a small set of long-lived outbound clients owned by application state and reused by all provider/network paths.

## Goals

- Centralize outbound HTTP client construction and ownership.
- Ensure provider request forwarding reuses persistent clients and connection pools.
- Ensure model discovery, update checks, quota checks, health checks, and backup/update background tasks use the same managed client path where appropriate.
- Normalize timeout, keep-alive, HTTP/2, and pool behavior.
- Add minimal network/client lifecycle instrumentation before DNS cache work begins.
- Prevent future helper functions from creating ad hoc HTTP clients on request hot paths.

## Non-goals

- Do not implement a custom DNS cache in this phase.
- Do not rewrite provider protocol logic unless needed to pass through the centralized client layer.
- Do not pin provider hostnames to IP addresses.
- Do not disable TLS verification or change certificate/SNI behavior.
- Do not overfit for Pi-hole specifically; the outcome should be generally useful for SBC and server deployments.

## Current-state audit

Perform a repository-wide audit for outbound network construction. Search for all direct uses of the HTTP client library and any lower-level socket/TLS clients. The audit should include at least:

- Provider request forwarding paths.
- Provider model listing and discovery paths.
- Provider quota, balance, or account-status checks.
- Update-check background task code.
- Backup or restore code if any remote operations are present.
- CLI diagnostic commands that perform network calls.
- Dashboard or API server helpers that call remote services.
- Tests and fixtures that may encode now-stale construction patterns.

For each call site, classify it as one of:

- Hot path: runs per proxied LLM request or streaming request.
- Warm path: runs during provider discovery or startup.
- Background path: runs periodically.
- CLI-only path: runs only when a user explicitly invokes a command.
- Test/mock path.

Hot, warm, and background paths should not construct fresh clients directly. CLI-only paths can be less strict, but should still prefer the shared factory when practical.

## Desired architecture

Introduce or formalize an outbound client manager owned by application state. Names can be adjusted to match existing code style, but the shape should be similar to:

```text
AppState
  ├── ProviderRegistry
  ├── OutboundClientManager
  │     ├── shared base HTTP client
  │     ├── optional per-provider clients when provider-specific settings require them
  │     ├── timeout and connection-pool policy
  │     └── future resolver/cache integration point
  ├── MetricsRegistry
  └── BackgroundTaskManager
```

Provider code should request an outbound client or execute requests through the manager. Provider code should not be responsible for connection-pool policy, keep-alive policy, DNS policy, proxy policy, or future resolver behavior.

A single global client is acceptable if all providers can share transport policy. If some providers require distinct base URLs, headers, proxy behavior, TLS settings, or HTTP/2 behavior, use a keyed per-provider client registry. Even in that case, each provider client should be constructed once and reused.

## Configuration design

Add or consolidate a network/client configuration section. Keep the surface area minimal but explicit.

Recommended initial shape:

```toml
[network.http]
connect_timeout_seconds = 10
request_timeout_seconds = 300
pool_idle_timeout_seconds = 90
pool_max_idle_per_host = 8
http2_adaptive_window = true
http2_keep_alive_interval_seconds = 30
http2_keep_alive_timeout_seconds = 10
```

Adjust keys to match existing config conventions. If the existing HTTP library does not expose some of these options, omit them rather than adding inert configuration.

Provider-level overrides may be useful later, but the first pass should avoid an expansive matrix of overrides unless the repository already has per-provider transport settings.

## Implementation steps

1. Identify the current HTTP client library and all construction sites.
2. Add an `OutboundClientManager` or equivalent module under the existing server/network/provider hierarchy.
3. Move default client construction into the manager.
4. Ensure the manager is initialized once during application startup and stored in app state.
5. Wire provider request forwarding through the shared manager.
6. Wire model discovery through the shared manager.
7. Wire update checks, quota checks, and background network calls through the shared manager.
8. Add a small escape hatch for tests so mock transports or test clients can be injected.
9. Remove or deprecate helper functions that construct ad hoc clients.
10. Add comments/docs near the manager warning that request hot paths must not construct clients.

## Streaming-specific requirements

LLM proxying often uses streaming responses. Ensure the centralized client does not accidentally buffer streaming responses or impose inappropriate request timeouts. Streaming behavior should preserve:

- Incremental body forwarding.
- Backpressure behavior.
- Cancellation propagation when downstream clients disconnect.
- Provider error handling.
- Existing metrics/accounting behavior.

Do not introduce a wrapper that forces full response materialization before forwarding.

## Instrumentation

Add baseline counters and logs before DNS caching is implemented. Suggested metrics:

- `eggpool_outbound_requests_total{provider,host,method,status_class}`
- `eggpool_outbound_request_errors_total{provider,host,error_kind}`
- `eggpool_outbound_request_duration_seconds{provider,host}` histogram if histograms already exist
- `eggpool_outbound_client_builds_total{scope}`

The `outbound_client_builds_total` metric is useful during rollout: it should remain stable after startup rather than increasing with request volume. If metrics naming conventions already exist, follow them.

Do not include API keys, authorization headers, request bodies, or full URLs in metrics or logs. Hostnames are acceptable; query strings should be redacted.

## Tests

Add tests that make client lifecycle regressions hard to reintroduce:

- Unit test that app initialization builds the expected number of outbound clients.
- Unit or integration test that multiple provider requests reuse the same manager/client instance.
- Test that model discovery uses the shared manager.
- Test that background update checks use the shared manager or the same factory.
- Test that streaming responses still stream and are not buffered.
- Test that timeout configuration is parsed and applied.

If direct assertion of connection reuse is difficult, use a mock transport or instrumented client factory that counts constructions. The core acceptance criterion is that request count increases without client construction count increasing.

## Manual validation

Run eggpool locally with Pi-hole or resolver logging enabled. Compare before/after under a small synthetic workload:

- Start server.
- Trigger model discovery.
- Send repeated requests to the same provider/model.
- Let background tasks run once if feasible.
- Confirm outbound client build count does not grow per request.
- Confirm DNS requests decrease if prior churn was caused by fresh clients.

This phase may already reduce most observed DNS noise. If it does, still continue with the DNS cache plan as a bounded polish item, but treat the client lifecycle cleanup as the primary fix.

## Acceptance criteria

- Hot-path provider requests do not construct fresh HTTP clients.
- Model discovery and recurring background network calls use centralized client management.
- Client construction is visible through logs or metrics and does not scale with request volume.
- Existing provider behavior, streaming behavior, and error handling remain intact.
- Timeout and pool configuration are centralized and documented.
- Tests cover the shared-client behavior and guard against accidental per-request client creation.

## Risks and mitigations

Risk: Centralizing clients may accidentally share headers or auth state across providers.

Mitigation: Keep authorization headers request-scoped or provider-scoped. Do not put mutable auth headers into a global shared client if the library treats default headers as global state.

Risk: A single client may not fit all provider transport requirements.

Mitigation: Use a manager that can host a shared default client plus keyed provider-specific clients only where required.

Risk: Streaming responses may be buffered by a convenience wrapper.

Mitigation: Keep streaming paths explicit and covered by tests.

Risk: Metrics may leak sensitive details.

Mitigation: Metrics should use provider name, sanitized host, method, status class, and coarse error kind only.

## Handoff notes

Implement this phase before custom DNS caching. If this phase finds per-request client construction, fix that first and measure the resulting Pi-hole/DNS behavior. The DNS cache should then integrate into the centralized outbound client manager rather than provider code.
