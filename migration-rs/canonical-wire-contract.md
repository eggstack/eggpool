# M6 Canonical Wire Contract

Status: frozen by W001; see [W001 closure evidence](closure/canonical-wire/001-status.md)

This document is the semantic handoff between the live Python implementation
and the later Rust canonical request/wire work. The authoritative oracle is
the bounded observation harness in `tests/migration_rs/canonical_wire_fixtures.py`
and its committed inputs under `fixtures/canonical-wire/`. Fixtures are
synthetic and never contain credentials, proxy credentials, session headers,
or arbitrary raw user bodies.

## Scope and surface vocabulary

The public client surfaces currently exposed by the canonical runtime are:

| Client surface | Canonical name | Native upstream profile |
|---|---|---|
| OpenAI Chat Completions | `chat_completions` | `openai_chat_completions` |
| OpenAI Responses | `responses` | `openai_responses` |
| Anthropic Messages | `messages` | `anthropic_messages` |

The static upstream profile registry contains five accepted identities:
`openai_chat_completions`, `openai_responses`, `anthropic_messages`,
`gemini_interactions`, and `gemini_generate_content`. Gemini generateContent
is a required wire-family representative even though it is not a separate
public EggPool client endpoint; Gemini Interactions is retained because it is
also a built-in production profile. Profile-to-codec mappings are frozen in
[`w001-fixture-matrix.json`](fixtures/canonical-wire/w001-fixture-matrix.json)
and must be read from `_wire_profiles.toml`, not inferred from provider names.

## Canonical ownership

The original client request is the canonical source of intent. Every alternate
target is encoded from that source IR; a previously translated provider body
must never become the source for a second translation. The IR retains ordered
roles and blocks, tool IDs/linkage, media source form, reasoning mode/effort/
budget/disable state, structured-output intent, streaming intent, and portable
generation controls. Unknown fields are not retained wholesale to avoid
silently expanding the contract; codecs either preserve an explicitly listed
portable extension or report a typed loss/warning/rejection.

M5 DTOs remain owned by M5. W002 may provide pure adapters from admitted IR to
request-independent routing and affinity facts, but no M6 observation or codec
may select an account, mutate claims/health/quota, call the catalog, or invoke
semantic model-router inference.

## Parity rules

The fixture oracle uses the following comparison classes:

| Area | Frozen rule |
|---|---|
| JSON object ordering | Semantic observations compare mappings independent of insertion order. When client/provider bytes are an exposed native path, preserve the existing compact UTF-8 bytes and field order; do not sort keys as a compatibility shortcut. |
| Arrays and event order | Exact order is semantic for messages, content blocks, tools, output blocks, SSE frames, and canonical events. |
| Numbers | Preserve integer versus floating intent where the IR exposes it. Compact JSON uses the active `jsonx` envelope: non-finite values are rejected or represented as `null` by the established backend, never emitted as invalid JSON. |
| Omitted/null/false/zero | Missing, `null`, `false`, and numeric zero remain distinct whenever production behavior distinguishes them. In particular, usage `None` means unreported while `0` means reported zero. Optional request controls must not be synthesized merely because a target has a default. |
| Unicode/escaping | UTF-8 content is semantic text. Escaping differences are not normalized when comparing exposed bytes; semantic comparisons decode JSON and retain code points. |
| Warnings and losses | Preserve source order and affected field/category. Do not compare only rendered prose. |
| Errors | Stable class/reason and affected field are exact contract data. Existing prose may be semantically normalized only where an inbound HTTP layer already permits it. |

Python currently has intentional compatibility quirks that are fixtures, not
Rust design invitations: OpenAI missing/zero totals can be reconstructed from
prompt plus completion counts; Anthropic total usage includes reported cache
read and creation counts; Anthropic stream cache creation is also exposed as
the legacy write counter; and the current canonical stream adapter can emit a
surface terminal event more than once when both a finish event and native stop
marker are present. These behaviors remain visible in the snapshot until a
later accepted plan changes them with new evidence.

## Typed outcome taxonomy

Fixture-level reason codes distinguish:

- `exact_native` — native/pass-through or lossless canonical conversion;
- `compatible_warning` — conversion is usable and carries a warning;
- `approved_semantic_loss` — configured policy permits a known loss;
- `unsupported_loss_rejected` — required semantics cannot be represented under
  the active loss policy;
- `malformed_client_request` and `malformed_provider_response`;
- `request_limit`, `media_limit`, `document_limit`, and `context_limit`;
- `unsupported_wire_profile`; and
- `incomplete_stream_terminal_evidence`.

The existing `LOSS_WARNING_KINDS` vocabulary remains the source for concrete
loss records, including tool, media/document, reasoning, structured-output,
cache-control, and provider-extension categories. Provider error evidence is
not a codec parse error: a valid provider error envelope is decoded as typed
upstream evidence with status, type, and bounded message metadata.

## Request and resource contract

