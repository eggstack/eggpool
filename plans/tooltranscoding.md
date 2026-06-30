# Tool-Use Transcoding (Phase 6.1)

## Goal

Translate tool use / function calling between OpenAI Chat Completions and
Anthropic Messages in both directions, for **non-streaming and streaming**
requests, so that OpenAI clients (OpenCode, Cursor, Aider, …) can drive
tool-using workflows against Anthropic-only upstreams (e.g. MiniMax
International) and Anthropic clients (Claude Code, …) can drive the same
workflows against OpenAI-compatible upstreams.

After this plan lands, the live log line the user hit

```
transcode.loss_warnings ... warnings=[
  {'kind': 'dropped_field', 'field': 'tools', 'reason': 'anthropic_unsupported'},
  {'kind': 'dropped_field', 'field': 'tool_choice', 'reason': 'anthropic_unsupported'},
  {'kind': 'dropped_field', 'field': 'stream_options', 'reason': 'anthropic_unsupported'},
]
```

no longer appears for tool-using requests. The OpenAI client's `tools`
and `tool_choice` reach MiniMax intact, and tool calls emitted by the
upstream Anthropic model are reconstructed as OpenAI `tool_calls` on the
response (streaming and non-streaming).

This is the first sub-phase of `plans/phase-6-deferred-features.md` § 1
("Tool use / function calling"). That section is now promoted from a
high-level sketch to an implementation-grade plan; later sub-phases
(6.2 vision, 6.3 thinking, …) will reuse the same scaffolding.

## Why this is a real bug, not a feature gap

The transcoder is **the user-facing reason** OpenCode cannot use MiniMax
M-series models with tools, even though the upstream speaks Anthropic and
the router is otherwise capable. The dropped `tools` field means the
model never sees the tool schemas; the dropped `tool_choice` means
clients cannot force/auto-configure tool use; the response decoder
(`openai_to_anthropic.py:179-241`) drops `tool_use` content blocks so
even if the model called a tool (because it inferred the schema from
elsewhere, or because `prompt-cache` priming primed one), the client
never sees the `tool_calls`. Both halves of the round trip must be
fixed together.

## Scope

In scope:

- **Request encoding (both directions)**:
  - Translate `tools` / `tool_choice` between OpenAI's function-shape and
    Anthropic's tool-shape.
  - Translate assistant `tool_calls` history into Anthropic `tool_use`
    content blocks.
  - Translate `role: "tool"` history messages into Anthropic `tool_result`
    content blocks (and vice versa).
  - Stable, per-request id translation between `call_…` (OpenAI) and
    `toolu_…` (Anthropic) via `ToolCallIdMap`.
- **Response decoding (both directions)**:
  - Reconstruct OpenAI `choices[].message.tool_calls` from Anthropic
    `content[].type == "tool_use"` blocks.
  - Reconstruct Anthropic `content[].type == "tool_result"` blocks from
    OpenAI `role: "tool"` messages.
  - Preserve `finish_reason` / `stop_reason` semantics (`tool_use` ↔
    `tool_calls`; `pause_turn` ↔ `tool_calls` + sentinel).
- **Streaming (both directions)**:
  - Track per-tool-call `index` across `content_block_start`,
    `input_json_delta`, `content_block_stop` events.
  - Emit OpenAI `delta.tool_calls[i] = {index, id, function: {name,
    arguments}}` chunks in insertion order.
  - Emit Anthropic `content_block_start` (with `id`, `name`, `input: {}`)
    + `content_block_delta` (`input_json_delta`, `partial_json`) +
    `content_block_stop` triples from accumulated OpenAI
    `tool_calls[*].function.arguments` strings.
  - Guarantee terminal chunks: the last SSE chunk always carries the
    terminal `finish_reason` / `stop_reason` plus `[DONE]` /
    `message_stop`.
- **Multi-tool-call / parallel_tool_calls**:
  - Preserve OpenAI's `parallel_tool_calls: true` semantics by emitting
    multiple Anthropic `tool_use` blocks at distinct indices.
  - OpenAI's `parallel_tool_calls: false` is dropped with a loss warning
    (no Anthropic equivalent — see "Known lossy mappings" below).
- **Updated fixtures, loss-warning catalogue, docs**.

Out of scope (deferred to other sub-phases of phase 6):

- Vision / image input (6.2).
- Extended thinking / `reasoning_content` (6.3).
- Structured outputs (`response_format`, `json_schema`) (6.4).
- Anthropic primitives like `cache_control` and `pause_turn` sentinels
  (6.5 — basic `pause_turn` → `tool_calls` mapping remains, no
  `__eggpool_pause_turn__` sentinel yet).
- Anthropic `mcp_servers` and `context_management` (drop with warning,
  unchanged).
- Token-id and message-id translation outside tool calls (none needed
  for OpenAI; Anthropic message ids are dropped on non-streaming
  responses today and that continues).

## Existing scaffolding to reuse

These exist today and must be wired in, not invented:

| Asset | Location | Status today |
|---|---|---|
| `ToolCallIdMap` (`call_…` ↔ `toolu_…`) | `src/eggpool/transcoder/ids.py:10` | Defined; **never imported anywhere**. |
| `STOP_REASON_MAP` (Anthropic `tool_use` → OpenAI `tool_calls`) | `src/eggpool/transcoder/openai_to_anthropic.py:19-27` | Defined and tested. |
| `_STOP_TO_FINISH` / `_FINISH_TO_STOP` mappings | `src/eggpool/transcoder/streaming.py:26-43` | Defined; streaming transcoder never emits tool deltas. |
| `incremental SSE observer` | `src/eggpool/proxy/sse_observer.py` | Used by both streaming classes. |
| `has_non_text_blocks` helper | `src/eggpool/transcoder/json_helpers.py:36` | Treats `tool_use` / `tool_result` blocks as "non-text" — needs a sibling helper `extract_tool_blocks` that walks both. |

The plan changes these files **in place** rather than adding parallel
modules.

## Files to modify

```
src/eggpool/transcoder/
├── openai_to_anthropic.py      # encode_request: tools, tool_choice, tool_calls history, role=tool messages
│                               # decode_response: tool_use content blocks → message.tool_calls
├── anthropic_to_openai.py      # mirror
├── streaming.py                # both streaming classes: tool delta state machines
├── ids.py                      # ToolCallIdMap: harden generation + add truncation helper
├── json_helpers.py             # add extract_tool_blocks; gate has_non_text_blocks on tool types
├── context.py                  # TranscodeContext gains id_map default
└── __init__.py                 # export id_map helper if needed

src/eggpool/api/proxy_request.py   # preflight: include Anthropic request tool blocks in token estimate

docs/transcoding.md               # update translation tables; document lossy mappings

tests/unit/test_transcoder/
├── test_openai_to_anthropic_body.py      # replace test_dropped_fields_from_payload with translation assertions
├── test_openai_to_anthropic_response.py  # replace test_tool_use_content_dropped
├── test_anthropic_to_openai_body.py      # replace test_tools_dropped_with_warning + test_tool_choice_dropped_with_warning
├── test_anthropic_to_openai_response.py  # new: tool_use → tool_calls assertions
├── test_streaming_openai_to_anthropic.py # new: tool delta stream
├── test_streaming_anthropic_to_openai.py # new: tool_use → tool_calls stream
├── test_ids.py                            # new: id map behaviour, truncation, uuid determinism
└── fixtures/
    ├── openai_tools_request.json          # NEW
    ├── openai_tool_call_response.json     # NEW (model emits tool_calls)
    ├── anthropic_tool_use_request.json    # NEW
    ├── anthropic_tool_use_response.json   # NEW
    └── openai_streaming_tool_calls.jsonl  # NEW (one SSE chunk per line)

tests/integration/
├── test_transcode_tools.py         # NEW: respx + coordinator, OpenAI→Anthropic and Anthropic→OpenAI
└── test_transcode_streaming_tools.py # NEW: streaming tool round-trip
```

No database migrations. No new dependencies. No config-section changes
(tools stay on by default — see "Configuration" below).

## Files to add

```
tests/unit/test_transcoder/test_ids.py
tests/integration/test_transcode_tools.py
tests/integration/test_transcode_streaming_tools.py
tests/unit/test_transcoder/fixtures/openai_tools_request.json
tests/unit/test_transcoder/fixtures/openai_tool_call_response.json
tests/unit/test_transcoder/fixtures/anthropic_tool_use_request.json
tests/unit/test_transcoder/fixtures/anthropic_tool_use_response.json
tests/unit/test_transcoder/fixtures/openai_streaming_tool_calls.jsonl
```

## Detailed design

### 1. `ToolCallIdMap` hardening (`src/eggpool/transcoder/ids.py`)

Today the map is defined but not imported. Promote it to the per-request
state container: `TranscodeContext` gains an `id_map: ToolCallIdMap`
default field (`context.py:25` next to `loss_warnings`).

Two semantic gaps in the existing implementation:

1. The id generator (`generate_upstream_id`, `ids.py:40`) uses a
   monotonic counter — this is fine within one request but the id
   strings `f"tcu_{self._counter}"` collide with Anthropic's `toolu_…`
   namespace. Anthropic ids are 22–24 hex chars after the prefix. Switch
   to `f"toolu_{uuid.uuid4().hex[:24]}"` (matches what real Anthropic
   responses emit and stays inside Anthropic's parser).
2. The reverse direction (`register(client_id=toolu_X, upstream_id=call_Y)`)
   must generate `call_` ids with the same shape: `f"call_{uuid.uuid4().hex[:24]}"`.
   OpenAI clients that echo back the id expect this format.

API additions:

```python
class ToolCallIdMap:
    def register(self, client_id: str, upstream_id: str) -> None: ...
    def to_upstream(self, client_id: str) -> str | None: ...
    def to_client(self, upstream_id: str) -> str | None: ...

    def generate_openai_id(self) -> str:
        """Generate an OpenAI-shaped id (`call_<hex>`)."""
        return f"call_{uuid.uuid4().hex[:24]}"

    def generate_anthropic_id(self) -> str:
        """Generate an Anthropic-shaped id (`toolu_<hex>`)."""
        return f"toolu_{uuid.uuid4().hex[:24]}"
```

`uuid` is already a stdlib import in the project (`eggpool/utils/...`
uses it elsewhere; grep confirms `import uuid` shows up in many
modules). No new dependency.

The streaming transcoder also needs a `truncate` helper:

```python
def truncate(self, upstream_id: str) -> None:
    """Drop accumulated arguments buffer for `upstream_id` after a
    malformed-JSON or mid-stream upstream cut-off. Emits a loss
    warning via the supplied TranscodeContext."""
```

This is called from `AnthropicToOpenAIStreaming.flush()` when the
accumulated `partial_json` fails `json.loads()`.

### 2. `OpenAIToAnthropic.encode_request` (`src/eggpool/transcoder/openai_to_anthropic.py`)

The current loop at lines 81–116 treats each message as either system /
user / assistant / tool (where `role: "tool"` is dropped wholesale) and
flattens `content` to a string. Replace with a content-list-aware loop
that:

1. Extracts the system message(s) as today.
2. For each non-system message, walks `content` as either:
   - `str` → wrap in `[{"type": "text", "text": s}]`
   - `list[dict]` → translate part-by-part
3. Special-cases `role: "assistant"` with `tool_calls` → emit a single
   content list with one `tool_use` block per call (using the id map
   to mint `toolu_…` ids).
4. Special-cases `role: "tool"` → emit Anthropic `tool_result` content
   blocks (using the id map to translate `tool_call_id` to `tool_use_id`).

Translation rules:

| OpenAI input | Anthropic output | Notes |
|---|---|---|
| `tools[i].type == "function"` | `tools[i]` with `name`, `description`, `input_schema` | `function.name`, `function.description`, `function.parameters` lifted; `strict` dropped (no Anthropic equivalent) with warning. |
| `tools[i].type` other than `"function"` | dropped with warning `unsupported_tool_type` | |
| `tool_choice: "none"` | `tool_choice: {type: "none"}` | |
| `tool_choice: "auto"` (default) | omit `tool_choice` (Anthropic default = auto) | If absent on input, omit on output — do not synthesize. |
| `tool_choice: "required"` | `tool_choice: {type: "any"}` | |
| `tool_choice: {type: "function", function: {name}}` | `tool_choice: {type: "tool", name}` | |
| `tool_choice: {type: "function", function: {name: ""}}` | dropped with warning `invalid_tool_choice` | |
| `parallel_tool_calls: true` | omit (Anthropic defaults to allowing parallel) | |
| `parallel_tool_calls: false` | dropped with warning `parallel_tool_calls_unsupported` | Anthropic has no parallel-disable knob. |
| `messages[i].role == "assistant"`, `tool_calls[j].id` (`call_…`) | `messages[i].content[k].type == "tool_use"`, `id` (`toolu_…`) | `id_map.register(call_…, toolu_…)`; Anthropic ids minted fresh. |
| `messages[i].role == "assistant"`, `tool_calls[j].type == "function"`, `function.name`, `function.arguments` | `tool_use.name`, `tool_use.input` (parsed JSON object) | If `arguments` is not valid JSON, keep it as `{"__raw_arguments__": "<string>"}` with warning `malformed_tool_arguments`. The model will see the warning in the loss log. |
| `messages[i].role == "assistant"`, `content: str`, `tool_calls: []` | as today (string content) | unchanged |
| `messages[i].role == "assistant"`, `content: str` + `tool_calls: [...]` | emit both: `content: [{type: "text", text: s}]` + `tool_use` blocks | Anthropic allows mixed text + tool_use in one assistant turn. |
| `messages[i].role == "tool"`, `content: str`, `tool_call_id` | `messages[i].role == "user"`, `content: [{type: "tool_result", tool_use_id: <toolu>, content: <str>, is_error: false}]` | `tool_use_id` resolved via `id_map.to_upstream(call_id)`. |
| `messages[i].role == "tool"`, `content: list` | `messages[i].content[k].type == "tool_result"`, `content: list` | Each part translated (text → text block; image → image block in 6.2). v1 supports text parts only; non-text parts dropped with warning `tool_result_image_dropped`. |
| `messages[i].role == "tool"`, `is_error: true` (extension) | `is_error: true` on the tool_result block | OpenAI does not define this; some clients emit it on failure. Forwarded if present. |

Move the dropped-field loop at line 167–175 so that it runs **only
after** the tools/tool_choice/parallel_tool_calls handling. After this
plan, the only fields that genuinely get dropped from a tool-using
request are: `function_call` (deprecated), `functions` (deprecated),
`logit_bias`, `top_p`, `presence_penalty`, `frequency_penalty`, `n`,
`logprobs`, `top_logprobs`, `response_format`, `seed`, `user`,
`stream_options.include_usage` (replaced — see streaming below).

### 3. `OpenAIToAnthropic.decode_response` (`src/eggpool/transcoder/openai_to_anthropic.py:179-241`)

Today the response decoder walks `content` and concatenates only `text`
parts. Replace with a structured walker:

| Anthropic input | OpenAI output |
|---|---|
| `content[k].type == "text"` | text accumulator (as today) |
| `content[k].type == "tool_use"`, `id` (`toolu_…`), `name`, `input` | `choices[0].message.tool_calls[k']` with `id` (`call_…`), `type: "function"`, `function.name`, `function.arguments` (JSON-stringified `input`) |
| `content[k].type == "thinking"` | dropped with warning `thinking_dropped` (kept for 6.3; same behaviour as today) |
| `content[k].type == "redacted_thinking"` | dropped with warning `thinking_dropped` |
| `stop_reason: "tool_use"` | `finish_reason: "tool_calls"` (already mapped) |
| `stop_reason: "pause_turn"` | `finish_reason: "tool_calls"` + append a sentinel `tool_calls` entry `{"id": "call_pause_turn_<req_id>", "type": "function", "function": {"name": "__eggpool_pause_turn__", "arguments": "{}"}}` so the client can detect the pause and resume with the same `tool_use_id`. (6.5 adds the dedicated sentinel surface; this plan emits it inline.) |

`tool_calls` index assignment: tools are emitted in the order they
appear in the Anthropic `content` array. Multiple tool_use blocks
preserve insertion order. No reordering.

The text accumulator for an assistant turn that has both text and
tool_use blocks is non-empty: keep the existing behaviour of
concatenating text parts and putting the result on `message.content`,
while also populating `message.tool_calls` (OpenAI allows both — many
clients rely on `content` for narration even when `tool_calls` is
populated).

`finish_reason: "tool_calls"` is emitted even when `tool_calls` is
empty (some edge cases produce zero-length `tool_use` blocks that the
upstream nonetheless signals with `stop_reason: "tool_use"`). The
warning `empty_tool_use_block` documents the situation.

### 4. `AnthropicToOpenAI.encode_request` (`src/eggpool/transcoder/anthropic_to_openai.py`)

Mirror of the above. Today the message loop drops non-text content
parts at lines 73–83. Replace with:

| Anthropic input | OpenAI output |
|---|---|
| `messages[i].content[k].type == "tool_use"`, `id` (`toolu_…`), `name`, `input` | `messages[i].role == "assistant"`, `tool_calls[k']` with `id` (`call_…`), `type: "function"`, `function.name`, `function.arguments` |
| `messages[i].content[k].type == "tool_result"`, `tool_use_id`, `content` (string) | `messages[i].role == "tool"`, `tool_call_id` (`call_…`), `content` (string) |
| `messages[i].content[k].type == "tool_result"`, `content: list` | `messages[i].role == "tool"`, `content` (joined text) | join text parts with `\n`; non-text dropped with warning. |
| `messages[i].content[k].type == "tool_result"`, `is_error: true` | `messages[i].role == "tool"`, `content: <error text>`, plus loss warning `tool_result_error_passthrough` | OpenAI has no `is_error` field; surface as a warning so the dashboard can flag it. |
| `tools[i].name`, `description`, `input_schema` | `tools[i].type == "function"`, `function.{name, description, parameters(input_schema)}` | |
| `tools[i].cache_control` | dropped with warning `cache_control_dropped` (phase 6.5) | |
| `tool_choice: {type: "auto"}` | `"auto"` | |
| `tool_choice: {type: "any"}` | `"required"` | |
| `tool_choice: {type: "tool", name}` | `{"type": "function", "function": {"name": name}}` | |
| `tool_choice: {type: "none"}` | `"none"` | |

After this change, `DROPPED_FIELDS` (`anthropic_to_openai.py:34`) shrinks to
`("top_k", "thinking", "cache_control", "context_management",
"container", "mcp_servers")` and `tools` / `tool_choice` are removed.

### 5. `AnthropicToOpenAI.decode_response` (`src/eggpool/transcoder/anthropic_to_openai.py:137-198`)

Today the response decoder at lines 144–198 reads
`choices[0].message.content` and `choices[0].finish_reason`; it does
**not** read `choices[0].message.tool_calls`. Extend with:

| OpenAI input | Anthropic output |
|---|---|
| `choices[0].message.tool_calls[k]` | `content[k'].type == "tool_use"`, `id` (`toolu_…`), `name`, `input` (parsed JSON object) | `arguments` JSON-parsed; malformed → `{"__raw_arguments__": "<string>"}` + warning. |
| `choices[0].message.content: str` + `tool_calls` | `content: [{type: "text", text: s}] + [{type: "tool_use", ...}]` | Anthropic allows mixed. |
| `choices[0].finish_reason == "tool_calls"` | `stop_reason: "tool_use"` | unchanged from STOP_REASON_MAP. |
| `choices[0].message.refusal` + `tool_calls` | refusal text block + tool_use blocks | refusal wins over content text. |

### 6. Streaming — `AnthropicToOpenAIStreaming` (`src/eggpool/transcoder/streaming.py:498-733`)

Add state for tool_use block tracking. Each Anthropic
`content_block_start` with `content_block.type == "tool_use"` opens an
indexed slot in `_tool_blocks: dict[int, _OpenAIToolCall]` keyed by the
Anthropic `index`. Each `content_block_delta` with
`delta.type == "input_json_delta"` appends `partial_json` to the slot's
argument buffer. `content_block_stop` flushes nothing extra but marks
the slot finalised (so `flush()` knows whether the buffer was complete).

Per `_OpenAIToolCall` state:

```python
@dataclass(slots=True)
class _OpenAIToolCall:
    index: int           # Anthropic content_block index
    openai_index: int    # 0-based position in the tool_calls array
    id: str              # translated call_<…> id
    name: str            # function name
    arguments: str = ""  # accumulated partial_json
    finalised: bool = False
```

Event-by-event translation:

```
upstream: event: content_block_start /
  data: {"content_block": {"type": "tool_use", "id": "toolu_X", "name": "get_weather", "input": {}}}
  → mint a new _OpenAIToolCall(id=call_Y, name=get_weather, openai_index=N)
  → register id_map(toolu_X → call_Y)
  → emit:
    data: {"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":N,"id":"call_Y","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}

upstream: event: content_block_delta /
  data: {"delta": {"type": "input_json_delta", "partial_json": "{\"city\""}}
  → append partial_json to arguments buffer
  → emit:
    data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":N,"function":{"arguments":"{\"city\""}}]},"finish_reason":null}]}

upstream: event: content_block_stop /
  data: {"index": N}
  → mark finalised
  → emit nothing extra (the final arguments chunk has already been emitted)
```

When `finish_reason: "tool_calls"` arrives on `message_delta`, emit:

```
data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}
data: [DONE]
```

The terminal `data: [DONE]` is the existing `_emit_done()` path
(streaming.py:728).

If `flush()` is called and the upstream never emitted a `content_block_stop`
for some open slot, the slot is left open. Finalise it: try
`json.loads(arguments)`; if it fails, emit a warning
`malformed_tool_arguments` via `TranscodeContext.loss_warnings` and emit
a final `data: {"choices":[{"delta":{"tool_calls":[{"index":N,"function":{"arguments":arguments}}]}}]}` chunk anyway (the client may still be able to use the partial JSON). This
prevents the client's tool-use loop from hanging on a missing terminal.

### 7. Streaming — `OpenAIToAnthropicStreaming` (`src/eggpool/transcoder/streaming.py:265-495`)

The reverse direction has to buffer `tool_calls[*].function.arguments`
strings across many OpenAI deltas until `finish_reason: "tool_calls"`
arrives, then emit the Anthropic shape.

State:

```python
@dataclass(slots=True)
class _AnthropicToolUse:
    openai_index: int    # OpenAI tool_calls[*].index
    anthropic_index: int # 0-based position in the Anthropic content block list
    id: str              # translated toolu_<…> id
    name: str            # function name
    arguments: str = ""  # accumulated arguments string
```

The current `_dispatch` (streaming.py:340) only inspects
`delta.content`. Extend it with a tool_calls branch:

```
upstream: data: {choices:[{delta:{tool_calls:[{index:0, id:"call_X", type:"function", function:{name:"get_weather", arguments:""}}]}}]}
  → if slot[0] is empty, allocate it: id_map(call_X → toolu_Y), name=get_weather, arguments=""
  → emit nothing (no Anthropic event corresponds to the id+name announcement — wait for finish)

upstream: data: {choices:[{delta:{tool_calls:[{index:0, function:{arguments:"{\"city\""}}]}}]}
  → slot[0].arguments += "{\"city\""
  → emit nothing

upstream: data: {choices:[{}, finish_reason:"tool_calls"]}
  → for each slot in openai_index order:
      parsed = json.loads(slot.arguments) or {"__raw_arguments__": slot.arguments}
      emit:
        event: content_block_start
        data: {"index":slot.anthropic_index, "content_block":{"type":"tool_use","id":slot.id,"name":slot.name,"input":parsed}}
        event: content_block_stop
        data: {"index":slot.anthropic_index}
  → continue with the existing finish flow: emit content_block_stop for text (if any), message_delta with stop_reason="tool_use", message_stop
```

Important edge case: the first OpenAI tool_call delta carries `id` and
`name` with `arguments: ""`. The second delta may carry `function.name`
and `function.arguments` only — no `id`. The slot must persist across
deltas keyed by OpenAI `tool_calls[*].index`. If an OpenAI chunk
arrives with a new `index`, allocate a new slot. If an existing slot
gets a non-empty `id`, that's an error condition (callers should not
update the id mid-stream); log a warning `tool_call_id_changed` and
re-register the new id.

