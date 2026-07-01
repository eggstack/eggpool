# Thinking/Reasoning Final Polish Plan

## Objective

Perform a narrow final polish pass on the completed thinking/reasoning implementation. The substantive provider-budget fix appears to have landed: selected-provider `effort_to_budget_tokens` mappings are now resolved from original client intent, and post-selection `CapabilityError` cleanup is wired for both streaming and non-streaming dispatch.

This plan should not reopen the broader roadmap. It targets only small correctness polish, test hardening, and validation evidence before the line of work is considered closed.

## Current state

The current implementation has the important pieces in place:

- `_extract_original_thinking_budget_inputs()` parses `context.original_body` and returns `(effort, None)` for OpenAI `reasoning_effort`, or `(None, budget)` for explicit Anthropic `thinking.budget_tokens` / `thinking_budget`.
- `_recompute_thinking_budget_for_selected_provider()` now passes the original effort/budget pair into `resolve_thinking_budget()` instead of passing the intermediate translated budget as explicit input.
- `_apply_selected_provider_transcode_adjustments()` centralizes selected-provider recompute for streaming and non-streaming dispatch.
- `_finalize_selected_capability_rejection()` finalizes the selected attempt, releases durable and in-memory reservation state, decrements active count, releases the health slot, marks the thinking trace rejected, finalizes the request row as client error, increments rejected thinking metrics, and avoids provider health penalties.
- `tests/unit/test_thinking_budget_provider_cleanup.py` covers provider effort mapping, explicit budget clamp, strict cleanup, streaming parity, and idempotent cleanup behavior.

## Non-goals

Do not change the core routing, transcoder, or capability schema semantics.

Do not add new thinking/reasoning API fields.

Do not rework the budget resolver beyond the polish items below.

Do not add provider-specific hardcoded model knowledge.

## Polish item 1: Always populate `thinking_trace.upstream_fields` when selected-provider recompute succeeds

### Problem

In `_recompute_thinking_budget_for_selected_provider()`, the trace currently populates `upstream_fields = ["thinking"]` only when:

```python
if context.thinking_trace.get("upstream_fields") is None:
    context.thinking_trace["upstream_fields"] = ["thinking"]
```

In the normal preflight path, early translation often sets `upstream_fields`, so this is usually harmless. But the default trace shape uses an empty list:

```python
"upstream_fields": []
```

If a synthetic, future, or alternate path reaches selected-provider recompute with `upstream_fields=[]`, the recompute will not populate it, even though it definitely wrote/validated Anthropic `thinking.budget_tokens`.

### Target file

- `src/eggpool/request/coordinator.py`

### Implementation

Change the condition to treat empty list as missing:

```python
if not context.thinking_trace.get("upstream_fields"):
    context.thinking_trace["upstream_fields"] = ["thinking"]
```

Do not overwrite non-empty values. If a future path adds multiple upstream fields, preserve them.

### Acceptance criteria

- When selected-provider recompute writes a thinking budget and the trace has `upstream_fields=[]`, the final trace contains `upstream_fields=["thinking"]`.
- When the trace already contains a non-empty upstream field list, recompute does not overwrite it.

## Polish item 2: Update tests to pin `upstream_fields` behavior

### Target file

- `tests/unit/test_thinking_budget_provider_cleanup.py`

### Test additions

Add assertions to the selected-provider effort mapping test:

```python
assert ctx.thinking_trace["upstream_fields"] == ["thinking"]
```

That test currently initializes the trace with `upstream_fields=[]`, making it the right regression coverage for this polish item.

Add a second micro-test if needed:

- initialize `upstream_fields=["thinking", "some_future_field"]`;
- run selected-provider recompute;
- assert the list is preserved, not overwritten.

### Acceptance criteria

- The new assertion fails before the code change and passes after it.
- Existing provider-budget tests remain unchanged semantically.

## Polish item 3: Verify cleanup helper cannot double-decrement active count through finalizer result semantics

### Concern

