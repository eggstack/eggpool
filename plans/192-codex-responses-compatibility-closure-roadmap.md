# Plan 192: Current Codex Responses compatibility closure roadmap

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` at `51d770e16598cdea76ed56670f68b885f7f68ed5`
>
> **External baselines:** OpenAI Codex `508a006d7aaa485ac0367c9e45c69ebb948af518`; OpenCodex `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`
>
> **Parent context:** Plans 139, 143–145, 147–160, and 183
>
> **Scope:** make an unmodified current Codex client reliable against Eggpool's `/v1/responses` endpoint over HTTP/SSE, including multi-turn replay, function/custom tool calls, reasoning continuity where representable, same-surface fidelity, translated-provider operation, and explicit-model discovery/configuration. Preserve Eggpool as a general provider router; do not specialize the product around Codex.

## Executive summary

Eggpool already has the hard integration boundary Codex needs: a `/v1/responses` client surface, custom-provider-compatible HTTP routing, stateless request handling, explicit model aliases, and provider wire negotiation. The remaining incompatibility is not provider registration. It is Responses wire fidelity.

The current Rust runtime admits every request into `CanonicalRequest`, optionally forwards the original body when the client and upstream surfaces are native-compatible, decodes every SSE stream into canonical events, and re-encodes those events for the downstream client. That architecture is sound for genuine cross-protocol translation, but it is currently too lossy for the richer Responses contract used by Codex.

Three current implementation facts are decisive:

1. `rust/src/request/admission.rs` accepts only `message`, `function_call`, and `function_call_output` as typed Responses `input` items. Current Codex can replay `reasoning`, `custom_tool_call`, `custom_tool_call_output`, `local_shell_call`, `tool_search_call`, `tool_search_output`, `web_search_call`, `image_generation_call`, compaction-related items, and future protocol items.
2. `rust/src/wire/runtime.rs` has request-body native passthrough, but disables it when Eggpool rewrites a canonical model alias to a different upstream model ID. A normal Eggpool alias therefore falls back to `encode_responses_request()`, which reconstructs a smaller request and loses Responses-native fields and item types.
3. streaming has no native passthrough equivalent. `WireStream` always runs provider bytes through `StreamEventDecoder` and `encode_client_event()`. In `rust/src/wire/stream.rs`, a Responses function call is exposed as `response.output_item.added` plus argument deltas, while canonical `ToolCallEnd` currently emits no Responses event. Current Codex intentionally ignores ordinary `response.function_call_arguments.delta` and `.done` for durable tool dispatch and commits the call from `response.output_item.done`.

OpenCodex independently demonstrates the correct bridge shape. Its current Responses SSE translator is stateful: it owns a stable response ID, output indexes, sequence numbers, distinct Responses item IDs and tool `call_id`s, accumulated argument buffers, reasoning state, and completed-item collectors. It emits `response.output_item.done` for assistant messages, reasoning items, function calls, custom tool calls, and server tools before `response.completed`. Its native/opaque recovery code also keeps protocol-specific state separate from normalized chat semantics rather than pretending all Responses items are ordinary messages.

The closure should therefore restore a deliberate two-path architecture:

```text
Responses client -> Responses upstream
    bounded admission + routing facts
    preserve source-native request/event semantics
    rewrite only fields Eggpool owns (primarily model/auth/endpoint)
    observe stream for terminal/usage state without rebuilding it

Responses client -> non-Responses upstream
    bounded admission
    canonical semantic translation
    explicit loss/failure policy for native-only features
    stateful Responses SSE synthesis on the downstream side
```

This is smaller and more future-proof than expanding the universal canonical IR until it mirrors the full OpenAI Responses schema.

---

## Relationship to Plans 143–145

Plans 143–145 correctly identified stateless Codex compatibility and preferred same-protocol passthrough. Their compatibility proof is no longer sufficient for the current Rust runtime because the subsequent wire-profile/canonical-stream work changed the execution boundary:

- request passthrough exists, but only when the model ID is unchanged;
- Responses request admission rejects current replay items before passthrough can help;
- SSE is always decoded and re-encoded when a `WireStream` exists;
- the current canonical stream event model does not carry enough Responses lifecycle state to recreate authoritative completed output items statelessly.

Treat this plan and Plans 193–195 as the current closure specification. Do not rewrite the historical plans beyond an optional one-line supersession pointer.

---

# Compatibility target

The supported initial Codex configuration is an ordinary custom provider using HTTP/SSE:

```toml
model = "<eggpool-model-or-alias>"
model_provider = "eggpool"

