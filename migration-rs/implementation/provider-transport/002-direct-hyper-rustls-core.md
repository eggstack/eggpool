# Provider Transport T002 — Direct Hyper/Rustls Provider HTTP Core

Status: closed; see [closure record](../../closure/provider-transport/002-status.md)

Repository baseline for planning: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md#t002--direct-hyperrustls-provider-http-core`

Applicable ADRs: ADR-0001, ADR-0002.

Primary class: infrastructure

## 1. Objective

Implement the reusable direct provider HTTP transport in Rust using Hyper/hyper-util and Rustls, matching the provider-side HTTPX contract frozen by T001. The result is a provider-scoped client capable of sending bounded request bodies and returning stream-capable response bodies over HTTP and HTTPS with controlled connection reuse, limits, timeouts, cancellation, and transport errors.

This milestone deliberately excludes Eggress and per-account proxy behavior so the base HTTP/TLS implementation can be qualified independently.

## 2. Dependencies

Hard: T001 closed. Foundation F001-F006 remain closed.

T003, T004, and all M5+ work remain downstream.

## 3. Python oracle

Use the T001 contract as the primary handoff, but re-inspect current `src/eggpool/providers/client_pool.py`, provider timeout config, stream timeout policy, and relevant HTTPX tests before implementation. If current code differs materially from T001, report the drift rather than silently following the stale plan.

## 4. Dependency policy

Add only the crates required for the provider HTTP core. Expected additions/feature expansions include:

- `bytes`;
- `http-body-util`;
- `hyper-util` with the client/client-legacy/http1/tokio features needed by the chosen implementation;
- `hyper-rustls` with a Rustls/webpki-root configuration justified by T001;
- direct `http`/`hyper` feature adjustments if needed.

Do not add Reqwest. Do not add a second TLS stack. Do not add an ORM, actor framework, generic service container, or provider-specific protocol SDK.

HTTP/2 must remain disabled unless T001 proves it is part of the provider contract.

## 5. Target module boundary

Create a focused provider transport module under `rust/src/providers/` (or the smallest equivalent module layout) containing concepts such as:

- `ProviderHttpClient` — cheap clone/handle around one provider-scoped Hyper client and immutable provider transport settings;
- `ProviderHttpConfig` or conversion from existing `ProviderConfig`;
- `TransportError` — stable Rust-side transport evidence, not routing policy;
- connector wrappers for connection admission/timeouts and IO timeout behavior;
- response wrapper exposing status, headers, extensions needed later, and a stream-capable body.

Do not put this implementation into `server.rs`. Do not split EggPool into additional Cargo packages for M4.

## 6. HTTP request/response contract

The public internal API should be neutral with respect to OpenAI/Anthropic/Gemini. It should accept a method, URI/path, headers, and bytes/body stream as defined by T001, and return raw HTTP response facts and a body stream. Provider auth/wire header construction belongs to later provider/wire work.

For the initial M4 implementation, finite `Bytes`/`Full<Bytes>` request bodies are acceptable because inference JSON construction is later work, but the response body must not be eagerly buffered by the transport layer. M6/M7 need to consume streaming response frames without replacing the HTTP client.

Preserve base URL joining semantics exactly enough for later provider paths. Reject invalid/authority-changing relative targets according to the T001 contract.

## 7. TLS and environment semantics

Use Rustls above the connector. The trust-root policy must be explicit and tested. Do not inherit ambient `HTTP_PROXY`/`HTTPS_PROXY` variables for direct provider traffic. Do not disable certificate or hostname verification to make local tests easier; use a deterministic test CA/connector strategy.

If Python honors certificate-related environment variables that T001 classified as contractual, implement only that proven behavior. Otherwise keep the production trust configuration minimal and deterministic.

## 8. Connection pooling and limits

Map the Python provider settings to Hyper/hyper-util behavior rather than ignoring them.

Required properties:

- connection reuse across multiple requests to the same provider authority;
- total physical connection admission bounded by `max_connections`;
- a caller waiting for connection capacity is bounded by `pool_timeout_s` and receives a distinguishable pool-timeout error;
- idle connection retention is bounded consistently with `max_keepalive` for the provider-scoped authority and `keepalive_timeout_s`;
- cancellation while waiting for capacity does not consume a permit;
- a physical connection holds its admission ownership for its entire lifetime, including while idle in the pool, and releases it when the connection actually closes;
- no counter/permit grows with request volume after steady state.

