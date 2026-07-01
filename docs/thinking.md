# Thinking & Reasoning Capability Support

Operator-facing guide for thinking/reasoning support in EggPool.

## 1. Conceptual Model

EggPool models thinking/reasoning as a three-layer problem:

1. **Native support** — a model may support extended thinking natively (e.g. Anthropic's `thinking` blocks, OpenAI's `reasoning_content`).
2. **Transcoding** — EggPool may be able to translate client controls between protocols (OpenAI `reasoning_effort` ↔ Anthropic `thinking` blocks).
3. **Discovery** — clients discover support through `eggpool.capabilities` in `/v1/models`.

Key invariants:

- **Unknown ≠ unsupported.** A status of `"unknown"` means no capability data has been observed. It is explicitly *not* a claim that thinking is unsupported. This avoids false negatives when catalog or model-info data has not yet been populated.
- **No fabrication.** EggPool does not generate hidden reasoning. It only forwards provider-exposed content. If the upstream does not return thinking blocks, the client does not receive them.
- **Protocol compatibility alone does not imply thinking support.** A model reachable via the OpenAI-compatible protocol may still lack thinking support. Capability status is independent of protocol compatibility.

Status values (`CapabilityStatus`):

| Value | Meaning |
|---|---|
| `supported` | Confirmed upstream thinking support (from provider catalog, model-info source, or manual override) |
| `unsupported` | Confirmed upstream does not support thinking |
| `unknown` | No data observed yet — not a claim of either support or lack |
| `mixed` | Some backing providers support thinking, others do not |
| `conflicting` | External sources disagree on support status; requires operator resolution via manual override |

Source: `src/eggpool/catalog/capabilities.py:35-45`

## 2. Enabling Thinking Transcoding

Thinking transcoding is **off by default**. The feature gate lives in `[transcoder.features]`:

```toml
[transcoder.features]
thinking = true
```

When `thinking = false` (the default), the transcoder **drops** thinking-related fields with a structured `reasoning_content_dropped` warning rather than forwarding them. Specifically:

- **OpenAI → Anthropic**: `reasoning_content` in assistant messages is dropped.
- **Anthropic → OpenAI**: `thinking` content blocks are dropped.
- **Streaming**: thinking stream deltas are dropped.

The `loss_policy` setting in `[transcoder]` controls whether dropped fields cause a request rejection or a warning log:

- `loss_policy = "warn"` (default): logs structured warnings per request but proceeds.
- `loss_policy = "reject"`: returns HTTP 400 when request-body translation would drop or alter thinking fields before upstream dispatch.

Example config for strict loss policy:

```toml
[transcoder]
loss_policy = "reject"
```

Source: `src/eggpool/transcoder/policy.py:217-225`

## 3. Configuration Reference

### Feature Gate

```toml
[transcoder.features]
thinking = true          # Enable thinking/reasoning transcoding (default: false)
```

### Budget Defaults

Global effort→budget token mapping, used when the model's capability does not carry a per-model mapping:

```toml
[transcoder.thinking_budget_defaults]
low = 1024
medium = 4096
high = 16384
```

