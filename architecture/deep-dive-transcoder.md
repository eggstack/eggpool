# Deep Dive: Protocol Transcoding

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md).

`rust/src/wire/` is the closed, provider-independent codec boundary between the OpenAI
Chat Completions, OpenAI Responses, Anthropic Messages, and Gemini surfaces. Every encode and
decode builds a fresh target payload from canonical intent: codecs never chain translated payloads,
and cross-surface preparation rejects native-only semantics or emits bounded adaptation notices
rather than dropping meaning silently.

## Canonical IR first

`wire::ir` is the EggPool canonical boundary facade over `eggpool_wire::ir`, re-exported type-identical
so admission, routing, runtime, codecs, and tests share one type. `CanonicalRequest`, `CanonicalResponse`,
`CanonicalEvent`, and `CanonicalUsage` carry provider-neutral meaning; `CanonicalToolKind::Freeform`
(Responses `custom`) and `CanonicalToolKind::DeferredSearch` (client-executed `tool_search`) keep
tool intent neutral; `ProviderErrorEvidence` and `ClientSurface` frame error and surface facts. The IR
never carries opaque provider JSON — that residue lives in provenance (below), and cross-surface encode
consults the semantic IR plus the plan, never source extras.

## Kernel seam and EggPool-owned joins (M002)

The pure sans-I/O kernel is `ir`, `adaptation` (neutral), `codec`, `codecs`, `additional_codecs`,
`decode`, `registry` (neutral), `stream`, plus `fidelity`, `provenance`, and `conformance`. It never
imports EggPool routing, catalog, config, request-runtime state, model-router, provider, database, server,
coordinator, Tokio, Axum, Hyper, TLS, or transport types — guarded by `rust/tests/wire_kernel_boundary.rs`,
which scans both the root facades and the extracted crate sources and pins the root files to
second-implementation-free facades. `rust/tests/wire_extraction_contract.rs` freezes the
exact/adapted/rejected corpus (ordered adaptation codes, presence semantics, native preservation, usage,
tool, and media cases, stream terminal/chunk behavior) so the move changes no behavior.

EggPool-owned joins live outside the kernel: `wire::adapters` maps catalog, request-preservation, routing,
and config facts into neutral types (`neutral_capability_status`, `neutral_thinking_capability`,
`neutral_native_summary`, `reasoning_capability_notices`, `native_preservation_notices`,
`provenance_from_preservation`, `thinking_requirement_from_intent`, `configured_profiles[_from_facts]`,
`compaction_capabilities_from_surface_config`, `validate_provider_references[_neutral]`); `wire::runtime`
owns selection-time joining (`WireRuntime`, `WireRuntimeContext`, `WireRuntimeIdentity`); and
`request::admission` owns the single bounded parse with `DecodeLimits::current()` passed through unchanged
(`MAX_JSON_DEPTH` 64), stateless Responses policy, token/context estimates, and the routing projection.

## Workspace split (M003)

The kernel is the single source of truth in `rust/crates/eggpool-wire` (published as `eggpool-wire 0.1.0` on crates.io; dependencies
only on `serde`/`serde_json`/`thiserror`/`sha2`/`toml`; MSRV 1.89; `unsafe_code = forbid`; no credential,
environment, filesystem, network, clock, random, async, database, or logging effects). Neutral `registry`
moved to `profile.rs`; every other module kept its file name. Root `rust/src/wire/` modules are narrow
facades (`pub use eggpool_wire::...`) preserving the canonical `wire::ir` path; `wire::adapters`,
`wire::runtime`, and `request::admission` remain EggPool-owned. Root integration tests stay authoritative and
the crate runs its own package tests; no new binary or packaging artifact was introduced.

The crate is also consumable by sibling semantic producers: `CanonicalRequest::from_canonical`
marks canonical origin without inventing a source surface, while decoded requests retain
their exact `RequestOrigin::ClientWire`. `encode_request_for_surface` selects the built-in
codec without runtime path/priority metadata; `RequestEncodeOptions` keeps protocol
serialization controls closed (currently OpenAI Chat streaming usage opt-in). The bounded
`CanonicalToolCallAccumulator` consumes canonical events and owns no SSE parser or tool
execution policy. EggPool remains the continuously qualified consumer and owns all HTTP,
admission, profile selection, routing, and transport joins. Crates.io currently contains
version 0.1.0; this milestone does not publish or promise a new semver contract, and the
first sibling integration should pin an immutable EggPool revision.

