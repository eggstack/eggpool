# Provider Transport Subsystem Roadmap

Status: corrective pass active after T005 post-closure review

Repository baseline: `1ae7539bbda741ebcac660d535d6e58e6360eae6`

Canonical source:

- `../000-long-term-specification.md`
- `../001-terminology-and-domain-model.md`
- `../002-long-term-roadmap.md#M4--provider-http-stack-and-eggress-outbound-proxy-integration`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

## 1. Purpose and ownership

This subsystem ports the provider-facing HTTP transport boundary from Python/HTTPX/httpcore/pproxy to Rust/Hyper/hyper-util/Rustls/Eggress while preserving the current EggPool provider/account connection model.

It owns provider HTTP connection construction, connection reuse and limits, direct TLS transport, per-account proxy transport, proxy URI resolution, transport-level timeout/error semantics, transport diagnostics, and deterministic qualification of direct and proxied connections.

It does not own provider selection, model catalog policy, account eligibility, quota/health, request codecs, upstream authentication/header construction, inference endpoint behavior, retry/failover, durable request finalization, rehash generations, daemon lifecycle, or the global non-provider `OutboundClientManager` equivalent.

## 2. Current Python contract

The Python oracle separates provider traffic from generic outbound networking. `ProviderClientPool` creates one long-lived default client per provider and a dedicated client only for accounts with a configured proxy. A direct account falls back to its provider client. Provider configuration supplies base URL, connect/read/write/pool timeout values, maximum connections, maximum keepalive connections, and keepalive expiry.

The pproxy integration is deliberately below HTTP/TLS semantics: `PProxyNetworkBackend.connect_tcp()` delegates TCP establishment to `pproxy.Connection(...).tcp_connect(host, port)`, after which httpcore/HTTPX owns TLS, HTTP framing, response streaming, pooling, and timeout/error mapping.

The public proxy contract is broader than HTTP/SOCKS. T001 froze mandatory rows covering working/documented provider-proxy behavior and selected Eggress features accordingly. A mandatory working Python form may not disappear silently during migration.

## 3. Rust target boundary

The Rust implementation uses Hyper/hyper-util for provider HTTP pooling and protocol handling, Rustls for TLS, and Eggress only for proxied TCP establishment. It does not start an Eggress listener and does not embed Python.

```text
ProviderClientPool
  -> ProviderHttpClient
       -> Hyper/hyper-util connection pool
            -> admission/timeout connector wrapper
                 -> direct TCP connector
                 OR
                 -> Eggress OutboundConnector
            -> Rustls for HTTPS
```

The same HTTP/TLS layer sits above direct and Eggress-backed TCP streams so proxying does not create a second HTTP implementation.

## 4. Invariants

- Python remains production until migration cutover.
- no provider request constructs a fresh client on the hot path;
- one provider client exists per provider generation; only proxied accounts get a dedicated account client;
- direct accounts on the same provider reuse the provider client;
- proxy configuration is account-scoped exactly as today;
- unsupported, malformed, or failed proxy expressions fail closed and never fall back to direct;
- proxy/API credentials are absent from logs, errors, snapshots, and fixtures;
- ambient proxy environment variables do not redirect direct provider clients;
- HTTP/1.1 remains the migration compatibility baseline unless oracle evidence requires otherwise;
- redirects remain disabled according to the frozen provider contract;
- response bodies remain stream-capable for later M6/M7 work;
- connection limits/timeouts are bounded and cancellation releases connection/admission ownership;
- Eggress uses `default-features = false`; enabled features are justified by the EggPool-specific compatibility corpus;
- mandatory proxy corpus rows require runtime evidence unless an accepted ADR explicitly approves a supported difference;
- no provider routing, retry, codec, or finalization semantics are added in this subsystem.

## 5. Dependency graph

```text
F001-F006 closed
      |
      v
T001 transport contract + fixture freeze (closed)
      |
      v
T002 direct Hyper/Rustls provider core (closed)
      |
      v
T003 Eggress connector + proxy parity (closed historically)
      |
      v
T004 provider/account client pool (closed)
      |
      v
T005 differential qualification + initial M4 closure (closed historically)
      |
      v
T006 extended proxy runtime interoperability corrective closure (ready)
      |
      v
M5 implementation planning may become dependency-ready
```

T001-T005 have closure records and remain useful evidence. Independent post-T005 review found that the mandatory `shadowsocks-aead`, `ssr-legacy-cipher`, `trojan`, and `ssh` rows were construction-qualified only even though T001/T003 require runtime evidence or an approved supported-difference decision. No ADR currently grants that waiver. M4 is therefore reopened only for T006.

M5 research/roadmap drafting may proceed, but no M5 implementation handoff is dependency-ready until T006 closes.

## 6. Milestones

### T001 — Provider transport contract and fixture freeze (closed)

Class: invariant/infrastructure

Froze direct/provider/account/proxy transport behavior, the EggPool-specific proxy capability corpus, exact-vs-semantic parity classes, deterministic fixtures, and the narrow Eggress feature set.

