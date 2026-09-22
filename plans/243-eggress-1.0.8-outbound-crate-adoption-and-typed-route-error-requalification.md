# Plan 243 — Eggress 1.0.8 Outbound-Crate Adoption and Typed Route-Error Requalification

Date: 2026-09-22
Status: complete
Planning baseline: d2942e50511cf11d5b016a0cb46de9c7c88cefc3 (main, Eggpool 0.8.0)

Upstream release baseline:
- Eggress release tag: v1.0.8
- tagged release commit: f0affac49c0fdaf6bcb51dfe1cb47f1f6548ffed
- previous Eggress release: v1.0.7
- upstream delta: 71 commits from v1.0.7 to v1.0.8
- current Eggpool Eggress pins: exact 1.0.7
- current provider HTTP engine: exact eggfetch-core 0.2.0 with native-http1,tls-rustls
- current production proxy facade: eggress-embed::outbound::OutboundConnector

Related Eggpool work:
- 215-eggfetch-transport-consolidation-roadmap.md
- 216-phase-1-eggfetch-direct-transport-foundation.md
- 217-phase-2-eggfetch-egress-dialer-integration.md
- 218-phase-3-eggfetch-transport-cutover-and-cleanup.md
- 219-phase-4-eggfetch-qualification-and-footprint-closure.md
- 220-eggfetch-0.1.7-adoption.md
- 241-eggfetch-0.2.0-adoption-and-provider-transport-requalification.md

Priority: P1 provider-transport dependency consolidation and correctness

## Objective

Adopt the published Eggress 1.0.8 crate family and move Eggpool's normal
listener-free provider proxy path from the full-service eggress-embed facade to
the new first-class eggress-outbound crate.

Use Eggress 1.0.8's typed outbound connection failure surface to remove the
remaining production message-string classifier in
rust/src/providers/transport.rs while preserving Eggpool's stable
TransportError contract, fail-closed proxy semantics, provider/account pool
ownership, coordinator retry/failover behavior, and Eggfetch 0.2.0 HTTP/TLS
ownership.

This is not a generic proxy rewrite. The intended result is a smaller and more
appropriate Eggress dependency boundary plus a typed route-error adapter.

The desired end state is:

1. all live Eggress pins used by Eggpool are exact 1.0.8 crates.io packages;
2. the normal provider proxy path imports eggress_outbound::OutboundConnector
   directly rather than eggress_embed::outbound::OutboundConnector;
3. the root ssh feature forwards to eggress-outbound/ssh;
4. the production dial path uses connect_tcp_detailed and maps
   OutboundConnectErrorKind plus OutboundConnectStage without inspecting error
   display strings;
5. Eggfetch remains the sole provider HTTP/1.1/origin-TLS/pooling engine;
6. configured proxy accounts remain fail-closed with no direct-network
   fallback after construction or route failure;
7. the test-only custom proxy-root seam remains isolated and does not become a
   production TLS override;
8. default and no-default feature builds retain their existing capability
   contract;
9. the normal release dependency/feature closure and artifact size are measured
   against the 1.0.7 baseline and any change is explained;
10. current documentation describes Eggress 1.0.8 and the outbound ownership
    boundary while historical plans remain unchanged.

## Why 1.0.8 is not just a version bump

Eggress 1.0.8 introduces a published eggress-outbound crate described by
upstream as the direct listener-free Rust dependency for opening proxy-chained
TCP connections without starting a listener service.

That is Eggpool's production use case.

In 1.0.8:

- eggress-outbound owns OutboundConnector and the concrete outbound chain
  execution implementation;
- eggress-embed re-exports eggress-outbound as a compatibility facade for
  full-service consumers;
- ordinary HTTP/SOCKS TCP chains are available in the outbound base surface;
- pproxy-compat enables from_pproxy_uri;
- pproxy-legacy and legacy-crypto retain Eggpool's compatibility proxy families;
- ssh is separately feature-gated and does not imply pproxy-compat;
- connect_tcp remains the compatibility string-error surface;
- connect_tcp_detailed exposes the same single execution through typed
  OutboundConnectError facts;
