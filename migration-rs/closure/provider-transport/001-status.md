# T001 Closure — Provider Transport Contract and Fixture Freeze

Status: closed

Recommendation: closed; T002 is dependency-ready.  T003 remains queued behind
T002, with its required Eggress feature decision now available.

Implementation commit: [`50d7ff4`](https://github.com/eggstack/eggpool/commit/50d7ff4)

Plan: [T001 — contract and fixture freeze](../../implementation/provider-transport/001-contract-and-fixture-freeze.md)

Contract: [provider transport contract](../../provider-transport-contract.md)

## Outcome

T001 froze the provider transport boundary without adding a Rust production
dependency or implementing the Rust HTTP stack.  The repository now contains
the direct/provider/account contract, a machine-readable pproxy corpus, local
HTTP/TLS/HTTP CONNECT/SOCKS5 fixtures, stable error observations, and focused
oracle tests for the boundary.

The current Python implementation had one canonical-invariant omission:
`httpx.AsyncClient` would otherwise inherit ambient proxy/certificate
environment variables.  Provider client construction now explicitly uses
`trust_env=False`, making the roadmap’s no-ambient-proxy rule executable while
leaving explicit per-account pproxy configuration unchanged.

The corpus also records and corrects three documentation drifts: HTTP/SOCKS
proxy credentials use a URI fragment rather than userinfo, `socks5h://` is not
accepted by pproxy 2.7.9, and `socks4a://` is not accepted as a standalone
pproxy client URI.  No working Python form was removed.

## Requirement matrix

| Requirement | Evidence | Result |
|---|---|---|
| Direct URL/path, HTTP/1.1, redirect, header, timeout, limit, keepalive, stream, cancellation, and close contract | `migration-rs/provider-transport-contract.md`; `client_pool.py`; focused tests | Pass |
| Effective provider read guardrail | `ProviderStreamTimeoutConfig.transport_read_timeout`; T001 assertion for 40-second maximum | Pass |
| No ambient process proxy/certificate inheritance for direct clients | `_build_client(..., trust_env=False)` and focused assertion | Pass |
| Provider/account topology and diagnostic snapshot | Existing pool tests plus `test_provider_pool_topology_and_snapshot_are_contractual` | Pass |
| Exact proxy resolution precedence/failures | `test_proxy_resolution_precedence_and_trimming`, environment matrix, mutual-exclusion/unknown-name tests | Pass |
| HTTP/1.1 finite/chunked/delayed/malformed/early-close fixture behavior | `RecordingHTTPServer` and delayed/protocol failure test | Pass |
| Verified local HTTPS observation | committed synthetic localhost certificate/key, `CERT_REQUIRED` context, direct HTTPS test | Pass |
| HTTP CONNECT and authenticated CONNECT observation | `HTTPConnectProxy`, target/header/auth observations, Python pproxy test | Pass |
| SOCKS5 auth and DNS target observation | `SOCKS5Proxy`, domain target and auth assertions, Python pproxy test | Pass |
| Configured proxy cannot fall back to direct | closed proxy test confirms upstream received no request | Pass |
| Stable error vocabulary preserves class/stage/path and redacts secrets | `TransportErrorObservation`, normalization and unique-marker assertions | Pass |
| Machine-readable capability corpus | `migration-rs/fixtures/provider-transport/proxy-capability-corpus.json` and schema/feature-justification test | Pass |
| Every enabled Eggress feature justified by a mandatory row | Corpus test; feature decision includes common, pproxy-compat, extended, pproxy-legacy, legacy-crypto, ssh | Pass |
| No unrelated Eggress features | Corpus explicitly rejects operations, reverse, and quic | Pass |
| Rust production dependency delta remains empty | `rust/Cargo.toml` unchanged by implementation commit | Pass |

## Proxy capability and Eggress decision

The exact corpus is the authoritative row-by-row record.  Mandatory working
Python rows are direct, HTTP CONNECT, authenticated HTTP CONNECT, SOCKS4,
SOCKS5, authenticated SOCKS5, `__` composition, modern Shadowsocks, SSR with
the legacy pproxy cipher surface, documented Trojan, and optional SSH.

The T003 dependency proposal is:

```toml
eggress-embed = { version = "=1.0.2", default-features = false, features = [
  "common", "pproxy-compat", "extended", "pproxy-legacy", "legacy-crypto", "ssh",
] }
```

The inspected release is tag `v1.0.2` at commit
`e76c8d480f411802ac5592e04655a07212be98b5`.  The lean build and upstream
compatibility parser tests passed with the selected feature set.  Eggress
runtime traffic is intentionally T003 evidence; this closure does not claim
that T003 connector runtime work is complete.

`operations`, `reverse`, and `quic` are explicitly excluded because they do
not establish provider TCP transport.  If T003 cannot execute a mandatory
Python row, it must stop and create a corrective plan or ADR; it may not drop
the row or fall back to direct networking.

## Verification evidence

Completed successfully:

- `rtk uv run pytest tests/migration_rs/test_t001_provider_transport.py -q --tb=short --maxfail=1` — 18 passed;
- `rtk uv run --extra proxy pytest tests/migration_rs/test_t001_provider_transport.py -q --tb=short --maxfail=1` — 18 passed;
- `rtk uv run --extra proxy pytest tests/unit/test_provider_client_pool.py tests/unit/test_pproxy_transport.py tests/unit/test_config.py -q --tb=short --maxfail=1` — 139 passed;
- `rtk cargo test -p eggress-embed --manifest-path /tmp/eggress-t001.no89vJ/Cargo.toml --no-default-features --features common,pproxy-compat,extended,pproxy-legacy,legacy-crypto,ssh --lib` — 7 passed;
- `rtk cargo test -p eggress-pproxy-compat --manifest-path /tmp/eggress-t001.no89vJ/Cargo.toml --features legacy-crypto,ssh` — 357 passed;
- `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 49 passed;
- `rtk uv run --extra proxy pytest tests/migration_rs -q --tb=short --maxfail=1` — 49 passed;
- `rtk cargo fmt --manifest-path rust/Cargo.toml -- --check` — passed;
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` — passed;
- `rtk cargo test --manifest-path rust/Cargo.toml` — 19 passed;
- `rtk openssl x509 -in tests/migration_rs/fixtures/tls/localhost.crt -noout -subject -ext subjectAltName` — localhost DNS/IP SANs verified;
- `rtk openssl pkey -in tests/migration_rs/fixtures/tls/localhost.key -noout` — synthetic test key verified;
- focused Ruff format/check for changed Python files; and
- `rtk git diff --cached --check` before implementation commit.

The full migration harness, Rust foundation gates, and repository smoke gate
are run again before the closure-state commit and are recorded below if their
results differ.  No live provider credentials or live external proxy were
used.

## Security, lifecycle, and known limitations

- Fixture servers record method/path/header names/body length/target identity,
  never request bodies, API keys, proxy passwords, cipher keys, or raw URI
  userinfo.
- TLS tests trust only the committed synthetic localhost certificate and keep
  hostname verification enabled; no production verification setting is
  weakened.
- Configured proxies are constructed only for explicit account proxy rows;
  proxy connection failure is terminal for that path and cannot select the
  provider direct client.
- HTTPX wording, generated connection IDs, timing jitter, and socket numbers
  are not compared.  Category, timeout stage, direct/proxied path, target
  evidence, and secret absence remain non-normalizable.
- Write backpressure and long connect stalls are deferred until T002/T003 can
  exercise them deterministically without timing-dependent tests.
- Eggress connector runtime, Rust Hyper/Rustls, and proxy-pool integration are
  not implemented in T001.

## Future-plan state

T001’s hard dependencies F001-F006 were already closed.  T001 is now closed,
so T002 is unblocked and moved to dependency-ready in the registry and provider
transport handoff index.  T003 remains blocked on T002 but no longer lacks the
T001 feature decision.  T004 remains blocked on T003, and T005 remains blocked
on T004.  M5 and later work remain blocked on M4/T005 as intended; no other
future plan can be safely unblocked by T001 alone.
