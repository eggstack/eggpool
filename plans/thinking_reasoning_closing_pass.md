# Thinking/Reasoning Capability Closing Pass

## Objective

Close the remaining correctness gaps in EggPool's thinking/reasoning capability implementation so the feature is safe to enable for OpenAI-compatible clients such as opencode and for Anthropic-compatible clients where the upstream model/provider actually supports thinking controls.

The implementation after the roadmap is broadly in the right shape: capability schema, config overrides, `/v1/models` exposure, routing hooks, budget resolver, streaming translation, observability, opencode annotations, docs, and tests are present. This closing pass should focus on semantic hardening rather than adding new surface area.

## Current repo shape

The post-roadmap implementation added the right major subsystems:

- `src/eggpool/catalog/capabilities.py` with typed thinking capability status/source/control/budget metadata.
- Top-level and provider-scoped `model_capabilities` config overrides.
- `/v1/models` namespaced `eggpool.capabilities` serialization.
- Capability-aware routing hooks in `routing/eligibility.py`, `routing/router.py`, and `request/coordinator.py`.
- `transcoder/budget_resolver.py` for effort-to-budget translation, clamping, and strict mode.
- OpenAI→Anthropic `reasoning_effort` to Anthropic `thinking` request conversion.
- Anthropic→OpenAI thinking response and streaming delta field emission.
- In-memory thinking metrics and persisted request trace fields.
- `docs/thinking.md` and expanded operator documentation.
- A substantial new unit/contract test matrix.

The remaining issues are specific and bounded. They are mostly policy-edge and wiring details.

## Non-goals

Do not add new provider integrations in this pass.

Do not infer thinking support from vague marketing language such as "reasoning model" unless a provider/source explicitly documents API controls or an operator override confirms support.

Do not emit hidden/provider-private reasoning. EggPool should only forward provider-exposed thinking/reasoning content.

Do not broaden the OpenAI compatibility layer beyond the fields already configured unless tests prove client compatibility.

## Phase A: Make missing capability metadata fail according to `unknown_thinking`

### Problem

The routing gate currently only evaluates thinking support when a provider model entry contains a `capabilities.thinking` block. If the block is absent, the account can remain eligible even when `[transcoder.capability_policy].unknown_thinking = "reject"`.

That is a fail-open path. Missing metadata should be semantically equivalent to `ThinkingCapability(status="unknown")`.

### Target files

- `src/eggpool/routing/eligibility.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/catalog/capabilities.py`
- `tests/unit/test_capability_routing.py`
- `tests/unit/test_thinking_reasoning_matrix.py`

### Implementation steps

1. Add a helper in `catalog/capabilities.py` if useful:

   ```python
   def extract_thinking_status_from_entry(entry: Mapping[str, object] | None) -> CapabilityStatus:
       if entry is None:
           return "unknown"
       caps_raw = entry.get("capabilities", {})
       if not isinstance(caps_raw, dict):
           return "unknown"
       if "thinking" not in caps_raw:
           return "unknown"
       caps = dict_to_model_capabilities({"thinking": caps_raw["thinking"]})
       return caps.thinking.status
   ```

2. In `get_eligible_accounts()`, always evaluate `check_candidate_thinking_eligibility()` when `thinking_requirement.required` is true, even when capability metadata is absent.
3. In `Router._classify_eligibility()`, mirror the same semantics for diagnostic reason codes.
4. In `_collect_gate_status()`, report `thinking_support = "unknown"` when metadata is absent and the request requires thinking.
5. Ensure `unknown_thinking = "reject"` rejects missing metadata.
6. Ensure `unknown_thinking = "allow_with_warning"` allows missing metadata but logs a warning.
7. Ensure `unknown_thinking = "route_best_effort"` allows missing metadata without warning.

### Acceptance criteria

