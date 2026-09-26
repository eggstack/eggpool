# Request Admission and Wire Roadmap

Status: closed

Long-term references:

- `plans/000-long-term-specification.md` — bounded generation-owned request admission, thin HTTP adapters, and secret-free resource handling.
- `plans/001-terminology-and-domain-model.md` — generation, lease, admission, and request-lifecycle ownership.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — sustaining transport/admission hardening without changing public wire capability.
- `plans/003-planning-process.md` — dependency, handoff, and closure rules.

Related ADRs:

- None required for Milestone 001. The milestone preserves the existing durable ownership decision: EggServe owns downstream H1 transport, while EggPool owns generation-specific application admission. It adds no public configuration key, protocol, storage schema, or reload/restart boundary.

## 1. Purpose and ownership boundary

This subsystem owns the bounded client-request admission path between EggPool's HTTP adapter and its canonical request/wire machinery. The current authority spans `rust/src/server/middleware.rs`, `rust/src/request/`, and the single production endpoint boundary in `rust/src/coordinator/endpoints.rs`.

It consumes:

- EggServe for downstream HTTP/1 parsing, request-body streaming, transport deadlines, framing, and connection lifecycle;
- `RuntimeManager` / `GenerationLease` for the live generation and that generation's `server.max_request_body_bytes`;
- Axum only as the HTTP adapter that carries the bounded body into the coordinator;
- request admission and wire code for one bounded parse, semantic validation, native preservation, and provider adaptation.

It must not own provider retries/finalization, account/model routing, provider transport, EggServe connection implementation, SQLite lifecycle, or public model/wire semantics.

The body-size limit remains generation-owned. Process-local raw-body memory admission is a defense-in-depth resource invariant above that semantic limit, not a replacement configuration authority.

## 2. Work classification

### Invariants

- Every inference route acquires one generation lease before application body admission and uses that generation's live `server.max_request_body_bytes`.
- A request whose declared or observed body exceeds the live generation limit is rejected before coordinator execution.
- Concurrent buffered inference request bodies have an explicit process-local aggregate raw-byte bound; the 1024 EggServe request concurrency ceiling is not allowed to multiply the per-request body ceiling without a second bound.
- Resource reservations are RAII-owned and released on success, rejection, cancellation, disconnect, panic unwinding, and endpoint completion; no permit/resource leak survives a dropped request future.
- Native request preservation, one-parse admission, compact admission, and finite/streaming routing semantics remain unchanged.
- Credentials, prompts, raw bodies, and request content never enter diagnostics, persistence, or resource-admission evidence.
- The 1 GiB EggServe transport/Tower hard ceiling remains unchanged and above the generation-owned live application limit.

### Capabilities

- Existing Chat Completions, Messages, Responses, and Responses Compact request handling.
- Existing live reload of `server.max_request_body_bytes`.
- Existing finite and streaming inference behavior and OpenAI/Codex compatibility.

Milestone 001 adds no new user-visible inference capability.

### Infrastructure

- A process-local weighted raw-body reservation primitive used by the inference admission middleware.
- Early `Content-Length` preflight against the generation-owned live limit.
- Bounded reservation for bodies without a trustworthy known length.
- A downstream request-body read deadline appropriate to LAN/local proxy operation rather than model-inference duration.
- Focused real-socket contention, cancellation, and recovery guards.

### Polish

- Clear architecture documentation separating transport ceiling, live per-request limit, aggregate raw-body memory admission, and downstream upload deadline.
- Stable, secret-free test evidence for the resource bound.

## 3. Non-goals

- No change to EggServe ownership, crate selection, feature flags, static serving, parser implementation, or the completed server-transport M001 integration.
- No new public `server.*` configuration key in Milestone 001.
- No reduction of the existing maximum accepted `server.max_request_body_bytes` value (1 GiB).
- No streaming JSON parser, tempfile/spool-to-disk request path, mmap body store, or provider-body streaming redesign.
- No change to canonical IR, request semantics, model selection, retry policy, provider timeout policy, or response streaming.
- No global worker pool, new Tokio runtime, broad queue, or general-purpose memory allocator/accounting framework.
- No exact RSS claim: the aggregate guard bounds retained raw request bytes, not allocator/serde tree overhead.
- No physical-SBC performance campaign unless correctness evidence exposes a target-class regression that cannot be characterized on loopback.

