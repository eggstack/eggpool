# Plan 152 — Default Wire-Surface Codecs and Streaming Semantics

Date: 2026-09-02
Status: ready after Plans 148–151
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Depends on: Plans 148–151
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f`
Priority: P0 API correctness / interoperability
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Implement the concrete request/response/stream adapters for the default wire surfaces used by the dynamic resolver, then migrate bundled provider contracts/hints to those profiles.

The default codec set is intentionally limited to five broadly useful model-generation surfaces:

```text
openai_chat_completions
openai_responses
anthropic_messages
gemini_interactions
gemini_generate_content
```

This phase must make Responses, Messages and Chat genuinely distinct wire grammars while allowing a common canonical request/response/event representation to bridge them. It must also establish native Gemini support without introducing Google SDK dependencies or stateful session routing.

---

# Scope decision and current research

Verification date: 2026-09-02. Re-check official upstream documentation immediately before implementation.

## Included

### OpenAI Chat Completions

Common compatibility endpoint across many hosted/local providers. Typical endpoint:

```text
POST /v1/chat/completions
```

Streaming is data-only SSE chunks with Chat Completions response/chunk shapes; `[DONE]` is commonly used but real compatibility providers may have documented terminal quirks. EggPool must use the codec/observer's semantic terminal evidence rather than assume every OpenAI-compatible provider is byte-identical.

Reference: <https://platform.openai.com/docs/api-reference/chat>

### OpenAI Responses

Distinct endpoint and schema:

```text
POST /v1/responses
```

Streaming uses typed `response.*` events. Current terminal states include successful completion and explicit failed/incomplete conditions. Responses reasoning is represented through Responses-specific fields such as the `reasoning` object rather than Anthropic token budgets.

Reference: <https://platform.openai.com/docs/api-reference/responses>

### Anthropic Messages

Direct endpoint:

```text
POST /v1/messages
```

Streaming uses named events such as `message_start`, content block events, `message_delta`, and terminal `message_stop`; errors are explicit events.

References:

- <https://platform.claude.com/docs/en/api/messages>
- <https://platform.claude.com/docs/en/build-with-claude/streaming>

### Gemini Interactions

Current Google-recommended, GA API for new Gemini model/agent work. Model interactions use:

```text
POST /v1/interactions
```

or the currently documented versioned equivalent for the selected base URL.

Streaming uses SSE when `stream=true`. Current event sequence includes:

```text
interaction.created
step.start
step.delta
step.stop
interaction.completed
```

with typed steps for text, thought and function calls. The API stores interactions by default; EggPool's generic failover-safe codec must explicitly use stateless behavior (`store=false`) and must not use `previous_interaction_id` or background execution.

References:

- <https://ai.google.dev/gemini-api/docs/interactions-overview>
- <https://ai.google.dev/api/interactions-api-v1>
- <https://ai.google.dev/gemini-api/docs/streaming>

### Gemini generateContent

Google's previous core API is now described as legacy but remains fully supported and widely deployed:

```text
POST /v1beta/models/{model}:generateContent
POST /v1beta/models/{model}:streamGenerateContent
```

Streaming uses the dedicated streaming method and SSE/query behavior documented by Google.

Reference: <https://ai.google.dev/api/generate-content>

---

# Explicitly excluded from default codecs

## OpenAI legacy Completions

Do not add `/v1/completions` as an alternate for arbitrary chat/tool requests. Prompt-only semantics require model/template-specific conversion and can silently change behavior.

## Realtime / Live / WebSocket

Do not add OpenAI Realtime, Gemini Live/BidiGenerateContent or other full-duplex stateful transports. They require session affinity, connection ownership and fundamentally different retry semantics.

## Cohere native v2 Chat

Do not add a sixth codec merely for Cohere. Cohere currently documents an official OpenAI SDK compatibility API at:

```text
https://api.cohere.ai/compatibility/v1
```

with Chat Completions, streaming, tools and structured output.

Reference: <https://docs.cohere.com/docs/compatibility-api>

## Bedrock Converse/Invoke

Do not add AWS-native signing/Converse semantics in this roadmap. Current Bedrock runtime exposes OpenAI Chat, OpenAI Responses and Anthropic Messages in addition to Converse/Invoke, so the default three major codecs already cover common Bedrock compatibility endpoints.

References:

- <https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html>
- <https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html>

## Ollama/vLLM native APIs

Use their common compatibility surfaces where available. Do not add local-runtime-specific generation codecs without a concrete feature gap.

---

# Codec interface

Each built-in codec should provide the minimum operations required by the coordinator/Plan 149 boundary:

```text
encode_request(canonical_request, selected capability/profile)
decode_response(upstream JSON)
decode_stream_frame(SSE/frame)
encode_response(canonical response/events, client surface)
terminal/acceptance policy
usage extraction hook or normalized output
```

The exact interface should reuse existing transcoder abstractions where practical. Avoid an inheritance-heavy framework.

The profile registry references codec IDs; Python owns the implementation and validates the mapping.

---

# Surface A — OpenAI Chat Completions

## Request encoding

Map canonical semantics to the current Chat shape:

- `model`;
- chronological `messages`;
- system/developer roles according to selected model/provider capability;
- multimodal `content` blocks where supported;
- `tools` / `tool_choice`;
- `temperature`, `top_p`, stop controls;
- `max_tokens` / `max_completion_tokens` according to current selected provider/model contract;
- structured response format where representable;
- provider-supported reasoning controls only through selected capability adaptation;
- `stream` and Chat-specific `stream_options` only where current provider contract requires/supports them.

Do not inject Chat-only fields into Responses or other surfaces.

## Response/stream

Reuse current Chat usage and tool-call handling where correct.

Terminal observer must tolerate current known compatibility-provider differences only through explicit policy, not by declaring any EOF successful. Existing OpenCode/Luna regressions around absent expected terminal markers should be represented as provider/codec evidence rather than causing client hangs.

If a provider returns a valid final finish reason but omits `[DONE]`, define the minimum accepted semantic terminal evidence based on current provider behavior and existing EggPool safety requirements. Do not globally weaken all streams merely to accommodate one provider.

---

# Surface B — OpenAI Responses

## Request encoding

Implement a real Responses encoder from canonical request intent rather than converting through Chat.

Map at minimum:

- model;
- stateless `input` / instructions;
- tools/function definitions;
- tool call/results representable by current Responses item semantics;
- text/structured output configuration;
- max output token control;
- reasoning intent using current Responses `reasoning` semantics where supported;
- stream boolean;
- `store=false` when required to enforce EggPool's stateless portable path.

Reject locally rather than silently strip unsupported stateful controls such as:

- `previous_response_id` requiring provider state;
- background jobs;
- conversation identifiers requiring affinity;
- retrieval/cancel semantics.

Same-surface existing Responses passthrough may remain available for the supported stateless subset.

## Streaming

Decode/observe typed Responses events directly.

At implementation time verify current terminal event set. Expected categories include:

```text
response.created
response.output_* / content/tool/reasoning delta events
response.completed
response.failed
response.incomplete
error
```

Map successful/failed/incomplete conditions to canonical terminal events.

Do not convert Responses SSE into Chat chunks merely to reuse `[DONE]` parsing.

Usage should be extracted from the complete terminal response object/event through the existing normalized accounting path.

---

# Surface C — Anthropic Messages

## Request encoding

Reuse current body translation helpers where they are semantically correct, but make the Messages codec own the final wire form:

- top-level system content;
- `messages` roles/content blocks;
- `tools` / tool choice;
- `max_tokens`;
- supported sampling/stop controls;
- image/document blocks under existing capability rules;
- reasoning/thinking representation based on selected current Anthropic-compatible contract.

Do not assume every Anthropic-compatible model uses manual `thinking: {type: enabled, budget_tokens: ...}`. Current Anthropic APIs include model-dependent adaptive/manual semantics; selected provider/model capabilities determine encoding.

## Streaming

Map named events to canonical events:

```text
message_start
content_block_start
content_block_delta
content_block_stop
message_delta
message_stop
error
```

Preserve current tool-call ID mapping and incremental JSON argument handling.

`message_stop`/explicit error remains the terminal signal; clean transport EOF without terminal evidence is not automatically success.

---

# Surface D — Gemini Interactions

## Provider/auth/path support

Support a provider with base URL configured so the profile path is `/interactions` or the current version-relative equivalent.

Direct Gemini API auth should support current `x-goog-api-key` rendering through Plan 148 surface/provider auth configuration. Do not add `google-genai` SDK.

## Stateless model subset

Encode ordinary canonical model turns only.

Required outbound behavior:

```text
model = selected model
input = canonical content/steps
system_instruction = canonical system instruction where present
tools = portable function declarations
stream = client/route stream mode
store = false
```

Generation configuration should carry only portable controls known to the selected model capability.

Do not route through generic negotiation:

- managed Google agents;
- `previous_interaction_id`;
- `background=true`;
- server-side conversation continuation;
- provider-specific asynchronous retrieval/cancel flows.

## Reasoning

Gemini Interactions can expose thought/reasoning as typed steps. Map client reasoning intent using the current selected model's documented generation/thinking configuration. Do not guess a Gemini thinking budget from OpenAI effort labels unless the capability registry has an explicit mapping.

Thought output may map to canonical reasoning events where policy permits exposing it. Do not fabricate reasoning visibility when the upstream omits/withholds it.

## Streaming

Current Google documentation describes typed SSE events:

```text
interaction.created
step.start
step.delta
step.stop
interaction.completed
```

and statuses such as `requires_action`, `completed`, `failed`, `cancelled`, and `incomplete`.

Map:

- model-output text step deltas -> canonical text deltas;
- thought step deltas -> reasoning deltas when exposed;
- function-call steps/argument deltas -> canonical tool-call events;
- terminal completed -> success;
- requires-action with portable function calls -> canonical tool-use terminal for the turn;
- failed/cancelled/incomplete -> explicit non-success terminal according to current semantics.

Re-check current event names immediately before coding; Google changed Interaction streaming event names during 2026, which is precisely why this knowledge belongs in one codec rather than router conditionals.

---

# Surface E — Gemini generateContent

## Paths

Use profile templates:

```text
/models/{model}:generateContent
/models/{model}:streamGenerateContent
```

Render only a validated canonical model ID. Never interpolate arbitrary request path text.

Use current documented streaming query/Accept behavior (`alt=sse` or current equivalent) inside the codec/profile implementation.

## Request mapping

Map the portable subset to Gemini-native fields:

- `contents` with user/model roles;
- `systemInstruction` / current casing per API version;
- function declarations/tools;
- generation config;
- structured output where supported;
- multimodal parts where current capability rules allow;
- current thinking config only from explicit selected capability mapping.

Do not support provider-specific Google Search/code execution as portable cross-surface tools in this plan.

## Response/stream

Decode candidates/content parts/function calls/usage into the canonical response/event representation.

Treat provider finish reasons and safety termination as explicit canonical stop/error outcomes; do not collapse all non-empty candidates to success.

Streaming uses the documented generateContent stream framing. Keep parsing incremental and bounded.

---

# Client-surface encoding

EggPool's existing public surfaces remain the primary exposed APIs:

```text
/v1/chat/completions
/v1/responses
/v1/messages
```

This plan does **not** require exposing Gemini-compatible public endpoints. Gemini is initially an upstream wire option reachable through the canonical translation boundary.

For each client surface:

- Chat clients receive real Chat response/chunk grammar;
- Responses clients receive real Responses object/SSE grammar;
- Anthropic clients receive real Messages object/SSE grammar.

Do not make a Responses client receive Chat chunks because the selected upstream happened to be Chat.

A future public Gemini endpoint can reuse the codecs but requires a separate product/API plan.

---

# OpenCode Go migration and regression correction

Update the bundled provider contract using Plan 148 data structures.

Current documented candidate set:

```text
openai_responses
openai_chat_completions
anthropic_messages
```

Add current low-authority hints for the models used by live acceptance tests, including at minimum:

- Muse Spark 1.2 Contributor -> `openai_responses`;
- GPT-5.6 Luna -> `openai_responses`;
- MiniMax M3 -> `anthropic_messages` if current docs still confirm it;
- one current MiMo/GLM model -> `openai_chat_completions`.

Remove/replace the current Muse built-in assumption that its native transport is Anthropic Messages.

Reasoning capability metadata may still say which effort labels the model accepts, but it must not imply Anthropic transport merely because thinking is supported.

### Current false-live test

`tests/integration/test_muse_spark_live_e2e.py` currently uses `respx`, fabricates an Anthropic `/messages` response, and hard-codes Muse as Anthropic. Rename/reclassify it as deterministic integration coverage or replace its assumptions. A test using a mocked upstream is not a live provider acceptance test.

Plan 153 adds the actual live suite.

---

# Provider-template updates

After codec implementation, re-check bundled templates and declare candidate profiles only where current official/provider docs support them.

Representative disposition:

- OpenAI: Chat + Responses;
- Anthropic: Messages;
- OpenCode Go: Chat + Responses + Messages;
- OpenRouter: declare only surfaces verified current and supported by its endpoint contract; same model may legitimately work on multiple surfaces;
- Ollama: Chat + Responses where current version supports both;
- llama.cpp: Chat + Responses where current server supports both;
- vLLM: Chat + Responses and optionally Messages only if current version/profile semantics are verified and worthwhile;
- Cohere: prefer its OpenAI compatibility template if added, not native v2 codec;
- Gemini direct: Interactions + generateContent if catalog/auth/connect setup is implemented.

Do not advertise a candidate merely because the path name is conventional. Verify current provider behavior/docs.

---

# Surface acceptance vs model-generation success

Plan 150 needs a point where a negotiation leader can release followers once an alternate profile is known to be structurally accepted.

For normal HTTP APIs, an upstream HTTP success status/accepted streaming response is enough to mark the surface usable; do not hold the negotiation single-flight lock until the generated body completes.

However:

- record long-term `last_success` only according to one clearly tested rule;
- if the stream immediately emits a protocol-level failure before useful output, do not claim semantic success merely because HTTP status was 200;
- surface acceptance and account/model generation health remain separate observations.

Recommended distinction:

```text
profile accepted -> release negotiation followers / tentative preference
request completed successfully -> refresh strong learned success
```

A later failure should not trigger another surface attempt after response handoff.

---

# Streaming liveness

Some reasoning-capable upstreams can spend substantial time without visible text/reasoning deltas. Do not treat absence of visible reasoning events alone as proof of a stall.

The proxy must distinguish:

- no upstream bytes / transport timeout;
- valid upstream keepalive/comment events;
- valid but silent reasoning interval;
- malformed stream;
- missing terminal event;
- completed stream.

Do not manufacture fake reasoning deltas solely to reassure clients.

If a client/harness requires a particular terminal grammar, the client-surface encoder must provide it based on canonical terminal evidence.

---

# Required codec tests

Use compact representative fixtures from current official API shapes.

For every implemented surface:

1. minimal text non-stream request/response;
2. minimal text stream with recognized terminal;
3. tool declaration + call + result round trip where portable;
4. max-output control mapping;
5. reasoning disabled/requested representative mapping;
6. usage extraction;
7. explicit provider error event/status;
8. malformed/premature EOF does not become successful completion.

Cross-surface high-value matrix only:

```text
Chat client -> Responses upstream
Chat client -> Messages upstream
Chat client -> Gemini Interactions upstream
Responses client -> Chat upstream
Responses client -> Messages upstream
Anthropic client -> Responses upstream
Anthropic client -> Chat upstream
```

Add Gemini generateContent cross-surface tests sufficient to prove the codec, but do not exhaustively test all 5×3 combinations if canonical codec unit tests already prove composition.

Use property/invariant tests where cheap:

- original tool call IDs remain associated;
- explicit reasoning disable never becomes enable;
- usage counts are non-negative and preserved when source reports them;
- terminal error cannot encode as successful client completion.

---

# Acceptance criteria

- [ ] Five default surface codec IDs are implemented and registered.
- [ ] OpenAI Responses uses its own request and typed SSE semantics, not Chat emulation internally.
- [ ] Anthropic Messages retains correct named SSE semantics.
- [ ] Gemini Interactions supports the stateless model subset with `store=false` and no provider SDK dependency.
- [ ] Gemini generateContent supports both unary and streaming path templates.
- [ ] Gemini Interactions/generateContent can be selected as upstreams without adding public Gemini endpoints.
- [ ] Same-surface passthrough remains available where no adaptation is needed.
- [ ] Cross-surface client output always uses the client's real wire grammar.
- [ ] Reasoning controls are encoded from canonical intent and selected model capability, not from surface guesses.
- [ ] Stateful Responses/Interactions features remain rejected/outside generic failover.
- [ ] OpenCode Go candidate mapping is data-driven and Muse currently prefers Responses when verified.
- [ ] OpenCode Go Muse is no longer encoded as Anthropic merely because it exposes reasoning tiers.
- [ ] Surface-specific auth/header rendering replaces the current need to send multiple auth schemes indiscriminately where provider config supports it.
- [ ] The mocked Muse test no longer claims to be live and no longer bakes the wrong `/messages` contract.
- [ ] No Cohere/Bedrock/Ollama/vLLM native codec is added without necessity.
- [ ] No SDK/runtime dependency is added for OpenAI/Anthropic/Google codec support.

---

# Rejection conditions

Reject implementation if it:

- treats Responses as Chat Completions with a different URL;
- maps Gemini Interactions stateful IDs through ordinary failover;
- exposes managed Google agents through the generic canonical model path;
- guesses reasoning budget equivalence among OpenAI/Anthropic/Gemini;
- adds every known vendor API as a default codec;
- uses provider SDKs instead of the existing HTTP stack;
- adds provider-name conditionals to the coordinator for Muse/OpenCode/Gemini;
- weakens all stream terminal validation to accommodate one quirky provider;
- buffers full SSE streams;
- makes the default CI contact live providers.

---

# Verification

Before implementation closure, record the exact official documentation versions/URLs checked for each of the five surfaces and the provider mappings updated in `_templates.toml` / `_wire_profiles.toml`.

Run focused codec/transcoder/stream/provider-template tests plus the ordinary lean gate. Live behavior is verified separately under Plan 153.

---

# Handoff

1. Re-check current official APIs and exact stream terminal event names.
2. Implement/finish Chat, Responses and Messages codecs first and migrate existing pairwise wrappers.
3. Add Gemini Interactions stateless codec.
4. Add Gemini generateContent codec/path templates.
5. Migrate bundled provider candidates/hints, especially OpenCode Go.
6. Reclassify/remove the mocked "live" Muse assumption.
7. Run focused composition/stream tests and normal gate.
8. Record implementation SHA, verified docs and any intentionally unsupported portable fields here.
