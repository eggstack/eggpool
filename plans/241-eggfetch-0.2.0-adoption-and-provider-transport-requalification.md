# Eggfetch 0.2.0 Adoption and Provider-Transport Requalification

Status: complete

Planning baseline: `f6799dcd50b790724257d37b99a1f40f91d87428` (`main`, Eggpool 0.8.0)

Upstream release baseline:
- published crate: `eggfetch-core =0.2.0`
- Eggfetch release tag: `v0.2.0`
- tagged release commit: `8959ca890ee34f4cf456aed648315322f1e83ef7`
- current Eggpool pin: exact `eggfetch-core =0.1.7`
- Eggfetch release CI at the tagged commit: main CI green

Related completed Eggpool work:
- `215-eggfetch-transport-consolidation-roadmap.md`
- `216-phase-1-eggfetch-direct-transport-foundation.md`
- `217-phase-2-eggfetch-egress-dialer-integration.md`
- `218-phase-3-eggfetch-transport-cutover-and-cleanup.md`
- `219-phase-4-eggfetch-qualification-and-footprint-closure.md`
- `220-eggfetch-0.1.7-adoption.md`

This plan is independent of the SQLite/SBC diagnostic line in plans 237-240. Do
not mix the two changes or use this dependency bump to alter SQLite/runtime
policy.

## Objective

Adopt the published `eggfetch-core 0.2.0` release in Eggpool while preserving
the provider transport architecture and the exact native-only feature profile
already qualified under 0.1.7.

The intended production dependency remains:

```toml
eggfetch-core = { version = "=0.2.0", default-features = false, features = [
    "native-http1",
    "tls-rustls",
] }
```

This is a narrow dependency requalification pass. It must not become another
provider-transport migration, a feature expansion, or an excuse to change
coordinator retry/failure semantics.

The desired end state is:

1. Eggpool resolves the crates.io `eggfetch-core 0.2.0` package exactly.
2. The existing `native-http1 + tls-rustls` integration compiles without
   behavior changes.
3. Eggpool still selects only the transport/routing/TLS capabilities it owns
   today.
4. Provider transport, cancellation, proxy fail-closed behavior, timeout/error
   classification, and pool ownership remain unchanged.
5. Current architecture/development documentation names 0.2.0 rather than
   presenting 0.1.7 as the live dependency.
6. The resolved graph and release artifact show no unexplained regression.

## Upstream findings that govern this plan

Eggfetch 0.2.0 is intentionally API-preserving relative to 0.1.7. Its release
notes state that the public Rust/Python/C/CLI/HTTPX APIs, feature graph,
defaults, MSRV (Rust 1.89), and dependency policy are unchanged from 0.1.7.

The `eggfetch-core/src/lib.rs` public root at tags `v0.1.7` and `v0.2.0`
has the same blob content. In particular, Eggpool's low-level native/custom
routing surface remains exported under the same feature gates:

- `Client` / `ClientBuilder`
- `Client::execute_http_body()`
- `NativeRequestOptions`
- `NativeResponseBody`
- `HttpVersionPolicy`
- `Dialer`, `DialTarget`, `DialFuture`, `DialStream`
- `DialError` / `DialErrorKind`
- `PhysicalConnectionPolicy`
- `TransportIoTimeout` / `TransportIoDirection`
- `TlsConfig` / `TrustStore`
- `Timeout` / `TimeoutPhase`
- the typed `eggfetch_core::Error` variants and helper methods consumed by
  `rust/src/providers/transport.rs`

The current feature graph also remains:

```text
native-http1
  -> transport-http1
  -> standard-route
  -> advanced-routing
```

`advanced-routing` remains required because Eggpool's proxied provider clients
install the custom Eggress `Dialer`.

Do not replace `native-http1` with either:

- `http1`: the compatibility/high-level bundle that additionally enables
  `high-level-url`, logical retry, redirects, and Basic auth; or
- `standard-http1`: the lean high-level recipe that omits
  `advanced-routing` and therefore cannot provide Eggpool's custom dialer
  boundary.

## Issue #24 / decompression finding

Eggfetch 0.2.0 includes the previously prepared streaming-decompression
chunk-boundary correction for issue #24. The fix replaces the private
stream-to-`AsyncRead` adapter with `tokio_util::io::StreamReader` for the
compression path and corrects fragmented gzip/Brotli/deflate/zstd decoding.

