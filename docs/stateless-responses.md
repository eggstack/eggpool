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
selection or upstream I/O. Omitted `store` is also rejected: clients must send
`store: false`. EggPool does not persist response IDs, conversation history,
retrieval state, cancellation state, or background jobs.

The canonical IR is built from the original client request. An alternate wire
codec always encodes from that source request; it never chains a previously
translated provider payload. Surface-specific credentials are rendered at
dispatch time and are not stored in the profile or metadata.

## Streaming

The stream adapter translates native upstream events into Responses events for
the client. `response.completed` is the only successful Responses terminal;
`response.failed` and `response.incomplete` are terminal non-success outcomes.
Gemini Interactions uses `interaction.completed`, while Gemini
`generateContent` uses a candidate `finishReason`. A transport EOF without
native terminal evidence is classified as incomplete and never receives a
synthetic client terminal event.

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
