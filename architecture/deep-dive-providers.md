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

Provider failures are typed before reaching health/retry effects. Per-model
failures quarantine only the affected pair; genuine transport failures may
advance account-wide health.

## Native dependency boundaries

Direct provider transport uses exact-pinned `eggfetch-core =0.1.5` with only
`http1` + `tls-rustls` through the native `Client::execute_http_body` API:
HTTP/1.1 only, no hidden canceled-request retry, physical live-connection
admission (`PhysicalConnectionPolicy`), Hyper idle-pool reuse/expiry,
connect timeout, established `TransportIoTimeout` read/write guards, WebPKI
plus explicit additional CA roots with Eggfetch-owned SNI/hostname
verification, and one Eggfetch-to-`TransportError` translation boundary.
EggPool keeps provider/account selection, request shaping, body bounds, pool
identity, Eggress selection, and stable `TransportError` classification;
Eggfetch owns HTTP/1.1 framing, origin TLS, pooling, admission, I/O guards,
and streaming body leases.

Direct and proxied provider routes share one Eggfetch HTTP/1.1 engine with
the same physical admission, idle-pool, connect/I/O timeout, and WebPKI plus
additional CA trust policy. Proxied accounts install a thin `EggressDialer`
that implements Eggfetch's general custom `Dialer` interface over Eggress's
existing raw TCP-route API; Eggfetch still performs origin TLS across the
returned stream, so proxy/route TLS and origin TLS stay separate trust
planes. Each account client owns a distinct Eggfetch `Client`, so pools never
collapse across accounts even for identical proxy URIs. A failed dial is the
only physical route a proxied client owns: there is no direct fallback.

Normal provider proxy construction crosses one stable Eggress boundary:
`eggress_embed::outbound::OutboundConnector::from_pproxy_uri` parses and
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
both routes. One deliberate limitation is documented in the adapter: the
pinned embed facade renders typed route errors to redacted
`EggressError::Runtime` strings, so that path classifies the route-failure
bucket with conservative message predicates mirroring the previous transport
behavior. The compatibility chain-executor path (SSH, test roots) keeps fully
typed `ChainError` classification. If a future Eggress embed API exposes the
typed route error, the predicates collapse to a direct match.

Eggress 1.0.6 has one documented facade gap: its outbound constructor builds
the SSH-capable executor without an SSH session cache. The explicitly named
default `eggress-ssh-fallback` feature therefore retains the matching 1.0.6
native chain executor and compatibility SSH session cache for SSH upstreams.
Builds that deliberately disable this fallback still compile and retain direct
and non-SSH proxy construction, but reject an SSH-containing proxy expression
as `TransportError::ProxyConfiguration` before any connection attempt. They do
not route the expression through the known-broken embed SSH path or fall back
to direct egress. This is a temporary compatibility boundary pending an
upstream Eggress facade fix.
The deterministic custom-root constructor uses that same narrow seam only
under `test-support`; it adds a test CA and never disables verification.
Protocol fixture crates remain dev-only. These are intentional compatibility
boundaries, while ordinary provider source uses the stable embed API.

Direct Hyper/Rustls dependencies remain declared until the transport-cutover
phase confirms no remaining references and removes them; they are no longer
used by provider transport code. SQLite's bundled and backup features
likewise belong to the database and lifecycle
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
