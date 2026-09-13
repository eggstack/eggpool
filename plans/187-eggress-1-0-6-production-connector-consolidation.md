# Plan 187: Eggress 1.0.6 production connector consolidation

> **Status:** complete (verified 2026-09-13; SSH facade exception documented)
>
> **Parent:** Plan 186 — Eggress embed consolidation roadmap
>
> **Phase:** 1 of 3
>
> **Scope:** Upgrade the complete Eggress dependency family to `1.0.6`, migrate normal provider proxy construction to `eggress-embed`, and delete Eggpool's duplicate production multi-hop/SSH chain construction without changing transport semantics.

## Problem statement

`rust/src/providers/transport.rs` currently uses two Eggress construction paths:

1. `EgressProxyDialer`, backed by `eggress_embed::outbound::OutboundConnector` for ordinary proxy URLs.
2. `ChainEgressProxyDialer`, backed directly by `eggress_core::chain::ChainExecutor`, for SSH-containing chains and the test-only custom-root path.

The second path requires Eggpool to understand Eggress internals. It parses pproxy expressions, translates compatibility arguments, serializes/parses TOML, compiles Eggress configuration, extracts hop state, creates an SSH session cache, and constructs the chain executor.

Eggress `1.0.6` moved the production form of that work behind `OutboundConnector::from_pproxy_uri`. The stable facade now accepts canonical multi-hop expressions directly and owns native chain compilation and SSH-capable execution.

The production path should therefore collapse to one embed-level connector boundary.

## Desired result

For a normal runtime build:

```text
ProviderHttpClient
  -> ProviderTcpConnector
      -> direct Hyper HttpConnector       (unproxied and explicit direct:// behavior)
      -> EgressProxyDialer
          -> eggress_embed::outbound::OutboundConnector
              -> Eggress-owned proxy chain
```

Eggpool should no longer construct or retain production `ProxyHopSpec`, `ChainExecutor`, `SshSessionCache`, compiled Eggress configs, or pproxy translation state.

## Workstream A — establish the pre-change baseline

Before editing transport code:

1. Record the current Eggress dependency/feature graph:

   ```bash
   cargo tree --manifest-path rust/Cargo.toml -e features | grep -E 'eggress|eggpool'
   cargo tree --manifest-path rust/Cargo.toml -d
   ```

2. Run the provider transport suite on the current revision:

   ```bash
   cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport
   ```

3. If the repository has a reproducible release build on the implementation host, record the stripped/release binary size for later comparison. This is informational in this phase; Plan 189 owns the final measurement.

Do not add a benchmarking dependency to Eggpool for this work.

## Workstream B — atomically upgrade the Eggress release family

Update every Eggress crate explicitly pinned by `rust/Cargo.toml` from `=1.0.2` to `=1.0.6` in one change, including dev/test protocol crates.

Do not leave a mixed `1.0.2`/`1.0.6` internal Eggress graph. The stable embed crate and any temporarily retained implementation crates must resolve to the same release line.

Initially preserve the current `eggress-embed` feature selection:

- `pproxy-compat`
- `extended`
- `pproxy-legacy`
- `legacy-crypto`
- `ssh`

Feature minimization belongs to Plan 189 after behavior is proven. Removing a feature while simultaneously changing the chain-construction boundary makes failures harder to classify.

After the manifest/lockfile update, compile and run the targeted provider transport suite before refactoring. If Eggress 1.0.6 itself exposes a compatibility regression, isolate that first rather than compensating for it inside the later refactor.

## Workstream C — route production pproxy expressions through the embed facade

### C1. Simplify the normal Eggress connector constructor

Change the production connector helper so `eggress_embed::outbound::OutboundConnector::from_pproxy_uri(proxy_url)` is the canonical constructor for supported proxy expressions, including `__`-separated multi-hop chains.

The helper should not manually create TOML for multi-hop input. Current Eggress already performs native pproxy-chain compilation internally.

Expected conceptual form:

```rust
fn build_egress_connector(
    proxy_url: &str,
) -> Result<eggress_embed::outbound::OutboundConnector, eggress_embed::EggressError> {
    eggress_embed::outbound::OutboundConnector::from_pproxy_uri(proxy_url)
}
```

Exact naming may differ if the implementation keeps an adapter for error mapping, but there must be one production chain-construction authority.

### C2. Remove the SSH-specific production branch

Delete the production need for:

- `proxy_uses_ssh`;
- SSH-specific selection in `ProviderTcpConnector::new`;
- `ChainEgressProxyDialer` for normal runtime construction;
- direct construction of `eggress_transport_ssh::SshSessionCache` in the normal path;
- direct `eggress_server::build_chain_executor` calls in the normal path;
- manual pproxy parse/translate/compile/extract logic in the normal path.