## Codecs and the closed registry

Five wire surfaces (`WireSurface`: `OpenaiChatCompletions`, `OpenaiResponses`, `AnthropicMessages`,
`GeminiInteractions`, `GeminiGenerateContent`) pair with request/response codecs (`WireCodecId`, `WireCodec`,
`builtin_codec_instance`: `OpenAiChatCodec`, `OpenAiResponsesCodec`, `AnthropicMessagesCodec`,
`GeminiInteractionsCodec`, `GeminiGenerateContentCodec`) and five streaming dialects (`StreamAdapterKind`).
`WireProfileRegistry` accepts only compiled codec IDs; `ConfiguredWireProfile` plus its
`WireProfileDefinition` must match the registered definition at use time (`validate_context` rejects
drift, empty identities, and oversized context fields). `CompatibilityPath` (via `compatibility_path` and
`WireProfileFlags::for_surfaces`) decides native versus translated handling; failures are typed
(`CodecError` with `CodecReasonCode`, `WireRuntimeError`, `DecodeError`, `MediaLimitError`) and malformed
wrappers fail closed instead of forwarding wrapper JSON as text.

## Adaptation policy and fidelity preflight

Shared pure policy lives in `adaptation`: `request_notices` and `native_summary_notices` evaluate loss
against `AdaptationPolicy`/`LossPolicy` with at most `MAX_ADAPTATION_NOTICES` (32) notices, covering
reasoning, tools, structured output, multimodal content, and cache controls. `TranslationPlan` preflights
the same decision the encoders will make: `Fidelity` (`Exact`, `WireNormalized`, `SemanticallyEquivalent`,
`Lossy`, `Unsupported`) plus ordered `AdaptationEffect`s (`AdaptationEffectClass`: `Rewritten`, `Omitted`,
`Approximated` reserved, `Synthesized` response-only, `Blocked`) via `plan_request_translation`,
`classify_notice`, `effects_for_notices`, and `fidelity_for_response_notices`. Routing never selects
providers from `Fidelity`: this is a protocol kernel, not an SDK, router, agent framework, tool executor,
store, or gateway API.

`WireProvenance` carries the bounded source-native residue separately from the IR — source identity, counts,
and structural field paths within `MAX_PROVENANCE_FRAGMENTS`/`MAX_PROVENANCE_TOTAL_BYTES` (plus depth and
name caps), redaction-safe in `Debug`, same-request lifetime, no persistence contract. Same-surface exact
restore may use provenance plus EggPool-owned model rewrites; cross-surface encode never consults it; and
incomplete provenance can never claim `Exact` (`may_restore_exact`/`may_restore_exact_for`).

## Two bounded Responses paths

Admission emits the canonical IR and, for Responses, a separate source-native preservation envelope
(`NativeRequestPreservation` with its content-free `NativeFeatureSummary`). Ordinary targets use canonical
semantic adaptation. A Responses-to-Responses target may instead preserve ordered native input items,
encrypted reasoning, native tool definitions, and extensions, applying only the EggPool-owned `model`
rewrite for an alias target. A non-Responses target consults the preservation summary first: native-only
items or tools (`has_cross_surface_blocker`) are a typed `UnsupportedSemanticFeature` blocker, while safely
omittable extensions become explicit adaptation notices.

## Tools: freeform and deferred search

Responses `custom` tools map to `CanonicalToolKind::Freeform` with a deterministic single-string `input`
wrapper; client-executed `tool_search` maps to `CanonicalToolKind::DeferredSearch` reusing the exact bounded
`query`/`limit` schema. Function-only codecs wrap freeform bodies in one bounded `input` string and expose
deferred search with its declared schema, using the per-request declaration — never the name alone — to
reconstruct downstream `custom_tool_call`/`tool_search_call` items. Namespaces, hosted/server search, shell,
image generation, and other server-owned tools stay native-only unless a general canonical equivalent is
added; EggPool never executes the search itself.

