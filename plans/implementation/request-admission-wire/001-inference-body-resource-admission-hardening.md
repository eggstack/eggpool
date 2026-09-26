# Request Admission and Wire Milestone 001 — Inference Body Resource Admission Hardening

Status: closed

Repository baseline: `04f447a4fa459385fddd58ac2cd58f29320725b5`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-001--inference-body-resource-admission-hardening`

Long-term requirements:

- `plans/000-long-term-specification.md` — bounded generation-owned request admission, thin server adapters, resource-safe local operation.
- `plans/001-terminology-and-domain-model.md` — generation lease and admission ownership.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — sustaining admission/transport correctness without widening capability.

Applicable ADRs:

- None required. This milestone preserves the existing EggServe/application ownership split, existing public configuration schema, and existing reload classification. Stop if implementation proves a new durable config or ownership decision is required.

Primary class: invariant

## 1. Objective

Harden the inference request-body boundary so EggPool has an explicit process-local bound on concurrent retained raw request bytes, rejects a known oversized body before receiving it, and no longer permits a downstream upload to occupy request-body resources for 24 hours.

The result must preserve:

- the generation-owned live `server.max_request_body_bytes` semantic authority;
- the exact-pinned EggServe 0.4.0 direct-Tower transport boundary and 1 GiB outer ceiling;
- one-parse `Bytes` admission and native forwarding;
- finite/streaming/compact behavior and public wire compatibility;
- live reload of `server.max_request_body_bytes`;
- no public memory-budget configuration in this milestone.

## 2. Why this milestone is ready

All hard/interface dependencies are already stable:

- server-transport M001 closed the EggServe 0.4.0 direct-Tower runtime and qualified streaming request bodies, malformed-body isolation, keep-alive, and shutdown;
- `RuntimeManager::acquire` / `GenerationLease` already supplies the live generation before body collection;
- `rust/src/coordinator/endpoints.rs` already receives bounded Axum `Bytes`, parses once, and owns finite-versus-streaming classification;
- `server.max_request_body_bytes` is already validated (default 10 MiB, maximum 1 GiB) and classified live.

No upstream EggServe change, dependency bump, migration, or unresolved interface decision is required.

## 3. Current implementation evidence

At the baseline:

1. `rust/src/server/middleware.rs::admit_inference_body`:
   - identifies the four public inference paths;
   - acquires a generation lease before reading the body;
   - reads the lease generation's `server.max_request_body_bytes`;
   - replaces the request body and calls `Limited::new(body, limit).collect().await`;
   - inserts the generation lease and rebuilt `Body::from(Bytes)` into the request.

   This correctly enforces the per-request live limit, but there is no aggregate memory/resource gate.

2. `rust/src/config.rs`:
   - defaults `server.max_request_body_bytes` to 10 MiB;
   - permits values up to 1 GiB.

3. `rust/src/config_reload_policy.rs`:
   - classifies `server.max_request_body_bytes` as `ReloadDisposition::Live`.

4. `rust/src/server/mod.rs::eggserve_runtime_config`:
   - configures 1024 connections and 1024 in-flight requests;
   - keeps a fixed 1 GiB EggServe/Tower request-body hard ceiling;
   - uses a 24-hour handler timeout;
   - also uses a 24-hour request-body read timeout.

5. `rust/src/coordinator/endpoints.rs`:
   - parses the already-collected `Bytes` once with the generation's max-body value;
   - may retain/clone the `Bytes` handle for native/provider dispatch;
   - public `FiniteRequest` / `StreamRequest` compatibility shapes are not an appropriate place to add an application-server resource token.

6. `rust/tests/server_transport.rs` already covers real-socket Content-Length/chunked over-limit behavior and listener recovery, but it does not prove aggregate raw-body admission, early declared-length rejection, or reservation release under contention/cancellation.

The hardening target is therefore downstream EggPool policy, not an EggServe integration defect.

## 4. Invariants that must not regress

- Authentication remains ahead of inference body admission.
- All four inference routes share one admission predicate/path.
- The generation lease is acquired before consulting the live per-request limit.
- `server.max_request_body_bytes` remains live-reloadable and authoritative for semantic request size.
- The 1 GiB EggServe/Tower ceiling remains a static transport defense, not application policy.
- One request is parsed/depth-checked once at the production endpoint boundary.
- Native same-surface forwarding continues to reuse the ingress `Bytes` backing allocation where legal; do not add a second whole-body copy.
- Public `FiniteRequest`, `StreamRequest`, compact, canonical IR, and wire DTO contracts do not gain a server-memory token.
- Streaming retry/finalization ownership does not move into server middleware.
- No raw body, prompt, credentials, cache keys, or provider bodies enter logs, metrics, persistence, or closure evidence.
- Server shutdown/body-task/generation/database ordering remains unchanged.
- Default and `--no-default-features` builds remain green.

## 5. Scope

### In scope

- A narrow process-local raw-body budget primitive, dependency-free, with RAII reservation and bounded secret-free test inspection.
- A 64 MiB internal aggregate floor, with effective admission ceiling `max(64 MiB, live_generation_max_body_bytes)`.
- Known-length preflight:
  - parse the already-normalized `Content-Length` value;
  - return the existing 413 JSON envelope before body collection when declared length exceeds the live generation limit;
  - reserve the declared bytes when valid and within limit.
- Unknown/chunked admission:
  - reserve the full live generation body limit before collection;
  - do not incrementally reserve chunks.
- Deterministic aggregate-budget exhaustion using the existing generic 503 service-unavailable response shape, without body/memory details.
- Reservation lifetime through the request execution window that can retain the ingress `Bytes`; finite/compact paths may conservatively retain it through endpoint completion, while streaming releases no later than upstream response handoff.
- Production EggServe `body_read_timeout` reduction from 24 hours to 5 minutes; keep the 24-hour handler timeout unchanged.
- Unit and real-socket contention/cancellation/reload/recovery tests.
- Current-authority architecture documentation.

### Explicitly out of scope

- Public memory-budget config, environment variable, CLI option, dashboard control, or reload key.
- Lowering the existing maximum `server.max_request_body_bytes` or the EggServe 1 GiB ceiling.
- Streaming JSON parsing, tempfile/disk spooling, mmap, compression admission, or multipart support.
- Provider timeouts, model execution timeouts, response streaming deadlines, retry budget, routing, quota, persistence, or accounting changes.
- EggServe source/version/feature changes.
- New crate dependencies.
- Exact RSS accounting or a general memory manager.
- Physical SBC benchmarking unless a correctness/performance issue appears during implementation.

## 6. Required production changes

### 6.1 Process-local raw-body budget

Add the smallest reusable primitive consistent with request-admission ownership (prefer `rust/src/request/` if it can remain HTTP-agnostic; otherwise keep a narrow private server admission helper and document why).

Required semantics:

- shared process-wide `in_use_raw_body_bytes`;
- effective ceiling supplied per acquisition from the already-leased generation:
  `max(64 * 1024 * 1024, live_max_body_bytes)`;
- one atomic reservation operation, returning an RAII guard on success;
- guard drop subtracts exactly once and wakes/updates waiters if a waiting implementation is used;
- overflow-safe arithmetic;
- no body contents or request identity retained;
- no spin loop;
- cancellation before successful reservation cannot leak usage;
- cancellation/drop after reservation always releases usage.

Prefer immediate overload rejection rather than an unbounded waiting queue. If a short bounded wait materially simplifies fair handoff, it must still be cancellation-safe, use no fixed-sleep polling, and terminate within the downstream body-read deadline. Do not create a general queueing subsystem.

### 6.2 Reservation sizing

After acquiring the generation lease, inspect request framing:

- if a valid normalized `Content-Length` is present and exceeds the live limit, return 413 immediately and do not poll/collect the body;
- if `Content-Length` is present and within limit, reserve exactly that many raw bytes before collection;
- if body length is not known (including chunked), reserve the full live per-request limit before collection.

The existing `Limited` collection remains the authoritative observed-byte guard even after preflight. Early length inspection is an optimization/security hardening, not a substitute for actual body limiting.

Do not implement chunk-by-chunk weighted acquisition. Partial reservations across multiple unknown bodies can deadlock when each request holds some budget but none can obtain enough to complete.

### 6.3 Reservation ownership/lifetime

Insert a private reservation guard into request extensions or another server-private ownership path so each inference handler keeps the reservation alive while the ingress body can still be retained by coordinator execution.

Requirements:

- all four inference handlers receive/retain the guard;
- finite and compact paths must not drop it before the request body is no longer retained for provider preparation/retry;
- streaming must not retain it for the lifetime of the downstream response after the upstream request/body handoff has completed;
- error paths, admission failures, provider failures before handoff, client disconnects, and task cancellation drop the guard naturally;
- do not add the guard to public `FiniteRequest` / `StreamRequest` fields merely to extend lifetime.

A conservative handler-scope lifetime is acceptable for M001 if tests demonstrate bounded concurrency without material ordinary-request regression; avoid a larger coordinator refactor solely to release a few milliseconds earlier.

### 6.4 Aggregate-budget exhaustion

Use the existing generic service-unavailable application response (503) for aggregate raw-body admission exhaustion.

Requirements:

- no new response schema;
- no configured limit, current usage, reservation amount, memory size, or raw body in the error detail;
- do not drain an arbitrarily large rejected body merely to preserve that connection;
- rely on EggServe's bounded abandoned/unread body behavior and prove the listener remains healthy for later connections;
- release no reservation because none was acquired.

### 6.5 Downstream upload deadline

In `rust/src/server/mod.rs::eggserve_runtime_config`:

- retain `disable_connection_total_timeout()`;
- retain 24-hour `handler_timeout` so provider/model execution is not accidentally bounded by the server transport;
- change `body_read_timeout` to five minutes;
- retain header, keep-alive, response-write, parser, connection, in-flight, and graceful-shutdown values unchanged.

Make the distinction explicit in the nearby comment: body-read time is client upload policy; handler time protects long model operations from a transport-owned deadline.

Production tests must assert the exact five-minute value without sleeping five minutes. A behavioral short-timeout test may use a test-only config seam if necessary; do not weaken production timeout solely to make the test fast.

### 6.6 Live reload semantics

Do not change `config_reload_policy.rs` classification.

For each request:

1. acquire the active generation lease;
2. obtain that generation's body limit;
3. derive reservation size/effective aggregate ceiling from that same value;
4. perform preflight/reservation/collection;
5. retain the generation lease and budget guard through the request execution ownership window.

A body-limit decrease does not cancel work already admitted under an old generation. Existing process-wide reservations remain counted until drop; if their sum exceeds the new generation's smaller effective ceiling, new requests fail closed until usage falls. A body-limit increase may admit a larger request immediately under the new generation but does not mutate old request semantics.

## 7. Ordered work packages

### Work package A — Raw-body budget primitive

Intent: introduce one auditable resource-accounting mechanism before touching HTTP behavior.

Required changes:

- implement the RAII budget/reservation primitive;
- overflow-safe acquisition and exact-once release;
- test-only bounded inspection of current usage/capacity decision if needed;
- no dependency or public API growth.

Acceptance evidence:

- exact reservation/release tests;
- concurrent acquisition cannot exceed the supplied effective ceiling;
- failed acquisition leaves usage unchanged;
- dropped guards restore usage;
- cancelled acquisition path leaks nothing;
- arithmetic boundary tests include zero, 64 MiB floor, and 1 GiB live limit.

### Work package B — HTTP preflight and aggregate admission

Intent: make every inference body cross the same per-request + aggregate gate before collection.

Required changes:

- preserve authentication and generation acquisition ordering;
- add known-length preflight;
- compute reservation size as defined in §6.2;
- acquire budget before `Limited::collect`;
- propagate a private guard to every inference handler;
- map aggregate exhaustion to generic 503;
- preserve 413 for per-request oversize.

Acceptance evidence:

- known oversize returns 413 before body poll/collection;
- known in-limit body reserves its declared length;
- chunked/unknown reserves the full live limit;
- budget exhaustion does not consume rejected body and listener recovers;
- Chat Completions, Messages, Responses, and Compact all traverse the same path.

### Work package C — Ownership/cancellation/contention qualification

Intent: prove the budget is a bound, not a leak or serialization hazard.

Required changes:

- deterministic fixture synchronization (accepted connection/body-start/provider-received gates), not arbitrary sleeps/yields;
- concurrent requests that intentionally fill the 64 MiB default effective budget;
- cancellation/drop paths while collecting and while coordinator execution retains the request;
- recovery request after reservation release;
- live generation limit transition coverage.

Acceptance evidence:

- observed test-only in-use raw bytes never exceed effective ceiling;
- no leaked usage after every terminal path;
- a blocked/rejected request cannot poison later healthy traffic;
- live increase/decrease behavior follows §6.6;
- streaming releases the body reservation by or before downstream stream handoff, not at stream completion.

### Work package D — Correct client-upload timeout

Intent: separate downstream upload defense from long model/provider execution.

Required changes:

- set production EggServe body-read timeout to five minutes;
- keep handler timeout 24 hours;
- update existing config-ownership unit guard to assert both;
- if feasible without production complexity, add a short test-only timeout fixture proving a stalled/incomplete body terminates and the listener remains healthy.

Acceptance evidence:

- exact production values asserted;
- no provider/model timeout changes;
- existing stalled downstream-response shutdown test remains green;
- incomplete/slow request behavior remains isolated.

### Work package E — Compatibility and documentation closure prep

Intent: prove no semantic/API expansion.

Required changes:

- update request-lifecycle/current architecture docs;
- if comments in server transport docs imply 24-hour body reads are provider policy, correct them without rewriting historical plans;
- record no dependency/config-schema/storage migration.

Acceptance evidence:

- focused and full verification below;
- no Cargo manifest/lock delta unless separately justified and then treated as scope expansion;
- no public request structs or wire DTOs changed for budget bookkeeping.

## 8. Failure, cancellation, restart, contention semantics

Per-request oversize:
- known declared oversize: 413 before body polling;
- observed oversize despite declaration/unknown framing: existing `Limited` 413 path;
- reservation is dropped on the latter path.

Aggregate exhaustion:
- generic 503; body/memory details are not exposed;
- unread body may make the current H1 connection non-reusable; listener/process remains healthy.

Cancellation/disconnect:
- dropping middleware/handler futures releases the reservation through RAII;
- no detached budget task;
- no completion message required for release.

Contention:
- no incremental per-chunk reservation;
- no spin;
- if implementation waits at all, waiting is bounded/cancellation-safe and does not hold partial bytes.

Reload:
- old admitted requests complete under old generation semantics;
- new requests use new generation semantics;
- process-wide usage from old requests remains counted until release.

Restart:
- budget is in-memory only and starts empty on process start; no persistence/migration.

Shutdown:
- budget state does not become a new shutdown authority or task;
- reservations disappear as request futures/body tasks are dropped/joined under the existing shutdown sequence.

## 9. Compatibility and migration

Public HTTP request/response schemas: unchanged.

Status behavior:
- existing 413 semantics retained for per-request body size;
- aggregate process resource exhaustion uses existing generic 503 service-unavailable shape.

Configuration:
- no new key;
- `server.max_request_body_bytes` validation/default/max/reload disposition unchanged;
- five-minute downstream body-read deadline is internal runtime policy, not a new config surface.

Data/storage: no migration.

Rust compatibility surfaces:
- do not add a required field to public `FiniteRequest` or `StreamRequest`;
- keep public request admission helpers usable by tests/embedding callers that do not pass through the HTTP server. The process body budget is an HTTP/server runtime concern layered before those semantic helpers, not retrofitted into every public pure admission function.

EggServe:
- no version/feature/source change;
- 1 GiB hard ceiling and direct Tower adapter unchanged.

## 10. Required tests

Add/extend the narrowest existing authorities.

`rust/src/request/` unit tests:
- budget floor/effective-ceiling calculation;
- exact known-size reservation;
- full-limit unknown reservation;
- overflow/zero boundaries;
- RAII release;
- failed/contended acquisition no leak.

`rust/src/server/` unit tests:
- production EggServe body-read timeout = 5 minutes;
- handler timeout remains 24 hours;
- policy/admission ownership remains EggServe-owned where already asserted.

`rust/tests/server_transport.rs` real-socket tests:
- known `Content-Length` above the live generation limit returns 413 without body collection;
- aggregate budget contention returns generic 503 and later healthy request succeeds;
- chunked/unknown reservation cannot exceed aggregate bound and cannot deadlock;
- disconnect/cancellation releases budget;
- finite and streaming paths remain correct;
- compact route remains on common admission;
- malformed/incomplete request isolation remains green.

Reload/generation tests:
- use the existing runtime/reload test authority (or smallest unit-level generation fixture) to prove a live max-body increase/decrease changes new-request admission only and does not leak old reservation state.

Semantic compatibility:
- existing `canonical_request`, coordinator C008/C009/C011, boundaries, Codex Responses, and Codex compact suites remain green.

Do not add fixed sleeps to prove acquisition/cancellation ordering. Synchronize on explicit fixture events under bounded `tokio::time::timeout`.

## 11. Required verification commands

Run from repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo test --manifest-path rust/Cargo.toml --lib request:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib server:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1

cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

If `Cargo.toml` or `Cargo.lock` changes unexpectedly, stop and explain why. A justified dependency/feature delta additionally requires:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Closure must distinguish local results from hosted CI. Do not claim commands that were not run.

## 12. Documentation updates

Update current-authority documentation only:

- `architecture/deep-dive-request-lifecycle.md` — add aggregate raw-body admission and known-length preflight to the bounded-admission description.
- `architecture/overview.md` and/or `architecture/deep-dive-runtime.md` only if needed to clarify process-local budget ownership and shutdown/reload semantics.
- `rust/README.md` only if its server section currently describes body timeout/admission in a way made false by this work.
- `AGENTS.md` only if a durable implementation invariant needs to be added for future agents.

Do not rewrite historical Plans 244–250 or server-transport M001 closure evidence.

## 13. Acceptance criteria

- One process-local RAII raw-body budget exists with no new dependency or public config.
- Effective aggregate raw-body ceiling is `max(64 MiB, live_generation_max_body_bytes)`.
- All four inference routes acquire a generation lease before body-size/budget admission.
- A known declared body larger than the live limit returns 413 before body collection.
- A known in-limit body reserves exactly its declared raw bytes.
- An unknown/chunked body reserves the full live per-request limit before collection.
- No partial/chunk-wise reservation algorithm can deadlock multiple bodies.
- Aggregate exhaustion returns only the existing generic 503 shape and does not expose resource values.
- Listener/process remain healthy after oversize, exhaustion, incomplete upload, and disconnect paths.
- Reservation usage returns to baseline after success, admission failure, provider failure, cancellation, disconnect, and shutdown.
- Live max-body increase/decrease affects new requests according to their acquired generation; existing reservations are not revoked and are still counted until drop.
- No second full ingress body copy is introduced.
- Public request structs, canonical IR, wire surfaces, status-success schemas, routing, retry, and persistence remain unchanged.
- EggServe 0.4.0/Tower integration and 1 GiB outer ceiling remain unchanged.
- Production downstream body-read timeout is five minutes; handler timeout remains 24 hours.
- Default and `--no-default-features` full verification pass.
- No medium/high/critical unresolved finding remains at closure.

## 14. Stop conditions

Stop and report rather than improvise if:

- a safe aggregate bound requires a new public config/reload key or durable ownership decision;
- the implementation would need to reduce the supported per-request maximum below 1 GiB;
- preserving native one-copy forwarding requires changing public `FiniteRequest` / `StreamRequest` API shapes;
- EggServe's unread/abandoned body behavior makes early rejection unsafe without an upstream change;
- aggregate accounting cannot be cancellation-safe without a detached task/general queue;
- a body-timeout change unexpectedly changes model/provider handler lifetime;
- live reload cannot preserve generation-owned limit semantics;
- a dependency/Cargo graph change appears necessary;
- scope expands into provider transport, routing, persistence, or response streaming;
- focused tests show a material regression that requires architecture beyond this bounded hardening pass.

If an upstream EggServe defect is discovered, preserve the current integration and create a separate upstream plan; do not patch around it by copying transport code into EggPool.

## 15. Closure evidence required

Create `plans/closure/request-admission-wire/001-status.md` with:

- implementation SHA and exact baseline;
- requirement-to-evidence matrix for every acceptance criterion;
- final budget formula/constant and ownership/lifetime description;
- real-socket evidence for early 413, aggregate exhaustion/recovery, chunked/unknown admission, disconnect/cancellation release, finite/streaming/compact behavior;
- live limit increase/decrease evidence;
- production timeout value evidence (5-minute body read / 24-hour handler);
- confirmation no public config/dependency/storage migration occurred;
- confirmation public request/wire types did not gain server budget state;
- exact focused/full/default/no-default/tooling commands and results;
- hosted CI result for the implementation candidate;
- security/privacy review confirming no request content/resource detail leakage;
- failure/cancellation/reload/shutdown review;
- severity-tagged residual findings;
- roadmap/registry disposition and blocked-work promotion audit.

A compile-only result or unit-only budget test is insufficient for closure.

## 16. Handoff notes

Treat this as admission/resource correctness, not an EggServe migration.

Use explicit fixture synchronization; do not use fixed sleeps/yield counts. Keep Rust tests serial.

Preserve unrelated user changes. Do not add a public knob because one might be convenient. The internal 64 MiB floor is deliberate for the local/SBC deployment profile while still permitting one request at any explicitly configured live per-request size up to the existing 1 GiB maximum.

The key implementation trap is reservation lifetime: releasing immediately after `collect()` would under-account because the same ingress `Bytes` backing may remain alive during coordinator/provider preparation. Retain the guard across that ownership window, but do not carry it through the full downstream streaming response after request handoff.
