# Plan 247 — EggServe Transport Requalification, Footprint, and Closure

Date: 2026-09-24
Status: implementation handoff (blocked on Plan 246 production cutover)
Planning baseline: `d492eade677acd6fc932c9a0c487b744a3070a91`
Depends on: `plans/246-eggserve-0.2.2-downstream-transport-reentry-and-production-cutover.md`
Closes implementation line opened by Plans 244–246 when all evidence passes
Priority: P1 downstream transport closure and release confidence

## Objective

Close the EggServe downstream-transport adoption only after the Plan 246
production cutover has been proven behaviorally equivalent at EggPool's
application boundary, dependency-safe, and acceptable for the project's
local/SBC deployment profile.

This plan is evidence and closure focused. Do not bundle unrelated runtime
optimization into it.

The target architecture is:

```text
EggServe direct H1 transport
  -> TowerToEggserve
     -> existing Axum Router
        -> EggPool middleware
           -> coordinator
              -> Eggfetch/Eggress provider transport
```

## Track A — real-socket HTTP compatibility matrix

Exercise the actual production `ServerRuntime::serve_listener` path over TCP,
not only Router oneshots.

Cover at minimum:

- HTTP/1.1 finite request/response;
- keep-alive reuse for multiple requests on one connection;
- explicit `Connection: close`;
- chunked request body;
- chunked/streaming response body;
- duplicate same-name response headers;
- malformed request framing;
- oversized request target;
- oversized header count/bytes;
- transport hard request-body ceiling;
- EggPool live generation body ceiling below the hard ceiling;
- disconnect during request upload;
- disconnect during response stream;
- healthy request after each failure class.

Where current EggPool behavior intentionally accepts HTTP/1.0, retain it;
otherwise record the current supported surface rather than broadening it.

Do not add HTTP/2 or HTTP/3.

## Track B — inference and Codex/OpenCode behavioral parity

Run the coordinator/wire suites that prove the downstream driver substitution
did not change application semantics.

Required:

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

Also retain whatever current OpenCode/config integration targets are touched by
normal server startup. Do not add live external-client credentials to this
closure.

Explicit streaming assertions:

- native Responses same-surface observation forwards original valid SSE bytes;
- translated Responses still emits required terminal
  `response.completed`;
- unknown valid native events remain preserved;
- Messages and Chat translated paths remain healthy;
- producer cancellation releases the generation lease;
- no post-handoff retry;
- terminal/finalization publication remains exactly once.

## Track C — auth/dashboard/health parity

Requalify the server-facing policy matrix through the new transport:

- public health and readiness;
- authenticated inference;
- Bearer and `x-api-key`;
- public-dashboard exemptions;
- private-dashboard enforcement;
- static resources;
- `/api/integrations/v1/profile` always authenticated;
- `/api/stats/runtime`, `/api/stats/update`, and `/api/status` always
  authenticated;
- `/v1/models` existing auth behavior;
- model detail route with wildcard path;
- update/status/dashboard JSON and HTML response content types.

EggServe transport normalization must not accidentally alter EggPool-owned
auth errors or JSON error bodies.

## Track D — shutdown/failure closure

Exercise all process-lifecycle paths with the EggServe child present:

- Ctrl-C;
- SIGTERM on Unix;
- explicit `ServerRuntimeHandle::request_shutdown`;
- unexpected EggServe terminal completion;
- idle keep-alive at shutdown;
- finite request in flight;
- body upload in flight;
- streaming response in flight;
- stalled downstream reader;
- retained generation/finalization reference;
- task-supervisor work in flight;
- DB close failure;
- control-socket close;
- ordinary shutdown deadline exhaustion in an application-owned resource after
  HTTP child completion.

Assert:

- EggServe child is joined before DB close;
- no accepted HTTP connection survives `ServerCompletion`;
- no body producer remains detached after final forced cleanup;
- `ShutdownReason` remains truthful;
- `ShutdownReport.phase`, `forced`, task counts, body-task counts, terminal
  reference counts, and DB status remain meaningful;
- a dead HTTP child cannot leave the process Running;
- PID/service cleanup happens after foreground server termination.

If a test can only pass by adding arbitrary sleeps, improve the fixture
boundary instead.

## Track E — dependency graph audit

Record the pre-cutover and post-cutover normal release graphs.

