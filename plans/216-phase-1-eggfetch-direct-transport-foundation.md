# Phase 1 — Eggfetch Direct Transport Foundation

Status: complete

Depends on: `215-eggfetch-transport-consolidation-roadmap.md`

## Objective

Prove that `eggfetch-core` 0.1.5 can replace Eggpool's bespoke **direct** provider transport without changing externally observable behavior. This phase deliberately excludes Eggress/custom-route migration so that HTTP/TLS/pooling/timeout semantics can be qualified independently before proxy complexity is introduced.

At the end of this phase the direct provider path should be capable of running on Eggfetch's native request API with parity against the current direct Hyper/Rustls implementation.

## Scope

### In scope

- raise the Rust MSRV from 1.88 to 1.89,
- add exact-pinned `eggfetch-core = 0.1.5` with only the required transport features,
- construct a direct Eggfetch client using HTTP/1.1 only,
- reproduce current WebPKI trust and explicit test/additional CA behavior,
- reproduce physical connection admission and idle-pool semantics,
- reproduce connect and established transport I/O timeouts,
- disable transparent Hyper canceled-request retries through Eggfetch,
- execute provider requests through `Client::execute_http_body(...)`,
- preserve streaming response-body behavior,
- add a stable Eggfetch-to-`TransportError` translation path for the direct cases needed in this phase,
- run the direct subset of the existing provider transport qualification suite.

### Out of scope

- Eggress/custom `Dialer` integration,
- removal of the old Eggress transport implementation,
- provider/coordinator retry changes,
- enabling Eggfetch high-level retries or redirects,
- HTTP/2 or HTTP/3,
- Eggfetch built-in proxy support,
- broad dependency deletion before all paths have cut over,
- binary-size conclusions.

## Files likely to change

Primary:

- `rust/Cargo.toml`
- `rust/Cargo.lock` if tracked/updated by the repository workflow
- `rust/src/providers/transport.rs`
- `rust/tests/provider_transport.rs`

Also update any CI/toolchain/release metadata that explicitly pins Rust 1.88 after confirming it is part of the supported MSRV contract.

Avoid moving unrelated provider code.

## Dependency and MSRV changes

### 1. Raise the MSRV

Change Eggpool's Rust MSRV to 1.89 because `eggfetch-core` 0.1.5 declares Rust 1.89.

Search the repository for all copies of the old `1.88` requirement and update only places that represent Eggpool's supported Rust toolchain/MSRV. Do not mechanically alter historical documentation or unrelated version strings.

### 2. Add Eggfetch with a narrow feature set

Use:

```toml
eggfetch-core = { version = "=0.1.5", default-features = false, features = [
    "http1",
    "tls-rustls",
] }
```

The exact pin is intentional for the initial migration. Eggfetch is pre-1.0 and the integration uses relatively low-level transport APIs.

Do not enable:

- `proxy`,
- `http2`,
- `http3`,
- JSON helpers,
- compression,
- cookies,
- multipart,
- native-root features.

Do not remove the existing direct Hyper/Rustls dependencies yet if the old Eggress path still requires them during the staged migration.

## Direct client construction

Create or refactor the `ProviderHttpClient` construction path so the direct route can build an Eggfetch `Client` with the following semantics.

### HTTP version

Force `HttpVersionPolicy::Http1Only`.

This is a compatibility migration, not a protocol upgrade. The existing transport explicitly uses Hyper HTTP/1 client support and the tests depend on HTTP/1 request/reuse behavior.

### Hidden retry behavior

Configure:

```rust
.retry_canceled_requests(false)
```

The current Eggpool transport deliberately disables Hyper's canceled-request retry so one coordinator attempt maps to one upstream transport attempt. Preserve that invariant.

Do not add Eggfetch logical retries anywhere on this path.

### Physical connection policy

Map current Eggpool configuration to `PhysicalConnectionPolicy`, not Eggfetch's logical request-concurrency setting:

```rust
PhysicalConnectionPolicy {
    max_live: Some(config.max_connections),
    admission_timeout: Some(config.pool_timeout),
}
```

