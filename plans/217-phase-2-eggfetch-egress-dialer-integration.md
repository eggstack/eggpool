# Phase 2 — Eggfetch Eggress Dialer Integration

Status: planned

Depends on:
- `215-eggfetch-transport-consolidation-roadmap.md`
- `216-phase-1-eggfetch-direct-transport-foundation.md`

## Objective

Route Eggpool's existing Eggress-backed provider connections through Eggfetch's general custom `Dialer` interface without moving proxy protocol knowledge into Eggfetch and without changing Eggpool's fail-closed routing/account-isolation semantics.

At the end of this phase, direct and proxied provider clients should share Eggfetch's HTTP/1.1, origin TLS, pooling, physical admission, and transport-I/O machinery. Eggress should provide only the authenticated raw route to the requested logical destination.

## Scope

### In scope

- implement a thin Eggress-to-Eggfetch `Dialer`,
- map Eggress route errors into Eggfetch `DialError` kinds without string parsing,
- build proxied `ProviderHttpClient` instances with that custom dialer,
- preserve one Eggfetch client/pool identity per proxied account,
- preserve destination host/port and origin TLS semantics,
- preserve fail-closed behavior for every supported Eggress route,
- preserve cancellation recovery and connection-capacity release,
- qualify SOCKS, HTTP CONNECT, Shadowsocks, SSR, Trojan, SSH, and chain routes using existing tests,
- qualify proxy authentication and route failures through the new adapter.

### Out of scope

- Eggfetch built-in proxy support,
- adding Eggress protocols to Eggfetch,
- changing Eggress's public API solely for Eggpool convenience unless an actual missing general-purpose primitive is discovered,
- changing provider routing/account selection,
- changing retry/failover/backoff behavior,
- deleting the old transport implementation before proxy parity is complete,
- HTTP/2 or HTTP/3.

## Architectural rule

The custom dialer boundary must remain deliberately small:

```text
Eggpool provider request
        |
        v
Eggfetch HTTP/1 + origin TLS + pool lifecycle
        |
        v
Eggfetch Dialer trait
        |
        v
Eggpool EggressDialer adapter
        |
        v
Eggress route connector
        |
        v
raw AsyncRead + AsyncWrite stream to logical destination
```

Eggress may perform route/proxy-specific handshakes and route-level TLS, such as a Trojan connection to a proxy. After that succeeds, Eggfetch still performs the destination/origin TLS handshake across the returned byte stream for an `https://` upstream.

Do not terminate origin TLS inside the adapter and do not return a stream whose identity corresponds to the proxy rather than the requested upstream destination.

## Adapter design

Implement the smallest practical private adapter, either in `rust/src/providers/transport.rs` or in a narrowly named provider-transport module if keeping it in the existing file would materially hurt readability.

Do not create a generic Eggpool transport framework around one trait implementation.

Conceptually:

```rust
struct EggressDialer {
    connector: /* existing Eggress connector type */,
}

impl eggfetch_core::transport::Dialer for EggressDialer {
    fn dial(&self, target: DialTarget) -> DialFuture<'_> {
        Box::pin(async move {
            let stream = self.connector
                /* use the current Eggress raw TCP-route API */
                .await
                .map_err(map_egress_dial_error)?;

            Ok(Box::new(stream) as DialStream)
        })
    }
}
```

The exact Eggress method/type names must come from the currently pinned Eggress version in Eggpool. Do not change upstream Eggress merely to make the pseudocode literal.

## Logical destination handling

Eggfetch's `DialTarget` supplies the logical destination host and port. Pass those values unchanged into Eggress's route establishment API, subject only to the type conversion required by Eggress.

Preserve domain names when Eggress can resolve remotely. Do not eagerly resolve every target to a local IP unless the existing Eggress route semantics require that behavior for that protocol.

This matters for:

- SOCKS5 domain-address requests,
- proxy-side DNS behavior,
- destination TLS SNI/hostname verification,
- routes where local DNS would leak or alter behavior.