All values must be > 0. See [Budget Mapping](#7-budget-mapping) for resolution order.

### Budget Resolution Policy

```toml
[transcoder]
budget_resolution_policy = "lenient"   # default
```

- `"lenient"`: uses a conservative fallback (4096 tokens) for unknown effort levels and allows budget clamping.
- `"strict"`: rejects unknown effort levels and clamped budgets with `BudgetResolutionError` (HTTP 400) before dispatch.

### OpenAI-Compatible Reasoning Fields

Controls which JSON field names EggPool emits for reasoning content in OpenAI-compatible responses:

```toml
[transcoder.openai_reasoning_fields]
non_stream = ["reasoning_content"]          # default
stream_delta = ["reasoning"]                # default
emit_compat_aliases = false                # default
```

When `emit_compat_aliases = true`, additional field names beyond the primary entry are emitted. Only enable when all downstream clients tolerate extra fields.

### Capability Policy

Controls how requests with explicit thinking controls are routed when the candidate model's capability status is not `"supported"`:

```toml
[transcoder.capability_policy]
unsupported_thinking = "reject"             # default: reject | warn_drop | route_best_effort
unknown_thinking = "reject"                 # default: reject | allow_with_warning | route_best_effort
mixed_collapsed_thinking = "filter"         # default: filter | reject | allow
```

See [Routing Policy](#6-routing-policy) for full details.

### Complete Example

```toml
[transcoder]
enabled = true
loss_policy = "warn"
prefer_native = true
budget_resolution_policy = "lenient"

[transcoder.features]
thinking = true
tools = true

[transcoder.thinking_budget_defaults]
low = 1024
medium = 4096
high = 16384

[transcoder.capability_policy]
unsupported_thinking = "reject"
unknown_thinking = "reject"
mixed_collapsed_thinking = "filter"

[transcoder.openai_reasoning_fields]
non_stream = ["reasoning_content"]
stream_delta = ["reasoning"]
emit_compat_aliases = false
```

## 4. Capability Overrides

Operators can override discovered capability data with manual TOML configuration. Overrides are applied in a 3-layer chain with increasing precedence:

1. **Defaults / discovered data** (from provider catalog, model-info sources)
2. **Global overrides** — `[model_capabilities."<model_id>".thinking]`
3. **Provider-scoped overrides** — `[providers.<id>.model_capabilities."<model_id>".thinking]`

Provider-scoped overrides take highest precedence when the provider ID matches.

### Override Fields

```toml
[model_capabilities."gpt-4o".thinking]
status = "supported"
source = "manual_override"
native_protocols = ["openai"]
budget_tokens_min = 1024
budget_tokens_max = 32768
effort_to_budget_tokens = { low = 512, medium = 2048, high = 8192 }
notes = "Operator-confirmed thinking support via OpenAI"
```

| Field | Type | Description |
|---|---|---|
| `status` | string | `"supported"`, `"unsupported"`, `"unknown"`, `"mixed"`, or `"conflicting"` |
| `source` | string | Defaults to `"manual_override"` when `status` is set |
| `native_protocols` | list | `"openai"` and/or `"anthropic"` |
| `budget_tokens_min` | int | Minimum budget (must be > 0) |
| `budget_tokens_max` | int | Maximum budget (must be > 0, ≥ min) |
| `effort_to_budget_tokens` | dict | Custom effort→budget mapping (e.g. `{ low = 512, medium = 2048 }`) |
| `notes` | string | Operator notes |

When `status = None`, the entire override is a no-op — all other fields are cleared.

### Provider-Scoped Example

```toml
[providers.anthropic-prod.model_capabilities."claude-sonnet-4-20250514".thinking]
status = "supported"
native_protocols = ["anthropic"]
budget_tokens_min = 1024
budget_tokens_max = 128000
effort_to_budget_tokens = { low = 1024, medium = 10000, high = 128000 }
```

Source: `src/eggpool/models/config.py:666-737`, `src/eggpool/catalog/capabilities.py:485-513`

## 5. `/v1/models` Metadata

Capabilities are exposed under `eggpool.capabilities.thinking` in each model object. The serialization is compact — unknown/empty values are omitted.

### Provider-Scoped Entry (full shape)

When `models.collapse_models = false` (the default), each provider gets its own model entry:

```json
{
  "id": "claude-sonnet-4-20250514/anthropic-prod",
  "object": "model",
  "owned_by": "anthropic-prod",
  "name": "Claude Sonnet 4",
  "eggpool": {
    "provider_id": "anthropic-prod",
    "base_model_id": "claude-sonnet-4-20250514",
    "capabilities": {
      "thinking": {
        "status": "supported",
        "source": "provider_catalog",
        "native_protocols": ["anthropic"],
        "anthropic_request_fields": ["thinking", "thinking_budget"],
        "anthropic_response_fields": ["thinking"],
        "anthropic_stream_delta_fields": ["thinking"],
        "anthropic_response_block_types": ["thinking"],
        "budget_tokens_min": 1024,
        "budget_tokens_max": 128000,
        "effort_to_budget_tokens": {"low": 1024, "medium": 10000, "high": 128000}
      }
    }
  }
}
```

### Collapsed Entry with Mixed Status

When `models.collapse_models = true`, a single entry aggregates all providers:

```json
{
  "id": "claude-sonnet-4-20250514",
  "object": "model",
  "eggpool": {
    "providers": ["anthropic-prod", "anthropic-readonly"],
    "routing_priority_max": 5,
    "capabilities": {
      "thinking": {
        "status": "mixed",
        "source": "aggregate",
        "native_protocols": ["anthropic"],
        "providers": {
          "anthropic-prod": "supported",
          "anthropic-readonly": "unsupported"
        }
      }
    }
  }
}
```

The `providers` dict in the thinking block shows per-provider status so clients can understand why the aggregate is `mixed`.

Source: `src/eggpool/catalog/capabilities.py:322-372`

## 6. Routing Policy

`[transcoder.capability_policy]` controls how requests with explicit thinking/reasoning controls are routed when candidate models have non-`"supported"` status.

### Per-Status Policies

**`unsupported_thinking`** — how to handle candidates whose thinking status is `"unsupported"`:

| Value | Behavior |
|---|---|
| `reject` (default) | Excludes the candidate from routing |
| `warn_drop` | Routes but logs a warning |
| `route_best_effort` | Ignores the status entirely |

**`unknown_thinking`** — how to handle candidates whose thinking status is `"unknown"`:

| Value | Behavior |
|---|---|
| `reject` (default) | Excludes the candidate |
| `allow_with_warning` | Routes but logs a warning |
| `route_best_effort` | Ignores the status entirely |

**`mixed_collapsed_thinking`** — how to handle collapsed-model entries with mixed provider support:

| Value | Behavior |
|---|---|
| `filter` (default) | Narrows to only supported providers; if none remain, falls through to rejection |
| `reject` | Excludes the model entirely |
| `allow` | Ignores the status entirely |

### Always-Rejected Status

`"conflicting"` status is **always rejected** regardless of policy settings. Operators resolve conflicts by setting a manual override that clears the conflict (see [Capability Overrides](#4-capability-overrides)).

### Error Distinction

| Error | HTTP Status | When |
|---|---|---|
| `CapabilityError` | 400 | No compatible upstream found for a thinking request |
| `ModelNotFoundError` | 404 | Model does not exist in the catalog |
| `ModelUnavailableError` | 503 | Model exists but is currently unavailable (health, quota, etc.) |

Source: `src/eggpool/catalog/capabilities.py:783-819`, `src/eggpool/errors.py:98-112`

## 7. Budget Mapping

The budget resolver translates client-side effort levels or explicit budgets into a concrete token count.

### Default Mapping

When no per-model or global config overrides apply:

| Effort Level | Budget Tokens |
|---|---|
| `low` | 1024 |
| `medium` | 4096 |
| `high` | 16384 |

Source: `src/eggpool/transcoder/budget_resolver.py:228`

### Resolution Order

The resolver evaluates these sources in order, stopping at the first match:

1. **Explicit `budget_tokens`** (Anthropic style) — validated and clamped to capability min/max.
2. **`reasoning_effort` via capability mapping** — looks up the effort in `ThinkingCapability.effort_to_budget_tokens`.
3. **Global config defaults** — `[transcoder.thinking_budget_defaults]`.
4. **Hard-coded fallback** — `low=1024, medium=4096, high=16384`.
5. **Unknown effort** — if the effort string is not recognized:
   - `"lenient"` mode: uses 4096 as a conservative fallback.
   - `"strict"` mode: raises `BudgetResolutionError` (HTTP 400).

### Clamping

When `budget_tokens_min` or `budget_tokens_max` are known (from capability data or overrides), the resolved budget is clamped to that range:

- Budgets below `min` are raised to `min`.
- Budgets above `max` are lowered to `max`.

Under `"strict"` policy, clamped budgets cause rejection. Under `"lenient"` policy, clamping is silently applied with a warning.

Source: `src/eggpool/transcoder/budget_resolver.py:296-341`

## 8. Client Examples

### OpenAI-Style Request (reasoning_effort)

```json
{
  "model": "minimax-m3/minimax",
  "messages": [
    {"role": "user", "content": "Solve this carefully."}
  ],
  "reasoning_effort": "medium"
}
```

EggPool resolves `"medium"` → 4096 tokens (from global defaults) and forwards as an Anthropic `thinking` block to the upstream provider.

### Anthropic-Style Request (thinking)

```json
{
  "model": "minimax-m3/minimax",
  "messages": [
    {"role": "user", "content": "Solve this carefully."}
  ],
  "thinking": {
    "type": "enabled",
    "budget_tokens": 4096
  }
}
```

EggPool receives the explicit budget, validates and clamps it against capability min/max, then forwards to the upstream provider.

### Streaming Request

For streaming, include `"stream": true` as usual. Thinking stream deltas are translated between protocols automatically:

- Anthropic `thinking` deltas → OpenAI `reasoning` delta field (configurable via `[transcoder.openai_reasoning_fields]`).
- OpenAI `reasoning` deltas → Anthropic `thinking` deltas.

## 9. OpenCode Integration

`eggpool configsetup opencode` generates an OpenCode-compatible configuration that includes thinking annotations for discovered models.

### Discovery Logic

The generator (`src/eggpool/integrations/opencode.py`) inspects each model's `capabilities.thinking.status`:

- **`"supported"`** → emits `"thinking": "supported"` in the model entry.
- **All other statuses** (`"unknown"`, `"unsupported"`, `"mixed"`, `"conflicting"`) → the `thinking` field is **omitted**.

This means the generated config never claims thinking support for models without confirmed upstream backing.

### Provider-Scoped Model IDs

When `collapse_models = false`, the generator renders provider-suffixed model IDs (e.g. `gpt-4o/openai`) so OpenCode's model picker disambiguates providers serving the same upstream model.

### Example Output

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "eggpool": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "EggPool",
      "options": {
        "baseURL": "http://localhost:11300/v1",
        "apiKey": "ep_..."
      },
      "models": {
        "claude-sonnet-4-20250514/anthropic-prod": {
          "name": "Claude Sonnet 4/anthropic-prod",
          "thinking": "supported",
          "limit": { "context": 200000 }
        },
        "gpt-4o/openai": {
          "limit": { "context": 128000 }
        }
      }
    }
  }
}
```

Note that `gpt-4o/openai` has no `thinking` field — its capability status is `unknown` or `unsupported`, so the annotation is omitted.

Source: `src/eggpool/integrations/opencode.py:12-30`

## 10. Observability

### In-Memory Counters

`ThinkingMetricsCounter` (`src/eggpool/metrics/thinking.py`) tracks per-request thinking decisions using pipe-delimited label keys:

| Counter Category | Key Format | Example |
|---|---|---|
| `requested` | `requested\|{client_protocol}` | `requested\|openai` |
| `transcoded` | `transcoded\|{client}\|{upstream}\|{provider}` | `transcoded\|openai\|anthropic\|anthropic-prod` |
| `dropped` | `dropped\|{client}\|{upstream}\|{reason}` | `dropped\|anthropic\|openai\|reasoning_content_dropped` |
| `rejected` | `rejected\|{client}\|{capability_status}` | `rejected\|openai\|unsupported` |
| `unknown_capability` | `unknown_capability\|{client}` | `unknown_capability\|openai` |
| `unsupported_capability` | `unsupported_capability\|{client}` | `unsupported_capability\|openai` |
| `budget_clamped` | `budget_clamped\|{client}\|{provider}` | `budget_clamped\|openai\|anthropic-prod` |

Counters are **in-memory only** and reset on restart. They complement the durable `usage_rollups` table.

### Per-Request Trace

Every request that involves thinking decisions stores a `thinking_trace_json` column on the `requests` table (migration `0039`). This contains the structured `ThinkingMetricEvent` for diagnostic inspection.

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/stats/thinking` | Returns in-memory counter snapshot with per-decision breakdown |
| `GET /api/stats/recent/{id}` | Includes `thinking_trace_json` in the request trace |
| `GET /api/stats/runtime` | Includes `thinking_metrics` in the runtime metrics block |

### Dashboard

The overview page shows a **Thinking/Reasoning** stat card when counters are non-zero. It displays total thinking requests with a breakdown: requested, transcoded, dropped, rejected, unknown-cap, unsupported-cap, and budget-clamped counts.

Source: `src/eggpool/metrics/thinking.py`, `src/eggpool/api/stats.py:452-463`, `src/eggpool/dashboard/render.py:1521-1579`

## 11. Troubleshooting

### `reasoning_effort` is ignored

**Symptoms:** Client sends `reasoning_effort` but the upstream receives no thinking controls.

**Cause:** Thinking transcoding is disabled. The feature gate `[transcoder.features]` defaults to `thinking = false`.

**Fix:** Enable the feature:

```toml
[transcoder.features]
thinking = true
```

Restart the service after configuration changes.

---

### Model listing shows `unknown` support

**Symptoms:** `/v1/models` returns `thinking.status = "unknown"` for a model you believe supports thinking.

**Cause:** No capability data has been populated yet. This is the default state — `"unknown"` means "no data observed", not "unsupported".

**Fix:** Wait for a catalog refresh cycle, or set a manual override:

```toml
[model_capabilities."model-id".thinking]
status = "supported"
native_protocols = ["anthropic"]
```

---

### Request rejected with `capability_error`

**Symptoms:** HTTP 400 with `CapabilityError` when sending a thinking request.

**Cause:** No compatible upstream account was found. All candidates were filtered by the capability policy (e.g. `unsupported_thinking = "reject"` with no `"supported"` providers).

**Fix:**
- Check the model's capability status in `/v1/models`.
- Verify `[transcoder.capability_policy]` settings.
- If the model does support thinking but status is wrong, set a manual override.
- If you want best-effort routing, change `unknown_thinking` or `unsupported_thinking` to `route_best_effort`.

---

### Collapsed model routes inconsistently

**Symptoms:** A collapsed model (collapse_models = true) sometimes routes to a thinking-capable provider, sometimes not.

**Cause:** The aggregate status is `"mixed"` — some providers support thinking, others do not. The default `mixed_collapsed_thinking = "filter"` narrows to supported providers, but if the supported provider is unavailable, the request may fall through.

**Fix:**
- Check per-provider status in the `providers` field of the collapsed model's capabilities.
- Set provider-scoped overrides to resolve inconsistencies.
- Consider using `collapse_models = false` to get explicit provider-scoped routing.

---

### Streaming reasoning deltas are missing

**Symptoms:** Non-streaming responses include reasoning content, but streaming responses do not.

**Cause:** Thinking transcoding is likely disabled for streaming. The feature gate affects both streaming and non-streaming paths.

**Fix:** Ensure `[transcoder.features] thinking = true` is set. Check `[transcoder.openai_reasoning_fields]` to verify the streaming delta field name matches your client's expectations (default: `"reasoning"`).

---

### Provider rejects an apparently supported budget

**Symptoms:** HTTP 400 from upstream with a budget-related error, even though the client sent a seemingly valid budget.

**Cause:** The resolved budget exceeds the provider's actual limits. The capability's `budget_tokens_max` may be incorrect, or the provider may have lower limits than configured.

**Fix:**
- Check the provider's actual limits (documentation or API response).
- Adjust `budget_tokens_max` in the capability override:
  ```toml
  [model_capabilities."model-id".thinking]
  budget_tokens_max = 8192
  ```
- Switch to `budget_resolution_policy = "strict"` to catch clamping before dispatch.

---

## 11. Closing-Pass Hardening

This section documents the semantic hardening applied to thinking/reasoning handling in the **closing pass** (Phase A–G).

### Phase A — Missing Capability Metadata Is `unknown`

Routing now treats a catalog entry with **no** `capabilities.thinking` block as semantically equivalent to an explicit `status = "unknown"`. Previously, missing metadata would silently fall through to `"supported"`, masking misconfiguration.

The helper `extract_thinking_status_from_entry()` (`src/eggpool/catalog/capabilities.py`) is the single source of truth for this classification — both `get_eligible_accounts()` and `Router._collect_gate_status()` route through it.

Operator impact:
- Models with unconfigured thinking capability now participate in the `unknown_thinking` policy evaluation (default: `reject`).
- Add a manual override to opt in:
  ```toml
  [model_capabilities."<model-id>".thinking]
  status = "supported"
  source = "manual_override"
  ```

### Phase B — `BudgetResolutionError` Is a `CapabilityError`

`BudgetResolutionError` (raised when strict policy rejects an unknown effort level or a clamped budget) is now a subclass of `CapabilityError`. The proxy layer's existing `except CapabilityError` handler automatically renders it as **HTTP 400** without any manual mapping code. The error carries rich kwargs (`model_id`, `requested_budget_tokens`, `resolved_budget_tokens`, `budget_resolution_policy`, `reason`, `provider_id`) for diagnostic logging.

### Phase C — Per-Provider Budget Recompute at Dispatch

After route selection but before upstream dispatch, `RequestCoordinator._recompute_thinking_budget_for_selected_provider()` re-runs `resolve_thinking_budget()` against the **selected provider's** capability (not the collapsed model-level one). This means:

- Provider-scoped `[providers.<id>.model_capabilities."<model-id>".thinking]` overrides take effect at dispatch time, not only at preflight translation.
- Strict rejections here flow through the same `CapabilityError` → HTTP 400 path as Phase B.
- The selected capability is recorded on the request trace under `thinking_trace.capability_status` / `thinking_trace.capability_source`.

### Phase D — Trace Decisions + Rejection Attribution

The transcoding trace now records a single string `decision` field with one of:

| Value | Meaning |
|---|---|
| `passthrough` | No thinking-related warnings (native or no thinking controls) |
| `transcoded` | A thinking-related warning present (e.g. budget resolution input) |
| `dropped` | A thinking field was dropped (`reasoning_content_dropped`, `thinking_signature_dropped`, `anthropic_top_level_thinking_dropped`) |
| `clamped` | `budget_clamped` warning present |
| `rejected` | `budget_rejected` warning present (strict policy) |

Rejections from capability-aware routing are attributed with the relevant thinking status (`unknown` vs `unsupported`) so the rejection counter and the operator-facing reason distinguish them.

### Phase E — Top-Level `reasoning_content` Detection

`classify_thinking_request()` now detects top-level `reasoning_content` on assistant messages (string or list). Clients that attach thinking text alongside `content` without going through `reasoning_content` content-blocks are now correctly classified as thinking-required.

### Phase F — `supports_tools` Removed from `ModelCapabilities`

The vestigial `supports_tools: True` field has been removed from `model_capabilities_to_dict()`. Tool support is owned by transcoder features (`[transcoder.features] tools = true`), not by `ModelCapabilities`. Tests pin the removal.

### Phase G — Explicit `anthropic_top_level_thinking_dropped` Kind

When the Anthropic-style top-level `thinking` block is dropped during Anthropic→OpenAI transcoding (no verified mapping), the warning now uses an **explicit** kind `anthropic_top_level_thinking_dropped` instead of the generic `dropped_field` bucket. Operators can configure `loss_policy = "reject"` per-subsystem and have this drop attributed accurately to the thinking trace.

### Phase H — Final Provider Budget Cleanup

This final cleanup pass closes two semantic gaps left after Phase C:

#### Original Client Intent Is Preserved Through Translation

The post-selection recompute previously forwarded the **already-translated** Anthropic `thinking.budget_tokens` value as both `requested_effort` and `requested_budget_tokens` to `resolve_thinking_budget()`. Because the resolver prioritises `requested_budget_tokens` over `requested_effort`, the selected provider's `effort_to_budget_tokens` mapping was silently bypassed for OpenAI `reasoning_effort` clients.

The new `_extract_original_thinking_budget_inputs()` helper parses `context.original_body` (not `context.upstream_body`) and returns either `(effort, None)` for OpenAI clients or `(None, budget)` for Anthropic clients. The recompute now passes **only** the original client intent, so:

- OpenAI `reasoning_effort = "high"` against a provider whose `effort_to_budget_tokens.high = 32768` produces an Anthropic body with `thinking.budget_tokens = 32768` even when global defaults are `16384`.
- Anthropic `thinking.budget_tokens = 50000` against a provider whose `budget_tokens_max = 8192` is still clamped/validated against the **selected** provider's min/max.

#### Post-Selection Capability Rejection Cleans Up State

Phase C noted that strict rejections propagate as `CapabilityError`, but did not handle the durable-state side effects already in flight by the time the recompute runs (request row, attempt row, reservation, active request count, health slot). The new `_finalize_selected_capability_rejection()` helper runs on rejection and:

- Finalizes the attempt row with `release_reason = "capability_rejected"`, `retry_category = "never"`, `status_code = 400`.
- Releases the reservation durably and removes it from the in-memory quota estimator.
- Decrements the router's active request count for the selected account.
- Releases the health-manager probe slot.
- Marks `thinking_trace.decision = "rejected"` and stamps `provider_id` on the trace.
- Finalizes the request row as `client_error` with `thinking_trace_json` persisted.
- Increments the thinking-metrics rejected counter with the rejection reason (`strict_clamp` / `unknown_effort_strict` / `capability_rejected`).
- Does **not** record a health failure — this is a client-validation outcome, not an account health signal.

The streaming and non-streaming dispatch paths share identical cleanup semantics via `_apply_selected_provider_transcode_adjustments()` (Phase 3 of the cleanup plan).

### Phase I — Final Polish

This small polish pass (`plans/thinking_reasoning_final_polish.md`) hardens the trace metadata and the no-health-penalty guarantee after Phase H:

- **`upstream_fields` is always populated when the recompute writes.** `_recompute_thinking_budget_for_selected_provider()` treats an empty list the same as `None` when deciding whether to populate `thinking_trace.upstream_fields = ["thinking"]`. A pre-populated non-empty list is preserved verbatim so future paths can stack additional upstream fields without being clobbered. The preflight translation already populates the field, so this is a defensive change for non-preflight paths (synthetic requests, future providers, alternate code paths).
- **No-health-penalty guarantee is test-pinned.** The strict-rejection cleanup tests now assert `HealthManager.is_account_healthy()` stays `True` and the underlying `AccountHealth` fields (`consecutive_failures`, `health_state`, `disabled_models`, `disabled_until`, `disabled_reason`, `cooldown_until`) stay at their default values after both the non-streaming and streaming cleanup paths, and through a double invocation. Capability rejection remains a client-validation outcome and never records an upstream health signal.

---

## 12. Tests for Closing-Pass Behavior

The closing pass adds regression coverage in:

- `tests/unit/test_capability_routing.py` — Phase A (`extract_thinking_status_from_entry`), Phase E (top-level `reasoning_content`)
- `tests/unit/test_capabilities.py` — Phase A, Phase D (`is_thinking_warning`, `classify_thinking_warning_decision`)
- `tests/unit/test_transcoder/test_budget_resolver.py` — Phase B (`BudgetResolutionError` is a `CapabilityError`)
- `tests/unit/test_transcoder/test_anthropic_to_openai_body.py` — Phase G (explicit kind)
- `tests/unit/test_thinking_budget_provider_cleanup.py` — Phase H (selected-provider effort mapping, clamp validation, strict-rejection cleanup invariants, streaming parity, idempotency) and Phase I (`upstream_fields` population + preservation, no-health-penalty regression)
- `tests/contract/test_transcoder_contract.py` — Phase A integration (annotated `claude-3` with `status = "supported"`)