- A thinking request to a provider/model with no `capabilities` block is rejected by default.
- The router explanation says `thinking_unknown` or equivalent, not generic `no_model` or `no_protocol`.
- The dashboard/diagnostic gate reports `thinking_support = "unknown"`.
- Existing non-thinking requests are unaffected.
- Tests cover absent capability dict, empty capability dict, and explicit `thinking.status = "unknown"`.

## Phase B: Convert strict budget failures into client 400 responses

### Problem

`BudgetResolutionError` can be raised from `OpenAIToAnthropic.encode_request()` when budget policy is strict and the request has an unknown effort or a clamped budget. The visible API error path catches `CapabilityError`, availability errors, upstream exhaustion, and protocol mismatch, but strict budget errors risk escaping as generic server errors.

### Target files

- `src/eggpool/transcoder/budget_resolver.py`
- `src/eggpool/transcoder/openai_to_anthropic.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/api/errors.py`
- `tests/unit/test_transcoder/test_budget_resolver.py`
- `tests/unit/test_thinking_reasoning_matrix.py`

### Implementation steps

1. Decide the error type:
   - Preferred: map strict budget failures to `CapabilityError(capability="thinking")` if the failure is capability-bound, such as budget above max.
   - Alternative: add a dedicated `InvalidThinkingBudgetError` subclass of `EggPoolError` and render it as OpenAI `invalid_request_error` / Anthropic `invalid_request_error`.
2. Do not let raw `BudgetResolutionError` propagate out of request execution.
3. Preserve structured detail: model id, provider id if known, requested effort, requested budget, resolved/clamped budget, policy, and reason.
4. In preflight translation, reject strict budget failures before context-limit checks if possible.
5. In actual dispatch translation, reject with the same client-visible shape.
6. Add tests for:
   - unknown effort with strict policy;
   - budget clamped below min with strict policy;
   - budget clamped above max with strict policy;
   - same requests under lenient policy returning translated payload and warnings.

### Acceptance criteria

- Strict budget failure returns HTTP 400, never 500.
- OpenAI endpoint returns an OpenAI-compatible error body.
- Anthropic endpoint returns an Anthropic-compatible error body.
- Error body includes enough detail for operator debugging but no prompt content.
- Tests assert status code and error type.

## Phase C: Resolve provider-specific budgets after account/provider selection

### Problem

The current OpenAI→Anthropic request translation happens before account selection. It looks up capability by `context.model_id` and passes `provider_id=None` into the budget resolver. This can miss provider-scoped budget overrides for collapsed model IDs.

### Target files

- `src/eggpool/request/coordinator.py`
- `src/eggpool/transcoder/openai_to_anthropic.py`
- `src/eggpool/catalog/cache.py`
- `src/eggpool/catalog/capabilities.py`
- `tests/unit/test_capability_routing.py`
- `tests/unit/test_transcoder/test_budget_resolver.py`
- `tests/unit/test_thinking_reasoning_matrix.py`

### Implementation options

Option 1, preferred: delay final request body transcoding until after account selection.

- Use preflight translation only for approximate validation/context-limit checks.
- Select an account/provider.
- Re-run `transcoder.encode_request()` with the selected provider model entry capability and provider id.
- Dispatch the selected-provider-specific upstream body.

Option 2, minimal patch: re-run only thinking budget injection after selection.

- Keep current early body translation.
- After selection, if the request has `reasoning_effort` and upstream protocol is Anthropic, resolve the selected provider's thinking capability and overwrite `out["thinking"]["budget_tokens"]`.
- This is less clean because it requires mutating already-translated payloads.

### Recommended design

Implement a helper on `RequestCoordinator`:

```python
def _resolve_selected_thinking_capability(
    self,
    *,
    model_id: str,
    provider_id: str,
) -> ThinkingCapability:
    entry = self._catalog.cache.get_provider_model_entry(model_id, provider_id)
    if entry is None:
        return ThinkingCapability()
    return dict_to_model_capabilities(entry.get("capabilities", {})).thinking
```