## 4. Current state

At repository baseline `04f447a4fa459385fddd58ac2cd58f29320725b5`:

- `rust/src/server/middleware.rs::admit_inference_body` acquires the active generation lease, reads that generation's `server.max_request_body_bytes`, then uses `Limited::new(body, limit).collect().await`. The body is therefore bounded per request but fully materialized as `Bytes` before the handler.
- The default live limit is 10 MiB and validation permits values up to 1 GiB. The field remains live-reloadable through `rust/src/config_reload_policy.rs`.
- `rust/src/server/mod.rs::eggserve_runtime_config` independently retains a 1 GiB transport/Tower ceiling and allows up to 1024 connections / 1024 in-flight service calls.
- There is no aggregate application body-buffer budget. The theoretical raw-body exposure can therefore scale with concurrent request count rather than with one explicit process resource bound.
- Known oversized `Content-Length` requests currently cross into body collection before the application limit rejects them; the middleware does not preflight the declared length.
- EggServe's production `body_read_timeout` is currently 24 hours, intentionally paired historically with the long handler budget. Upstream defines this timeout as a downstream request-body read deadline, so it is not provider/model execution policy and does not need the 24-hour value.
- `rust/src/coordinator/endpoints.rs` parses the already bounded `Bytes` once and preserves that backing allocation for native dispatch when possible. Public `FiniteRequest` and `StreamRequest` compatibility shapes must not be changed merely to carry a server resource token.
- Server transport M001 is closed on EggServe 0.4.0 with real-socket framing/body-limit/shutdown coverage. This roadmap consumes that stable interface; it does not reopen it.

## 5. Target architecture

```text
EggServe H1 transport
  hard body ceiling = 1 GiB
  downstream body-read deadline = bounded upload window
        |
        v
Axum auth
        |
        v
acquire GenerationLease
        |
        +--> live_limit = generation.config.server.max_request_body_bytes
        |
        +--> Content-Length known and > live_limit
        |       -> 413 before body collection
        |
        +--> raw-body budget reservation
        |       known length -> reserve declared bytes
        |       unknown/chunked -> reserve live_limit
        |       process effective ceiling -> max(64 MiB, live_limit)
        |       unavailable -> bounded overload rejection; never spin
        |
        v
Limited(body, live_limit).collect()
        |
        v
handler retains reservation through endpoint execution / stream handoff
        |
        v
coordinator one-parse admission -> routing/provider execution
        |
        v
reservation Drop -> process raw-body budget released
```

The aggregate budget is intentionally process-local and internal. A live increase of the generation limit may raise the effective raw-body ceiling enough to admit one request at that configured size; a live decrease does not revoke already-admitted requests, and new requests fail closed until existing reservations fall beneath the new effective ceiling.

The initial internal floor is 64 MiB. Thus the default 10 MiB live limit permits a bounded small number of full-size/unknown-length bodies while ordinary known small requests reserve only their declared size. When an operator deliberately raises the per-request limit above 64 MiB, the effective aggregate ceiling becomes that live limit rather than silently making the configured maximum impossible.

## 6. Dependency graph

```text
Server transport M001 — EggServe 0.4.0 direct Tower runtime (closed)
        |
        | interface: stable streaming body + timeout + abandoned-body behavior
        v
Request-admission-wire M001 — body resource admission hardening
        |
        +-- hard: existing generation-owned live body limit (satisfied)
        +-- interface: coordinator one-parse Bytes boundary (stable)
        `-- soft: future broader request/wire milestones