## Remote compaction

Provider capabilities (`supports_remote_compaction_v1` plus an optional compact path,
`supports_remote_compaction_v2`) live on the provider-owned `wire_surfaces` table and resolve at dispatch
into `CompactionCapabilities` via `compaction_capabilities_from_surface_config`; the closed registry stays
data-only. The v1 compact operation (`prepare_compact_dispatch`, finite-only, Responses-surface only)
preserves the source-native compact envelope exactly except for the EggPool-owned model rewrite (or the
configured `compact_path_template`), and its result is bounded and validated as `CompactResponse`
(`CompactResponseOutcome::Success` versus `ProviderError`) without canonicalizing into an ordinary
completion. A v2 `compaction_trigger` input item is a native-only signal: translated targets reject it as
`UnsupportedSemanticFeature`, and native targets preserve it only with explicit v2 capability — neither form
is ever converted to user text. Production compact execution takes the private single-owner
`CompactAdmittedRequest` (`execute_compact_admitted`) so the preserved JSON tree is never duplicated, while
public `FiniteRequest` constructors stay unchanged.

## Streaming: observe-and-forward versus synthesis

Responses-to-Responses streaming uses observe-and-forward: the bounded `SseDecoder` frames each event within
`MAX_SSE_FRAME_BYTES`, `StreamEventDecoder::observe_native_push` observes terminal and usage facts, and the
original valid provider bytes — including unknown future event types — go to the caller unchanged, with the
final `StreamTerminalSummary` available exactly as on the translated path. Cross-surface output uses the
stateful `ClientStreamEncoder`: bounded active text, reasoning, and argument buffers; indexed completed
message, reasoning, function, custom, and deferred-search call items; a generated output-item id distinct from
the canonical invocation `call_id`; deferred fragments accumulated silently by source index and call identity
(no invented delta grammar; the authoritative `tool_search_call` done item carries the bounded `query`/`limit`
object); interleaved call deltas kept with their source index while later tool outputs pair by `call_id`.
A native terminal event is required — translated Responses output demands `response.completed` (terminal
evidence `ResponsesCompleted`; `Incomplete`/`ProviderError` terminal states converge as failure or incomplete,
never success) — and transport EOF is never synthesized into success. The encoder never fabricates OpenAI
encrypted reasoning content. Protocol-only vectors (`stream_conformance_vectors`, `sse_split_points`) pin
framing and terminal classification, including `response.completed` terminal vectors.

## Admission boundary (no duplication)

`request::admission` (`admit_request`, `admit_compact_request`, `AdmissionOptions`, `AdmittedRequest` with
`routing_facts`, content-free `Debug`) owns one bounded parse, stateless Responses policy (`store` omitted or
false accepted; `store: true`, `previous_response_id`, conversation references, and background execution
rejected as `StatefulResponsesFeature`), media/document caps, token and reservation estimates, and the native
envelope. It never encodes provider payloads, never plans translations, and never stores credentials, prompts,
raw bodies, or cache keys.

## Invariants

- Canonical IR before adaptation: every encode builds a fresh target payload from canonical intent; translated payloads are never chained.
- Two bounded Responses paths only: canonical semantic adaptation, or source-native same-surface preservation with at most the EggPool-owned `model` rewrite.
- Native-only semantics are blockers or explicit notices cross-surface — never silent drops, never invented reasoning, metadata, ids, or terminal events.
- Same-surface exact restore requires complete provenance (`may_restore_exact[_for]`); incomplete provenance can never claim `Exact`, and cross-surface encode never consults provenance.
- Compact stays native, finite, and single-owner: v1 preserves the source envelope with model-only rewrite, v2 triggers stay native-only signals, and the preserved tree is never duplicated or canonicalized into a completion.
- Streams require native terminal evidence (`response.completed` for translated Responses output); EOF and malformed frames are typed failures, and unknown valid native events forward unchanged.
- The kernel stays sans-I/O and EggPool-free (boundary test enforced); all routing, catalog, config, transport, retry, and persistence joins live outside it.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_extraction_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_kernel_boundary -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
```
