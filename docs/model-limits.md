# Model Context Limits

EggPool supports configurable effective context limits for individual models on individual providers. This lets operators advertise a smaller context window than the provider physically supports.

## Why Effective Limits Exist

Some models support very large context windows (e.g., 1M tokens), but the cost and latency at that scale can be prohibitive. An operator may want to cap the advertised context window so that OpenCode's compaction machinery triggers earlier, keeping requests in a more efficient regime.

## Configuration

### Global Model Limits

Apply to all providers unless overridden per-provider:

```toml
[model_overrides."model-id"]
max_context_tokens = 200000
max_input_tokens = 180000
max_output_tokens = 16384
enforce_context_limit = true
```

### Provider-Specific Model Limits

Override global limits for a specific provider:

```toml
[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
enforce_context_limit = true
```

### Precedence

Each field is resolved independently:

1. Provider-specific override
2. Global override
3. Upstream-reported metadata
4. Unknown (None)

### Cross-Field Validation

- `max_input_tokens` must not exceed `max_context_tokens`
- `max_output_tokens` must not exceed `max_context_tokens`

## Model ID Matching

Override keys match the **base model ID**, not the provider-suffixed exposed ID:

```toml
# Correct: base model ID
[providers.opencode-go.model_overrides."MiniMax-M3"]

# Wrong: provider-suffixed ID
[providers.opencode-go.model_overrides."MiniMax-M3/opencode-go"]
```

## Unsuffixed vs Provider-Suffixed Models

When the same model is served by multiple providers:

- **Unsuffixed** (`MiniMax-M3`): Uses the conservative minimum across all providers
- **Provider-suffixed** (`MiniMax-M3/opencode-go`): Uses that provider's exact limit

## OpenCode Integration

Generate an OpenCode configuration with model limits:

```bash
eggpool configsetup opencode --json-only > opencode-config.json
```

This produces a JSON file with explicit `limit.context`, `limit.input`, and `limit.output` values for each model. OpenCode uses these to trigger compaction before exceeding the configured window.

## Server-Side Enforcement

When `enforce_context_limit = true` (the default), EggPool rejects requests that exceed the configured context limit. This is a defensive guardrail, not the primary compaction mechanism. The primary mechanism is OpenCode's native compaction driven by the advertised limits.

Enforcement returns HTTP 400 with protocol-appropriate error envelopes for OpenAI and Anthropic endpoints.

## Restart Requirements

Configuration changes to model limits require a service restart. Live reload is not supported for model limit policy.