Then ensure actual dispatch translation uses this capability once `selected.provider_id` is known.

### Acceptance criteria

- Provider-scoped `effort_to_budget_tokens` overrides are honored when calling a collapsed model id.
- Provider-scoped budget min/max clamps are honored when calling a collapsed model id.
- The thinking trace records the selected provider's capability status/source where available.
- Preflight and final dispatch do not disagree silently; if final provider-specific translation rejects, the client receives a 400 before any upstream dispatch.
- Tests configure two providers for the same collapsed model with different high budgets and verify the selected provider's budget is used.

## Phase D: Fix thinking trace and metrics classification

### Problem

The current trace classifier filters warnings by checking whether the warning's `field` contains the string `thinking`. Budget resolver warnings such as `budget_clamped`, `unknown_effort`, and `budget_rejected` may not include a `field`, so clamped/unknown budget cases can be misclassified as plain `transcoded`.

### Target files

- `src/eggpool/request/coordinator.py`
- `src/eggpool/metrics/thinking.py`
- `src/eggpool/db/repositories.py`
- `src/eggpool/request/finalizer.py`
- `tests/unit/test_thinking_metrics.py`
- `tests/unit/test_thinking_reasoning_matrix.py`

### Implementation steps

1. Add a helper that classifies thinking warnings by `kind`, not by `field` substring:

   ```python
   THINKING_WARNING_KINDS = {
       "thinking_signature_dropped",
       "reasoning_content_dropped",
       "budget_clamped",
       "unknown_effort",
       "budget_rejected",
       "budget_resolution_no_input",
   }
   ```

2. Treat `dropped_field` as thinking-related only when `field` is one of `thinking`, `reasoning_effort`, `reasoning`, `reasoning_content`, `thinking_budget`, or contains known thinking block paths.
3. Populate `resolved_budget_tokens` from `ThinkingBudgetResolution` rather than trying to infer it from warnings.
4. Populate `upstream_fields` when `reasoning_effort` is translated to Anthropic `thinking`.
5. Use selected provider id in metrics instead of `"unknown"` where available.
6. Increment `unknown_capability` and `unsupported_capability` counters when candidate filtering rejects for those statuses.
7. Ensure no prompt text or reasoning content is persisted in `thinking_trace_json`.

### Acceptance criteria

- Unknown effort under lenient policy records decision `transcoded` with warning metadata, not `none`.
- Clamped budget records decision `clamped` and increments `budget_clamped`.
- Strict budget rejection records decision `rejected` and increments `thinking_rejected_total` equivalent.
- Unsupported/unknown capability rejections increment distinct counters.
- Persisted request trace contains only metadata, never prompt content or reasoning text.

## Phase E: Detect top-level OpenAI assistant `reasoning_content` in request history

### Problem

The classifier detects some list-content reasoning blocks, but OpenAI-compatible assistant messages commonly carry `reasoning_content` as a top-level field. The transcoder already consumes/emits top-level `reasoning_content`; routing classification should detect it too so history-preservation requests do not route to incompatible providers.

### Target files

- `src/eggpool/catalog/capabilities.py`
- `tests/unit/test_capabilities.py`
- `tests/unit/test_thinking_reasoning_matrix.py`

### Implementation steps

1. In `classify_thinking_request()`, inspect each assistant message for a top-level `reasoning_content` key.
2. Add `reasoning_content` to `fields` when present and non-empty.
3. Keep detection protocol-neutral enough that Anthropic thinking blocks in history are also detected.
4. Avoid false positives on arbitrary user text containing the phrase `reasoning_content`.

### Acceptance criteria

- OpenAI request with assistant top-level `reasoning_content` yields `ThinkingRequestRequirement(required=True, fields=[...])`.
- Plain assistant text content does not trigger thinking requirement.
- Anthropic `content[].type = "thinking"` still triggers thinking requirement.
- Routing uses this classification to enforce capability policy.

