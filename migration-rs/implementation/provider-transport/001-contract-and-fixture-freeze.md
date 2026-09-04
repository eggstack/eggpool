# Provider Transport T001 — Contract and Fixture Freeze

Status: closed; see [closure record](../../closure/provider-transport/001-status.md)

Repository baseline: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md#t001--provider-transport-contract-and-fixture-freeze`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

Primary class: invariant/infrastructure

## 1. Objective

Freeze the provider transport behavior that the Rust implementation must reproduce before adding outbound HTTP dependencies. This milestone converts the current Python implementation, docs, tests, and Eggress compatibility evidence into a narrow executable contract for T002-T005.

The output must answer two questions with evidence:

1. what direct provider HTTP behavior is contractual for EggPool; and
2. which pproxy URI/protocol forms are actually part of EggPool's supported contract and therefore require Eggress features.

Do not implement the Rust provider HTTP stack in T001.

## 2. Dependencies

Hard: F001-F006 closed.

No M5+ subsystem is required.

## 3. Python oracle evidence to inspect

At minimum inspect current repository versions of:

- `src/eggpool/providers/client_pool.py`;
- `src/eggpool/providers/pproxy_transport.py`;
- `src/eggpool/providers/outbound.py` only to preserve the provider-vs-global-network boundary;
- `src/eggpool/models/config.py` proxy resolution and provider timeout models;
- `tests/unit/test_provider_client_pool.py`;
- `tests/unit/test_pproxy_transport.py` and any proxy/config tests;
- `docs/proxy.md`;
- provider/client-pool runtime diagnostic consumers where they define observable snapshot shape.

Inspect Eggress `eggress-embed` 1.0.2, its `OutboundConnector`, feature definitions, and maintained pproxy 2.7.9 compatibility matrix. Record the exact Eggress commit/release inspected.

## 4. Required contract inventory

Create a canonical migration document such as `migration-rs/provider-transport-contract.md` containing the following.

### A. Direct provider client behavior

Record:

- provider base URL/path joining behavior;
- HTTP protocol baseline;
- redirect behavior;
- default/request headers attributable to HTTPX versus EggPool-owned headers;
- connect/read/write/pool timeout inputs and the effective provider read guardrail derived from stream timeout settings;
- `max_connections`, `max_keepalive`, and keepalive expiry semantics;
- direct-account fallback to the provider client;
- response body streaming ownership and when a connection returns to the pool;
- connection, TLS, protocol, pool, read, and write failure categories visible to EggPool;
- cancellation/close behavior relevant to later retry/finalization;
- ambient environment behavior that must or must not be inherited (`HTTP_PROXY`, `HTTPS_PROXY`, certificate variables, etc.).

Classify each item as exact parity, semantic parity, incidental HTTP-library behavior, or explicitly deferred.

### B. Provider/account pool topology

Freeze:

- one default client per provider;
- one account-specific client only for accounts whose resolved proxy is non-null;
- proxied account never falls through to default direct client;
- direct account uses provider client;
- missing provider behavior;
- observable snapshot fields and ordering;
- displaced/re-registration semantics: determine whether this is a true runtime contract or Python implementation detail that Rust generation replacement can avoid.

### C. Proxy configuration resolution

Record exact resolution precedence and failures for:

- `proxy_url`;
- `proxy_url_env`;
- named `[proxies.*]` using `url`;
- named proxy using `url_env`;
- unset, empty, and whitespace-only env vars;
- trimming behavior;
- mutual exclusivity and unknown named proxies;
- secret-safe error requirements.

### D. Proxy URI capability corpus

Build a table/fixture corpus from docs, config tests, and Python runtime probes. At minimum consider:

- `direct://` as a test/reference transport;
- HTTP CONNECT with and without authentication;
- SOCKS4/4a;
- SOCKS5 and username/password authentication;
- DNS-through-proxy semantics where the URI form distinguishes them;
- canonical `__` multi-hop expressions if accepted by the current pproxy path;
- Shadowsocks forms currently documented/accepted;
- SSR forms currently documented/accepted;
- Trojan;
- SSH;
- any `+` composition, fragment authentication, local-bind/rules/plugin syntax that EggPool docs claim.

For every row record: Python parse result, Python connection result against a deterministic fixture where feasible, Eggress 1.0.2 construction result, Eggress runtime result, required Eggress feature, parity class, and whether the row is mandatory for Rust cutover.

