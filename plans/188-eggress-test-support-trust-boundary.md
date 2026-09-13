# Plan 188: Eggress test-support trust boundary isolation

> **Status:** complete (verified 2026-09-13; SSH facade exception documented)
>
> **Parent:** Plan 186 — Eggress embed consolidation roadmap
>
> **Depends on:** Plan 187
>
> **Phase:** 2 of 3
>
> **Scope:** Isolate the deterministic custom proxy-TLS trust path and fixture-only Eggress implementation dependencies so default production code crosses only the stable `eggress-embed` boundary.

## Problem statement

After Plan 187, normal provider proxy construction should no longer require direct Eggress implementation crates. One important exception remains: Eggpool's deterministic proxy-TLS integration tests.

`ProviderHttpClient::new_with_proxy_test_root` permits a local test CA to be supplied for proxy protocols such as Trojan. This is intentionally different from disabling certificate verification: production trust semantics remain unchanged while integration tests can establish a real verified TLS session against an ephemeral local peer.

The current public Eggress `1.0.6` `OutboundConnector` constructors do not expose caller-provided `rustls::ClientConfig` or additional trust roots for the proxy-chain TLS layer. The existing Eggpool test-support implementation therefore constructs the chain executor through Eggress implementation crates.

That testing need must not force those implementation crates to remain first-class production dependencies.

## Desired dependency boundary

The end state should be:

```text
Default production build
  eggpool provider transport
    -> eggress-embed

Feature-gated test-support library path
  eggpool test trust adapter
    -> only the Eggress implementation crates required to inject a test TLS root

Integration-test fixtures
  -> protocol/core crates used to implement deterministic local peers
```

The distinction is important:

- transitive implementation crates pulled in by `eggress-embed` are normal facade ownership;
- direct implementation dependencies needed only to compile a test hook should be optional and activated by `test-support`;
- protocol crates used only to create local fixtures belong in dev dependencies;
- no implementation crate should remain a default direct dependency merely because tests need it.

## Workstream A — isolate the custom-root implementation

Move the direct Eggress chain-construction logic needed by `new_with_proxy_test_root` behind an explicit test-support boundary.

Acceptable structures include:

- a small `#[cfg(feature = "test-support")]` submodule in `providers/transport.rs`; or
- a dedicated private `providers::transport::test_support` module if that materially improves readability.

Do not create a reusable public proxy framework. The module exists solely to support deterministic transport qualification.

The test-support implementation may continue to:

- parse the configured pproxy expression;
- compile the chain using the matching Eggress `1.0.6` implementation APIs;
- construct the executor with the supplied Rustls client configuration;
- create SSH session state when required by the chain;
- return the resulting proxied stream through the same Eggpool transport wrapper.

It must not be reachable from the normal production constructor.

## Workstream B — eliminate production references to implementation-crate types

After Plan 187, inspect `rust/src/providers/transport.rs` for implementation-crate names that remain only because shared type aliases or traits expose them.

For example, if the normal `ProxyDialer` future explicitly names `eggress_core::BoxStream`, refactor the adapter so the production bridge returns an Eggpool-local transport type or performs the `TokioIo`/`ProviderStream` conversion inside the embed adapter. Type inference or a local trait object is preferable to making `eggress-core` a default direct dependency solely to spell the facade's return type.

The goal is not to hide transitive implementation details cosmetically. The goal is to ensure non-test Eggpool source does not need to import an Eggress implementation crate.

Acceptance search:

```bash
rg 'eggress_(core|config|pproxy_compat|server|uri|transport_ssh)' rust/src
```

Every remaining match must be visibly under `#[cfg(feature = "test-support")]` or separately documented as an unavoidable facade boundary.

If an implementation crate is still needed by ordinary production code after the Plan 187 refactor, stop and document the exact API gap before proceeding with manifest pruning.

## Workstream C — make test-only dependencies structurally optional

Change `rust/Cargo.toml` so Eggress implementation crates used only by the custom-root library path are optional dependencies activated by `test-support` rather than unconditional production dependencies.

Conceptually:

```toml
[features]
default = []
test-support = [
    # dep:... only for implementation crates actually required by the
    # deterministic proxy TLS trust adapter
]
```

Do not blindly copy the current dependency list into this feature. First refactor the code, then identify the minimal set needed by the gated test-support implementation.

Likely candidates to audit include:

- `eggress-core`;
- `eggress-config`;
- `eggress-pproxy-compat`;
- `eggress-server`;
- `eggress-uri`;
- `eggress-transport-ssh`.

The exact retained set is determined by compiled source usage after Plan 187. Each optional dependency that remains must have a short manifest comment explaining the custom-root test requirement.

All retained Eggress implementation dependencies must use the exact same `1.0.6` release line as `eggress-embed`.

## Workstream D — keep fixture dependencies in dev scope

`rust/tests/provider_transport.rs` directly uses Eggress types/protocol implementations to create real local peers. These are legitimate fixture dependencies and are not evidence that production code owns those protocols.

Audit the existing dev dependencies independently from the library test-support dependencies. Expected legitimate examples include protocol crates used to implement:

- Shadowsocks/SSR fixtures;
- Trojan fixtures;
- low-level stream/target helpers where the test server needs them.

If a fixture dependency is used only by tests, keep it under `[dev-dependencies]` rather than promoting it to a normal dependency.

Do not rewrite functional local peers merely to reduce the number of dev-dependency lines. Real protocol interoperability tests are more valuable than a cosmetically smaller test graph.

## Workstream E — preserve strict TLS semantics

The test trust path must continue to model production TLS verification accurately.

Required invariants:

1. `new_with_proxy_test_root` adds only the caller-provided deterministic root required by the fixture.
2. Hostname/certificate verification remains enabled.
3. Production constructors never consume the test root.
4. A Trojan/local TLS proxy signed by the supplied test CA succeeds.
5. The same proxy without that root fails TLS verification.
6. No `danger_accept_invalid_certs`, custom no-op verifier, or blanket insecure mode is added.
7. Secrets embedded in proxy URLs remain redacted from surfaced errors.

If Eggress 1.0.6 changed the exact executor/TLS constructor shape, adapt the gated test-support implementation while retaining these semantic tests.

## Workstream F — decide whether an upstream Eggress trust hook is warranted

Do **not** make an upstream Eggress API change a prerequisite for this plan.

The current requirement is a test-only Eggpool seam. Adding a stable `eggress-embed` API that accepts arbitrary TLS configuration would expand Eggress's public security surface and create a long-term compatibility obligation.

Only propose an upstream API if research during implementation identifies a broader production use case, such as multiple downstream embedders requiring enterprise/private proxy CAs.

If that broader need appears, the preferred API should be narrowly typed around trust configuration rather than exposing the complete internal `ChainExecutor` constructor. It must not provide a verification-disable escape hatch by default.

Otherwise retain the small gated Eggpool adapter and document it as intentional.

## Workstream G — compile-matrix proof

The feature boundary is successful only if both default and test-support configurations compile independently.

Run at minimum:

```bash
cargo check --manifest-path rust/Cargo.toml --all-targets
cargo check --manifest-path rust/Cargo.toml --all-targets --features test-support
cargo check --manifest-path rust/Cargo.toml --all-targets --all-features
```

Then inspect feature activation:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --features test-support
```

The default tree may still contain Eggress implementation crates **transitively through `eggress-embed`**. That is expected. The requirement is that Eggpool itself no longer activates/directly imports those crates for production-only source.

Use inverse queries where useful:

```bash
cargo tree --manifest-path rust/Cargo.toml -i eggress-core
cargo tree --manifest-path rust/Cargo.toml -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-transport-ssh
```

Interpret these results by ownership path; do not treat transitive presence as failure.

## Mandatory regression tests

Run the provider transport suite explicitly with `test-support`:

```bash
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport
```

The following cases are mandatory:

- Trojan proxy succeeds when its test CA is supplied;
- Trojan proxy fails without the test CA;
- SSH proxy success remains operational;
- SSH auth failure remains classified and redacted;
- SSH timeout/cancellation remains bounded;
- multi-hop transport remains operational;
- unreachable proxies do not fall back to direct;
- the mandatory proxy URI construction corpus still passes.

If current tests do not prove that the custom-root constructor is unavailable in a normal build, add a compile-time/module-boundary assertion only if it is simple. The `#[cfg(feature = "test-support")]` API gate plus default build check is normally sufficient.

## Mandatory verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets
cargo check --manifest-path rust/Cargo.toml --all-targets --features test-support
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport
cargo test --manifest-path rust/Cargo.toml --all-features
```

Also run:

```bash
rg 'eggress_(core|config|pproxy_compat|server|uri|transport_ssh)' rust/src
```

Review every match manually for correct feature gating.

## Completion criteria

Plan 188 is complete when:

- normal provider proxy construction imports only the stable Eggress embed API;
- direct implementation-crate use in `rust/src` is confined to an explicit `test-support` boundary;
- implementation crates needed only by that boundary are optional rather than unconditional production dependencies;
- protocol/helper crates needed only for integration fixtures remain dev dependencies;
- the deterministic custom-root success/failure pair still proves real TLS verification;
- the default build compiles without activating the gated test-support source;
- the all-features build and full proxy suite remain green;
- no insecure TLS bypass was introduced.

## Non-goals

Do not in this phase:

- eliminate transitive Eggress implementation crates from Cargo.lock;
- rewrite real proxy fixtures into mocks;
- weaken TLS verification;
- add a generic certificate-verifier escape hatch;
- require an upstream Eggress change solely for test aesthetics;
- minimize unrelated Eggress features;
- measure final binary-size impact; Plan 189 owns closure measurements.

## Approval checklist

- [ ] Custom-root code isolated under `test-support`.
- [ ] Production source no longer imports Eggress implementation crates.
- [ ] Optional dependency list matches actual gated source use.
- [ ] Dev dependencies remain dev-only where appropriate.
- [ ] All Eggress pins remain on one release line.
- [ ] Test CA succeeds through real TLS verification.
- [ ] Missing test CA still fails verification.
- [ ] No insecure verifier/bypass added.
- [ ] Default, test-support, and all-features compile matrices pass.
- [ ] Provider transport qualification passes.

## Closure evidence

Verified 2026-09-13 at exact closure head `3c39e70`. `new_with_proxy_test_root` remains available only under
`test-support`; its deterministic root is used by the real Trojan fixture and
the missing-root case still fails TLS verification. No insecure verifier or
certificate bypass was introduced. Fixture protocol dependencies are in
`[dev-dependencies]`, and the custom-root implementation dependencies are
optional feature dependencies.

The 1.0.6 facade audit found one separate production exception: its outbound
constructor does not install the SSH session cache required for SSH upstreams.
The supported SSH behavior is preserved through the explicitly named default
`eggress-ssh-fallback` feature, while the custom-root constructor itself stays
under `test-support`. This exact facade gap is carried forward as the only
documented exception to the otherwise stable-embed production boundary.

Default, `test-support`, and all-features checks passed; strict all-features
Clippy passed; the provider transport qualification passed all 35 tests; and
the full all-features Rust suite passed all 476 tests.
