# Provider Transport Contract — T001

Status: frozen for T002/T003 handoff

Plan: [T001 — contract and fixture freeze](implementation/provider-transport/001-contract-and-fixture-freeze.md)

Repository baseline: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

This is the executable handoff for the provider transport migration.  Python
remains the production oracle.  The Rust implementation must preserve the
EggPool-owned facts below; incidental HTTPX wording and generated socket
identity are not parity requirements.

## Evidence set

The contract was checked against:

- `src/eggpool/providers/client_pool.py`;
- `src/eggpool/providers/pproxy_transport.py`;
- `src/eggpool/providers/outbound.py` (the non-provider boundary);
- `src/eggpool/models/config.py` and `src/eggpool/providers/contract.py`;
- `tests/unit/test_provider_client_pool.py`,
  `tests/unit/test_pproxy_transport.py`, and proxy/config tests;
- `docs/proxy.md`;
- provider-pool consumers in `runtime_metrics.py` and the network diagnostics
  endpoint; and
- the T001 fixtures in `tests/migration_rs/provider_transport_fixtures.py` and
  `tests/migration_rs/test_t001_provider_transport.py`.

HTTPX is 0.28.1 in the repository environment.  The optional Python oracle is
pproxy 2.7.9.  The Eggress release inspected is `eggress-embed` 1.0.2 at tag
`v1.0.2`, commit
`e76c8d480f411802ac5592e04655a07212be98b5` (release commit dated
2026-08-19).  The inspected upstream artifacts are its `Cargo.toml`,
`eggress-embed/src/outbound.rs`, pproxy compatibility parser/tests, and
`docs/parity/PPROXY_PRACTICAL_COMPATIBILITY_MATRIX.md`.

## Direct provider client

