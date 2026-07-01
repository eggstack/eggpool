# Roadmap: Thinking and Reasoning Capability Exposure

## Purpose

EggPool currently has partial transcoder support for Anthropic thinking blocks and OpenAI-style reasoning fields, but that support is not consistently wired through the runtime path and is not exposed as first-class model metadata. Clients such as opencode can only make reliable use of model-specific thinking modes if EggPool can do three things:

1. Correctly preserve or translate thinking/reasoning controls through the router and transcoder.
2. Know which upstream model/provider/account combinations actually support those controls.
3. Expose that capability through `/v1/models` and generated client configs without overstating support.

This roadmap turns thinking/reasoning support into a cataloged, route-aware, observable capability rather than an incidental request-body field.

## Target end state

EggPool should expose standard OpenAI-compatible model objects while adding namespaced EggPool metadata for advanced clients:

```json
{
  "id": "minimax-m3/minimax",
  "object": "model",
  "owned_by": "minimax",
  "eggpool": {
    "provider_id": "minimax",
    "base_model_id": "minimax-m3",
    "limits": {
      "context": 220000,
      "output": 8192
    },
    "capabilities": {
      "protocols": ["anthropic"],
      "client_protocols": ["openai", "anthropic"],
      "thinking": {
        "status": "supported",
        "native_protocols": ["anthropic"],
        "source": "manual_override",
        "openai": {
          "request_fields": ["reasoning_effort"],
          "response_fields": ["reasoning_content"],
          "stream_delta_fields": ["reasoning"]
        },
        "anthropic": {
          "request_fields": ["thinking"],
          "response_block_types": ["thinking"]
        },
        "effort_to_budget_tokens": {
          "low": 1024,
          "medium": 4096,
          "high": 16384
        }
      }
    }
  }
}
```

The exact schema may evolve, but the core requirement is stable: separate model support, transcoder ability, and client-visible controls.

## Design principles

Thinking support must not be inferred solely from protocol compatibility. An Anthropic-compatible endpoint does not guarantee that every model accepts `thinking`, and an OpenAI-compatible endpoint does not guarantee support for `reasoning_effort` or reasoning deltas.

Capability state must preserve uncertainty. EggPool should distinguish `supported`, `unsupported`, `unknown`, `mixed`, and eventually `conflicting` rather than collapsing everything to booleans.

Manual overrides must exist. Provider catalogs and external model metadata will lag reality, especially for aggregator models and newly released models.

Explicit thinking requests should not silently degrade by default. If a client asks for `reasoning_effort` or Anthropic `thinking`, routing should prefer or require an upstream that can honor the request according to policy.

Model listing must remain OpenAI-compatible. Capability metadata should be namespaced under `eggpool` so strict clients can ignore it safely.

## Phase map

### Phase 1: Runtime transcoder policy wiring

Fix the existing data-plane bug where app startup stores `config.transcoder` but `RequestCoordinator` is constructed without it. Add regression tests proving that actual coordinator translation honors `[transcoder.features].thinking`, not only preflight translation.

### Phase 2: Canonical capability schema

Add internal models for thinking/reasoning capabilities, including status, source, protocol support, client-visible controls, and budget metadata. Keep the schema independent of any one provider protocol.

### Phase 3: Config overrides

Add global and provider-scoped model capability overrides so operators can declare thinking support when catalogs are incomplete. Provider-scoped overrides must take precedence over global overrides.

### Phase 4: Model-info enrichment

Extend the model-info aggregation path to ingest thinking/reasoning capability details when sources expose them. Preserve provenance and confidence; do not convert vague “reasoning model” marketing into API-control support unless the source is explicit.

### Phase 5: `/v1/models` capability exposure

Extend model serialization to include compact `eggpool.capabilities` metadata for provider-scoped and collapsed model listings. Collapsed models must represent mixed provider states accurately.

### Phase 6: Capability-aware routing

Teach the router to detect explicit thinking requests and avoid upstreams that cannot satisfy them. Add configurable policy for unsupported and unknown capability states.

### Phase 7: Budget resolution

Move effort-to-budget translation out of hard-coded transcoder branches into a reusable resolver. Support global defaults, provider/model overrides, capability min/max clamps, warnings, and strict rejection.

### Phase 8: Response-field compatibility

Normalize non-streaming and streaming reasoning output fields for OpenAI-compatible clients. Make field emission configurable enough to support clients that expect `reasoning`, `reasoning_content`, or both where safe.

### Phase 9: `configsetup opencode`

Update opencode config generation so it lists all available exposed models and does not hide thinking-capable provider-scoped models. Generate model settings conservatively based on capability metadata.

### Phase 10: Observability

Add counters, request trace fields, and dashboard/API surfaces for thinking requested/transcoded/dropped/rejected/clamped events and capability sources.

### Phase 11: Test matrix

Add cross-protocol request, response, streaming, model listing, routing, and collapsed-model tests. Ensure tests fail if preflight and actual dispatch use different feature configurations.

### Phase 12: Documentation and migration guidance

Update transcoding, provider, model metadata, and config setup docs with examples, policy behavior, and manual override guidance.

## Suggested implementation order

The implementation should start with Phase 1 because it corrects current runtime behavior. Phases 2 and 3 should follow immediately because they establish a local truth model for capability support. Phase 5 can then expose this truth to clients, while Phase 6 prevents explicit thinking requests from being silently routed to incompatible upstreams.

Phases 7 and 8 refine correctness and compatibility. Phase 9 makes the feature useful for opencode. Phases 10 through 12 close the loop with operator visibility, regression coverage, and documentation.

## Non-goals

This roadmap does not require EggPool to invent or synthesize hidden chain-of-thought. It only forwards or translates reasoning/thinking content that upstream providers expose through their APIs.

This roadmap does not require breaking OpenAI-compatible model objects. Additional metadata must remain namespaced.

This roadmap does not require every provider to support thinking. Unknown or unsupported support should be represented honestly.

## Completion definition

This line of work is complete when an OpenAI-compatible client can inspect `/v1/models`, identify which exposed models support thinking/reasoning controls, send a thinking request, have EggPool route it only to a compatible upstream according to policy, receive non-streaming or streaming reasoning output in configured OpenAI-compatible fields, and observe the decision path through tests, metrics, and docs.
