# Request Admission and Wire Milestone 006 — External Semantic-Producer Consumer Contract

Status: ready

Repository baseline: `8067ad3d1eef5a40ae6e300923d8be3b75437d26`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-006--external-semantic-producer-consumer-contract`

Long-term requirements:

- `plans/000-long-term-specification.md` — preserve canonical intent, bounded adaptation, and transport/runtime ownership.
- `plans/001-terminology-and-domain-model.md` — wire surface, canonical request, request lifecycle, and provider ownership.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — sustaining protocol correctness without expanding runtime authority.
- `plans/003-planning-process.md` — bounded cross-repository handoff and closure evidence.

Applicable ADRs:

- None required. M002–M005 already established `eggpool-wire` as the reusable sans-I/O protocol kernel. This milestone makes that existing boundary consumable by a sibling semantic producer without changing EggPool HTTP protocols, routing, persistence, provider transport, reload semantics, or publication policy. CodeGG will initially pin an immutable EggPool revision; a crates.io/versioning policy change remains a separate release decision.

Primary class: infrastructure

## 1. Objective

Make `rust/crates/eggpool-wire` a clean, directly consumable protocol kernel for callers that already own provider-neutral request semantics, specifically CodeGG, while preserving EggPool's current finite/streaming behavior and keeping the crate sans-I/O.

Close four concrete integration gaps:

1. canonical requests must be constructible without pretending they originated from an OpenAI/Anthropic client wire surface;
2. request encoding must expose a protocol-level opt-in for OpenAI streaming usage without placing transport/runtime policy in the canonical semantic IR;
3. the kernel must expose a bounded provider-neutral accumulator for reconstructing completed tool calls from incremental canonical stream events;
4. callers must be able to encode for a built-in wire surface without fabricating EggPool runtime path/priority configuration.

The result is an external-consumer contract, not an SDK and not a migration of CodeGG policy into EggPool.

## 2. Why this milestone is ready

M002, M003, M004, and M005 are closed. The extracted crate is already the single implementation of canonical IR, adaptation/fidelity, five finite codecs, five streaming decoders, provenance, and conformance vectors. Its normal dependency set remains limited to Serde/Serde JSON/SHA-2/Thiserror/TOML, MSRV is 1.89, and the kernel boundary forbids EggPool runtime/network ownership.

The CodeGG review identified only bounded API/semantic gaps; no new upstream protocol family, provider transport, routing behavior, or persistence capability is required.

## 3. Current implementation evidence

At the baseline:

- `rust/crates/eggpool-wire/src/ir.rs::CanonicalRequest` requires `client_surface: ClientSurface`. Adaptation notices derive source identity from that field and the Responses codec has source-specific behavior, so an external semantic producer cannot honestly construct a request without impersonating one of the three EggPool client endpoints.
- `rust/crates/eggpool-wire/src/codec.rs::WireCodec::encode_request` accepts `ConfiguredWireProfile`, although finite codec implementations use its wire definition/family and do not require EggPool runtime path, stream-path, or priority policy.
- `rust/crates/eggpool-wire/src/codecs.rs` does not encode OpenAI `stream_options.include_usage`. CodeGG's direct OpenAI path currently requests streaming usage unless explicitly disabled, so migration without an explicit codec option would regress accounting evidence.
- `rust/crates/eggpool-wire/src/stream.rs::StreamEventDecoder` correctly exposes `ToolCallStart`, `ToolCallArgumentsDelta`, and `ToolCallStop`, including interleaved/indexed calls. It does not expose a reusable completed-call accumulator; CodeGG currently maintains that assembly inside its provider SSE parser.
- `rust/crates/eggpool-wire/src/conformance.rs` already exports deterministic arbitrary-chunk/terminal vectors suitable for sibling-consumer qualification.
- `architecture/deep-dive-transcoder.md`, crate-level rustdoc, historical closure records, and current package metadata are not fully aligned on whether `eggpool-wire` is "internal", unpublished, or already published. This milestone must reconcile documentation to repository/release truth without performing an unrelated publication.
- `rust/src/wire/adapters.rs` and `rust/src/wire/runtime.rs` remain the EggPool-owned joins for config, routing, admission/native preservation, profile selection, and transport. Those owners must not move.

## 4. Invariants that must not regress

- `eggpool-wire` remains sans-I/O: no HTTP client/server, credential, environment, filesystem, clock, RNG, async runtime, database, logging, provider routing, retry, or account state.
- EggPool remains the first continuously qualified consumer and retains exactly one codec/stream implementation.
- Canonical request origin describes provenance only; it must not become provider selection or routing policy.
- Cross-surface conversion continues to build a fresh target payload from canonical semantics. No translated-payload chaining.
- Native Responses preservation remains an EggPool-owned same-surface path; canonical-origin requests never fabricate native Responses provenance or provider-owned encrypted/signature fields.
- Existing default encoding remains byte/semantic compatible unless the new caller explicitly opts into an additive encoding option.
- Streaming state stays bounded and incremental; no full-stream buffering.
- Tool-call assembly keys concurrent/interleaved calls by stable call identity/source index and never guesses tool identity from textual content.
- Transport EOF is not synthesized into successful completion.
- Errors/notices remain typed and redaction-safe; prompts, bodies, tool arguments, credentials, and cache keys do not enter diagnostics.

## 5. Scope

### In scope

- Add a neutral canonical-request origin representation, or an equivalent API with the same semantics, so a caller can construct provider-neutral requests with no fabricated source surface.
- Preserve exact wire-origin information for requests decoded from Chat Completions, Responses, or Messages.
- Add a small closed request-encoding options type for protocol-level serialization controls, initially covering OpenAI streaming-usage opt-in.
- Add a surface-oriented built-in encode entry point that does not require callers to synthesize `ConfiguredWireProfile` runtime metadata.
- Add a bounded canonical tool-call accumulator over `CanonicalEvent`.
- Export/document the minimum stable types/functions needed by a pinned sibling consumer.
- Add crate-local and EggPool integration regressions proving all existing EggPool behavior stays unchanged.
- Reconcile `eggpool-wire` documentation/package-publication wording to current repository/release truth without publishing a new version in this milestone.

### Explicitly out of scope

- CodeGG source changes.
- Provider HTTP clients, endpoints, authentication, static headers, model discovery, retry, routing, accounts, quota, health, or persistence.
- Provider/model-specific transforms such as Laguna tool aliases, `reasoning_content`, or `chat_template_kwargs.enable_thinking`.
- Bedrock Converse/EventStream support.
- Stateful/hosted Responses execution, background mode, continuation, `previous_response_id`, or server-executed tools.
- General arbitrary provider-extension JSON in the canonical IR.
- A new crates.io release, semver-1.0 promise, repository split, or package rename.
- Changes to EggPool public HTTP behavior.

## 6. Required production changes

### Canonical request origin

Replace the assumption that every `CanonicalRequest` was decoded from an EggPool client endpoint with an explicit origin contract. A canonically constructed request must be representable with no source wire surface. Requests decoded from existing client endpoints must still retain their source surface exactly.

Adaptation/fidelity code must report `source_surface = None` for canonical-origin requests and preserve the current source surface for decoded wire requests. Source-specific Responses preservation/rejection checks must apply only when the request actually originated from Responses syntax, never because an external caller selected Responses as its target.

Do not encode an external application name, provider name, or routing identity into this type.

### Surface-oriented request encoding

Expose a pure built-in API conceptually equivalent to:

```rust
encode_request_for_surface(
    request: &CanonicalRequest,
    target: WireSurface,
    options: &RequestEncodeOptions,
) -> Result<CodecOutput<Value>, CodecError>
```

The exact naming may follow current crate conventions. The helper must select the existing built-in codec/registry definition and must not require path templates, stream paths, priority, auth, provider configuration, or runtime state.

Keep `ConfiguredWireProfile` available for EggPool runtime joins where it is actually authoritative; do not move config/profile selection into the kernel helper.

### Request encoding options

Introduce a closed, typed options structure rather than a free-form provider JSON bag. The first required option is streaming usage inclusion for OpenAI Chat Completions.

Requirements:

- default options reproduce existing EggPool output;
- when explicitly requested and `request.stream == true`, OpenAI Chat encoding emits `stream_options.include_usage = true`;
- the option has no model/provider routing meaning;
- unsupported target surfaces either ignore a demonstrably non-semantic option or return a typed notice/error according to an explicitly tested contract; do not silently reinterpret it as a reasoning/model control;
- no option admits arbitrary keys.

### Completed tool-call accumulation

Add a bounded pure state machine over `CanonicalEvent` that can reconstruct completed provider-neutral tool calls from incremental start/argument/stop events.

It must:

- support OpenAI Chat, Responses, Anthropic Messages, Gemini Interactions, and Gemini GenerateContent event streams through the existing canonical decoder;
- support multiple interleaved tool calls using call ID and source index without cross-contamination;
- accept argument deltas in arbitrary transport chunking because chunking is already normalized below this layer;
- finalize a call on authoritative tool/content/response completion according to the canonical event contract, including protocols that do not emit a dedicated tool-stop event;
- enforce explicit per-call and aggregate retained-byte limits;
- surface malformed/incomplete identity as typed errors rather than inventing IDs/names;
- return arguments as canonical text/bytes (or another protocol-neutral representation) and leave application-specific JSON-schema/tool authorization to the caller;
- release retained state on completion/error/drop and expose no process-global cache.

Do not fold application tool execution, permission policy, or textual tool repair into this accumulator.

### Public API/documentation seam

Crate-level rustdoc must include a no-I/O semantic-producer example covering:

```text
caller semantic request
 -> CanonicalRequest (canonical origin)
 -> encode for one WireSurface
 -> caller-owned HTTP
 -> StreamEventDecoder
 -> canonical event accumulator
 -> caller-owned application events
