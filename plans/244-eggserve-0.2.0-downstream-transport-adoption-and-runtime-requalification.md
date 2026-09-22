# Plan 244 — EggServe 0.2.0 Downstream Transport Adoption and Runtime Requalification

Date: 2026-09-22
Status: implementation handoff
Planning baseline: `6dd4cff3ee83cce2f901ebe07922ac014f99c893` (main, Eggpool 0.8.0)
Upstream baseline: EggServe 0.2.0
Priority: P1 downstream transport ownership, robustness, and maintenance consolidation

Related Eggpool work:
- `plans/230-residual-native-runtime-efficiency-roadmap.md`
- `plans/234-sbc-performance-and-release-footprint-closure.md`
- `plans/241-eggfetch-0.2.0-adoption-and-provider-transport-requalification.md`
- `plans/243-eggress-1.0.8-outbound-crate-adoption-and-typed-route-error-requalification.md`

## Objective

Adopt EggServe 0.2.0 as Eggpool's downstream HTTP transport/runtime boundary
without rewriting Eggpool's Axum application layer or moving application
policy into EggServe.

The intended ownership shape is:

```text
TCP listener
  -> EggServe 0.2.0 HTTP runtime
     -> TowerToEggserve
        -> existing Axum Router
           -> existing Eggpool auth/body-admission middleware
           -> health/dashboard/inference handlers
           -> coordinator
```

EggServe should own transport parsing, connection acceptance/tracking, final
HTTP framing normalization, transport-level resource ceilings, and HTTP
connection drain. Eggpool remains the sole authority for process lifecycle,
signals, runtime generations, request/coordinator semantics, provider routing,
stream finalization, metrics, database shutdown, PID/control-socket ownership,
and operator-visible shutdown reporting.

This is a transport substitution, not an Axum removal project.

The desired end state is:

1. `axum::serve` is no longer Eggpool's production downstream connection
   driver;
2. the existing `build_router(AppState)` topology remains the application
   routing authority;
3. EggServe's `TowerToEggserve` adapter is the only bridge between the
   EggServe transport and the existing Axum/Tower application;
4. streaming Responses/Chat/Messages remain incremental with no whole-response
   buffering or second stream parser;
5. the live generation-aware Eggpool request-body limit remains authoritative
   below a fixed EggServe defense-in-depth ceiling;
6. EggServe defaults that would shorten legitimate inference lifetimes are not
   adopted implicitly;
7. Eggpool's `ServerRuntime` remains the process shutdown owner and composes
   EggServe as one child lifecycle;
8. authentication, dashboard-public exemptions, health/readiness, Codex
   compatibility, coordinator retry/finalization, and provider semantics are
   unchanged;
9. dependency and release-footprint changes are measured rather than assumed
   beneficial;
10. adoption stops rather than introducing a polling/hacky lifecycle bridge if
    EggServe 0.2.0 lacks the passive terminal-observation contract Eggpool needs.

## Why EggServe 0.2.0 is now a plausible boundary

EggServe 0.2.0 exposes the application-server pieces Eggpool needs:

- a generic transport-owning server;
- caller-owned/pre-bound TCP listener support;
- a canonical request/response body model with bounded streaming;
- `http`/`http-body` interoperability;
- a feature-gated `TowerToEggserve` adapter;
- graceful/forced HTTP shutdown;
- request-header/body/handler/connection/write timeout controls;
- connection and in-flight-request admission;
- bounded parser/header/target policy;
- final response normalization;
- a Rust 1.89 MSRV matching Eggpool's current floor.

The Tower adapter allows the existing Axum router to remain intact. That is the
critical constraint: do not translate every Eggpool route into EggServe-native
handlers merely to use the new transport.

## Current Eggpool boundary

At the planning baseline, `rust/src/server/mod.rs`:

- binds a `tokio::net::TcpListener`;
- builds the Axum router with `build_router(AppState)`;
- starts `axum::serve(listener, app)`;
- attaches `with_graceful_shutdown` to the Eggpool quiesce signal;
- separately owns signal registration, control-server closure, body-task
  tracking, task-supervisor drain, generation retirement, metrics flush,
  database close, and `ShutdownReport`.

`rust/src/server/middleware.rs` deliberately acquires a live
`GenerationLease` before collecting an inference body and enforces the active
generation's `server.max_request_body_bytes`.

`rust/src/server/inference.rs` uses a bounded mpsc bridge for streaming
responses and retains the generation lease and `StreamingExecution` until the
body producer terminates.

Those application/runtime ownership rules must survive the transport change.

## Architecture invariants

Eggpool owns:

- route registration and application middleware;
- API-key authentication and dashboard-public exemptions;
- generation acquisition and live configuration semantics;
- inference body admission below the hard transport ceiling;
- coordinator admission, retry legality, routing, provider submission, wire
  adaptation, finalization, and durable publication;
- body-producer task ownership;
- process signals and shutdown reason;
- task-supervisor shutdown;
- generation retirement;
- metrics flushing;
- control-socket close;
- database close;
- PID lifecycle;
- the final `ShutdownReport`.

EggServe owns only:

- downstream TCP accept;
- HTTP/1 parser/framing;
- canonical request-body transport;
- transport parser/header/target ceilings;
- HTTP connection and pre-response service admission;
- downstream response framing normalization;
- HTTP connection/write lifecycle;
- HTTP connection drain/abort.

Axum remains:

- the router;
- the extractor/middleware/application compatibility layer.

Do not move coordinator or runtime policy into EggServe. Do not add a second
router. Do not replace Eggpool's body producer with an EggServe-specific
producer if the Tower bridge already streams it correctly.

## Phase 0 — compile/API gate before lifecycle edits

Before changing production server behavior, prove the exact 0.2.0 crates.io
surface against the current Axum router.

Add the narrow candidate dependency:

```toml
eggserve-core = { version = "=0.2.0", default-features = false, features = ["tower"] }
```

Do not enable:

- `tls`;
- `http2`;
- `http3`;
- Python/static-serving behavior by feature choice;
- any future default feature set.

Build a small repository-local compile fixture or temporary implementation seam
that proves:

1. `build_router(AppState)` can be passed to
   `eggserve_core::server::TowerToEggserve`;
2. EggServe's canonical `RequestBody` satisfies the Axum/Tower input body
   requirements;
3. Axum's response body satisfies EggServe's incremental
   `http_body::Body<Data = Bytes>` conversion requirements;
4. an Axum `Body::from_stream` response crosses the adapter incrementally;
5. duplicate response headers used by provider passthrough remain semantically
   correct after the adapter;
6. no whole request/response body buffering is introduced by the bridge.

If this exact adapter does not compile with Axum 0.8, stop. Do not write a
second custom generic HTTP adapter inside Eggpool. Record the public API gap and
move the needed generic bridge upstream to EggServe.

### Mandatory lifecycle API gate

Eggpool currently observes both:

- an explicit quiesce request; and
- unexpected HTTP server completion/failure.

Before production cutover, prove EggServe 0.2.0 exposes a passive way to await
or subscribe to terminal server state without triggering shutdown.

`ServerHandle::wait()` is not sufficient if invoking it initiates shutdown,
and a synchronous `state()` getter alone is not an acceptable production
replacement.

Acceptance for this gate is one of:

- a public EggServe 0.2.0 passive terminal/failed-state future or subscription
  exists and can be used directly; or
- the current 0.2.0 public API has another non-polling composition that
  preserves the same semantics.

If neither exists, **stop Eggpool implementation at this gate** and create a
narrow upstream EggServe follow-up for passive terminal observation. Do not:

- add periodic state polling;
- reach into EggServe private lifecycle internals;
- fork its accept loop into Eggpool;
- detach the HTTP runtime and assume it cannot fail.

The migration is worthwhile only if lifecycle ownership becomes cleaner.

