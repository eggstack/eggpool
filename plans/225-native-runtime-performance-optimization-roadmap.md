# Plan 225 — Native Runtime Performance Optimization Roadmap

Date: 2026-09-20  
Status: complete  
Planning baseline: 3e90d36c4094af1c93756ace1c882f55b5f8f5d5  
Parent context: completed Plans 220, 223, and 224; active Plan 191  
Priority: P0/P1 request-path efficiency with evidence-gated structural follow-up  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Reduce CPU work, allocation pressure, and avoidable contention in the native Rust
runtime without changing Eggpool's HTTP/API surface, provider behavior, routing
semantics, failure isolation, durable finalization, or local/LAN deployment
model.

The current runtime is already structurally sound for an SBC-hosted proxy:
provider connections are pooled, response bodies stream incrementally, default
metrics writes are coalesced, SQLite uses WAL/NORMAL, routing does not await
network or SQLite while holding its selection lock, and Eggfetch 0.1.7 already
removed the avoidable high-level URL/IDNA/DashMap dependency closure.

The remaining high-confidence wins are concentrated in the inference hot path.
The most important issue is repeated full JSON parsing and body ownership churn
before the first provider byte can be submitted.

This roadmap keeps the campaign narrow. It deliberately separates deterministic
source-level wins from runtime changes that must be justified by measurements.

## Current evidence

### 1. Ordinary inference currently parses the same request several times

The direct finite/stream path currently crosses these parsing boundaries:

1. rust/src/server/inference.rs::handle_inference parses the body to inspect
   stream.
2. The same handler parses the body again to validate the stream field shape.
3. rust/src/coordinator/endpoints.rs::execute_finite or execute_stream parses
   the body again and clones the top-level object.
4. FiniteRequest::new or StreamRequest::new calls request admission, and
   rust/src/request/admission.rs::parse_once parses the body again.

The ordinary concrete-model path therefore performs roughly four complete
serde_json parses before wire preparation.

Virtual routing can add further work:

- resolve_virtual calls admit_request again to build the semantic/affinity view;
- rewrite_model_field reparses before serializing a model rewrite;
- finish_virtual_resolution can call rewrite_model_field twice when a selected
  concrete model also carries an Eggpool provider qualifier.

The architecture already contains the pieces needed to avoid this:
FiniteRequest::from_admitted, StreamRequest::from_admitted, and
canonical_request_from_value.

### 2. Native request pass-through copies the complete body

rust/src/wire/runtime.rs receives raw_body as a byte slice and native
pass-through builds a new Bytes allocation with Bytes::copy_from_slice.
The same pattern exists for native compact preparation.

The original Axum request body is already Bytes. For an unchanged native
request there is no protocol reason to copy the entire prompt/tool payload.

### 3. Attempt preparation clones request-owned data before a synchronous phase

Both finite and streaming coordinators currently clone:

- provider configuration;
- account credentials into a new String;
- incoming HeaderMap;
- request/correlation identifiers;
- Bytes handles;
- wire profile/fingerprint;
- AdmittedRequest.

Bytes cloning is cheap, but AdmittedRequest can own the canonical request and
Responses native preservation tree. Provider/profile/header ownership is also
duplicated even though preparation is synchronous and completes before the
provider send is awaited.

### 4. Provider client topology is immutable but hot lookup uses mutexes and
allocated tuple keys

ProviderClientPool is built completely before publication, but stores provider
and account clients behind std::sync::Mutex maps. Account lookup allocates two
Strings solely to query BTreeMap<(String, String), ...>.

The pool still needs an atomic close boundary so retiring generations can stop
new submissions while already-cloned clients finish. The optimization must
preserve that behavior.

### 5. Dashboard reads share the one SQLite gate with lifecycle writes

Database intentionally serializes operations around one tokio-rusqlite
connection using Semaphore::new(1). DashboardRepository::load performs many
aggregate/list queries during one Database::call.

For the normal low-write workload this simplicity is desirable. A sufficiently
large dashboard query can nevertheless occupy the sole database gate while
publication/finalization work waits.

This is a measurement target, not a mandate for a general connection pool.

### 6. Streaming bridge and Tokio runtime are evidence-gated opportunities

server/inference.rs currently bridges StreamingExecution into Axum with:

- one spawned task per downstream stream;
- a bounded mpsc channel of 32 chunks;
- ReceiverStream -> Body::from_stream.

main.rs uses Tokio current_thread. The configured server thread count does not
currently select a multithread runtime.

Both choices are simple and safe. Neither should be replaced until the
higher-confidence parse/copy work lands and a representative benchmark shows
material residual overhead.

## API and behavior invariants

This campaign must preserve all externally observable contracts unless a
separate compatibility finding proves an existing bug.

Do not change:

- POST /v1/chat/completions;
- POST /v1/responses;
- POST /v1/responses/compact;
- POST /v1/messages;
- response/error schema and status mapping;
- stream versus finite behavior;
- stateless Responses policy;
- source-native Responses preservation;
- provider-qualified model syntax;
- virtual-model affinity/selector semantics;
- one shared retry/submission budget;
- no retry after streaming handoff;
- provider/account routing, quota, health, or fairness semantics;
- fail-closed proxy behavior;
- existing cancellation/finalization ownership;
- public CLI/config keys;
- ProviderClientPool public behavior;
- --no-default-features behavior;
- exact eggfetch-core 0.1.7 native-http1,tls-rustls profile.

Public Rust inspection/construction types that tests or sibling modules already
use should remain available. Add narrower internal fast paths rather than
deleting existing public helpers simply because the server no longer needs
their fully-owned shape.

