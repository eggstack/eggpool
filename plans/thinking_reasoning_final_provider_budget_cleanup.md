# Thinking/Reasoning Final Provider Budget Cleanup

## Objective

Close the last two semantic gaps in the thinking/reasoning implementation:

1. Ensure selected-provider `effort_to_budget_tokens` mappings actually override collapsed/global/default mappings for OpenAI `reasoning_effort` requests.
2. Ensure any post-selection capability rejection, especially strict provider-specific budget rejection, cleans up the selected attempt, reservation, active request count, and health slot before returning a controlled client error.

This is a narrow cleanup pass. The current implementation already fixed the broad closing-pass issues: missing capability metadata is treated as `unknown`, strict budget errors are `CapabilityError`s, top-level assistant `reasoning_content` is detected, `supports_tools` was decoupled from thinking support, and Anthropic top-level `thinking` drops have a precise warning kind. This plan only addresses the remaining post-selection budget semantics and lifecycle safety.

## Current behavior to fix

### Gap 1: selected-provider effort mapping can be bypassed

`RequestCoordinator._recompute_thinking_budget_for_selected_provider()` re-resolves the selected provider's budget after account/provider selection. However, it currently parses the already-translated Anthropic `thinking.budget_tokens` and calls `resolve_thinking_budget()` with both:

```python
requested_effort=requested_effort,
requested_budget_tokens=int(budget_value),
```

The budget resolver prioritizes `requested_budget_tokens` before `requested_effort`. That means the previously resolved collapsed/global/default budget is treated as explicit and prevents the selected provider's `effort_to_budget_tokens` mapping from being applied.

Example failure:

- Global/default `high = 16384`.
- Selected provider override has `high = 32768`.
- Client sends OpenAI `reasoning_effort = "high"`.
- Early translation emits `thinking.budget_tokens = 16384`.
- Post-selection recompute sees `requested_budget_tokens = 16384` and keeps it, instead of resolving `high` through the selected provider mapping to `32768`.

### Gap 2: post-selection `CapabilityError` can leave selected state dirty

Post-selection recompute happens after account selection and persistence. By that point EggPool may already have:

- created request/reservation/attempt rows;
- incremented active request count;
- added an in-memory reservation;
- acquired a health-manager request slot.

If `_recompute_thinking_budget_for_selected_provider()` raises `BudgetResolutionError` / `CapabilityError`, the error is raised before upstream dispatch, but the normal upstream-attempt finalization path may not run because the exception is not a retryable/non-retryable upstream wrapper. The request can therefore leave stale pending durable rows or runtime counters.

## Non-goals

Do not redesign the whole transcoder flow.

Do not add new client-facing fields.

Do not change the conservative Anthropic→OpenAI top-level `thinking` drop behavior.

Do not infer thinking capability from provider protocol alone.

## Phase 1: Preserve original client budget intent through translation

### Design

Post-selection recompute must know whether the original client supplied:

- OpenAI effort: `reasoning_effort`;
- Anthropic explicit budget: top-level `thinking.budget_tokens`;
- no explicit thinking budget, only a translated intermediate budget.

The selected-provider recompute should prefer original client intent, not the intermediate translated body.

### Implementation steps

1. Add a helper near `_recompute_thinking_budget_for_selected_provider()`:

   ```python
   def _extract_original_thinking_budget_inputs(
       self,
       context: ProxyRequestContext,
   ) -> tuple[str | None, int | None]:
       ...
   ```

2. Parse `context.original_body`, not `context.upstream_body`.
3. If original body contains OpenAI `reasoning_effort`, return `(effort, None)`.
4. If original body contains Anthropic top-level `thinking.budget_tokens`, return `(None, budget_tokens)`.
5. If original body contains a legacy/direct `thinking_budget`, return `(None, budget)` unless the existing request classifier treats it differently.
6. Only fall back to the already-translated `thinking.budget_tokens` if the original request did not provide `reasoning_effort` or explicit budget and the translated body nevertheless has a thinking block.
7. In `_recompute_thinking_budget_for_selected_provider()`, call `resolve_thinking_budget()` with either:

   ```python
   requested_effort=original_effort,
   requested_budget_tokens=None,
   ```

   for OpenAI effort requests, or:

   ```python
   requested_effort=None,
   requested_budget_tokens=original_budget,
   ```

   for explicit budget requests.

8. Document the branch in comments: OpenAI effort must be re-resolved against the selected provider's `effort_to_budget_tokens`; explicit Anthropic budget must be validated/clamped against the selected provider's min/max.

### Acceptance criteria