## Phase 1 — establish an Eggpool-specific EggServe transport profile

Do not use EggServe's generic defaults unchanged.

The default EggServe 0.2.0 handler timeout (30 s) and total TCP connection
lifetime (60 s) are not compatible with an LLM proxy where first response and
stream lifetime can legitimately exceed those values.

Add one private helper in the server module, for example:

```text
fn eggserve_runtime_config(config: &Config, bind: SocketAddr) -> Result<RuntimeConfig, ServerError>
```

It must centralize every intentional downstream transport value. Do not scatter
EggServe builder calls across startup/tests.

### Request-body ceiling

EggServe has a server-construction-time hard request-body ceiling with a
0.2.0 maximum of 1 GiB. Eggpool's generation-aware
`server.max_request_body_bytes` remains live-reloadable.

Use this two-level contract:

```text
EggServe transport ceiling: fixed 1 GiB
EggServe Tower body policy: fixed bounded Stream <= 1 GiB
Eggpool live generation limit: current server.max_request_body_bytes
```

The EggServe ceiling is defense in depth only. The existing
`admit_inference_body` middleware must continue to acquire the active
generation first and enforce that generation's current lower/equal limit.

Because the existing Eggpool TOML accepts any positive `u64`, add an explicit
configuration validation ceiling of 1 GiB if the EggServe runtime requires it.
This is a real compatibility narrowing and must be documented.

Required reload tests:

- 10 MiB -> 20 MiB remains live;
- a valid live change takes effect for newly acquired generations;
- a value above the supported hard ceiling is rejected during config
  validation and never published;
- an invalid candidate does not disturb the active generation.

Do not reclassify the field as restart-required merely because EggServe has a
fixed hard ceiling.

### Timeout profile

The first adoption pass must preserve Eggpool/coordinator/provider timeouts as
the application authority.

Do not accept EggServe's 30 s handler or 60 s total defaults.

Select explicit large finite compatibility values only after proving they are
safe in EggServe 0.2.0 and do not overflow its deadline arithmetic. The values
must be high enough that ordinary Eggpool upstream first-byte and long-stream
behavior is governed by existing coordinator/provider policy rather than the
transport wrapper.

The implementation must document each selected value and why it is
non-binding for the supported Eggpool workload.

Do not add new public timeout TOML keys in this plan.

A later performance/security plan may tighten transport timeouts only with
separate behavioral evidence.

### Parser/resource profile

Explicitly configure, rather than inherit silently:

- `max_request_body_bytes`;
- `max_connections`;
- `max_in_flight_requests`;
- `max_buf_size`;
- `max_headers`;
- `max_header_bytes`;
- `max_request_target_bytes`;
- `header_read_timeout`;
- `body_read_timeout`;
- `handler_timeout`;
- `keep_alive_idle_timeout`;
- `response_write_timeout`;
- `connection_total_timeout`;
- `graceful_shutdown_timeout`.

For the initial cutover, favor compatibility over aggressive tightening.
Do not accidentally reject normal Codex/OpenCode headers, model paths, large
but valid JSON payloads, or long-lived streaming responses.

No tunnel/WebSocket capability is needed for current Eggpool surfaces.

## Phase 2 — replace the production Axum connection driver only

Keep:

- `build_router(state) -> Router`;
- all route declarations;
- all Axum handlers;
- all application middleware;
- all extractors and response builders.

Replace only the production connection-driving portion of
`ServerRuntime::serve_listener`.

Expected shape:

```text
existing pre-bound tokio::net::TcpListener
  -> EggServe Server::builder()
       .runtime(eggserve_runtime_config(...))
       .from_listener(listener)
       .build()
  -> TowerToEggserve::with_policy(existing_axum_router, bounded_stream_policy)
  -> start_with_service(...)
```

Preserve the pre-bound listener. Do not let EggServe independently bind after
Eggpool has already performed startup conflict checks.

Do not add EggServe static serving for the dashboard. The current embedded
dashboard routes remain Axum application responses.

