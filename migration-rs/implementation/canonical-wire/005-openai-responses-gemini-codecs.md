# W005 — OpenAI Responses and Gemini generateContent Codecs

Status: planned; blocked on W004 closure by default serial handoff

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w005--openai-responses-and-gemini-generatecontent-codecs`

Primary class: capability

Hard dependencies: W003 closure; W004 closure is the default serial promotion gate so the shared codec contract is exercised once before the second family pair lands.

## 1. Objective

Implement finite request/response codecs for OpenAI Responses and Gemini generateContent against the same W002 canonical IR and W003 codec contract used by W004. Preserve surface-specific content/output/tool/reasoning/usage semantics while avoiding a parallel IR or provider-specific coordinator logic.

## 2. Python oracle

Use W001 observations plus current OpenAI Responses and Gemini wire codec/transcoder behavior. `_wire_profiles.toml` remains the static profile source. Dynamic wire resolver/retry behavior is not part of this plan.

## 3. OpenAI Responses request decode/encode

Cover current supported semantics for:

- model and streaming intent;
- input strings/items/message content;
- system/developer/user/assistant/tool semantics;
- response output/tool/function-call structures represented on input;
- tools/tool choice and function schemas;
- reasoning effort/summary/content controls and source intent;
- response-format/text-format/structured-output constraints;
- media/document inputs delegated to W007;
- generation controls and fields intentionally preserved by EggPool.

Do not assume Chat Completions field names are interchangeable with Responses. Canonicalize semantics, not syntax.

## 4. Gemini generateContent request decode/encode

Cover:

- `contents` role/parts structure;
- system instruction semantics;
- text and function-call/function-response parts;
- tool declarations/configuration;
- generation configuration and stop/finish controls;
- thinking/reasoning configuration represented by the current provider contract;
- response schema/structured output where supported;
- media/file/document parts delegated to W007;
- provider-specific field naming without leaking provider syntax into canonical core types unnecessarily.

Malformed role/part/function structures must fail typed validation.

## 5. Cross-wire semantic conversion

Support conversions between these surfaces and the canonical semantics already exercised by W004. The implementation should make all four families mutually reachable where Python currently promises compatibility, subject to explicit W006/W007 loss policy.

Requirements:

- OpenAI Responses output-item semantics must not be flattened in a way that loses function/reasoning identity;
- Gemini parts/function calls/responses must preserve linkage and ordering;
- system/developer differences must be surfaced as adaptation policy rather than silently merged when materially significant;
- canonical model identity remains routing-owned;
- unsupported surface-specific controls produce warning/rejection metadata rather than disappearing.

## 6. Finite response decode/encode

For both families map:

- text/content output;
- function/tool calls and results where applicable;
- reasoning/thinking content/metadata;
- response IDs/model identity safe to forward;
- finish/stop reason;
- safety/block/error outcomes where current EggPool treats them as provider evidence;
- usage/cache counters through canonical usage;
- valid provider error payload vs malformed success/error shape.

Do not treat a Gemini blocked/error response as successful empty text if Python surfaces a terminal/provider error class.

## 7. Shared canonical architecture

W005 must reuse W002/W003/W004 infrastructure. It may extend canonical enums/fields only when W001 demonstrates a semantic concept that cannot otherwise be represented. Any such extension must remain provider-neutral enough for W006-W009.

Do not create `GeminiCanonicalRequest` or `ResponsesCanonicalRequest` as parallel core IRs merely to reduce implementation effort.

## 8. Streaming boundary

As in W004, finite event-shape helpers may be added, but incremental SSE/event-stream framing and completion evidence remain W008. No network reads, response handoff, retries, or timeouts.

## 9. Required differential tests

Cover at minimum:

1. minimal native OpenAI Responses request/response;
2. minimal native Gemini request/response;
3. multi-part content/output items;
4. system/developer/system-instruction mapping;
5. function/tool declarations, calls, responses, IDs;
6. reasoning/thinking controls/content;
7. structured output/schema controls;
8. finish/stop/block/safety outcomes represented by current contract;
9. usage/cache counters;
10. Responses/Gemini -> canonical -> Chat/Anthropic representative conversions and reverse directions;
11. omitted/null/empty/zero semantics;
12. valid provider error vs malformed payload;
13. malformed content/part/function shapes;
14. no raw provider/client body leakage in errors.

## 10. Resource/security posture

Reuse existing dependencies. Keep transformations synchronous/pure. Apply W002 allocation/size bounds before expanding parts or schemas. No provider credentials, headers, or network clients in codec types.

## 11. Verification

Run Rust all-target tests, W001 migration observations, targeted Python Responses/Gemini codec/transcoder tests, formatting/lint/type checks, and `git diff --check`. No live provider inference.

## 12. Acceptance criteria

W005 closes only if:

- native finite Responses and Gemini observations match Python semantically;
- all four supported families now share one canonical codec architecture;
- representative cross-family conversions preserve core content/tool/reasoning identity or return explicit adaptation outcomes;
- malformed/block/provider-error states are not converted into false success;
- W006 can centralize advanced semantic policy without rewriting family codecs.

## 13. Stop conditions

Do not close if provider-family syntax leaks into routing/coordinator state, a second canonical IR is introduced, function-call linkage is silently lost, Gemini block/error outcomes become empty success, or dynamic wire retry/HTTP behavior enters the codec.

## 14. Closure evidence

Create `migration-rs/closure/canonical-wire/005-status.md` with profile/surface fixture coverage, four-family shared-contract review, verification, and registry transition promoting W006.
