# Plan 246 — EggServe 0.2.2 Downstream Transport Re-entry and Production Cutover

Date: 2026-09-24
Status: implementation handoff
Planning baseline: `d492eade677acd6fc932c9a0c487b744a3070a91` (main, EggPool 0.8.0)
Supersedes implementation path: `plans/244-eggserve-0.2.0-downstream-transport-adoption-and-runtime-requalification.md`
Preserves stop record: Plan 245 remains historical and must not be rewritten
Upstream registry baseline:
- `eggserve-core =0.2.2`
- `eggserve-server =0.2.1`
Priority: P1 downstream HTTP transport ownership and robustness

Related EggPool work:
- Plan 241 — Eggfetch 0.2.0 provider transport
- Plan 243 — Eggress 1.0.8 outbound adoption
- Plan 244 — original EggServe transport-adoption design
- Plan 245 — 0.2.0 Phase-0 stop verdict

Upstream closure evidence:
- EggServe Plan 274 implementation candidate
  `e49d67b3a459a11686a20a9c13cb183fc2a1dbd4`
- EggServe Plan 275 publication closure
- published `eggserve-core 0.2.2`
- published `eggserve-server 0.2.1`
- clean registry-only Axum 0.8 consumer passed with incremental request and
  response streaming, duplicate headers, middleware, disconnect cleanup, and
  typed control/completion shutdown

## Objective

Resume the EggServe adoption line after the exact upstream defects that stopped
Plan 244 were corrected and published.

Replace only EggPool's downstream HTTP connection driver:

```text
current
pre-bound TcpListener
  -> axum::serve
     -> existing Axum Router
        -> EggPool middleware / handlers / coordinator

target
pre-bound TcpListener
  -> eggserve-server 0.2.1 direct H1 runtime
     -> eggserve-core 0.2.2 TowerToEggserve
        -> existing Axum Router
           -> EggPool middleware / handlers / coordinator
```

This is a transport/runtime substitution, not an application-server rewrite.

EggPool remains the authority for:

- process signals and shutdown reason;
- control socket and PID lifecycle;
- runtime generations and leases;
- route topology and authentication;
- live request-body policy;
- coordinator admission/routing/retry/finalization;
- provider transport;
- streaming producer ownership;
- metrics publication;
- SQLite lifecycle;
- operator-facing `ShutdownReport`.

EggServe owns:

- downstream TCP acceptance;
- HTTP/1 parsing and final framing;
- transport-level parser/resource ceilings;
- connection/request transport admission;
- canonical request-body transport;
- response-body transport and no-progress handling;
- HTTP connection drain.

Axum remains EggPool's router/extractor/middleware compatibility layer.

## Why Plan 244 can resume

Plan 245 stopped for two independent upstream failures.

### The Tower/http-body compile blocker is closed

Published `eggserve-core 0.2.2` replaced the illegal external-trait /
external-type implementation with a core-owned `HttpRequestBody` wrapper.
The `tower` feature now compiles and a clean crates.io-only Axum 0.8 consumer
proved:

- `TowerToEggserve<axum::Router>` compiles through public APIs;
- chunked requests remain incremental;
- `Body::from_stream` responses remain incremental;
- duplicate same-name response headers survive the bridge;
- middleware executes normally;
- disconnect drops the streaming producer.

No EggPool-local HTTP-body compatibility layer is needed or permitted.

### The lifecycle blocker is closed

Published `eggserve-server 0.2.1` exposes:

```rust
let handle = server.start_with_service(service).await?;
let (control, mut completion) = handle.into_parts();
```

`ServerControl` is cloneable and requests shutdown without owning
completion. `ServerCompletion::wait(&mut self)` is passive,
cancellation-safe, returns a typed terminal result, and maps top-level task
panic/cancellation to a stable server error.

The direct runtime also supports
`RuntimeConfigBuilder::disable_connection_total_timeout()`.

## Phase 0 — exact registry re-gate in EggPool before production edits

Do not start by modifying `serve_listener`.

Add the exact dependencies:

```toml
eggserve-core = {
    version = "=0.2.2",
    default-features = false,
    features = ["tower"],
}
eggserve-server = {
    version = "=0.2.1",
    default-features = false,
}
```

Do not enable EggServe:

- default features;
- TLS;
- HTTP/2;
- HTTP/3;
- static-serving features;
- Python surfaces.

The compatibility core may resolve `eggserve-static` transitively because of
its current package topology. Do not use the static service.

Before touching production behavior, add the smallest compile/runtime fixture
needed to prove the exact EggPool router type crosses the published bridge.

Acceptance:

1. `build_router(AppState)` is accepted by
   `eggserve_core::server::TowerToEggserve`;
2. the direct `eggserve_server::Server` accepts the wrapped service;
3. the existing Axum body type satisfies the response-body bound;
4. one `Body::from_stream` response crosses incrementally;
5. no local `http_body::Body` wrapper or copied EggServe adapter is added.

If this exact registry pair fails in EggPool despite the upstream external
fixture, stop this plan and record the concrete downstream type mismatch.
Do not introduce a private bridge.

## Phase 1 — private EggServe transport profile

Add one private construction helper in the HTTP-server boundary, for example:

```text
fn eggserve_runtime_config() -> Result<eggserve_server::RuntimeConfig, ServerError>
```

or an equivalently narrow helper.

Keep transport policy centralized. Do not scatter EggServe builder values
through handlers/tests.

### Total connection lifetime

Call:

```rust
.disable_connection_total_timeout()
```

EggPool legitimately supports long-lived keep-alive connections and inference
streams. The generic 60-second EggServe total-lifetime default must never
become the application lifetime authority.

### Request-body hierarchy

Use a fixed defense-in-depth EggServe ceiling:

```text
EggServe runtime max_request_body_bytes = 1 GiB
TowerToEggserve RequestBodyPolicy::Stream max_bytes = 1 GiB
EggPool live generation max_request_body_bytes = operator value
                                                  (default 10 MiB)
```

The existing `server::middleware::admit_inference_body` remains the
application admission authority. It must still:

1. acquire the current `GenerationLease` before body collection;
2. read that generation's `server.max_request_body_bytes`;
3. return EggPool's existing JSON 413 when the live limit is crossed;
4. attach the same generation lease to the request;
5. hand one bounded `Bytes` value to the handler.

The EggServe layer must not collect the full 1 GiB ceiling before Axum sees the
body.

### Config validation ceiling

Because EggServe's transport hard maximum is 1 GiB, tighten
`Config::validate` to require:

```text
0 < server.max_request_body_bytes <= 1 GiB
```

Keep the field live-reloadable in
`rust/src/config_reload_policy.rs`.

Required tests:

- default 10 MiB remains valid;
- exactly 1 GiB is valid;
- 1 GiB + 1 is rejected before candidate publication;
- 10 MiB -> 20 MiB remains a live transition;
- invalid reload leaves the active generation untouched.

Do not reclassify the field as restart-required.

### Compatibility-oriented timeout profile

Do not inherit EggServe's generic 30-second handler/body defaults or
60-second total-lifetime default.

The first cutover must use explicit private values chosen to avoid becoming
the normal application timeout authority. At minimum:

- total connection lifetime: disabled;
- handler timeout: deliberately much larger than normal provider
  first-byte/read policies;
- request-body read timeout: deliberately compatibility-oriented rather than a
  new short upload deadline;
- header-read timeout: explicit;
- keep-alive idle timeout: explicit;
- response-write no-progress timeout: explicit;
- graceful-shutdown timeout: explicit and no greater than 5 seconds so it
  fits inside EggPool's current 10-second foreground shutdown budget.

Do not add new public TOML timeout keys in this plan.

If implementation chooses exact constants different from Plan 244's earlier
suggestions, document each value beside the helper and prove precedence with
short deterministic test-only profiles. Do not use EggServe's defaults
implicitly.

### Parser/admission profile

Set explicit private values for:

- `max_connections`;
- `max_in_flight_requests`;
- `max_buf_size`;
- `max_headers`;
- `max_header_bytes`;
- `max_request_target_bytes`.

Favor compatibility for the first transport substitution. Do not tune for
aggressive SBC minimization in this plan.

No tunnel/WebSocket capability is required.

## Phase 2 — production connection-driver replacement

Modify only the connection-driving portion of
`rust/src/server/mod.rs::ServerRuntime::serve_listener`.