Add or retain tests that prove the requested host/port reaches the route connector correctly.

## Fail-closed routing

Eggfetch's custom dialer path must be the only physical route available to a proxied `ProviderHttpClient`.

A proxied client must never fall back to Eggfetch's direct connector when:

- proxy authentication fails,
- the proxy is unreachable,
- a chain hop fails,
- the route times out,
- the remote proxy rejects the destination,
- the custom dialer returns any other error.

Do not enable Eggfetch's `proxy` feature alongside the custom dialer. Eggfetch 0.1.5 intentionally rejects incompatible routing combinations; keep the configuration simple and explicit.

## Pool identity and account isolation

Preserve `ProviderClientPool`'s current model:

- one direct provider client,
- one separately built proxied provider client per configured account/route identity.

Each proxied `ProviderHttpClient` must own a distinct Eggfetch `Client`, even if two accounts happen to resolve to the same textual proxy URI/configuration.

Do not globally cache/deduplicate Eggfetch clients by route URI. Existing tests intentionally prove that account/route pools do not collapse into one connection pool.

The same physical connection policy from phase 1 applies independently to each client instance unless current Eggpool configuration explicitly defines otherwise.

## Origin TLS over Eggress

For HTTPS providers:

1. Eggress establishes the requested raw route.
2. Eggfetch receives that stream from `EggressDialer`.
3. Eggfetch performs TLS to the logical destination host using the phase-1 TLS configuration.
4. Eggfetch verifies the destination certificate against WebPKI roots plus configured additional roots.
5. HTTP/1.1 runs inside that origin TLS session.

This separates two trust planes:

- Eggress validates any proxy/route TLS required by the selected transport.
- Eggfetch validates provider/origin TLS.

Do not reuse proxy certificate/trust configuration as provider/origin trust configuration.

## Error conversion inside the dialer

Convert Eggress route failures to Eggfetch `DialError` using the most specific stable category available:

- route/connect establishment failure -> `DialErrorKind::Connection`,
- route timeout -> `DialErrorKind::Timeout`,
- proxy authentication failure -> `DialErrorKind::Authentication`,
- explicit proxy/destination rejection -> `DialErrorKind::Rejected`,
- failures that cannot be classified safely -> `DialErrorKind::Other`.

Attach the original error as the source when the types and lifetime bounds allow it. Do not include proxy credentials/secrets in a custom display string.

Avoid string matching against Eggress error messages if Eggress exposes typed variants or predicates.

## Translation back to Eggpool `TransportError`

Phase 1's Eggfetch error translator must now handle `Error::CustomTransport`/`custom_transport_error()`.

Preserve Eggpool's current distinction between route/Eggress failures and ordinary direct connection failures. The exact mapping should be driven by the existing `TransportError` contract and tests, but the intended shape is:

- custom dial timeout -> existing Eggress timeout category where applicable,
- custom authentication/rejection/route connection error -> existing Eggress/connect category used today for that failure,
- physical admission timeout remains `PoolTimeout` regardless of route,
- Eggfetch origin TLS failure remains `Tls`, not an Eggress authentication error,
- read/write inactivity on an already established proxied stream remains `ReadTimeout`/`WriteTimeout`.

Keep error mapping centralized. Do not duplicate a different conversion table in every proxy protocol branch.

## Cancellation and lifecycle semantics

The existing Eggress transport tests cover important resource behavior. Preserve all of it through Eggfetch's physical connection lifecycle.

Specifically verify cancellation:

- while waiting for physical connection admission,
- during route establishment,
- during proxy authentication/handshake,
- after a connection is established but before response completion,
- while a response body is outstanding.

After cancellation, a subsequent request through the same `ProviderHttpClient` must be able to acquire capacity and complete. No permit, pool lease, Eggress connection, or partially initialized connection should leave the client permanently wedged.

