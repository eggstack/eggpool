# Provider Transport T003 — Eggress Connector and Proxy Parity

Status: queued; blocked on T002 closure and T001 feature decision

Repository baseline for planning: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md#t003--eggress-connector-and-proxy-parity`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

Primary class: infrastructure/capability

## 1. Objective

Add per-account outbound proxy transport to the T002 provider HTTP core using Eggress's listener-free `OutboundConnector`. Preserve the Python pproxy abstraction boundary: Eggress establishes the TCP stream, while the same Rustls/Hyper stack used for direct provider traffic owns TLS, HTTP framing, response streaming, connection pooling, and provider timeouts above that stream.

T003 must implement only the proxy forms proven mandatory by T001. It must fail closed for unsupported or malformed forms and must never silently route a configured proxied account directly.

## 2. Dependencies

Hard: T001 and T002 closed.

T001 must contain the reviewed Eggress feature decision and proxy capability corpus. If that decision identifies a mandatory gap in Eggress 1.0.2, resolve the gap or architecture decision before beginning T003.

## 3. Dependency policy

Add `eggress-embed` pinned to the reviewed version during migration qualification, expected initially as:

```toml
eggress-embed = { version = "=1.0.2", default-features = false, features = [/* T001 evidence */] }
```

Do not enable `full`. Enable only features justified by T001. Do not add a second proxy crate, Python embedding, or a localhost Eggress listener.

If development against a local Eggress checkout is necessary to correct a proven upstream gap, keep that temporary relationship explicit and do not leave an unpublished path dependency in the final closure state unless an ADR deliberately accepts it.

## 4. Proxy configuration resolution

Port runtime proxy URL resolution into the Rust config/runtime boundary with Python-compatible semantics:

1. `account.proxy_url` if present;
2. otherwise `account.proxy_url_env` resolved from the environment;
3. otherwise no proxy when `account.proxy` is absent;
4. otherwise resolve named `[proxies.<name>]` using `url` or `url_env`.

Environment values must follow the T001/Python contract for unset, empty, whitespace-only, and trimming behavior. Configuration validation continues to enforce mutual exclusivity and named-reference validity.

Do not resolve proxy env values into long-lived diagnostics or serialize them into state. Return/hold only what is needed to construct the account client.

## 5. Eggress connector boundary

Implement a custom connector compatible with the T002 Hyper/Rustls stack. The connector should:

- compile/validate `OutboundConnector::from_pproxy_uri(...)` at account-client construction time where possible;
- call `connect_tcp`/`connect_tcp_timeout` for the requested target authority;
- adapt Eggress's `BoxStream` into the AsyncRead/AsyncWrite + Hyper connection traits needed by the client;
- carry T002 connection-lifetime admission ownership across the physical proxied connection;
- expose no Eggress listener/socket on localhost;
- use the same Rustls HTTPS wrapping and HTTP/1.1 client behavior as direct traffic.

Avoid duplicating the direct connector's timeout/pool/error code. Prefer a small enum/strategy for direct versus Eggress TCP establishment beneath one provider HTTP client builder.

## 6. Proxy parse/construction failures

A configured proxy that fails to parse, translate, or satisfy the enabled feature set is a configuration/transport construction failure. It must prevent that account's proxied client from being treated as usable. Never catch the error and construct a direct client as a convenience fallback.

Error output may contain a safe scheme/host/port or Eggress redacted expression when useful, but may not contain:

- URI userinfo;
- fragment auth;
- Shadowsocks/SSR cipher keys/passwords;
- SSH passwords/private-key material;
- environment variable secret values;
- provider API keys.

Add regression tests using unique synthetic secret markers and assert their total absence from `Display`, `Debug` surfaces exposed to logs, and migration observations.

## 7. Runtime failure mapping

Map Eggress connection errors into the stable T002/T001 transport categories rather than leaking Eggress internals into later routing policy. Preserve enough evidence to distinguish:

- proxy configuration/unsupported form;
- proxy connect timeout;
- proxy endpoint connection failure;
- proxy authentication rejection where deterministically observable;
- target connection failure reported through proxy;
- subsequent TLS/HTTP/read/write failures handled by the common T002 layer.

Do not decide whether a failure is retryable or account-specific; that policy remains later.

## 8. DNS and target semantics

