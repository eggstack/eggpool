# Per-provider model context limit implementation plan

## Objective

Add configurable effective context limits for individual models on individual providers, with MiniMax M3 through OpenCode Go as the initial use case:

```toml
[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
```

The feature must make the configured limit visible to OpenCode so OpenCode's own compaction machinery treats 220,000 tokens as the model context window. It must also preserve the upstream-reported physical context window for diagnostics and future policy decisions.

The intended semantics are:

- The provider may physically support a larger context window.
- EggPool advertises a smaller effective context window selected by the operator.
- OpenCode compacts before crossing the effective limit.
- Provider-specific limits remain independent when the same model exists through multiple providers.
- Unsuffixed model exposure uses a conservative limit when several providers can serve the same model.
- EggPool can optionally enforce the policy server-side so stale or non-OpenCode clients cannot bypass it.

This plan is deliberately separated into metadata policy, OpenCode integration, and request enforcement. The OpenCode integration is the part that causes proactive compaction. Server-side rejection is only a defensive guardrail and must not be mistaken for compaction.

## Current codebase observations

The existing implementation already has most of the structural pieces needed:

- `src/eggpool/models/config.py` defines `ProviderConfig`, `ModelOverrideConfig`, and `AppConfig` using Pydantic with `extra="forbid"`.
- Global model overrides currently support protocol and pricing fields under `[model_overrides.<model-id>]`.
- `src/eggpool/catalog/service.py` applies global model overrides during catalog refresh and maintains provider-specific catalog entries.
- `src/eggpool/catalog/cache.py` stores both global model metadata and `(model_id, provider_id)` metadata. It also exposes provider-suffixed IDs in the form `model-id/provider-id`.
- `src/eggpool/app.py` currently returns only basic OpenAI-compatible fields from `GET /v1/models`; it does not expose context limits.
- `src/eggpool/cli.py` has `eggpool configsetup opencode`, but its generated configuration uses an old/minimal provider snippet and does not declare models or model limits.
- OpenCode compaction is driven by its resolved model metadata, specifically `model.limit.context`, `model.limit.input`, and the output-token reservation. Adding an arbitrary context field to EggPool's `/v1/models` response is therefore insufficient by itself.

The implementation should build on the provider-specific catalog map rather than introducing a second parallel model registry.

## Non-goals

Do not implement any of the following as part of this change:

- Actual prompt compaction inside EggPool.
- Token-aware message truncation in EggPool.
- Rewriting user prompts to fit the configured window.
- A tokenizer implementation for every model family.
- Database migrations solely for static operator configuration.
- Dynamic live reload of context policy unless the existing configuration reload path can safely update all dependent components.
- Treating `max_context_tokens` as the provider's physical limit. It is an operator-selected effective limit.

## Configuration design

### 1. Add a reusable model limit override type

In `src/eggpool/models/config.py`, introduce a focused Pydantic model rather than duplicating fields in several classes:

```python
class ModelLimitOverrideConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_context_tokens: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    enforce_context_limit: bool = True
```

The three token fields have distinct meanings:

- `max_context_tokens`: total input plus generated output capacity advertised to clients.
- `max_input_tokens`: optional provider/model-specific input ceiling when it differs from total context.
- `max_output_tokens`: effective generation ceiling used for OpenCode output reservation and request validation.

Do not infer `max_input_tokens = max_context_tokens`. A model may have one total context limit and a separate output reservation.

### 2. Extend global model overrides

Extend `ModelOverrideConfig` with the same fields. Either inherit from `ModelLimitOverrideConfig` or compose it carefully. Prefer inheritance only if it remains clear and Pydantic validation behaves predictably:

```python
class ModelOverrideConfig(ModelLimitOverrideConfig):
    protocol: Literal["openai", "anthropic"] | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_read_per_million_microdollars: int | None = None
    cache_write_per_million_microdollars: int | None = None
```

The existing `[model_overrides.<model-id>]` section remains a provider-independent default.

### 3. Add provider-scoped overrides

Add this field to `ProviderConfig`:

```python
model_overrides: dict[str, ModelLimitOverrideConfig] = {}
```

