# Stateless Responses Support

`POST /v1/responses` is a **stateless same-protocol passthrough** to providers that explicitly declare a Responses endpoint. It is not a general Responses API implementation and does not claim OpenAI Responses parity. The surface exists so current Codex (which only speaks the Responses wire API) can route through EggPool.

## Eligibility

A provider participates in `/v1/responses` when its
`wire_surfaces.openai_responses` candidate is configured. The legacy
`responses_path` field remains supported and is synthesized into that
candidate when `wire_surfaces` is absent. Bundled templates ship with
`responses_path = "/responses"` for openai, ollama-local, llamacpp-local, and
vllm-local; other providers can opt in explicitly. Chat Completions eligibility
is unchanged.

## Constraints

- **Stateless only.** Requests carrying `previous_response_id`, any `conversation` reference (including empty `{}` or string IDs), `store = true`, or `background = true` are rejected locally with HTTP 400 before any provider selection or upstream I/O. Omitted `store` is also rejected — clients must send `store: false` explicitly so EggPool does not silently rely on a provider's default retention behaviour.
- **No translation.** No Responses ↔ Anthropic translation, no Responses ↔ Chat Completions rewrite, no content IR, and no provider-specific Responses plugin. The accepted body is forwarded unchanged apart from the canonical `model` provider-suffix/base-ID normalization; Anthropic thinking-budget rewrites and Chat `stream_options.include_usage` injection are skipped.
- **No state persistence.** EggPool does not store `response.id`, `previous_response_id`, or conversation history; there is no `/v1/responses/{id}` retrieval, no cancellation, no delete, and no background-job endpoint.

## Protocol Rejection

A provider whose only documented protocol is Anthropic is rejected locally with HTTP 400 — the Responses surface cannot route through `/messages`.

## Terminal Stream Semantics

`response.completed` is the only successful canonical Responses terminal event. `response.failed` and `response.incomplete` are terminal non-success outcomes: the upstream event is forwarded unchanged to the client, the request is durably finalized as non-success, and no provider/account failover is attempted after downstream handoff.

## Provider Routing

Bundled provider templates that do not advertise a Responses path are not eligible for `/v1/responses` traffic; the request is routed only among providers that explicitly declared one.

## Codex Integration

The Codex integration renderer emits a current `[model_providers.eggpool]` block with `wire_api = "responses"` and an `env_key = "EGGPOOL_API_KEY"` reference — see `eggpool configsetup codex --print-secret`.
