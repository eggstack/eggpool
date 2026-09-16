# Stateless Responses Support

`POST /v1/responses` is EggPool's stateless OpenAI Responses client surface.
It emits and accepts the Responses grammar at the client boundary while the
selected provider may use OpenAI Chat Completions, Anthropic Messages, native
Gemini Interactions, Gemini `generateContent`, or OpenAI Responses.

## Eligibility and constraints

A provider participates when it declares a compatible `wire_surfaces` profile.
The legacy `responses_path` field remains supported and is synthesized into an
`openai_responses` profile when `wire_surfaces` is absent. The provider/model
resolver still decides eligibility; surface priorities and bundled hints are
only preferences.

Responses requests remain stateless. Requests carrying
`previous_response_id`, any `conversation` reference, `store = true`, or
`background = true` are rejected locally with HTTP 400 before provider
selection or upstream I/O. Omitted `store` is treated as stateless, as is
`store: false`. EggPool does not persist response IDs, conversation history,
retrieval state, cancellation state, or background jobs.

Responses admission produces both a bounded canonical semantic projection and
a source-native preservation envelope. Native Responses-to-Responses routing
forwards the original validated JSON, or rewrites only the top-level `model`
when an EggPool alias selects a different upstream model. This preserves input
item order, reasoning replay fields, native tools, and current/future
extension fields without putting arbitrary Responses JSON in the canonical IR.
Cross-surface routing still encodes from canonical semantics; native-only
items/tools fail with `UnsupportedSemanticFeature`, while safely omittable
top-level extensions produce explicit adaptation notices. Surface-specific
credentials are rendered at dispatch time and are not stored in the profile or
metadata.

Ordinary function tools are portable across the built-in function-tool
surfaces. Responses `custom` tools are represented canonically as bounded
freeform tools and, for a function-only upstream, use a deterministic
`{"input":"..."}` wrapper. The wrapper is removed before the downstream
Responses `custom_tool_call` item is emitted; malformed wrappers fail closed.
Other native/server tools remain native-only and are preserved on a native
Responses route or rejected before dispatch when no semantic equivalent exists.

## Streaming

Responses streaming has two modes. When both client and upstream surfaces are
Responses, EggPool incrementally observes the SSE framing and terminal/usage
evidence while forwarding the original valid bytes unchanged, including
unknown forward-compatible events. When the upstream surface differs, a
stateful bounded encoder synthesizes Responses message, reasoning, and
function-call lifecycles. Function calls end with an authoritative
`response.output_item.done` containing complete arguments, a separate output
item ID, the canonical `call_id`, name, and status. `response.completed` is the
only successful Responses terminal; `response.failed` and
`response.incomplete` are terminal non-success outcomes. A transport EOF
without native terminal evidence is classified as incomplete and never
receives a synthetic client terminal event.

## Configuration

Declare concrete provider candidates when paths or auth shapes differ:

```toml
[providers.gemini-native.wire_surfaces.gemini_generate_content]
path_template = "/models/{model}:generateContent"
stream_path_template = "/models/{model}:streamGenerateContent"
priority = 100
```

The built-in registry owns five closed surface IDs:
`openai_chat_completions`, `openai_responses`, `anthropic_messages`,
`gemini_interactions`, and `gemini_generate_content`. See
[Provider Catalog](providers.md) for bundled templates and
[Protocol Transcoding](transcoding.md) for the canonical boundary.

## Codex integration

The Codex integration renderer emits a current `[model_providers.eggpool]`
block with `wire_api = "responses"` and an `env_key = "EGGPOOL_API_KEY"`
reference, with WebSockets disabled — see
`eggpool configsetup codex --model <model>`. Codex model discovery is not
provided by `/v1/models`; configure an explicit model or alias.

## Remote compaction compatibility

Compaction is a stateless operation and does not imply stored Responses.
`POST /v1/responses/compact` accepts the history/checkpoint input required
for that operation and returns replacement material in the same
request/response transaction. EggPool persists no conversations, response
IDs, or compacted history for future continuation; `previous_response_id`,
stored conversations, and background Responses remain outside the stateless
contract.

The endpoint is a distinct model-facing operation, not an ordinary assistant
completion: it participates in the normal provider selection, failure
isolation, health effects, quota/accounting, bounded-body, retry-budget, and
cancellation ownership, and compact failures are distinguishable in safe
diagnostics without storing prompts or replacement history. Only native
same-surface forwarding is supported today — the source-native compact JSON
is preserved exactly except for the EggPool-owned model rewrite — and
targets without explicit `supports_remote_compaction_v1` capability plus a
`compact_path_template` fail before upstream submission. There is
deliberately no translated compaction fallback: correct rejection is
preferable to a lossy compact result that breaks a long-running agent later.

Current v2 `compaction_trigger` items over the normal Responses endpoint
require explicit native `supports_remote_compaction_v2` capability and are
otherwise rejected with `UnsupportedSemanticFeature`; a trigger is never
treated as user text or silently converted through a codec that cannot
represent it. EggPool does not advertise v2 remote compaction until the
complete request/result path is qualified end to end, and the generated
Codex provider configuration continues to use Codex's local compaction path
by default.
