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
