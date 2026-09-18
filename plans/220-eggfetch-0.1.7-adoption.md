# Eggfetch 0.1.7 Adoption and Native-Profile Requalification

Status: planned

Planning baseline: `2f0e07e3e5d24771f37cdf144a34ae6ba8ca7a7f` (`main`, Eggpool 0.8.0)

Upstream release baseline:
- published crate: `eggfetch-core =0.1.7`
- Eggfetch release tag: `v0.1.7`
- tagged release commit: `43c3b312f2def887d0f0b7ce539faa626adf2cc8`
- prior Eggpool pin: exact `eggfetch-core =0.1.5`

Related completed Eggpool work:
- `215-eggfetch-transport-consolidation-roadmap.md`
- `216-phase-1-eggfetch-direct-transport-foundation.md`
- `217-phase-2-eggfetch-egress-dialer-integration.md`
- `218-phase-3-eggfetch-transport-cutover-and-cleanup.md`
- `219-phase-4-eggfetch-qualification-and-footprint-closure.md`

## Objective

Adopt the published `eggfetch-core 0.1.7` release in Eggpool's provider transport and select the new native-only HTTP/1 feature profile so Eggpool receives the upstream dependency/footprint improvements without enabling Eggfetch's high-level URL, retry, redirect, authentication, built-in proxy, or HTTP/2/3 policy.

Preserve the provider transport architecture that plans 215-219 already qualified:

- Eggpool owns provider/account routing, retry/failover accounting, stable `TransportError`, request shaping, body bounds, and pool identity.
- Eggfetch owns HTTP/1.1 framing, origin TLS, Hyper pooling, physical connection admission, established transport I/O guardrails, and native response-body lifecycle.
- Eggress owns proxy/route establishment and route-level TLS.
- proxied accounts continue to use Eggfetch's general custom `Dialer`; Eggfetch built-in proxy support stays disabled.
- no configured proxy failure may fall back to direct egress.

This is an upstream-version adoption and dependency-profile correction. It must not reopen the provider transport architecture or introduce a second HTTP path.

## Why 0.1.7 is worth adopting

Eggpool's 0.1.5 migration intentionally accepted an approximately +8% release-artifact increase because the maintenance consolidation was still valuable. The phase-4 closure attributed the new resolved closure to Eggfetch's then-unconditional `url -> idna -> ICU` path plus `dashmap`.

The published 0.1.7 tree contains the general upstream work requested after that measurement:

1. `dashmap` was removed from `eggfetch-core` in favor of a standard-library `RwLock<HashMap<...>>` pool/cache implementation. The per-origin waiter-registration/idle-eviction race was corrected at the same boundary.
2. Native `Client::execute_http_body()` no longer serializes an already-parsed `http::Uri` and reparses it through `url::Url`. Native origin identity is derived directly from `http::Uri`.
3. `url` and `percent-encoding` are optional behind `high-level-url`.
4. The HTTP feature graph now separates primitive transport and routing capability from high-level request policy.
5. High-level logical retry, redirect following, and Basic-auth support are separately feature-owned.
6. Eggfetch's HTTP CONNECT helper is optional behind Eggfetch's built-in `proxy` feature rather than part of the native provider profile.

These changes directly address the dependency closure that made the original Eggpool migration larger.

Eggfetch 0.1.7 also contains two transport behavior changes:

- `Timeout.total` is once again one absolute deadline through response-body EOF/trailers on native/high-level surfaces.
- identical caller-supplied resolved-target routes can reuse a bounded Hyper client.

Neither should change Eggpool behavior under the current provider configuration: Eggpool does not configure Eggfetch `Timeout.total`, and Eggpool does not use `ResolvedTarget` for provider dispatch.

## Critical feature-selection finding

Do **not** perform a version-only bump while retaining Eggpool's current feature list:

```toml
eggfetch-core = { version = "=0.1.7", default-features = false, features = [
    "http1",
    "tls-rustls",
] }
```