- typed errors expose kind(), stage(), hop_index(), and protocol();
- Eggress's internal built-in protocol classifier uses concrete error types,
  not display-string matching.

The public typed kinds are:

- Timeout
- Dns
- ConnectionRefused
- NetworkUnreachable
- HostUnreachable
- Authentication
- Tls
- Protocol
- Policy
- Other

The public stages are:

- DirectConnect
- HopConnect
- HopHandshake
- Deadline

Eggpool currently documents its production Eggress error classifier as
temporary debt: eggress-embed 1.0.7 returns EggressError::Runtime(String), so
map_egress_dial_error searches redacted text for timeout/authentication/target
markers before creating Eggfetch DialError values.

Plan 243 closes that debt using the 1.0.8 typed surface.

## Architecture invariants

Preserve the ownership boundary already established by plans 215-220 and
requalified by Plan 241.

Eggpool owns:
- provider/account selection;
- coordinator retry/failover legality;
- request shaping and finite body bounds;
- provider/account client identity;
- stable TransportError categories;
- health/quota/failure effects;
- the decision to use direct versus configured proxy routing.

Eggfetch 0.2.0 owns:
- HTTP/1.1 framing;
- destination/origin TLS;
- WebPKI plus Eggpool's explicit provider test roots;
- Hyper connection pooling and idle expiry;
- physical connection admission;
- established transport read/write inactivity guards;
- the custom Dialer interface;
- native incremental response-body leases.

Eggress 1.0.8 owns:
- pproxy-compatible proxy expression validation/compilation;
- listener-free proxy-chain establishment;
- proxy protocol handshakes;
- route-level TLS;
- optional SSH session ownership;
- typed route-establishment diagnostic facts.

The custom Eggfetch Dialer remains a thin byte-stream adapter. It must not
acquire HTTP framing, origin TLS, request retries, provider policy, or account
routing.

One coordinator attempt must remain at most one provider transport submission.
Neither Eggfetch nor Eggress may add a hidden request retry or direct fallback.

Route-level TLS failures remain proxy-route failures. They must not be mapped
to Eggpool TransportError::Tls, which is reserved for provider/origin TLS in
the existing architecture.

## Current source boundary

At the planning baseline, rust/src/providers/transport.rs contains:

- EggressDialerInner::Outbound holding
  Arc<eggress_embed::outbound::OutboundConnector>;
- build_egress_connector returning
  eggress_embed::outbound::OutboundConnector / eggress_embed::EggressError;
- EggressDialer::dial calling OutboundConnector::connect_tcp;
- map_egress_dial_error accepting eggress_embed::EggressError and classifying
  EggressError::Runtime(String) with lowercased message predicates;
- a separate cfg(test-support) TestRoot path that directly uses
  eggress_core::chain::ChainExecutor so deterministic local proxy CAs can be
  injected.

The TestRoot path is not the production dependency boundary and should not be
used as justification to keep eggress-embed.

## Phase 1 — update the exact Eggress package family

Update rust/Cargo.toml from exact 1.0.7 pins to exact 1.0.8 pins.

Replace the normal production facade dependency with eggress-outbound.

Expected production shape:

~~~toml
eggress-outbound = { version = "=1.0.8", default-features = false, features = [
    "pproxy-compat",
    "pproxy-legacy",
    "legacy-crypto",
] }

ssh = ["eggress-outbound/ssh"]
~~~

Do not enable eggress-outbound defaults.

Do not enable outbound features merely because they exist. In particular,
Eggpool does not need the outbound crate's:

- toml constructor feature;
- udp association feature;
- quic feature;
- insecure-tls feature.