```

Milestone 001 is closed with its required implementation, real-socket, default,
no-default, tooling, and hosted verification evidence recorded in the closure
status file.

## 7. Milestones

### Milestone 001 — Inference body resource admission hardening

Class: invariant

Objective: bound concurrent retained raw inference request bytes independently of EggServe request concurrency, reject declared oversize bodies before collection, and replace the 24-hour downstream upload deadline with a bounded LAN-appropriate value while preserving existing application/wire semantics.

Dependencies:

- Server transport M001: interface, closed/stable.
- Existing generation lease and live body-limit contract: hard, satisfied.
- Coordinator one-parse request boundary: interface, stable.

Deliverable boundary:

- one narrow process-local body-budget primitive with RAII reservations and no new dependency;
- middleware preflight/reservation integration for all four inference routes;
- explicit overload/cancellation/recovery semantics;
- production downstream body-read timeout reduced to five minutes while the 24-hour handler budget remains untouched;
- focused unit/real-socket tests for known-length, chunked/unknown, contention, cancellation, live-limit changes, and recovery;
- architecture/current-authority documentation.

User or operator value: large or slow client uploads cannot multiply into unbounded raw-body memory pressure simply because the transport admits many concurrent requests; oversized declared requests are rejected without needlessly receiving their body; slow uploads release transport/application resources on a bounded timescale.

Exit conditions:

- all four inference routes share the hardened middleware path;
- default effective aggregate raw-body ceiling is 64 MiB and is never below the active generation's live per-request body limit;
- known declared lengths reserve only their declared bytes and receive 413 before collection when above the generation limit;
- unknown/chunked bodies reserve the full live per-request limit before collection, preventing incremental weighted-budget deadlock;
- aggregate budget exhaustion has deterministic bounded behavior, does not read/retain the rejected body, and does not poison the listener;
- every reservation is released on all terminal/cancellation paths;
- live limit increase/decrease semantics are explicitly tested without changing reload classification;
- downstream request-body timeout is five minutes; model/provider handler timeout remains 24 hours;
- no public config/schema/dependency change and no public request/wire compatibility regression;
- closure record accepted.

Deferred work:

- configurable aggregate-memory budgets, adaptive budgets from machine RAM/cgroups, disk spooling, or streaming JSON parsing require separate capability/design work if real workloads justify them.
- exact serde/allocator amplification accounting is not part of this milestone.

## 8. Cross-cutting requirements

Storage/migration: none. No database or migration change.

Protocol/compatibility: preserve existing success shapes and per-request 413 behavior. Resource exhaustion may use the existing generic 503 application-unavailable envelope; do not introduce a new public schema, bespoke status code, or leak resource counters. Unread rejected request bodies may cause the current EggServe connection-abandon/close behavior; the listener and subsequent connections must remain healthy.

Security/auth: authentication remains outside/above body admission, so unauthenticated inference traffic cannot consume the application raw-body budget. Early size rejection and budget exhaustion must not echo lengths, configured limits, body fragments, credentials, or memory counters in logs/diagnostics.

Concurrency/cancellation/recovery: reservations must be drop-safe and cancellation-safe. Do not implement incremental acquire-as-chunks-arrive because two or more bodies could each retain partial budget and deadlock while waiting for the remainder. Reserve known lengths exactly; reserve the full live limit for unknown/chunked bodies before collection. Avoid spin loops and unbounded waiter queues.

Live reload: `server.max_request_body_bytes` remains live. Admission uses the already-acquired generation lease as the semantic authority. A limit decrease never retroactively cancels admitted work; new requests use the new generation's smaller limit/effective budget. Existing reservations from older generations remain accounted process-wide until dropped.

Observability: tests may expose counters through test-only inspection, but production diagnostics should not gain body-content or per-request memory details. A bounded aggregate in-use gauge is not required for M001.

Performance/resources: ordinary known-length requests should reserve only their actual declared size, avoiding the conservative full-limit reservation used for chunked/unknown bodies. No additional body copy should be introduced. The existing `Bytes` one-parse/native-forwarding path remains intact.

Docs/ops: distinguish four separate concepts: EggServe 1 GiB transport hard ceiling, generation-owned live per-request limit, process-local aggregate raw-body budget, and five-minute downstream upload deadline.

## 9. Verification strategy

Use `rust/tests/server_transport.rs` for real-socket behavior and listener recovery, request/admission/coordinator targets for semantic non-regression, and config/reload tests for generation authority.

Required contention cases include:

- known `Content-Length` above live limit returns 413 without entering body collection;
- multiple known-size bodies cannot exceed the aggregate raw-byte ceiling;
- unknown/chunked requests reserve the full live limit and cannot deadlock through partial reservations;
- budget exhaustion returns the chosen existing 503 envelope without consuming an attacker-controlled body; after a reservation drops, a healthy request succeeds;
- dropping/cancelling a waiting or admitted request releases all reservation state;
- a live body-limit increase and decrease affects only requests acquiring the corresponding active generation and does not leak capacity across retiring generations;
- finite, streaming, and compact requests still cross the same generation-owned admission path;
- the five-minute production body-read timeout is asserted directly; any behavioral timeout test uses a test-specific short duration rather than sleeping for production time.

Rust tests run serial with `--test-threads=1`. Full workspace and `--no-default-features` parity remain closure gates.

## 10. Risks and decision points

- Holding a raw-body reservation only for collection would under-account because the ingress `Bytes` backing can remain alive through request preparation/execution. The reservation lifetime must cover the server/coordinator ownership window, at minimum through finite endpoint completion or streaming handoff.
- Holding reservations through the entire downstream streaming response would be unnecessarily conservative; release once the request body/request execution no longer retains the ingress buffer.
- The aggregate bound tracks raw request bytes, not the serde JSON tree. Do not claim a strict RSS ceiling.
- A new public memory-budget knob would create config/reload/operations surface and is out of scope. If implementation cannot provide a safe internal bound without such a knob, stop and write an ADR/capability plan rather than silently adding one.
- Do not lower the 1 GiB accepted configuration maximum to make memory accounting easier.
- Do not move body-limit authority into EggServe or make EggServe's static 1 GiB ceiling the live application limit.
- If early rejection conflicts with EggServe's abandoned-body/keep-alive semantics, preserve safety by closing that connection; do not drain arbitrarily large rejected bodies merely to keep it reusable.

## 11. Completion definition

M001 is closed on its recorded body-admission evidence. M002–M004 are also closed on the extraction and fidelity/provenance evidence recorded in their closure files. Explicit user direction keeps this roadmap active for the bounded M005 corrective/cleanup pass. It closes again only after M005 reconciles the planning control surfaces, resolves the two low-severity M004 cleanup findings without changing wire behavior, and records closure evidence.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — inference body resource admission hardening | closed | `plans/implementation/request-admission-wire/001-inference-body-resource-admission-hardening.md` | `plans/closure/request-admission-wire/001-status.md` | none |
| 002 — wire-kernel extraction seam and contract freeze | closed | `plans/implementation/request-admission-wire/002-wire-kernel-extraction-seam-and-contract-freeze.md` | `plans/closure/request-admission-wire/002-status.md` | none |
| 003 — sans-I/O wire-kernel extraction and EggPool cutover | closed | `plans/implementation/request-admission-wire/003-sans-io-wire-kernel-extraction-and-eggpool-cutover.md` | `plans/closure/request-admission-wire/003-status.md` | none |
| 004 — fidelity, provenance, and conformance hardening | closed | `plans/implementation/request-admission-wire/004-fidelity-provenance-and-conformance-hardening.md` | `plans/closure/request-admission-wire/004-status.md` | none |
| 005 — planning reconciliation and minor wire cleanup | closed | `plans/implementation/request-admission-wire/005-planning-reconciliation-and-minor-wire-cleanup.md` | `plans/closure/request-admission-wire/005-status.md` | none |


## 13. Wire-kernel extraction extension

The M001 sections above remain the closed record for body admission. This
extension governs M002–M004 and does not reopen M001 implementation.

### Current extraction evidence

At baseline `61470ef788e287c49b4a51062eaba439049d46dc`, the reusable protocol machinery is concentrated in
`rust/src/wire/`, but the files are not yet a clean crate boundary:

- `wire/ir.rs` is mostly provider-neutral, but
  `ReasoningIntent::to_thinking_requirement` reaches into EggPool routing.
- `wire/adaptation.rs` owns the useful bounded loss vocabulary
  (`AdaptationNotice`, `LossPolicy`, exact/adapted/rejected behavior), but
  currently imports EggPool catalog capability types and
  `request::NativeRequestPreservation`.
- `wire/codecs.rs` and `wire/additional_codecs.rs` own OpenAI Chat,
  OpenAI Responses, Anthropic Messages, Gemini Interactions, and Gemini
  generateContent finite codecs, but client decoding calls back into
  `request::admission` and its EggPool-specific policy/limit helpers.
- `wire/registry.rs` mixes neutral codec/profile vocabulary with EggPool
  `Config` conversion.
- `wire/stream.rs` already has the desired sans-I/O incremental SSE decoder,
  terminal-evidence model, translated stream events, and native observation
  machinery with no transport ownership.
- `wire/runtime.rs` is intentionally *not* extraction material: it joins
  request admission, routing facts, selected profiles, body encoding,
  compaction, and EggPool coordinator/runtime policy.

The existing regression surface is substantial and must be treated as the
compatibility contract: `rust/tests/wire_adaptation.rs`,
`wire_codecs.rs`, `wire_multimodal.rs`, `wire_profiles.rs`,
`wire_qualification.rs`, `wire_runtime.rs`, `wire_stream.rs`,
`canonical_request.rs`, `codex_responses_compat.rs`,
`codex_compaction_compat.rs`, and the wire fixtures under
`tests/fixtures/wire/`.

### Target ownership after extraction

```text
EggPool request/server/coordinator/runtime policy
  - body/resource admission
  - stateless Responses product policy
  - token/context estimates
  - routing/catalog/config adapters
  - profile selection and compaction execution
  - provider transport and finalization
                |
                v