A documented form that does not actually work in the current Python product may be recorded as documentation drift, but this must be evidenced rather than assumed. A working Python form cannot be silently removed merely to keep the Rust dependency set small.

## 5. Deterministic fixture additions

Extend `tests/migration_rs` with transport-oriented local fixtures rather than live providers. The harness should be able to create/observe, as needed:

- an HTTP/1.1 upstream that records method/path/header names/body length and can return finite or delayed/chunked bodies;
- a local TLS upstream with a test CA/certificate strategy that does not weaken production verification;
- connection-count/reuse observations without retaining secrets or request bodies;
- an HTTP CONNECT proxy fixture;
- a SOCKS5 proxy fixture with optional auth;
- failure fixtures for refused connection, connect stall, delayed response data, premature close, malformed framing, and bounded write backpressure where deterministic;
- proxy target observations sufficient to prove traffic used the configured proxy instead of going direct.

Use existing Eggress testkit components only if they can be consumed cleanly without turning EggPool's production dependency into a test-only workspace coupling. Otherwise keep the migration fixture implementation local and minimal.

## 6. Eggress feature decision

At T001 closure, record the exact dependency proposal for T003. Start from:

`eggress-embed = { version = "=1.0.2", default-features = false, features = [...] }`

Enable only features justified by the mandatory corpus. Expected candidates are `common`, `pproxy-compat`, `extended`, `pproxy-legacy`, `ssh`, and `legacy-crypto`. Explicitly reject unrelated `operations`, `reverse`, and `quic` unless new repository evidence changes the product requirement.

If Eggress cannot represent a mandatory working Python form, stop. Either correct Eggress first or create/supersede an ADR describing an intentional supported difference. Do not hide the gap by dropping a test or falling back to direct.

## 7. Error and secret contract

Create a transport error observation vocabulary that later Rust tests can compare without depending on HTTPX class names verbatim. Preserve distinctions needed for later policy: pool timeout, connect timeout, TLS/connect failure, write timeout/error, read timeout/error, protocol failure, proxy parse/config failure, and cancellation.

Normalization may discard incidental library wording, socket numbers, timing jitter, and generated certificate details. It may not discard failure class, target/proxy identity class, status, timeout stage, direct-versus-proxied evidence, or whether a secret was exposed.

Proxy URI and API-key fixtures must use synthetic credentials. Add explicit negative assertions that username/password/token fragments do not appear in captured diagnostics.

## 8. Tests

Required T001 tests include:

- contract fixtures are deterministic and bounded;
- direct HTTP observation from Python can be captured;
- at least one HTTPS observation from Python can be captured;
- HTTP CONNECT and SOCKS5 proxy observations can be captured from Python when pproxy is available;
- proxy-env resolution matrix matches Python behavior;
- the proxy capability corpus is machine-readable or mechanically testable;
- differential normalization rejects a changed error class or changed direct/proxy path;
- secret-bearing proxy URIs remain redacted in observations;
- every enabled Eggress feature has at least one corpus row that justifies it.

## 9. Verification

Run the existing Rust foundation gates to ensure no regression, the migration harness suite, targeted Python provider/client-pool/proxy/config tests, and the new T001 fixture/corpus tests. No live provider credentials are required.

## 10. Non-goals

No Hyper client, Rustls production connector, Eggress production dependency, Rust provider pool, inference route, provider auth injection, catalog request, routing, retry, or finalization implementation.

## 11. Acceptance criteria

T001 closes only when an implementation agent can build T002 and T003 without guessing about HTTP protocol, redirects, pooling, timeout stages, proxy resolution, Eggress features, or secret redaction. The contract corpus must make a future narrowing of proxy support visible as a failing/changed artifact.

## 12. Stop conditions

Stop and report if:

- the public docs and working Python proxy behavior materially conflict in a way that requires product choice;
- a mandatory working proxy form cannot be executed by current Eggress;
- deterministic TLS/proxy fixtures would require weakening production trust behavior;
- a proposed normalization would erase direct-versus-proxy or failure-stage differences.

## 13. Closure evidence

Provide the contract document, proxy capability matrix/corpus, fixture inventory, Eggress release/feature decision, exact-vs-semantic parity table, targeted test outputs, dependency delta (expected none in production Rust), and any discovered documentation drift.
