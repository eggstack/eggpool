# W007 — Multimodal, Documents, Cache Controls, and Provider Adaptation

Status: dependency-ready; W006 closure accepted

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w007--multimodal-documents-cache-controls-and-provider-sensitive-pure-adaptation`

Primary class: capability/invariant

Hard dependency: W006 accepted closure.

## 1. Objective

Port the remaining pure request/response adaptation semantics for images, documents, other supported media, cache-control/body markers, and provider-sensitive body shaping. Preserve strict resource bounds and explicit loss behavior without introducing fetches, uploads, auth headers, or provider HTTP.

## 2. Python oracle

Use W001 observations plus current multimodal/document/cache helpers in the wire codecs/transcoder layer and `request.limits`. The contract is the supported transformation behavior, not every legacy helper function.

## 3. Canonical media/document model

Use/extend W002 canonical content types to represent only supported forms, including as applicable:

- external URL/reference;
- data URI/inline base64 payload;
- MIME/media type;
- image detail/quality hint where supported;
- document/file content and media type;
- provider file/reference identity only when current EggPool can carry it without performing a new upload/fetch;
- text extracted/provided directly by the client where it is semantically a document part.

Keep raw media bytes out of `Debug`, errors, traces, and routing state.

## 4. Resource bounds

Enforce the W001/W002 contract for maximum media/document count, per-item/aggregate byte estimates, bounded data-URI/base64 validation, MIME/type sanity, URL/reference length, and provider extension metadata. Use checked/saturating arithmetic. Reject malformed or impossible encoded sizes before unbounded decode/copy.

## 5. Cross-wire media mapping

For every supported source/target pair preserve URL-vs-inline semantics where possible, media type, and supported image hints. Apply W006 warning/rejection policy for unsupported forms. Never silently replace unsupported media/documents with text placeholders unless the frozen Python policy explicitly does so.

Do not fetch remote URLs or upload files in M6.

## 6. Documents

Keep document behavior distinct where needed: supported MIME/types, inline/reference forms, count/size limits, unsupported-target handling, and response-side file/document parts that EggPool actually exposes. No OCR, extraction, filesystem read, archive processing, remote fetch, or provider file API.

## 7. Cache controls and provider body markers

Port pure payload semantics for cache markers/breakpoints/ephemeral flags or equivalent body fields currently translated by EggPool. Choose behavior from explicit provider/profile context; preserve semantic placement; use W006 outcomes when unsupported; do not mutate usage/accounting state. Cache-token usage remains W008.

## 8. Provider-sensitive pure adaptation

Represent stable provider body quirks as small typed/static flags from W003 profile metadata when practical. Do not create a provider plugin framework or configuration DSL, and do not key semantic behavior on account name, API-key shape, or runtime error history.

## 9. Response adaptation

Map supported media/document/reference response parts to canonical content and client profiles under W006 policy. Unsupported provider-only/binary artifacts must not become misleading text success.

## 10. Security requirements

- never dereference/fetch media URLs;
- never log inline base64/data URI contents;
- never include filesystem paths or credentials in adaptation output;
- reject oversized/control-filled metadata;
- no decompression/archive processing;
- no SSRF-capable path;
- no unsafe code;
- no media-processing dependency unless W001 proves one mandatory.

## 11. Required differential tests

Cover URL and inline images, malformed/oversized base64/data URI, aggregate media limits, image hints, supported documents/MIME types, unsupported-target warnings/rejections, pass-through file/reference forms, cache-marker placement, unsupported cache controls, finite response media conversion, redaction with synthetic sentinels, and proof that no network/filesystem operation occurs.

Existing W002 limit and W006 loss suites must remain green.

## 12. Resource posture

Keep large payload data borrowed/shared where practical. Avoid decode/re-encode unless target syntax requires it and bounds are already satisfied. No spawned tasks/background work.

## 13. Verification

Run all Rust tests, W001 migration observations, targeted Python multimodal/document/cache/transcoder tests, formatting/lint/type checks, and `git diff --check`. No live provider/file calls.

## 14. Acceptance criteria

W007 closes only if supported media/document/cache semantics match Python, all size/count bounds precede expensive operations, unsupported transformations use W006 outcomes, media data cannot leak through diagnostics, no fetch/upload/filesystem behavior exists, and W008 can focus only on streaming/event/usage behavior.

## 15. Stop conditions

Do not close if a URL is dereferenced, arbitrary base64 is decoded before bounds, unsupported documents are silently textified, cache behavior depends on runtime/account history, or large inline data can appear in diagnostics.

## 16. Closure evidence

Create `migration-rs/closure/canonical-wire/007-status.md` with fixture matrix, limit/redaction evidence, dependency review, verification, and registry transition promoting W008.