Map EggServe startup/lifecycle errors into Eggpool's existing `ServerError`
boundary without exposing transport implementation strings to clients.

Prefer a narrow new variant such as an HTTP runtime error wrapper only if the
existing `Bind`/lifecycle variants cannot preserve context truthfully. Do not
collapse configuration/lifecycle failures into misleading bind failures.

## Phase 3 — preserve application request-body and auth semantics

The Tower bridge must not bypass or duplicate Eggpool middleware.

Requalify:

- `/v1/healthz` and `/v1/readyz` remain unauthenticated;
- static dashboard assets remain unauthenticated as today;
- public-dashboard ordinary pages/data retain their existing exemption;
- inference `/v1/*` remains authenticated;
- `/api/integrations/*`, `/api/stats/runtime`,
  `/api/stats/update`, and `/api/status` remain authenticated regardless
  of dashboard-public mode;
- private-dashboard mode restores auth on ordinary dashboard pages/data;
- Bearer and `x-api-key` behavior remains unchanged.

For inference bodies, prove that the request enters Eggpool's
`admit_inference_body` middleware as a streaming Axum body and that the
generation-aware `Limited` collector remains the application limit.

The EggServe body adapter must not prebuffer a complete 1 GiB body merely
because the hard ceiling is high.

Required body cases:

- content-length below live limit;
- chunked/unknown-length below live limit;
- content-length above live Eggpool limit;
- chunked body crossing the live limit;
- client disconnect mid-body;
- body error followed by a healthy request;
- live limit change with old/new generation ownership.

Keep error bodies/statuses stable where Eggpool currently owns them. In
particular, the existing Eggpool JSON 413 for inference admission should not
silently become a different EggServe generic error for requests that are below
the hard transport ceiling but above the live generation limit.

## Phase 4 — streaming response parity and cancellation

This is the highest-risk behavioral area.

The existing producer in `rust/src/server/inference.rs` must remain bounded
and incremental:

```text
StreamingExecution
  -> mpsc(32)
  -> Axum Body::from_stream
  -> Tower response body
  -> EggServe response_from_http_body
  -> canonical ResponseStream
  -> downstream socket
```

Do not add another unbounded channel or collect the stream.

Qualification must prove:

1. first streamed bytes can reach the client before provider completion;
2. native Responses SSE bytes remain byte-preserved through Eggpool's existing
   native observe-and-forward path;
3. translated streaming still uses the existing bounded wire encoder;
4. valid unknown native Responses events remain preserved;
5. duplicate/provider response headers retain required semantics;
6. downstream disconnect drops the response consumer and eventually releases
   the Eggpool producer/lease;
7. provider/producer failure after response commitment closes/finalizes
   correctly and never synthesizes a second HTTP response;
8. `response.completed`/wire-terminal rules remain unchanged;
9. a cancelled/failed stream does not poison the next request;
10. no post-handoff provider retry is reintroduced.

Retain `BodyTaskTracker` initially. EggServe owns transport-body consumption,
but Eggpool's producer task owns `StreamingExecution`, generation lease,
metrics, and finalization. Removing that tracker requires separate evidence
that all of those application resources are observable and bounded through one
replacement owner.

Run at minimum:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
```

Add a focused downstream-HTTP integration target if the existing tests do not
exercise actual socket framing across the new EggServe boundary. Prefer one
target such as `rust/tests/server_transport.rs` rather than scattering raw
socket fixtures through coordinator tests.

## Phase 5 — compose EggServe shutdown under ServerRuntime

Do not allow EggServe and Eggpool to become competing process lifecycle
authorities.

The intended shutdown ownership is:

```text
signal/control/requested shutdown
  -> ServerRuntimeHandle::request_shutdown
     -> quiescing
     -> stop new EggServe HTTP acceptance / begin HTTP drain
     -> stop process task admission
     -> bounded HTTP/application body drain
     -> task supervisor + metrics
     -> generation manager close
     -> DB close
     -> stopped / ShutdownReport
