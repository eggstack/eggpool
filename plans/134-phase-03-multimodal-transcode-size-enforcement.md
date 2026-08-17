# Plan 134 — Phase 3: Multimodal Transcoding and Upstream Size Enforcement

## Objective

Use the Phase 2 content IR and capability model to make OpenAI Chat Completions ↔ Anthropic Messages transcoding multimodal where semantics are representable, while rejecting or warning explicitly when they are not.

## Prerequisite

Plan 133 must be complete. Do not implement another independent branch-heavy translation layer.

## Work items

### 1. Image parity

Preserve OpenAI image URL/base64 ↔ Anthropic image URL/base64 only when the selected provider/model/protocol contract supports the corresponding source form. Prefer native protocol routing when it preserves more semantics than transcoding.

### 2. PDF/document translation

Support representable PDF/document mappings in both directions. Distinguish:

- PDF base64 supported by target;
- PDF URL supported by target;
- provider/file reference supported by target;
- unsupported document media type.

Do not fetch a remote document merely to convert URL to base64. Loss policy should handle non-representable forms.

### 3. Tool-result media

Stop automatically flattening image/document tool-result content to text when the target protocol supports media-bearing tool results. Preserve ordering and tool-call ID mapping.

If the target cannot represent the media, emit a bounded structured loss warning; obey existing warn/reject policy.

### 4. Audio/file behavior

Where OpenAI Chat or the selected compatible runtime supports an input form that Anthropic Messages cannot represent, keep explicit loss behavior. Do not invent a fake mapping. The code should distinguish `unsupported_modality` from `unsupported_source_form`.

### 5. Serialized upstream request-size validation

Add provider/protocol capability/config for maximum serialized upstream request bytes when known. Validate the actual encoded provider-bound body before dispatch.

This specifically fixes the current conceptual mismatch where decoded PDF bytes can be under a provider's nominal attachment limit while base64 expansion and the rest of the JSON make the final HTTP body exceed the provider request limit.

Requirements:

- validate after final provider-specific translation/preparation and before durable/upstream dispatch at the narrowest safe boundary;
- use already encoded bytes when available; do not encode twice solely to measure;
- return a local capability/request-too-large error, not a provider retry;
- never quarantine/back off an account because EggPool locally detected its own oversized payload;
- preserve the global inbound request-body limit as a separate concern.

### 6. Streaming

Streaming response translation must preserve existing ordering and usage/finalization behavior. Multimodal request support should not add per-frame async work to the SSE hot path.

## Provider-specific validation

At minimum validate fixtures/contracts for direct Anthropic plus local providers introduced in Plan 132 whose compatibility surfaces differ in source-form support. Ollama is a required case because its OpenAI and Anthropic compatibility surfaces may not expose identical multimodal semantics.

## Tests

Add focused cases to existing transcoder/proxy contract suites:

- OpenAI image URL → Anthropic URL when supported;
- URL rejected/warned when only base64 is supported;
- base64 image in both directions;
- PDF base64 in both directions where target permits it;
- PDF URL preserved where target permits it and explicitly lost otherwise;
- media-bearing tool results preserved when supported;
- oversized final serialized request rejected locally;
- base64 payload below decoded-file limit but above serialized request limit is rejected;
- local preparation failure does not produce provider retry/backoff;
- same-protocol multimodal passthrough remains untouched.

## Acceptance criteria

- Supported images/documents/tool-result media survive OpenAI ↔ Anthropic translation without unnecessary flattening.
- Unsupported modalities/source forms produce explicit bounded loss metadata and obey the existing loss policy.
- EggPool never fetches external media to make a translation possible.
- Final outgoing body size is checked against selected provider/protocol limits using encoded bytes.
- Local size/transcode errors cannot suppress a provider account.
- Streaming hot-path structure remains unchanged except where required for response semantics.
- No new mandatory dependency or CI workflow.
