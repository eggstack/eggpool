# Provider Transport T006 — Extended Proxy Runtime Interoperability Closure

Status: ready for handoff

Repository baseline: `1ae7539bbda741ebcac660d535d6e58e6360eae6`

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md#t006--extended-proxy-runtime-interoperability-closure`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

Primary class: invariant/corrective

## 1. Objective

Close the single post-T005 qualification gap found by independent review: the frozen T001 provider-transport contract and T003 implementation plan require runtime evidence for mandatory proxy corpus rows, but the Shadowsocks, SSR, Trojan, and SSH rows were closed with connector-construction evidence only because deterministic protocol peers were not bundled in EggPool.

T006 is a corrective interoperability pass. It must either produce bounded end-to-end runtime evidence for every mandatory extended proxy family or record an explicit approved supported-difference decision through the normal ADR process. Construction-only success is not sufficient for a mandatory row.

Do not reopen the already-qualified direct HTTP/TLS core, HTTP CONNECT/SOCKS behavior, provider/account pool topology, or transport error ownership except where a regression is exposed by the new runtime tests.

## 2. Why M4 is reopened

T001 froze a compatibility corpus so a working Python proxy form could not disappear silently during migration. T003 then required every mandatory T001 row to be passing or covered by an approved supported-difference decision and called for runtime protocol-family cases. T005 correctly recorded that `shadowsocks-aead`, `ssr-legacy-cipher`, `trojan`, and `ssh` had Rust construction qualification only.

That limitation is transparent, but transparency is not the same as satisfying the frozen acceptance criteria. No accepted ADR currently reclassifies these mandatory rows as construction-only. M4 therefore remains functionally strong but is not fully closed against its own contract until this evidence gap is resolved.

## 3. Scope

T006 owns only:

- deterministic local runtime peers or equivalent bounded interoperability fixtures for mandatory extended proxy families;
- end-to-end transport observations through EggPool's actual Rust `ProviderHttpClient` and Eggress connector;
- fail-closed, redaction, cancellation, and capacity-release assertions for those paths;
- any narrowly required Eggress fixture/test-only integration;
- amendment/re-run of the T005 contract-to-evidence matrix;
- final M4 closure decision.

T006 does not own:

- provider selection, catalog policy, routing, quota, health, or model routers;
- OpenAI/Anthropic/Gemini request codecs or SSE;
- retry/failover or durable finalization;
- runtime generations/rehash;
- production listener modes or generic outbound networking;
- new proxy protocols not already mandatory in the T001 corpus;
- live paid-provider qualification.

## 4. Mandatory runtime rows

Re-read `migration-rs/provider-transport-contract.md` and its machine-readable corpus before implementation. At the current baseline, the unresolved mandatory runtime families are:

- Shadowsocks AEAD;
- SSR legacy-cipher/plugin path represented by the frozen mandatory corpus;
- Trojan;
- SSH.

If the T001 corpus has multiple mandatory variants within a family, cover each distinct runtime behavior that justifies a separate row. Do not broaden the requirement from documented-but-nonworking rows that T001 already classified as product/documentation drift.

If repository evidence shows one of the four rows was incorrectly marked mandatory in T001, do not silently edit the corpus. Document the oracle evidence and use the planning/ADR process to change the contract explicitly.

## 5. Fixture strategy

Prefer the smallest deterministic fixture strategy that exercises the real production connector path.

First inspect Eggress 1.0.2's existing protocol tests, testkit, and public/test-only helpers for Shadowsocks, SSR, Trojan, and SSH. Reuse them only when that can be done without turning EggPool production code into an Eggress listener/runtime integration or adding broad production dependencies solely for tests.

Acceptable approaches, in preference order:

1. consume a stable Eggress test helper as a dev/test-only dependency if it cleanly exposes a protocol peer;
2. instantiate the minimal Eggress protocol/server components inside `rust/tests` behind dev/test dependencies;
3. implement a small local peer fixture in EggPool tests when the protocol surface required for the frozen row is narrow and doing so is materially simpler;
4. for SSH, use a deterministic local test server/key strategy with no host-network or external-service dependency.