Keep unchanged:

- `build_router(AppState)`;
- route declarations;
- Axum middleware;
- handlers and extractors;
- `BodyTaskTracker`;
- coordinator entry points;
- runtime-generation ownership;
- control-server ownership;
- signal registration;
- `ShutdownReason`;
- `ShutdownReport`.

Expected production shape:

```text
existing pre-bound tokio::net::TcpListener
  -> eggserve_server::Server::builder()
       .runtime(explicit EggPool transport config)
       .from_listener(listener)
       .build()
  -> TowerToEggserve::with_policy(
       existing Axum Router,
       RequestBodyPolicy::Stream { max_bytes: 1 GiB }
     )
  -> start_with_service(...)
  -> ServerHandle::into_parts()
```

Preserve the caller-bound listener so startup conflict checking and ownership
remain EggPool's.

Do not let EggServe bind a second socket.

### Error mapping

Add a narrow EggPool `ServerError` variant for EggServe
startup/runtime failures if mapping them to `Bind` would be misleading.

Do not surface raw transport internals to HTTP clients.

Keep source errors available for operator diagnostics through the existing
error boundary.

## Phase 3 — compose passive completion under EggPool lifecycle

The foreground supervisor must select between:

- unexpected EggServe terminal completion; and
- EggPool's existing quiesce signal.

Target shape:

```rust
let (control, mut completion) = eggserve_handle.into_parts();

tokio::select! {
    result = completion.wait() => {
        // server terminated without an EggPool quiesce request
    }
    _ = handle.wait_for_quiesce() => {
        control.shutdown();
        // await typed completion before shared runtime/DB teardown
    }
}
```

### Normal shutdown ordering

Required order:

```text
signal/control/requested shutdown
  -> EggPool enters Quiescing
  -> ServerControl::shutdown()
  -> EggServe stops accept and drains owned connections
  -> ServerCompletion resolves
  -> EggPool closes control listener
  -> supervised/background/application resources drain
  -> generation manager/finalizers close
  -> metrics flush
  -> DB closes
  -> Stopped / ShutdownReport
```

Never close the DB or shared runtime state while the EggServe child still owns
live request tasks.

### Single outer shutdown deadline

Preserve one EggPool foreground shutdown deadline.

EggServe's direct H1 driver internally bounds every post-shutdown connection
drain to:

```text
min(RuntimeConfig.graceful_shutdown_timeout, 5 seconds)
```

and `ServerCompletion::wait()` does not resolve until runtime-owned connection
tasks have been joined.

Configure EggServe's child graceful timeout at or below 5 seconds and account
for its elapsed drain time against EggPool's existing 10-second deadline.

Refactor the EggPool close path to carry an absolute deadline or remaining
budget if necessary; do not accidentally create "5 seconds HTTP + another
10 seconds resources" as an undocumented new total shutdown contract.

Add a deterministic stalled-downstream-consumer test proving EggServe
completion resolves within the child drain budget and EggPool still reaches
bounded resource close.

Do not fake forced termination by dropping `ServerCompletion` and then
closing shared resources underneath a potentially live HTTP child.

If exact integration evidence shows EggServe can violate the documented
bounded completion contract in an ordinary H1 case, stop and move a force
termination primitive upstream rather than adding unsafe teardown ordering in
EggPool.

### Unexpected completion

If `completion.wait()` resolves before EggPool requests quiescence:

- classify the child terminal result;
- request EggPool shutdown with `ShutdownReason::ServerCompleted`;
- close process resources through the same bounded path;
- preserve typed distinction between clean unexpected completion and
  EggServe terminal error where useful to the returned `ServerError`;
- never leave a process with a dead listener and `Running` phase.

No polling.

## Phase 4 — preserve request/auth/application semantics

Requalify through the real EggServe socket boundary:

Unauthenticated as today:

- `/v1/healthz`;
- `/v1/readyz`;
- static dashboard assets;
- public-dashboard ordinary pages/data according to existing policy.

Always authenticated as today:

- inference `/v1/*`;
- `/api/integrations/*`;
- `/api/stats/runtime`;
- `/api/stats/update`;
- `/api/status`.

Private dashboard mode must continue restoring auth to ordinary dashboard
pages/data.

Body cases:

- Content-Length below live limit;
- chunked/unknown-length below live limit;
- Content-Length above live EggPool limit but below 1 GiB transport ceiling;
- chunked body crossing the live EggPool limit;
- client disconnect during upload;
- invalid body followed by healthy request;
- live limit rehash where old/new requests retain their generation-owned
  limits.

Keep EggPool's status/error bodies where EggPool owns policy.

## Phase 5 — preserve streaming ownership

Do not change
`rust/src/server/inference.rs::finish_stream_execution` merely to satisfy the
new transport.

The intended chain remains:

```text
StreamingExecution
  -> bounded mpsc(32)
  -> Axum Body::from_stream
  -> TowerToEggserve response conversion
  -> EggServe canonical ResponseStream
  -> downstream socket
```

Keep `BodyTaskTracker`.

Prove:

- first downstream stream bytes arrive before provider completion;
- native Responses SSE bytes remain byte-preserved;
- translated Responses still use the existing bounded encoder;
- valid unknown native events survive;
- duplicate provider response headers survive;
- downstream disconnect eventually drops the receiver, producer, execution,
  and generation lease;
- post-handoff provider/producer failure never synthesizes a second HTTP
  response;
- no retry becomes possible after handoff;
- a failed/cancelled stream does not poison the next request.

Use observable deterministic gates rather than sleep-based guesses.

## Phase 6 — focused implementation qualification

Add a dedicated loopback target, preferably:

```text
rust/tests/server_transport.rs
```

if no existing target cleanly owns the raw downstream HTTP transport boundary.

It should cover the new server-specific behavior rather than duplicating every
coordinator test.

Run at minimum:

```bash
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
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

If `health` is not a standalone integration target in the current tree, run
the corresponding library/server tests instead; do not invent a dead target
just to match this plan.

## Phase 7 — dependency/no-default safety gate

The EggServe dependency is unconditional downstream HTTP infrastructure and
must compile with EggPool's `--no-default-features` profile.

Run:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-server
```

Verify specifically:

- no EggServe TLS/H2/H3 feature is enabled;
- EggServe does not alter Eggfetch/Eggress/SSH feature selection;
- Axum remains a direct EggPool application dependency;
- no local adapter dependency was added unnecessarily;
- `eggserve-static` transitive presence, if still pulled through core, is
  recorded rather than misrepresented.

## Acceptance criteria

- [ ] Exact published `eggserve-core =0.2.2` + `eggserve-server =0.2.1`
      are pinned.
- [ ] Phase-0 EggPool router/registry bridge passes without a local adapter.
- [ ] `axum::serve` is no longer the production downstream connection
      driver.
- [ ] Existing Axum router/middleware/handlers remain the application layer.
- [ ] Pre-bound listener ownership remains EggPool-owned.
- [ ] Total connection lifetime is explicitly disabled.
- [ ] EggServe request-body hard ceiling and Tower body policy are explicit.
- [ ] EggPool generation-aware body limit remains live and authoritative.
- [ ] Config rejects body limits above the EggServe hard maximum.
- [ ] Passive typed child completion is integrated without polling.
- [ ] Child HTTP drain completes before shared resource/DB teardown.
- [ ] One EggPool outer shutdown deadline remains authoritative.
- [ ] Stalled downstream clients cannot make ordinary H1 shutdown unbounded.
- [ ] Unexpected child completion takes the process out of Running.
- [ ] Authentication/dashboard exemptions are unchanged.
- [ ] Finite and streaming inference behavior is unchanged.
- [ ] No retry is reintroduced after streaming handoff.
- [ ] Default and no-default builds/tests remain valid.
- [ ] No EggServe TLS/H2/H3/static capability is used by EggPool.

## Rollback rule

If the exact EggPool composition exposes an upstream contract gap that cannot
be solved by configuration or documented public APIs, revert the production
driver change and record a new stop/corrective plan.

Do not:

- copy EggServe interop code into EggPool;
- poll child state;
- reach into private EggServe internals;
- fork the EggServe accept loop;
- rewrite the Axum router into EggServe-native handlers;
- close DB/runtime state underneath an unjoined EggServe child.

Plan 247 owns final repository-wide behavior, dependency/footprint, and
documentation closure after this cutover is functionally green.
