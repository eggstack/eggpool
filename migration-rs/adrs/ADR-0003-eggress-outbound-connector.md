# ADR-0003 — Eggress In-Process Outbound Connector Replaces pproxy

Status: accepted

## Context

Current EggPool per-account proxying is narrower than running a general proxy service. The HTTPX/httpcore transport creates provider connections by delegating TCP establishment to `pproxy.Connection(...).tcp_connect(host, port)` and then runs normal TLS/HTTP above that stream.

Eggress already exposes a Rust-native listener-free outbound connector that parses pproxy-style expressions and returns an established async TCP stream.

## Alternatives considered

1. Keep Python pproxy through a subprocess or embedded Python runtime.
2. Start an Eggress SOCKS/HTTP listener locally and configure the Rust HTTP client to use localhost.
3. Implement proxy protocols again inside EggPool.
4. Embed Eggress and use `OutboundConnector::from_pproxy_uri(...).connect_tcp(...)` directly beneath Hyper.

## Decision

Use option 4.

Rust EggPool will integrate Eggress in-process as the connector for per-account outbound proxy configuration. It will not start a local proxy listener merely to reproduce the current pproxy transport boundary.

Use the narrowest Eggress features that satisfy EggPool's documented proxy URI contract. `default-features = false` is preferred. Optional features such as SSH, extended encrypted proxy protocols, or legacy compatibility are enabled only when the contract inventory demonstrates EggPool must support them.

Unsupported URI/hop features must fail closed. Failure must never silently become a direct connection.

## Consequences

- Python pproxy disappears from the Rust runtime dependency set.
- the custom Python `AsyncPProxyTransport`, network backend, stream wrappers, and httpcore exception-mapping layer are not ported literally;
- connection pooling remains owned by EggPool's provider/account HTTP clients;
- Eggress owns proxy chain establishment only;
- secrets in proxy expressions must remain redacted in diagnostics.

## Compatibility implications

The migration must build a proxy URI corpus from current docs/config/tests and qualify it against Python pproxy behavior where EggPool claims support.

Eggress-wide pproxy parity is not itself proof of EggPool parity. EggPool must verify the exact subset it exposes, including authentication, DNS behavior, chain syntax, timeouts, errors, and feature-gated schemes.

## Non-goals

This ADR does not add inbound proxy services, UDP provider traffic, transparent interception, system proxy mutation, or new proxy schemes to EggPool.
