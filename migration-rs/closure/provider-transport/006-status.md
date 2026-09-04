# T006 Closure — Extended Proxy Runtime Interoperability

Status: closed

Recommendation: closed; M4 is closed after the corrective pass and M5
planning/implementation handoff work is unblocked. M6-M12 remain sequenced
behind their independent dependencies.

Implementation commit: [`4b3a95a`](https://github.com/eggstack/eggpool/commit/4b3a95a)

Plan: [T006 — extended proxy runtime interoperability closure](../../implementation/provider-transport/006-extended-proxy-runtime-qualification.md)

Contract: [provider transport contract](../../provider-transport-contract.md)

## Outcome

T006 closes the post-T005 evidence gap. Every mandatory extended proxy corpus
row now has deterministic runtime evidence through EggPool's
`ProviderHttpClient`, Hyper/hyper-util, and Eggress outbound transport boundary.
The transport remains fail-closed: a configured proxy failure cannot fall back
to direct target networking, and the transport does not add request retries.

The implementation keeps the frozen Eggress feature set. For SSH-bearing proxy
chains, EggPool uses Eggress's public chain executor with a session cache. This
qualifies the production `ProviderHttpClient::new_with_proxy` path while
working around Eggress 1.0.2's convenience constructor, which creates its
executor without an SSH session cache. No proxy protocol was reimplemented in
EggPool.

## Runtime evidence matrix

| Mandatory corpus row | Runtime evidence | Result |
|---|---|---|
| `shadowsocks-aead` | `extended_encrypted_proxies_reach_the_target_through_provider_transport`; local AES-256-GCM peer records target authority and relays an HTTP request/response | Pass |
| `ssr-legacy-cipher` | Same test exercises the frozen SSR URI through the local SSR legacy peer and records target authority and response | Pass |
| `trojan` | `trojan_proxy_reaches_the_target_with_test_only_proxy_root`; local TLS peer verifies the Trojan password and relays HTTP through the test-only proxy-root constructor | Pass |
| `ssh` | `ssh_proxy_reaches_the_target_with_the_eggress_compatibility_policy`; local OpenSSH peer accepts the generated key and the production `new_with_proxy` path relays HTTP | Pass |

The frozen SSR form carries no password-negotiation field in the selected
Eggress compatibility path, so its evidence covers legacy framing, target
authority, and relay success. Wrong-auth coverage is tested for Shadowsocks,
Trojan, and SSH where the selected form permits credential rejection.

## Failure, redaction, and lifecycle evidence

- `extended_encrypted_proxy_auth_failure_is_redacted_and_fail_closed`,
  `trojan_auth_failure_is_redacted_and_cannot_fall_back_direct`, and
  `ssh_auth_failure_is_redacted_and_cannot_fall_back_direct` prove wrong
  credentials fail in the proxy layer, do not reach the target, and do not
  expose synthetic secrets or key paths in EggPool errors.
- `extended_encrypted_proxy_cancellation_recovers_through_same_client` and
  `ssh_cancellation_does_not_poison_the_provider_client` cancel a proxy
  connection attempt and then complete a later request through the same client.
  Admission ownership remains RAII-bound to the physical connection.
- Existing T005 tests continue to cover direct/proxied pool separation,
  pooling, timeout categories, request streaming, fail-closed common proxy
  behavior, and no implicit Hyper request retry. Multi-connection proxy
  fixtures now run relay workers concurrently so pool isolation remains
  observable under the complete feature-enabled suite.
- Proxy observations retain only target authority, handshake counts, request
  paths, and response facts. Synthetic passwords, userinfo, private-key paths,
  and provider authorization markers are absent from supported errors and
  observations. Trojan's synthetic CA is scoped to the test-only constructor;
  production trust roots are unchanged.

Eggress's SSH compatibility policy intentionally leaves host-key verification
disabled in the inspected 1.0.2 transport implementation. The fixture uses a
generated host key and records this as an explicit compatibility property; it
does not silently add a verification bypass to EggPool.

## Corpus and dependency results

`migration-rs/fixtures/provider-transport/proxy-capability-corpus.json` now
marks the seven T005 runtime rows and all four T006 extended rows as runtime
passes. `http-connect-userinfo-auth`, `socks4a`, `socks5h`, and
`http-plus-socks5` remain documented/product-drift rows and are not promoted
to Rust requirements.

The production dependency remains `eggress-embed = 1.0.2` with
`default-features = false` and exactly `common`, `pproxy-compat`, `extended`,
`pproxy-legacy`, `legacy-crypto`, and `ssh`. The direct Eggress config,
compatibility, server, URI, and SSH crates are the pinned public building
blocks used for SSH cache-aware construction; no `operations`, `reverse`, or
`quic` feature, Reqwest dependency, second TLS stack, listener, or external
proxy service was added.

## Verification evidence

Commands completed successfully:

- `cargo fmt --manifest-path rust/Cargo.toml -- --check`;
- `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`;
- the same Clippy command with `--features test-support`;
- `cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1` — 52 passed;
- the same Rust suite with `--features test-support` — 57 passed;
- `uv run --extra proxy pytest tests/migration_rs -q --tb=short --maxfail=1` — 49 passed;
- targeted provider-pool, pproxy-transport, and config tests — 139 passed;
- `cargo check` with and without `test-support` on all targets;
- `cargo tree --manifest-path rust/Cargo.toml -e features` review; and
- `git diff --check`.

The declared Rust 1.85 MSRV check remains part of the T005 evidence and is
unchanged by this corrective pass. No live provider, API key, paid proxy,
Docker service, or external network was used.

## Downstream state

T006 moves from dependency-ready to completed. The provider-transport roadmap
and handoff sequence are closed after corrective pass, and M5 planning and
implementation handoff work is explicitly unblocked. No M5 implementation
plan exists yet, so no nonexistent plan is marked dependency-ready. M6
transcoding/SSE, M7 coordinator/finalization, M8 runtime generations, M9
operational lifecycle, M10 qualification, M11 cutover, and M12 Python
retirement remain blocked/sequenced behind their independent hard dependencies.
