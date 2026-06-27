# Unified Endpoint — Roadmap

## Why

Today EggPool rejects any request whose client-facing protocol does not match
the protocol of the selected upstream account
(`src/eggpool/request/coordinator.py:2053-2079` → `_validate_endpoint` raises
`ProtocolMismatchError`). This is hard-coded in the protocol-mismatch error
that the user hit:

```python
# src/eggpool/catalog/protocols.py:184-188
if model_protocol == "anthropic":
    msg = (
        f"Model {model_id!r} uses the Anthropic protocol. "
        "Use /v1/messages instead of /v1/chat/completions."
    )
```

The consequence is that **MiniMax International** (`api.minimax.io/anthropic`,
Anthropic-only) is unreachable from any OpenAI-compatible client (OpenCode,
Cursor, Aider, etc.) and **Claude Code** is unreachable from any
Anthropic-incompatible upstream. Operators have to pick one provider per
client type, which fragments the catalogue and breaks routing.

The fix is a **bidirectional protocol transcoder** that translates between
OpenAI Chat Completions and Anthropic Messages at the request, response, and
streaming layers. With it, the same upstream provider can serve both client
ecosystems, the existing `eggpool configsetup opencode` works against any
provider, and the catalogue collapses the protocol axis: a MiniMax-M2.7 model
served by an Anthropic-only upstream becomes accessible from OpenCode clients
without per-model protocol pinning.

## Scope and non-goals

In scope:

- Translating OpenAI Chat Completions ↔ Anthropic Messages for **text-only,
  non-streaming** requests and responses end to end. This is the v1 slice.
- Translating **streaming** text-only responses (SSE → SSE).
- Translating **usage / cost** fields so the existing `FinalizationData`
  pipeline continues to record accurate accounting.
- Translating **non-retryable error envelopes** (400/404 pass-through).
- Routing transparency: a client gets the right model from any provider
  that serves it, regardless of the client's protocol.
- A `[transcoder]` config section with operator controls.

Explicit non-goals for v1:

- Tool use / function calling translation. Deferred to a follow-up phase.
- Vision / image content translation. Deferred.
- Extended thinking / `reasoning_content` translation. Deferred.
- PDF / document content translation. Deferred.
- Audio input translation. Deferred.
- Web search / built-in tools. Deferred.
- Structured outputs (`response_format` / `json_schema`). Deferred.

These features are individually tractable but each adds 200–800 lines of
edge-case handling. We land text first, then layer the rest.

## High-level architecture

The transcoder is a **stateless layer** that sits between the existing
protocol-aware edges (endpoint entry, upstream dispatch, SSE framing) and the
protocol-agnostic core (catalog, routing, reservation, finalization). Three
insertion points only:

```
client request (OpenAI)
       │
       ▼
 POST /v1/chat/completions                  ← existing endpoint
       │
       ▼
 handle_proxy_request                       ← existing pipeline entry
       │
       ▼
 ProxyRequestContext                        ← add `upstream_protocol`
       │
       ▼
 RequestCoordinator.execute()               ← existing orchestrator
       │
       ▼
 _select_and_persist_attempt                ← existing routing
       │  (extended to consider transcodable accounts)
       ▼
 _execute_non_streaming / _execute_streaming
       │
       ├─→ request body: transcode if protocols differ
       ├─→ upstream URL:   compose with UPSTREAM protocol
       ├─→ headers:        compose with UPSTREAM static headers
       │
       ├─→ upstream response body (non-streaming):
       │     transcode upstream → client protocol if successful
       │     pass through if non-retryable error (after envelope re-render)
       │
       └─→ upstream SSE (streaming):
             observer parses UPSTREAM frames (existing)
             stream translator yields CLIENT SSE bytes
       │
       ▼
 PreparedProxyResponse                      ← unchanged shape
       │
       ▼
 render_proxy_response                      ← existing renderer
       │
       ▼
 client (OpenAI)
```

The transcoder is **per-request state**, not global. Each invocation has its
own:

- tool-call-id translation map (`call_…` ↔ `toolu_…`), if and when tools land.
- content-block-to-content-part index for ordering preservation.
- loss-of-information report (informational only; never fatal in v1).

## Design decisions

### 1. Where the protocol split lives

`ProxyRequestContext` already carries `protocol: str` for the client side.
We extend it with `upstream_protocol: str` and `transcode_required: bool`.

The protocol split is **resolved at attempt selection time**, not at endpoint
entry. This is because the upstream protocol depends on the selected account,
which depends on routing, which depends on the catalogue. Trying to compute
it eagerly in `handle_proxy_request` would force us to re-resolve on every
retry.

Practical consequence: `_get_upstream_url`, `_build_upstream_headers`,
`_execute_non_streaming`, `_execute_streaming`, `_extract_non_stream_usage`,
and `_build_stream_generator` all become
`upstream_protocol`-parameterized. Today they read `context.protocol`. The
change is mechanical but pervasive.

