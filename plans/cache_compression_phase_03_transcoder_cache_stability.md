# Phase 3 Plan: Transcoder Cache Stability and Boundary Preservation

Date: 2026-07-01

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

## Goal

Make the protocol transcoder explicitly cache-stable. Identical client-visible stable prefixes should produce identical provider-visible stable prefixes when configuration and request content are identical. Provider-native cache boundaries should be preserved where possible and loss should be explicit where not possible.

This phase still does not compress requests. It prepares the request pipeline so later compression can safely preserve cacheable prefixes and only mutate volatile suffixes.

## Non-goals

- Do not implement compression transforms.
- Do not synthesize Anthropic cache controls yet.
- Do not route based on cache metrics.
- Do not alter same-provider account fairness.
- Do not add semantic or model-based prefix rewriting.

## Why this phase matters

Provider prompt caches are typically prefix-sensitive. If EggPool translates OpenAI requests into Anthropic requests, or Anthropic requests into OpenAI requests, cache locality can still work if translation is deterministic. Cache locality degrades if the transcoder:

- Reorders stable content inconsistently.
- Serializes semantically identical structures differently between requests.
- Drops provider-native cache-control metadata silently.
- Injects request IDs, timestamps, warnings, or account-specific data into model-visible prefix content.
- Places transformed fields before stable content unpredictably.

The desired invariant is:

```text
same client stable prefix + same EggPool config
=> same canonical stable prefix
=> same provider-visible stable prefix
```

## Implementation tasks

### 1. Audit transcoder entry points

Identify all bidirectional translation paths:

- OpenAI Chat Completions to Anthropic Messages.
- Anthropic Messages to OpenAI Chat Completions or compatible shape.
- Streaming request/response paths if request bodies are prepared separately.
- Tool-use translation.
- Vision/document content translation.
- Thinking/reasoning field translation.
- Structured output translation.
- Error/loss warning behavior.

Document where model-visible request bodies are built and where provider-specific fields are inserted.

### 2. Define stable serialization rules

For any provider-visible structure produced by EggPool, ensure stable ordering and deterministic output.

Rules:

- Preserve original user message order.
- Preserve tool schema order unless an existing documented normalization requires otherwise.
- Preserve content block order.
- Use deterministic ordering when constructing dictionaries from internal maps.
- Avoid including process-local object IDs, timestamps, random UUIDs, selected account names, or retry counters in model-visible content.
- Ensure warning metadata goes to logs/stats, not into stable prefix content.

If JSON serialization is centralized, add tests around it. If provider bodies are normal Python dictionaries passed to httpx/json serialization, ensure construction order is deterministic.

### 3. Preserve provider-native cache fields where possible

Audit cache-related fields and extension fields:

- Anthropic `cache_control` on content blocks or top-level structures if currently supported.
- Anthropic primitives feature gate behavior.
- OpenAI cache-related fields/extensions if supported by current client/provider APIs.
- Provider wrappers that pass cache controls through OpenAI-compatible endpoints.

Implementation rules:

- If inbound and outbound protocols both support a cache field, preserve it.
- If inbound supports a cache field but outbound does not, record structured loss metadata.
- Do not drop cache controls silently.
- Do not convert cache fields into prompt text.
- Do not synthesize new cache controls in this phase.

### 4. Add cache-boundary annotations through transcoding

Use phase 2 segmentation metadata to carry cache boundary/protection status through protocol translation.

Requirements:

- Protected stable prefix segments remain protected after transcoding.
- Segment source locations may change, but the mapping should remain inspectable for tests.
- If a protocol conversion forces a protected field into a different location, record the transformation type.
- If a field is lost because the target protocol has no equivalent, record loss metadata.

### 5. Add loss accounting for cache fields

If the existing transcoder already has a loss policy, extend it with cache-specific reason codes.

Suggested reason codes:

- `cache_control_unsupported_by_target_protocol`
- `cache_control_feature_disabled`
- `cache_control_invalid_shape`
- `provider_extension_not_preserved`
- `stable_prefix_preserved`
- `stable_prefix_reordered_canonically`

Loss policy behavior:

- `warn`: record warning and continue.
- `reject`: reject if a protected cache control would be lost.

Do not make `reject` the default.

### 6. Prefix-equivalence test harness

Add a helper that extracts provider-visible stable prefix material after request preparation/transcoding. Use it in tests to compare prefixes across requests.

Test scenarios:

- Same OpenAI request translated twice produces identical Anthropic stable prefix.
- Same Anthropic request translated twice produces identical OpenAI stable prefix where representable.
- Changing only the volatile suffix does not change provider-visible stable prefix.
- Changing system/tool schema does change provider-visible stable prefix.
- Streaming and non-streaming variants do not inject different stable-prefix content, unless protocol requires a field outside model-visible content.

### 7. Protect warning/log insertion behavior

Search for any code that injects warnings into request bodies. If warnings are needed for clients, they should be response metadata, logs, or dashboard counters, not prompt text in the stable prefix.

Add a test ensuring cache/compression/transcoding warnings do not appear in model-visible stable prefix content.

### 8. Backward compatibility

The transcoder should preserve existing request/response semantics. Any change to field ordering should be semantically neutral. Any newly rejected request behavior must be gated behind existing or new strict loss policy configuration.

## Acceptance criteria

- Identical client stable prefixes produce identical provider-visible stable prefixes.
- Volatile suffix changes do not perturb stable prefix hash/equivalence output.
- Provider-native cache controls are preserved when target protocol supports them.
- Cache-control loss is explicitly recorded when preservation is impossible.
- No cache-related metric influences routing.
- Existing transcoder tests continue to pass.

## Manual verification

1. Send two OpenAI-compatible requests with identical system/tools/prior context but different latest tool output.
2. Capture provider-bound request bodies in a test harness or mock upstream.
3. Confirm the stable prefix is identical across both requests.
4. Repeat for Anthropic-compatible requests with native cache controls.
5. Confirm cache controls are preserved or loss is logged according to policy.
6. Confirm account selection remains governed by current fairness/quota behavior.

## Rollback notes

Most changes should be internal determinism and metadata improvements. If a provider compatibility issue appears, gate the specific preservation behavior behind a feature flag while keeping the prefix-stability tests for supported paths.