| Behavior | Frozen contract | Parity class | Evidence/deferred work |
|---|---|---|---|
| Base URL | HTTPX receives the validated provider `base_url`; provider endpoint composition strips trailing base slashes and leading endpoint slashes, then joins with one slash. Endpoint trailing slashes survive. Duplicate `/v1`, `/api/v1`, and `/compatible-mode/v1` prefixes are rejected by `compose_provider_url`. | exact EggPool semantics | `providers/contract.py`; T002 |
| Protocol | HTTP/1.1 is the baseline. HTTP/2 is disabled; no opportunistic protocol upgrade. | exact | client-pool `httpx.AsyncClient` defaults and roadmap invariant; T002 |
| Redirects | Redirect following is disabled (`follow_redirects=False`). A redirect response is returned, not followed. | exact | HTTPX default is made explicit for Rust; T002 |
| Ambient environment | Direct provider clients use `trust_env=False`. `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and certificate environment variables must not redirect or alter direct provider transport. | exact/security | T001 pins this policy in `_build_client`; T002 must test it |
| HTTPX defaults | HTTPX contributes its default `Accept`, `Accept-Encoding`, `Connection`, and `User-Agent` headers. Request-specific `Host`, `Content-Length`, and content headers are generated as applicable. User-agent spelling/version and header ordering are incidental. | semantic/incidental library behavior | T002 may use different library defaults unless a provider depends on them |
| EggPool headers | Provider auth, static headers, and protocol-required headers come from `providers/contract.py` at dispatch. Transport does not invent provider credentials and test fixtures use no API key. | exact boundary | T003/T004; auth is not T001 transport |
| Timeout inputs | `connect_timeout_s`, `read_timeout_s`, `write_timeout_s`, and `pool_timeout_s` map to connect, response-read, request-write, and pool-wait stages. | exact stages; semantic implementation | `ProviderConfig` and `_build_client`; T002 |
| Effective read guardrail | `max(read_timeout_s, first_byte_timeout_s if set, idle_timeout_s if set)`. `max_lifetime_s` is compatibility-only and is not enforced. | exact | `ProviderStreamTimeoutConfig.transport_read_timeout`; T002 |
| Limits | `max_connections` bounds physical connections; `max_keepalive` bounds idle retained connections; `keepalive_timeout_s` expires idle connections. | exact resource semantics | HTTPX/httpcore configuration; T002 |
| Response ownership | Response bodies remain stream-capable. The caller owns consuming/closing the response; only a fully consumed or explicitly closed response can return a reusable connection to the pool. A partial/failed body discards the unusable connection. | exact ownership; semantic pool internals | HTTPX/httpcore; T002/M6 |
| Failure categories | Connect timeout/error, TLS/connect error, pool timeout, read timeout/error, write timeout/error, proxy error, and local/remote protocol error remain distinguishable. HTTPX class names and text are incidental. | exact category; semantic wording | `HTTPCORE_EXC_MAP`; T002 taxonomy |
| Cancellation/close | Cancellation propagates. Connect/TLS cancellation closes the stream; failed reads/writes do not return a poisoned connection. Client/pool close is bounded and releases idle resources. | exact lifecycle; semantic mechanism | pproxy stream implementation and pool close; T002 |

The Python `ProviderClientPool` does not use the global
`OutboundClientManager`.  The latter remains the owner for update/catalog and
other non-provider traffic and is outside M4.

## Provider/account pool topology

| Case | Frozen behavior |
|---|---|
| Provider client | One default client is registered per configured provider. |
| Proxied account | An account with a resolved non-null proxy receives one dedicated client using the same provider timeout/limit settings. |
| Direct account | An account without a proxy uses the provider client; it does not receive an account client. |
| Isolation | A proxied account never falls through to the provider direct client. A direct account never uses another account's client. |
| Missing provider | `get_client` raises `UpstreamError("No client for provider ...")`. The default legacy provider accessor returns `None` when absent. |
| Snapshot | `build_count` is provider clients plus account clients; `providers` is sorted and counts each provider client plus its account clients; `account_client_count` is the number of account clients; `account_clients` is sorted `{provider_id, account_name}` records. No URL, credential, proxy URI, or request data is exposed. |
| Close | Current Python re-registration displaces the prior client and closes all displaced/current clients once during bounded pool close. This is an implementation detail, not a Rust generation contract: generation replacement can close the retiring pool instead of re-registering keys in place. |

The runtime diagnostic consumer uses this snapshot only to report client
construction counts and provider scopes.  It must not be expanded with secret
or request-level transport details.

## Proxy resolution

Resolution is account-scoped and has this exact order:

1. `account.proxy_url` when the field is present;
2. `account.proxy_url_env`, resolved from the named environment variable;
3. no proxy when `account.proxy` is absent;
4. named `[proxies.<name>]`, using its `url` or `url_env`.

`AccountConfig` rejects more than one of `proxy`, `proxy_url`, and
`proxy_url_env`.  `AppConfig` rejects an unknown named proxy.  `ProxyConfig`
requires exactly one truthy `url`/`url_env` source.  Inline URL values and
named inline `url` values are not trimmed; environment values are trimmed
before they become proxy URIs.

| Input | Result |
|---|---|
| no account proxy field | `None`, direct provider client |
| `proxy_url = "..."` | exact inline string, including any caller whitespace |
| `proxy_url_env` unset or empty | `ConfigError`, “not set” |
| `proxy_url_env` whitespace-only | `ConfigError`, “whitespace-only” |
| `proxy_url_env` non-empty | stripped value |
| named `url` | exact named inline string |
| named `url_env` unset/empty/whitespace-only | same secret-safe env errors, naming account and env variable only |
| multiple account sources | `ConfigError` before client construction |
| unknown named proxy | `ConfigError` before client construction |
| malformed/unsupported resolved URI | fail closed; no direct fallback |

Error text may identify the account and environment variable name.  It must
not include the environment value, proxy userinfo, fragment credentials,
cipher key, SSH material, or provider API key.

## Proxy URI capability corpus

The machine-readable corpus is
[`proxy-capability-corpus.json`](fixtures/provider-transport/proxy-capability-corpus.json).
`PORT` is substituted only by deterministic local fixtures; credentials are
synthetic.  The corpus records parse, connection, Eggress construction, and
Eggress runtime status independently so deferred T003 runtime work cannot be
mistaken for a passing result.

### Required forms

The current Python product has executable evidence for `direct://`, HTTP
CONNECT, fragment-authenticated HTTP CONNECT, SOCKS4, SOCKS5,
fragment-authenticated SOCKS5, `__` chain parsing, modern Shadowsocks and
Trojan construction, SSR construction with a legacy cipher, and optional SSH
construction.  The local HTTP CONNECT and SOCKS5 fixtures prove target
authority/domain observations and never retain credentials or body bytes.

`socks5://` sends a domain target to the proxy when the target is a hostname;
this is the DNS-through-proxy contract.  The Rust connector must not replace
that with local resolution for mandatory domain-target rows.

### Documentation drift

The old operator examples used `http://user:password@host` for HTTP proxy
authentication.  pproxy 2.7.9 interprets URI userinfo as encrypted-protocol
`cipher:key` and rejects that example.  The supported form is
`http://host:port#user:password`; `docs/proxy.md` now shows that form.

The old troubleshooting text suggested `socks5h://`; pproxy 2.7.9 rejects it
as a standalone client URI.  The documented EggPool form is `socks5://`, which
already sends hostname targets to the SOCKS5 proxy.  `socks4a://` is likewise
recorded as parser drift rather than promoted to a Rust-only EggPool feature.