```

Use one Eggpool outer shutdown deadline. Do not stack unrelated full 10-second
budgets such that HTTP gets 10 seconds and application resources get another
10 seconds unless the total contract explicitly intends that.

EggServe's shutdown result must be folded into the existing Eggpool forced
shutdown decision.

Required cases:

- Ctrl-C clean shutdown;
- SIGTERM clean shutdown on Unix;
- explicit `ServerRuntimeHandle::request_shutdown`;
- idle keep-alive connection during shutdown;
- active finite request;
- active request-body upload;
- active streaming response;
- stalled downstream response consumer;
- retained generation/finalization reference;
- forced deadline expiry;
- control socket closes before final DB teardown;
- DB close failure remains reported distinctly;
- PID cleanup still happens only after server/runtime termination.

Do not let dropping an EggServe handle become an accidental second shutdown
trigger during ordinary scope movement.

Preserve `ShutdownReason` and `ShutdownReport` as Eggpool-facing types;
EggServe's `ShutdownResult` is child-runtime evidence only.

## Phase 6 — unexpected HTTP runtime failure

Preserve current behavior where an unexpected HTTP server terminal condition
causes the foreground runtime to leave `Running` and proceed through bounded
resource cleanup.

Add a deterministic test that forces the narrowest available EggServe terminal
failure seam and proves:

- Eggpool detects the terminal condition without polling;
- the shutdown reason is deterministic;
- no request admission remains open;
- body tasks are bounded/aborted as needed;
- task supervisor and generations close;
- database close still occurs;
- the process does not remain alive with a dead HTTP listener.

If EggServe 0.2.0 cannot expose this cleanly, the Phase 0 lifecycle gate remains
a blocker. Do not waive it because accept-loop failure is uncommon.

## Phase 7 — HTTP behavior and compatibility qualification

Add/extend real-socket tests across the new runtime boundary for:

- HTTP/1.0 and HTTP/1.1 behavior currently accepted by Eggpool where relevant;
- keep-alive reuse;
- connection close;
- finite JSON responses;
- chunked streaming responses;
- HEAD/body-forbidden normalization where applicable;
- malformed request framing;
- oversized header count/bytes;
- oversized request target;
- hard transport body ceiling;
- live Eggpool body ceiling;
- duplicate response headers;
- client disconnects;
- slow/stalled request body under the explicit compatibility timeout profile;
- slow first response under the explicit compatibility timeout profile;
- streaming beyond EggServe's generic 60-second default using a shortened
  deterministic test profile that proves the configured total, not the
  default, is authoritative.

Do not add multi-minute tests. Parameterize the transport profile in tests so
the same precedence can be proven with millisecond/second-scale deadlines.

Re-run:

```bash
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

If a new `server_transport` target is created, keep it deterministic,
loopback-only, and free of external provider credentials.

## Phase 8 — dependency and feature graph qualification

Cargo remains the dependency authority.

