# Plan 133 — Phase 2: Multimodal Capability Model and Narrow Content IR

## Status

Complete. Content IR types and MultimodalCapabilities are implemented. Decode/encode helpers remain for Plan 134.

## Objective

Create the smallest shared abstraction needed to translate multimodal message content without growing pairwise protocol-specific branches indefinitely.

## Problem

Current OpenAI ↔ Anthropic translators directly inspect protocol-specific content blocks. Images are partly supported, PDFs are asymmetric, audio/file blocks are dropped in some directions, and non-text tool-result media is flattened. A third protocol surface would otherwise multiply pairwise logic.

## Design constraint

Do **not** canonicalize an entire OpenAI or Anthropic request. Sampling, reasoning controls, caching, tool-choice semantics, structured output, and provider extensions remain in their existing protocol-specific paths.

The IR covers content blocks only.

## Proposed content IR

Implement ordinary dataclasses/typed structures, not a framework. Required concepts:

- `TextContent(text)`;
- `ImageContent(source, media_type, detail?)` where source is URL, base64, or provider/file identifier when representable;
- `DocumentContent(source, media_type, filename?)`;
- `AudioContent(source, media_type/format?)` only if a current supported protocol can preserve it;
- tool-use block;
- tool-result block whose content can itself contain supported content blocks;
- thinking/reasoning/refusal only where needed to preserve ordering with other blocks.

Do not introduce a generic arbitrary EAV content node.

## Capability model

Replace reliance on coarse `supports_vision` for transcoding decisions with additive granular facts. Keep backward compatibility while migrating callers.

Candidate capability dimensions:

- image input: base64, URL, file/provider reference;
- document/PDF input: base64, URL, file/provider reference;
- audio input where applicable;
- non-text media inside tool results;
- maximum source/attachment bytes when provider docs define them;
- maximum serialized request bytes when provider docs define them.

Capabilities are provider/model/protocol scoped. Unknown remains unknown and must never authorize a field.

## Work items

1. Define content IR types in a narrowly named transcoder module.
2. Implement OpenAI content decode helpers to IR.
3. Implement Anthropic content decode helpers to IR.
4. Implement protocol encoding helpers from IR.
5. Preserve source strings without unnecessary decoded copies. Base64 validation should continue using encoded-length rejection before decode where possible.
6. Convert existing image/PDF helpers incrementally; do not rewrite unrelated text/tool/reasoning logic in the same change.
7. Extend capability serialization/config override types with granular modality/source support while retaining compatibility with existing catalog rows.
8. Add bounded loss reasons for unsupported source form vs unsupported modality so operators can distinguish them.

## Memory/resource constraints

- Do not decode and retain base64 media merely to normalize it.
- Preserve original encoded strings when forwarding.
- Avoid deep-copying whole request graphs.
- IR instances should only be created for content blocks participating in translation; same-protocol passthrough must not canonicalize content.

## Tests

Extend existing transcoder contract/unit suites with fixture-driven cases covering text+image, image URL, image base64, PDF base64, tool-result media, malformed base64, and unknown capability behavior. Tests must verify no unsupported target field is emitted when capability is unknown.

Do not create a new multimodal CI workflow or golden-file framework.

## Acceptance criteria

- There is one narrow content-block representation shared by both protocol translators.
- Same-protocol requests still bypass the IR.
- Existing text/tool/thinking behavior remains unchanged.
- Capability checks can distinguish at least image URL vs image base64 and PDF URL vs PDF base64.
- Unknown capabilities do not emit native multimodal fields.
- Tool-result content can represent nested media without flattening at the IR boundary.
- Base64 handling does not introduce an extra long-lived decoded copy.
- No new runtime dependency.

## Out of scope

Responses API, image generation, audio transcription endpoints, embeddings, media fetching/downloading by EggPool, and provider-side file upload APIs unless later explicitly scoped.
