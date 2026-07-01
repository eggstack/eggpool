# Phase 2: Canonical Thinking Capability Schema

## Objective

Add a protocol-neutral internal representation for thinking/reasoning capability. EggPool needs a stable capability model before it can expose capabilities in `/v1/models`, route based on explicit thinking requests, or generate correct client configs.

## Problem statement

The current transcoder knows how to translate some fields, but there is no catalog-level model of whether a specific model/provider/account supports thinking controls. This encourages unsafe assumptions such as treating all Anthropic-compatible models as thinking-capable or treating all OpenAI-compatible models as reasoning-capable.

The schema should separate these concepts:

1. Model behavior: whether the model/API actually supports thinking controls.
2. Protocol support: which upstream protocol exposes those controls natively.
3. Transcoder support: whether EggPool can translate client controls into upstream controls.
4. Client exposure: which fields a client can send or receive through EggPool.

## Proposed internal model

Add Pydantic models near the catalog/model metadata layer, not inside a specific transcoder implementation.

Suggested shape:

```python
CapabilityStatus = Literal[
    "supported",
    "unsupported",
    "unknown",
    "mixed",
    "conflicting",
]

CapabilitySource = Literal[
    "provider_catalog",
    "model_info",
    "manual_override",
    "heuristic",
    "aggregate",
    "unknown",
]

ProtocolName = Literal["openai", "anthropic"]

class ThinkingClientControls(BaseModel):
    request_fields: list[str] = Field(default_factory=list)
    response_fields: list[str] = Field(default_factory=list)
    stream_delta_fields: list[str] = Field(default_factory=list)
    response_block_types: list[str] = Field(default_factory=list)

class ThinkingCapability(BaseModel):
    status: CapabilityStatus = "unknown"
    source: CapabilitySource = "unknown"
    native_protocols: list[ProtocolName] = Field(default_factory=list)
    client_controls: dict[ProtocolName, ThinkingClientControls] = Field(default_factory=dict)
    budget_tokens_min: int | None = None
    budget_tokens_max: int | None = None
    effort_to_budget_tokens: dict[str, int] | None = None
    notes: str | None = None

class ModelCapabilities(BaseModel):
    thinking: ThinkingCapability = Field(default_factory=ThinkingCapability)
```

Exact module placement should match the existing catalog and model-info organization.

## Implementation tasks

1. Inspect existing model/catalog metadata classes.
2. Add `ModelCapabilities` and `ThinkingCapability` in a location imported by catalog, serializers, and config parsing without circular imports.
3. Add defaults that preserve current behavior: absent capability means `unknown`, not `unsupported`.
4. Add helpers for:
   - merging provider-scoped and global capabilities;
   - deriving aggregate capability across collapsed providers;
   - serializing compact metadata for `/v1/models`;
   - determining whether a request requires thinking support.
5. Add unit tests for default construction, serialization, merge precedence, and aggregate states.

## Merge semantics

Capability merge order should be deterministic:

1. Built-in safe defaults.
2. Provider catalog/model-info data.
3. Global model overrides.
4. Provider-scoped model overrides.

Manual overrides should win over discovered metadata because the operator may know more than stale catalogs.

## Aggregate semantics

Collapsed model entries may represent multiple providers. Their thinking capability should be:

- `supported` only if every routable backing provider for that exposed model is known supported.
- `unsupported` only if every routable backing provider is known unsupported.
- `unknown` if all backing providers are unknown.
- `mixed` if supported/unsupported/unknown states vary.
- `conflicting` if the same provider/model has incompatible explicit metadata from multiple sources and no override resolves it.

## Acceptance criteria

- A protocol-neutral capability model exists and is importable by catalog, routing, serialization, and config code.
- Missing capability data serializes as `unknown`.
- The schema can represent provider-specific, global, aggregate, and conflicting capability states.
- Tests prove that protocol compatibility alone does not imply thinking support.
- The schema does not break existing model listing or routing behavior when unused.

## Risks

Overfitting the schema too early could make future provider features awkward. Keep the first schema focused on thinking/reasoning but leave room for future capability families such as vision, tools, structured outputs, prompt caching, and logprobs.

## Completion check

Run unit tests for the new model classes and existing catalog/model serialization tests. Confirm no production behavior changes until later phases explicitly consume the new capability data.