## Phase F: Remove accidental `supports_tools` emission from thinking capability serialization

### Problem

`model_capabilities_to_dict()` currently sets `result["supports_tools"] = True` when thinking status is `supported` or `mixed`. Thinking support and tool support are unrelated. This appears to be a copy/paste artifact and can corrupt capability metadata.

### Target files

- `src/eggpool/catalog/capabilities.py`
- `tests/unit/test_capabilities.py`
- `tests/unit/test_api_models.py`

### Implementation steps

1. Remove the `supports_tools` write from `model_capabilities_to_dict()`.
2. Ensure model/tool capability serialization remains handled by the existing catalog/model fields, not the thinking capability block.
3. Add a regression test proving `ThinkingCapability(status="supported")` serializes only thinking metadata and does not set tool support.

### Acceptance criteria

- Thinking support no longer implies `supports_tools` anywhere in serialized capability output.
- `/v1/models` still exposes `eggpool.capabilities.thinking.status` correctly.
- Existing tool-support tests still pass.

## Phase G: Clarify and test Anthropic-client to OpenAI-upstream top-level thinking behavior

### Problem

Anthropic top-level `thinking` is still dropped when transcoding Anthropic client requests to OpenAI upstreams. That may be acceptable because OpenAI-side reasoning-control semantics are not fully compatible, but the behavior must be explicit and tested. EggPool should not claim bidirectional top-level thinking-control translation unless implemented.

### Target files

- `src/eggpool/transcoder/anthropic_to_openai.py`
- `docs/thinking.md`
- `docs/transcoding.md`
- `tests/unit/test_thinking_reasoning_matrix.py`
- `tests/contract/test_transcoder_contract.py`

### Implementation steps

1. Keep current drop behavior unless a verified OpenAI field target exists.
2. Make the warning precise, e.g. `anthropic_top_level_thinking_dropped`, rather than generic `dropped_field/openai_unsupported`.
3. Ensure `loss_policy = "reject"` converts this drop into a 400 during preflight.
4. Update docs to state:
   - OpenAI `reasoning_effort` → Anthropic `thinking` is supported when enabled.
   - Anthropic response `thinking` → OpenAI reasoning fields is supported when enabled.
   - Anthropic top-level request `thinking` → OpenAI upstream request controls is not currently supported unless/until a verified OpenAI-compatible control mapping is added.
5. Add tests for warn and reject modes.

### Acceptance criteria

- Anthropic top-level `thinking` to OpenAI upstream emits a specific structured warning.
- `loss_policy = "reject"` rejects it before dispatch.
- Docs do not imply full bidirectional top-level control support.
- opencode-facing docs remain focused on OpenAI-client request controls.

## Phase H: Tighten tests and run validation

### Required tests

Run, at minimum:

```bash
pytest tests/unit/test_capabilities.py
pytest tests/unit/test_capability_overrides.py
pytest tests/unit/test_capability_routing.py
pytest tests/unit/test_api_models.py
pytest tests/unit/test_thinking_metrics.py
pytest tests/unit/test_thinking_reasoning_matrix.py
pytest tests/unit/test_transcoder/test_budget_resolver.py
pytest tests/unit/test_transcoder/test_reasoning_fields.py
pytest tests/contract/test_transcoder_contract.py
```

Then run the full suite:

```bash
pytest
```

If the repo uses type/lint gates, also run the existing commands from README/CI, such as:

```bash
python -m compileall src tests
ruff check src tests
pyright
```

Only include commands that are already part of the project toolchain or documented developer flow.

### Required new regression tests

