# T002 Closure — Direct Hyper/Rustls Provider HTTP Core

Status: closed

Recommendation: closed; T003 is dependency-ready. T004 remains queued behind
T003, T005 remains queued behind T004, and M5+ remains blocked on M4/T005.

Implementation commits: [`c9f448a`](https://github.com/eggstack/eggpool/commit/c9f448a), [`2696e52`](https://github.com/eggstack/eggpool/commit/2696e52)

Plan: [T002 — direct Hyper/Rustls provider HTTP core](../../implementation/provider-transport/002-direct-hyper-rustls-core.md)

Contract: [provider transport contract](../../provider-transport-contract.md)

## Outcome

T002 implements the neutral, direct provider HTTP boundary under
`rust/src/providers/`. `ProviderHttpClient` is a cheap cloneable handle around
one Hyper legacy client configured for HTTP/1.1 only. `ProviderHttpConfig`
converts the existing Rust provider settings, including the T001 effective
read guardrail. `ProviderBody` exposes incremental response chunks without
eager buffering, and `TransportError` provides stable pool/connect/TLS,
write/read, protocol, cancellation, and request-bound categories without
retaining or displaying source errors, headers, bodies, or credentials.

Physical connection admission is a semaphore acquired only when Hyper needs a
new connection. The owned permit lives inside the connection wrapper, so it
survives idle pooling and drops when the physical connection is discarded.
Hyper's per-authority idle count and idle timer enforce the keepalive bounds.
The connector applies explicit pool, connect, read-inactivity, and
write-inactivity timers; cancellation drops the pending acquisition or
connection and does not leak capacity. Rustls uses Mozilla webpki roots by
default, with additional DER roots accepted only through an explicit
constructor setting used by the deterministic TLS fixture.

No Reqwest, HTTP/2, ambient proxy handling, provider authentication, routing,
retry policy, inference dispatch, or Eggress dependency was added.

## Requirement matrix

| Requirement | Evidence | Result |
|---|---|---|
| Narrow Hyper/Rustls dependency set, no second HTTP/TLS stack | `rust/Cargo.toml`; `hyper`, `hyper-util` client-legacy/http1/tokio, `hyper-rustls` http1/ring/tls12/webpki-tokio, direct `rustls`; no Reqwest | Pass |
| Neutral method/path/header/body API and safe base joining | `ProviderHttpClient::send`; `join_provider_target`; unit tests reject absolute and authority-changing targets | Pass |
| Direct HTTP exact request/response observations | `rust/tests/provider_transport.rs::direct_http_preserves_request_shape_and_reuses_http11_connection` | Pass |
| HTTP/1.1 reuse and bounded idle retention/expiry | reuse test plus `idle_keepalive_expiry_forces_a_new_physical_connection`; Hyper HTTP/1-only builder settings | Pass |
| Physical connection admission and pool wait timeout | `AdmissionConnector`; `pool_pressure_times_out_without_leaking_capacity` | Pass |
| Connect failure classification and permit cleanup | `refused_connection_is_classified_as_connect_failure`; owned permit drops on connector error | Pass |
| Connect timeout, request-write, and response-read guardrails | connector `tokio::time::timeout`; `TimedConnection` read/write inactivity wrappers; delayed-header read-timeout test | Pass; deterministic long-stall/write-backpressure fixtures remain deferred by T001 |
| Stream-capable response ownership and protocol failure | `ProviderBody::next`; chunked incremental test; premature close maps to `Protocol` | Pass |
| TLS trust roots and hostname verification | generated CA/leaf loopback fixture; HTTPS success and hostname-mismatch `Tls` assertion | Pass |
| Cancellation does not consume capacity | cancelled pool-wait task followed by successful request; connection wrapper owns permit for drop cleanup | Pass |
| Direct path ignores ambient proxy variables | connector is constructed directly from Hyper/Rustls and never reads proxy environment; T001 Python oracle explicitly freezes `trust_env=False` | Pass |
| Secret-safe diagnostics | unit/integration redaction assertions; unit-bearing `TransportError`; `ProviderHttpClient` debug prints only safe authority from validated config | Pass |
| Lifecycle boundary | cloneable shared Hyper client; drop-driven pool ownership is documented; no background task or process-wide pool introduced | Pass |
| No routing/provider policy in transport | module contains no auth, account selection, retry, health, quota, or finalization code | Pass |

## Differential verification

The Python oracle remained unchanged and was re-run against the frozen T001
contract. Rust and Python therefore compare the same exact owned facts for
method, path/query, request body, status, relevant response body bytes, HTTP
version baseline, direct path, TLS hostname behavior, connection reuse,
keepalive expiry, timeout stage, and secret absence. Hyper-specific header
defaults, error wording, connection IDs, and timing jitter remain semantic or
incidental as permitted by T001. No redirect, HTTP/2 upgrade, or ambient
proxy behavior is normalized away.

No live provider traffic or external proxy was used.

## Verification evidence

Completed successfully:

- `rtk cargo fmt --manifest-path rust/Cargo.toml -- --check` — passed;
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` — passed;
- `rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1` — 10 passed;
- `rtk cargo test --manifest-path rust/Cargo.toml` — 32 passed;
- `rtk uv sync --frozen --extra ci` — passed;
- `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 46 passed, 3 skipped;
- `rtk uv run --extra proxy pytest tests/migration_rs/test_t001_provider_transport.py tests/unit/test_provider_client_pool.py tests/unit/test_pproxy_transport.py -q --tb=short --maxfail=1` — 40 passed;
- `rtk uv run ruff format --check src/ tests/ scripts/` — passed;
- `rtk uv run ruff check src/ tests/ scripts/` — passed;
- `rtk uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations;
- `rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed.

## Security, lifecycle, and known limitations

- Transport errors intentionally expose only stable category text. Underlying
  Hyper, Rustls, socket, request, and response details are not included in
  `TransportError` display output.
- The fixture records request structure and body bytes only inside the local
  test assertion; production transport does not log or persist them.
- TLS verification remains enabled. The test client adds a generated CA via
  the explicit DER-root setting; production defaults use webpki roots.
- T001 explicitly deferred deterministic long connect-stall and write-
  backpressure fixtures. The corresponding connect/write timers are
  implemented; deterministic stage tests for those cases remain appropriate
  follow-up coverage if a stable fixture is available.
- Eggress/proxy transport, provider/account topology, inference dispatch,
  retries, and lifecycle generations remain downstream work and are not
  implied by this closure.

Unresolved mandatory findings: none.

## Future-plan state

T002 is closed because its direct HTTP/HTTPS acceptance criteria and resource
ownership requirements pass against the T001 contract. T003 is moved from
queued/blocked to dependency-ready: T001's Eggress feature decision is already
closed and T002 now supplies the common HTTP/TLS layer. T004 remains queued on
T003, T005 remains queued on T004, and no M5+ plan is safely unblocked by T002
alone.