Closure: [T001](../closure/provider-transport/001-status.md).

### T002 — Direct Hyper/Rustls provider HTTP core (closed)

Class: infrastructure

Implemented direct provider HTTP using Hyper/hyper-util/Rustls with bounded connection reuse, provider timeout policy, streaming response ownership, cancellation cleanup, and stable transport errors.

Closure: [T002](../closure/provider-transport/002-status.md).

### T003 — Eggress connector and proxy parity (historically closed)

Class: infrastructure/capability

Added the narrow `eggress-embed` dependency, proxy resolution, Eggress-backed custom connector, fail-closed construction/runtime behavior, redacted diagnostics, and common HTTP/SOCKS runtime qualification.

Post-closure review: construction-only evidence for mandatory extended protocol families is insufficient under T001/T003 and is corrected by T006. The T003 closure record remains historical evidence rather than being rewritten.

Closure: [T003](../closure/provider-transport/003-status.md).

### T004 — Provider/account client pool and lifecycle boundary (closed)

Class: capability/invariant

Ported one default client per provider plus dedicated proxied-account clients, direct fallback only for non-proxied accounts, stable diagnostics, immutable topology, and bounded lifecycle ownership.

Closure: [T004](../closure/provider-transport/004-status.md).

### T005 — Provider transport differential qualification and initial M4 closure (historically closed)

Class: invariant

Qualified direct HTTP/TLS, pooling, timeout/cancellation, CONNECT/SOCKS, fail-closed proxy behavior, redaction, no automatic Hyper retry, and dependency footprint. It transparently recorded construction-only qualification for Shadowsocks/SSR/Trojan/SSH.

Post-closure review determined that this known fixture limitation conflicts with the frozen mandatory runtime criterion. T005 remains valid evidence for all covered rows but no longer represents final M4 closure by itself.

Closure: [T005](../closure/provider-transport/005-status.md).

### T006 — Extended proxy runtime interoperability closure (ready for handoff)

Class: invariant/corrective

Add bounded deterministic runtime peers or equivalent interoperability fixtures for every T001 mandatory extended proxy family, exercise them through EggPool's actual `ProviderHttpClient`/Eggress transport path, prove fail-closed/redaction/cancellation semantics, and re-run the T005 closure matrix.

Required current rows: Shadowsocks AEAD, SSR legacy path, Trojan, and SSH, subject to revalidation against the frozen T001 corpus.

Implementation plan: [T006](../implementation/provider-transport/006-extended-proxy-runtime-qualification.md).

Exit: every mandatory extended row has real runtime evidence or an explicit accepted ADR supported-difference decision; full T005 gates remain green; no unresolved high/medium M4 correctness issue remains.

## 7. Eggress feature policy

Current selected production feature set remains pinned and narrow: `common`, `pproxy-compat`, `extended`, `pproxy-legacy`, `legacy-crypto`, and `ssh` with `default-features = false`. T006 must prove the runtime need for the extended/legacy/SSH features already justified by T001 rather than expanding the feature set.

`operations`, `reverse`, and `quic` remain outside provider TCP transport. No Reqwest or second TLS stack is permitted.

If an Eggress defect prevents a mandatory row, correct/qualify Eggress first or approve an explicit supported difference. Do not add a second production proxy implementation inside EggPool.

## 8. Failure, cancellation, and resource semantics

Transport construction failures are local/configuration failures and happen before provider handoff. Connect/read/write/pool and proxy handshake failures remain distinguishable enough for later policy. A dropped/cancelled request must not permanently consume an admission permit or leave a live proxy stream. Closing/dropping pools must release idle connections boundedly.

M4 does not decide retryability or account suppression. It supplies stable transport evidence/errors to later M5/M7 logic.

## 9. Verification strategy

Use deterministic loopback fixtures and the migration oracle harness. T006 extends existing common-proxy tests with real extended-family peers rather than live provider/proxy dependencies. Required observations include actual proxy traversal, target authority, authentication result, target HTTP request/response, failure stage, direct-vs-proxy identity, secret absence, cancellation cleanup, and subsequent client recovery.

No broad load farm, browser matrix, live provider, or external proxy service is required.

## 10. Non-goals

- no inference route implementation;
- no model catalog/routing/quota/health work;
- no provider auth/wire codecs/SSE;
- no retry/failover/finalization;
- no global outbound manager;
- no runtime generation/rehash work;
- no Python pproxy removal;
- no Eggress listener, UDP provider transport, reverse proxy, transparent proxying, or system proxy mutation.

## 11. Subsystem closure condition

M4 may be declared fully closed only after T006 has accepted closure evidence. At that point T001-T006 together must prove that direct and all mandatory proxied provider transport paths are runtime-qualified (or explicitly approved differences), fail closed, preserve streaming/pooling/timeout/cancellation semantics, and retain the narrow dependency posture required for local/SBC deployment.

Closing M4 still does not mean inference dispatch works; it means the transport beneath M5-M7 is stable enough to become a hard dependency.
