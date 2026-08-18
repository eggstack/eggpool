# Deep Dive: Provider Architecture

Back to [Overview](overview.md)

## Purpose

Provider configuration separates provider identity, protocol/model contracts,
credentials, account health, and outbound transport. Connection pooling and
the operating-system resolver provide the normal network path; EggPool does
not add a process-local DNS cache.

## Key modules

- `providers/contract.py` — provider URL, protocol, model, authentication, and
  capability contracts.
- `providers/client_pool.py` — provider/account client ownership and bounded
  HTTPX connection pools.
- `providers/outbound.py` — shared outbound client manager and diagnostics.
- `providers/pproxy_transport.py` — per-account proxy transport when configured.
- `providers/auth.py` — credential/header construction and redacted diagnostics.
- `providers/connect.py` — provider setup and connectivity probes.

## Provider client lifecycle

`ProviderClientPool` owns clients for eligible provider accounts. Normal clients
use HTTPX transports and connection reuse. Accounts with configured proxies use
the pproxy transport path. Both paths are generation-owned and close with the
retiring generation.

## Configuration and model IDs

Provider-suffixed model IDs use `model-id/provider-id`. `compose_provider_url()`
is the single URL construction authority. Static model rows are the source of
truth when a provider serves a non-default protocol or capability contract.

## Invariants

- API credentials never appear in diagnostics or logs.
- Provider/model capability data gates native protocol fields.
- Upstream failures, not local quota estimates, suppress account routing.
- OS resolution and HTTP connection reuse are the default network behavior.
- Proxy routing remains per-account and independent of normal resolution.
- Bundled local-runtime multimodal capability declarations represent verified
  protocol-surface behavior (e.g. base64 image support on the OpenAI-compatible
  endpoint), not guarantees that every loaded model supports the modality.
  Provider-bound decisions use the *selected* provider's row — collapsed models
  served by multiple providers with different capability rows do not borrow
  another provider's claim.
- Provider-bound serialized-size decisions resolve against the selected
  provider's capability row. Speculative universal ceilings (such as a
  universal 5 MiB local-runtime ceiling) are not encoded in bundled templates;
  only verified provider-defined limits are advertised.
