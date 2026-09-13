# Plan 190: Eggress optional-feature corrective closure

> **Status:** Ready
>
> **Parent:** Corrective follow-on to completed Plans 186–189
>
> **Scope:** Make the documented `eggress-ssh-fallback` feature genuinely optional, make `--no-default-features` a supported compile configuration, fail closed for SSH when the fallback is absent, and add a small CI guard so this feature-boundary regression cannot silently return.

## Problem statement

Plans 186–189 successfully moved ordinary provider proxy construction behind `eggress-embed`, retained a narrowly named native fallback for the known Eggress 1.0.6 SSH facade gap, preserved the deterministic test-root seam, and reduced the measured default release binary by roughly 1.75%.

The resulting Cargo feature declaration says the native SSH fallback is optional:

```toml
[features]
default = ["eggress-ssh-fallback"]
test-support = ["eggress-ssh-fallback"]
eggress-ssh-fallback = [
    "dep:eggress-core",
    "dep:eggress-config",
    "dep:eggress-pproxy-compat",
    "dep:eggress-server",
    "dep:eggress-uri",
    "dep:eggress-transport-ssh",
]
```

However, `rust/src/providers/transport.rs` currently assumes the feature is present inside `ProviderTcpConnector::new`:

- the SSH match guard calls `proxy_uses_ssh(proxy_url)`;
- the matching arm calls `build_chain_egress_dialer(proxy_url, None)`;
- both helpers are compiled only under `#[cfg(feature = "eggress-ssh-fallback")]`.

Consequently, the source/manifest contract is inconsistent. A default build succeeds because the fallback is enabled by default, while a `--no-default-features` build is expected to fail name resolution before it can exercise the intended reduced feature configuration.

The existing CI does not cover this configuration. It runs fmt, default-feature Clippy, and default-feature workspace tests, so the mismatch can regress without detection.

This plan is deliberately narrow. It does **not** reopen the Eggress consolidation, remove SSH support from the default product, weaken TLS verification, or redesign the provider transport.

## Root cause

The implementation conflates two independent questions:

1. Does the configured pproxy expression contain an SSH hop?
2. Is Eggpool compiled with the native compatibility path required to execute SSH correctly against Eggress 1.0.6?

The first question is feature-independent string/configuration classification and must always compile.

The second question is feature-dependent. When `eggress-ssh-fallback` is enabled, Eggpool should construct the native SSH-capable chain exactly as it does today. When the feature is disabled, Eggpool must reject an SSH-containing proxy configuration deterministically and must **not**:

- fall back to a direct connection;
- send the SSH expression through the known-broken Eggress 1.0.6 embed SSH path;
- make the native implementation crates unconditional again;
- silently ignore the SSH hop;
- weaken host-key or TLS verification.

## Desired end state

After this corrective pass:

1. `cargo check --manifest-path rust/Cargo.toml --all-targets --no-default-features` succeeds.
2. Default builds retain the current SSH behavior through `eggress-ssh-fallback`.
3. `test-support` continues to imply `eggress-ssh-fallback`, preserving the verified custom-root fixture path.
4. No-default builds retain direct and non-SSH Eggress proxy functionality.
5. An SSH-containing proxy expression in a no-default build fails during connector construction with the existing configuration-error contract, before any network connection is attempted.
6. An unavailable SSH fallback can never degrade into direct egress.
7. Optional native Eggress dependencies remain feature-gated rather than becoming unconditional.
8. The `eggress-embed` SSH feature is activated only when Eggpool actually exposes the SSH path, if the current Eggress feature graph permits that separation cleanly.
9. CI includes a low-cost no-default compilation/lint gate.
10. Plans 186–189 remain complete and unchanged except for any strictly necessary cross-reference note; Plan 190 carries its own closure evidence.

## Workstream A — make SSH detection feature-independent

### A1. Keep configuration classification always available

`proxy_uses_ssh` performs only pproxy-expression inspection and does not require any optional Eggress implementation crate. Remove its `#[cfg(feature = "eggress-ssh-fallback")]` guard.

Do not replace this with a dependency-heavy parser merely to answer whether an SSH protocol token is present. The existing conservative scheme-token check is appropriate as long as the provider transport's accepted pproxy syntax remains unchanged.

Qualification should cover at least:

- `ssh://...`;
- a multi-hop expression with SSH first;
- a multi-hop expression with SSH later;
- combined protocol schemes where `ssh` is one protocol token;
- ordinary HTTP/SOCKS/Shadowsocks/Trojan expressions returning false;
- strings containing the substring `ssh` outside a protocol token not being misclassified if such a case is representable by the accepted syntax.

Reuse existing classification tests if present; add only the minimum missing regression cases.

### A2. Centralize feature-dependent SSH construction

Do not scatter `#[cfg]` branches through `ProviderTcpConnector::new`.

Prefer a small helper with two compile-time implementations, conceptually:

```rust
#[cfg(feature = "eggress-ssh-fallback")]
fn build_ssh_proxy_dialer(
    proxy_url: &str,
) -> Result<Arc<dyn ProxyDialer>, TransportError> {
    // Existing native compatibility path.
}

#[cfg(not(feature = "eggress-ssh-fallback"))]
fn build_ssh_proxy_dialer(
    _proxy_url: &str,
) -> Result<Arc<dyn ProxyDialer>, TransportError> {
    Err(TransportError::ProxyConfiguration)
}
```

The exact return type may differ to fit current ownership cleanly. The important properties are:

- `ProviderTcpConnector::new` has one stable call site;
- all configurations compile;
- the enabled implementation delegates to the existing `build_chain_egress_dialer` path;
- the disabled implementation has no references to optional Eggress crates;
- the disabled implementation fails before dialing.

Avoid adding a new public transport error variant unless the existing `ProxyConfiguration` category cannot accurately express the condition. The current category is sufficient for an intentionally unavailable configured transport capability.

### A3. Preserve explicit direct behavior

Do not change the existing `direct://` special case. It must continue to:

1. validate the control expression through Eggress;
2. use Eggpool's direct Hyper connector intentionally;
3. remain distinguishable from any proxy failure;
4. never become the fallback path for unavailable SSH support.

## Workstream B — make Cargo feature wiring semantically honest

### B1. Keep native implementation dependencies optional

The following dependencies must remain optional and activated only through the native fallback/test-support relationship:

- `eggress-core`;
- `eggress-config`;
- `eggress-pproxy-compat`;
- `eggress-server`;
- `eggress-uri`;
- `eggress-transport-ssh`.

Do not solve the compile failure by removing `optional = true`, by adding these crates to unconditional dependencies, or by expanding `eggress-embed` default features.

### B2. Gate the embed `ssh` feature with Eggpool's SSH capability when practical

The current `eggress-embed` dependency enables `ssh` unconditionally even when Eggpool is compiled with `--no-default-features`:

```toml
eggress-embed = { version = "=1.0.6", default-features = false, features = [
    "pproxy-compat",
    "pproxy-legacy",
    "legacy-crypto",
    "ssh",
] }
```

Because Eggress 1.0.6's embed SSH constructor is the facade gap that necessitated the native fallback, enabling its SSH feature in a build that intentionally omits Eggpool's SSH fallback is misleading and may retain unnecessary transitive code.

If Cargo feature resolution permits it without changing non-SSH behavior, move activation to the root feature:

```toml
eggress-embed = { version = "=1.0.6", default-features = false, features = [
    "pproxy-compat",
    "pproxy-legacy",
    "legacy-crypto",
] }

eggress-ssh-fallback = [
    "eggress-embed/ssh",
    # existing optional native dependencies...
]
```

This is preferred because:

- default builds remain unchanged;
- `test-support` still implies the same complete SSH capability;
- no-default builds do not advertise or link a known-unusable facade SSH path unnecessarily;
- the feature graph better describes product capability.

Treat this as a graph-qualified cleanup, not a mandatory rewrite. If Eggress 1.0.6 unexpectedly requires its `ssh` feature to parse or operate a non-SSH pproxy mode that Eggpool supports, retain the existing embed activation and document the reason. Do not regress working protocols for theoretical graph purity.

### B3. Do not split `test-support` in this pass

Today:

```toml
test-support = ["eggress-ssh-fallback"]
```

The custom proxy-TLS test-root path and production SSH workaround share the same native chain-construction seam. That coupling is acceptable while Eggress 1.0.6 requires the native executor.

Do not create a second nearly identical set of optional feature dependencies solely to make the names more granular. When a future Eggress release fixes the embed SSH session-cache gap, removal of the production fallback can reassess the minimum native subset still required by `test-support`.