The request oracle covers minimal, rich, presence-sensitive, Unicode, invalid,
and boundary requests. It includes system/developer/user/assistant/tool roles;
content-part arrays; tools, choices, parallel-call intent, tool results and
IDs; reasoning controls and history; structured-output schemas; image/document
URL and inline forms; cache/provider extensions; stream metadata; malformed
JSON/top-level/model/content shapes; and request/context sizing.

Externally meaningful ceilings are:

| Resource | Limit/source |
|---|---|
| Raw request body | 10 MiB default `server.max_request_body_bytes`; reject before unbounded parse/read. |
| SSE frame/carry buffer | 64 KiB (`proxy.sse.MAX_SSE_FRAME_BYTES`); oversized frames are discarded and reported. |
| Speculative reservation estimate | 128,000 estimated input tokens; the estimate is bounded and is not billing truth. |
| Context estimate | Decoded-value estimate plus byte floor, minimum 1,000 tokens; catalog-enforced context/input/output limits remain provider/model facts. |
| Automatic affinity prefix | 4,096 UTF-8 bytes; explicit route-session header 512 bytes. These are M5-owned inputs and are never copied into M6 observations. |

Body sizing uses the bounded `request.body` and `request.limits` behavior.
Base64/data-URI sizes are measured without decoding unbounded input. Integer
arithmetic is checked/saturating at the existing Python boundary. Estimates
must not be presented as provider billing usage.

## Response and usage contract

Finite observations cover text, multiple output blocks, reasoning, tool calls,
finish/stop causes, structured output, provider errors, malformed shapes, and
missing/unknown usage. Canonical usage vocabulary is:

`input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`,
`cache_write_input_tokens`, `reasoning_tokens`, and
`cache_counter_status` (`reported`, `not_reported`, `unknown_format`).

`None` is not zero. OpenAI cache reads come from the recognized nested/legacy
cached-token fields and do not become cache writes. Anthropic cache reads and
creation are retained separately and their canonical cached total is their
sum. Missing usage or an unrecognized usage shape is `unknown_format` for the
non-stream normalizer; a successfully parsed usage object without cache fields
is `not_reported`. Streaming merged usage retains these distinctions and a
missing final usage event remains missing.

## SSE and terminal evidence

SSE framing is incremental, UTF-8 aware, bounded, and chunk-independent. It
accepts LF and CRLF, `event`, `id`, comments, ignored fields, multiline data,
blank-line termination, empty/comment-only records, malformed JSON payloads,
and an unterminated final line. Small fixtures run representative splits and
an all-single-byte split; the semantic frame/event sequence must be identical.

Terminal evidence is typed and never synthesized merely because transport EOF
occurred:

| Wire family | Successful/terminal evidence |
|---|---|
| OpenAI Chat | `[DONE]` → `openai_done` |
| OpenAI Responses | `response.completed`, `response.incomplete`, `response.failed` → `responses_completed`, `responses_incomplete`, `responses_failed` |
| Anthropic | `message_stop` → `anthropic_message_stop` |
| Gemini Interactions | completed/requires-action or other interaction status → `gemini_completed`/`gemini_incomplete` |
| Gemini generateContent | `finishReason=STOP` or another finish reason → `gemini_completed`/`gemini_incomplete` |

Clean EOF after evidence is complete. EOF before evidence is an incomplete
stream outcome, even if payload bytes were observed. Oversized carry state is
discarded with bounded diagnostics; it is never promoted to a valid terminal
event.

## M5 DTO bridge

W002 owns the pure bridge from admitted canonical request plus caller-supplied
static feasibility facts to M5 `RoutingRequestFacts`: model identity, client
surface/protocol, supplied upstream protocol facts, projected tokens,
canonical thinking requirement, and explicit freshness/time facts. It also
produces the D007 affinity input from bounded explicit identity or the
system/developer plus first-user prefix rules. The bridge performs no catalog
or database access and no state mutation.

## M7 boundary

The following are explicitly outside W001/M6 and must not be smuggled into a
codec or fixture oracle: DB-backed or learned wire preference, rejected-wire
candidate state, alternate-wire retry, provider HTTP submission or auth
headers, downstream response-start ownership, timeout/cancellation outcome
policy, retry classification/failure effects, durable attempt persistence, and
finalization. M6 accepts finite bytes and returns bounded semantic/encoded
results. M7 owns mutable attempt and lifecycle policy around that boundary.

## Fixture artifacts

- [`w001-fixture-matrix.json`](fixtures/canonical-wire/w001-fixture-matrix.json)
  — scope, surfaces, profile identities, stable reason codes, limits, and M7
  exclusions;
- [`w001-sse-fixture-inventory.json`](fixtures/canonical-wire/w001-sse-fixture-inventory.json)
  — byte-fixture grammar and terminal coverage;
- [`w001-python-observations.json`](fixtures/canonical-wire/w001-python-observations.json)
  — committed semantic projection used by the repeatability tests.

The complete richer observation is generated by the Python harness at test
time. No live provider call, network negotiation, credential, timestamp, UUID,
process ID, or raw personal content is needed.
