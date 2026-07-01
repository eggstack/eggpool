# Phase 8: Response-Field Compatibility

## Objective

Normalize how EggPool exposes upstream thinking output to OpenAI-compatible clients in non-streaming and streaming responses. Clients vary in which reasoning field names they recognize, so EggPool should make response field emission explicit and configurable.

## Problem statement

The current non-streaming Anthropic-to-OpenAI path can expose thinking as `message.reasoning_content` when thinking is enabled. The streaming path emits Anthropic `thinking_delta` as OpenAI `delta.reasoning`. That may work for some clients, but OpenAI-compatible clients are not fully standardized around reasoning field names.

EggPool should avoid ad hoc field choices and make compatibility behavior intentional.

## Proposed config

```toml
[transcoder.openai_reasoning_fields]
non_stream = ["reasoning_content"]
stream_delta = ["reasoning"]
emit_compat_aliases = false
```

If `emit_compat_aliases = true`, EggPool may emit more than one configured field where doing so does not break strict parsers. Default should remain conservative.

## Implementation tasks

1. Inspect non-streaming Anthropic-to-OpenAI response decode.
2. Inspect streaming Anthropic-to-OpenAI `thinking_delta` handling.
3. Add a small field-emission helper for OpenAI-compatible reasoning content.
4. Wire config into non-streaming decode.
5. Wire config into streaming translation.
6. Ensure streaming translation is feature-gated consistently with non-streaming translation unless the project deliberately chooses unconditional pass-through.
7. Add tests for each configured field mode.

## Field behavior

Recommended defaults:

- Non-streaming: `choices[].message.reasoning_content`.
- Streaming: `choices[].delta.reasoning`.

Optional compatibility aliases:

- Non-streaming: `reasoning`, `reasoning_content`.
- Streaming: `reasoning`, `reasoning_content`.

Do not emit aliases by default if strict clients may reject unknown fields or duplicate semantic fields.

## Feature gating

Current streaming thinking delta behavior appears less feature-gated than non-streaming behavior. Decide and enforce one rule:

- Preferred: if `[transcoder.features].thinking = false`, streaming thinking deltas should be dropped with a structured warning or converted according to loss policy.
- Alternative: document streaming pass-through as always enabled. This is less consistent and should be avoided unless existing clients depend on it.

## Acceptance criteria

- Non-streaming reasoning output uses configured OpenAI field names.
- Streaming reasoning deltas use configured OpenAI delta field names.
- Streaming and non-streaming feature gating are consistent.
- Tests verify exact JSON chunks for Anthropic `thinking_delta` translation.
- Tests verify no thinking content is emitted when feature gating disables it and policy requires dropping.
- EggPool never fabricates reasoning content; it only forwards upstream-exposed thinking/reasoning text.

## Risks

Emitting non-standard fields may confuse some clients. Keep defaults narrow and documented.

If existing users rely on current `delta.reasoning`, preserve it as default unless there is a strong compatibility reason to change.

## Completion check

Use a synthetic Anthropic SSE stream with `content_block_delta` and `thinking_delta`. Confirm EggPool emits the configured OpenAI streaming delta field exactly, and confirm the field disappears or warning path triggers when thinking transcoding is disabled.
