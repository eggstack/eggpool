# Deep Dive: Providers and Outbound Clients

Back to [Architecture](README.md)

`rust/src/providers/` owns provider contracts, account credentials, endpoint
composition, Eggfetch direct/proxied transport with the Eggress route dialer,
and the per-provider/account client pool. Provider profiles
describe protocol, URL, auth shape, wire surface, and capability facts without
storing secrets in metadata.

`ProviderClientPool` is generation-owned. Direct and configured proxy accounts
use the selected transport path; a configured proxy never silently falls back
to direct transport. Credentials are rendered only while constructing dispatch
headers. The closed wire registry under `rust/src/wire/` accepts only compiled
codec IDs and does not probe in the background.

The pool publishes one immutable `ClientTopology` per generation. Provider
defaults are keyed by provider ID and account-specific clients are nested by
provider ID and account name, so ordinary lookup borrows both keys without a
temporary tuple allocation or topology mutex. Closing a generation atomically
removes the topology: lookups that already cloned a client may finish, while
later lookups fail closed.

Provider failures are typed before reaching health/retry effects. Per-model
failures quarantine only the affected pair; genuine transport failures may
advance account-wide health.

Provider-client cancellation is an ownership contract: aborting a request must
not poison the configured client or create a direct-network fallback. The
`rust/tests/provider_transport.rs` qualification fixtures make this contract
observable without changing production transport: encrypted-proxy and SSH
tests gate the first accepted TCP connection, separate accepted connections
from completed handshakes, and use the subsequent same-client recovery request
as the release proof. A cancelled handshake may complete protocol negotiation;
the semantic assertions are proxy-target integrity, one recovery origin
request, and successful client reuse rather than an incidental handshake count.

## Native dependency boundaries

Direct provider transport uses exact-pinned `eggfetch-core =0.2.0` with
`native-http1` + `tls-rustls` through the native
`Client::execute_http_body` API. `native-http1` expands to
`transport-http1`, `standard-route`, and `advanced-routing`: it supplies the
direct route and the custom `Dialer` capability without selecting Eggfetch's
high-level policy bundle. HTTP/1.1 remains the only provider protocol, with no
hidden canceled-request retry, physical live-connection admission
(`PhysicalConnectionPolicy`), Hyper idle-pool reuse/expiry, connect timeout,
established `TransportIoTimeout` read/write guards, WebPKI plus explicit
additional CA roots with Eggfetch-owned SNI/hostname verification, and one
Eggfetch-to-`TransportError` translation boundary. EggPool keeps
provider/account selection, request shaping, body bounds, pool identity,
Eggress selection, and stable `TransportError` classification; Eggfetch owns
HTTP/1.1 framing, origin TLS, pooling, admission, I/O guards, and streaming
body leases.

Do not replace `native-http1` with Eggfetch's `http1` compatibility alias:
that alias also enables `high-level-url`, logical retry, redirects, and
Basic-auth policy. `standard-http1` is also wrong because it omits
`advanced-routing`, which proxied accounts need for Eggpool's custom Eggress
`Dialer`. The provider profile leaves built-in proxy, HTTP/2/3, compression,
native roots, JSON, cookies, and multipart features disabled. Eggpool does not
configure Eggfetch `Timeout.total`; connect and established read/write
inactivity remain the separate Eggpool-owned timeout layers described below.

Eggfetch 0.2.0 derives native origin facts directly from Eggpool's parsed
`http::Uri`; the provider path does not serialize through `url::Url` or add
IDNA conversion. `join_provider_target()` remains the boundary that validates
the configured authority and rejects unsafe absolute or authority-form
relative targets before dispatch. The updater's bounded Hyper/Rustls client
in `operations/update.rs` remains a separate owner and is not being migrated
to Eggfetch by this adoption.

Direct and proxied provider routes share one Eggfetch HTTP/1.1 engine with
the same physical admission, idle-pool, connect/I/O timeout, and WebPKI plus
additional CA trust policy. Proxied accounts install a thin `EggressDialer`
that implements Eggfetch's general custom `Dialer` interface over Eggress's
existing raw TCP-route API; Eggfetch still performs origin TLS across the
returned stream, so proxy/route TLS and origin TLS stay separate trust
planes. Each account client owns a distinct Eggfetch `Client`, so pools never
collapse across accounts even for identical proxy URIs. A failed dial is the
only physical route a proxied client owns: there is no direct fallback.

