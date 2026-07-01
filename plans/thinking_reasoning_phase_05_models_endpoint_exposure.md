# Phase 5: `/v1/models` Capability Exposure

## Objective

Expose thinking/reasoning capability metadata through `/v1/models` in a way advanced clients such as opencode can inspect while preserving standard OpenAI-compatible model object shape.

## Problem statement

EggPool currently exposes model identity, provider metadata, limits, and model-info summary, but it does not advertise which models support thinking/reasoning controls. A client can only send `reasoning_effort` if it already knows the model supports it. EggPool should make this discoverable through namespaced metadata.

## Design requirements

1. Keep standard OpenAI fields unchanged.
2. Put all EggPool-specific metadata under `eggpool`.
3. Expose provider-scoped capability truth for provider-scoped model ids.
4. Expose aggregate state for collapsed model ids without pretending all backing providers are identical.
5. Include enough protocol/control metadata for clients to know what fields are meaningful.

## Proposed serialized shape

Provider-scoped model:

```json
{
  "id": "minimax-m3/minimax",
  "object": "model",
  "owned_by": "minimax",
  "eggpool": {
    "base_model_id": "minimax-m3",
    "provider_id": "minimax",
    "limits": {
      "context": 220000,
      "output": 8192
    },
    "capabilities": {
      "protocols": ["anthropic"],
      "client_protocols": ["openai", "anthropic"],
      "thinking": {
        "status": "supported",
        "source": "manual_override",
        "native_protocols": ["anthropic"],
        "openai_request_fields": ["reasoning_effort"],
        "openai_response_fields": ["reasoning_content"],
        "openai_stream_delta_fields": ["reasoning"],
        "anthropic_request_fields": ["thinking"],
        "anthropic_response_block_types": ["thinking"],
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

Collapsed model with mixed provider state:

```json
{
  "id": "minimax-m3",
  "eggpool": {
    "capabilities": {
      "thinking": {
        "status": "mixed",
        "providers": {
          "minimax": "supported",
          "openrouter": "unknown"
        }
      }
    }
  }
}
```

## Implementation tasks

1. Inspect `serialize_openai_model()` and current `/v1/models` route.
2. Add capability serialization helpers that convert canonical capability objects into compact JSON-safe dictionaries.
3. Include provider protocol and client protocol reachability where known.
4. Attach `eggpool.capabilities` for provider-scoped model entries.
5. Attach aggregate capability state for collapsed entries.
6. Avoid adding empty capability objects unless the repository convention favors explicit empty metadata.
7. Add tests for provider-scoped, collapsed-supported, collapsed-unknown, and collapsed-mixed cases.

## Aggregate rules

For collapsed models:

- All supported -> `supported`.
- All unsupported -> `unsupported`.
- All unknown -> `unknown`.
- Any mixture -> `mixed`.
- Any unresolved explicit conflict -> `conflicting`, unless the implementation chooses `mixed` plus conflict details.

Expose provider-specific statuses under `providers` for `mixed` and `conflicting` states.

## Acceptance criteria

- `/v1/models` includes `eggpool.capabilities.thinking` where capability metadata is available.
- Existing OpenAI-compatible fields remain unchanged.
- Strict clients that ignore `eggpool` still work.
- Provider-scoped model ids expose provider-specific truth.
- Collapsed model ids do not overstate support when backing providers differ.
- Tests prove unknown capability remains unknown and is not coerced to unsupported.

## Risks

Large model lists may become noisy if capability metadata is too verbose. Keep the first payload compact and omit low-value notes unless debug mode or model detail endpoint exists.

## Completion check

Run the model API tests and manually inspect `/v1/models` with at least one configured thinking override. Confirm the JSON includes `eggpool.capabilities.thinking.status = supported` for that provider-scoped model and does not claim support for unrelated models.