Do not spawn arbitrary external daemons, Docker containers, or live internet proxies as a normal test prerequisite. Do not weaken production TLS/SSH verification to make fixtures pass.

## 6. Required end-to-end path

Each mandatory row must exercise the same boundary later provider traffic will use:

```text
ProviderHttpClient
  -> Hyper/hyper-util HTTP/1.1 pool
  -> Rustls when target is HTTPS
  -> Eggress-backed connector
  -> deterministic proxy protocol peer
  -> deterministic HTTP/HTTPS target
  -> incremental ProviderBody response
```

Calling `OutboundConnector::from_pproxy_uri()` successfully is useful construction coverage but does not satisfy this section.

The fixture must observe enough non-secret facts to prove the configured proxy was actually traversed: target authority, protocol/auth outcome, proxy connection count or equivalent path evidence, and the target HTTP request/response.

## 7. Per-family qualification requirements

For each mandatory family, provide at minimum:

- one successful TCP tunnel carrying a real HTTP request/response through EggPool's provider client;
- the configured target authority observed by the proxy peer;
- required authentication/key/password/cipher negotiation success;
- a deterministic wrong-secret or authentication/handshake failure when the protocol permits it;
- proof the failure does not create a direct connection to the target;
- operator-facing error/log capture with synthetic secrets absent;
- bounded cancellation or connection-abort coverage sufficient to prove the provider connection permit returns to baseline;
- successful subsequent request after a failed/cancelled attempt, proving no poisoned connector/client state.

For SSH, include the host-key policy actually selected by the T001 contract/Eggress integration. If Eggress's pproxy-compatible SSH behavior intentionally differs from Python host-key handling, it requires an explicit supported-difference decision rather than normalization.

For Shadowsocks/SSR/Trojan, use only methods/plugins/features already frozen as mandatory. Do not add unrelated legacy ciphers just because Eggress supports them.

## 8. Chain/composition coverage

T005 already qualifies the mandatory HTTP→SOCKS5 `__` chain. T006 does not need a combinatorial chain matrix.

However, if T001 marks an extended-family composition as mandatory, add one representative end-to-end case for that exact composition. The observation must prove every hop is traversed in order and that failure of an intermediate hop does not bypass to a later/direct target.

## 9. Fail-closed requirement

A configured extended proxy is a routing constraint. On parse, construction, DNS, connect, authentication, handshake, timeout, or runtime relay failure:

- the request must fail in the transport layer;
- no connection may be attempted directly to the provider target;
- no alternate proxy may be selected by M4;
- no request retry may be introduced by the transport;
- capacity/connection ownership must be released.

Instrument deterministic target fixtures so a direct-fallback regression is observable rather than inferred from the returned error.

## 10. Secret-redaction corpus

Use unique synthetic markers for:

- Shadowsocks password/key material;
- SSR password/cipher/plugin parameters where secret-bearing;
- Trojan password;
- SSH password/private-key/passphrase/userinfo where applicable;
- proxy URI fragment/userinfo authentication;
- provider Authorization/API-key headers used in the neutral target request.

Trigger construction and runtime failures. Assert marker absence from:

- `Display` and `Debug` error output exposed by EggPool;
- captured tracing/log output;
- test observation snapshots;
- provider pool/network diagnostic snapshots;
- panic output from supported error paths;
- closure evidence committed to the repository.

Do not require EggPool to sanitize arbitrary third-party panic text after an invariant-breaking bug; supported parse/runtime failures must remain non-panicking and redacted.

## 11. Cancellation, limits, and cleanup

Reuse the T002/T005 admission/capacity instrumentation where possible. For at least one encrypted family and SSH:

- cancel during proxy handshake/connect;
- verify the admission permit returns to baseline;
- verify fixture-observed connections converge after cleanup;
- issue a subsequent successful request through the same client;
- confirm no new client is constructed per attempt.

If the protocol peer has long-lived background tasks, ensure test teardown is explicitly bounded. No fixture task may leak beyond its test scope.

## 12. Dependency constraints

Production dependency policy remains unchanged unless evidence proves a mandatory row cannot execute with the currently selected features.

Expected production dependency remains pinned `eggress-embed = 1.0.2`, `default-features = false`, with only the already justified features (`common`, `pproxy-compat`, `extended`, `pproxy-legacy`, `legacy-crypto`, `ssh`).