Normal provider proxy construction crosses one listener-free Eggress
boundary: `eggress_outbound::OutboundConnector::from_pproxy_uri` parses and
compiles single-hop and canonical `__`-separated multi-hop expressions, and
the dialer passes the logical destination host/port through unchanged so
domain names survive to SOCKS5 requests, proxy-side DNS, and origin SNI.
Explicit `direct://` is still validated through Eggress but uses the direct
Eggfetch client for loopback/test targets that Eggress rejects as private
egress.

Route failures translate through typed `DialError` kinds into the stable
`TransportError` proxy categories (`ProxyConnectTimeout`,
`ProxyAuthentication`, `ProxyTargetConnect`, `ProxyConnect`); origin TLS
failures stay `Tls` and physical admission timeout stays `PoolTimeout` on
both routes. The production dial path calls
`OutboundConnector::connect_tcp_detailed` and maps the typed
`OutboundConnectError` kind/stage facts directly: `Timeout` and
`Authentication` select their categories on any stage, `ConnectionRefused`
during `HopHandshake` is proxy-target rejection while the same kind at
`DirectConnect`/`HopConnect` stays a proxy-endpoint failure, and route-level
`Tls` never masquerades as origin `Tls`. The private test-root
chain-executor path keeps its own fully typed `ChainError` classification.
`Deadline` is not observable on this path because Eggpool never sets an outer
route timeout; Eggfetch's connect timeout owns the provider deadline.

Eggpool delegates provider proxy-chain construction and execution to
`eggress-outbound` 1.0.8. The root `ssh` capability enables the outbound
crate's native SSH session ownership plus the compatibility crate's SSH
translation support by default (pproxy-style SSH expressions construct only
when both cfg gates agree); `--no-default-features` omits that capability
and rejects SSH-containing expressions as
`TransportError::ProxyConfiguration` before dialing while retaining non-SSH
proxy construction. There is no Eggpool SSH executor fallback and no direct
fallback after a proxy construction or dial failure.

Eggpool retains one private `test-support` adapter for deterministic custom
proxy TLS roots. It uses the low-level Eggress chain executor only to inject a
verified fixture CA, supplies no SSH session state, and is never reachable from
production constructors. Protocol fixture crates remain dev-only.

Provider transport cutover is complete: `ProviderHttpClient` is Eggfetch-only
and the bespoke Hyper/Rustls connector/admission/timer machinery is gone.
The `hyper`, `hyper-util`, `hyper-rustls`, `rustls`, and `webpki-roots`
direct dependencies remain for their live owners outside provider transport:
the `operations/update.rs` self-update release client (bounded Hyper/Rustls
metadata/artifact fetch with WebPKI roots), typed `hyper::Error` /
`rustls::Error` source inspection in the Eggfetch-to-`TransportError`
boundary, and the `test-support` Eggress route-TLS seam. SQLite's bundled
and backup features likewise belong to the database and lifecycle
contracts. Review the resolved graph with `cargo tree -e features` before
changing any of these boundaries. Run the repository policy gate as part of
dependency changes:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

The policy deliberately reports duplicate versions instead of rejecting the
legitimate Eggress/SSH/crypto and platform families in the current lockfile.

## Consolidation footprint (Phase 4 measurement)

Before/after comparison of the pre-migration revision against the post-cutover
revision, both built with Rust 1.89.0 for `aarch64-apple-darwin`, default
features, the normal release profile, and no stripping (matching the release
packaging, which passes `--strip false` to maturin):

| Measurement | Before | After | Delta |
|---|---:|---:|---|
| final artifact bytes | 27,908,656 | 30,139,568 | +2,230,912 (+8.0%) |
| direct dependencies | includes `tower-service` | `tower-service` removed | -1 |
| resolved packages (`cargo metadata`) | 383 | 414 | +31 |
| Eggfetch enabled features (superseded 0.1.5 `http1` profile) | N/A | `http1` + `tls-rustls` only | minimal |