That fix is **not an active Eggpool provider-transport bug** under the current
feature selection.

Eggpool currently enables only:

- `native-http1`
- `tls-rustls`

It does not enable:

- `compression-gzip`
- `compression-brotli`
- `compression-deflate`
- `compression-zstd`

The provider path uses the frame-preserving
`Client::execute_http_body()`/`NativeResponseBody` surface. Therefore this
adoption must not enable decompression, inject `Accept-Encoding`, or otherwise
change upstream response semantics merely because 0.2.0 contains the fix.

Record the issue #24 fix as an upstream correctness improvement that remains
dormant for Eggpool's current feature profile.

## Architecture invariants

Preserve the ownership boundary established by plans 215-220:

- Eggpool owns provider/account routing, coordinator retry/failover,
  request shaping, body bounds, stable `TransportError`, account-specific
  client identity, and failure effects.
- Eggfetch owns HTTP/1.1 framing, destination/origin TLS, Hyper connection
  pooling, physical connection admission, established transport I/O guards,
  and native response-body lifecycle.
- Eggress owns proxy/route establishment and route-level TLS.
- proxied accounts provide an Eggress-backed custom `Dialer` to Eggfetch;
  Eggfetch's built-in proxy feature remains disabled.
- a configured proxy route remains fail-closed and may never silently fall
  back to direct networking.
- one Eggpool coordinator attempt remains one upstream transport attempt;
  Eggfetch logical retry remains disabled.
- account clients retain distinct Eggfetch client/pool instances.
- HTTP/1.1 remains the only provider transport protocol.
- origin TLS remains inside Eggfetch after the Eggress route returns its byte
  stream.
- no Eggfetch error type escapes above `providers::transport`.

Do not modify these invariants unless compilation or qualification exposes a
concrete incompatibility. If that occurs, stop treating the work as a routine
dependency bump and document the finding before widening scope.

## Expected implementation files

The normal implementation should be small.

Required:
- `rust/Cargo.toml`
- `rust/Cargo.lock`

Expected current-authority documentation/comment updates:
- `README.md`
- `architecture/overview.md`
- `architecture/deep-dive-providers.md`
- `.opencode/skills/architecture/SKILL.md`
- `.opencode/skills/development/SKILL.md`
- `rust/src/providers/transport.rs` only for stale version-specific comments

Historical plans 215-220 are evidence and must not be rewritten to pretend they
originally targeted 0.2.0.

Before editing documentation, search the current tree for live 0.1.7
references, for example:

```bash
rg -n 'eggfetch-core.*0\.1\.7|Eggfetch 0\.1\.7|eggfetch.*0\.1\.7|0\.1\.7 native' \
  README.md architecture .opencode rust/src AGENTS.md
```

Update only references that describe the current dependency or current
development rule. Leave historical measurements and completed-plan evidence
clearly historical.

No production source change beyond comments is expected. If
`rust/src/providers/transport.rs` requires executable changes to compile,
keep them minimal and justify each change against a public 0.2.0 API or
behavioral difference.

## Phase 1 — exact package adoption

Change only the exact Eggfetch pin:

```toml
# before
eggfetch-core = { version = "=0.1.7", default-features = false, features = [
    "native-http1",
    "tls-rustls",
] }

# after
eggfetch-core = { version = "=0.2.0", default-features = false, features = [
    "native-http1",
    "tls-rustls",
] }
```

Regenerate the lockfile narrowly. Prefer an exact package update rather than a
general dependency refresh:

```bash
cargo update --manifest-path rust/Cargo.toml -p eggfetch-core --precise 0.2.0
```

If Cargo also moves unrelated packages because the lockfile requires it,
inspect and record why. Do not accept an opportunistic general dependency
upgrade in this plan.

Confirm `rust/Cargo.lock` resolves `eggfetch-core 0.2.0` from the crates.io
registry rather than a git/path/patch override.

Do not change Eggpool's Rust 1.89 MSRV; Eggfetch 0.2.0 retains the same floor.

## Phase 2 — compile the existing integration unchanged first

Before editing executable provider code, compile/test the current adapter
against 0.2.0.