sans-I/O wire kernel
  - canonical request/response/event IR
  - presence and reasoning/tool/media semantics
  - bounded native provenance vocabulary
  - loss/adaptation policy and preflight fidelity
  - finite codecs
  - SSE framing/decoding/encoding and terminal evidence
  - neutral codec/profile identifiers
                |
                v
OpenAI Chat / OpenAI Responses / Anthropic Messages /
Gemini Interactions / Gemini generateContent
```

EggPool MUST remain the first and continuously qualified consumer. Extraction
must not introduce a second implementation, translated-payload chaining, an
HTTP/runtime dependency in the kernel, or an externally published artifact as
a prerequisite for EggPool builds.

### Milestone 002 — Wire-kernel extraction seam and contract freeze

Class: invariant

Objective: remove EggPool-only dependencies from the extractable protocol
modules and freeze current finite/streaming behavior before moving source
files. The milestone makes the boundary movable; it does not create a second
codec implementation or change public wire behavior.

Key exit conditions:

- protocol parsing is separated from EggPool's body/resource admission,
  stateless Responses policy, token estimates, routing conversion, and config
  conversion;
- neutral capability/provenance inputs replace imports from EggPool catalog
  and request types, with exact adapters at the EggPool boundary;
- all current numeric/media/depth semantics used by EggPool remain identical;
- a deterministic extraction contract corpus covers exact/adapted/rejected
  outcomes, same-surface preservation, usage, tools, multimodal blocks,
  reasoning, null-vs-missing presence, and stream terminal evidence;
- an enforceable dependency guard proves extractable modules do not import
  routing, catalog, config, request-runtime, provider, database, server, Tokio,
  Axum, Hyper, or transport state;
- all existing wire/Codex/request integration tests remain green.

### Milestone 003 — Sans-I/O wire-kernel extraction and EggPool cutover

Class: infrastructure

Objective: create one internal workspace crate containing the neutral wire
kernel and cut EggPool over to it without changing any client/provider behavior.

Hard dependency: M002 closed.

Key exit conditions:

- the workspace crate is `publish = false` during cutover and has no HTTP
  client/server, async runtime, credentials, retry, routing, SQLite, or config
  ownership;
- EggPool root modules become adapters/re-exports around the crate rather than
  carrying forked codec copies;
- `wire/runtime.rs`, request admission/resource budgeting, routing/catalog
  capability ownership, config projection, compaction execution, and provider
  transport remain in EggPool;
- OpenAI Chat, Responses, Messages, Gemini Interactions, and Gemini
  generateContent finite and streaming compatibility remain unchanged;
- native Responses request preservation and native stream
  observe-and-forward behavior remain native paths and are never forced
  through a lossy canonical re-encode;
- default and `--no-default-features` workspace qualification, dependency
  audit, and feature/dependency graph evidence close with no capability loss.

### Milestone 004 — Fidelity, provenance, and conformance hardening

Class: infrastructure

Objective: after the internal cutover is proven, turn the extracted kernel's
existing loss-aware behavior into a reusable, auditable API without changing
EggPool's externally observable decisions.

Hard dependency: M003 closed.

The public-kernel direction is deliberately narrower than another generic LLM
SDK. Current Rust alternatives already provide canonical provider models and
translation; the useful differentiation is explicit translation fidelity,
bounded source-native provenance, strict terminal evidence, and reusable
conformance vectors.

Key exit conditions:

- a pure preflight translation-plan API reports whether a conversion is exact,
  wire-normalized/semantically equivalent, lossy, or unsupported before
  encoding and uses the same decision engine as actual conversion;
- adaptation effects are typed and redaction-safe; existing EggPool notice
  codes and `LossPolicy::Warn/Reject` behavior either remain stable or have a
  proven compatibility mapping;
- source-native provenance is a separate bounded object from semantic IR, so
  same-surface restoration does not pollute the canonical model with arbitrary
  provider fields;
- provenance never fabricates provider-owned signatures, encrypted reasoning,
  IDs, or terminal events and has explicit completeness/truncation state;
- deterministic cross-surface and arbitrary-chunk-boundary stream conformance
  vectors live with the kernel and are also exercised by EggPool integration
  tests;
- no crates.io publication or repository split is required to close M004.
  External publication is a later release decision after the internal
  consumer has remained qualified.


### Milestone 005 — Planning reconciliation and minor wire cleanup

Class: polish

Objective: close the post-M004 cleanup debt without reopening the completed
wire-kernel architecture. Reconcile the request-admission-wire roadmap and
registry with the accepted M004 closure, eliminate the duplicated
client-executed `tool_search` declaration predicate behind one neutral
kernel helper, and make the currently unemitted `Approximated` effect class
an explicit documented reservation rather than an accidental-looking dead
variant.

Dependencies:

- M002, M003, and M004: hard, closed.
- No external dependency.

Deliverable boundary:

- planning/status reconciliation only; no rewrite of historical closure
  records or legacy plans;
- one neutral `eggpool-wire` classifier for client-executed
  `tool_search` declarations, reused by structural decode and EggPool native
  preservation;
- rustdoc/tests that make `AdaptationEffectClass::Approximated` intentionally
  reserved while current codecs continue to drop unsupported reasoning
  controls rather than claim approximation;
- no protocol, routing, admission, provider, config, storage, dependency, or
  publication change.

Exit conditions:

- the roadmap contains one row per M001–M005 milestone and no stale blocked
  M004 row;
- the registry identifies M004 as the latest completed extraction milestone,
  M005 as the ready/current corrective milestone while work is open, and
  transitions the subsystem to closed when M005 closure is accepted;
- the duplicated `is_client_tool_search_declaration` logic has one semantic
  owner in `eggpool-wire` and both decode/preservation paths prove identical
  acceptance for client/server/missing execution forms;
- `Approximated` is either explicitly documented/tested as reserved or,
  only if repository evidence proves it has no intended API role, removed
  with all exhaustive matches and docs updated; no existing fidelity outcome
  changes;
- `wire_extraction_contract`, `wire_kernel_boundary`, canonical request,
  Codex Responses, default, and no-default workspace suites remain green;
- closure record accepted with no medium-or-higher finding and no remaining
  contradictory request-admission-wire status metadata.