## Workstream C — lock fail-closed behavior in tests

### C1. Add a no-default SSH rejection regression

Add the smallest test that proves an SSH-containing proxy cannot be constructed when `eggress-ssh-fallback` is disabled.

The test should be compiled only when the fallback is absent, for example with:

```rust
#[cfg(not(feature = "eggress-ssh-fallback"))]
```

It should:

1. build a normal `ProviderHttpConfig`;
2. call `ProviderHttpClient::new_with_proxy` with a syntactically representative SSH pproxy URI;
3. assert `TransportError::ProxyConfiguration`;
4. require no live proxy fixture and make no external connection.

If testing `ProviderTcpConnector` directly would require exposing private internals, test through the existing public `ProviderHttpClient` constructor instead. Do not broaden production visibility for the sake of this test.

### C2. Preserve the default SSH interoperability tests

The existing live SSH success/authentication/cancellation qualification remains mandatory with default/test-support features. It proves the corrective compile changes did not accidentally route SSH into the broken facade path.

Preserve the existing assertions around:

- successful SSH proxy traversal;
- authentication failure classification;
- timeout/cancellation behavior;
- no direct bypass after proxy failure.

### C3. Prove non-SSH no-default construction still works

Add or identify a feature-neutral regression that constructs at least one non-SSH proxy path under no-default features. This can be a configuration/construction test unless an existing inexpensive live fixture already covers the path.

The purpose is to prevent an implementation that simply rejects all proxies whenever the fallback feature is disabled.

Prefer an HTTP CONNECT or SOCKS5 representative path because those are simple and unambiguous.

## Workstream D — add a CI feature-boundary guard

The current `.github/workflows/ci.yml` has one qualification job and does not exercise `--no-default-features`.

Keep CI proportional to this local/SBC project. Do not add a feature-power-set matrix.

Add two inexpensive commands to the existing Rust qualification job:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

Place them near the existing default Clippy/test steps.

A second full no-default workspace test run is optional rather than required in CI if it materially increases runtime. The essential CI contract is that the reduced feature surface compiles and lints. Behavioral no-default regressions should be covered by a targeted local/normal test invocation as described below.

Do not add new CI jobs, operating-system matrices, services, or proxy daemons for this correction.

## Workstream E — qualification matrix

Run the following from the repository root after implementation.

### E1. Formatting

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
```

### E2. Reduced feature surface

Mandatory:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features
```

If the complete no-default test command includes existing integration tests whose semantics explicitly require the default SSH capability, do not weaken or delete those tests merely to make the command green. Instead:

1. gate only the genuinely SSH-capability-specific cases on `eggress-ssh-fallback`;
2. keep feature-neutral tests running;
3. record the exact resulting no-default command and counts in closure evidence.

The preferred end state is that the complete no-default test suite compiles and passes.

### E3. Default and full qualification

Mandatory:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --all-targets --all-features
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-features -- --test-threads=1
```

Preserve the existing provider transport count/coverage rather than replacing the real interoperability suite with unit tests.

### E4. Dependency feature graph

Capture:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml -e features
```

Confirm:

- default resolution still activates `eggress-ssh-fallback` and the native SSH dependencies;
- no-default resolution does not activate those optional dependencies **as root fallback edges**;
- any same crates still present transitively through `eggress-embed` are identified accurately rather than misreported as a failed optional-dependency cleanup;
- if Workstream B2 succeeds, `eggress-embed/ssh` is absent from no-default resolution and present in default resolution.

### E5. Policy and repository hygiene

```bash
cargo deny --manifest-path rust/Cargo.toml check
git diff --check
```

A new default-release binary-size comparison is not required for this corrective pass because default behavior and default feature activation should remain equivalent. If the default release size changes materially, investigate before closure and record the reason.

## Workstream F — documentation and closure evidence

Update only documentation that currently claims or implies that the fallback is optional without explaining disabled-feature behavior.

The architecture description should state succinctly:

- Eggress 1.0.6 embed handles ordinary proxy construction;
- the default `eggress-ssh-fallback` preserves supported SSH because 1.0.6 omits the required SSH session cache in its embed executor;
- builds that deliberately disable the fallback reject SSH proxy configurations fail-closed;
- this is a temporary compatibility boundary pending an upstream Eggress facade fix.