Do not enable `operations`, `reverse`, or `quic`. Do not add Reqwest or another TLS stack. Dev-only test helpers are acceptable when narrowly scoped and documented.

If an Eggress defect prevents a mandatory runtime row, stop EggPool implementation work for that row and record the exact upstream defect. The preferred correction is to fix/qualify Eggress first, then resume T006. Do not reimplement a full proxy protocol in EggPool production code.

## 13. Python oracle usage

Where practical, run the same deterministic peer against Python `pproxy` and Rust Eggress and compare stable observations. Exact packetization, cipher implementation internals, timing, and library error strings are not parity requirements.

Required comparison facts include:

- whether the connection succeeds;
- target authority/DNS mode where applicable;
- authentication success/failure;
- target HTTP request shape/status/body;
- failure stage/category;
- direct-versus-proxy path;
- secret leakage.

If Python cannot execute a row that T001 currently marks mandatory, capture that evidence and correct the frozen contract through explicit review rather than manufacturing Rust-only parity work.

## 14. T005 requalification

After extended-family runtime tests pass, re-run the T005 qualification gates and amend the provider-transport closure state.

Required final evidence includes:

- updated contract-to-evidence matrix with runtime test references for every mandatory row;
- updated proxy corpus status distinguishing runtime-qualified, construction-only non-mandatory, and documented drift rows;
- final Eggress feature/dependency tree review;
- direct/proxy fail-closed and retry assertions still passing;
- no regression in T002/T004 pool/cancellation semantics;
- a new T006 closure record.

Do not delete the historical T005 closure record. If desired, append a clearly labeled post-closure-review note to it after T006 implementation so its historical decision and later correction remain traceable.

## 15. Verification

At minimum record successful execution of:

- `cargo fmt --manifest-path rust/Cargo.toml -- --check`;
- `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`;
- `cargo test --manifest-path rust/Cargo.toml -- --test-threads=1`;
- complete `tests/migration_rs` suite with the proxy extra enabled where required;
- targeted Python `test_provider_client_pool.py`, `test_pproxy_transport.py`, proxy/config tests, and new oracle fixtures;
- targeted feature build/check proving the selected Eggress features on the declared Rust 1.85 baseline;
- `cargo tree --manifest-path rust/Cargo.toml -e features` review;
- `git diff --check`.

No live API key, paid provider, broad GitHub Actions matrix, Docker matrix, or throughput benchmark is required.

## 16. Acceptance criteria

T006 closes only if all are true:

- every T001 mandatory extended proxy row has actual end-to-end runtime evidence through EggPool's production transport path, or an explicit accepted ADR supported-difference decision;
- each exercised proxy family proves the target was reached through the configured proxy path;
- configured proxy failure cannot fall back to direct;
- required auth/handshake failure cases are correctly classified and bounded;
- synthetic secrets do not appear in captured operator-facing evidence;
- cancellation/failure returns connection admission ownership to baseline;
- a later successful request works after failure/cancellation;
- no implicit HTTP request retry has reappeared;
- no unjustified Eggress/HTTP/TLS dependency expansion occurred;
- the full T005 matrix is re-run and no unresolved high/medium M4 correctness finding remains.

## 17. Stop conditions

Do not close M4 if:

- any mandatory row remains construction-only without an ADR;
- a proxy failure can reach the provider directly;
- a protocol requires disabling verification/security checks to pass;
- secret material is present in supported errors/logs;
- cancellation leaks a connection/admission permit;
- an Eggress defect is hidden by implementing an unrelated second proxy stack in EggPool;
- the T001 contract is weakened solely to avoid building deterministic evidence.

## 18. Closure and downstream state

On successful T006 closure:

- write `migration-rs/closure/provider-transport/006-status.md` with the runtime evidence matrix;
- mark the provider-transport roadmap closed after corrective pass;
- move T006 from dependency-ready to completed in the registry;
- explicitly re-unblock M5 planning/implementation handoff work.

Until then, M5 implementation plans must remain blocked. M5 roadmap research may continue, but it must not rely on M4 being fully closed.
