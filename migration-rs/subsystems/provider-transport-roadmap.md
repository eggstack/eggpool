# Provider Transport Subsystem Roadmap

Status: active

Repository baseline: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

Canonical source:

- `../000-long-term-specification.md`
- `../001-terminology-and-domain-model.md`
- `../002-long-term-roadmap.md#M4--provider-http-stack-and-eggress-outbound-proxy-integration`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

## 1. Purpose and ownership

This subsystem ports the provider-facing HTTP transport boundary from Python/HTTPX/httpcore/pproxy to Rust/Hyper/hyper-util/Rustls/Eggress while preserving the current EggPool provider/account connection model.

It owns provider HTTP connection construction, connection reuse and limits, direct TLS transport, per-account proxy transport, proxy URI resolution, transport-level timeout/error semantics, transport diagnostics, and deterministic qualification of direct and proxied connections.

It does not own provider selection, model catalog policy, account eligibility, quota/health, request codecs, upstream authentication/header construction, inference endpoint behavior, retry/failover, durable request finalization, rehash generations, daemon lifecycle, or the global non-provider `OutboundClientManager` equivalent.

## 2. Current Python contract

The Python oracle separates provider traffic from generic outbound networking. `ProviderClientPool` creates one long-lived default client per provider and a dedicated client only for accounts with a configured proxy. A direct account falls back to its provider client. Provider configuration supplies base URL, connect/read/write/pool timeout values, maximum connections, maximum keepalive connections, and keepalive expiry.

The pproxy integration is deliberately below HTTP/TLS semantics: `PProxyNetworkBackend.connect_tcp()` delegates TCP establishment to `pproxy.Connection(...).tcp_connect(host, port)`, after which httpcore/HTTPX owns TLS, HTTP framing, response streaming, pooling, and timeout/error mapping.

The public proxy contract is broader than the minimum common case. Configuration supports named proxies, inline `proxy_url`, and `proxy_url_env`; documentation claims pproxy-style HTTP, SOCKS4/4a, SOCKS5, Shadowsocks, SSR, SSH, Trojan, authentication, and composition. The implementation work must determine which documented forms actually work in the Python oracle before selecting Eggress features. It may not silently narrow a working user-visible contract.

## 3. Rust target boundary

The Rust implementation uses Hyper/hyper-util for provider HTTP pooling and protocol handling, Rustls for TLS, and Eggress only for proxied TCP establishment. It does not start an Eggress listener and does not embed Python.

The intended layering is:

```text
ProviderClientPool
  -> ProviderHttpClient
       -> Hyper/hyper-util connection pool
            -> timeout/connection-limit connector wrapper
                 -> direct TCP connector
                 OR
                 -> Eggress OutboundConnector
            -> Rustls for HTTPS
```

The same HTTP/TLS layer must sit above direct and Eggress-backed TCP streams so proxying does not create a second HTTP implementation.

## 4. Invariants

- Python remains production until migration cutover.
- no provider request constructs a fresh client on the hot path;
- one provider client exists per provider generation; only proxied accounts get a dedicated account client;
- direct accounts on the same provider reuse the provider client;
- proxy configuration is account-scoped exactly as today;
- unsupported or malformed proxy expressions fail closed and never fall back to direct;
- proxy/API credentials are absent from logs, errors, snapshots, and fixtures;
- ambient process proxy environment variables must not silently redirect direct provider clients;
- HTTP protocol behavior is not upgraded opportunistically during migration; HTTP/1.1 remains the compatibility baseline unless oracle evidence requires otherwise;
- redirects remain disabled unless the Python provider client contract says otherwise;
- response bodies remain stream-capable for later M6/M7 work even when M4 tests use finite bodies;
- connection limits and timeouts remain bounded; cancellation must release any connection/permit ownership;
- Eggress is used with `default-features = false`; enabled features are justified by the EggPool-specific compatibility corpus rather than Eggress's broad default feature set;
- no provider routing, retry, codec, or finalization semantics are added in this subsystem.

## 5. Dependency graph

```text
F001-F006 closed
      |
      v
T001 transport contract + fixture freeze
      |
      v
T002 direct Hyper/Rustls provider core
      |
      v
T003 Eggress connector + proxy parity
      |
      v
T004 provider/account client pool
      |
      v
T005 differential qualification + M4 closure
      |
      v
M5 catalog/routing/quota/health planning may become dependency-ready
```

T004 is closed and T005 is now the dependency-ready handoff. Later plans are registered now so the complete workstream is visible, but each becomes ready only after its hard predecessor closes.

## 6. Milestones

### T001 — Provider transport contract and fixture freeze

Class: invariant/infrastructure

Freeze the Python transport behavior that Rust must match. Build the EggPool-specific proxy capability corpus, classify exact versus semantic parity, extend local HTTP/proxy fixtures, and determine the narrow Eggress feature set required by proven contract behavior.