Run:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-server
cargo deny --manifest-path rust/Cargo.toml check
```

Classify new packages by live purpose.

Expected:

- direct `eggserve-core =0.2.2`;
- direct `eggserve-server =0.2.1`;
- `tower` interop only;
- no EggServe TLS/H2/H3;
- no change to the exact Eggfetch 0.2.0 provider profile;
- no change to Eggress 1.0.8 SSH/default feature policy.

Current `eggserve-core` packaging may pull `eggserve-static` and PHF-related
crates transitively even though EggPool does not use static serving.

If that is the dominant dependency/size cost, record an upstream EggServe
packaging opportunity to expose the Tower bridge from a transport-only package.
Do not copy/fork the bridge locally to save packages.

Do not remove EggPool's direct `axum`, `http`, `http-body-util`,
`tower`, `hyper`, or `hyper-util` declarations unless a live-source audit
proves the direct dependency is no longer owned anywhere else.

## Track F — release footprint/resource comparison

Use the same toolchain, target, default feature set, and release profile for
baseline and candidate.

Record:

| Measurement | Pre-EggServe baseline | EggServe candidate | Delta |
|---|---:|---:|---:|
| stripped/release artifact bytes | | | |
| Cargo.lock package count | | | |
| release dependency count | | | |
| startup RSS | | | |
| idle RSS | | | |
| finite loopback latency | | | |
| streaming first-byte latency | | | |
| sustained streaming throughput | | | |

Prefer the repository's existing qualification tooling rather than inventing a
second benchmark harness.

Development-host measurements are relative evidence, not physical SBC proof.

Acceptance is not "EggServe must be faster." The migration is justified by
HTTP transport ownership, bounded connection shutdown, parser/resource policy,
and maintenance consolidation. Small explained overhead is acceptable.

A material unexplained regression in artifact size, RSS, latency, or streaming
throughput blocks closure until understood.

Do not automatically reopen the physical Pi campaign from Plans 235–239.
Run a new target-class SBC pass only if the normal comparison exposes a
material regression or a behavior that could plausibly differ on the target.

## Track G — full repository gates

Run the current complete repository matrix:

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

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Use the repository's exact current command set if it changes during
implementation.

Any new server transport integration target must run under the ordinary Rust
workspace suite; do not create a permanent special CI workflow unless a
regression class genuinely cannot be covered otherwise.

## Track H — current-authority documentation

After tests and measurements pass, update current authority:

- `README.md`;
- `rust/README.md`;
- `AGENTS.md`;
- `architecture/README.md` if its subsystem index names the old driver;
- `architecture/overview.md`;
- `architecture/deep-dive-request-lifecycle.md`;
- `architecture/deep-dive-runtime.md`;
- `architecture/deep-dive-security.md`;
- relevant deployment/performance docs if footprint numbers are stated;
- `.opencode/skills/architecture/SKILL.md`;
- `.opencode/skills/development/SKILL.md`.

The resulting description should be precise:

```text
EggServe owns downstream HTTP/1 transport and connection lifecycle.
TowerToEggserve adapts into EggPool's existing Axum application router.
EggPool owns authentication, request limits, runtime generations,
coordinator/provider behavior, persistence, and process lifecycle.
```

Do not describe EggServe as:

- the EggPool application router;
- provider transport;
- TLS termination;
- HTTP/2 or HTTP/3 support;
- static dashboard file server.

Historical Plans 244 and 245 remain unchanged.

## Track I — closure record

Update Plan 246 and this plan with:

- implementation commit SHA;
- exact resolved EggServe versions;
- focused test results;
- full serial workspace result;
- no-default result;
- dependency/feature graph summary;
- artifact/resource comparison;
- hosted CI run;
- any upstream packaging follow-up;
- final disposition.

Then update the current plan-outcome note in `AGENTS.md`.

Closure dispositions:

### COMPLETE

Use only when:

- production uses EggServe;
- behavioral parity passes;
- shutdown ordering is bounded and clean;
- dependency/security gates pass;
- footprint/resource impact is acceptable and explained;
- docs describe the shipped boundary.

### CORRECTIVE REQUIRED

Use when the architecture is retained but a bounded downstream defect remains.
Create a new append-only corrective plan rather than editing historical plans
to hide it.

### ROLLBACK

Use when the real EggPool workload exposes an upstream contract failure or a
material unexplained regression that defeats the migration rationale.

Rollback restores `axum::serve` as the connection driver while retaining the
evidence and exact reason. Do not preserve half-integrated transport code.

## Acceptance criteria

- [ ] Plan 246 production cutover is complete.
- [ ] Real-socket HTTP behavior is qualified.
- [ ] Inference/Codex/wire behavior is unchanged.
- [ ] Auth/dashboard/health behavior is unchanged.
- [ ] Child HTTP runtime is always joined before shared resource teardown.
- [ ] Stalled clients remain bounded during shutdown.
- [ ] Unexpected child failure is process-visible.
- [ ] Default and no-default profiles pass.
- [ ] Dependency graph is understood and policy-clean.
- [ ] Release footprint/resource deltas are recorded and acceptable.
- [ ] No unnecessary physical-SBC requalification is claimed.
- [ ] Current documentation reflects EggServe -> Tower/Axum -> EggPool.
- [ ] Historical Plans 244/245 remain append-only evidence.
- [ ] Final hosted CI is green.

Plan 247 is complete only when the EggServe transport is both shipped in the
tree and supported by retained behavioral/measurement evidence.
