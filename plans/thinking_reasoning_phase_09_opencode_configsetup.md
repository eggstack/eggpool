# Phase 9: `eggpool configsetup opencode`

## Objective

Update `eggpool configsetup opencode` so opencode users can see and select all exposed EggPool models, including thinking-capable models, without depending on prior usage history or hard-coded provider assumptions.

## Problem statement

The model page and generated client configs should expose the full aggregated model set, not only models that have already been used. If EggPool adds thinking/reasoning capability metadata under `/v1/models`, the opencode config generator should preserve that discoverability and avoid hiding provider-scoped models.

## Implementation tasks

1. Inspect the current `configsetup opencode` command implementation.
2. Identify whether it reads live `/v1/models`, catalog cache, provider configs, or usage history.
3. Ensure the generated opencode config includes all currently exposed models from the catalog/model exposure layer.
4. Ensure provider-scoped model ids are included when EggPool exposes them.
5. If opencode supports model metadata or comments, include a minimal capability note for thinking-capable models.
6. Do not hard-code `reasoning_effort` for models whose support is `unknown` or `unsupported`.
7. Add CLI tests or golden output tests for generated config.

## Desired behavior

The generated config should favor the same model ids visible through `/v1/models`. If EggPool exposes both collapsed and provider-scoped models, the config should either include both or follow the existing EggPool exposure policy consistently.

Thinking-capable models should be selectable. The generator should not require that the user has previously routed a request to that model.

## Capability handling

If opencode has a native way to express model reasoning options, map only known-supported EggPool capabilities into that shape. If opencode does not have a native way, expose model names and rely on `/v1/models` metadata for advanced discovery.

Suggested behavior by capability status:

- `supported`: include any safe opencode model options or comments.
- `unknown`: include the model, but do not declare reasoning controls.
- `unsupported`: include the model, but do not declare reasoning controls.
- `mixed`: prefer provider-scoped ids for thinking usage, or include a warning/comment if collapsed id may route to non-thinking providers.

## Acceptance criteria

- `eggpool configsetup opencode` lists all available exposed models, not only used models.
- Thinking-capable provider-scoped models are visible.
- The generated config does not claim thinking support for unknown/unsupported models.
- Mixed collapsed models do not silently appear as uniformly thinking-capable.
- Tests verify generated config with supported, unknown, unsupported, and mixed models.

## Risks

The exact opencode config schema may evolve. Keep EggPool's generator conservative and avoid writing unsupported opencode-specific fields unless verified against current opencode behavior.

## Completion check

Create a fixture with three providers and four models: one supported, one unsupported, one unknown, and one mixed collapsed model. Run `eggpool configsetup opencode` and confirm all models appear while only known-supported models receive reasoning-related annotations/options.