## Existing Plan 191 is part of this campaign

Do not write another Eggress migration plan.

plans/191-eggress-1-0-7-facade-migration-and-fallback-retirement.md remains
applicable to the current baseline:

- Eggpool is still exact-pinned to Eggress 1.0.6;
- the default eggress-ssh-fallback feature still activates direct
  implementation crates;
- Eggress 1.0.7 is intended to move production SSH back behind the stable
  eggress-embed facade.

Execute Plan 191 as the dependency/maintenance-footprint lane of this campaign.
Revalidate its registry/version assumptions at execution time, but leave the
historical plan itself intact.

## Implementation sequence

### Phase A — Plan 226: single admission plus zero-copy native request body

Implement the deterministic request-path changes first:

- one authoritative request parse/depth validation on the normal endpoint path;
- no top-level payload clone merely to inspect fields;
- reuse the parsed Value for virtual/concrete model resolution;
- construct finite/stream requests through from_admitted;
- retain the incoming Bytes allocation for unchanged native forwarding;
- preserve existing public wrappers and error semantics.

This is the highest-value work because its cost scales directly with large
Codex/codegg prompts and tool schemas.

### Phase B — Existing Plan 191: Eggress 1.0.7 facade migration

Execute the already-written migration after or independently from Plan 226.

Prefer landing it before Plan 227 if both branches touch provider construction,
so the later ownership cleanup targets the final provider boundary.

### Phase C — Plan 227: coordinator/provider ownership and allocation cleanup

After the admission/body ownership contract is stable:

- add an internal borrowed attempt-preparation path;
- avoid deep AdmittedRequest/provider/header cloning before dispatch;
- keep public AttemptInput/PreparedRequest surfaces available;
- remove allocated account lookup keys and per-request topology mutexes where
  the generation-close contract can be preserved cleanly;
- precompute static routing inputs;
- remove trivial header-name lowercase allocation.

Do not introduce broad Arc wrapping or lifetimes that cross async submission.

### Phase D — Plan 228: qualification and contention characterization

Measure the resulting runtime before changing structural concurrency.

Plan 228 owns:

- before/after request-path measurements;
- dashboard/SQLite gate characterization and query-plan review;
- streaming bridge measurements;
- current-thread versus optional multithread runtime characterization;
- routing selection-lock characterization;
- only evidence-supported structural changes;
- campaign closure evidence.

## Measurement rules

Use comparable builds and workloads.

At minimum record:

- exact commit;
- rustc version and target;
- release/debug profile;
- host class;
- request surface;
- request-body size;
- concurrency;
- warm-up count;
- measured sample count.

Representative local fixture sizes should include at least:

- small: approximately 4-8 KiB;
- medium: approximately 64-128 KiB;
- large: approximately 512 KiB or a representative large agent request below
  the configured body limit.

Exercise:

- native finite Chat Completions;
- native finite Responses;
- native Responses stream;
- one cross-surface/transcoded case;
- one provider-qualified model rewrite;
- one virtual model route.

Prefer loopback provider fixtures so WAN/provider variance is excluded.

Record wall time/throughput and p50/p95 latency. Record RSS and CPU utilization
when the host tooling makes those values reliable. Do not add a heavy benchmark
framework or benchmark-only runtime dependency.

For the request parsing/body work, structural evidence is also important:
document the number of full serde_json parses and full-body copies remaining on
each representative path.

## Scope discipline

Do not add:

- Redis/Postgres;
- a general SQLite pool;
- a second SQLite writer;
- an ORM;
- custom allocators;
- object pools for JSON/request structures;
- HTTP/2/HTTP/3 merely for speed;
- transparent provider retries below the coordinator;
- whole-stream buffering;
- lock-free routing machinery without measured lock contention;
- a permanent large performance test matrix.

Do not weaken correctness tests to obtain better benchmark numbers.

## Validation baseline

Each implementation plan carries focused checks. Before campaign closure, run:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
~~~

When Plan 191 or another dependency/feature change lands, also run cargo deny
and the dependency/feature graph checks required by AGENTS.md.

## Completion criteria

This roadmap is complete when:

1. Plan 226 is implemented and records direct-path parse/copy closure.
2. Plan 191 is either completed or records a current external blocker without
   being duplicated.
3. Plan 227 is implemented with public behavior preserved.
4. Plan 228 records comparable before/after measurements.
5. Any dashboard/runtime/streaming structural optimization is supported by
   measurements rather than intuition.
6. Full default and no-default validation is green.
7. No provider capability, API endpoint, CLI/config contract, retry/finalization
   guarantee, or stream semantic has regressed.
8. This plan receives a closure record summarizing final measured deltas and
   linking the implementation commits.

## Handoff note

The campaign should optimize ownership boundaries, not redesign the product.

The highest-confidence target is the path from Axum Bytes to AdmittedRequest to
the selected wire body. Make that path parse once and retain the original Bytes
allocation when nothing needs rewriting. Only after that work is measured
should the implementation spend complexity on runtime threading, stream-body
plumbing, or extra SQLite connections.

## Campaign closure — 2026-09-20

Plans 226, 227, and 228 are complete. The native request path now admits a
bounded body once, preserves unchanged native `Bytes`, borrows only through
synchronous attempt preparation, and uses an immutable provider/account client
topology with atomic close. Loopback and file-backed SQLite qualification did
not justify a second read connection, stream-body redesign, multithread Tokio
runtime, or routing-lock change. Default/no-default Rust, locked release,
Python tooling, package-boundary, and documentation checks are green. See the
closure records in Plans 226–228 for the exact evidence and limitations.