That is the wrong 0.1.7 profile.

In 0.1.7, `http1` is a compatibility/high-level alias:

```text
http1
  -> native-http1
  -> high-level-url
  -> logical-retry
  -> redirects
  -> basic-auth
```

Using it would re-enable exactly the policy/dependency families Eggpool's native provider transport does not use.

The intended Eggpool dependency is:

```toml
eggfetch-core = { version = "=0.1.7", default-features = false, features = [
    "native-http1",
    "tls-rustls",
] }
```

`native-http1` expands to Eggfetch's HTTP/1 transport plus both routing capabilities:

```text
transport-http1
standard-route
advanced-routing
```

That is the correct shape for Eggpool because:

- direct provider clients need the normal standard DNS/TCP/TLS route;
- proxied provider clients need `advanced-routing` for the custom Eggress `Dialer`;
- both paths use the frame-preserving native `execute_http_body()` surface;
- neither path needs `high-level-url`, logical retry, redirect following, Basic auth, Eggfetch built-in proxy, HTTP/2, HTTP/3, compression, cookies, JSON, or multipart.

Do not replace `native-http1` with `standard-http1`. The latter intentionally omits advanced routing and therefore cannot support Eggpool's custom Eggress dialer.

Do not spell out `transport-http1 + standard-route + advanced-routing` unless an upstream compatibility problem requires it. Prefer the upstream semantic alias `native-http1`.

## Expected source changes

The production change should remain small.

Expected files:

- `rust/Cargo.toml`
- `rust/Cargo.lock`
- `rust/src/providers/transport.rs` only for stale version/feature comments or compile-required compatibility adjustments
- `architecture/deep-dive-providers.md`
- this plan's closure record

Do not modify provider/coordinator behavior merely because the dependency version changed.

### Manifest change

Change:

```toml
eggfetch-core = { version = "=0.1.5", default-features = false, features = [
    "http1",
    "tls-rustls",
] }
```

to:

```toml
eggfetch-core = { version = "=0.1.7", default-features = false, features = [
    "native-http1",
    "tls-rustls",
] }
```

Keep the exact pre-1.0 pin. This plan does not authorize a semver range.

Update the lockfile from the registry release. Do not use a git dependency or patch override once 0.1.7 is available from crates.io.

## Provider transport compatibility audit

Before changing behavior, compile the existing provider transport against 0.1.7 and verify that the following public surfaces remain available under `native-http1,tls-rustls`:

- `Client` / `ClientBuilder`
- `Client::execute_http_body()`
- `NativeRequestOptions`
- `NativeResponseBody`
- `HttpVersionPolicy::Http1Only`
- `Dialer`, `DialTarget`, `DialFuture`, `DialStream`, `DialError`, `DialErrorKind`
- `PhysicalConnectionPolicy`
- `TransportIoTimeout` / `TransportIoDirection`
- `TlsConfig` / `TrustStore::WebPkiOnly`
- `Timeout` / `TimeoutPhase`
- `Error::is_physical_connection_admission_timeout()`
- `Error::custom_transport_error()`

The 0.1.7 tagged source retains these APIs under the intended features. Treat compilation as the final package-level proof.

Do not switch to Eggfetch's high-level `RequestBuilder` API.

## Timeout semantics

Eggpool currently configures:

```rust
Timeout {
    connect: Some(config.connect_timeout),
    ..Default::default()
}
```

and separately configures:

```rust
TransportIoTimeout {
    read: Some(config.read_timeout),
    write: Some(config.write_timeout),
}
```

Keep that division.

Do not add `Timeout.total`, `Timeout.read`, `Timeout.write`, or `Timeout.pool` merely because 0.1.7 improved total-deadline handling. Doing so would add a second logical timeout layer and could change established Eggpool error precedence.