Run the focused provider target first:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support \
  --test provider_transport -- --test-threads=1
```

If this compiles and passes, do not refactor the adapter.

If compilation fails, audit the exact failing symbol against the 0.2.0 public
surface. Any compatibility patch must preserve:

- `execute_http_body()` as the provider send path;
- `NativeRequestOptions`;
- frame-preserving `NativeResponseBody`;
- custom `Dialer`;
- existing physical admission and established I/O timeout settings;
- WebPKI plus explicit additional test roots;
- current error translation precedence.

Do not switch to Eggfetch's high-level `RequestBuilder` API.

## Phase 3 — feature/dependency graph proof

Cargo is authoritative. Run:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggfetch-core
cargo deny --manifest-path rust/Cargo.toml check
```

The final Eggfetch edge must include:

- `native-http1`
- `transport-http1`
- `standard-route`
- `advanced-routing`
- `tls-rustls`

The final Eggfetch edge must not activate:

- high-level `http1` as the selected compatibility bundle
- `high-level-url`
- `logical-retry`
- `redirects`
- `basic-auth`
- built-in `proxy`
- `http2`, `native-http2`, `transport-http2`
- `http3`
- `compression-gzip`
- `compression-brotli`
- `compression-deflate`
- `compression-zstd`
- `tls-native-roots`
- `json`
- `cookies`
- `multipart`

Because 0.2.0 intentionally preserves the 0.1.7 feature/dependency policy, no
new Eggpool runtime package family is expected from this upgrade. If the
resolved graph gains a material dependency family, investigate it before
closing the plan.

Do not remove Eggpool's direct `hyper`, `hyper-util`, `hyper-rustls`,
`rustls`, or `webpki-roots` dependencies as part of this bump. The current
architecture documents live non-provider owners for them (updater,
typed-source inspection, and test-support). Dependency deletion requires a
separate owner audit.

## Phase 4 — provider transport behavioral qualification

The existing transport suite should remain the primary acceptance oracle.
Preserve coverage for at least:

- direct HTTP request shape;
- HTTP/1.1-only behavior;
- keep-alive reuse and idle expiry;
- no redirect following;
- no hidden logical retry;
- incremental/chunked raw body delivery;
- premature response close;
- finite body bounds;
- physical connection cap/admission timeout;
- cancellation while waiting for or using capacity;
- client/pool recovery after cancellation;
- direct connection refusal and connect timeout;
- established read inactivity timeout;
- established write inactivity timeout;
- explicit additional CA roots;
- hostname/certificate validation;
- separate provider/account pool identity;
- custom Eggress dialer target preservation;
- HTTP CONNECT, SOCKS, Shadowsocks/SSR, Trojan, SSH, and multi-hop cases
  already represented by the fixture suite;
- proxy authentication/target rejection classification;
- no direct fallback for configured proxy accounts;
- successful reuse after route failure.

Do not add a decompression-specific Eggpool test merely to exercise issue #24:
the corresponding features are intentionally absent. The feature-graph
assertion is the correct proof for that boundary.

### Stable error mapping

Keep the existing `TransportError` contract and the current typed mapping
order:

1. physical admission;
2. established transport I/O timeout;
3. phase-aware timeout;
4. TLS;
5. cancellation;
6. framing/protocol/body;
7. target/request construction;
8. built-in proxy variants defensively;
9. custom Eggress dial error kind;
10. direct connection;
11. caller-phase fallback.

The custom dial mapping remains:

```text
Timeout        -> ProxyConnectTimeout
Authentication -> ProxyAuthentication
Rejected       -> ProxyTargetConnect
Connection     -> ProxyConnect
Other          -> ProxyConnect
```

Do not introduce new string parsing in the Eggfetch error boundary. The
existing conservative message classification is confined to the separate
Eggress embed facade path and is not part of this upgrade.

## Phase 5 — coordinator/failure-semantics qualification

A provider dependency bump is not complete until the layer above transport
still observes the same attempt/failure semantics.