- For OpenAI `reasoning_effort`, selected provider `effort_to_budget_tokens` wins over global/default mapping.
- For explicit Anthropic `thinking.budget_tokens`, selected provider min/max validation still applies, but no effort mapping is used.
- Existing early translation remains usable for context-limit preflight.
- Tests prove the selected provider's `high = 32768` mapping is honored when global/default `high = 16384`.

## Phase 2: Move provider-specific recompute into a cleanup-safe selected-attempt wrapper

### Design

Any validation that happens after attempt selection must have the same cleanup guarantees as an upstream attempt failure. If recompute rejects before dispatch, EggPool should finalize the attempt as a client-side capability rejection and undo runtime side effects.

### Implementation options

#### Option A: recompute immediately after selection, inside the same protected branch

After `_select_and_persist_attempt()` returns and before `_execute_upstream()`, run provider-specific recompute in a `try/except CapabilityError` block. On rejection:

1. finalize the attempt as failed/non-retryable/client error;
2. release the reservation;
3. decrement active request count;
4. remove in-memory reservation from quota estimator;
5. release the health-manager slot;
6. finalize the request as error with `thinking_trace_json`;
7. re-raise `CapabilityError` so the API layer returns HTTP 400.

This option keeps the recompute near selection and avoids embedding cleanup inside non-streaming/streaming execution branches.

#### Option B: keep recompute in `_execute_non_streaming()` and `_execute_streaming()` but add cleanup around `_execute_upstream()`

Wrap the `_execute_upstream()` call in the main execute loop with `except CapabilityError as err:`. If `last_selected` exists and the attempt has not been finalized, perform cleanup and re-raise.

This is less localized because `_execute_upstream()` can also raise capability errors for future reasons, but it may be safer as a general post-selection client-rejection cleanup mechanism.

### Recommended approach

Use Option B as a general safety net, and optionally move recompute to immediately after selection if that simplifies invariants.

Add a helper:

```python
async def _finalize_selected_capability_rejection(
    self,
    *,
    context: ProxyRequestContext,
    selected: SelectedAttempt,
    err: CapabilityError,
) -> None:
    ...
```

The helper should be idempotent or guarded so it does not double-finalize if a future caller invokes it after an attempt already transitioned.

### Cleanup details

The helper should:

1. Call `AttemptFinalizer.finalize_failed_attempt()` with:
   - `attempt_id=selected.attempt_id`
   - `reservation_id=selected.reservation_id`
   - `status_code=400`
   - `error_class=type(err).__name__`
   - `release_reason="capability_rejected"`
   - `retry_category=RetryCategory.NEVER.value`
   - `bytes_received=len(context.original_body)`
   - `latency_ms=self._elapsed_ms(context)`
   - `is_retry_outcome=False`
2. If the finalizer reports reservation released, remove the in-memory reservation from quota estimator and decrement active request count.
3. Release the health-manager request slot for `selected.account_name`.
4. Mark `context.thinking_trace["decision"] = "rejected"` when present.
5. Mark `context.thinking_trace["capability_status"]` or a specific budget status such as `budget_rejected`.
6. Finalize the request row as error if needed, preserving `thinking_trace_json`.
7. Do not apply health failure transitions. This is a client/capability validation rejection, not an upstream health problem.

### Acceptance criteria

- A strict provider-specific budget rejection after selection returns HTTP 400.
- No upstream request is dispatched.
- The attempt row is finalized, not left pending.
- The reservation is released durably and removed from in-memory quota state.
- Active request count is decremented.
- Health-manager slot is released.
- No health failure is recorded for the selected upstream account.

## Phase 3: Tighten recompute call sites for both streaming and non-streaming

### Problem

The current recompute call is duplicated in `_execute_non_streaming()` and `_execute_streaming()`. Duplication increases the chance that cleanup semantics diverge.

### Implementation steps

1. Prefer one shared method that runs before either execution branch sends upstream bytes:

   ```python
   def _apply_selected_provider_transcode_adjustments(
       self,
       *,
       context: ProxyRequestContext,
       selected: SelectedAttempt,
   ) -> None:
       ...
   ```

2. Call it once after selection and before entering `_execute_upstream()` if possible.
3. If it must remain inside `_execute_non_streaming()` and `_execute_streaming()`, ensure both call the same helper and both are protected by the same `CapabilityError` cleanup path.
4. Add comments clarifying that this phase is pre-dispatch validation and must not be treated as an upstream failure.

### Acceptance criteria