The 0.1.7 `Timeout.total` correction should therefore be behaviorally dormant for Eggpool. Keep the defensive `TimeoutPhase::Total` mapping in `map_eggfetch_error()` unless the compiler/API requires a narrower change.

Qualification must prove that:

- slow/stalled provider response bodies still classify through `TransportIoTimeout::Read` -> `TransportError::ReadTimeout`;
- stalled request writes still classify through `TransportIoTimeout::Write` -> `TransportError::WriteTimeout`;
- connect timeout remains `ConnectTimeout` for direct routes and `ProxyConnectTimeout` for custom Eggress routes;
- physical admission timeout remains `PoolTimeout`;
- no new total deadline terminates a long but active provider stream.

## Native URI semantics

0.1.7 now derives native origin facts directly from Eggpool's `http::Uri`.

Eggpool already constructs absolute provider URIs in `join_provider_target()`, rejects credential-bearing authorities, and keeps provider target paths relative to the configured base authority. Preserve that code and use it as the integration boundary.

Add or retain focused coverage proving:

- normal HTTP and HTTPS provider authorities still produce the same Host/request target;
- explicit non-default ports retain their identity;
- IPv4/IPv6 authority handling does not regress if already supported by the provider transport suite;
- unsafe absolute/authority-form provider-relative targets remain rejected by Eggpool before dispatch;
- custom Eggress dial targets still receive the logical destination host/effective port and domain names remain un-resolved locally.

Do not add IDNA conversion inside Eggpool. The provider configuration is already parsed into `http::Uri`; 0.1.7's native contract makes that URI authoritative.

## Custom Eggress dialer compatibility

The custom dialer is the reason Eggpool needs `native-http1` rather than the lean `standard-http1` profile.

Verify both compile-time and runtime behavior:

1. direct clients build and send without a dialer;
2. proxied account clients build with `ClientBuilder::dialer(...)`;
3. custom-dialer failures stay fail-closed;
4. no Eggfetch built-in `proxy` feature is enabled;
5. origin TLS still occurs in Eggfetch after Eggress returns the route stream;
6. account-specific clients still own distinct Eggfetch clients/pools.

Do not move proxy protocol knowledge into Eggfetch.

Do not combine this work with the separate Eggress version/facade cleanup. Eggpool remains pinned to its currently reviewed Eggress version until that line is adopted independently.

## Error-boundary compatibility

Keep `TransportError` stable.

The current `map_eggfetch_error()` is intentionally broad enough to recognize high-level Eggfetch error variants defensively even when those capabilities are not enabled. 0.1.7 keeps the error enum compatible, so no policy rewrite is expected.

If the 0.1.7 package exposes a compile difference:

- prefer typed matching;
- preserve existing precedence (physical admission -> established I/O -> request phase timeout -> TLS -> cancellation -> protocol -> target construction -> proxy/custom dialer -> direct connection -> phase fallback);
- do not introduce message-string parsing;
- do not leak `eggfetch_core::Error` above `providers::transport`.

The `DialErrorKind` mapping remains:

```text
Timeout        -> ProxyConnectTimeout
Authentication -> ProxyAuthentication
Rejected       -> ProxyTargetConnect
Connection     -> ProxyConnect
Other          -> ProxyConnect
```

Any behavioral reclassification requires a separately justified finding, not a dependency bump convenience.

## Dependency and feature audit

The most important acceptance proof is the resolved Eggpool graph, not Eggfetch's standalone fixture.