SSH is now a capability of the configured `OutboundConnector` when the embed `ssh` feature is present. Eggpool should not branch on the proxy scheme merely to select an internal executor.

### C3. Keep the low-level adapter small

`EgressProxyDialer` may remain as the bridge from Eggress's `BoxStream` to Eggpool's Hyper connector abstraction. It should do only what Eggpool actually owns:

- clone/hold the already-constructed `OutboundConnector`;
- call `connect_tcp(host, port)`;
- return the stream to `ProviderTcpConnector`;
- preserve the existing outer error mapping.

Do not move HTTP, TLS-to-destination, retry, admission, or timeout ownership into this adapter.

## Workstream D — preserve `direct://` behavior explicitly

Keep the existing explicit `direct://` special case during this phase.

Today Eggpool validates the expression through Eggress but uses the normal direct provider connector instead of the Eggress stream for actual IO. This keeps explicit-direct routing equivalent to the unproxied provider path and supports the existing loopback/private test cases.

Do not replace this with `OutboundConnector::connect_tcp` as incidental cleanup.

Acceptance requirement:

```text
proxy_url == "direct://"
    => remains operational
    => still reaches local test providers
    => still uses Eggpool's direct provider transport semantics
```

If a future change wants Eggress to own `direct://` IO, treat that as a separate behavior change with explicit private/loopback, TLS, timeout, and connection-lifetime qualification.

## Workstream E — preserve Eggpool transport/error ownership

### E1. Provider TLS

Do not alter `ProviderHttpClient`'s destination TLS construction. The proxy connector supplies the underlying byte stream; Eggpool's Rustls/Hyper layer continues to negotiate and verify HTTPS to the provider.

### E2. Physical connection admission

Do not move or weaken `AdmissionConnector`, its semaphore, or `TimedConnection`. `max_connections` must remain a physical live-connection bound as currently implemented.

### E3. Read/write/connect/pool timeout semantics

Do not reinterpret the existing configuration fields. The migration changes how a proxy TCP stream is established, not which layer owns timeout policy.

### E4. Retry ownership

Keep `retry_canceled_requests(false)` and all coordinator-owned retry/failover behavior unchanged. Eggress connector construction must not create hidden HTTP attempts.

### E5. Stable `TransportError` mapping

Do not expose `EggressError` directly through provider APIs. Keep the existing Eggpool categories:

- proxy configuration;
- proxy connect / connect timeout;
- proxy authentication;
- proxy target-connect failure;
- TLS/read/write/etc. categories already owned by the outer transport.

Eggress's public error enum intentionally has broad facade categories. Do not redesign Eggpool's classification taxonomy in this phase merely because the source error type changed. Existing regression tests for authentication, timeout, target failure, and redaction remain authoritative.

## Workstream F — preserve the test-root path temporarily

`ProviderHttpClient::new_with_proxy_test_root` may continue using direct Eggress implementation crates during Plan 187 so the production migration is not blocked by the absence of a public embed trust-injection hook.

Requirements for this temporary exception:

- it remains gated by `#[cfg(feature = "test-support")]`;
- no production constructor calls it;
- the implementation continues to add a deterministic root rather than disabling TLS verification;
- it is isolated and dependency-gated by Plan 188 immediately after this phase.

Do not delete the Trojan custom-root tests to make the dependency graph look cleaner.

## Workstream G — source cleanup

After production construction is migrated, remove imports/functions/types that no longer participate in a non-test build.

Expected production deletions include the old chain compilation/TOML plumbing. Run source search to prove no accidental implementation-crate use remains outside the test-support seam:

```bash
rg 'eggress_(core|config|pproxy_compat|server|uri|transport_ssh)' rust/src
```

At the end of Plan 187, any hits must be either:

- under an explicit `#[cfg(feature = "test-support")]` path retained for Plan 188; or
- separately justified in the implementation notes.

Do not remove manifest dependencies yet solely because production imports disappeared; Plan 188 first makes the test-only boundary explicit, then Plan 189 performs final pruning.

## Targeted acceptance tests

The following behaviors are mandatory gates for this phase. Reuse existing tests in `rust/tests/provider_transport.rs` wherever possible.

### Construction corpus

All previously supported expressions must still construct or reject exactly as intended, including:

- `direct://`;
- SOCKS4/SOCKS5;
- HTTP/HTTPS proxy forms;
- Shadowsocks;
- SSR;
- Trojan;
- SSH;
- HTTP -> SOCKS5 multi-hop.