The pproxy compatibility dependency may still retain TOML/config packages in
the resolved lockfile through its own implementation. The acceptance criterion
is that Eggpool does not select unnecessary eggress-outbound feature surfaces,
not that every transitive TOML/UDP crate must disappear.

Update the exact test-support/dev Eggress family to 1.0.8 as well, including
the live entries for:

- eggress-core;
- eggress-config;
- eggress-pproxy-compat;
- eggress-server;
- eggress-uri;
- eggress-protocol-shadowsocks;
- eggress-protocol-trojan.

Use a narrow lockfile update. Do not bundle unrelated dependency upgrades.

Before deleting eggress-embed from the manifest, prove it has no remaining live
owner:

~~~bash
rg -n 'eggress_embed|eggress-embed' rust README.md architecture AGENTS.md .opencode/skills
~~~

Historical plan references do not count as live owners.

If a live non-provider owner exists, retain the dependency for that owner and
record it. Do not force deletion by rewriting unrelated code.

## Phase 2 — adopt eggress-outbound directly

Change the production connector type and constructor in
rust/src/providers/transport.rs to the new supported authority:

~~~text
eggress_outbound::OutboundConnector
~~~

Use:

~~~text
OutboundConnector::from_pproxy_uri(...)
~~~

for the existing pproxy-compatible single-hop and canonical __-separated
multi-hop expressions.

Keep explicit direct:// behavior unchanged:

- validate direct:// through Eggress so malformed control syntax still fails
  closed;
- use the direct Eggfetch provider client after successful validation;
- do not route direct:// through Eggress merely to make the new crate visible.

Do not move provider origin TLS into Eggress.

Do not adopt Eggress listener/service lifecycle APIs.

Do not add an Eggpool-owned proxy executor or SSH session cache.

## Phase 3 — replace string error parsing with the typed detailed surface

Change the normal outbound dial from connect_tcp to connect_tcp_detailed.

Delete the production map_egress_dial_error string classifier after the new
typed mapping is qualified.

The new adapter should consume only stable public Eggress facts:

- OutboundConnectError::kind();
- OutboundConnectError::stage();
- optionally hop_index()/protocol() for bounded tests or diagnostics, but not
  for secret-bearing logging.

Map typed Eggress route errors into Eggfetch DialErrorKind conservatively.

Required baseline mapping:

~~~text
Eggress kind/stage                         Eggfetch DialErrorKind
----------------------------------------------------------------
Timeout / any stage                       Timeout
Authentication / any stage                Authentication
ConnectionRefused + HopHandshake          Rejected
Dns                                       Connection
ConnectionRefused + DirectConnect         Connection
ConnectionRefused + HopConnect            Connection
NetworkUnreachable                        Connection
HostUnreachable                           Connection
Tls                                       Connection
Protocol                                  Other
Policy                                    Other
Other                                     Other
~~~

Deadline should normally be absent because Eggpool currently calls the
non-outer-timeout detailed method and lets Eggfetch's connect timeout own the
provider connection deadline. If Deadline becomes observable unexpectedly,
map Timeout/Deadline to Timeout and document why.

Do not classify Protocol as Authentication or target rejection without a typed
fact that proves it.

Do not classify route Tls as origin Tls.

ConnectionRefused at HopHandshake is the one deliberate refinement:
Eggress has already reached the proxy and the proxy protocol is reporting a
target connection refusal. That is the appropriate typed evidence for
Eggpool's existing ProxyTargetConnect category.

The Eggfetch custom-dial mapping above this adapter remains:

~~~text
Timeout        -> TransportError::ProxyConnectTimeout
Authentication -> TransportError::ProxyAuthentication
Rejected       -> TransportError::ProxyTargetConnect
Connection     -> TransportError::ProxyConnect
Other          -> TransportError::ProxyConnect
~~~

Do not add new public TransportError variants in this plan.

## Phase 4 — preserve the test-only custom proxy-root seam

