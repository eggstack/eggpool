# Phase 6: Capability-Aware Routing

## Objective

Make routing aware of explicit thinking/reasoning requests so EggPool does not silently send those requests to upstream models that cannot honor them.

## Problem statement

Once EggPool accepts and advertises thinking controls, routing must account for them. If a client sends OpenAI `reasoning_effort` or Anthropic `thinking`, EggPool should not choose a provider/account whose selected model is known unsupported. Silent field dropping is especially problematic because the client explicitly requested a model behavior.

## Request classification

Add a small detector that classifies whether a request requires thinking support.

OpenAI client indicators:

- Top-level `reasoning_effort`.
- Assistant history containing `reasoning_content` that must be preserved across protocol translation.
- Future configured OpenAI reasoning fields if Phase 8 adds aliases.

Anthropic client indicators:

- Top-level `thinking`.
- Assistant/user message content blocks of type `thinking` if those are accepted in history.

The detector should return a structured result, not a boolean:

```python
class ThinkingRequestRequirement(BaseModel):
    required: bool
    client_protocol: str
    fields: list[str]
    requested_effort: str | None = None
    requested_budget_tokens: int | None = None
```

## Policy config

Add a routing/transcoder capability policy. Suggested shape:

```toml
[transcoder.capability_policy]
unsupported_thinking = "reject"      # reject | warn_drop | route_best_effort
unknown_thinking = "reject"          # reject | allow_with_warning | route_best_effort
mixed_collapsed_thinking = "filter"  # filter | reject | allow
```

Default should favor correctness. A client explicitly asking for thinking should get either a compatible upstream or a clear error.

## Implementation tasks

1. Add request classification helper.
2. Add capability lookup for a candidate model/provider/account.
3. Integrate capability filtering into the routing candidate selection path.
4. Filter or reject candidates based on policy when thinking is explicitly requested.
5. Make error responses distinguish:
   - no model found;
   - model exists but thinking is unsupported;
   - model exists but thinking support is unknown;
   - collapsed model has mixed provider support and policy rejects ambiguity.
6. Preserve existing behavior for requests that do not ask for thinking.
7. Add tests for each policy mode.

## Routing behavior

Recommended default:

- `supported`: candidate allowed.
- `unsupported`: reject candidate; if no candidates remain, return a capability error.
- `unknown`: reject by default or allow only if config explicitly permits unknown support.
- `mixed`: for collapsed models, filter to supported providers if possible; otherwise apply unknown/unsupported policy.
- `conflicting`: reject unless manual override resolves it.

## Error shape

Use the repository's existing error response style. Include enough detail for debugging:

```json
{
  "error": {
    "type": "capability_error",
    "message": "Model minimax-m3 is available, but no eligible provider is known to support requested thinking controls.",
    "capability": "thinking",
    "requested_fields": ["reasoning_effort"],
    "model": "minimax-m3"
  }
}
```

## Acceptance criteria

- Requests without thinking controls route exactly as before.
- Requests with `reasoning_effort` do not route to candidates marked `unsupported` under default policy.
- Requests with `reasoning_effort` handle `unknown` according to config.
- Collapsed model requests with mixed provider support filter to supported providers when policy allows filtering.
- Capability errors are clear and distinct from model-not-found errors.
- Tests cover supported, unsupported, unknown, mixed, and conflicting capability states.

## Risks

This phase can change routing outcomes for clients that already send reasoning fields. That is desirable for correctness but may surprise users. Document the policy and provide an escape hatch such as `route_best_effort`.

## Completion check

Configure two providers for the same model, one supported and one unsupported. Send a collapsed-model OpenAI request with `reasoning_effort`. Confirm only the supported provider is eligible under filter policy, and confirm a clear rejection if no supported providers exist.