`_finalize_selected_capability_rejection()` decrements active request count only when `finalize_result.reservation_released` is true. The existing idempotency test covers double invocation and proves the second call does not corrupt active count or in-memory quota state.

This item is mostly validation: confirm the invariant is documented and that the test name/comments make the coupling clear.

### Target file

- `tests/unit/test_thinking_budget_provider_cleanup.py`

### Implementation

Review `TestCleanupHelperIdempotent.test_double_cleanup_does_not_corrupt` and add a clarifying comment near the assertion:

```python
# The second finalizer call should return reservation_released=False,
# so active count and in-memory reservation should not be decremented twice.
```

No code change is required if the test already passes.

### Acceptance criteria

- The idempotency test remains explicit about why active count does not go negative.
- No behavior change unless the test exposes a real bug.

## Polish item 4: Confirm no health penalty path is reachable from post-selection capability rejection

### Concern

The cleanup helper intentionally releases the health-manager probe slot without calling `_apply_health_transition()`. The normal execute loop catches `_RetryableUpstreamError` / `_NonRetryableUpstreamError` for health transitions; `CapabilityError` is re-raised after cleanup and should be handled by the API layer, not by upstream health logic.

### Target files

- `src/eggpool/request/coordinator.py`
- `tests/unit/test_thinking_budget_provider_cleanup.py`

### Implementation

Add a regression assertion to strict cleanup tests if the `HealthManager` exposes readable state:

- after cleanup, account should not be marked failed, rate-limited, quota-exhausted, or model-disabled;
- active/probe slot should be released.

If current test scaffolding cannot inspect these directly without brittle private attributes, add a concise comment explaining why no assertion is added.

### Acceptance criteria

- The test suite either asserts health state is not penalized or documents why the helper-level test is sufficient.
- No health transition call is added to the capability cleanup path.

## Polish item 5: Validate targeted and full test suites

### Required targeted tests

Run:

```bash
pytest tests/unit/test_thinking_budget_provider_cleanup.py
pytest tests/unit/test_capability_routing.py
pytest tests/unit/test_thinking_reasoning_matrix.py
pytest tests/unit/test_transcoder/test_budget_resolver.py
pytest tests/contract/test_transcoder_contract.py
```

### Recommended full validation

Run:

```bash
pytest
```

If these are part of the repo's normal gate, also run:

```bash
python -m compileall src tests
ruff check src tests
pyright
```

Do not invent new tooling. Use the repo's existing development/CI workflow.

### Acceptance criteria

- Targeted provider-budget cleanup tests pass.
- Full pytest passes or any failures are clearly unrelated and documented.
- No new lint/type failures are introduced in modified files.

## Implementation checklist

1. Change `if context.thinking_trace.get("upstream_fields") is None:` to `if not context.thinking_trace.get("upstream_fields"):` in `_recompute_thinking_budget_for_selected_provider()`.
2. Add `upstream_fields` assertion to the selected-provider effort mapping test.
3. Add preservation coverage for a pre-populated `upstream_fields` list if simple.
4. Clarify the idempotency test comment around second cleanup call and finalizer result semantics.
5. Add or document health-manager no-penalty assertion.
6. Run targeted tests.
7. Run full pytest.
8. Update `docs/thinking.md` only if the polish changes reveal a documentation mismatch. The existing Phase H docs likely already describe the desired semantics.

## Completion criteria

This polish pass is complete when:

- Selected-provider recompute always leaves `thinking_trace.upstream_fields` accurate when it modifies Anthropic `thinking`.
- The behavior is covered by tests.
- Cleanup idempotency remains covered and understandable.
- Capability rejection remains a client-validation path with no upstream health penalty.
- Targeted tests and full validation have been run or any inability to run them is explicitly recorded in the handoff.

## Suggested commit breakdown

One small commit is sufficient:

```text
fix: polish selected-provider thinking trace metadata
```

If validation/docs changes are substantial, split into:

```text
fix: polish selected-provider thinking trace metadata
test: tighten thinking budget cleanup assertions
docs: record thinking budget cleanup validation
```