Do not add a retry in the dialer to mask cancellation or route failures. Retry/failover remains Eggpool coordinator policy.

## Connection pooling through custom routes

Allow Eggfetch/Hyper to pool and reuse successfully established proxied connections just as the current Eggpool Hyper client does.

Qualify:

- reuse through the same account/client,
- no reuse across distinct account/client instances,
- idle expiry,
- release after EOF,
- behavior after premature close,
- behavior after early body drop,
- new connection establishment after stale/closed connections without hidden extra logical attempts.

Do not put a second long-lived connection pool inside `EggressDialer`. It should establish raw streams on demand; Eggfetch is the connection-pool owner for provider HTTP transports after this migration.

## Existing proxy matrix to preserve

Run the current provider transport cases covering, at minimum:

- SOCKS4,
- SOCKS5,
- HTTP CONNECT,
- Shadowsocks,
- ShadowsocksR/SSR,
- Trojan,
- SSH,
- supported chained routes,
- authenticated routes,
- authentication rejection,
- unavailable proxy endpoints,
- route/destination rejection,
- proxied HTTPS/origin TLS,
- separate pools for separate accounts/routes,
- cancellation and recovery.

Do not replace these acceptance tests with mocks of `Dialer`; the value of the suite is that it validates actual protocol integration and lifecycle behavior.

A small unit test for `map_egress_dial_error` is useful if it prevents classification drift, but it is supplemental to the integration suite.

## Security checks

During implementation, explicitly confirm:

- no credentials are copied into Eggfetch URLs or logs unless already required by existing provider behavior,
- `DialError`/`TransportError` formatting does not expose proxy passwords, SSH secrets, or provider API keys,
- custom route failure cannot invoke direct network fallback,
- destination certificate validation still uses the logical provider hostname,
- local DNS is not newly forced for routes that previously supported proxy-side DNS,
- chain order and authentication remain owned by Eggress.

## Dependency policy

Continue using the phase-1 Eggfetch feature set. Do not enable Eggfetch's optional proxy dependency tree because Eggress already owns routing.

Do not remove Hyper/Rustls-related direct dependencies until phase 3 confirms no remaining old-path references.

Do not add an Eggfetch feature or upstream API just for Eggpool unless the implementation uncovers a genuine general-purpose gap in the 0.1.5 `Dialer` contract. If a gap appears, document the exact missing capability and stop the affected slice rather than specializing Eggfetch prematurely.

## Validation commands

Run the same baseline commands as phase 1, with the full provider transport test file rather than only direct tests:

```bash
cargo fmt --check
cargo check --all-targets
cargo clippy --all-targets --all-features -- -D warnings
cargo test --test provider_transport
cargo test
```

Match repository CI where platform-specific feature constraints differ.

## Phase completion criteria

Phase 2 is complete when:

- Eggress-backed clients use Eggfetch's custom `Dialer` interface,
- the adapter contains no HTTP protocol or origin TLS implementation,
- Eggfetch's built-in proxy feature remains disabled,
- logical target host/port semantics are preserved,
- all proxy failures remain fail-closed,
- proxy/route and origin TLS trust planes remain separate,
- per-account client/pool isolation is preserved,
- physical connection limits and idle pooling work through custom routes,
- typed custom-dial errors map back to stable Eggpool `TransportError` categories,
- all existing Eggress protocol/chaining/authentication/cancellation tests pass,
- direct transport tests from phase 1 remain green.

## Handoff notes

The desired result is intentionally boring: a small adapter that turns an Eggress raw stream into Eggfetch's `DialStream`. If this phase starts accumulating proxy protocol branches in Eggfetch-facing code, re-evaluate the boundary; protocol behavior belongs in Eggress.

Do not weaken fail-closed tests to accommodate Eggfetch. The 0.1.5 custom dialer path was designed to own the physical route without direct fallback, so a fallback indicates an integration/configuration defect that should be fixed.
