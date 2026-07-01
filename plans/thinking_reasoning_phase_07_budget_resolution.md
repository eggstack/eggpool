# Phase 7: Thinking Budget Resolution

## Objective

Move effort-to-budget translation out of hard-coded transcoder branches and into a reusable budget resolver that understands global defaults, provider/model overrides, capability min/max limits, clamping, and strict rejection.

## Problem statement

The current OpenAI-to-Anthropic mapping is simple:

- `low` -> 1024
- `medium` -> 4096
- `high` -> 16384

That is a reasonable fallback, but it is not sufficient as a long-term capability model. Different providers and models may support different thinking budgets. Some may reject high budgets; others may benefit from larger budgets. Hard-coding the mapping inside one transcoder also makes it difficult for `/v1/models`, routing, docs, and tests to agree on behavior.

## Proposed resolver

Add a helper near transcoder policy or capability code:

```python
class ThinkingBudgetResolution(BaseModel):
    budget_tokens: int
    source: str
    clamped: bool = False
    warnings: list[TranscodeWarning] = Field(default_factory=list)


def resolve_thinking_budget(
    *,
    model_id: str,
    provider_id: str | None,
    requested_effort: str | None,
    requested_budget_tokens: int | None,
    capability: ThinkingCapability,
    policy: TranscoderPolicy,
) -> ThinkingBudgetResolution:
    ...
```

The resolver should support OpenAI-style effort, Anthropic-style explicit budgets, and future configured aliases.

## Configuration

Support global defaults:

```toml
[transcoder.thinking_budget_defaults]
low = 1024
medium = 4096
high = 16384
```

Support provider/model-specific mapping via capability overrides:

```toml
[providers.minimax.model_capabilities."minimax-m3".thinking.effort_to_budget_tokens]
low = 2048
medium = 8192
high = 16384
```

## Resolution order

1. If Anthropic client supplies explicit `thinking.budget_tokens`, validate it against known limits.
2. If OpenAI client supplies `reasoning_effort`, resolve effort using provider/model mapping if present.
3. Fall back to global defaults.
4. If effort is unknown, either use `medium` with warning or reject according to policy.
5. Clamp to capability min/max if policy allows clamping.
6. Reject if strict policy forbids clamping or the requested value violates known upstream limits.

## Implementation tasks

1. Add budget default config.
2. Add resolver and result type.
3. Replace hard-coded mapping in OpenAI-to-Anthropic request translation.
4. Use the same resolver metadata when serializing `/v1/models` capability info.
5. Add warnings for unknown effort, clamped budget, and missing budget defaults.
6. Add strict rejection path if policy requires exact support.
7. Add unit tests independent of HTTP routing.

## Acceptance criteria

- Effort-to-budget mapping is centralized.
- Model/provider capability overrides can tune budget values.
- Budgets are clamped or rejected according to policy.
- Transcoder warnings record clamping and fallback behavior.
- `/v1/models` reports the same effective effort mapping used at runtime when enough context is available.
- Tests cover low, medium, high, unknown effort, explicit Anthropic budget, min clamp, max clamp, and strict rejection.

## Risks

There may not always be a provider id at the point of early preflight translation. Avoid resolving provider-specific budgets until the provider is known, or ensure the preflight result can be recalculated after routing. Runtime translation should be authoritative.

## Completion check

Configure a model with `high = 32768` but `budget_tokens_max = 16384`. Under clamp policy, confirm the upstream request uses `16384` with a warning. Under strict policy, confirm the request is rejected before dispatch.
