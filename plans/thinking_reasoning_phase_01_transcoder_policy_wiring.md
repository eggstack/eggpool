# Phase 1: Runtime Transcoder Policy Wiring

## Objective

Ensure the runtime proxy path honors the configured transcoder policy, especially `[transcoder.features].thinking`, during actual dispatch. The current shape suggests app startup stores `config.transcoder` on application state, but `RequestCoordinator` is constructed without the policy. That makes preflight behavior and real coordinator behavior diverge.

## Problem statement

Thinking support is feature-gated. The transcoders only preserve or translate `reasoning_effort`, assistant `reasoning_content`, and Anthropic thinking blocks when `features.thinking` is enabled. If `RequestCoordinator` does not receive the configured policy, the coordinator sees `self._transcoder_policy is None`, passes `features=None`, and drops thinking fields even when config enables them.

This creates a misleading state: configuration and docs imply thinking transcoding can be enabled, while the actual dispatch path can still degrade the request.

## Implementation tasks

1. Inspect `src/eggpool/app.py` startup wiring.
2. Locate the `RequestCoordinator(...)` construction.
3. Pass `transcoder_policy=config.transcoder` into the constructor.
4. Verify no duplicate or conflicting policy source exists.
5. Keep `app.state.transcoder_policy = config.transcoder` if other routes, preflight helpers, or diagnostics use it.
6. Audit all direct `RequestCoordinator` instantiations in tests and production code. Update tests to either pass a policy explicitly or assert the desired default behavior.

## Regression tests

Add tests that prove preflight and actual dispatch use the same feature configuration.

### Test 1: OpenAI reasoning effort is translated when enabled

Input client payload:

```json
{
  "model": "anthropic-only-model",
  "messages": [{"role": "user", "content": "test"}],
  "reasoning_effort": "medium"
}
```

Config:

```toml
[transcoder.features]
thinking = true
```

Expected upstream Anthropic request includes:

```json
{
  "thinking": {
    "type": "enabled",
    "budget_tokens": 4096
  }
}
```

### Test 2: OpenAI reasoning effort is dropped or rejected when disabled

Same input payload.

Config:

```toml
[transcoder.features]
thinking = false
```

Expected behavior depends on current loss policy. At minimum, the upstream payload must not contain `thinking`, and a structured warning should identify `reasoning_effort` as dropped because thinking transcoding is disabled.

### Test 3: Assistant reasoning content survives when enabled

Input message history contains an assistant message with `reasoning_content`. With thinking enabled and OpenAI-to-Anthropic transcoding active, the Anthropic message content should include a `thinking` block.

### Test 4: Coordinator policy differs from default

Instantiate a coordinator in a test with a non-default `TranscoderPolicy(features=TranscoderFeatures(thinking=True))`. Assert the actual outbound request uses that policy. This prevents future regressions where constructors silently fall back to defaults.

## Files likely involved

- `src/eggpool/app.py`
- `src/eggpool/proxy/coordinator.py`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/transcoder/policy.py`
- Existing proxy/transcoder tests under `tests/`

## Acceptance criteria

- `RequestCoordinator` receives the configured `TranscoderPolicy` from app startup.
- Enabling `[transcoder.features].thinking` changes actual upstream request translation, not only preflight translation.
- Disabling thinking preserves current safe-drop behavior.
- Tests fail if app wiring omits `transcoder_policy=config.transcoder` again.
- Existing non-thinking transcoding tests continue to pass.

## Risks

Some tests may currently rely on implicit `None` policy defaults. Update those tests deliberately rather than preserving accidental behavior.

Some upstreams may reject `thinking` despite the config being enabled. That is expected and addressed by later capability-aware phases. This phase only fixes feature-flag propagation.

## Completion check

Run the targeted transcoder and proxy test subsets, then run the full test suite if feasible. Manually inspect a captured outbound request for an OpenAI client request routed to an Anthropic upstream with `reasoning_effort = "medium"` and thinking enabled.