The cfg(test-support) TestRoot path exists so local fixtures such as Trojan can
inject a deterministic proxy CA while production Eggress keeps its own normal
route trust policy.

Eggress 1.0.8's new OutboundConnector does not make that production behavior
necessary.

Keep the test-only low-level chain executor seam unless 1.0.8 offers an
equivalent public test-root constructor without expanding production
capability.

Requirements:

- it remains cfg(test-support);
- production constructors cannot inject a route CA;
- it supplies no Eggpool-owned SSH session cache;
- it does not become a second production proxy implementation;
- its typed ChainError mapping remains isolated;
- origin TLS remains in Eggfetch;
- default release builds cannot reach the seam.

Do not refactor this path merely to make it look identical to the production
outbound connector. Its different trust purpose is intentional.

## Phase 5 — focused typed-error qualification

Extend rust/tests/provider_transport.rs only where the current corpus cannot
prove the new typed boundary.

The focused acceptance cases should prove:

1. proxy authentication failure maps deterministically to
   TransportError::ProxyAuthentication when Eggress reports typed
   Authentication;
2. proxy target refusal reported during HopHandshake maps to
   TransportError::ProxyTargetConnect;
3. failure to connect to a proxy endpoint remains
   TransportError::ProxyConnect rather than ProxyTargetConnect;
4. route timeout maps to TransportError::ProxyConnectTimeout;
5. route-level TLS failure remains a proxy connection category and is not
   TransportError::Tls;
6. malformed/unsupported proxy configuration still fails before dialing as
   TransportError::ProxyConfiguration;
7. error Display/Debug and any test diagnostics cannot contain proxy usernames,
   passwords, keys, full credential-bearing URIs, provider credentials, or raw
   request bodies;
8. no failed proxy route reaches the provider directly;
9. a client remains usable after cancellation or route failure.

Prefer extending existing HTTP CONNECT/SOCKS/SSH/encrypted fixture boundaries
rather than adding a parallel proxy test framework.

Where an existing assertion currently accepts multiple broad outcomes only
because the 1.0.7 string classifier was lossy, tighten it when the 1.0.8 typed
fact makes the result deterministic.

Do not weaken any existing fail-closed assertion to accommodate the upgrade.

## Phase 6 — provider transport and cancellation qualification

Run both provider transport surfaces:

~~~bash
cargo test --manifest-path rust/Cargo.toml   --test provider_transport -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml   --features test-support   --test provider_transport -- --test-threads=1
~~~

Preserve existing coverage for:

- direct HTTP/HTTPS;
- HTTP CONNECT;
- authenticated HTTP CONNECT;
- SOCKS4;
- SOCKS5 and authenticated SOCKS5;
- canonical multi-hop chains;
- Shadowsocks;
- ShadowsocksR / legacy crypto;
- Trojan through the deterministic test-root seam;
- SSH under the root ssh capability;
- domain target preservation / proxy-side DNS;
- provider origin TLS after proxy establishment;
- account-specific pool isolation;
- keep-alive reuse;
- cancellation followed by same-client recovery;
- malformed proxy fail-closed behavior;
- no direct fallback.

Cancellation tests must continue using observable fixture gates and bounded
timeouts, not fixed sleeps or yield-count guesses.

If a cancellation fixture becomes flaky after the dependency change, diagnose
ownership before changing timeout values.

## Phase 7 — coordinator/failure-semantics requalification

A more precise proxy error classifier can affect failure effects and retry
legality even when transport I/O itself is correct.

Run:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
~~~

Acceptance requires:

- one coordinator attempt still produces no more than one provider submission;
- no Eggress retry/fallback layer is introduced;
- ProxyAuthentication and ProxyTargetConnect remain governed by existing
  coordinator/health policy rather than new special cases;
- cancellation remains cancellation, not a synthetic route failure;
- transport failure never requires process restart or database repair;
- provider/account health remains recoverable through existing state
  transitions.

