# Phase 3: Capability Config Overrides

## Objective

Add operator-controlled model capability overrides for thinking/reasoning support. External catalogs will not always expose newly added model controls, and provider behavior may differ by account, aggregator, or upstream protocol. EggPool needs a deterministic override path.

## Problem statement

Without overrides, EggPool must choose between unsafe inference and excessive uncertainty. For example, a model may be known from real API testing to accept Anthropic `thinking`, while the provider's model listing does not declare that fact. Conversely, an Anthropic-compatible provider may reject `thinking` even though the protocol shape supports it.

Config overrides let operators pin reality locally while preserving provenance.

## Proposed config shape

Support global model overrides:

```toml
[model_capabilities."minimax-m3".thinking]
status = "supported"
native_protocols = ["anthropic"]
budget_tokens_min = 1024
budget_tokens_max = 16384
source = "manual_override"

[model_capabilities."some-openai-model".thinking]
status = "unsupported"
source = "manual_override"
```

Support provider-scoped overrides:

```toml
[providers.minimax.model_capabilities."minimax-m3".thinking]
status = "supported"
native_protocols = ["anthropic"]
budget_tokens_min = 1024
budget_tokens_max = 16384
source = "manual_override"
```

Provider-scoped overrides should win over global model overrides.

## Implementation tasks

1. Inspect existing config models and provider config parsing.
2. Add a top-level `model_capabilities` map keyed by model id/base model id.
3. Add provider-scoped `model_capabilities` maps under each provider.
4. Reuse the canonical capability schema from Phase 2 where practical.
5. Validate status, source, protocols, and budget fields at config-load time.
6. Apply overrides during catalog/model exposure and routing capability lookup.
7. Preserve source as `manual_override` unless the config explicitly supports a narrower value.
8. Add examples to the default/sample config only if that file is already used for documented optional settings.

## Validation rules

- `status` must be one of the canonical capability statuses.
- `native_protocols` must contain known protocol names only.
- `budget_tokens_min` and `budget_tokens_max` must be positive integers when present.
- `budget_tokens_min <= budget_tokens_max` when both are present.
- `effort_to_budget_tokens` values must be positive integers.
- Unknown fields should either be rejected or reported according to the repository's existing config strictness pattern.

## Precedence rules

Use this order:

1. Built-in default: `unknown`.
2. Discovered provider/model-info capability.
3. Top-level global model override.
4. Provider-scoped model override.

If the same model appears under multiple providers, provider-scoped overrides must not leak to other providers.

## Acceptance criteria

- Operators can declare thinking support for a model globally.
- Operators can override thinking support for a provider-specific model.
- Provider-scoped overrides take precedence over global overrides.
- Invalid config fails fast with a clear error.
- Missing overrides preserve current behavior.
- Tests cover global override, provider override, precedence, invalid status, invalid protocol, and invalid budget bounds.

## Risks

Config key choice matters. Some provider listings may use provider-specific aliases rather than canonical base model ids. The implementation should document whether keys match exposed model id, base model id, provider-native model id, or all of those via alias matching.

## Completion check

Create a test config with both global and provider-scoped overrides for the same model. Confirm `/v1/models` and internal lookup return the provider-scoped value for that provider and the global value elsewhere.