```

Reconcile the current "internal/publish=false" versus published/package metadata wording. Record the actual supported consumption contract. The milestone does not itself publish a new crate version; a pinned Git consumer is sufficient.

## 7. Ordered work packages

### Work package A — Canonical-origin semantics

Intent: remove the fake-client-surface requirement without weakening provenance/fidelity guarantees.

Required changes:

- introduce the explicit canonical/wire request-origin representation;
- migrate structural decode to stamp the exact client wire origin;
- migrate adaptation/fidelity/source-error construction to use optional source wire identity;
- preserve native Responses-specific behavior only for genuine Responses-origin requests;
- update equality/debug/tests without retaining request contents in diagnostics.

Acceptance evidence:

- canonical-origin request can encode to every supported target;
- its notices/errors carry no fabricated source surface;
- decoded Chat/Responses/Messages requests preserve existing source identities and outcomes;
- native Responses preservation tests remain unchanged.

### Work package B — Consumer-oriented encode API and stream-usage option

Intent: let a semantic producer use built-in codecs without constructing EggPool runtime metadata and without losing CodeGG streaming-usage behavior.

Required changes:

- add the surface-oriented encode helper over existing codec implementations;
- introduce the closed encode-options type;
- implement explicit OpenAI Chat streaming-usage opt-in;
- keep the legacy/runtime encode path behavior-identical by default;
- add typed tests for stream=false, default options, explicit include-usage, and non-OpenAI targets.

Acceptance evidence:

- no EggPool runtime/config type is required by the new helper;
- default EggPool payload fixtures remain unchanged;
- explicit OpenAI usage opt-in emits the expected field exactly once.

### Work package C — Bounded completed-tool-call accumulator

Intent: make incremental canonical stream output directly consumable by applications that operate on complete tool calls.

Required changes:

- add the bounded accumulator and completed-call output type;
- cover parallel/interleaved calls, missing stop events closed by terminal completion, malformed identity, overflow, provider error, incomplete terminal, and reset/drop behavior;
- compose only canonical events; do not parse provider SSE a second time.

Acceptance evidence:

- deterministic tests replay tool-call streams from all five adapters;
- arbitrary SSE byte split points produce identical completed calls;
- malformed/incomplete/overflow cases fail deterministically and boundedly.

### Work package D — External-consumer contract and regression closure

Intent: prove the new seam remains reusable without turning the crate into an SDK.

Required changes:

- add/extend crate examples and rustdoc;
- extend the kernel boundary guard to forbid new runtime/network imports;
- add a source-isolated package/consumer compile test or equivalent fixture that uses only the exported `eggpool-wire` API;
- reconcile publication/internal wording;
- run EggPool root integration suites to prove no existing behavior changed.

Acceptance evidence:

- standalone package dependency tree remains minimal;
- source-isolated consumer compiles without EggPool root;
- all required kernel and root wire suites pass.

## 8. Failure, cancellation, restart, contention semantics

The kernel remains synchronous/pure. It owns no cancellation token, retry timer, task, socket, or restart state. Dropping a decoder/accumulator releases all retained memory.

Accumulator overflow, malformed identity, malformed provider events, provider-error terminal events, and EOF/incomplete terminal states remain distinguishable. A consumer may decide how to map them into its own retry/error taxonomy, but the kernel must not retry or downgrade them.

Concurrent requests use independent decoder/accumulator instances; no global mutable registry/cache is introduced.

## 9. Compatibility and migration

EggPool root behavior is compatibility authority. Existing root facade paths and runtime profile/config joins must continue working.

The new canonical-origin path is additive. Decoded client requests continue carrying their wire origin. Existing callers that use `ConfiguredWireProfile` may remain unchanged; the new helper is for direct semantic producers.

The streaming-usage option defaults to current behavior, so EggPool requests do not gain `stream_options` merely because the option exists.

No storage/config/public HTTP migration is permitted.

For the first CodeGG adoption, an immutable Git revision is the expected consumption mode even if a crates.io artifact exists. Do not block M006 on a publication/release program.

## 10. Required tests

Focused crate tests must cover:

- canonical-origin vs each existing wire-origin adaptation/fidelity source identity;
- all five surface encoders from canonical origin;
- OpenAI streaming usage default/opt-in/non-stream behavior;
- completed tool calls for OpenAI Chat, Responses, Anthropic, Gemini Interactions, and Gemini GenerateContent;
- multiple interleaved calls and call-index fallback;
- malformed/missing ID/name, duplicate/conflicting call identity, byte-limit overflow, provider error, incomplete stream, EOF, and post-terminal data;
- arbitrary transport chunk splits using the existing conformance helpers;
- no-fabrication cases for provider-owned reasoning/signature/native Responses data.

EggPool integration regression targets remain mandatory:

- `wire_extraction_contract`
- `wire_kernel_boundary`
- `wire_codecs`
- `wire_stream`
- `wire_runtime`
- `wire_qualification`
- `wire_adaptation`
- `wire_profiles`
- `wire_multimodal`
- `canonical_request`
- `codex_responses_compat`
- `codex_compaction_compat`

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo test --manifest-path rust/crates/eggpool-wire/Cargo.toml
cargo tree --manifest-path rust/crates/eggpool-wire/Cargo.toml -e features

cargo test --manifest-path rust/Cargo.toml --test wire_extraction_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_kernel_boundary -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_multimodal -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1

cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

Run repository dependency/license/tooling guards required by the current development workflow if the package manifest or dependency graph changes.

## 12. Documentation updates

Update as needed:

- `rust/crates/eggpool-wire/src/lib.rs` — semantic-producer contract, origin semantics, encode options, accumulator, no-I/O guarantees, actual publication status.
- `architecture/deep-dive-transcoder.md` — external sibling-consumer boundary while keeping EggPool runtime joins separate.
- `architecture/overview.md` only if the review index needs a new exported-kernel note.
- `plans/subsystems/request-admission-wire-roadmap.md`, registry, and closure record.

Do not add CodeGG implementation details to EggPool architecture docs; link to the cross-repository consumer only as boundary evidence.

## 13. Acceptance criteria

- A caller with an already-semantic request can construct a canonical request without claiming OpenAI/Anthropic client provenance.
- The caller can encode that request for any existing built-in `WireSurface` without constructing EggPool runtime path/priority/config state.
- OpenAI Chat streaming usage can be requested explicitly without changing default EggPool payloads.
- A caller can feed arbitrary chunks through `StreamEventDecoder` and a bounded canonical accumulator to recover complete tool calls without maintaining a provider-specific SSE parser.
- EggPool retains identical externally observable finite/streaming behavior under default options.
- Kernel dependency/boundary guards remain clean.
- Documentation has one non-contradictory statement of the crate's supported consumption/publication state.
- Closure records an immutable EggPool commit suitable for CodeGG to pin.

## 14. Stop conditions

Stop and report rather than improvise if:

- satisfying CodeGG requires provider/account routing, HTTP, credentials, model catalog, retry, or provider-specific model policy inside `eggpool-wire`;
- canonical-origin support requires an arbitrary provider-extension map in semantic IR;
- the tool-call accumulator requires unbounded buffering or a second provider/SSE parser;
- native Responses exactness would be weakened;
- an existing EggPool wire fixture changes for default options without an explicitly justified compatibility correction;
- the work requires publishing a new crate version or making a semver-1.0 promise;
- the package dependency graph gains an async/network/runtime dependency.

## 15. Closure evidence required

The closure record must include:

- implementation commit(s);
- exact exported API used for canonical origin, surface encoding/options, and completed tool-call accumulation;
- before/after `cargo tree` evidence for `eggpool-wire`;
- package-only and source-isolated consumer results;
- required focused/root/default/no-default test and Clippy results;
- confirmation that default EggPool wire fixtures/public HTTP behavior are unchanged;
- conformance evidence for interleaved/arbitrary-chunk tool calls and terminal/overflow failures;
- documentation/publication-state reconciliation;
- the immutable commit SHA downstream CodeGG should pin;
- severity-classified residual findings and a disposition of closed, conditionally closed, corrective pass required, or blocked.

## 16. Handoff notes

Keep the implementation inside the existing kernel seam. Prefer additive constructors/helpers and small typed options over exposing EggPool runtime structs. Preserve unrelated work and the repository's serial-test requirement. Do not use real provider credentials or live API calls for closure.