The result classifies as **larger by a measured amount**. The delta is
explained, not accidental: the entire added package closure arrives through
`eggfetch-core` itself (`url` → `idna` → ICU tables, plus `dashmap`), while
no package leaves the resolved graph because `hyper`/`hyper-util`/
`hyper-rustls`/`rustls`/`webpki-roots` remain both transitively and directly
for the live owners listed above. `cargo tree -e features` confirms no
proxy, HTTP/2, HTTP/3, compression, native-root, JSON, cookie, or multipart
activation, and both old and new transports are never linked together — the
old engine is deleted. A bounded local smoke check (boot, degraded-but-valid
`/v1/readyz` + `/api/status` with zero accounts, ~15.6 MB idle RSS, clean
`stop` with no lingering tasks) shows no lifecycle or footprint regression at
rest. The increase is acceptable: behavior is fully preserved, bespoke
transport ownership is gone, and the artifact remains suitable for SBC/local
deployment. Trimming Eggfetch's own `url`/IDNA closure would be a separate
general upstream improvement, not a migration follow-up.

The source-maintenance win is the only file changed under `rust/src/`:
`providers/transport.rs` is rewritten around the Eggfetch engine with the
custom Hyper connector, physical admission wrapper, established-I/O timeout
wrapper, manual origin TLS connector, Hyper pool construction, and
string-based error plumbing deleted. What remains Eggpool-owned is request
validation, route/dialer selection, and the centralized typed
Eggfetch-to-`TransportError` boundary.

## Eggfetch 0.1.7 adoption measurement (2026-09-18)

The adoption compares the plan-220 starting commit (`2f0e07e3`) and the
candidate under the same local Rust `1.98.1` toolchain, `aarch64-apple-darwin`
target, default features, normal release profile, and no stripping:

| Measurement | 0.1.5 / `http1` baseline | 0.1.7 / `native-http1` candidate | Delta |
|---|---:|---:|---:|
| final release artifact bytes | 30,622,256 | 30,370,272 | -251,984 (-0.82%) |
| resolved packages (`cargo metadata`) | 414 | 385 | -29 |
| direct Eggfetch feature profile | `http1`, `tls-rustls` | `native-http1`, `tls-rustls` | native-only |

The candidate graph resolves `native-http1`, `transport-http1`,
`standard-route`, `advanced-routing`, and `tls-rustls`. The `cargo tree -i`
queries for `url`, `idna`, `icu_provider`, `icu_normalizer`,
`icu_properties`, and `dashmap` report no package, so the former Eggfetch
URL/IDNA/ICU and DashMap closures are absent from the resolved Eggpool graph.
The candidate still retains direct Hyper/Rustls dependencies for the updater,
error-boundary inspection, and test-support owners documented above.

## Eggfetch 0.2.0 adoption measurement (2026-09-22)

