# Provider Transport T005 — Differential Qualification and M4 Closure

Status: queued; blocked on T004 closure

Repository baseline for planning: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md#t005--provider-transport-differential-qualification-and-closure`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

Primary class: invariant

## 1. Objective

Qualify the complete M4 provider transport boundary after T001-T004 and decide whether it is stable enough for M5 catalog/routing/quota/health work to depend on. This milestone is a closure pass, not a place to add new provider features.

The qualification must prove that direct and required proxied provider HTTP behavior is parity-equivalent to Python at the transport boundary, resource ownership remains bounded under concurrency/cancellation/failure, and the selected Eggress dependency does not silently widen or narrow EggPool's contract.

## 2. Dependencies

Hard: T001-T004 closed.

No M5 implementation may be required to pass T005. If a test needs routing or provider codecs to exercise transport, replace it with a neutral transport fixture rather than pulling the downstream subsystem forward.

## 3. Qualification matrix

Create a reviewable matrix mapping every T001 contractual row to implementation evidence. At minimum cover the following classes.

### A. Direct HTTP/HTTPS

- method/path/query preservation;
- request header/body preservation at the neutral transport boundary;
- status/header/body response preservation;
- incremental response body consumption;
- HTTP/1.1 protocol baseline and redirect behavior;
- provider TLS hostname/certificate verification;
- base URL/path joining;
- ambient proxy environment does not redirect direct clients.

### B. Pooling and limits

- provider client reuse across repeated requests;
- physical connection reuse where keepalive permits;
- idle expiry;
- `max_connections` bound under concurrency;
- `pool_timeout_s` classification under saturation;
- `max_keepalive`/idle-retention behavior within the T001 semantic contract;
- build/client counts remain stable with request volume.

### C. Timeout and failure stages

- connection refused;
- DNS/connect stall where deterministic;
- TLS failure;
- connect timeout;
- request write failure/timeout where deterministically fixtureable;
- response read failure/timeout;
- premature EOF / malformed HTTP framing;
- pool timeout;
- cancellation during capacity wait, connect, and response consumption.

Compare stable failure class/stage, not library wording.

### D. Proxy resolution and isolation

- inline `proxy_url`;
- `proxy_url_env` including whitespace/unset failures;
- named proxy `url` and `url_env`;
- mixed direct/proxied accounts;
- proxied account uses a dedicated pool;
- two proxied accounts remain pool-isolated;
- a configured proxy failure never creates direct target traffic.

### E. Proxy protocols/capabilities

Run every T001 mandatory proxy corpus row supported by the selected Eggress features. Include HTTP CONNECT, SOCKS cases, authentication, DNS semantics, chains, and any enabled encrypted/SSH/legacy families.

For rows classified as supported differences, require an explicit written rationale and test that demonstrates the Rust behavior. Do not mark a row passing solely because Eggress's own repository has a parity test.

### F. Redaction/security

Inject unique synthetic secrets into:

- provider Authorization/API-key header;
- proxy user/password/userinfo;
- proxy fragment authentication;
- encrypted-proxy password/key fields;
- proxy env values.

Trigger parse, construction, authentication, connection, TLS, timeout, request, response, and shutdown failures as applicable. Assert none of the secret markers appear in operator-facing errors, logs captured by the harness, snapshots, closure fixtures, or panic output from supported error paths.

## 4. Differential harness requirements

By T005 the migration harness should support reusable observations for direct and proxied provider transport. Normalize only facts explicitly classified by T001.

Permitted examples:

- ephemeral source ports;
- connection IDs;
- exact elapsed milliseconds within a bounded range;
- HTTP library error prose;
- automatically generated Date or implementation-specific headers classified incidental.

Forbidden normalization examples:

- direct versus proxied path;
- target hostname/DNS mode;
- status code;
- request path/body;
- proxy authentication success/failure;
- error stage/category;
- whether a connection limit was exceeded;
- whether a retry occurred inside the transport;
- secret leakage;
- response streaming versus eager buffering.

## 5. No implicit transport retries

Confirm that T002/T003 do not introduce automatic request retries that alter the later coordinator's ownership. Connection establishment behavior internal to Hyper/Eggress must be documented, but a failed submitted HTTP request must not be silently replayed by the M4 layer unless the Python oracle does the same and T001 explicitly classified it contractual.

Retry/failover belongs to M7, where persistence and downstream handoff state are known.

## 6. Cancellation and leak qualification