These are documentation corrections, not silent supported differences.  The
working Python forms remain mandatory in the corpus.

The corpus also records a provider-outbound distinction for `http+socks5://`.
pproxy parses that spelling as listener protocol composition, but Eggress's
listener-free `OutboundConnector` correctly reports no upstream for it.  It is
therefore not an EggPool account proxy form; multi-hop outbound configuration
uses the `__` chain syntax.  The row is retained as documentation drift rather
than being silently treated as a Rust-only supported difference.

## Deterministic fixture inventory

`provider_transport_fixtures.py` provides:

- a threaded HTTP/1.1 upstream that records method, path, header names, body
  length, and connection ID without retaining bodies;
- finite responses, delayed headers, delayed chunked bodies, premature close,
  and malformed framing;
- connection-open and keepalive-reuse observations;
- a TLS upstream using a committed synthetic `localhost` trust anchor and
  certificate with SANs for `localhost` and `127.0.0.1`;
- an HTTP CONNECT proxy with auth success/failure and target authority
  observations;
- a SOCKS5 CONNECT proxy with optional username/password auth and address-kind
  observations (`domain`, `ipv4`, or `ipv6`); and
- bounded bidirectional relay and teardown behavior.

The TLS test builds a normal `CERT_REQUIRED`/hostname-checking client context.
The certificate is trusted only by that test client; no production trust
verification is disabled.  Refused connections use an explicitly closed
loopback endpoint.  Write backpressure and long connect stalls remain
deferred until T002/T003 can exercise them without timing-dependent tests.

## Stable transport error observations

Tests use `TransportErrorObservation` rather than comparing HTTPX class names
or error strings.  It preserves:

- category: `pool_timeout`, `connect_timeout`, `connect_error`,
  `read_timeout`, `read_error`, `write_timeout`, `write_error`,
  `protocol_error`, `proxy_error`, or `cancelled`;
- stage: pool, connect, write, read, TLS, or protocol;
- network path: `direct` or `proxied`; and
- a proxy endpoint containing only scheme and host/port.

Normalization discards wording, socket numbers, timing jitter, and generated
certificate details.  It never discards failure class, stage, status/target
identity when present, direct-versus-proxied evidence, or secret exposure.

## Eggress decision for T003

Use this exact starting dependency proposal:

```toml
eggress-embed = { version = "=1.0.2", default-features = false, features = [
  "common", "pproxy-compat", "extended", "pproxy-legacy", "legacy-crypto", "ssh",
] }
```

Feature justification is explicit:

- `common`: direct, HTTP CONNECT, SOCKS4, SOCKS5, and TCP transport;
- `pproxy-compat`: `OutboundConnector::from_pproxy_uri` and pproxy URI
  translation;
- `extended`: documented modern Shadowsocks and Trojan forms;
- `pproxy-legacy` plus `legacy-crypto`: documented SSR and the working
  pproxy legacy cipher surface;
- `ssh`: documented optional SSH upstream.

`operations`, `reverse`, and `quic` are rejected for M4 provider TCP
transport.  No Eggress production dependency was added in T001.  The lean
feature set was compiled against the inspected release and the upstream
compatibility/parser tests provide construction evidence; actual Eggress
connector traffic, TLS-over-proxy, and all mandatory runtime rows are T003
work.  If T003 finds a mandatory Python form that this feature set cannot
execute, it must stop and create a corrective plan or ADR rather than drop the
row or fall back to direct networking.

## Exact versus semantic parity handoff

| Exact in T002/T003 | Semantic/implementation freedom |
|---|---|
| HTTP/1.1 baseline and no redirects | Hyper/hyper-util connection object layout |
| provider URL/path composition | HTTP library default header spelling/version where providers do not depend on it |
| timeout stage and effective read guardrail | error wording and nested source chains |
| connection and keepalive bounds | generated connection IDs and socket numbers |
| stream ownership and cancellation cleanup | certificate serial/expiry details in test diagnostics |
| direct/proxied path and target/DNS evidence | proxy hop metadata not consumed by EggPool |
| proxy resolution precedence and fail-closed behavior | displaced-client bookkeeping if generation replacement closes whole pools |
| secret absence from diagnostics, snapshots, and fixtures | internal metrics names not exposed by EggPool |

## Dependency and scope result

Production Rust dependency delta: none.  T001 adds only migration fixtures,
contract data, tests, and the oracle-policy correction that makes the
canonical no-ambient-proxy invariant explicit in Python.  It adds no Hyper,
Rustls, Eggress, provider pool, auth, catalog, routing, retry, finalization,
or inference implementation.