Run the current focused targets named by the repository development skill:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
```

Acceptance requires:

- one coordinator attempt still produces at most one transport submission;
- transport failures retain the same retry/failover legality;
- account/provider health effects do not change;
- failed/cancelled requests do not poison the provider pool;
- no restart or database refresh is required to recover from transport
  failures.

## Phase 6 — reduced-feature and full repository gates

The root `ssh` feature remains optional, so dependency adoption must qualify
both the default and reduced surfaces.

Run:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets \
  --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features

cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release

cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

For the reduced build, preserve the existing contract:

- direct provider transport works;
- non-SSH Eggress proxy routes remain available;
- SSH-containing proxy configuration is rejected as
  `TransportError::ProxyConfiguration`;
- no Eggpool-owned SSH fallback appears.

Do not add permanent CI jobs for this version bump.

## Phase 7 — release footprint sanity check

Because the selected 0.2.0 feature/dependency graph is expected to match the
0.1.7 profile, this upgrade should be approximately footprint-neutral.

Build the planning baseline and candidate under comparable conditions when
practical:

- same Rust toolchain;
- same target triple;
- same release profile;
- same default feature set;
- same strip/LTO/codegen settings.

Record at least:

| Measurement | 0.1.7 baseline | 0.2.0 candidate | Delta |
|---|---:|---:|---:|
| final release artifact bytes | | | |
| resolved package count | | | |
| Eggfetch selected features | `native-http1,tls-rustls` | | |
| unexpected Eggfetch feature families | none | | |

There is no fixed byte threshold. A small linker/codegen difference is not a
blocker. A material unexplained increase or new dependency family is a blocker
until understood.

Do not reopen the physical-SBC benchmark program for this dependency bump.
Plans 235-240 own that evidence line. Only run a new physical-SBC pass if
ordinary qualification exposes a transport/resource regression that cannot be
resolved on the normal fixture boundary.

## Phase 8 — current-authority documentation cleanup

Update current documentation to state the live dependency is
`eggfetch-core =0.2.0` with `native-http1,tls-rustls`.

At minimum inspect/update:

- `README.md`
- `architecture/overview.md`
- `architecture/deep-dive-providers.md`
- `.opencode/skills/architecture/SKILL.md`
- `.opencode/skills/development/SKILL.md`
- `rust/src/providers/transport.rs` version-specific module comments
- `AGENTS.md` if it carries the current pin

The provider deep dive should record a short dated 0.2.0 adoption subsection
that states:

- exact 0.2.0 pin;
- no intentional Eggfetch public Rust/feature/MSRV break from 0.1.7;
- the same native feature selection remains required;
- issue #24 is fixed upstream but compression remains disabled in Eggpool;
- no high-level URL/retry/redirect/auth/proxy/HTTP2/HTTP3 policy was enabled;
- the resolved graph/artifact delta observed during qualification;
- updater HTTP remains a separate owner;
- plans 215-220 remain the historical migration/adoption evidence.

Do not rewrite old plans or old dated measurements.

## Rollback rule

If 0.2.0 fails a provider/coordinator invariant and the failure cannot be fixed
with a narrow API-compatibility adjustment, revert:

- the exact manifest pin;
- the lockfile movement;
- current-authority documentation changed only for the candidate.

Return to the exact reviewed `0.1.7` state and document the incompatibility in
a new corrective plan. Do not mask a regression by weakening tests, broadening
timeouts, changing error categories, or enabling a different Eggfetch feature
bundle.

## Explicit non-goals

This plan does not authorize:

- enabling response compression/decompression;
- adding `Accept-Encoding` behavior;
- using Eggfetch's high-level request builder;
- enabling logical retries or redirects;
- enabling Eggfetch built-in proxy support;
- enabling HTTP/2 or HTTP/3 provider transport;
- changing `TransportError`;
- changing provider/account routing or health policy;
- changing coordinator retry budgets;
- changing pool limits/timeouts;
- changing origin or route TLS ownership;
- upgrading Eggress;
- consuming a new Eggress typed-error facade;
- migrating `operations/update.rs` to Eggfetch;
- deleting direct Hyper/Rustls dependencies without a separate owner audit;
- a general Cargo dependency refresh;
- changing runtime concurrency or SQLite behavior;
- reopening the SBC/SQLite optimization line.

## Acceptance checklist

- [ ] `rust/Cargo.toml` exact-pins `eggfetch-core =0.2.0`.
- [ ] `rust/Cargo.lock` resolves the crates.io 0.2.0 package with no git/path override.
- [ ] The selected Eggfetch profile remains exactly native HTTP/1 + Rustls with advanced routing.
- [ ] High-level URL/retry/redirect/Basic-auth/built-in-proxy features remain absent.
- [ ] HTTP/2, HTTP/3, native roots, compression, JSON, cookies, and multipart remain absent.
- [ ] The existing provider adapter compiles without executable changes, or every required compatibility change is narrow and justified.
- [ ] Default provider transport qualification passes.
- [ ] `test-support` provider/proxy qualification passes.
- [ ] No-default-feature qualification passes.
- [ ] Coordinator C008/C009/C011, boundaries, finalization, publication, and wire-runtime targets pass.
- [ ] One Eggpool attempt remains one upstream submission.
- [ ] Cancellation/failure paths recover the same client without restart.
- [ ] Configured proxy routes remain fail-closed with no direct fallback.
- [ ] Timeout and `TransportError` classifications remain stable.
- [ ] `cargo deny` passes.
- [ ] Resolved feature/dependency graph has no unexplained new family.
- [ ] Locked release build passes.
- [ ] Any release-artifact delta is measured and explained.
- [ ] Current architecture/development docs and provider comments name 0.2.0.
- [ ] Historical plans 215-220 remain untouched except as referenced evidence.
- [ ] No compression behavior, provider protocol, routing, SQLite, or SBC-policy change is bundled into the adoption.

## Completion evidence

When implementation lands, update this plan's status to `complete` and append a
short completion note containing:

- implementation commit SHA;
- final `eggfetch-core` lockfile version/source;
- focused provider test counts;
- focused coordinator test results;
- no-default-feature result;
- serial workspace result;
- `cargo deny` result;
- selected/absent Eggfetch feature proof;
- release artifact/package-count comparison if measured;
- list of current-authority documentation files updated;
- any deviations from the expected source-change set.

Do not claim closure from compilation alone. The dependency change is closed
only when the provider behavior and resolved feature graph are requalified.

## Completion note — 2026-09-22

- Implementation commit: `551fbd9e` (`Adopt eggfetch-core 0.2.0 and requalify
  provider transport`).
- `rust/Cargo.toml` exact-pins `eggfetch-core =0.2.0` (`native-http1`,
  `tls-rustls`); `rust/Cargo.lock` resolves the crates.io 0.2.0 package
  (`6cd254b8…`) with no git/path override. Narrow `cargo update -p
  eggfetch-core --precise 0.2.0` moved only that package.
- No executable provider change: the existing adapter compiled against 0.2.0
  unchanged (only the version comment in `transport.rs` was updated).
- Provider transport: default 30 passed; `test-support` 35 passed.
- Coordinator: C008 29, C009 13, C011 17, boundaries 5, finalization 10,
  publication 6, wire-runtime 8 — all pass; one attempt remains one upstream
  submission with unchanged retry/failover and fail-closed proxy behavior.
- No-default-features: serial suite passes (an early parallel run showed a
  `database_compatibility` timing flake that passes serially and in
  isolation; the repo mandates `--test-threads=1`).
- Serial workspace suite: 704 passed, 0 failed. `cargo deny` passes
  (advisories/bans/licenses/sources ok).
- Feature graph: exactly `native-http1`, `transport-http1`,
  `standard-route`, `advanced-routing`, `tls-rustls`; all forbidden families
  absent. Resolved packages unchanged at 386.
- Footprint (same host, Rust 1.98.1, `aarch64-apple-darwin`, default
  features, release, no strip): 0.1.7 baseline 27,288,864 bytes vs 0.2.0
  candidate 27,268,944 bytes (-0.07%, neutral).
- `qualification-db-diagnostics` feature build still checks and its focused
  targets pass; release workflows enable no Cargo features, so the
  qualification feature cannot leak into artifacts.
- Docs updated: `README.md`, `rust/README.md`, `AGENTS.md`,
  `architecture/overview.md`, `architecture/deep-dive-providers.md` (dated
  0.2.0 subsection), `.opencode/skills/{architecture,development,
  documentation}/SKILL.md`, `transport.rs` comment. Plans 215–220 untouched.
- No compression, protocol, routing, SQLite, or SBC-policy change bundled.
  Upstream issue #24 fix stays dormant (no `compression-*` features, no
  `Accept-Encoding` change).