Run bounded stress-style local tests sufficient to reveal ownership leaks without creating a load-testing project. Examples:

- repeated cancel during pool wait;
- repeated cancel during connect/proxy handshake;
- repeated drop of partially consumed response bodies;
- proxy endpoint repeatedly accepts then closes;
- mixed direct/proxied concurrent requests at configured capacity.

After each bounded run verify:

- connection-limit permits return to baseline;
- fixture-observed open connections converge to baseline after shutdown/idle expiry;
- no client construction count grows with request count;
- process shutdown remains bounded;
- no panic/poisoned global state prevents a subsequent successful request.

Do not invent a numerical throughput target for M4.

## 7. Dependency and footprint review

Review the final `rust/Cargo.toml`/lockfile delta.

Required conclusions:

- only one provider HTTP stack exists;
- only one TLS stack exists;
- Eggress uses `default-features = false`;
- every enabled Eggress optional feature has T001/T005 evidence;
- no Eggress listener/operations/reverse/quic dependency leaked in without need;
- no Reqwest was added;
- no unnecessary runtime framework or internal crate split was introduced.

If feature unification pulls substantial unused Eggress functionality despite narrow features, record it and decide whether it is acceptable for the SBC target before closure.

## 8. Python regression boundary

Run targeted existing Python tests for provider client pool, pproxy transport, proxy/config resolution, and any tests touched by harness changes. The Python production implementation must remain behaviorally unchanged by M4 planning/qualification support.

Do not remove pproxy from Python packaging during M4; Python remains the oracle until cutover.

## 9. Documentation and evidence updates

At closure:

- update `rust/README.md` with the provider transport status and neutral test invocation, without claiming inference support;
- update the provider transport contract with any approved supported differences discovered during implementation;
- create `migration-rs/closure/provider-transport/` closure records for T001-T005 according to normal planning governance;
- update the subsystem roadmap/registry only after evidence passes;
- if proxy documentation has proven drift, plan/document correction without changing the Python user's supported behavior deceptively.

Do not rewrite `docs/proxy.md` to say Rust is production before M11.

## 10. Required verification

At minimum record:

- `cargo fmt --manifest-path rust/Cargo.toml -- --check`;
- `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`;
- `cargo test --manifest-path rust/Cargo.toml`;
- complete `tests/migration_rs` suite;
- targeted Python provider/client-pool/proxy/config tests;
- Python smoke suite if harness/server integration changed;
- `git diff --check`;
- any focused feature-build commands needed to prove the final Eggress feature set compiles on the declared MSRV/target profile.

No broad GitHub Actions matrix or live paid-provider suite is required.

## 11. M4 acceptance criteria

M4 closes only if all are true:

- T001-T004 are independently closed;
- direct provider HTTP/HTTPS transport matches the frozen contract;
- all mandatory proxy corpus rows are passing or have an explicit approved supported-difference decision;
- proxied accounts cannot bypass their proxy on configuration/runtime failure;
- provider/account pool topology and safe snapshot match Python semantics;
- connection limits/timeouts/cancellation are bounded and leak-free under the qualification cases;
- response bodies remain stream-capable;
- no transport-level automatic retry interferes with later coordinator ownership;
- no synthetic secret appears in diagnostic evidence;
- dependency/feature scope is appropriate for a local/SBC deployment;
- no unresolved high/medium correctness issue remains inside M4 scope.

## 12. Stop conditions and corrective work

Do not close M4 if a mandatory contract row fails, a configured proxy can fall back to direct, capacity permits leak after cancellation, TLS verification differs materially, or transport errors collapse distinctions needed by later policy.

A failed closure creates a bounded corrective provider-transport plan under the planning process; do not retroactively weaken T001 or normalize the mismatch away.

## 13. Downstream handoff

On successful closure, expose a concise stable interface/evidence summary for M5 and later M7:

- how to obtain the provider/account client for an already-selected account;
- raw request/streaming response API;
- transport error categories;
- connection/timeout ownership;
- what M4 explicitly does not retry or classify.

M5 may then build catalog/account/routing/health logic without reopening TCP/TLS/proxy design. M6/M7 may later use the same clients for actual wire requests and streaming ownership.

## 14. Closure evidence

The T005 closure record must include the full contract-to-test matrix, selected Eggress version/features, direct/proxy differential results, pooling/timeout/cancellation evidence, redaction corpus results, dependency review, unresolved findings by severity, verification commands/results, and a clear `closed` or `corrective plan required` recommendation.