Do not change coordinator retry budgets, backoff, quarantine, or account
selection merely because 1.0.8 exposes richer route metadata.

## Phase 8 — no-default and SSH feature contract

The root ssh feature remains optional.

Required default behavior:

- pproxy-compatible SSH proxy expressions construct and execute through
  Eggress's native SSH ownership;
- no Eggpool SSH executor/session fallback is introduced.

Required no-default behavior:

- direct provider transport still works;
- non-SSH proxy routes still construct and execute;
- SSH-containing proxy configuration fails before dialing as
  TransportError::ProxyConfiguration.

Run:

~~~bash
cargo check --manifest-path rust/Cargo.toml   --workspace --all-targets --no-default-features

cargo clippy --manifest-path rust/Cargo.toml   --workspace --all-targets --no-default-features -- -D warnings

cargo test --manifest-path rust/Cargo.toml   --no-default-features -- --test-threads=1
~~~

Also inspect the feature graph to prove pproxy-compat does not accidentally
enable SSH and ssh does not replace the explicit compatibility feature.

## Phase 9 — dependency and footprint proof

Run:

~~~bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggress-outbound
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
~~~

Explicitly verify the production Eggress feature profile.

Expected required features:
- pproxy-compat;
- pproxy-legacy;
- legacy-crypto;
- ssh only when the root default ssh capability is enabled.

Expected absent eggress-outbound features unless a live owner is found:
- toml;
- udp;
- quic;
- insecure-tls.

Do not infer footprint reduction from the lockfile alone.

Eggpool's test-support/dev dependencies may retain server/runtime/config
packages in Cargo.lock even after the production dependency boundary becomes
smaller. Measure the normal release dependency closure separately from the full
workspace/lockfile graph.

Record at least:

| Measurement | Eggress 1.0.7 baseline | Eggress 1.0.8 candidate | Delta |
|---|---:|---:|---:|
| release artifact bytes | | | |
| total Cargo.lock package count | | | |
| normal release normal/build dependency count | | | |
| production Eggress entry crate | eggress-embed | eggress-outbound | |
| selected outbound features | N/A | | |
| eggress-runtime linked into normal release | | | |
| eggress-server linked into normal release | | | |
| string route-error classifier | present | absent | |

Use comparable:
- Rust toolchain;
- target triple;
- release profile;
- default features;
- strip/LTO/codegen settings.

A smaller artifact is desirable but not an acceptance requirement by itself.
The structural win is removal of unnecessary service-facade ownership and
message parsing. Any material size increase or unexpected dependency family
must be explained before closure.

Do not reopen the physical-SBC benchmark campaign solely for this dependency
change. Run a new SBC pass only if normal qualification reveals a resource or
transport regression that cannot be characterized with the existing fixtures.

## Phase 10 — RustSec / dependency-policy cleanup

Eggress 1.0.8 still resolves russh 0.62.6 with rsa 0.10.0-rc.18 when SSH is
enabled. Therefore Eggpool's RUSTSEC-2023-0071 exception must not be removed
merely because Eggress moved to 1.0.8.

However, deny.toml currently describes the RSA path as flowing through a pinned
Eggress 1.0.2 stack. That statement is stale.

Update only the current advisory rationale so it accurately names the live
Eggress 1.0.8 / russh 0.62.6 SSH path and retains the existing re-review rule.

Do not claim the RSA advisory is fixed.

Do not add an advisory suppression copied from Eggress unless cargo deny on
Eggpool's actual graph proves it is required.

## Phase 11 — full repository gate

Run the normal dependency-change gate:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo clippy --manifest-path rust/Cargo.toml   --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml   --workspace --all-targets -- --test-threads=1

cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release

cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
~~~

Do not add a permanent CI job specifically for 1.0.8.

## Phase 12 — current-authority documentation

After implementation and measurement, update current documentation/guidance.

At minimum inspect:

- README.md;
- rust/README.md;
- AGENTS.md;
- architecture/overview.md;
- architecture/deep-dive-providers.md;
- .opencode/skills/architecture/SKILL.md;
- .opencode/skills/development/SKILL.md;
- .opencode/skills/documentation/SKILL.md;
- deny.toml advisory comments;
- rust/src/providers/transport.rs module/boundary comments.

Current documentation should state:

- Eggress 1.0.8 is the live provider proxy dependency family;
- normal listener-free proxy dialing uses eggress-outbound directly;
- eggress-embed is not the production outbound owner unless a separate live
  owner was found during implementation;
- Eggress owns proxy/route establishment and route TLS;
- Eggfetch 0.2.0 still owns HTTP/1.1, provider/origin TLS, pooling, admission,
  and transport I/O;
- the production route classifier consumes typed Eggress error facts rather
  than display strings;
- the cfg(test-support) custom route-root seam remains test-only;
- root ssh forwards to eggress-outbound/ssh;
- no-default builds retain direct/non-SSH proxy support and reject SSH config;
- the measured dependency/artifact effect is recorded without overstating
  lockfile/package removal;
- the RSA advisory exception remains because the SSH stack still resolves the
  affected rsa prerelease.

Add a dated Eggress 1.0.8 adoption subsection to
architecture/deep-dive-providers.md with the final measured evidence.

Do not rewrite completed Plans 215-220 or 241 to make them appear to have used
1.0.8. They are historical evidence.

## Explicit non-goals

Plan 243 does not authorize:

- changing provider wire formats;
- changing OpenAI/Anthropic/Gemini/Codex compatibility;
- changing Eggfetch 0.2.0 features or API usage;
- enabling Eggfetch built-in proxy support;
- enabling HTTP/2 or HTTP/3 provider transport;
- enabling Eggress QUIC/H3;
- enabling Eggress UDP for provider transport;
- enabling insecure TLS;
- moving provider/origin TLS from Eggfetch to Eggress;
- changing provider/account routing;
- changing retry budgets or backoff;
- changing health/quota policy;
- adding direct fallback after a proxy failure;
- adding an Eggpool-owned SSH implementation;
- making the test-only custom proxy CA surface production-accessible;
- changing SQLite, runtime scheduling, or SBC policy;
- opportunistically upgrading unrelated crates;
- removing direct Hyper/Rustls dependencies without a separate live-owner
  audit;
- treating a smaller binary as permission to remove supported proxy families.

## Rollback rule

If Eggress 1.0.8 cannot preserve the provider/proxy invariants with the direct
outbound crate and a narrow typed adapter:

1. revert the Eggress package-family pins;
2. restore the 1.0.7 eggress-embed production facade;
3. restore documentation changed only for the candidate;
4. retain Eggfetch 0.2.0 and all unrelated completed work;
5. document the concrete incompatibility in a new corrective plan.

Do not preserve 1.0.8 by:
- weakening proxy tests;
- broadening timeouts without evidence;
- reintroducing message parsing around the typed surface;
- changing coordinator policy;
- accepting direct fallback;
- dropping a supported proxy protocol.

## Acceptance checklist

- [ ] All live Eggress crates used by Eggpool exact-pin 1.0.8.
- [ ] rust/Cargo.lock resolves crates.io 1.0.8 packages with no git/path override.
- [ ] Production listener-free routing imports eggress-outbound directly.
- [ ] eggress-embed is removed from the live production dependency set unless a
      separately documented owner remains.
- [ ] Root ssh forwards to eggress-outbound/ssh.
- [ ] pproxy-compat, pproxy-legacy, and legacy-crypto behavior is preserved.
- [ ] Unneeded outbound toml/udp/quic/insecure-tls features are absent.
- [ ] Production dialing uses connect_tcp_detailed.
- [ ] Production Eggress route failures are classified without message-string
      inspection.
