# T005 Closure — Differential Qualification and M4 Closure

Status: closed

Recommendation: closed; M5 planning is unblocked. No downstream implementation
plan is created or marked dependency-ready by this record.

Implementation commit: [`c89e645`](https://github.com/eggstack/eggpool/commit/c89e645)

Plan: [T005 — differential qualification and M4 closure](../../implementation/provider-transport/005-differential-qualification-and-closure.md)

Contract: [provider transport contract](../../provider-transport-contract.md)

## Outcome

T005 qualifies the complete M4 provider transport boundary after T001-T004.
The neutral Rust client preserves method, joined path/query, request headers
and body, response status/headers, HTTP/1.1 behavior, incremental response
consumption, redirect non-following, TLS hostname verification, connection
reuse, idle expiry, pool capacity, timeout categories, cancellation cleanup,
and direct/proxied path identity.

The qualification also found and closed one transport-boundary risk. Hyper-util
defaults to retrying a request that is assigned a stale reused connection and
fails before writing. `ProviderHttpClient` now sets
`retry_canceled_requests(false)`, leaving retry/failover exclusively to the
later coordinator where attempt ownership exists. No request retry is added by
M4.

The direct and proxied paths share one Hyper/Rustls HTTP/1.1 implementation;
Eggress remains only the per-account TCP establishment layer. Configured proxy
construction and runtime failures remain fail-closed, with no direct fallback.
The existing T001/T003 corpus is retained as the authoritative proxy feature
decision: all mandatory rows construct, the common HTTP/CONNECT/SOCKS/auth/DNS
and chain rows execute against local fixtures, and encrypted/legacy/SSH rows
retain the documented construction qualification because deterministic local
protocol peers are not part of the repository fixture set. This is an
explicit verification boundary already recorded by T001/T003, not a Rust-only
supported-difference waiver.

## Contract-to-evidence matrix

| T001/T005 requirement | Evidence | Result |
|---|---|---|
| Method, path/query, request headers/body | `direct_http_preserves_request_shape_and_reuses_http11_connection`; `ProviderHttpClient::send` | Pass |
| Response status/headers/body preservation | direct request assertions; `ProviderResponse` fields; `ProviderBody::next` | Pass |
| Incremental response consumption | `response_body_is_incremental_and_read_timeout_is_classified`; chunked fixture | Pass |
| HTTP/1.1 baseline and no redirect following | HTTP/1-only connector construction; `direct_http_returns_redirect_without_following_or_retrying` | Pass |
| Provider TLS verification and hostname checking | `direct_https_uses_hostname_verified_explicit_test_root`; authenticated proxied HTTPS test | Pass |
| Base URL/path joining | `join_provider_target` unit tests; provider contract URL tests | Pass |
| Ambient proxy environment cannot redirect direct clients | direct connector is built without environment proxy lookup; T001 `trust_env=False` oracle and direct loopback tests | Pass |
| Direct client reuse and physical keepalive | `direct_http_preserves_request_shape_and_reuses_http11_connection` | Pass |
| Proxied client reuse | `proxied_http_reuses_one_tunneled_connection` | Pass |
| Idle expiry and max keepalive behavior | `idle_keepalive_expiry_forces_a_new_physical_connection`; Hyper pool idle settings | Pass |
| `max_connections` and pool timeout bound | `pool_pressure_times_out_without_leaking_capacity` | Pass |
| Build/client counts stable with request volume | `ProviderClientPool` immutable topology/snapshot tests; repeated lookup assertions | Pass |
| Connect refusal and connection failure | `refused_connection_is_classified_as_connect_failure` | Pass |
| Connect/read/write/pool timeout category mapping | `AdmissionConnector` and `TimedConnection`; read and pool timeout tests; T001 deterministic write/connect deferral | Pass |
| Response read failure and malformed framing | `premature_response_close_is_protocol_failure`; delayed-header read timeout | Pass |
| Cancellation during capacity wait | `cancellation_during_pool_wait_releases_no_permit` | Pass |
| Response drop/failed body does not retain capacity | owned permit lives in `TimedConnection`; pool-pressure release and premature-body tests | Pass |
| Inline, env, named proxy resolution and failures | Rust `Config::resolve_account_proxy_url`; T001 resolution matrix; T004 pool construction tests | Pass |
| Mixed direct/proxied topology and account isolation | T004 topology tests; `proxied_accounts_keep_separate_pools_even_with_identical_proxy_uris` | Pass |
| Proxy cannot bypass direct target on failure | `socks5_auth_preserves_domain_target_and_rejection_is_not_direct`; malformed construction test | Pass |
| HTTP CONNECT and authenticated CONNECT | `http_connect_proxy_preserves_target_and_request_response`; `authenticated_http_connect_preserves_provider_tls_verification` | Pass |
| SOCKS target DNS semantics and auth | `socks5_auth_preserves_domain_target_and_rejection_is_not_direct` | Pass |
| Required `__` chain | `http_then_socks5_chain_preserves_both_hops` | Pass |
| Mandatory encrypted/legacy/SSH corpus construction | `mandatory_proxy_corpus_uri_families_construct`; corpus feature assertions | Pass; runtime peers remain an explicit fixture limitation |
| Secret absence in errors, snapshots, fixtures, and proxy headers | T001 structural observation/redaction tests; Rust malformed-proxy, pool snapshot, and authenticated CONNECT header tests | Pass |
| No transport-level automatic retry | explicit `retry_canceled_requests(false)` in `ProviderHttpClient::build`; redirect regression | Pass |

## Differential and proxy corpus results

The reusable migration observations remain structural: request method/path,
header names, body length, response status/body, proxy target authority,
SOCKS address kind, authentication outcome, network path, stable failure
category/stage, and redacted proxy endpoint. Socket IDs, timing jitter,
library error wording, and generated certificate details are normalized only
where T001 permits it. Status, target, request body/path, proxy-vs-direct
identity, DNS mode, error stage/category, retry behavior, and secret markers
are not normalized.

| Corpus result | Rows |
|---|---|
| Python and Rust construction/runtime qualified | `direct`, `http-connect`, `http-connect-auth-fragment`, `socks4`, `socks5`, `socks5-auth-fragment`, `http-socks5-chain` |
| Rust construction qualified; deterministic protocol peer not bundled | `shadowsocks-aead`, `ssr-legacy-cipher`, `trojan`, `ssh` |
| Documentation/product drift, not Rust cutover requirements | `http-connect-userinfo-auth`, `socks4a`, `socks5h`, `http-plus-socks5` |

The T001 Python oracle was rerun with the optional proxy dependency. The Rust
fixtures independently observe the same non-normalizable facts and do not
race the Python fixtures or share mutable state. No live provider, external
proxy, API key, or real proxy credential was used.

## Dependency and footprint review

The final provider stack is Hyper 1 + hyper-util legacy client/HTTP1 + Rustls
0.23 + Eggress 1.0.2. `eggress-embed` is pinned and uses
`default-features = false` with exactly `common`, `pproxy-compat`, `extended`,
`pproxy-legacy`, `legacy-crypto`, and `ssh`. `operations`, `reverse`, and
`quic` are not enabled. No Reqwest, second TLS stack, listener, reverse
proxy, or QUIC transport was added.

Cargo's package graph necessarily includes Eggress's `eggress-server`,
`eggress-runtime`, and `eggress-udp` packages because `eggress-embed` 1.0.2
declares them as unconditional dependency packages. Their listener,
operations, reverse, and QUIC functionality remains feature-disabled; this
upstream packaging footprint is accepted for the M4 SBC target because the
provider integration uses only `OutboundConnector` and no listener lifecycle.

## Ownership and handoff

M5/M7 may obtain a selected account client from
`ProviderClientPool::get_client(provider_id, account_name)`. A missing account
client falls back only to the provider direct client when the account has no
dedicated proxy client; configured proxy accounts are represented by dedicated
clients and never fall back to direct transport. `ProviderHttpClient::send`
takes a neutral method, provider-relative target, headers, and finite body and
returns raw status/headers plus the incremental `ProviderBody` stream.

Transport exposes stable categories for configuration, proxy configuration,
pool wait, connect/proxy/TLS, write, read, protocol, cancellation, and request
body-limit failures. M4 owns connection admission, connect/read/write/pool
timeouts, TLS setup, proxy establishment, pooling, and stream ownership. It
does not retry, fail over, route, score accounts, classify health, persist
attempts, inject credentials, translate wire protocols, or finalize requests.

## Verification evidence

Commands actually completed successfully:

- `cargo fmt --manifest-path rust/Cargo.toml -- --check`;
- `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`;
- `cargo test --manifest-path rust/Cargo.toml -- --test-threads=1` — 49 passed;
- `uv run --extra proxy pytest tests/migration_rs -q --tb=short --maxfail=1` — 49 passed;
- `uv run --extra proxy pytest tests/unit/test_provider_client_pool.py tests/unit/test_pproxy_transport.py tests/unit/test_config.py -q --tb=short --maxfail=1` — 139 passed;
- `uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed;
- `cargo tree --manifest-path rust/Cargo.toml -e features` and targeted inverse trees for Eggress feature review;
- `rustup run 1.85.0-aarch64-apple-darwin cargo check --manifest-path rust/Cargo.toml` — passed; and
- `git diff --check`.

The declared Rust 1.85 toolchain was installed locally for the MSRV check; no
repository toolchain or lockfile change was required.

## Security, lifecycle, and findings

- No synthetic secret marker appeared in tested `Display`/`Debug` errors,
  snapshots, proxy observations, or provider-facing proxy CONNECT headers.
- Cancellation drops pending semaphore acquisition and connection wrappers;
  the permit is owned by the physical connection and returns on drop.
- Direct and proxied pools are distinct, including two account pools using an
  identical proxy URI. Shutdown is drop-driven and bounded by the fixture
  ownership scopes; no background Eggress listener or shutdown task exists.
- The encrypted/legacy/SSH runtime rows have construction evidence only. They
  are a known test-fixture limitation inherited from T001/T003, not an
  unresolved M4 correctness finding or a claim of live interoperability.
- Unresolved high findings: none. Unresolved medium findings: none.
- Unresolved low findings: none within M4. Future protocol-peer
  interoperability testing remains appropriate before any product cutover
  claims for those optional proxy families.

## Future-plan state

T005 is closed and M4 is removed from the active provider-transport queue.
M5 catalog/account registry/routing/quota/health planning is unblocked because
the complete T001-T005 hard sequence now has accepted closure records and a
stable provider-client/error handoff. No M5 implementation plan exists in the
repository yet, so no nonexistent plan is marked dependency-ready.

M6 transcoding/SSE, M7 coordinator/finalization, M8 runtime generations, M9
operational lifecycle, M10 qualification, M11 cutover, and M12 Python
retirement remain sequenced behind their independent hard dependencies. T005
does not unblock those milestones directly.