### 2. Routing: native vs transcodable eligibility

Today `account_supports_protocol(account_name, protocol)` (`accounts/registry.py:154`)
filters candidates against the **client** protocol. With transcoding enabled,
the rule relaxes to "account supports the **native model protocol**, OR an
account whose `provider.protocols` intersects both `{client, upstream}`."

We introduce `account_supports_protocol_any(account_name, *protocols)` and a
new selector parameter `transcode_eligibility: list[ProtocolName]` on the
router and eligibility functions. When transcoding is globally disabled
(default), the existing strict behaviour is preserved. When enabled, the
selector widens but routing still prefers native matches when available.

The catalogue still resolves each model's native protocol exactly as today.
The transcoder's job is to bridge, not to override.

### 3. Body translation: pure functions, no I/O

Every translator is a pure function `dict[str, Any] → dict[str, Any]` over
already-decoded JSON. We never transcode `bytes` directly; the existing
`encode_json_body` (`request/body.py:17`) handles the boundary. This makes
the translators trivially unit-testable and reentrant-safe under asyncio.

The contract for each translator:

```python
class BodyTranscoder(Protocol):
    def encode(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Translate client body → upstream body.
        Returns (new_payload, loss_warnings).
        """

    def decode(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Translate upstream body → client body."""
```

Two concrete implementations: `OpenAIToAnthropic` and `AnthropicToOpenAI`.
They share helper modules (`ids.py`, `usage.py`) but not state.

### 4. Streaming translation: pull-based, observer-decoupled

The existing `IncrementalSSEObserver` (`proxy/sse_observer.py:48`) already
parses SSE frames incrementally and exposes them as `SSEFrame` events. We
introduce a new `StreamingTranscoder` that:

1. Drives the upstream `aiter_bytes()` directly.
2. Feeds each chunk to an `IncrementalSSEObserver(upstream_protocol)`.
3. Translates the observed usage to the canonical `StreamUsageResult`.
4. Independently re-emits the **bytes the client should see**, frame by
   frame, in the **client** SSE format.

The existing `yield chunk` in `_build_stream_generator`
(`coordinator.py:1248`) becomes `async for out_chunk in
streaming_translator.feed(chunk): yield out_chunk`. The observer still
extracts usage; the transcoder handles byte emission. The two concerns are
cleanly separated.

The transcoder must be **backpressured** — it cannot buffer the entire
upstream response in memory. Each upstream frame produces one or more
client frames; frames are streamed out as soon as they are complete.

### 5. Error envelope translation

Three failure modes:

- **Pre-stream upstream error (status ≥ 400, body present)**: parsed,
  classified by the existing `_classify_upstream_error`
  (`coordinator.py:1572`), then re-rendered in the **client** envelope. The
  upstream error envelope is decoded in `upstream_protocol`, the client
  envelope is encoded in `client_protocol`.
- **Mid-stream SSE error event**: captured by the observer during the
  pre-DONE pass; re-rendered in client SSE format and forwarded as part of
  the stream. Client sees a single SSE error event before terminal close.
- **Post-stream disconnect**: existing behaviour is to yield whatever has
  accumulated and finalize. We preserve this and add a synthetic terminal
  chunk in client format if the upstream cut off mid-frame.

`src/eggpool/api/errors.py` already exposes `openai_error_response` and
`anthropic_error_response`; we reuse these for the envelope re-render. The
only new helper is one that parses an upstream error body into a structured
`UpstreamErrorPayload`.

### 6. Token estimation and limits

`src/eggpool/request/limits.py` already accepts a `protocol` argument for
`requested_output_tokens`. We make the preflight `_check_context_limits`
(`api/proxy_request.py:150`) run **twice** when transcoding is active: once
on the client payload (matches what the client asked for) and once on the
translated upstream payload (matches what the upstream will see). The more
restrictive of the two wins. This avoids the situation where a client asks
for 8k output tokens and the translated Anthropic payload with `max_tokens`
absent ends up with the upstream's default.

Cache-control hints are out of scope for v1 (no prompt caching translation).

### 7. Cost extraction

The existing `_extract_non_stream_usage` and `OpenAIStreamUsageExtractor` /
`AnthropicStreamUsageExtractor` parse the **upstream** body/stream. The
transcoder doesn't change what they see; usage extraction continues to be
done in upstream protocol terms. The finalizer (`finalizer.py`) and pricing
calculator are already protocol-agnostic, so no changes needed there.

### 8. Operator controls