A viable implementation is a connection-lifetime semaphore inside the connector: acquire before a new physical connection, hold the owned permit inside the returned IO connection object, and let Hyper cancellation abort a pending connector acquisition when an idle pooled connection wins the race. This is guidance, not a requirement if a simpler implementation proves the same external semantics.

Do not serialize all requests through one global mutex merely to enforce limits.

## 9. Timeout semantics

Implement and test the T001 timeout stages:

- pool wait;
- connect, including DNS/TCP/TLS establishment as defined by the contract;
- request write inactivity/guardrail;
- response read inactivity/guardrail.

The provider's effective read timeout must use the same contract T001 froze from `ProviderStreamTimeoutConfig.transport_read_timeout(...)`. Later stream first-byte/idle/max-lifetime semantics may be refined in M6/M7, but the transport guardrail cannot be weaker or unbounded accidentally.

Hyper does not supply HTTPX's four timeout knobs as one object; implement the needed bounds explicitly at the connector/IO/body layer. Avoid a single whole-request timeout that would incorrectly terminate long valid streaming responses.

## 10. Transport error taxonomy

Map implementation-specific errors into stable variants/categories sufficient for later failure classification, including at least:

- `PoolTimeout`;
- `ConnectTimeout`;
- `Connect`/DNS/TLS failure;
- `WriteTimeout` / `Write`;
- `ReadTimeout` / `Read`;
- `Protocol`/remote framing failure;
- `Cancelled` only if explicit representation is useful—otherwise propagate task cancellation cleanly rather than converting it to a normal upstream error.

Do not decide retryability, backoff, provider health, or account suppression in this module.

Error display must not dump request authorization headers, API keys, bodies, or full secret-bearing URIs.

## 11. Lifecycle and cancellation

The client must be cheap to clone and own a shared underlying pool. Dropping one handle must not tear down the pool while other handles are live. Provide an explicit close/shutdown boundary only if the Hyper implementation requires it; otherwise document drop-driven ownership and add tests proving process/generation teardown releases resources.

Cancellation during connect, request write, or response body read must release capacity and close unusable connections. A partially consumed response must either be drained/reusable according to Hyper semantics or cause that connection to be discarded; do not return a poisoned stream to the pool.

## 12. Tests

Add Rust unit/integration tests and migration fixtures covering at least:

- direct HTTP success with exact method/path/body/header observations;
- direct HTTPS success with hostname verification intact;
- two sequential requests reuse one physical connection when allowed;
- idle expiry causes a later physical reconnect;
- maximum connection pressure never exceeds configured capacity;
- pool wait times out in the correct class;
- connect refusal and connect stall classification;
- delayed response read classification;
- bounded request write/backpressure classification where deterministic;
- premature close/protocol error classification;
- response body can be consumed incrementally rather than eagerly buffered;
- cancellation during capacity wait/connect/read releases permits/resources;
- direct client ignores unrelated ambient proxy environment variables;
- no secret-bearing headers/bodies appear in transport error output.

Use loopback fixtures. Do not add live-provider tests to mandatory closure.

## 13. Differential verification

Use the T001 observation model to compare Python and Rust for direct HTTP/HTTPS cases. Exact parity is required for EggPool-owned method/path/request body and materially relevant status/header/body facts. Semantic parity is acceptable for library-specific error strings, generated connection IDs, Date/User-Agent headers if classified incidental, and timing jitter.

A changed timeout stage, unexpected redirect, HTTP/2 upgrade, or environmental proxy use is not normalizable.

## 14. Non-goals

- no Eggress dependency or proxy URI handling;
- no `ProviderClientPool` topology yet beyond test construction;
- no provider authentication injection;
- no model catalog request code;
- no routing/account selection;
- no retry/failover/finalization;
- no inference Axum route integration;
- no global non-provider outbound manager.

## 15. Acceptance criteria

T002 closes when a provider-scoped direct client can satisfy the T001 direct transport corpus over HTTP and HTTPS, demonstrates bounded connection reuse/limits/timeouts/cancellation, returns stream-capable bodies, and introduces no second HTTP/TLS stack or routing policy.

## 16. Stop conditions

Stop if Hyper/hyper-util cannot reproduce a mandatory T001 connection-limit or timeout property without a major architectural workaround; if TLS parity would require disabling verification; or if implementation starts pulling provider wire/routing/coordinator logic into the transport layer.

## 17. Closure evidence

Record dependency/feature delta, module/API shape, direct transport parity matrix, connection reuse/capacity evidence, timeout/error cases, cancellation/resource evidence, secret-redaction assertions, Rust fmt/clippy/test output, migration differential output, and targeted Python oracle tests.