Plan 241 adopts the published `eggfetch-core =0.2.0` crate with the unchanged
`native-http1` + `tls-rustls` profile. Upstream 0.2.0 is API-preserving
relative to 0.1.7 (same public Rust surface, feature graph, defaults, and
MSRV Rust 1.89); the only Eggpool-visible upstream fix is the streaming-
decompression chunk-boundary correction (issue #24), which stays dormant
because EggPool does not enable any `compression-*` feature and keeps using
the frame-preserving `Client::execute_http_body`/`NativeResponseBody` path
with no injected `Accept-Encoding`.

The adoption compares the 0.1.7 baseline and the 0.2.0 candidate under the
same local Rust `1.98.1` toolchain, `aarch64-apple-darwin` target, default
features, normal release profile, and no stripping:

| Measurement | 0.1.7 baseline | 0.2.0 candidate | Delta |
|---|---:|---:|---:|
| final release artifact bytes | 27,288,864 | 27,268,944 | -19,920 (-0.07%) |
| resolved packages (`cargo metadata`) | 386 | 386 | 0 |
| direct Eggfetch feature profile | `native-http1`, `tls-rustls` | `native-http1`, `tls-rustls` | unchanged |
| unexpected Eggfetch feature families | none | none | — |

The resolved graph selects exactly `native-http1`, `transport-http1`,
`standard-route`, `advanced-routing`, and `tls-rustls`; high-level `http1`,
`high-level-url`, logical retry, redirects, Basic-auth, built-in proxy,
HTTP/2/3, compression, native roots, JSON, cookies, and multipart remain
absent. No Eggpool source change was required beyond the exact pin and
version comments: the existing provider, coordinator, and cancellation
qualification passes unchanged, and one coordinator attempt still produces at
most one transport submission. Plans 215–220 remain the historical
migration/adoption evidence; the 0.1.7 measurement above stays historical.

## Eggress 1.0.8 outbound-crate adoption measurement (2026-09-22)

Plan 243 adopts the published Eggress `1.0.8` crate family and moves the
normal listener-free provider proxy path from the full-service
`eggress-embed` facade to the first-class `eggress-outbound` crate, which is
Eggpool's actual use case (proxy-chained TCP without a listener service).
The production dial path switches from `connect_tcp` to
`connect_tcp_detailed`, and the message-string route-error classifier is
deleted in favor of the typed `OutboundConnectErrorKind`/`OutboundConnectStage`
adapter described above.

The adoption compares the 1.0.7 baseline and the 1.0.8 candidate under the
same local Rust `1.98.1` toolchain, `x86_64-apple-darwin` target, default
features, normal release profile, and no stripping:

| Measurement | 1.0.7 baseline | 1.0.8 candidate | Delta |
|---|---:|---:|---|
| final release artifact bytes | 27,268,944 | 27,250,640 | -18,304 (-0.07%) |
| resolved packages (`Cargo.lock` entries) | 386 | 378 | -8 |
| normal non-dev dependency lines (`cargo tree -e no-dev`) | 428 | 414 | -14 |
| production Eggress entry crate | `eggress-embed` | `eggress-outbound` | facade removed |
| selected outbound features | N/A | `pproxy-compat`, `pproxy-legacy`, `legacy-crypto` (+`ssh` under default) | minimal |
| `eggress-runtime` in normal release | yes (via facade) | no (package gone) | removed |
| `eggress-server` in normal release | yes (via facade) | no (test-support/dev only) | removed |
| string route-error classifier | present | absent | removed |

The result classifies as **smaller by a measured amount**, but the delta is
not the acceptance criterion on its own. The structural win is the smaller
dependency boundary plus the typed failure surface: the full-service closure
(`eggress-embed`, `eggress-runtime`, `eggress-metrics`, `prometheus-client`,
`parking_lot`, `dtoa`, and their platform shims) leaves the graph, while the
only additions are `eggress-outbound`, `eggfetch-http-connect` (a new
`eggress-protocol-http` 1.0.8 dependency), and `base64` 0.23.1 alongside the
already-present 0.22.1 line. `cargo tree -e features` confirms the required
`pproxy-compat`/`pproxy-legacy`/`legacy-crypto` profile with `ssh` only under
the default root capability, and the forbidden `toml`/`udp`/`quic`/
`insecure-tls` outbound features absent. No unrelated crate was upgraded:
every other lockfile change is a `1.0.7` → `1.0.8` Eggress-family pin move,
all still resolving to crates.io with no git/path override.

Three deviations from the plan's expected shape were required during
implementation, all conservative and all requalified:

- Root `ssh` is `["eggress-outbound/ssh", "eggress-pproxy-compat/ssh"]`
  rather than outbound alone. Upstream keeps `ssh` and `pproxy-compat` as
  independent cfg gates, and 1.0.7's facade forwarded the compat-side gate
  weakly from inside the facade; a weak edge from Eggpool cannot fire because
  the compatibility package reaches the production graph through outbound,
  not through the optional test-support edge. The strong edge flips only that
  cfg flag (`ssh = []` adds no packages) and preserves SSH URI construction.
- `test-support` enables `eggress-server/ssh` to match the outbound flag.
  `eggress-server` 1.0.8 re-exports `eggress-outbound::build_chain_executor`,
  whose arity follows the outbound `ssh` gate, while the server's internal
  call site follows its own; building the server without `ssh` while outbound
  has it is an upstream arity skew that fails compilation. The seam still
  passes no SSH session cache and remains `cfg(test-support)`.
- The RSA advisory exception (`RUSTSEC-2023-0071`) is retained with a
  corrected rationale: the affected `rsa 0.10.0-rc.18` still resolves through
  the live Eggress 1.0.8 / `russh` 0.62.7 SSH path.

Focused qualification tightened one previously broad assertion (HTTP CONNECT
rejection without credentials is now deterministically
`ProxyAuthentication`) and added wrong-credential, SOCKS5 target-refusal
(`ProxyTargetConnect`), closed-proxy-endpoint (`ProxyConnect`), blackholed
route-timeout (`ProxyConnectTimeout`), and untrusted route-TLS
(`ProxyConnect`, never `Tls`) coverage. Provider transport passes 34/34
default and 40/40 with `test-support`; coordinator C008/C009/C011,
boundaries, finalization, publication, and wire-runtime pass; no-default
check/clippy/tests pass with SSH rejected pre-dial; `cargo deny` passes.
Plans 215–220 and 241 remain historical evidence.
