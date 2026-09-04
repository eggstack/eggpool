# T003 Closure — Eggress Connector and Proxy Parity

Status: closed

Recommendation: closed; T004 is dependency-ready. T005 remains queued behind
T004, and M5 remains blocked on the complete M4/T005 sequence.

Implementation commit: [`5b34d8b`](https://github.com/eggstack/eggpool/commit/5b34d8b)

Plan: [T003 — Eggress connector and proxy parity](../../implementation/provider-transport/003-eggress-connector-and-proxy-parity.md)

Contract: [provider transport contract](../../provider-transport-contract.md)

## Outcome

T003 adds `eggress-embed` 1.0.2 with exactly the reviewed non-default feature
set: `common`, `pproxy-compat`, `extended`, `pproxy-legacy`, `legacy-crypto`,
and `ssh`. `operations`, `reverse`, and `quic` remain disabled. No second
proxy crate, Python embedding, or localhost Eggress listener was added.

`ProviderHttpClient::new_with_proxy` validates the configured proxy during
client construction, then uses an Eggress-backed TCP service beneath the same
Hyper HTTP/1.1 and Rustls client used for direct traffic. Eggress streams are
adapted once into Hyper's runtime IO traits. The existing physical-connection
semaphore and read/write/connect/pool timeout wrapper owns the proxied stream
for its entire pooled lifetime.

Proxy resolution is available at the Rust configuration boundary with Python
precedence: inline account URL, account environment URL, no proxy, then named
proxy URL/environment. Inline values remain exact; environment values reject
unset/empty/whitespace-only values and trim non-empty values. Resolution errors
contain only the account and environment variable names.

Full `__` chains are lowered to Eggress's native `[[upstreams]]` TOML shape.
This is required because the reviewed 1.0.2 embed convenience constructor
accepts only the first pproxy hop; using it directly for a chain would silently
discard later hops. The `direct://` control form is validated through Eggress
and uses the common direct provider dialer for explicit direct semantics,
including loopback deterministic fixtures that Eggress's SSRF guard rejects.

The frozen corpus was corrected for `http+socks5://`: pproxy parses this as
listener protocol composition, while Eggress correctly reports no outbound
upstream for it. It is now recorded as documentation drift, not as a Rust-only
supported difference; provider account multi-hop uses `__`.

## Requirement matrix

| Requirement | Evidence | Result |
|---|---|---|
| Pinned narrow Eggress dependency and feature decision | `rust/Cargo.toml`, `rust/Cargo.lock`, `cargo tree -e features`, T001 corpus | Pass |
| Direct and Eggress-backed TCP below one HTTP/TLS stack | `ProviderTcpConnector`, `ProviderStream`, `ProviderHttpClient::new_with_proxy` | Pass |
| Direct control URI and mandatory URI families construct | `mandatory_proxy_corpus_uri_families_construct`; direct control test | Pass |
| HTTP CONNECT request/response path | `http_connect_proxy_preserves_target_and_request_response` | Pass |
| Authenticated HTTP CONNECT | `authenticated_http_connect_preserves_provider_tls_verification` | Pass |
| SOCKS5 auth and proxy-side DNS target | `socks5_auth_preserves_domain_target_and_rejection_is_not_direct` | Pass |
| Required multi-hop chain | `http_then_socks5_chain_preserves_both_hops` | Pass |
| Required encrypted/legacy/SSH construction corpus | same mandatory corpus test; selected features are asserted by Cargo resolution | Pass; runtime peers remain outside deterministic local fixtures |
| Malformed/unsupported proxy fails closed | `malformed_proxy_fails_closed_without_secret_bearing_diagnostics` | Pass |
| Proxy auth/connect failure mapping | stable `ProxyAuthentication`, `ProxyConnect`, `ProxyConnectTimeout`, `ProxyTargetConnect` categories and rejection test | Pass |
| Target DNS semantics | CONNECT authority and SOCKS5 `domain:localhost` observations | Pass |
| HTTPS provider TLS remains verified and proxy sees no provider auth | authenticated CONNECT TLS test; proxy records CONNECT headers and excludes `authorization` | Pass |
| Connection reuse and account pool isolation | T002 reuse coverage plus `identical_proxy_endpoints_keep_account_pools_isolated` | Pass |
| Admission ownership, timeout, cancellation cleanup | shared `AdmissionConnector`/`TimedConnection`; T002 cancellation and pool-timeout tests | Pass; proxied path composes the same ownership wrapper |
| Secret-safe parse, construction, connection, TLS, and diagnostics | stable error mapping, redacted marker test, no credential-bearing source in `TransportMarker` debug | Pass |
| No Eggress listener or second HTTP stack | dependency/source inspection and connector architecture | Pass |

## Differential and compatibility evidence

The T001 Python oracle remained unchanged. Rust tests compare the same
non-normalizable facts: proxy target identity, HTTP status/body, HTTPS
certificate verification, SOCKS domain-vs-IP address kind, authentication
outcome, chain-hop observations, failure category, and secret absence. Socket
numbers, connection IDs, timing jitter, Eggress wording, and internal hop
metadata are not compared.

The mandatory corpus results are: direct, HTTP CONNECT, authenticated HTTP
CONNECT, SOCKS4/SOCKS5, authenticated SOCKS5, `__` chain, Shadowsocks, SSR,
Trojan, and SSH all construct successfully. HTTP CONNECT, authenticated
CONNECT/TLS, SOCKS5/auth/DNS, and the HTTP→SOCKS5 chain execute against local
fixtures. `http+socks5://` is explicitly documentation drift and is not an
EggPool provider outbound form.

No live provider, external proxy, API key, or real proxy credential was used.

## Verification evidence

Commands actually run:

- `rtk cargo fmt --manifest-path rust/Cargo.toml -- --check` — passed;
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` — passed;
- `rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1` — 18 passed;
- `rtk cargo test --manifest-path rust/Cargo.toml --lib` — 16 passed;
- `rtk cargo test --manifest-path rust/Cargo.toml` — 41 passed;
- `rtk uv sync --frozen --extra ci` — passed;
- `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 46 passed, 3 skipped;
- `rtk uv run --extra proxy pytest tests/migration_rs/test_t001_provider_transport.py tests/unit/test_provider_client_pool.py tests/unit/test_pproxy_transport.py tests/unit/test_config.py -q --tb=short --maxfail=1` — 157 passed;
- `rtk uv run ruff format --check src/ tests/ scripts/` — passed;
- `rtk uv run ruff check src/ tests/ scripts/` — passed;
- `rtk uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations;
- `rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed;
- `rtk git diff --cached --check` — passed before the implementation commit.

## Security, lifecycle, and known limitations

- Proxy parse and runtime errors are converted to stable categories and do not
  retain Eggress error text. `TransportMarker` debug output omits its source;
  public `TransportError` display is credential-free.
- Proxy credentials and provider authorization remain outside the transport
  diagnostics. The CONNECT fixture confirms provider authorization is sent
  only inside the post-CONNECT TLS stream.
- The adapter opens no listener and does not enable Eggress `full`,
  `operations`, `reverse`, or `quic` features.
- Encrypted/legacy/SSH forms are construction-qualified only because the
  frozen T001 corpus has no deterministic local peers for those protocols.
  Runtime peer qualification remains a later explicit interoperability task;
  it is not claimed here as provider traffic evidence.
- T001's deterministic long connect-stall/write-backpressure fixtures remain
  deferred; T002's bounded timers and cancellation/permit coverage remain the
  applicable evidence.
- This milestone does not build the provider/account pool, routing, retries,
  credentials, inference dispatch, rehash generations, or finalization.

Unresolved mandatory findings: none.

## Future-plan state

T003 is closed because the required proxy construction/runtime boundary,
fail-closed behavior, TLS/DNS/auth semantics, account-pool isolation, and
redaction evidence pass against the corrected T001 corpus. T004 is moved from
queued/blocked to dependency-ready because T001, T002, and T003 are closed.
T005 remains queued behind T004. M5 and later work remain blocked on M4/T005;
no other future plan is safely unblocked by T003 alone.