- Non-streaming and streaming requests use identical selected-provider budget recompute semantics.
- A regression test verifies streaming strict budget rejection cleans up just like non-streaming rejection.
- No duplicate logic remains except minimal call-site branching.

## Phase 4: Add focused regression tests

### Required tests

Add or update tests in the existing thinking/capability test modules.

#### Test 1: selected provider effort mapping wins

Fixture:

- Global/default `high = 16384`.
- Provider A capability: `thinking.status = supported`, `effort_to_budget_tokens.high = 32768`.
- Client request: OpenAI `reasoning_effort = "high"` to collapsed model.
- Router selects Provider A.

Expected:

- Final upstream Anthropic body uses `thinking.budget_tokens = 32768`.
- `thinking_trace.resolved_budget_tokens = 32768`.
- No budget clamping warning unless provider max requires it.

#### Test 2: explicit Anthropic budget is clamped against selected provider

Fixture:

- Client request has Anthropic `thinking.budget_tokens = 50000`.
- Selected provider max is `16384`.
- Lenient policy.

Expected:

- Final upstream body uses `16384`.
- `budget_clamped` warning is recorded.
- `thinking_trace.decision = "clamped"` or equivalent.

#### Test 3: strict selected-provider clamp rejection cleans up

Fixture:

- Same as Test 2, but `budget_resolution_policy = "strict"`.

Expected:

- API returns HTTP 400 capability error.
- No upstream request is sent.
- Attempt is finalized as error/rejected.
- Reservation is released.
- Active request count is decremented.
- Health slot is released.
- Request trace contains `decision = "rejected"` and no prompt/reasoning content.

#### Test 4: streaming strict selected-provider rejection cleans up

Same as Test 3, but `stream = true`.

Expected:

- Same cleanup invariants as non-streaming.
- No lazy stream generator is returned after rejection.

#### Test 5: no cleanup on pre-selection capability rejection

A thinking request rejected before account selection because all candidates are `unknown` should not try to finalize a selected attempt because none exists.

Expected:

- HTTP 400 capability error.
- No attempt row exists.
- No active count/reservation side effects.

## Phase 5: Observability and docs adjustments

### Trace fields

When provider-specific recompute changes the budget, ensure `thinking_trace` records:

- `resolved_budget_tokens` after final provider-specific resolution;
- `capability_status` from selected provider;
- `capability_source` from selected provider;
- `upstream_fields = ["thinking"]` for OpenAI→Anthropic translations;
- `decision = "clamped"` if provider-specific clamp occurred;
- `decision = "rejected"` if strict provider-specific validation failed.

### Metrics

If the strict provider-specific validation fails, increment:

- rejected thinking counter;
- budget rejection/clamp counter if that exists in the current metrics module;
- do not increment upstream failure/health counters.

### Docs

Update `docs/thinking.md` and any relevant architecture note to clarify:

- OpenAI effort mappings are resolved once for preflight and re-resolved after selected provider resolution.
- Provider-scoped mappings win at dispatch for collapsed model ids.
- Post-selection capability rejection is treated as client validation failure, not upstream failure.

## Validation commands

Run targeted tests first:

```bash
pytest tests/unit/test_capability_routing.py
pytest tests/unit/test_thinking_reasoning_matrix.py
pytest tests/unit/test_transcoder/test_budget_resolver.py
pytest tests/unit/test_transcoder/test_reasoning_fields.py
pytest tests/contract/test_transcoder_contract.py
```

Then run the full suite:

```bash
pytest
```

If the repo's existing developer workflow includes static checks, run them too:

```bash
python -m compileall src tests
ruff check src tests
pyright
```

Only treat type/lint failures as blockers if those checks are part of the repo's normal gate.

## Completion criteria

This final cleanup is complete when:

1. Selected-provider `effort_to_budget_tokens` mappings are applied for OpenAI `reasoning_effort` requests using collapsed model ids.
2. Explicit client budgets are still validated/clamped against the selected provider's min/max.
3. Strict provider-specific budget rejection returns controlled HTTP 400 before upstream dispatch.
4. Post-selection capability rejection finalizes durable attempt/request state and releases runtime counters/reservations/health slots.
5. Streaming and non-streaming paths share the same semantics.
6. Tests assert both final upstream body contents and cleanup invariants.
7. No provider health penalty is applied for client/capability validation failures.

## Suggested commit breakdown

1. `fix: preserve selected provider effort mapping for thinking budgets`
2. `fix: finalize selected attempts on capability rejection`
3. `test: cover provider-specific thinking budget cleanup`
4. `docs: clarify selected-provider thinking budget resolution`