Parallel tool calls: OpenAI's `tool_calls[*].index` is monotonically
increasing within one assistant turn. Map OpenAI index → insertion
order in our internal list. Anthropic indices are the position in that
internal list at emission time.

Terminal emission: today the OpenAI → Anthropic streaming transcoder
emits a single `content_block_stop` for index 0 (line 442–447) when
`self._content_block_started`. Replace with a loop over all open
content blocks (text + tool_use) before the `message_delta` /
`message_stop` pair. Text block stays at Anthropic index 0;
tool_use blocks follow in insertion order.

### 8. `stream_options` handling

The user's log line includes `'field': 'stream_options', 'reason':
'anthropic_unsupported'`. Today the entire `stream_options` object is
dropped because Anthropic doesn't have an analogue. **However**, the
single field inside that controls our behaviour is
`stream_options.include_usage: bool` — which the existing
`AnthropicToOpenAIStreaming._on_message_delta` already consults at
line 668 to decide whether to forward the upstream usage chunk.

Implementation:

- **OpenAI → Anthropic (encode_request)**: lift
  `stream_options.include_usage` out of `stream_options`, drop the rest
  of `stream_options` with warning, and stash it on the
  `TranscodeContext` (new field `request_include_usage: bool`) so the
  streaming transcoder can read it back during emission. The streaming
  transcoder is constructed per-request (select_streaming_transcoder is
  called from coordinator), so a context kwarg suffices.
- **Anthropic → OpenAI (encode_request)**: nothing to do — OpenAI
  defaults to omitting usage in streaming; we only forward usage if
  `stream_options.include_usage` was set on the request, which it
  wasn't.

### 9. Preflight token accounting (`src/eggpool/api/proxy_request.py`)

`_check_context_limits` runs against the **client** payload. With tools
in play, the upstream Anthropic payload adds:
- A `tools` array (each tool's `input_schema` may be large).
- `tool_use` content blocks (history) → count toward input tokens.
- `tool_result` content blocks (history) → count toward input tokens.

The Anthropic tokenizer sees these strings, not the OpenAI function
serialization. v1 does not run a second preflight on the translated
upstream payload (the docstring at `api/proxy_request.py:150` already
defers this to phase 6). Instead, add a **conservative padding** when
transcoding tools: `+max(64, sum(len(json.dumps(tool)) for tool in
tools) // 4)` input tokens. This is a rough heuristic — Anthropic's
tool schemas typically encode to ~30% of their JSON size in tokens.
Operators who need exact preflight can switch off transcoding for that
model.

This padding is added inside `RequestCoordinator._execute_non_streaming`
**after** `encode_request` but before dispatch. It does not need a
second pass through `_check_context_limits`; the existing
`ContextLimitExceededError` path will still fire if the request now
exceeds the model's documented limit.

### 10. Loss-warning catalogue update (`src/eggpool/transcoder/__init__.py`)

Add the new kinds to the catalogue documented in `phase-6-deferred-features.md:108`:

```python
LOSS_WARNING_KINDS = frozenset({
    # ... existing ...
    # 6.1 (tools) — this plan
    "dropped_field", "value_clamped",
    "tool_call_id_translated", "parallel_tool_calls_collapsed",
    "tool_result_image_dropped", "malformed_tool_arguments",
    "invalid_tool_choice", "unsupported_tool_type",
    "empty_tool_use_block", "tool_call_id_changed",
    "tool_result_error_passthrough",
})
```

`tool_call_id_translated` is emitted whenever the id map mints a new id
on either side, so operators can audit id-translation traffic. Other
warnings are emitted on the specific edge cases above.

### 11. Documentation updates (`docs/transcoding.md`)

Three changes:

1. Replace the current "Dropped, warning emitted" entries for `tools`,
   `tool_choice`, `messages[tool]`, `tool_use` blocks, `functions`,
   `function_call`, `parallel_tool_calls` with their translation rows.
   Add rows for `tool_calls` (assistant history), `tool_result`
   (history and response), `stream_options.include_usage` (lifted).
2. Extend the "Known Lossy Mappings" table with new entries:
   - `parallel_tool_calls: false` → dropped (Anthropic has no
     parallel-disable)
   - `function_call` / `functions` → dropped (deprecated OpenAI API)
   - `pause_turn` → `tool_calls` + sentinel
     `__eggpool_pause_turn__` (6.1 placeholder; 6.5 refines)
   - `tools[].function.strict` → dropped
3. Update the "Known Limitations" section: remove item 1 ("Text-only in
   v1") and replace with "Tool calling and structured outputs are
   translated; vision / thinking / PDF / audio are still deferred to
   later sub-phases."

### 12. Configuration

No new `[transcoder]` block. Tools stay on by default once this plan
ships — dropping tools silently was the bug; turning tools on by
default matches the policy that "transcoding is on by default"
(transcoding.md:9).

Operators who want to opt out of tool translation (and revert to
silent-drop) can set:

```toml
[transcoder.features]
tools = false    # (phase 6 scaffold; not present in code today)
```

If we land `tools = false` support as part of this plan, the
`encode_request` paths re-check the feature flag from `TranscoderPolicy`
and fall back to the v1 drop-with-warning behaviour. **For the first
merge**, we ship tools-on-only and add the feature flag in a follow-up
PR — splitting these keeps the diff small and the test surface tight.

## Updated and new tests

### Update (replace) existing tests

`tests/unit/test_transcoder/test_openai_to_anthropic_body.py`:

- **Delete** `TestDroppedFields.test_dropped_fields_from_payload` and
  split into `TestToolTranslation` covering: zero tools, one tool, many
  parallel tools, mixed tool types, all four `tool_choice` shapes,
  `parallel_tool_calls: true/false`, `tools[].function.strict` drop
  warning.
- **Replace** the `tools`, `tool_choice`, `parallel_tool_calls`
  entries in `test_dropped_fields_from_payload` with translation
  assertions.

`tests/unit/test_transcoder/test_openai_to_anthropic_response.py`:

- **Replace** `test_tool_use_content_dropped` with
  `test_tool_use_block_becomes_tool_call` (asserts
  `message.tool_calls[0].function.name == "weather"`, id round-trip
  via id_map, arguments JSON-decoded).
- Add `test_multiple_tool_use_blocks` (two `tool_use` content blocks
  produce two `tool_calls`).
- Add `test_text_and_tool_use_both_emitted`.

`tests/unit/test_transcoder/test_anthropic_to_openai_body.py`:

- **Replace** `test_tools_dropped_with_warning` and
  `test_tool_choice_dropped_with_warning` with translation assertions.
- Add `test_tool_use_history_translated` (Anthropic `tool_use` content
  block → OpenAI assistant `tool_calls`).
- Add `test_tool_result_history_translated`.

`tests/unit/test_transcoder/test_anthropic_to_openai_response.py`:

- New `test_tool_calls_become_tool_use_blocks`.
- New `test_text_and_tool_calls_both_emitted`.

### New unit tests

`tests/unit/test_transcoder/test_ids.py`:

- `register` is bidirectional.
- `generate_openai_id` returns `call_<24 hex>`.
- `generate_anthropic_id` returns `toolu_<24 hex>`.
- Two generators never collide over 10 000 iterations (probabilistic).

`tests/unit/test_transcoder/test_streaming_anthropic_to_openai.py`:

Already has `test_message_delta_tool_use_maps_to_tool_calls` (line 296).
Add:
- `test_tool_use_block_emits_openai_tool_call_chunks` — full
  content_block_start + input_json_delta + content_block_stop +
  message_delta flow produces the expected OpenAI deltas in order.
- `test_multiple_tool_use_blocks_parallel`
- `test_tool_arguments_split_across_chunks` — partial_json arrives
  across byte boundaries; arguments reassembled.
- `test_flush_emits_pending_tool_call` — upstream cut off before
  content_block_stop; flush still emits a final delta with the
  accumulated arguments.
- `test_malformed_tool_arguments_warning` — partial_json is not valid
  JSON; warning emitted, raw string still delivered.

`tests/unit/test_transcoder/test_streaming_openai_to_anthropic.py`:

- `test_tool_call_chunks_emit_anthropic_block_at_finish` — three
  incremental `function.arguments` deltas + finish → one
  content_block_start + one content_block_stop with the assembled
  `input` JSON.
- `test_multiple_tool_calls_parallel` — two `tool_calls[*]` with
  different `index` → two Anthropic blocks at distinct indices.
- `test_malformed_arguments_passthrough` — arguments string is not
  valid JSON; warning emitted, raw string preserved as
  `input.__raw_arguments__`.

### New fixtures

`tests/unit/test_transcoder/fixtures/openai_tools_request.json`:

```json
{
  "model": "gpt-4",
  "messages": [{"role": "user", "content": "Weather in SF?"}],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        "strict": true
      }
    }
  ],
  "tool_choice": {"type": "function", "function": {"name": "get_weather"}}
}
```

`tests/unit/test_transcoder/fixtures/openai_tool_call_response.json`:

```json
{
  "id": "chatcmpl-X",
  "object": "chat.completion",
  "created": 0,
  "model": "gpt-4",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_aaa",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\": \"SF\"}"}
      }]
    },
    "finish_reason": "tool_calls"
  }],
  "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
}
```

`tests/unit/test_transcoder/fixtures/anthropic_tool_use_request.json`:

```json
{
  "model": "claude-3",
  "system": "Be concise.",
  "messages": [{"role": "user", "content": "Weather in SF?"}],
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
  }],
  "tool_choice": {"type": "tool", "name": "get_weather"}
}
```

`tests/unit/test_transcoder/fixtures/anthropic_tool_use_response.json`:

```json
{
  "id": "msg_X",
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "Let me check."},
    {"type": "tool_use", "id": "toolu_bbb", "name": "get_weather", "input": {"city": "SF"}}
  ],
  "stop_reason": "tool_use",
  "usage": {"input_tokens": 10, "output_tokens": 5}
}
```

### New integration tests

`tests/integration/test_transcode_tools.py` — uses respx to mock
`https://api.minimax.io/anthropic/v1/messages`:

- **Round-trip non-streaming**:
  1. POST `/v1/chat/completions` with `tools` and `tool_choice`.
  2. Coordinator translates to Anthropic shape; respx sees a POST with
     `tools[].name == "get_weather"`, `tools[].input_schema`, and
     `tool_choice == {type: "tool", name: "get_weather"}`.
  3. respx returns an Anthropic response with a `tool_use` block.
  4. Client receives an OpenAI response with `tool_calls[0].function`
     populated, id format `call_<hex>`, arguments as JSON string,
     `finish_reason: "tool_calls"`.
  5. `transcode.loss_warnings` does **not** contain a `tools` /
     `tool_choice` / `messages[tool]` entry.
- **Anthropic client → OpenAI upstream** mirror test.
- **Multi-tool-call**: two tools in one assistant turn → two
  `tool_calls` on the response.
- **`role: "tool"` history round-trip**: the OpenAI client sends a
  tool result; the Anthropic upstream receives a `tool_result` content
  block with the correct `tool_use_id`.

`tests/integration/test_transcode_streaming_tools.py` — respx returns
an Anthropic SSE stream:

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_X","role":"assistant","model":"MiniMax-M3","content":[],"usage":{"input_tokens":10,"output_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_bbb","name":"get_weather","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"city\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\"SF\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}

event: message_stop
data: {"type":"message_stop"}
```

The integration test asserts the client receives an OpenAI SSE stream
in the shape:

```
data: {"id":"msg_X","object":"chat.completion.chunk","model":"MiniMax-M3","choices":[{"delta":{"role":"assistant"}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_<hex>","type":"function","function":{"name":"get_weather","arguments":""}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\":"}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"SF\"}"}}]}}]}