```toml
[transcoder]
# Master switch. When false (default), every request must match its
# upstream protocol exactly — today's behaviour. When true, requests
# are transcoded if and only if the selected account's provider supports
# the upstream protocol natively but does not support the client
# protocol.
enabled = false

# How to handle loss-of-information during transcoding. v1 supports
# "warn" only (structured log per request). Future: "reject".
loss_policy = "warn"

# When true, prefer native-protocol accounts over transcodable ones
# during routing. When false, transcodable accounts may outrank
# native ones if their routing_priority is higher. Default true
# preserves existing behaviour for accounts that already match.
prefer_native = true
```

### 9. Testing strategy

We follow the project's existing three-tier test pattern:

- `tests/unit/test_transcoder/` — pure-function translator tests, one file
  per concern (messages, tools, vision, streaming, usage, errors, ids).
  Each test round-trips representative payloads and asserts loss-warnings.
- `tests/integration/test_transcode_*.py` — end-to-end through the
  existing `tests/integration/test_proxy_integration.py` fixture pattern.
  Uses `respx` to mock upstreams and asserts the full request/response
  shape including SSE bytes.
- `tests/contract/test_transcoder_contract.py` — fixture that boots a real
  test app and verifies contract-level invariants:
  - OpenAI client → Anthropic upstream: request shape matches, response
    shape is OpenAI, usage is recorded.
  - Anthropic client → OpenAI upstream: mirror.
  - Streaming round trip: bytes-emitted count matches expected chunk
    count; usage is finalized; `[DONE]`/terminal `message_stop` is
    honoured.
  - Error pass-through: 400 from upstream becomes an OpenAI/Anthropic
    error envelope depending on the client.

## Phase structure

The work is split into six phases. Each phase has its own plan file. The
phases are ordered so each one lands on top of the previous with a
mergeable, testable PR.

| # | Phase | Plan file | Deliverable |
|---|---|---|---|
| 1 | Foundation | `phase-1-foundation.md` | `TranscoderPolicy` config, `upstream_protocol` field on `ProxyRequestContext`, selector parameter on routing, helper modules (`ids.py`, `usage.py`, `errors.py`) with unit tests. No behavioural change yet. |
| 2 | Body translation | `phase-2-body-translation.md` | `OpenAIToAnthropic` and `AnthropicToOpenAI` body translators for text-only requests/responses, with full unit test coverage. Coordinator wired to call them when `transcode_required`. Error envelope translation for non-streaming pass-through. |
| 3 | Streaming translation | `phase-3-streaming-translation.md` | `StreamingTranscoder` for both directions, fed by the existing observer, with backpressure and frame-level correctness tests. Replaces `yield chunk` in `_build_stream_generator`. |
| 4 | Routing and eligibility | `phase-4-routing-eligibility.md` | Widened account selector, native-preference ordering, integration tests proving models reach transcodable accounts. |
| 5 | Operator controls and docs | `phase-5-operator-controls-docs.md` | `[transcoder]` config section, `eggpool configsetup opencode` annotations, README updates, default-off rollout, structured logging. |
| 6 | Deferred features | `phase-6-deferred-features.md` | Tools, vision, thinking, structured outputs. Each is its own sub-phase gated on operator opt-in. |

The roadmap file (this file) is the index. Each phase file is self-contained
with its own validation steps and acceptance criteria.

## Risk register

| Risk | Mitigation |
|---|---|
| Translation bugs leak token-billing differences. | v1 uses upstream-protocol usage extraction; finalizer stores upstream-reported values verbatim. Locally derived cost applies only as a fallback. |
| Mid-stream SSE translation stalls the event loop. | Streaming transcoder uses `asyncio.Queue` with a bounded buffer and explicit backpressure tests. |
| Tool-call id collisions across requests. | `ids.py` keys its map on `request_id` plus a per-call UUID, scoped to the request lifetime only. |
| Hidden provider-specific quirks (e.g. MiniMax strips `anthropic-version`). | Provider contract already exposes `headers`; transcoder leaves existing static headers untouched and adds protocol-required ones only. |
| Operators enable transcoding globally and lose native-protocol debugging visibility. | `prefer_native = true` default; structured logs include `client_protocol`, `upstream_protocol`, `native_match` for every transcoded attempt. |
| SSE re-emission changes byte counts observed by clients. | Contract tests assert exact byte counts on representative fixtures; streaming shield continues to wrap finalizer. |

## Definition of done (roadmap)

The roadmap is complete when:

1. All six phase files exist in `plans/` and reference each other.
2. The architecture invariants in `architecture/README.md` still hold
   (single-connection serialization, `db.transaction()` for DML, etc.).
3. No existing test changes behaviour; new tests live alongside.
4. `[transcoder]` config is documented in `config.example.toml`.
5. README points operators at the new section.
6. CHANGELOG mentions the feature as opt-in.

No code is written for the roadmap itself — implementation begins once the
phase plans are reviewed and the first phase PR is opened.