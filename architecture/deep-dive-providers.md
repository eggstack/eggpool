# Deep Dive: Providers and Outbound Clients

Back to [Architecture](README.md)

`rust/src/providers/` owns provider contracts, account credentials, endpoint
composition, direct Hyper/Rustls transport, and the per-provider/account client
pool. Provider profiles describe protocol, URL, auth shape, wire surface, and
capability facts without storing secrets in metadata.

`ProviderClientPool` is generation-owned. Direct and configured proxy accounts
use the selected transport path; a configured proxy never silently falls back
to direct transport. Credentials are rendered only while constructing dispatch
headers. The closed wire registry under `rust/src/wire/` accepts only compiled
codec IDs and does not probe in the background.

Provider failures are typed before reaching health/retry effects. Per-model
failures quarantine only the affected pair; genuine transport failures may
advance account-wide health.

## Native dependency boundaries

The provider transport keeps direct ownership of its protocol boundary. The
Eggress component crates named by `rust/src/providers/transport.rs` provide
core target types, pproxy parsing/translation, TOML compilation, chain
execution, URI hop specifications, and SSH session caching. The selected
Eggress features retain pproxy-compatible outbound URIs, extended protocols,
legacy Shadowsocks methods/plugins, and SSH chains. These are compatibility
contracts, not redundant declarations.

The surrounding HTTP client intentionally remains a separate Hyper/Rustls
stack: HTTP/1.1 only, Rustls with `ring` and TLS 1.2, deterministic webpki
roots, bounded pooling, and explicit timeout/error classification. SQLite's
bundled and backup features likewise belong to the database and lifecycle
contracts. Review the resolved graph with `cargo tree -e features` before
changing any of these boundaries.