data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

(Captured by `httpx.AsyncClient`'s `aiter_bytes()` against the test
server, parsed back through `IncrementalSSEObserver` for shape
assertions.)

## Validation

After implementation:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_transcoder/ -v
uv run pytest tests/integration/test_transcode_tools.py -v
uv run pytest tests/integration/test_transcode_streaming_tools.py -v
uv run pytest tests/                                  # full suite
```

CI sets `PYTHONHASHSEED=0` and `TZ=UTC` (AGENTS.md); reproduce locally.

Acceptance criteria:

- The user's exact log line no longer fires for tool-using requests.
  The `transcode.loss_warnings` entry for the user's request shows
  zero `tools` / `tool_choice` / `messages[tool]` drops. The
  `transcoded_request` line still shows `loss_warnings=N` where N may
  include `top_p` / `presence_penalty` / etc. but never any tool
  fields.
- Every existing unit test that codifies tool-dropping is updated; no
  test asserts that tools are dropped silently any more.
- A live `eggpool` (not just a respx mock) against MiniMax
  International with `tools` returns a 200 with `tool_calls`
  populated. Verified manually per `deployment.md` "Verifying a fix"
  (curl + `--include` + `Authorization: Bearer $EGGPOOL_KEY`); not
  added to CI because MiniMax credentials are operator-specific.
- Streaming tools round-trip works byte-for-byte against a real
  MiniMax response. Same manual verification path.
- The dashboard `/runtime` Transcoding card shows `loss_warnings` for
  tool fields no longer contributing to the top-N.
- `transcoder.loss_warnings` count for the user's request drops from
  3 to 0 (for tool-only requests).

## Risk register

| Risk | Mitigation |
|---|---|
| Anthropic rejects translated `tool_choice` shape on a model that doesn't support it. | Catch upstream 400 with `invalid_request_error` and retry once with `tool_choice: {type: "auto"}`. Wire into the existing `_classify_upstream_error` retry path; no new retry tier. |
| Argument JSON round-trips lose precision on BigInt / large numbers. | Documented limitation: we JSON-parse and re-serialize, so numeric precision follows JSON's rules (no BigInt). Loss warning `precision_lost` only if the parsed number exceeds `2**53`. |
| `id_map` collides across concurrent requests. | Map is per-`TranscodeContext`, which is per-request (`ProxyRequestContext.transcode_context`). No sharing. |
| Streaming partial_json fails `json.loads()` at flush time. | Warning + raw string passthrough. Client can still attempt to parse the partial value. |
| Multiple tool_calls in one OpenAI delta (rare but allowed). | Slot allocation is keyed on `tool_calls[*].index`; multiple entries per delta are handled in a small inner loop. |
| Backwards compat: existing tests that expected drops now fail. | Update them in this plan. The list is small (4 tests; see "Update (replace) existing tests" above). |
| Operator turns off `[transcoder] enabled = false` (legacy escape hatch). | Today that escape hatch disables transcoding entirely. Behaviour unchanged. |

## Definition of done

- All "Files to modify" changes landed; all "Files to add" exist.
- All four "Update (replace) existing tests" tests rewritten; all new
  unit and integration tests pass.
- `docs/transcoding.md` updated with the new translation rows and
  lossy mappings.
- `CHANGELOG.md` entry under the next release: "Tool calling now
  translates between OpenAI and Anthropic protocols in both
  directions, including streaming."
- User's reported log line no longer appears for tool-using requests
  against MiniMax International.
- No regressions in `tests/unit/test_transcoder/` or
  `tests/integration/test_transcode_*` — full suite passes.
- Roadmap (`plans/00-roadmap.md`) updated: phase 6.1 marked complete.

## Follow-up work (not in this plan)

- Phase 6.5 adds the `pause_turn` sentinel surface in
  `OpenAIToAnthropicStreaming` and refines `tool_result_error_passthrough`
  into a structured `is_error` field on the response.
- Phase 6.5 also adds the `[transcoder.features] tools = false` opt-out
  (deferred from this plan; the first merge ships tools-on-only).
- Phase 6.2 (vision) extends `extract_tool_blocks` / `has_non_text_blocks`
  to handle `image` content blocks inside `tool_result` arrays
  (Anthropic supports this; OpenAI clients vary).
- `eggpool stats transcoding` CLI grows two new columns: `tool_calls`
  request count and `tool_arguments_bytes` histogram. Tracked separately.