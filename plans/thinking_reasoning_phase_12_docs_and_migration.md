# Phase 12: Documentation and Migration Guidance

## Objective

Document thinking/reasoning capability support, configuration, model listing exposure, routing policy, and client usage so operators can safely enable the feature and understand its limitations.

## Problem statement

Thinking/reasoning support has subtle semantics. Users need to understand the difference between model capability, protocol compatibility, transcoder support, and client visibility. Documentation should prevent false expectations such as assuming every Anthropic-compatible model supports thinking or assuming EggPool can synthesize reasoning output.

## Documentation targets

Update the relevant existing docs. Likely candidates:

- `docs/transcoding.md`
- provider/model-info documentation
- config reference or example config
- opencode config setup documentation
- dashboard/runtime metrics documentation if present
- README feature summary if the project keeps high-level features there

## Required documentation sections

### 1. Conceptual model

Explain:

- A model may support thinking natively.
- EggPool may be able to transcode client controls to that native protocol.
- Clients may discover support through `eggpool.capabilities` in `/v1/models`.
- Unknown support is not the same as unsupported.
- EggPool does not fabricate hidden reasoning; it only forwards provider-exposed content.

### 2. Enabling thinking transcoding

Document:

```toml
[transcoder.features]
thinking = true
```

Explain behavior when disabled and how `loss_policy` affects dropped/rejected fields.

### 3. Capability overrides

Document global overrides:

```toml
[model_capabilities."minimax-m3".thinking]
status = "supported"
native_protocols = ["anthropic"]
budget_tokens_min = 1024
budget_tokens_max = 16384
```

Document provider-scoped overrides:

```toml
[providers.minimax.model_capabilities."minimax-m3".thinking]
status = "supported"
native_protocols = ["anthropic"]
```

Explain precedence.

### 4. `/v1/models` metadata

Show an example `eggpool.capabilities.thinking` block. Explain provider-scoped vs collapsed model behavior and mixed capability states.

### 5. Routing policy

Document policy options such as:

```toml
[transcoder.capability_policy]
unsupported_thinking = "reject"
unknown_thinking = "reject"
mixed_collapsed_thinking = "filter"
```

Explain the consequences of strict rejection versus best-effort routing.

### 6. Budget mapping

Document defaults:

- `low = 1024`
- `medium = 4096`
- `high = 16384`

Explain provider/model overrides, clamping, warnings, and strict rejection.

### 7. Client examples

OpenAI-style request:

```json
{
  "model": "minimax-m3/minimax",
  "messages": [{"role": "user", "content": "Solve this carefully."}],
  "reasoning_effort": "medium"
}
```

Anthropic-style request:

```json
{
  "model": "minimax-m3/minimax",
  "messages": [{"role": "user", "content": "Solve this carefully."}],
  "thinking": {
    "type": "enabled",
    "budget_tokens": 4096
  }
}
```

### 8. opencode guidance

Explain how `eggpool configsetup opencode` discovers exposed models, how thinking-capable models appear, and when provider-scoped model ids are preferable to collapsed ids.

### 9. Troubleshooting

Include cases:

- `reasoning_effort` is ignored.
- Model listing shows `unknown` support.
- Request is rejected with `capability_error`.
- Collapsed model routes inconsistently because backing providers differ.
- Streaming reasoning deltas are missing.
- Provider rejects an apparently supported budget.

## Acceptance criteria

- Docs explain capability, protocol, transcoder, and client exposure separately.
- Docs include exact config examples for enabling thinking and declaring model support.
- Docs show `/v1/models` capability metadata.
- Docs describe routing policy defaults and failure modes.
- Docs include opencode-specific guidance.
- README or feature list is updated only with accurate, non-overstated claims.

## Risks

Do not imply universal model support. Keep examples clearly marked as examples, and prefer provider/model-specific statements only when verified.

Do not include provider-private or user-specific API behavior unless it is documented or configured locally as an override.

## Completion check

A new operator should be able to enable thinking transcoding, add a model capability override, verify `/v1/models`, generate opencode config, and understand any capability error using only repository documentation.
