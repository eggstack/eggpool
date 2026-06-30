# Phase 6 — Deferred features

## Goal

Extend the text-only transcoder to cover the remaining protocol
features that v1 deliberately deferred: tool calling, vision / image
input, extended thinking / reasoning, structured outputs, and
Anthropic-specific primitives like `pause_turn` and `cache_control`. Each
sub-phase is gated behind a small `[transcoder.features]` config block
so operators can opt into one without enabling the others.

After this phase, the transcoder covers the full request/response
surface that today's OpenAI and Anthropic clients actually use. Tool
calling in particular is what unlocks agent workflows on Anthropic-only
upstreams from OpenCode / Aider / Cursor.

## Scope

Five sub-phases, ordered by demand. Each is independently shippable.
Operators can enable any combination.

| # | Sub-phase | Plan section | Status |
|---|---|---|---|
| 6.1 | Tool use / function calling | [§ 1](#1-tool-use--function-calling) | ✅ Implemented 2026-06-30 (`plans/tooltranscoding.md`) |
| 6.2 | Vision / image input | [§ 2](#2-vision--image-input) | Pending |
| 6.3 | Extended thinking / reasoning | [§ 3](#3-extended-thinking--reasoning) | Pending |
| 6.4 | Structured outputs | [§ 4](#4-structured-outputs) | Pending |
| 6.5 | Anthropic primitives | [§ 5](#5-anthropic-primitives) | Partial — basic `pause_turn` → `__eggpool_pause_turn__` sentinel handling and `cache_control_dropped` warning shipped with 6.1; structured `pause_turn` surface remains pending |

Each sub-phase is independently reviewed and merged. They share a
scaffolding: each adds a feature flag under `[transcoder.features]` and
each is **off** by default.

## Configuration

```toml
[transcoder]
enabled = true
loss_policy = "warn"
prefer_native = true

[transcoder.features]
# 6.1 — bidirectional tool calling translation
tools = false
# 6.2 — image / document content parts (vision models)
vision = false
# 6.3 — extended thinking blocks ↔ reasoning_content
thinking = false
# 6.4 — OpenAI response_format / json_schema coercion
structured_outputs = false
# 6.5 — Anthropic-only primitives
anthropic_primitives = false
```

The `features` block is a Pydantic model with all five fields defaulting
to `false`. Adding a sixth feature is a single field addition.

The coordinator checks the relevant flag before invoking each translator
hook. When a feature is off, the v1 behaviour (drop with warning)
prevails for any input that exercises that feature. Operators who enable
transcoder but not `tools` see exactly today's behaviour for tool
requests, including the warnings.

## Shared infrastructure

### Per-request id map

`ids.py` from phase 1 gains a `ToolCallIdMap` populated lazily by the
tool-use translators (6.1):

```python
class ToolCallIdMap:
    """Bidirectional id translation between OpenAI call_xxx and
    Anthropic toolu_xxx ids, scoped to one request."""

    def __init__(self) -> None:
        self._openai_to_anthropic: dict[str, str] = {}
        self._anthropic_to_openai: dict[str, str] = {}

    def openai_to_anthropic(self, openai_id: str) -> str:
        if openai_id not in self._openai_to_anthropic:
            self._openai_to_anthropic[openai_id] = (
                f"toolu_{uuid.uuid4().hex[:24]}"
            )
        return self._openai_to_anthropic[openai_id]

    def anthropic_to_openai(self, anthropic_id: str) -> str:
        if anthropic_id not in self._anthropic_to_openai:
            self._anthropic_to_openai[anthropic_id] = (
                f"call_{uuid.uuid4().hex[:24]}"
            )
        return self._anthropic_to_openai[anthropic_id]
```

The map is owned by `TranscodeContext` (added in phase 1):

```python
@dataclass(slots=True)
class TranscodeContext:
    # ... existing fields ...
    id_map: ToolCallIdMap = field(default_factory=ToolCallIdMap)
```

### Loss-warning kinds catalogue

Each sub-phase uses a stable `kind` string for `loss_warnings.append`.
The catalogue lives in `transcoder/__init__.py` so dashboards and
tests can reference it without string typos:

```python
LOSS_WARNING_KINDS = frozenset({
    # Phase 2 (text-only)
    "dropped_field", "value_clamped",
    # 6.1 (tools) — implemented 2026-06-30
    "tool_call_id_translated", "parallel_tool_calls_collapsed",
    "tool_result_image_dropped", "malformed_tool_arguments",
    "invalid_tool_choice", "unsupported_tool_type",
    "empty_tool_use_block", "tool_call_id_changed",
    "tool_result_error_passthrough", "cache_control_dropped",
    "pause_turn", "non_text_content_dropped", "tool_result_inferred",
    # 6.2 (vision) — pending
    "image_unsupported_format", "image_url_to_base64_fallback",
    "image_too_large",
    # 6.3 (thinking) — pending
    "thinking_signature_dropped", "reasoning_content_dropped",
    # 6.4 (structured outputs) — pending
    "response_format_to_system_prompt",
    # 6.5 (anthropic primitives) — partial
    "pause_turn_surface", "top_k_dropped",
})
```

A unit test asserts the catalogue stays in sync with the actual warning
strings emitted by the translators (regex / fixture check).

---

## 1. Tool use / function calling

### Translation rules

**OpenAI → Anthropic**

| OpenAI input | Anthropic output |
|---|---|
| `tools[i].type == "function"`, `function.name`, `function.description`, `function.parameters`, `function.strict` | `tools[i].name`, `tools[i].description`, `tools[i].input_schema` (verbatim JSON Schema), drop `strict` |
| `tool_choice: "none"` | `tool_choice: {type: "none"}` |
| `tool_choice: "auto"` | `tool_choice: {type: "auto"}` |
| `tool_choice: "required"` | `tool_choice: {type: "any"}` |
| `tool_choice: {"type": "function", "function": {"name": "..."}}` | `tool_choice: {type: "tool", name: "..."}` |
| `parallel_tool_calls: false` | dropped with warning; Anthropic has no parallel-disable knob |
| `messages[i].role == "assistant"`, `tool_calls[i].function.arguments` (string) | `messages[i].role == "assistant"`, `content[i].type == "tool_use"`, `input` parsed from `arguments` JSON |
| `messages[i].role == "assistant"`, `tool_calls[i].id` (call_xxx) | `messages[i].content[i].type == "tool_use"`, `id` (toolu_xxx) via id_map |
| `messages[i].role == "tool"`, `content` (string), `tool_call_id` (call_xxx) | `messages[i].role == "user"`, `content[i].type == "tool_result"`, `tool_use_id` (toolu_xxx) via id_map, `content: text` if string |
| `messages[i].role == "tool"`, `content` (parts array) | `messages[i].content[i].type == "tool_result"`, `content` mapped part-by-part; image parts allowed |

**Anthropic → OpenAI**

| Anthropic input | OpenAI output |
|---|---|
| `tools[i].name`, `tools[i].description`, `tools[i].input_schema` | `tools[i].type == "function"`, `function.{name,description,parameters(input_schema)}` |
| `tools[i].cache_control` | dropped with warning (cache_control is 6.5) |
| `tool_choice: {type: "auto"}` | `"auto"` |
| `tool_choice: {type: "any"}` | `"required"` |
| `tool_choice: {type: "tool", name}` | `{"type": "function", "function": {"name"}}` |
| `tool_choice: {type: "none"}` | `"none"` |
| `messages[i].content[j].type == "tool_use"`, `id`, `name`, `input` | `messages[i].role == "assistant"`, `tool_calls[k]` with `id` (call_xxx), `function.name`, `function.arguments` (JSON-stringified `input`) |
| `messages[i].content[j].type == "tool_result"`, `tool_use_id`, `content` (string or array) | `messages[i].role == "tool"`, `tool_call_id` (call_xxx), `content` (string) |

### Streaming translation

`OpenAIToAnthropicStreaming` and `AnthropicToOpenAIStreaming` (phase 3)
gain tool-call deltas:

**OpenAI → Anthropic streaming**

```
upstream: data: {choices:[{delta:{tool_calls:[{index:0, id:"call_xxx", type:"function", function:{name:"get_weather", arguments:""}}]}}]}
  → buffer call_xxx → toolu_xxx id via id_map
  → emit nothing yet (no input yet)

upstream: data: {choices:[{delta:{tool_calls:[{index:0, function:{arguments:"{\"city\""}}]}}]}
  → accumulate JSON string

upstream: data: {choices:[{delta:{tool_calls:[{index:0, function:{arguments:"\":\"SF\"}"}}]}}]}
  → continue

upstream: data: {choices:[{delta:{}}, finish_reason:"tool_calls"}]
  → parse accumulated JSON
  → emit:
    event: content_block_start
    data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_xxx","name":"get_weather","input":{}}}
    event: content_block_stop
    data: {"type":"content_block_stop","index":0}
  → mark tool_use index for ordering; final message_delta sets stop_reason="tool_use"
```

**Anthropic → OpenAI streaming**

```
upstream: event: content_block_start / data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_xxx","name":"get_weather","input":{}}}
  → emit:
    data: {"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_yyy","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}

upstream: event: content_block_delta / data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"city\""}}
  → emit:
    data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\""}}]},"finish_reason":null}]}

upstream: event: content_block_delta / data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\":\"SF\"}"}}
  → emit:
    data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\":\"SF\"}"}}]},"finish_reason":null}]}

upstream: event: content_block_stop / data: {"type":"content_block_stop","index":0}
  → emit:
    data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}
    data: [DONE]
```

### Multi-tool-call streaming

`parallel_tool_calls` from OpenAI is preserved by `index`. Anthropic has
no explicit index — the transcoder maintains an insertion-order index map
in the streaming state. The streaming transcoder's `flush()` ensures
every `content_block_start` produces exactly one terminal tool-call
chunk on the OpenAI side, even if the upstream cut off mid-arguments
(graceful-degradation with `loss_warning: truncated_tool_call`).

### Tests

- One unit test per direction covering: zero tools, one tool, multiple
  parallel tools, tool choice variants, tool result round-trip (OpenAI
  sends tool result → Anthropic upstream → Anthropic upstream emits
  final assistant tool_use → OpenAI client receives tool_calls).
- Streaming tests with byte boundaries spanning `input_json_delta`
  partial JSON.
- Error tests: malformed `arguments` JSON surfaces as a 502 to the
  client with a clear `error.message`.

### Files

```
src/eggpool/transcoder/openai_to_anthropic.py   # extend encode_request + decode_response
src/eggpool/transcoder/anthropic_to_openai.py   # mirror
src/eggpool/transcoder/streaming.py             # extend both streaming classes
src/eggpool/transcoder/ids.py                   # ToolCallIdMap (was scaffold in phase 1)

tests/unit/test_transcoder/test_openai_to_anthropic_tools.py
tests/unit/test_transcoder/test_anthropic_to_openai_tools.py
tests/unit/test_transcoder/test_streaming_tools.py
tests/integration/test_transcode_tools.py
```

---

## 2. Vision / image input

### Translation rules

**OpenAI → Anthropic**

| OpenAI input | Anthropic output |
|---|---|
| `messages[i].content[j].type == "image_url"`, `image_url.url` is `data:image/png;base64,...` | `messages[i].content[j].type == "image"`, `source: {type: "base64", media_type: "image/png", data: <bytes>}` |
| `image_url.url` is `https://...` | `source: {type: "url", url: <url>}` |
| `image_url.detail: "auto"\|"low"\|"high"` | dropped (Anthropic has no per-image detail knob) |
| `content[j].type == "input_audio"` | dropped with warning; no Anthropic audio input |
| `content[j].type == "file"` with `file.file_data` data URI | dropped with warning; documents are Anthropic-specific (see below) |
| `content[j].type == "file"` with `file.file_id` | dropped with warning (operator-side file storage not bridged) |

**Anthropic → OpenAI**

| Anthropic input | OpenAI output |
|---|---|
| `content[j].type == "image"`, `source.type == "base64"`, `media_type`, `data` | `content[j].type == "image_url"`, `image_url.url: "data:{media_type};base64,{data}"` |
| `content[j].type == "image"`, `source.type == "url"`, `url` | `content[j].type == "image_url"`, `image_url.url: <url>` |
| `content[j].type == "document"`, `source.type == "base64"`, `media_type == "application/pdf"`, `data` | `content[j].type == "file"`, `file.filename: "document.pdf"`, `file.file_data: "data:application/pdf;base64,{data}"` |
| `content[j].type == "document"`, `source.type == "url"` | dropped with warning; OpenAI has no PDF URL intake |
| `content[j].type == "document"`, `media_type` non-PDF | dropped with warning |

### Size limits

Anthropic rejects images larger than 5 MB and PDFs larger than 32 MB
(per docs). The transcoder checks `len(data) * 3 / 4 > limit` after
base64 decode and emits `image_too_large` / `pdf_too_large` loss
warnings; the upstream will reject anyway, but the warning lets
operators diagnose.

### Tests

- Unit tests round-trip a 1×1 PNG (a few hundred bytes) and a small PDF
  in both directions.
- Streaming tests are not required — images are not streamed.
- Error tests confirm `image_too_large` loss warning.

### Files

```
src/eggpool/transcoder/openai_to_anthropic.py   # extend content-part translation
src/eggpool/transcoder/anthropic_to_openai.py   # extend content-part translation

tests/unit/test_transcoder/test_openai_to_anthropic_vision.py
tests/unit/test_transcoder/test_anthropic_to_openai_vision.py
tests/integration/test_transcode_vision.py
```

---

## 3. Extended thinking / reasoning

### Translation rules

**OpenAI → Anthropic**

| OpenAI input | Anthropic output |
|---|---|
| `reasoning_effort: "low"\|"medium"\|"high"` on the request | `thinking: {type: "enabled", budget_tokens: <heuristic>}` (low→1024, medium→4096, high→16384) |
| `messages[i].role == "assistant"`, `reasoning_content` (string) | `messages[i].content[j].type == "thinking"`, `thinking: <content>` (signature dropped) |

**Anthropic → OpenAI**

| Anthropic input | OpenAI output |
|---|---|
| `thinking: {type: "enabled", budget_tokens}` on the request | drop with warning; OpenAI clients use `reasoning_effort` |
| `content[j].type == "thinking"`, `thinking`, `signature` | `messages[i].reasoning_content: <thinking>` (signature dropped with warning) |
| `content[j].type == "redacted_thinking"` | dropped with warning |
| `content[j].delta.type == "thinking_delta"` (streaming) | `delta.reasoning` |

### Streaming translation

Anthropic `thinking_delta` events translate to OpenAI
`delta.reasoning` strings. Order is preserved: thinking comes before
tool_use comes before text in Anthropic, and `reasoning_content` comes
before `content` in OpenAI. The streaming transcoder maintains block
indices accordingly.

### Why the signature is dropped

The Anthropic thinking signature is a cryptographic receipt that allows
re-feeding the thinking block back to the same model without
re-computation. When transcoding to OpenAI, the signature cannot be
re-fed because OpenAI has no equivalent. Dropping is safe; the next
turn simply re-reasons. The loss warning documents this.

A future enhancement may thread the signature through as an
out-of-band annotation on the response (e.g. an OpenAI `metadata`
field) so a sophisticated client could preserve it for re-feeding to a
different Anthropic endpoint. Out of scope for 6.3.

### Tests

- Unit tests for non-streaming request and response translation in both
  directions.
- Streaming tests for thinking deltas interleaved with text deltas.
- Loss-warning emission for dropped signatures and dropped
  `reasoning_effort` heuristic values.

### Files

```
src/eggpool/transcoder/openai_to_anthropic.py
src/eggpool/transcoder/anthropic_to_openai.py
src/eggpool/transcoder/streaming.py

tests/unit/test_transcoder/test_thinking.py
tests/unit/test_transcoder/test_streaming_thinking.py
tests/integration/test_transcode_thinking.py
```

---

## 4. Structured outputs

### Translation rules

OpenAI's `response_format: {type: "json_schema", json_schema: {...}}`
has no Anthropic equivalent. The transcoder translates it into a
**system-prompt coercion** that asks the model to respond in JSON
matching the schema, plus a structural validator that runs against the
upstream's text response.

**OpenAI → Anthropic (request)**

```
response_format: {type: "json_object"}
  → system: append "\n\nRespond with a valid JSON object. Do not include any text outside the JSON."

response_format: {type: "json_schema", json_schema: {name, schema, strict}}
  → system: append "\n\nRespond with a JSON object that matches this schema: <schema-as-string>. Do not include any text outside the JSON."
```

`strict: true` adds "Be precise; do not omit required fields."

**Anthropic → OpenAI (response)**

For non-streaming: parse the upstream text response as JSON; if it
parses, return it as `choices[0].message.content` (a JSON-stringified
value); if it fails, emit a loss warning and return the raw text. v1
does not validate against the schema on the transcoder side; the
client's own validator (which it always had) catches violations.

For streaming: the transcoder collects all text deltas and emits them
as a single OpenAI delta with `content: "<full json>"` near the end of
the stream. The terminal chunk carries `finish_reason: "stop"` and
`[DONE]`. The stream looks unusual to a strict OpenAI client that
expects incremental deltas, but every structured-output client
implements `finalize()` semantics, not incremental parsing, so this is
acceptable.

### Trade-offs

Anthropic does not enforce JSON-schema-constrained generation natively.
The transcoder's coercion is best-effort. Operators who need strict
guarantees should run an Anthropic-native client. This is documented in
`docs/transcoding.md` under "Structured outputs".

### Tests

- Unit tests for `json_object` and `json_schema` request translation.
- Unit tests for valid-JSON response round-trip.
- Unit tests for invalid-JSON response: loss warning emitted, raw text
  preserved.
- Streaming tests: deltas accumulate, final chunk is the assembled
  JSON.

### Files

```
src/eggpool/transcoder/openai_to_anthropic.py   # extend encode_request for response_format
src/eggpool/transcoder/anthropic_to_openai.py   # extend decode_response for JSON parsing
src/eggpool/transcoder/streaming.py             # streaming variant

tests/unit/test_transcoder/test_structured_outputs.py
tests/integration/test_transcode_structured.py
```

---

## 5. Anthropic primitives

These are Anthropic-only fields with no OpenAI counterpart. v1 dropped
them with warnings; 6.5 adds explicit handling where reasonable.

| Anthropic field | Translation |
|---|---|
| `top_k` | dropped with `top_k_dropped` warning. OpenAI does not sample by top-k. |
| `cache_control: {type: "ephemeral"}` | dropped with `cache_control_dropped` warning. OpenAI auto-caches without explicit hints. Could be approximated by heuristically attaching `cache_control` to the last system block on the reverse direction (Anthropic → Anthropic), but that is out of scope. |
| `metadata.user_id` → OpenAI `user` | translate verbatim |
| `stop_sequences` → OpenAI `stop` (already in v1 phase 2) | unchanged |
| `thinking` (already 6.3) | unchanged |
| `context_management: {edits: [...]}` | dropped with warning; experimental |
| `container: {...}` | dropped with warning; experimental |
| `mcp_servers: [...]` | dropped with warning; experimental |
| `service_tier` (Anthropic-only) | dropped; OpenAI `service_tier` translation already in phase 2 |
| `pause_turn` stop_reason | surface as `finish_reason: "tool_calls"` plus a sentinel `tool_call.name: "__eggpool_pause_turn__"` so clients can detect long-running tool calls and resume with the same `tool_use_id`. Documented in `docs/transcoding.md`. |

### Tests

- One test per primitive confirming the documented translation or
  warning.

### Files

```
src/eggpool/transcoder/openai_to_anthropic.py
src/eggpool/transcoder/anthropic_to_openai.py

tests/unit/test_transcoder/test_anthropic_primitives.py
```

---

## Validation per sub-phase

Each sub-phase follows the same validation pattern:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_transcoder/ -v
uv run pytest tests/integration/test_transcode_<feature>.py -v
uv run pytest tests/                                            # full suite
```

Acceptance criteria per sub-phase:

- The feature flag toggles behaviour correctly. Disabling the flag
  preserves today's warning-and-drop behaviour.
- Loss-warning catalogue is updated and `test_loss_warning_catalogue`
  still passes.
- Existing tests are unchanged.
- Documentation in `docs/transcoding.md` is updated with the new
  translation table row(s).

## Definition of done (phase 6)

The phase is complete when **all five sub-phases** are merged. The
release notes for the version that closes phase 6 read:

> Bidirectional OpenAI ↔ Anthropic protocol transcoding now covers
> tool use, vision, extended thinking, structured outputs, and the
> remaining Anthropic primitives. Operators enable individual
> features under `[transcoder.features]`. Text-only behaviour is the
> default.

A separate deprecation note in the same release:

> The `loss_policy = "reject"` setting is now implemented. When set,
> requests whose translation would lose information (e.g. an OpenAI
> `logit_bias` to an Anthropic upstream) are rejected with HTTP 400
> before dispatch instead of silently dropping the field. Operators
> who prefer the warn-and-continue behaviour should leave
> `loss_policy = "warn"` (the default).

The `"reject"` policy implementation is a small follow-up after 6.5
because it requires the coordinator to read `loss_warnings` after
`encode_request` and short-circuit. The implementation is documented in
`plans/phase-6-reject-policy.md` (to be written when 6.5 lands).

## Completed Phases

### 6.1 — Tool use / function calling (shipped 2026-06-30)

Sub-phase 6.1 is complete. The implementation-grade plan lives in
`plans/tooltranscoding.md`; the operator-facing documentation lives in
`docs/transcoding.md` § Tool-Use Transcoding.

What shipped:

- Body translation in both directions for `tools`, `tool_choice`,
  `parallel_tool_calls`, assistant `tool_calls` history, `role: "tool"`
  history, and `tool_use` / `tool_result` content blocks.
- Streaming tool-call delta translation via the existing
  `StreamingTranscoder` interface (`AnthropicToOpenAIStreaming` and
  `OpenAIToAnthropicStreaming`). `AnthropicToOpenAIStreaming` emits
  OpenAI `tool_calls` deltas in insertion order; `OpenAIToAnthropicStreaming`
  buffers `tool_calls[*].function.arguments` chunks and flushes
  Anthropic `tool_use` blocks on `finish_reason: "tool_calls"`.
- A per-request `ToolCallIdMap` (`TranscodeContext.id_map`) minting
  `call_<24 hex>` and `toolu_<24 hex>` ids so the two namespaces never
  collide. `generate_openai_id()` / `generate_anthropic_id()` produce
  24 hex characters after the prefix.
- `pause_turn` sentinel handling: Anthropic's `pause_turn` `stop_reason`
  maps to `finish_reason: "tool_calls"` plus a synthetic
  `__eggpool_pause_turn__` tool_call entry. OpenAI clients detect the
  pause by name. A `pause_turn` loss warning is appended whenever the
  sentinel is synthesized.
- `stream_options.include_usage` lifting onto
  `TranscodeContext.request_include_usage` so the streaming transcoder
  can decide whether to forward upstream usage chunks.
- New loss-warning kinds registered in `LOSS_WARNING_KINDS`:
  `tool_call_id_translated`, `tool_call_id_changed`,
  `parallel_tool_calls_collapsed`, `malformed_tool_arguments`,
  `invalid_tool_choice`, `unsupported_tool_type`, `empty_tool_use_block`,
  `tool_result_image_dropped`, `tool_result_error_passthrough`,
  `cache_control_dropped`, `pause_turn`, `non_text_content_dropped`,
  `tool_result_inferred`.

Sub-phase 6.1 ships with tools-on-only; the `[transcoder.features] tools = false`
opt-out is a phase 6.5 follow-up. The structured `pause_turn` surface
(first-class rather than the inline sentinel) and the structured
`is_error` response shape are also 6.5 follow-ups.