After the change run at least:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo deny --manifest-path rust/Cargo.toml check
```

Also inspect inverse ownership where available:

```bash
cargo tree --manifest-path rust/Cargo.toml -i eggfetch-core
cargo tree --manifest-path rust/Cargo.toml -i url
cargo tree --manifest-path rust/Cargo.toml -i idna
cargo tree --manifest-path rust/Cargo.toml -i dashmap
```

If an inverse query reports no package, record that as the expected removal.

For the Eggfetch edge specifically prove:

### Required enabled features

- `native-http1`
- `transport-http1`
- `standard-route`
- `advanced-routing`
- `tls-rustls`

### Required absent Eggfetch features

- `http1` compatibility bundle as a directly selected feature
- `high-level-url`
- `logical-retry`
- `redirects`
- `basic-auth`
- `proxy`
- `http2` / `native-http2` / `transport-http2`
- `http3`
- compression features
- `tls-native-roots`
- `json`
- `cookies`
- `multipart`

Because feature unification is workspace-wide, inspect the final graph rather than assuming the manifest declaration is sufficient.

### Expected package movement

The upstream native profile no longer requires Eggfetch's prior:

- `url -> idna -> ICU` closure;
- `dashmap` closure.

Do not state that every individual transitive crate disappears from the entire Eggpool graph until `cargo tree -i` proves it. Eggress, platform crates, or another Eggpool dependency may independently own some of the same packages.

Likewise, Eggpool directly owns `getrandom`, and other dependencies may own Base64/hash-map/platform crates. Distinguish "removed from the Eggfetch edge" from "removed from the entire resolved graph."

## Footprint measurement

Footprint recovery is a primary reason for this adoption, but the result must be measured.

### A. Immediate adoption delta

Compare:

- before: Eggpool `2f0e07e3e5d24771f37cdf144a34ae6ba8ca7a7f` with Eggfetch 0.1.5/`http1,tls-rustls`;
- after: the 0.1.7/`native-http1,tls-rustls` implementation commit.

Use identical:

- Rust toolchain;
- target triple;
- release profile;
- LTO/codegen settings;
- strip setting;
- environment class.

Record:

| Measurement | 0.1.5 current baseline | 0.1.7 native profile | Delta |
|---|---:|---:|---:|
| final release artifact bytes | | | |
| resolved packages | | | |
| direct dependencies | | | |
| Eggfetch feature profile | `http1,tls-rustls` | `native-http1,tls-rustls` | |
| Eggfetch-owned URL/IDNA/ICU closure | present | | |
| Eggfetch-owned DashMap closure | present | | |

### B. End-to-end migration context

If the same historical build environment is reproducible, compare the final 0.1.7 candidate with the original pre-Eggfetch baseline `7cc0521c` used by plan 219.

This determines how much of the original measured +2,230,912 byte (+8.0%) migration delta has been recovered by the upstream general-purpose footprint work.

Do not combine incomparable target/toolchain/strip settings. If the historical comparison cannot be reproduced, keep the old plan-219 table and add only the new current->candidate measurement.

No fixed byte threshold is required. A failure to remove the expected Eggfetch feature/dependency closure is a blocker; a smaller-than-expected binary change is not automatically a blocker if the graph is correct and linker behavior explains it.

## Provider transport qualification

Run the existing acceptance suite unchanged first.

Required focused runs:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
```

Preserve coverage for:

- direct HTTP request shape;
- HTTP/1.1 only;
- keep-alive reuse;
- idle expiry;
- no redirect following;
- no hidden logical retry;
- incremental/chunked bodies;
- premature body close;
- physical connection cap/admission timeout;
- cancellation and permit recovery;
- direct connection refusal/connect timeout;
- established read/write inactivity timeout;
- explicit additional CA roots and hostname validation;
- separate provider/account pool identity;
- HTTP CONNECT;
- SOCKS4/SOCKS5;
- Shadowsocks/SSR;
- Trojan;
- SSH under the existing Eggress compatibility feature;
- multi-hop chains;
- proxy authentication and target rejection;
- fail-closed proxy routes;
- recovery with a successful request after failures/cancellation.

Do not weaken or delete a test because Eggfetch internals changed.

### Coordinator behavior

Re-run the coordinator/provider tests used by plan 219 to prove that one Eggpool attempt is still one upstream attempt and transport categories still drive the same failure effects.

At minimum include the current equivalents of:

- `c008`
- `c009`
- `c011`
- `boundaries`
- `finalization`
- `publication`