[model_providers.eggpool]
name = "Eggpool"
base_url = "http://127.0.0.1:<port>/v1"
env_key = "EGGPOOL_API_KEY"
wire_api = "responses"
supports_websockets = false
```

Exact field names should be rechecked against the Codex baseline used by the implementation pass, but the compatibility contract is:

- Codex remains unmodified;
- WebSocket Responses is not required;
- `POST /v1/responses` with SSE is required;
- `store: false` stateless turns are required;
- explicit Eggpool model IDs/aliases are required for the initial closure;
- model-picker population from Eggpool is not required for the initial closure;
- a clean stream must contain a valid `response.completed` event before EOF;
- tool and assistant output that Codex must retain must arrive as complete `response.output_item.done` items.

## Required request features

At minimum accept and correctly preserve or deliberately translate the current Codex request envelope:

- `model`;
- `instructions`;
- typed `input` history;
- `tools` including function and custom/freeform tools where used by the selected Codex model configuration;
- `tool_choice`;
- `parallel_tool_calls`;
- `reasoning` settings;
- `include`, especially `reasoning.encrypted_content`;
- `store: false`;
- `stream: true`;
- `stream_options` when present;
- `prompt_cache_key` when present;
- `service_tier` when present;
- `text` controls when present;
- client metadata/extensions that are safe to forward on the native path.

Stateful Responses features remain outside Eggpool's local-stateless contract unless separately designed. Re-validate and fail closed for fields such as `store: true`, `previous_response_id`, conversation ownership, and background execution rather than accidentally forwarding them after the native-preservation work.

## Required SSE features

The downstream Responses surface must correctly handle:

- `response.created`;
- text deltas plus a completed assistant message item;
- reasoning summary/raw reasoning deltas when translated;
- ordinary function-call lifecycle;
- custom/freeform tool-call lifecycle when used;
- server-tool items already supported by Eggpool;
- `response.completed` with a non-empty stable response ID;
- `response.failed` and `response.incomplete`;
- premature EOF as failure rather than success.

---

# Current source findings

## Request admission

Primary file: `rust/src/request/admission.rs`.

`decode_message_array()` currently rejects every Responses input item type other than `message`, `function_call`, and `function_call_output` with `UnsupportedContent { kind: "input item" }`.

`decode_tools()` currently treats every tool object as if it were a normal function-like `CanonicalTool`. This is sufficient for existing generic function tools but does not preserve the semantic distinction among Responses `function`, `custom`, `namespace`, `tool_search`, and `web_search` definitions. A native Responses request can therefore be rejected or semantically altered before routing.

The admission layer also drops the parsed source JSON after constructing `CanonicalRequest`; only raw bytes and canonical semantics survive in `AdmittedRequest`.

## Request runtime

Primary file: `rust/src/wire/runtime.rs`.

`WireProfileFlags::for_surfaces()` marks native-compatible request surfaces as `body_passthrough`.

`prepare_admitted_request()` forwards raw request bytes only when all of these are true:

- the selected path is native-compatible;
- body passthrough is enabled;
- client/upstream surfaces are native;
- `upstream_model_id == admission.canonical.model`.

If Eggpool resolves an alias to a different upstream model ID, the request is rebuilt through the finite codec. That means the aliasing function itself currently disables the most important fidelity path.

## Responses request encoder

Primary file: `rust/src/wire/additional_codecs.rs`.

`encode_responses_request()` reconstructs `input` from canonical messages and emits only canonical function-call/result forms. It forces `store: false`, maps selected controls, and cannot reproduce arbitrary source-native Responses items or extension fields. This remains appropriate for cross-surface translation; it should not be the default path for native Responses requests that merely need a model rewrite.

## Stream runtime

Primary files:

- `rust/src/wire/stream.rs`;
- `rust/src/wire/runtime.rs`;
- `rust/src/coordinator/streaming/execution.rs`.

`WireStream` owns a `StreamEventDecoder`, but the client encoder is a stateless free function. `ActiveStream::decode_chunk()` always pushes a chunk through M6 and emits the bytes produced by `wire.encode_client_event(event)` whenever `wire` is present. There is no "observe but forward raw bytes" mode.

For Responses specifically:

- `response.output_item.added(function_call)` becomes a canonical tool start;
- function argument deltas become canonical tool starts/deltas;
- `response.function_call_arguments.done` becomes a tool end;
- ordinary `response.output_item.done(function_call)` is not currently decoded into the ordinary tool lifecycle;
- canonical tool end currently emits no Responses frame;
- reasoning deltas lose required index/item metadata;
- successful terminal encoding can use an empty response ID when no ID survived the canonical path.

This is sufficient for lightweight text compatibility but not for current Codex tool/history semantics.

---

# Architectural decision

Do not turn `CanonicalRequest`, `CanonicalMessage`, or `CanonicalEvent` into full copies of the Responses protocol.

Use three narrower concepts instead:

1. **source-native request preservation** at admission/runtime, separate from canonical routing semantics;
2. **native stream forwarding with observation** when client and upstream surfaces are both Responses;
3. **stateful Responses synthesis** only when Eggpool actually translates from another upstream surface into a Responses client.

This keeps the canonical IR provider-neutral and lets future Responses additions pass through Eggpool without requiring an immediate IR release.

The one place canonical semantics should be expanded is where a concept is genuinely cross-provider and needed for translation, such as distinguishing function-style JSON tools from freeform/custom tools. Even there, prefer a small tool-kind enum over OpenAI event-specific fields such as `sequence_number` or `output_index`.

---

# Workstream ordering

## Phase A — Native request fidelity

Implement Plan 193 first.

The result must allow a valid current Codex Responses body to reach a Responses upstream with only the fields Eggpool intentionally owns changed. Model alias rewriting must no longer force full canonical reconstruction. Unknown-but-bounded Responses input/tool fields must not be rejected merely because the native upstream can already understand them.

Do not relax safety/resource limits or stateful-feature rejection.

## Phase B — Native stream fidelity and translated stream completion

Implement Plan 194 next.

Native Responses streams must be forwarded without event reconstruction while still being observed for terminal status, usage, malformed stream handling, and coordinator finalization.

Translated streams must use a stateful downstream Responses encoder and emit complete output items, including function calls.

## Phase C — Codex tool/replay compatibility and conformance

Implement Plan 195 last.

Close custom/freeform-tool translation where needed, add exact Codex/OpenCodex-derived fixtures, document the custom-provider configuration, and define the model-discovery boundary.

---

# Non-goals

Do not use this work to:

- fork or patch Codex;
- require OpenCodex as a runtime dependency;
- emulate OpenAI server-side response storage;
- add WebSocket Responses support solely for Codex;
- replace Eggpool's provider-neutral wire registry;
- make OpenAI Responses objects the universal internal representation;
- create a Codex-specific provider kind or Codex-only routing policy;
- change standard `/v1/models` into Codex's richer private catalog schema;
- silently discard untranslatable tool/history items on cross-surface routes;
- weaken bounded request/SSE parsing;
- accumulate an unbounded response transcript in memory;
- add a second routing/retry pipeline for Codex traffic.

---

# Acceptance criteria

This roadmap is complete only when all of the following are true:

1. Current unmodified Codex can make a first text turn through Eggpool using an explicit model alias.
2. A second turn containing prior Responses output/history is admitted and routed correctly.
3. A function tool call is received by Codex as a complete `response.output_item.done` function-call item with stable `call_id`, complete arguments, and valid status before `response.completed`.
4. The resulting `function_call_output` can be sent back through Eggpool and the model can continue the turn.
5. At least one parallel-function-call fixture works.
6. Native Responses->Responses routing preserves current Codex replay items and request controls even when Eggpool rewrites the model alias.
7. Native Responses->Responses streaming does not normalize away unknown/forward-compatible SSE events.
8. Translated Chat/Anthropic/Gemini->Responses streaming generates a valid Responses lifecycle rather than relying on native passthrough.
9. Reasoning summary events include the metadata current Codex requires when Eggpool synthesizes them; native encrypted reasoning survives native pass-through.
10. Custom/freeform tools either work through the supported translation path or fail before upstream dispatch with a precise unsupported-feature classification; no request is silently changed from freeform semantics to an incompatible JSON function.
11. EOF before a valid Responses terminal event remains a failed stream.
12. `response.completed` emitted by Eggpool has a non-empty response ID.
13. Explicit model configuration is documented and tested; lack of automatic Codex model-picker discovery does not block the closure.
14. Standard Eggpool `/v1/models` behavior remains OpenAI-compatible for existing consumers.
15. Existing Chat Completions, Anthropic Messages, Gemini Interactions/generateContent, routing, retry, accounting, and stream-finalization suites remain green.

---

# Validation commands

Implementation should use the repository's current formatting/lint/test commands, plus focused coverage comparable to:

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml wire
cargo test --manifest-path rust/Cargo.toml request
cargo test --manifest-path rust/Cargo.toml coordinator::streaming
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat
```

Use the exact test target names created by the implementation. Do not add a sprawling provider matrix when deterministic codec/coordinator fixtures prove the contract.

For live qualification, use one local Eggpool instance plus current Codex configured as a custom Responses provider. Exercise text, tool use, tool-result continuation, and at least one second-turn replay. Record the Codex commit/version used in the closure evidence.

---

# Handoff notes

Read Plans 193–195 before editing code. They intentionally separate native fidelity from translation so an implementer does not "fix" Codex by adding OpenAI-specific fields to every canonical type.

If current Codex has moved materially beyond `508a006d7aaa485ac0367c9e45c69ebb948af518` when implementation starts, re-run the narrow source audit against:

- its Responses request builder;
- `codex-api` Responses SSE parser;
- `ResponseItem` history variants;
- custom-provider/model-provider configuration;
- model catalog manager.

Update fixtures only for observed protocol changes. Keep the architectural boundary unchanged unless Codex now requires a stateful server feature that conflicts with Eggpool's stateless contract.