Run:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo deny --manifest-path rust/Cargo.toml check
```

Record the exact EggServe graph pulled into the normal release.

Expected direct profile:

- `eggserve-core =0.2.0`;
- `tower` feature;
- no `tls`;
- no `http2`;
- no `http3`.

0.2.0's `eggserve-core` compatibility facade unconditionally depends on
`eggserve-static`. Do not pretend the initial adoption removes Axum or reduces
the dependency graph.

Specifically record whether the release gains the PHF/static-serving family
(`phf`, `phf_shared`, `phf_generator`, `phf_macros`, `siphasher`) or
other packages that are not live Eggpool owners.

Do not remove Eggpool's direct `axum`, `http`, `http-body-util`, `tower`,
`hyper`, or `hyper-util` dependencies merely because EggServe also uses
them. Each direct dependency must be audited against live Eggpool source/test
owners before deletion.

### Upstream packaging follow-up criterion

If the migration is behaviorally successful but the only meaningful footprint
cost comes from `eggserve-core` forcing static-serving machinery into an
application-server consumer, record that as an EggServe upstream packaging
opportunity.

The preferred upstream shape would expose the Tower/http interop bridge from a
transport-only leaf boundary (for example `eggserve-server` plus a narrow
interop crate) so consumers do not need `eggserve-static`.

That upstream cleanup is **not** a prerequisite for this Plan if the measured
Eggpool release impact is small and acceptable. It must not be implemented as
an Eggpool-local fork or copied adapter.

## Phase 9 — release footprint and runtime comparison

Build baseline and candidate comparably:

- same Rust toolchain;
- same target triple;
- same default features;
- same release profile;
- same LTO/strip/codegen settings.

Record:

| Measurement | Axum transport baseline | EggServe candidate | Delta |
|---|---:|---:|---:|
| release artifact bytes | | | |
| Cargo.lock package count | | | |
| normal release dependency count | | | |
| startup RSS | | | |
| idle RSS | | | |
| finite loopback request latency | | | |
| streaming first-byte latency | | | |
| sustained streaming throughput | | | |

Use existing loopback/qualification machinery where practical. Development-host
measurements are relative evidence only; do not label them physical-SBC
qualification.

Do not reopen the Plan 235–239 physical-SBC program automatically. A fresh
physical target-class pass is justified only if the transport substitution
shows a material unexplained CPU/RSS/latency/artifact regression on normal
qualification or changes the conclusions of the existing SBC work.

The migration is not required to be faster than direct `axum::serve`.
Acceptance requires that any overhead be small, explained, and justified by the
transport robustness/maintenance consolidation.

A material unexplained regression is a blocker.

## Phase 10 — full repository gates

Run the repository-standard serial matrix:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1

cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1

cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release

cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

The root `ssh`/no-default provider contract from Plan 243 remains unchanged.
EggServe is a downstream server dependency and must not alter Eggress/Eggfetch
feature selection.

No new permanent CI job is required solely for this migration unless the new
server transport target exposes a regression class not covered by the normal
workspace matrix.

## Phase 11 — current-authority documentation

After behavior is qualified, update current documentation, not historical
plans.

At minimum inspect/update:

- `README.md`;
- `rust/README.md`;
- `AGENTS.md`;
- `architecture/overview.md`;
- `architecture/deep-dive-request-lifecycle.md`;
- `architecture/deep-dive-runtime.md`;
- `architecture/deep-dive-security.md`;
- `.opencode/skills/architecture/SKILL.md`;
- `.opencode/skills/development/SKILL.md`.

The architecture should describe the resulting boundary as:

```text
EggServe downstream transport
  -> Tower/Axum application router
  -> Eggpool middleware/coordinator