Use the T001 corpus to preserve whether the target hostname is presented to the proxy or resolved locally for each required proxy form. Do not normalize a DNS-mode difference merely because both eventually reach the same loopback fixture.

Tests must be able to prove the proxy saw the expected target host/domain form for HTTP CONNECT/SOCKS cases where that is contractual.

## 9. Required proxy corpus

Implement and test every T001 row marked mandatory. At minimum the workstream is expected to cover, if T001 confirms current support:

- `direct://` as an Eggress adapter control case;
- HTTP CONNECT;
- authenticated HTTP CONNECT;
- SOCKS4/4a;
- SOCKS5;
- SOCKS5 username/password auth;
- required DNS semantics;
- required `__` multi-hop chains;
- required Shadowsocks/Trojan/SSR/SSH forms according to the selected feature set.

Do not add support merely because Eggress can do it. The goal is EggPool contract parity, not exposing the full Eggress product surface.

## 10. TLS-over-proxy behavior

For HTTPS provider targets, Eggress terminates only the proxy transport required by the chain; the provider TLS session must be established by the same Rustls layer used by the direct T002 client unless a mandatory proxy protocol intrinsically adds its own hop encryption. Provider certificate/hostname verification must remain intact.

A proxy that can observe the CONNECT target must not receive provider Authorization headers before the provider TLS tunnel is established.

## 11. Pooling and account isolation

Hyper connection pools for proxied accounts must be distinct from the provider's direct pool and from other proxied accounts, even when proxy URIs are identical. This matches the Python topology and prevents connection reuse from collapsing account-level network identity.

Physical connection limits and idle retention come from the provider config exactly as for direct clients; T003 must not invent proxy-specific unbounded pools.

## 12. Tests

Required tests include:

- proxy URL resolution precedence and env trimming/failure parity;
- construction of every mandatory T001 URI family;
- malformed/unsupported URI fails before provider request and does not create a direct connection;
- HTTP request succeeds through the local HTTP CONNECT fixture;
- HTTPS request succeeds through CONNECT with provider TLS verification intact;
- SOCKS5 target/auth success and rejection;
- DNS target semantics;
- mandatory chain behavior;
- additional enabled protocol-family cases from T001;
- connection reuse through a proxy;
- separate account pools remain separate even with same proxy endpoint;
- connect timeout/cancellation releases T002 connection permits;
- killing/refusing the proxy never causes direct target traffic;
- secret marker redaction across parse, construction, connection, auth, TLS, and shutdown errors;
- no localhost listener is opened by the Eggress integration.

## 13. Differential verification

Use paired Python pproxy and Rust Eggress observations against the same logical local fixtures, but never have both implementations race on the same mutable fixture state where doing so changes behavior. Compare proxy target, request facts after tunneling, status/body stream, failure stage, auth result, and DNS mode according to T001 normalization.

Eggress's internal error wording and hop metadata may differ semantically; direct-versus-proxy behavior, authentication outcome, target identity, and failure class may not be normalized away.

## 14. Non-goals

- no provider/account pool registry yet beyond construction helpers/tests;
- no routing or selection of which account should receive a request;
- no provider authentication/header injection;
- no UDP provider traffic;
- no reverse/backward proxy roles;
- no system proxy mutation;
- no Eggress CLI/listener lifecycle;
- no inference dispatch/retry/finalization.

## 15. Acceptance criteria

T003 closes when the T002 provider client can be constructed over direct or Eggress-backed TCP establishment, every mandatory T001 proxy row is either passing or explicitly resolved by an approved supported-difference decision, configured proxy failures are fail-closed, provider TLS remains correct, pools stay account-isolated, and diagnostics prove secret redaction.

## 16. Stop conditions

Stop if:

- the required Eggress feature set expands beyond T001 without new evidence;
- a mandatory Python proxy form remains unsupported by Eggress;
- proxy failures can reach the target directly;
- TLS verification must be weakened;
- implementing the proxy requires an Eggress listener or a second HTTP stack;
- secret-bearing proxy expressions cannot be reliably redacted.

## 17. Closure evidence

Record the exact Eggress dependency/features, proxy capability results by corpus row, proxy resolution parity matrix, connector architecture, direct-fallback negative evidence, TLS/DNS/auth tests, connection/account isolation results, timeout/cancellation evidence, redaction tests, and Rust/migration/Python verification output.