This is a critical semantic requirement. Eggpool currently holds its admission permit for the lifetime of the established physical connection, including while that connection is idle in Hyper's pool. Eggfetch's physical connection policy was designed for this lifecycle.

Do **not** substitute `.max_connections(config.max_connections)` or another logical pool limit. With multiplexed protocols those controls describe requests, not physical transports, and even in the current HTTP/1-only configuration using the wrong abstraction would create future semantic drift.

### Idle pool

Preserve:

- `config.pool_idle_timeout`,
- the current idle-per-host cap behavior.

Set Eggfetch's idle pool controls so the direct-path reuse and idle-expiry tests remain unchanged. Continue to bound idle slots consistently with `config.max_connections`; the physical policy remains the authoritative live-connection bound.

### Connect timeout

Map Eggpool's current connection-establishment timeout to Eggfetch's connect timeout facility.

Verify through the existing refused/unreachable/connect-timeout tests that the resulting error remains `TransportError::ConnectTimeout` where the current implementation reports that category.

Do not conflate the connect timeout with physical admission wait time. Admission wait remains `pool_timeout` and should classify as `PoolTimeout`.

### Established transport read/write inactivity

Replace the direct path's `TimedConnection` behavior with:

```rust
TransportIoTimeout {
    read: Some(config.read_timeout),
    write: Some(config.write_timeout),
}
```

These are transport-level inactivity guards and are the closest semantic match to the current socket wrapper.

Do not also add Eggfetch high-level request read/write timeout policy unless a test demonstrates that a separate existing Eggpool behavior requires it. Doubling timeout layers can change error precedence and classify a body/header stall differently.

## TLS configuration

### Preserve packaged WebPKI roots

Build Eggfetch TLS configuration with:

```rust
TrustStore::WebPkiOnly
```

Do not accept Eggfetch's default native-root behavior for this migration. The existing Eggpool direct client uses packaged WebPKI roots, and changing trust-store authority is outside scope.

### Preserve additional CA support

Translate current explicit DER CA/test-root support to Eggfetch's `TlsConfigBuilder` additional-CA API.

The resulting trust store must be:

- standard WebPKI roots,
- plus every explicit additional root supplied by Eggpool.

Do not replace the base roots with only the additional root unless that is explicitly what the current Eggpool helper does in a particular test path.

### Preserve origin identity verification

Eggfetch must continue to own destination TLS, including SNI and certificate hostname verification. Validate both successful explicit-root TLS and hostname/certificate failure cases.

## Native HTTP execution path

Use:

```rust
Client::execute_http_body(request, NativeRequestOptions::default())
```

or equivalent explicit native options required by the current Eggpool timeout configuration.

This API is preferred because it deliberately avoids Eggfetch's high-level convenience policy: redirects, logical retry policy, cookies, authentication helpers, automatic response decoding, and decoded-body limits.

### Request construction

Continue constructing an `http::Request` in Eggpool. Preserve the existing behavior for:

- method,
- absolute target URI expected by the Eggfetch native API,
- provider headers,
- `Host`,
- `Content-Length`,
- HTTP/1.1 version,
- maximum request-body rejection before dispatch.

Use the current `http-body-util`/`Bytes` body representation if it maps cleanly to Eggfetch's generic body bounds. Do not introduce an intermediate copy or JSON serialization layer merely to use Eggfetch.

### Response handling

Keep the response body frame-preserving and streaming.

The integration should expose/consume Eggfetch's `NativeResponseBody` through the same semantics the current Eggpool callers expect. Do not collect the full response body in `ProviderHttpClient::send` unless the current caller already does so above this layer.

Confirm that connection/pool ownership behaves correctly when a response body is:

- consumed to EOF,
- dropped early,
- canceled by the caller,
- terminated by an upstream error,
- stalled until the read inactivity timeout.

## Error translation

Introduce one private mapping function for Eggfetch direct errors, for example `map_eggfetch_error`, without exposing Eggfetch errors to provider/coordinator code.

Use typed inspection in this order where necessary to avoid broad categories swallowing specific timeout cases:

1. physical connection admission timeout -> `TransportError::PoolTimeout`,
2. established transport read timeout -> `TransportError::ReadTimeout`,
3. established transport write timeout -> `TransportError::WriteTimeout`,
4. connect timeout -> `TransportError::ConnectTimeout`,
5. TLS establishment/verification -> `TransportError::Tls`,
6. malformed/protocol/body framing failures -> the current closest `MalformedResponse` or `Protocol` category,
7. ordinary direct connection failures -> `TransportError::Connect`.

Preserve source/error context where it is safe and useful. Do not parse error strings to discover timeout direction if Eggfetch exposes typed metadata.

Keep existing `TransportError` variants even if some are not yet used by the direct Eggfetch path; phase 2 needs the Eggress-specific variants.

## Staging recommendation

Prefer a contained transition inside `ProviderHttpClient` rather than adding a second public client abstraction.

A temporary internal enum or construction branch is acceptable if it lets the direct path move first while existing Eggress code remains functional. Remove temporary dual-stack scaffolding in phase 3.

Do not add a user-facing configuration flag selecting old versus new transport.

## Required tests

Use the current `rust/tests/provider_transport.rs` cases as acceptance tests. At minimum, the direct subset must prove:

- direct request method/URI/header/body shape is unchanged,
- `Host` and `Content-Length` behavior is unchanged,
- HTTP/1.1 connection reuse works,
- idle connections expire according to `pool_idle_timeout`,
- redirects are not followed,
- no hidden retry creates an extra upstream request,
- response bytes arrive incrementally rather than being buffered wholesale,
- chunked/framed response handling remains correct,
- premature server close maps to the existing error category,
- physical connection admission blocks at `max_connections`,
- admission wait times out as `PoolTimeout`,
- cancellation while waiting for admission does not leak capacity,
- direct connection refusal maps correctly,
- connect timeout maps correctly,
- established read inactivity maps to `ReadTimeout`,
- established write inactivity behavior remains equivalent where covered,
- maximum request-body enforcement occurs before an oversized request is dispatched,
- WebPKI/default TLS still works,
- explicit additional CA roots work,
- hostname/certificate verification still fails closed,
- dropping/canceling a body does not poison subsequent use of the client.

Add focused tests only if an existing acceptance test cannot distinguish the required behavior.

## Validation commands

Use the repository's current Rust workflow as authority, but at minimum run from the Rust crate/workspace root:

```bash
cargo fmt --check
cargo check --all-targets
cargo clippy --all-targets --all-features -- -D warnings
cargo test --test provider_transport
cargo test
```

If the repository does not run `--all-features` in CI or a platform-only feature makes that command inappropriate, match CI rather than inventing a broader permanent requirement.

## Phase completion criteria

Phase 1 is complete when:

- Eggpool declares Rust 1.89 consistently,
- `eggfetch-core =0.1.5` is integrated with only the intended features,
- the direct provider path can use Eggfetch's native HTTP body API,
- HTTP/1-only, no-redirect, and no-hidden-retry invariants are preserved,
- physical connection limits and admission timeout semantics are preserved,
- connect/read/write timeout classifications remain stable,
- WebPKI plus additional CA behavior is preserved,
- direct response streaming and cancellation/resource-release behavior is preserved,
- the direct transport acceptance tests pass,
- no Eggress behavior has been intentionally changed yet.

## Handoff notes

If a direct-path parity test fails, fix the semantic mapping before proceeding to Eggress. Do not paper over a mismatch by weakening the existing test unless the old behavior is demonstrably incorrect and the change is separately documented.

The key risk in this phase is using an Eggfetch convenience control whose name resembles an Eggpool setting but whose lifecycle differs. In particular, treat physical admission, established I/O inactivity, logical request timeouts, and logical request concurrency as separate concepts.

## Completion note

Implemented on `main` (direct Eggfetch client, MSRV 1.89, exact-pinned
`eggfetch-core =0.1.5` with `http1` + `tls-rustls`). Direct-path parity was
proven by the provider transport acceptance subset and re-verified through
the phase 3 cutover and the phase 4 qualification pass (`219`).