Use the repository's actual current test names if they have changed.

## Full repository validation

Run the repository's current policy gates, not stale command assumptions copied from this plan.

At minimum:

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
```

Also run the Python/tooling checks required by the current repository workflow if this commit is intended to be a normal mainline handoff.

Do not expand permanent CI solely for this version adoption.

## Runtime smoke

Repeat a bounded smoke representative of the plan-219 closure:

- boot the release build with a valid zero-account/degraded configuration;
- verify `/v1/readyz` and `/api/status`;
- exercise one direct provider fixture if practical;
- exercise one proxied fixture if practical;
- stop cleanly;
- record idle RSS only if measured under a comparable environment.

The smoke should mainly detect lifecycle/task regressions. Do not turn this adoption into a new performance framework.

## Documentation updates

Update `architecture/deep-dive-providers.md` so it no longer states that provider transport is pinned to 0.1.5 or that Eggfetch necessarily brings `url/idna/ICU + dashmap`.

Document:

- exact 0.1.7 pin;
- why `native-http1` is selected rather than `http1` or `standard-http1`;
- the absent high-level policy features;
- current measured dependency/artifact delta;
- that the native URI path no longer requires `url::Url`;
- that Eggpool still does not configure Eggfetch `Timeout.total`;
- that updater HTTP remains a separate owner for now.

Keep the original plan-219 measurement as historical evidence; do not rewrite history. Add a clearly dated/new subsection for the 0.1.7 adoption result.

Update stale version comments in `rust/src/providers/transport.rs`.

## Explicit non-goals

Do not combine this plan with:

- upgrading Eggress or consuming Eggress's typed detailed connect-error API;
- deleting the current Eggress SSH compatibility path;
- moving `operations/update.rs` onto Eggfetch;
- enabling Eggfetch built-in proxy support;
- adding Eggfetch logical retries or redirects;
- adding a provider-wide total timeout;
- HTTP/2 or HTTP/3 enablement;
- provider routing/coordinator redesign;
- changing public `TransportError` variants;
- deleting direct Hyper/Rustls dependencies that still have live updater/test/error-boundary owners;
- a general dependency-upgrade sweep.

### Why updater migration is separate

`operations/update.rs` currently owns bounded metadata/artifact downloads, trusted redirect policy, artifact size validation, integrity verification, and an independent overall deadline. Eggfetch 0.1.7 offers leaner standard/native profiles, but replacing this updater client would change security-policy ownership rather than merely adopt a provider dependency. Evaluate that separately after this adoption establishes the new Eggfetch footprint baseline.

## Stop conditions

Stop and document rather than forcing the change if:

1. `native-http1,tls-rustls` cannot expose the current custom Dialer/native body/lifecycle APIs from the published 0.1.7 crate;
2. the resolved graph unexpectedly activates `high-level-url`, retries, redirects, Basic auth, built-in proxy, H2/H3, native roots, or compression and the owner cannot be explained;
3. direct or proxied provider tests show changed attempt counts;
4. physical admission no longer bounds live connections including idle pooled sockets;
5. established I/O inactivity changes into a request-level read/write/total timeout;
6. a failed Eggress dial can reach the direct route;
7. origin TLS or trust-root behavior changes;
8. provider/account client pool isolation changes;
9. the 0.1.7 dependency cannot be resolved from crates.io without a git/path override.

Do not work around these by re-enabling `http1` wholesale. Determine the specific missing 0.1.7 capability first.

## Implementation sequence

### 1. Freeze baseline evidence

Before editing, record:

- Eggpool HEAD;
- current `eggfetch-core =0.1.5` declaration;
- current resolved package count;
- current Eggfetch feature activation;
- current release artifact bytes under the chosen measurement environment.

Reuse plan-219 values only when the build settings are genuinely identical.

### 2. Change the exact dependency and feature alias together

In one manifest change:

- `=0.1.5 -> =0.1.7`;
- `http1 -> native-http1`;
- retain `tls-rustls`;
- retain `default-features = false`.

Regenerate `rust/Cargo.lock` from crates.io.

Do not land an intermediate `0.1.7 + http1` state.

### 3. Compile before source adaptation

Run `cargo check` on the provider/normal feature profile before editing `transport.rs`.

If it compiles, restrict production changes to version comments/documentation.

If it fails, make only the narrow API compatibility changes required by 0.1.7 while preserving the established semantics in this plan.

### 4. Audit the resolved graph

Prove the exact Eggfetch features and dependency owners.

Confirm the native profile excludes the former URL/IDNA/ICU and DashMap edges unless another independent Eggpool dependency owns them.

### 5. Run provider/coordinator qualification

Run the normal and `test-support` provider suites plus the coordinator attempt/error-effect tests.

### 6. Run full repository gates

Run Rust, dependency-policy, and repository tooling checks.

### 7. Measure footprint and update architecture evidence

Capture current->candidate and, where reproducible, original-pre-migration->candidate measurements.

Update `architecture/deep-dive-providers.md` without deleting the historical 0.1.5 record.

### 8. Record closure

Append to this plan:

- implementation commit SHA;
- actual resolved Eggfetch features;
- inverse dependency results for `url`, `idna`, ICU family, and `dashmap`;
- provider suite counts;
- coordinator suite results;
- full validation results;
- before/after resolved package counts;
- before/after artifact bytes;
- runtime smoke result;
- any remaining direct dependency owners.

## Completion criteria

This plan is complete when:

- [ ] Eggpool exact-pins published `eggfetch-core =0.1.7`.
- [ ] Eggpool selects `native-http1,tls-rustls`, not the 0.1.7 `http1` compatibility bundle.
- [ ] `default-features = false` remains set.
- [ ] Eggfetch built-in proxy remains disabled.
- [ ] Custom Eggress dialing still compiles through `advanced-routing`.
- [ ] Direct provider transport still uses the standard route.
- [ ] No Eggfetch high-level retry or redirect policy is enabled.
- [ ] No provider-wide `Timeout.total` is introduced.
- [ ] Physical connection admission semantics are unchanged.
- [ ] Established read/write inactivity semantics are unchanged.
- [ ] Origin TLS/WebPKI/additional-root behavior is unchanged.
- [ ] Provider/account pool isolation is unchanged.
- [ ] Proxy failure remains fail-closed.
- [ ] Existing `TransportError` classifications remain stable.
- [ ] Provider transport suites pass in normal and test-support profiles.
- [ ] Coordinator attempt/error-effect qualification passes.
- [ ] Full repository validation passes.
- [ ] `cargo deny` passes.
- [ ] Resolved Eggfetch feature graph is recorded.
- [ ] URL/IDNA/ICU and DashMap ownership/removal is recorded from the actual Eggpool graph.
- [ ] Release artifact and resolved-package deltas are measured under comparable conditions.
- [ ] `architecture/deep-dive-providers.md` reflects 0.1.7 while preserving the historical 0.1.5 measurement.
- [ ] No updater/Eggress/unrelated transport scope was pulled into this adoption.

## Expected end state

The desired end state is the same provider transport architecture that already passed plans 215-219, but against the published Eggfetch version that now matches Eggpool's actual native use case.

The implementation should be materially simpler than the original migration:

```text
Eggpool provider/account policy
        |
        v
eggfetch-core 0.1.7
  native-http1 + tls-rustls
  - no high-level URL layer
  - no logical retry
  - no redirects
  - no Basic auth
  - no built-in proxy
        |
        +--> direct standard TCP/TLS route
        |
        +--> custom Eggress Dialer -> Eggress route -> origin TLS in Eggfetch
```

Success is behavioral parity plus removal of the avoidable Eggfetch dependency closure. Any binary-size recovery is measured and recorded rather than assumed.