### Live proxy paths

The existing live tests must continue to prove:

- Shadowsocks traffic is actually encrypted/proxied;
- SSR reaches the provider through the configured transport;
- Trojan succeeds with the deterministic test CA;
- Trojan fails without the custom root;
- SSH succeeds with valid credentials;
- SSH authentication failure remains classified correctly;
- SSH cancellation/timeout remains bounded;
- HTTP -> SOCKS5 multi-hop uses both hops in order.

### Failure isolation

Retain tests proving:

- an unreachable configured proxy never silently bypasses to direct;
- credentials are absent from diagnostics;
- account-specific proxy clients remain isolated from provider-direct clients;
- pool and timeout settings remain attached to proxied clients.

Add a new regression only if the new facade path exposes a behavior not already asserted by this suite.

## Mandatory verification

From repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets --all-features
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport
cargo test --manifest-path rust/Cargo.toml --all-features
cargo tree --manifest-path rust/Cargo.toml -e features
```

Also inspect:

```bash
rg 'ChainEgressProxyDialer|proxy_uses_ssh|translate_from_uris|compile_config|SshSessionCache|build_chain_executor' rust/src/providers
```

For a successful implementation, these names must not remain in the normal production path. Test-support exceptions are handled by Plan 188.

## Completion criteria

Plan 187 is complete when:

- every Eggress pin is on `1.0.6` with no mixed old release in Eggpool's explicit manifest entries;
- normal single-hop, multi-hop, and SSH proxy construction uses `OutboundConnector::from_pproxy_uri`;
- Eggpool's duplicate production chain/TOML compilation path is deleted;
- `direct://` retains current behavior;
- destination TLS, connection admission, timeouts, retry ownership, and `TransportError` behavior remain unchanged;
- targeted and full tests pass;
- the only remaining reason for Eggpool to name Eggress implementation crates in provider transport is the explicit test-support custom-root seam.

## Non-goals

Do not in this phase:

- minimize Eggress feature flags;
- claim binary-size savings;
- replace Hyper/Rustls with Eggfetch;
- move provider retries into Eggress;
- change supported proxy syntax;
- change `direct://` IO ownership;
- remove the deterministic Trojan trust test;
- add an insecure TLS mode;
- redesign Eggress or Eggpool error taxonomies;
- introduce a new generic connector framework.

## Approval checklist

- [x] Eggress dependency family upgraded atomically to 1.0.6.
- [x] Baseline provider proxy suite passed before refactor.
- [x] Production single-hop and multi-hop construction moved behind `eggress-embed`; supported SSH uses the documented 1.0.6 facade fallback because the published facade does not install an SSH session cache.
- [x] The duplicate non-SSH production chain compiler/executor path was removed; the narrow SSH fallback is explicit and feature-gated.
- [x] Explicit-direct behavior preserved.
- [x] Proxy failures still cannot bypass to direct.
- [x] Destination and proxy TLS verification remain enabled.
- [x] Existing timeout/admission/retry invariants remain intact.
- [x] Provider transport and full Rust qualification pass.
- [x] Remaining implementation-crate usage is limited to the documented 1.0.6 SSH fallback and the `test-support` custom-root seam.

## Closure evidence

Verified 2026-09-13 at exact implementation/closure head `d648df8`. All Eggress pins were upgraded atomically from `1.0.2`
to `1.0.6`, and the provider transport suite passed before and after the
change (35 tests in each run). `OutboundConnector::from_pproxy_uri` now owns
normal single-hop and canonical multi-hop construction; the old TOML wrapper
and scheme-based production selection were removed for those paths. The
explicit `direct://` route, destination TLS, connection admission, timeout,
retry, redaction, and no-fallback behavior remain qualified.

During qualification, the published 1.0.6 embed implementation was shown to
construct its executor without an SSH session cache, producing
`no handler for protocols: [Ssh]`. The supported SSH path therefore remains
in a narrowly named, matching-1.0.6 `eggress-ssh-fallback` feature with
`SshSessionCache::new_compatibility()`. This is an intentional, documented
facade exception rather than a silent loss of SSH support; all other normal
proxy construction crosses the embed boundary.

The implementation was revalidated on 2026-09-13 from `fd27582a`: the
provider transport suite passed, strict all-features Clippy passed, the full
all-features Rust suite passed, and the provider architecture documentation
was corrected to describe the shipped boundary and its explicit SSH
exception. No later plan depends on Plan 187; Plans 188 and 189 are already
closed, and no future-plan status transition is required.