Do not rewrite the completed Plans 186–189. Their closure evidence describes the state that was actually qualified at the time. Add closure evidence to Plan 190 instead.

Closure evidence must record:

- exact implementation commit;
- exact Cargo feature wiring after the fix;
- the no-default check/lint/test results;
- the default provider transport result;
- the all-features result;
- `cargo deny` result;
- whether `eggress-embed/ssh` was successfully moved under the fallback feature;
- confirmation that SSH without the fallback returns configuration failure before dialing;
- confirmation that direct/non-SSH proxy construction remains available without defaults.

## Files expected to change

Primary:

- `rust/src/providers/transport.rs`
- `rust/Cargo.toml`
- `.github/workflows/ci.yml`

Likely test changes:

- `rust/tests/provider_transport.rs`, if the no-default regression belongs naturally in the integration transport suite; or
- a focused unit-test module in `rust/src/providers/transport.rs` if that avoids exposing private internals and better isolates compile-feature behavior.

Documentation only if needed for accuracy:

- `architecture/deep-dive-providers.md`
- this plan's closure-evidence section.

Do not modify unrelated routing, provider adapters, retry state, database code, updater code, or dashboard/server code.

## Non-goals

Do **not** use this plan to:

- remove the default SSH fallback before Eggress exposes a working embed SSH executor/session cache;
- add a new Eggress abstraction or fork Eggress behavior inside Eggpool;
- move provider HTTP/TLS ownership into Eggress;
- redesign `ProviderHttpClient`;
- replace Hyper/Rustls with Eggfetch;
- split `test-support` into new micro-features without an observed need;
- broaden the CI matrix beyond the no-default boundary being corrected;
- add insecure TLS/SSH verification modes;
- remove legacy Shadowsocks/SSR features;
- re-open the already completed footprint work;
- chase Cargo.lock package count as a success metric.

## Implementation order

1. Reproduce the current no-default compile failure and preserve its diagnostic in implementation notes.
2. Make SSH-expression classification feature-independent.
3. Add the cfg-paired SSH dialer-construction helper and fail-closed disabled implementation.
4. Run no-default `cargo check` immediately.
5. Move `eggress-embed/ssh` under `eggress-ssh-fallback` if the feature graph and non-SSH qualification permit it cleanly.
6. Add the no-default SSH rejection and non-SSH construction regressions.
7. Run the reduced/default/all-features qualification matrix.
8. Add the two no-default CI commands.
9. Re-run fmt, Clippy, tests, `cargo deny`, and `git diff --check` from the final tree.
10. Append closure evidence to Plan 190 and mark only Plan 190 complete.

This order keeps failures attributable: first fix source cfg correctness, then improve feature wiring, then lock the boundary in tests and CI.

## Approval checklist

- [ ] Current `--no-default-features` failure reproduced before implementation.
- [ ] `proxy_uses_ssh` or equivalent classification compiles independent of the fallback feature.
- [ ] Default SSH continues to use the native 1.0.6 compatibility fallback.
- [ ] No-default SSH configuration fails as `ProxyConfiguration` before dialing.
- [ ] No SSH failure can fall back to direct egress.
- [ ] Direct and non-SSH proxy construction remain available with no default features.
- [ ] Optional native Eggress dependencies remain feature-gated.
- [ ] `eggress-embed/ssh` is feature-gated with Eggpool SSH support when safely possible, or the reason it remains unconditional is documented.
- [ ] `test-support` remains functional and verified without weakening certificate checks.
- [ ] CI checks no-default compile and Clippy without adding a broad feature matrix.
- [ ] No-default test qualification passes with capability-specific tests gated accurately.
- [ ] Default provider transport qualification passes.
- [ ] All-features Clippy and tests pass.
- [ ] `cargo deny` passes.
- [ ] Plans 186–189 remain closed.
- [ ] Closure evidence is appended to this plan with exact commit and command results.

## Completion criterion

Plan 190 is complete when the manifest truthfully describes `eggress-ssh-fallback` as optional: Eggpool builds cleanly without defaults, non-SSH transport remains usable, SSH is rejected explicitly and fail-closed when its required compatibility implementation is absent, default builds retain fully qualified SSH support, and CI permanently exercises the reduced feature boundary.