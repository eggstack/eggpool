# Phase 11: Thinking/Reasoning Test Matrix

## Objective

Add a comprehensive regression test matrix for thinking/reasoning capability, transcoding, model listing, routing, streaming, and config behavior.

## Problem statement

This feature spans multiple subsystems. A narrow transcoder unit test is not enough because the original bug class involved app wiring and coordinator behavior, not only body translation. Tests must prove that configured feature flags and capability metadata survive from config load through routing and response conversion.

## Test groups

### 1. Config and capability schema tests

Cover:

- Default capability is `unknown`.
- Global model override sets thinking support.
- Provider-scoped override wins over global override.
- Invalid status is rejected.
- Invalid protocol is rejected.
- Invalid budget bounds are rejected.
- Mixed collapsed capability is computed correctly.

### 2. `/v1/models` serialization tests

Cover:

- Provider-scoped model with supported thinking.
- Provider-scoped model with unknown thinking.
- Collapsed model with all supported providers.
- Collapsed model with all unknown providers.
- Collapsed model with mixed supported/unknown/unsupported providers.
- Existing OpenAI-compatible fields remain stable.

### 3. Request classification tests

Cover:

- OpenAI `reasoning_effort` marks request as requiring thinking.
- Assistant `reasoning_content` marks request as preserving thinking history when cross-protocol translation requires it.
- Anthropic top-level `thinking` marks request as requiring thinking.
- Plain requests do not require thinking.

### 4. Routing tests

Cover:

- Supported provider remains eligible.
- Unsupported provider is filtered or rejected under default policy.
- Unknown provider follows `unknown_thinking` policy.
- Collapsed mixed model filters to supported provider when configured to filter.
- Collapsed mixed model rejects when configured to reject.
- Clear capability error is returned when no eligible provider remains.

### 5. OpenAI-to-Anthropic request transcoding tests

Cover:

- `reasoning_effort = low` -> expected budget.
- `reasoning_effort = medium` -> expected budget.
- `reasoning_effort = high` -> expected budget.
- Unknown effort fallback or rejection.
- Assistant `reasoning_content` -> Anthropic `thinking` block when enabled.
- Same fields are dropped or rejected when thinking feature is disabled.

### 6. Anthropic-to-OpenAI request transcoding tests

Cover:

- Anthropic top-level `thinking` behavior for OpenAI upstream.
- Anthropic thinking content blocks in history.
- Unsupported reverse mapping is explicitly warned or rejected rather than silently ignored.

### 7. Non-streaming response tests

Cover:

- Anthropic `content[].thinking` -> OpenAI `message.reasoning_content` by default.
- Redacted thinking is dropped with warning.
- Configured response field aliases behave as expected.
- Feature-disabled path does not emit thinking content.

### 8. Streaming response tests

Cover:

- Anthropic `thinking_delta` -> OpenAI configured delta field.
- Ordering is preserved relative to text deltas.
- Feature-disabled path is consistent with non-streaming behavior.
- Tool-call streaming remains unaffected.

### 9. App/coordinator integration tests

Cover:

- `RequestCoordinator` receives configured `TranscoderPolicy` from app startup.
- Preflight translation and actual dispatch translation agree.
- Tests fail if coordinator falls back to `features=None` unexpectedly.

### 10. Observability tests

Cover:

- Requested counter increments.
- Transcoded counter increments.
- Dropped counter increments.
- Rejected counter increments.
- Budget clamped counter increments.
- Request trace contains no prompt or reasoning content, only decision metadata.

## Fixtures

Create synthetic providers rather than relying on live providers. At minimum:

- `anthropic_supported_provider`
- `anthropic_unknown_provider`
- `anthropic_unsupported_provider`
- `openai_supported_provider` if OpenAI-side reasoning support is modeled
- `collapsed_mixed_model` backed by at least two providers

## Acceptance criteria

- The test suite covers config, catalog, model listing, routing, request translation, response translation, streaming, and observability.
- Tests prove that protocol compatibility alone does not imply thinking support.
- Tests fail if `/v1/models` claims support without an explicit source or override.
- Tests fail if app startup omits transcoder policy injection into `RequestCoordinator`.
- Existing non-thinking tests continue to pass.

## Risks

Overly broad integration tests can become brittle. Keep most logic in unit tests with a small number of end-to-end tests for wiring bugs.

## Completion check

Run the full test suite and inspect coverage for the thinking/reasoning modules. Verify that intentionally disabling policy injection causes at least one integration test to fail.
