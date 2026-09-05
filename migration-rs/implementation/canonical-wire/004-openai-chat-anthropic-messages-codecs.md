# W004 — OpenAI Chat Completions and Anthropic Messages Codecs

Status: closed; see [closure record](../../closure/canonical-wire/004-status.md)

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w004--openai-chat-completions-and-anthropic-messages-codecs`

Primary class: capability

Hard dependency: W003 accepted closure.

## 1. Objective

Implement finite request/response codecs for OpenAI Chat Completions and Anthropic Messages against the W002 canonical IR and W003 codec contract. Preserve current native and cross-wire semantic behavior without provider HTTP, retry, or streaming lifecycle ownership.

## 2. Python oracle

Use W001 observations plus the current OpenAI-chat/Anthropic codec and transcoder modules. Behavioral parity is authoritative; do not reproduce Python class/file structure where one canonical Rust transformation is simpler.

## 3. OpenAI Chat request decode/encode

Cover at minimum:

- model, streaming intent, generation controls represented in canonical IR;
- system/developer/user/assistant/tool roles;
- string and multipart content;
- tool definitions/function schema;
- assistant tool calls and tool results;
- tool choice and parallel-tool intent;
- reasoning controls/content currently supported on this surface;
- response-format/structured-output intent;
- media/document placeholders handled by W007;
- cache/provider extensions handled by W007/W006 where applicable.

Preserve omission/null distinctions frozen by W001. Reject malformed role/content/tool structures with W003 typed errors.

## 4. Anthropic Messages request decode/encode

Cover:

- top-level system semantics and ordered messages;
- text/content blocks;
- tool-use/tool-result blocks and IDs;
- stop/generation controls;
- thinking controls/content where supported;
- response-format semantics only where representable under the frozen compatibility policy;
- W007 media/document/cache hooks.

Do not flatten Anthropic content blocks to plain text if doing so loses tool/reasoning/media identity.

## 5. Cross-wire conversion

Using canonical IR as the only semantic bridge, support the currently promised Chat <-> Anthropic conversions. Required rules:

- system/developer placement follows Python compatibility behavior;
- tool call/result linkage survives conversion;
- unsupported role/content constructs produce typed warning/loss/rejection per W001/W006 contract;
- native profile encode does not incur cross-wire warnings merely because canonicalization occurred;
- model identity is caller/routing controlled, not rewritten opportunistically by the codec.

W006 will centralize complex reasoning/tool/loss policy. W004 may implement the basic hooks and native representation required by W006, but must not invent duplicate policy engines.

## 6. Finite response decode/encode

Map both families to canonical responses and back, including:

- text/content blocks;
- tool calls;
- reasoning content where present;
- finish/stop reason semantics;
- response/model IDs that are safe and meaningful to preserve;
- usage through the canonical usage structure;
- explicit provider error payloads as structured provider-error evidence, not codec failures when the payload shape itself is valid.

Unknown/malformed success shapes must fail deterministically rather than producing an empty successful response.

## 7. JSON and extension policy

Use `serde_json::Value` only at the boundary where schemas are truly open. Prefer typed internal structures for known fields. Unknown provider/client fields follow W001's preserve/drop/reject policy; do not copy arbitrary input objects into output by default.

## 8. No streaming implementation yet

W004 may define event conversion helpers required by W008, but it must not implement network reads, SSE buffering, response handoff, stream timeout, retry, or completion ownership. W008 owns integrated stream adapters.

## 9. Required differential tests

Cover at least:

1. minimal native Chat request/response;
2. minimal native Anthropic request/response;
3. system/developer semantics;
4. multipart content;
5. tool definitions, multiple tool calls, tool results, IDs, tool choice;
6. reasoning control/content representative fixtures;
7. structured-output representative fixtures;
8. omission/null/zero/empty cases;
9. Chat -> Anthropic and Anthropic -> Chat semantic observations;
10. finish/stop reason mappings;
11. usage normalization hooks;
12. valid provider error object vs malformed response distinction;
13. malformed roles/content/tool objects;
14. no raw body/credential leakage in errors/debug output.

W006/W007 may extend the feature-depth matrix, but W004 cannot close if basic tool/reasoning constructs cannot be represented without opaque raw JSON escape hatches.

## 10. Resource/security posture

No new dependencies should be required. Avoid cloning whole JSON trees when canonical typed data can be moved/borrowed. Enforce W002 limits before constructing large vectors/strings. No auth/header/proxy data appears in codec types.

## 11. Verification

Run full Rust tests plus W001 migration observations and targeted Python Chat/Anthropic codec/transcoder tests. Record exact commands in closure. No live provider call.

## 12. Acceptance criteria

W004 closes only if:

- native finite Chat and Anthropic request/response observations match Python semantically;
- basic bidirectional cross-wire conversions use canonical IR and preserve tool/content identities;
- malformed/provider-error distinctions are explicit;
- no network/retry/stream lifecycle behavior is added;
- W005 can implement the remaining families against the same codec contract without changing W004's public interface materially.

## 13. Stop conditions

Do not close if conversion relies on round-tripping through the other provider's raw JSON, tool IDs are silently lost, malformed provider responses become empty success, or the implementation introduces a second transformation architecture parallel to canonical IR.

## 14. Closure evidence

Create `migration-rs/closure/canonical-wire/004-status.md` with native/cross-wire fixture matrix, unresolved supported differences, verification, and registry transition promoting W005 under the default serial handoff.
