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
reference — see `eggpool configsetup codex --print-secret`.