- [ ] Proxy authentication maps deterministically to ProxyAuthentication.
- [ ] Typed proxy target refusal at HopHandshake maps to ProxyTargetConnect.
- [ ] Proxy endpoint connection failure remains ProxyConnect.
- [ ] Proxy timeout remains ProxyConnectTimeout.
- [ ] Route TLS is not misclassified as provider/origin Tls.
- [ ] Configured proxy routes remain fail-closed with no direct fallback.
- [ ] TestRoot remains cfg(test-support) and cannot affect release behavior.
- [ ] Default SSH proxy behavior passes.
- [ ] No-default direct/non-SSH behavior passes and SSH configuration is rejected
      before dialing.
- [ ] Provider transport default target passes.
- [ ] Provider transport test-support target passes.
- [ ] C008/C009/C011, boundaries, finalization, publication, and wire-runtime
      targets pass.
- [ ] One coordinator attempt remains at most one upstream transport submission.
- [ ] Cancellation/recovery tests remain bounded and pass.
- [ ] Strict Clippy passes.
- [ ] Serial workspace suite passes.
- [ ] Locked release build passes.
- [ ] cargo deny passes.
- [ ] RSA advisory exception is retained but its stale Eggress-version rationale
      is corrected.
- [ ] Normal release Eggress feature/dependency closure is measured.
- [ ] Release artifact delta is measured and explained.
- [ ] Current architecture/development/documentation guidance describes 1.0.8
      and the outbound crate boundary.
- [ ] Historical completed plans remain untouched.
- [ ] No unrelated runtime, routing, wire, database, or SBC change is bundled.

## Completion evidence

When implementation lands, change this plan's status to complete and append a
completion record containing:

- implementation commit SHA;
- final exact Eggress package versions and crates.io source proof;
- whether eggress-embed remains in the live normal dependency graph and, if so,
  its owner;
- final eggress-outbound feature set under default and no-default builds;
- provider_transport test counts for default and test-support;
- the focused typed error classification cases and outcomes;
- coordinator focused target outcomes;
- no-default qualification result;
- serial workspace test count;
- strict Clippy result;
- cargo deny result;
- RSA advisory-path confirmation;
- normal release dependency-count comparison;
- release artifact byte comparison;
- current-authority files updated;
- any deviation from the expected source-change set.

Do not claim closure from compilation alone. The line is complete only when the
typed failure semantics, fail-closed route behavior, feature graph, and normal
release dependency boundary are requalified.

## Completion record (2026-09-22)

- Implementation commit SHA: `0c68fed8` (main).
- Final exact Eggress package versions: every live `eggress-*` entry resolves
  to crates.io `=1.0.8` (`eggress-outbound`, `eggress-core`, `eggress-config`,
  `eggress-pproxy-compat`, `eggress-protocol-shadowsocks`,
  `eggress-protocol-socks`, `eggress-protocol-trojan`,
  `eggress-protocol-websocket`, `eggress-protocol-http`, `eggress-uri`,
  `eggress-routing`, `eggress-relay`, `eggress-transport-ssh`,
  `eggress-transport-tls`, `eggress-server`, `eggress-config`); no git/path
  override. `russh` stays `0.62.7`, `rsa` stays `0.10.0-rc.18`.
- `eggress-embed` no longer exists in the graph: the facade closure
  (`eggress-embed`, `eggress-runtime`, `eggress-metrics`,
  `prometheus-client` + derive, `parking_lot` + core, `lock_api`,
  `scopeguard`, `redox_syscall`, `dtoa`) left, while the only additions are
  `eggress-outbound`, `eggfetch-http-connect` (new
  `eggress-protocol-http 1.0.8` dependency), and `base64 0.23.1` beside the
  existing 0.22.1 line. No unrelated crate was upgraded.