Exit: the direct/provider/account/proxy transport contract is reviewable and executable without live providers, and there is a recorded feature decision for Eggress.

### T002 — Direct Hyper/Rustls provider HTTP core

Class: infrastructure

Implement the direct provider HTTP client using Hyper/hyper-util/Rustls with bounded connection reuse, provider timeout policy, streaming response ownership, cancellation cleanup, and a transport error taxonomy suitable for later coordinator classification.

Exit: direct HTTP and HTTPS local fixtures satisfy the T001 contract and demonstrate connection reuse/limits/timeouts without Reqwest or provider routing.

### T003 — Eggress connector and proxy parity

Class: infrastructure/capability

Add the narrow `eggress-embed` dependency, exact proxy configuration resolution, an Eggress-backed custom Hyper connector, fail-closed construction/runtime behavior, redacted diagnostics, and the proxy protocol corpus selected by T001.

Exit: controlled proxied TCP/HTTP/HTTPS fixtures match the Python pproxy transport boundary for the required proxy forms and never bypass a configured proxy.

### T004 — Provider/account client pool and lifecycle boundary

Class: capability/invariant

Port `ProviderClientPool` semantics around the T002/T003 clients: one default client per provider, account-specific clients only where proxying requires them, direct-account fallback, stable diagnostics, deterministic close/drop behavior, and migration-stage integration into Rust process state without adding dispatch.

Exit: configuration produces the same provider/account transport topology as Python and lifecycle tests show clients/connections do not grow with request count or leak across shutdown.

### T005 — Provider transport differential qualification and closure

Class: invariant

Run the complete direct/proxied transport differential matrix, concurrency/cancellation/timeout cases, proxy feature and redaction checks, and dependency/resource review. Record supported differences explicitly and close M4 only if no unresolved mandatory contract gaps remain.

Exit: M4's long-term roadmap exit condition is satisfied and M5 may be planned against a stable provider transport interface.

## 7. Eggress feature policy

T001 owns the final decision. The expected starting map is:

- `common` + `pproxy-compat` for direct/HTTP/SOCKS compatibility;
- `extended` only if the EggPool contract requires currently working Shadowsocks/Trojan forms;
- `pproxy-legacy` only if currently working SSR behavior is part of the contract;
- `ssh` only if currently working SSH proxy behavior is part of the contract;
- `legacy-crypto` only for specifically proven legacy methods that EggPool must retain;
- no `operations`, `reverse`, or `quic` for provider TCP transport.

Do not use Eggress `full` merely because EggPool documentation lists multiple schemes.

## 8. Failure, cancellation, and resource semantics

Transport construction failures are local/configuration failures and must happen before a request is handed to a provider. Connect/read/write/pool timeout categories must remain distinguishable enough for the later coordinator to reproduce Python failure classification. A dropped/cancelled request must not permanently consume a connection-limit permit or leave an Eggress stream alive. Closing a client pool must bound shutdown and release idle connections.

M4 does not decide retryability or account suppression. It supplies stable transport evidence/errors to M7/M5 rather than embedding policy into the connector.

## 9. Verification strategy

Use deterministic loopback fixtures and the existing migration oracle harness. Prefer paired Python/Rust observations over live provider traffic. Required classes include direct HTTP, direct HTTPS, keepalive reuse, connection limit pressure, connect/read/write/pool timeout behavior, early close, malformed response framing where practical, configured proxy success/failure, authenticated proxy behavior, DNS target behavior, proxy chain behavior selected by T001, cancellation, secret redaction, and unsupported-form fail-closed behavior.

No broad browser, load farm, or live-provider matrix is required for M4 closure.

## 10. Non-goals

- no `/v1/chat/completions`, `/v1/messages`, or `/v1/responses` dispatch;
- no model discovery/catalog refresh policy;
- no provider auth/header/wire-surface construction beyond neutral transport test requests;
- no account routing, quotas, backoffs, health scoring, fairness, or model routers;
- no canonical OpenAI/Anthropic/Gemini transcoding or SSE translation;
- no retry/failover/finalization;
- no global update/pricing/background HTTP manager;
- no runtime generation/rehash implementation;
- no Python pproxy removal from the production Python package yet;
- no Eggress listener, UDP provider transport, transparent proxying, reverse proxy mode, or system proxy mutation.

## 11. Subsystem closure condition

M4 closes only after T001-T005 have individual closure evidence and the Rust candidate can construct and exercise provider/account direct and required proxied HTTP clients against deterministic fixtures with parity-equivalent timeout, isolation, pooling, redaction, and failure behavior. Closing M4 does not mean inference dispatch works; it means the transport beneath later routing/codec/coordinator work is stable.