1. Thinking request + missing capability metadata + default policy rejects.
2. Thinking request + missing capability metadata + `allow_with_warning` routes and logs/metrics reflect unknown.
3. Strict budget unknown effort returns 400, not 500.
4. Strict budget clamp returns 400, not 500.
5. Collapsed model with provider-specific budget override uses selected provider's budget.
6. Top-level assistant `reasoning_content` triggers thinking requirement.
7. Thinking capability serialization does not emit `supports_tools`.
8. Anthropic top-level request `thinking` to OpenAI upstream has explicit warning/reject behavior.
9. Streaming `thinking_delta` is dropped when `features.thinking = false` and emitted with configured field when true.
10. Metrics classify clamped/rejected/dropped/transcoded paths correctly.

## Manual smoke test matrix

Use synthetic/local provider stubs where possible.

### Smoke 1: OpenAI client to Anthropic upstream, supported model

Request:

```json
{
  "model": "demo-reasoning/demo-anthropic",
  "messages": [{"role": "user", "content": "Think carefully."}],
  "reasoning_effort": "medium"
}
```

Expected:

- Request routes only to providers with `thinking.status = "supported"`.
- Upstream body includes Anthropic `thinking` with resolved `budget_tokens`.
- `/v1/models` shows `eggpool.capabilities.thinking.status = "supported"`.

### Smoke 2: OpenAI client to unknown-support model

Expected under default policy:

- 400 `capability_error` or equivalent controlled client error.
- No upstream dispatch.
- Metrics increment rejected/unknown capability.

### Smoke 3: Collapsed model with mixed support

Expected with default `mixed_collapsed_thinking = "filter"`:

- Supported providers remain eligible.
- Unknown/unsupported providers are filtered out.
- Provider-scoped model ids expose specific capability truth.

### Smoke 4: Strict budget clamp

Expected:

- Request is rejected before upstream dispatch.
- Response is 400, not 500.
- Trace records budget rejection metadata without content.

### Smoke 5: Streaming thinking delta

Expected:

- With `thinking = true`, Anthropic `thinking_delta` becomes configured OpenAI delta field.
- With `thinking = false`, no thinking delta is emitted.
- Text/tool streaming behavior remains unchanged.

## Documentation updates

Update `docs/thinking.md` and `docs/transcoding.md` after implementation to reflect final semantics:

- Missing capability metadata is treated as `unknown` and follows `unknown_thinking` policy.
- Strict budget failures are client 400 errors.
- Provider-scoped overrides are applied after provider selection for collapsed model ids.
- Anthropic top-level request `thinking` to OpenAI upstream is not a supported top-level control mapping unless explicitly implemented.
- Thinking support does not imply tool support.

## Acceptance criteria for the closing pass

This line of work can be considered closed when all of the following are true:

1. No capability metadata fail-open path remains for explicit thinking requests.
2. Strict budget policy failures return controlled client errors.
3. Provider-specific budget overrides are honored after selected provider resolution.
4. Thinking metrics and traces correctly distinguish requested, translated, dropped, rejected, clamped, passthrough, unknown, and unsupported outcomes.
5. OpenAI assistant top-level `reasoning_content` in history is detected and capability-gated.
6. Thinking capability serialization does not mutate unrelated tool-support metadata.
7. Anthropic→OpenAI top-level thinking-control behavior is explicit, documented, and tested.
8. `/v1/models` still exposes provider-scoped and collapsed capability metadata without overclaiming support.
9. opencode config generation still exposes all models and only annotates confirmed thinking support.
10. The targeted test set and full test suite pass locally or in CI.

## Suggested commit breakdown

1. `fix: treat missing thinking metadata as unknown capability`
2. `fix: render strict thinking budget failures as client errors`
3. `fix: resolve thinking budgets with selected provider capabilities`
4. `fix: classify thinking traces by warning kind`
5. `fix: detect assistant reasoning_content in thinking requests`
6. `fix: decouple thinking support from tool support metadata`
7. `docs: clarify anthropic thinking control translation limits`
8. `test: add thinking reasoning closing pass regressions`

Keep commits small enough that a regression in routing, transcoding, or observability can be bisected independently.