- Final `eggress-outbound` feature set: `pproxy-compat`, `pproxy-legacy`,
  `legacy-crypto` (hence `extended`), plus `ssh` only under the default root
  capability. `toml`/`udp`/`quic`/`insecure-tls` are absent from the resolved
  outbound closure.
- `provider_transport` outcomes: 34/34 default, 40/40 with `test-support`.
- Focused typed-error cases and outcomes:
  - unauthenticated HTTP CONNECT rejection now asserts deterministically
    `ProxyAuthentication` (previously accepted `ProxyAuthentication |
    ProxyConnect`);
  - wrong-credential HTTP CONNECT maps to `ProxyAuthentication`;
  - SOCKS5 protocol target refusal maps to `ProxyTargetConnect`;
  - closed proxy endpoint maps to `ProxyConnect` (typed
    `ConnectionRefused`/`HopConnect`, proved at unit level too);
  - blackholed route maps to `ProxyConnectTimeout` via Eggfetch's connect
    timeout;
  - untrusted Trojan route TLS maps to `ProxyConnect`, never `Tls`;
  - malformed expressions fail construction as `ProxyConfiguration`;
  - all failure Displays/Debugs assert secret-free; no failed route reaches
    the provider; cancellation/recovery tests pass unchanged.
- Coordinator focused targets: `coordinator_c008` 29/29, `coordinator_c009`
  13/13, `coordinator_c011` 17/17, `coordinator_boundaries` 5/5,
  `coordinator_finalization` 10/10, `coordinator_publication` 6/6,
  `wire_runtime` 8/8.
- No-default qualification: `cargo check`, strict Clippy, and the full
  no-default suite (56 result groups, 0 failures) pass; SSH proxy config is
  rejected pre-dial and the no-default graph contains zero `ssh` features.
- Serial workspace suite: 60 suites, 708 passed, 0 failed. Strict Clippy
  passes on default and no-default graphs.
- `cargo deny check`: advisories, bans, licenses, sources all ok.
- RSA advisory path: `RUSTSEC-2023-0071` exception retained; rationale
  corrected from the stale 1.0.2 stack to the live Eggress 1.0.8 /
  `russh` 0.62.7 path with the existing re-review rule.
- Normal release dependency comparison (Rust 1.98.1, x86_64-apple-darwin,
  default features, unstripped release profile):
  - artifact bytes: 27,268,944 → 27,250,640 (-18,304, -0.07%);
  - `Cargo.lock` entries: 386 → 378 (-8);
  - non-dev tree lines: 428 → 414 (-14);
  - `eggress-runtime`/`eggress-server` no longer linked into the normal
    release; string classifier removed.
- Current-authority files updated: `README.md`, `rust/README.md`,
  `docs/proxy.md` (also corrected the stale `eggress-ssh-fallback` feature
  name), `AGENTS.md` (Plan 243 paragraph + 1.0.8 SSH bullet),
  `architecture/overview.md`, `architecture/deep-dive-providers.md` (boundary
  refresh + dated 1.0.8 adoption subsection),
  `.opencode/skills/development/SKILL.md`,
  `.opencode/skills/documentation/SKILL.md`, `deny.toml` rationale,
  `transport.rs` boundary comments. No change needed in
  `.opencode/skills/architecture/SKILL.md` (no version-specific Eggress
  claim). Historical plans untouched.
- Deviations from the expected source-change set (all conservative):
  1. root `ssh` is `["eggress-outbound/ssh", "eggress-pproxy-compat/ssh"]`:
     pproxy-style SSH needs both upstream cfg gates, and a weak `?` edge
     cannot fire because the compat package reaches production through
     outbound rather than the optional test-support edge;
  2. `test-support` uses `eggress-server/ssh` to match the outbound flag
     (eggress-server 1.0.8 re-exports the outbound executor whose arity
     follows the outbound `ssh` gate; mismatched flags fail compilation);
  3. `deny.toml` names `russh 0.62.7` (actual resolved version) rather than
     the plan's `0.62.6`.