Example:

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
enforce_context_limit = true
```

This nesting is preferable to a compound top-level key because provider ownership and model policy remain colocated.

### 4. Define deterministic precedence

Use the following precedence independently for each field:

1. Provider-specific model override.
2. Global model override.
3. Provider-discovered model metadata.
4. Unknown (`None`).

A provider override must not replace the entire global override object. Resolve each field independently so this works:

```toml
[model_overrides."MiniMax-M3"]
max_output_tokens = 16384

[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
```

The effective result is context `220000` and output `16384`.

### 5. Add cross-field validation

Validate only relationships that are unambiguously invalid:

- `max_input_tokens <= max_context_tokens` when both are set.
- `max_output_tokens <= max_context_tokens` when both are set.

Do not require `max_input_tokens + max_output_tokens <= max_context_tokens`; some APIs define input and output maxima independently and OpenCode already reserves output from total context where appropriate.

Errors must include provider and model identity when validation happens at `AppConfig` level. Example:

```text
providers.opencode-go.model_overrides.MiniMax-M3: max_output_tokens (262144) exceeds max_context_tokens (220000)
```

### 6. Document exact model ID matching

Override keys should match the upstream/base model ID, not the provider-suffixed exposed ID. For example:

```toml
[providers.opencode-go.model_overrides."MiniMax-M3"]
```

not:

```toml
[providers.opencode-go.model_overrides."MiniMax-M3/opencode-go"]
```

The suffix is a client-facing routing selector. Internally, the cache already stores the base model and provider separately.

## Effective limit domain model

### 1. Add a dedicated resolver module

Create `src/eggpool/catalog/limits.py`. Keep policy resolution out of `CatalogService`, `ModelCatalogCache`, and request handlers.

Suggested immutable result type:

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class EffectiveModelLimits:
    context_tokens: int | None
    input_tokens: int | None
    output_tokens: int | None
    enforce: bool
    context_source: str | None
    input_source: str | None
    output_source: str | None
```

Use per-field provenance rather than one generic `source`, because a context limit may come from a provider override while output comes from upstream metadata.

Suggested source values:

- `provider_override`
- `global_override`
- `upstream_metadata`
- `unknown`

Avoid free-form provider names inside the source field. Provider identity is already part of the lookup key.

### 2. Implement upstream metadata extraction defensively

Add a small extraction function that inspects normalized `capabilities` and `source_metadata`. Providers use inconsistent keys, so support a bounded list of known aliases without recursively scanning arbitrary JSON.

Candidate context keys:

```text
context_window
context_length
max_context_tokens
max_position_embeddings
```

Candidate input keys:

```text
max_input_tokens
input_token_limit
```

Candidate output keys:

```text
max_output_tokens
output_token_limit
max_completion_tokens
```

Extraction rules:

- Accept positive integers.
- Accept numeric strings only if strict integer parsing succeeds.
- Reject booleans, floats with fractional values, zero, and negative values.
- Prefer normalized `capabilities` over opaque `source_metadata` if both contain a value.
- Do not mutate the source metadata.
- Do not guess units.

Write the extractor so new aliases can be added in one table rather than new conditionals throughout the code.

### 3. Resolve limits by provider and base model

Suggested interface:

```python
class ModelLimitResolver:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def resolve(
        self,
        *,
        provider_id: str,
        model_id: str,
        capabilities: Mapping[str, object],
        source_metadata: Mapping[str, object],
    ) -> EffectiveModelLimits:
        ...
```

The resolver should be pure after construction. It must not access the database, network, cache, or account registry.

### 4. Add a conservative merge helper for unsuffixed models

When one unsuffixed model can route to several providers, OpenCode sees one model and needs one advertised limit. Add a helper that merges a sequence of provider-specific limits:

```python
def conservative_limits(
    limits: Iterable[EffectiveModelLimits],
) -> EffectiveModelLimits:
    ...
```

Rules:

- For each numeric field, take the minimum of known positive values.
- If every provider has `None`, return `None`.
- `enforce` should be true if the selected route can ever land on an enforcing provider. The request path should ultimately validate against the chosen provider, but client advertisement must remain conservative.
- Provenance for a conservative merge may be `conservative_provider_minimum`.

Do not select the first provider encountered. Current cache iteration through account sets is not a stable policy boundary.

## Catalog integration

### 1. Resolve limits during account refresh

In `CatalogService.__init__`, create a `ModelLimitResolver` from `AppConfig`.

In `_fetch_and_process_account()`, after protocol resolution and provider protocol validation, resolve limits for every normalized model using the current `provider_id`.

Attach two separate structures:

```python
model["discovered_limits"] = {
    "context_tokens": discovered.context_tokens,
    "input_tokens": discovered.input_tokens,
    "output_tokens": discovered.output_tokens,
}

model["effective_limits"] = {
    "context_tokens": effective.context_tokens,
    "input_tokens": effective.input_tokens,
    "output_tokens": effective.output_tokens,
    "enforce": effective.enforce,
    "context_source": effective.context_source,
    "input_source": effective.input_source,
    "output_source": effective.output_source,
}
```

`discovered_limits` preserves what the provider advertised. `effective_limits` is EggPool policy after overrides.

Do not overwrite `capabilities` or `source_metadata` with effective policy values.

### 2. Preserve fields in `ModelCatalogCache`

Update `ModelCatalogCache.update_from_account()` so provider-specific entries retain both structures:

```python
"discovered_limits": model.get("discovered_limits", {}),
"effective_limits": model.get("effective_limits", {}),
```

The global first-seen entry may also retain them for backward compatibility, but no provider-sensitive behavior may depend on the global entry when provider metadata exists.

### 3. Fix unsuffixed exposure deterministically

`get_models_for_exposure()` currently chooses the first provider-specific entry with a protocol from a set of visible accounts. That behavior is adequate for display metadata but not for context limits.

Change only the limit selection behavior:

- Continue selecting display/protocol metadata according to existing rules unless a separate cleanup is warranted.
- Collect each distinct visible provider's `effective_limits` for the model.
- Replace the exposed model copy's `effective_limits` with the conservative merge.
- Include an optional `eligible_provider_ids` field for diagnostics if useful.

This prevents a high-limit provider from being selected for metadata while routing later chooses a lower-limit provider.

### 4. Preserve exact provider limits for suffixed exposure

`get_provider_suffixed_models()` already works with `(model_id, provider_id)` entries and emits `model-id/provider-id`. Ensure it carries the provider's exact `effective_limits` without conservative merging.

Example:

```text
MiniMax-M3/opencode-go -> 220000
MiniMax-M3/minimax     -> 512000
```

### 5. Handle persisted catalog state

The current `models` table persists capabilities and source metadata, while provider-specific entries are reconstructed from global metadata before the next live refresh. Provider-specific effective limits are configuration-derived and should not require a migration.

On cache load:

- Load existing model rows as today.
- When constructing provider-specific entries from persisted global data, rerun `ModelLimitResolver` for each `(model_id, provider_id)` using the loaded capabilities/source metadata.
- Do not persist effective limits as authoritative database state. Configuration must remain the source of truth.
- If discovered limits are not separately persisted, reconstruct them from capabilities/source metadata.

This avoids stale effective policy after changing `config.toml` and restarting.

### 6. Configuration reload behavior

`reload_config()` currently updates `app.state.config`, repositories, and the account registry, but long-lived services may retain the old config object.

Do not claim context policy hot reload works unless all of the following are updated atomically:

- `CatalogService` configuration and resolver.
- Router or coordinator references used for enforcement.
- Generated/exposed model metadata.

The safest initial behavior is to require restart, consistent with the README's stated operational model. If SIGHUP remains supported for account synchronization, document that model-limit changes require process restart. A later change can add explicit service-level `replace_config()` methods.

## Client-facing model metadata

### 1. Keep `/v1/models` OpenAI compatible

Do not rely on custom `/v1/models` fields as the sole OpenCode integration. OpenCode does not derive compaction thresholds from arbitrary OpenAI model list extensions.

It is still useful to add a namespaced extension for observability:

```json
{
  "id": "MiniMax-M3/opencode-go",
  "object": "model",
  "created": 0,
  "owned_by": "opencode-go",
  "name": "MiniMax M3",
  "eggpool": {
    "base_model_id": "MiniMax-M3",
    "provider_id": "opencode-go",
    "limits": {
      "context": 220000,
      "input": null,
      "output": 16384
    }
  }
}
```

Compatibility constraints:

- Existing top-level fields must remain unchanged.
- Unknown extension fields must not alter routing.
- Omit `eggpool.limits` when all values are unknown, or return explicit nulls consistently; choose one behavior and test it.
- Set `owned_by` to the resolved provider where available rather than hard-coding `opencode`, but treat that as optional cleanup if it expands scope.

### 2. Add an internal serialization helper

Avoid constructing model dictionaries directly inside the FastAPI route. Create a helper such as:

```python
def serialize_openai_model(model: Mapping[str, object]) -> dict[str, object]:
    ...
```

Place it in `src/eggpool/api/models.py` and have `app.py` call it. This makes extension behavior testable without booting the application.

## OpenCode configuration generation

This is the critical integration for proactive compaction.

### 1. Replace the current hand-built legacy snippet

`configsetup_opencode()` currently emits:

```json
{
  "providers": {
    "eggpool": {
      "api_key": "...",
      "base_url": "http://host:port/v1"
    }
  }
}
```

Update it to current OpenCode provider syntax and serialize with `json.dumps()` rather than string concatenation. Use a structure equivalent to:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "eggpool": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "EggPool",
      "options": {
        "baseURL": "http://host:port/v1",
        "apiKey": "ep_..."
      },
      "models": {
        "MiniMax-M3/opencode-go": {
          "name": "MiniMax M3",
          "limit": {
            "context": 220000,
            "output": 16384
          }
        }
      }
    }
  }
}
```

Confirm the exact OpenCode schema keys against the version targeted by the project before implementation. The current code's plural `providers`, `api_key`, and `base_url` are likely not the current native configuration shape.

### 2. Generate models from the live or persisted catalog

`configsetup opencode` needs model data. Implement a helper service instead of embedding database/network logic in Click:

```python
def build_opencode_provider_config(
    *,
    base_url: str,
    api_key: str,
    models: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    ...
```

Preferred CLI behavior:

1. Load and structurally validate `AppConfig`.
2. Open the configured SQLite database read-only or through the existing database abstraction.
3. Load persisted catalog metadata.
4. Reapply current configuration overrides through `ModelLimitResolver`.
5. Generate both unsuffixed and provider-suffixed model definitions according to the configured exposure behavior.
6. Print deterministic, indented JSON.

Do not require valid upstream credentials merely to generate config from an existing catalog.

If the catalog is empty, generate the provider connection block and print a warning explaining that `eggpool models refresh` or a server startup is required before model-specific limits can be included.

### 3. Include only known values

For each OpenCode model:

```python
limit: dict[str, int] = {}
if context_tokens is not None:
    limit["context"] = context_tokens
if input_tokens is not None:
    limit["input"] = input_tokens
if output_tokens is not None:
    limit["output"] = output_tokens
```

Do not emit zero as a placeholder. In OpenCode, a zero context limit may disable overflow checks rather than mean unknown.

### 4. Preserve routing IDs

The generated model map key must exactly equal the model ID clients send to EggPool:

- Unsuffixed exposure: `MiniMax-M3`.
- Provider-suffixed exposure: `MiniMax-M3/opencode-go`.

The display name can remain human-readable and does not participate in routing.

### 5. Make output deterministic

Sort model IDs lexicographically before JSON serialization. Deterministic output simplifies tests, diffs, and operator updates.

### 6. Consider a machine-readable output flag

Add `--json-only` or make stdout contain only JSON while status/warnings go to stderr. Clipboard status must go to stderr. This allows:

```bash
eggpool --config config.toml configsetup opencode --json-only > /tmp/eggpool-opencode.json
```

Do not silently overwrite the user's OpenCode configuration in this change.

## Request-side enforcement

Implement this after advertisement and config generation are correct. It is a guardrail, not the primary behavior.

### 1. Add a protocol-neutral error

In `src/eggpool/errors.py`, add:

```python
class ContextLimitExceededError(AggregatorError):
    def __init__(
        self,
        *,
        model_id: str,
        estimated_input_tokens: int,
        requested_output_tokens: int | None,
        max_context_tokens: int | None,
        max_input_tokens: int | None,
    ) -> None:
        ...
```

Keep structured properties on the exception so OpenAI and Anthropic handlers can serialize protocol-appropriate envelopes.

Return HTTP 400, not 413. This is a semantic token-limit error, not an HTTP body byte-size error.

### 2. Resolve limits after the route target is known

Provider-specific enforcement must use the provider chosen for the attempt, not a random global catalog entry.

Integrate at the narrowest point where all of these are available:

- Base model ID.
- Selected account.
- Selected provider ID.
- Parsed request body.
- Requested output-token value.
- Provider-specific model metadata.

Inspect `src/eggpool/request/coordinator.py` and the attempt construction path before implementing. Prefer a small validator called immediately before forwarding an attempt upstream. This allows retries to another provider to validate against that provider's policy.

### 3. Do not build a universal tokenizer in this pass

Use a conservative estimator abstraction:

```python
class RequestTokenEstimator(Protocol):
    def estimate_input_tokens(
        self,
        *,
        protocol: Literal["openai", "anthropic"],
        body: Mapping[str, object],
        model_id: str,
    ) -> int | None:
        ...
```

Initial implementation options, in priority order:

1. Reuse any existing token/cost estimation path already parsing request content.
2. Use provider-reported usage only for post-request accounting, not preflight enforcement.
3. If no credible preflight estimator exists, enforce only declared output limits initially and log that context enforcement is unavailable.

Do not reject based on a crude character ratio without a documented safety margin. False rejection is worse than allowing the upstream to return its own context error.

### 4. Add configurable estimation tolerance only if needed

If a preflight estimator is available but approximate, introduce a single global safety setting rather than provider-specific magic constants:

```toml
[models]
context_estimation_tolerance = 0.02
```

Validation threshold:

```text
estimated total > max_context_tokens * (1 + tolerance)
```

The advertised OpenCode context remains exactly the configured value. Tolerance applies only to server-side rejection.

### 5. Extract output request values by protocol

OpenAI-compatible requests may use `max_tokens` or `max_completion_tokens`. Anthropic requests use `max_tokens`.

Normalize these into one value before validation. If absent, use the effective model output maximum when calculating worst-case total context only if that matches current upstream behavior. Otherwise validate input independently and let OpenCode's advertised metadata handle output reservation.

### 6. Protocol-specific API errors

OpenAI endpoint response:

```json
{
  "error": {
    "message": "Estimated request context exceeds EggPool's configured 220000-token limit for MiniMax-M3/opencode-go",
    "type": "invalid_request_error",
    "code": "context_length_exceeded"
  }
}
```

Anthropic endpoint response:

```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "Estimated request context exceeds EggPool's configured 220000-token limit for MiniMax-M3/opencode-go"
  }
}
```

Do not route this error through the generic 502 path.

### 7. Observability

Record rejected requests with a distinct failure classification, for example:

```text
context_limit_exceeded
```

Do not persist request content. Record only:

- Model ID.
- Provider ID if selected.
- Estimated input tokens.
- Requested output tokens.
- Effective limits.
- Estimator name/version.

Avoid counting a preflight policy rejection as an upstream provider failure or circuit-breaker event.

## Dashboard and stats

This can be included in the same feature if the current dashboard model table is straightforward to extend; otherwise make it a follow-up.

Add columns or detail fields for:

- Discovered context.
- Effective context.
- Effective input limit.
- Effective output limit.
- Limit source.
- Enforcement enabled.

Display mismatches clearly, for example:

```text
Upstream: 1,000,000
Effective: 220,000
Source: provider override
```

Do not label effective context as upstream capacity.

## Documentation changes

### `config.example.toml`

Add a commented provider-specific example under OpenCode Go:

```toml
# [providers.opencode-go.model_overrides."MiniMax-M3"]
# max_context_tokens = 220000
# max_output_tokens = 16384
# enforce_context_limit = true
```

Add a global override example and explain precedence.

### `README.md`

Update the configuration section to mention:

- Global model overrides.
- Provider-specific model overrides.
- Effective versus physical context.
- `eggpool configsetup opencode` regenerates explicit model metadata.
- OpenCode must consume the generated model definitions for proactive compaction.
- Configuration changes require restart.

### New operator documentation

Create `docs/model-limits.md` covering:

- Why effective context limits exist.
- MiniMax long-context cost/latency use case.
- Exact TOML syntax.
- Provider-suffixed versus unsuffixed models.
- Conservative minimum behavior.
- OpenCode compaction headroom.
- Difference between advertised limits and server-side enforcement.
- Regenerating and merging OpenCode config.

Do not hard-code a claim that MiniMax M3 always has a particular physical limit; provider metadata can change.

## Test plan

### Configuration unit tests

Add tests under the existing configuration test module for:

1. Provider-scoped context override parses.
2. Global context override parses.
3. Unknown override field is rejected.
4. Zero and negative token limits are rejected.
5. Output greater than context is rejected.
6. Input greater than context is rejected.
7. Provider override and global override merge per field.
8. Exact model IDs containing dots, slashes, or mixed case are handled as TOML quoted keys.
9. Legacy configurations without model limits remain valid.

### Limit resolver unit tests

Create `tests/unit/catalog/test_limits.py` with table-driven cases:

1. Provider override beats global override.
2. Global override beats discovered metadata.
3. Missing provider override falls back to global.
4. Missing all overrides uses upstream metadata.
5. Numeric strings parse.
6. Booleans do not parse as integers.
7. Non-integral floats are ignored.
8. Capabilities beat source metadata.
9. Per-field provenance is correct.
10. Conservative merge selects the minimum known value.
11. Conservative merge ignores unknown values when known values exist.
12. Conservative merge returns unknown when every value is unknown.

### Catalog cache tests

Extend cache tests for:

1. Provider-specific effective limits survive `update_from_account()`.
2. Two providers can retain different limits for one base model.
3. Suffixed exposure returns each provider's exact limit.
4. Unsuffixed exposure returns the conservative minimum.
5. Account iteration order does not change the exposed limit.
6. Stale provider/account removal updates conservative limits correctly.

### Catalog service tests

Use mocked model-list responses:

1. Upstream metadata is extracted.
2. Provider override is applied during refresh.
3. Raw source metadata remains unchanged.
4. Persisted catalog hydration reapplies current configuration.
5. Restart with a changed limit produces the new effective value without a database migration.

### `/v1/models` contract tests

Verify:

1. Existing required OpenAI fields remain present.
2. Namespaced EggPool metadata is present only as specified.
3. Provider-suffixed IDs include provider-specific limits.
4. Unsuffixed IDs include conservative limits.
5. Unknown limits do not become zero.
6. Existing clients that ignore extensions still receive a valid model list.

### OpenCode config generator tests

Test the pure builder separately from Click:

1. Current OpenCode provider schema is emitted.
2. `baseURL` ends in `/v1` exactly once.
3. API key is placed in the intended options field.
4. Models are sorted.
5. Context/input/output are omitted independently when unknown.
6. Zero is never emitted.
7. Provider-suffixed IDs are preserved exactly.
8. Empty catalog emits a valid provider block.
9. JSON output round-trips through `json.loads()`.
10. Status messages do not contaminate `--json-only` stdout.

### Enforcement tests

Only add these when a credible estimator path exists:

1. Request below context limit is forwarded.
2. Request above context limit returns HTTP 400 without upstream call.
3. Input-specific limit is enforced.
4. Output-specific limit is enforced.
5. Enforcement disabled allows forwarding.
6. Retried provider uses its own limit.
7. Policy rejection does not penalize account health.
8. OpenAI endpoint returns OpenAI error envelope.
9. Anthropic endpoint returns Anthropic error envelope.
10. No request content is persisted.

### End-to-end verification

Perform a manual OpenCode test with a deliberately small temporary limit, such as 8,000 tokens, so compaction can be triggered quickly:

1. Configure the temporary limit for a test model/provider.
2. Refresh the EggPool catalog.
3. Run `eggpool configsetup opencode --json-only`.
4. Merge the generated provider definition into a temporary OpenCode config.
5. Start a long subagent session.
6. Confirm OpenCode displays the configured context size.
7. Confirm OpenCode compacts before exceeding the usable threshold.
8. Confirm EggPool forwards the compacted request.
9. Restore the intended 220,000-token MiniMax M3 limit.

Do not attempt the first manual verification at 220,000 tokens; it is expensive and makes failures slow to diagnose.

## Recommended implementation phases

### Phase 1: Configuration and pure policy

Files:

- `src/eggpool/models/config.py`
- `src/eggpool/catalog/limits.py`
- configuration tests
- `tests/unit/catalog/test_limits.py`

Deliverables:

- Provider and global override parsing.
- Cross-field validation.
- Upstream metadata extraction.
- Effective limit resolver.
- Conservative merge helper.

Acceptance criteria:

- Pure unit tests cover precedence and validation.
- No request path or database behavior changes.

### Phase 2: Catalog propagation and exposure

Files:

- `src/eggpool/catalog/service.py`
- `src/eggpool/catalog/cache.py`
- `src/eggpool/api/models.py` (new)
- `src/eggpool/app.py`
- catalog/cache/API tests

Deliverables:

- Per-provider discovered and effective limits.
- Conservative unsuffixed limits.
- Exact suffixed limits.
- Optional namespaced `/v1/models` metadata.
- Restart-safe reapplication of overrides.

Acceptance criteria:

- Same base model can expose different provider-suffixed context limits.
- Unsuffixed limit is deterministic and conservative.
- No database migration is required.

### Phase 3: OpenCode config generation

Files:

- `src/eggpool/cli.py`
- preferably a new helper module such as `src/eggpool/integrations/opencode.py`
- CLI and integration tests
- README/config documentation

Deliverables:

- Current OpenCode provider syntax.
- Explicit model map with `limit.context`, `limit.input`, and `limit.output`.
- Deterministic JSON.
- Empty-catalog warning behavior.
- Machine-readable output mode.

Acceptance criteria:

- OpenCode resolves MiniMax M3 through EggPool with context `220000`.
- OpenCode's normal overflow/compaction path sees the effective limit.
- Clipboard output and stdout are valid JSON when requested.

### Phase 4: Defensive request enforcement

Files depend on final inspection of the coordinator and attempt path, likely:

- `src/eggpool/request/coordinator.py`
- request parsing/normalization helpers
- `src/eggpool/errors.py`
- `src/eggpool/api/chat_completions.py`
- `src/eggpool/api/messages.py`
- request lifecycle/statistics tests

Deliverables:

- Provider-specific preflight validation where token estimates are credible.
- Protocol-correct HTTP 400 errors.
- No health penalty for local policy rejection.
- Structured rejection telemetry.

Acceptance criteria:

- Stale clients cannot substantially exceed an enforced effective limit.
- Policy rejection cannot be confused with an upstream outage.

### Phase 5: Operational polish

Files:

- `config.example.toml`
- `README.md`
- `docs/model-limits.md`
- dashboard rendering/stats files if included

Deliverables:

- Operator documentation.
- Dashboard visibility.
- Manual OpenCode compaction verification.

## Small-model implementation guidance

Keep these invariants explicit while coding:

1. Always resolve overrides with the base model ID and provider ID separately.
2. Never put provider-specific policy only in the global `_models` cache entry.
3. Never use first-account or first-provider iteration order to choose a context limit.
4. Never emit zero for an unknown OpenCode model limit.
5. Never call server-side rejection "compaction".
6. Never mutate raw provider metadata to represent EggPool policy.
7. Never penalize provider health for a local policy rejection.
8. Never require a database migration for static override fields unless a later product requirement needs historical snapshots.
9. Keep OpenCode schema generation in a pure helper and serialize with `json.dumps()`.
10. Require restart for policy changes until every long-lived consumer can replace its resolver safely.

## Suggested initial MiniMax configuration

Use the exact model ID returned by the OpenCode Go model catalog. Do not assume capitalization or punctuation until confirmed from a live refresh.

```toml
[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
enforce_context_limit = true
```

If the actual catalog ID differs, use that exact base ID instead. After implementation, verify with:

```bash
eggpool --config config.toml models refresh
eggpool --config config.toml configsetup opencode --json-only
```

The generated OpenCode model entry should contain:

```json
"limit": {
  "context": 220000,
  "output": 16384
}
```

OpenCode will reserve output headroom when calculating usable context, so automatic compaction should occur before the full 220,000-token total is consumed. That earlier threshold is expected and is the desired behavior for avoiding the expensive long-context regime.

## Final definition of done

The feature is complete when all of the following are true:

- `config.toml` supports global and provider-specific model context/input/output limits.
- Provider-specific overrides take precedence per field.
- The catalog preserves raw discovered values and separate effective policy values.
- Provider-suffixed models retain independent limits.
- Unsuffixed models advertise deterministic conservative limits.
- `eggpool configsetup opencode` emits explicit OpenCode model limits using current schema.
- OpenCode sees MiniMax M3 as a 220,000-token model and compacts through its native overflow path.
- Unknown limits are not represented as zero.
- Existing configurations and standard `/v1/models` clients remain compatible.
- Optional enforcement returns local protocol-correct errors without harming provider health.
- Tests cover precedence, exposure, config generation, persistence/restart behavior, and enforcement.
- Documentation explains effective versus physical context and restart requirements.