```

Do not describe EggServe as replacing Axum if Axum remains the router and
application compatibility layer.

Document:

- exact EggServe version/feature selection;
- the explicit Eggpool transport profile;
- the fixed hard body ceiling vs live generation-aware body limit;
- shutdown ownership;
- measured dependency/artifact impact;
- whether static-serving transitive overhead remains;
- any upstream EggServe follow-up created.

Historical Plan 230–243 evidence remains unchanged.

## Rollback rule

If EggServe 0.2.0 cannot satisfy the passive terminal-observation gate,
streaming/cancellation parity, live body-limit contract, or bounded shutdown
ownership without a local workaround, do not force adoption.

Rollback/remove:

- the EggServe dependency;
- the candidate server driver;
- candidate-only documentation;
- candidate lockfile additions.

Return to the existing `axum::serve` boundary and write a new corrective plan
after the required upstream EggServe capability exists.

Do not mask a regression by:

- weakening streaming terminal requirements;
- increasing Eggpool coordinator/provider retry budgets;
- changing auth exemptions;
- changing generation lease ownership;
- reclassifying `server.max_request_body_bytes` as restart-required;
- buffering whole streams;
- introducing unbounded channels;
- polling EggServe lifecycle state;
- detaching a failed HTTP runtime from the process;
- expanding public timeout configuration merely to make the migration fit.

## Explicit non-goals

This plan does not authorize:

- removing Axum;
- rewriting handlers in EggServe-native `Service`;
- migrating dashboard assets to `eggserve-static`;
- enabling downstream TLS;
- enabling HTTP/2 or HTTP/3;
- enabling WebSockets/tunnels;
- changing provider Eggfetch transport;
- changing Eggress routing;
- changing coordinator retry/failover;
- changing wire codecs;
- changing SQLite behavior;
- changing Tokio from `current_thread`;
- changing routing locks;
- changing metrics persistence;
- removing `BodyTaskTracker` without separate ownership proof;
- adding a second process/server lifecycle authority;
- copying EggServe interop code into Eggpool;
- a general dependency refresh.

## Acceptance checklist

- [ ] Exact `eggserve-core =0.2.0` candidate uses only the required Tower profile.
- [ ] Current Axum router compiles directly through `TowerToEggserve`.
- [ ] Streaming Axum response bodies cross the adapter incrementally.
- [ ] EggServe 0.2.0 provides a non-polling passive terminal-observation composition; otherwise implementation stops and an upstream blocker is recorded.
- [ ] Production `axum::serve` is replaced only after the API/lifecycle gates pass.
- [ ] `build_router`, route topology, middleware, and handlers remain application authorities.
- [ ] Eggpool remains the process/signal/generation/DB shutdown authority.
- [ ] EggServe HTTP drain is composed under one bounded Eggpool shutdown budget.
- [ ] Unexpected HTTP runtime failure cannot leave Eggpool alive with a dead listener.
- [ ] EggServe generic 30 s handler / 60 s total defaults are not silently used.
- [ ] The selected transport timeout profile is explicit, tested, and non-binding for ordinary inference behavior.
- [ ] EggServe hard request-body ceiling is explicit and <= 1 GiB.
- [ ] Eggpool's generation-aware live body limit remains authoritative below that ceiling.
- [ ] >1 GiB config is rejected deterministically if required by the 0.2.0 runtime.
- [ ] Live request-body-limit reload remains live and affects newly acquired generations.
- [ ] Auth/dashboard-public behavior is unchanged.
- [ ] Health/readiness/status/integration-profile behavior is unchanged.
- [ ] Finite inference behavior is unchanged.
- [ ] Native Responses streaming remains byte-preserving and terminal-evidence-driven.
- [ ] Translated streaming remains bounded and incremental.
- [ ] Downstream cancellation releases producer tasks/generation leases and the next request succeeds.
- [ ] No post-handoff retry is introduced.
- [ ] No whole-response buffer or unbounded bridge is introduced.
- [ ] Codex Responses and compaction compatibility targets pass.
- [ ] Default and no-default provider feature contracts remain unchanged.
- [ ] `cargo deny` passes.
- [ ] Release dependency/feature graph is recorded.
- [ ] Release artifact and basic runtime/loopback deltas are measured and explained.
- [ ] Any static-serving/PHF overhead from `eggserve-core` is explicitly recorded.
- [ ] Full serial workspace suite passes.
- [ ] Current architecture/development docs reflect the final ownership boundary.

## Completion evidence

When implementation lands, update this plan's status to `complete` and append
a completion note containing:

- implementation commit SHA;
- final EggServe crate/version/features;
- whether the passive terminal-observation gate passed directly or required an
  upstream EggServe release;
- exact Eggpool transport profile values;
- request-body hard/live-limit behavior;
- focused server/auth/body/streaming test results;
- coordinator/wire/Codex qualification results;
- shutdown and unexpected-server-failure qualification results;
- no-default result;
- full serial workspace result;
- `cargo deny` result;
- release dependency/package/artifact comparison;
- loopback latency/RSS/streaming comparison;
- documentation files updated;
- any upstream EggServe follow-up created;
- any deviations from the expected source-change set.

Do not claim closure from compilation alone. This line is complete only when
the transport boundary, streaming lifecycle, shutdown ownership, and release
footprint are all requalified.
