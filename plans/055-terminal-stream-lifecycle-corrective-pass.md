# Plan 055 — Terminal Stream Lifecycle Corrective Pass

Date: 2026-07-31
Status: stream-specific implementation complete at `13cdd493`; residual retained-cleanup convergence is tracked and closed by Plan 056
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Supersedes closure claims in: Plans 047, 048, 049, and 053 where they conflict with this plan
Planning baseline: `8cde724d30cd1b418793bfe62fdbf4a49615e589`

## Objective

Close the remaining correctness defects from the upstream-streaming hardening work without rebuilding the test, CI, evidence, or lifecycle apparatus that Plan 054 intentionally removed.

The implementation must:

1. stop leaking one `RequestFinalizationSupervisor` entry for every non-cancelled streaming request;
2. make every selected streaming terminal outcome use the same retained finalization path;
3. remove the remaining cancellation windows in retryable-attempt cleanup and post-commit selection compensation;
4. resolve the mismatch between the stream EOF classifier's `retryable` result and the response lifecycle that currently cannot safely retry after response handoff;
5. simplify timeout behavior where the current implementation exceeds demonstrated requirements;
6. correct the remaining nested thinking-control policy inconsistency;
7. retain only a small, behavior-focused test set and make no CI expansion.

The stream-specific corrections landed in `13cdd493`. The retryable-attempt
cleanup, post-commit compensation, waiter-cancellation terminal bridge, and
retained-registry capacity work described below remained as residual defects
and are intentionally closed by Plan 056.

This is a corrective pass, not another architecture phase. Reuse the existing `RequestFinalizationSupervisor`, `RequestFinalizationJob`, `_finalize_terminal()`, `AttemptFinalizer`, `SSEDecoder`, `IncrementalSSEObserver`, and provider configuration models. Do not introduce a second supervisor, a generalized workflow engine, a new evidence schema, a soak runner, or a new CI job.

## Current confirmed defects

### 1. Orphaned `pending_stream` jobs

`RequestCoordinator._build_stream_generator()` registers a finalization job with outcome `pending_stream` before the generator begins. Only the cancellation branch later populates and runs that job.

Normal completion, protocol-premature EOF, and generic midstream error paths call `RequestFinalizer.finalize()` directly. The registered `pending_stream` job is therefore never completed or reconciled from the supervisor registry.

The supervisor is bounded. Repeated successful streams can eventually fill the active-job registry and force detached, untracked finalization jobs. This is a long-running-process degradation defect and the highest-priority item in this plan.

### 2. Streaming terminal paths bypass `_finalize_terminal()`

The coordinator already has `_finalize_terminal()` as the canonical retained path for normal non-streaming completion, request errors, cancellation, and exhausted requests. The streaming generator still contains direct finalizer calls for:

- canonical completion;
- compatibility completion;
- premature EOF;
- generic midstream exceptions.

This contradicts the documented invariant that every selected request outcome has one retained terminal owner. It also leaves cancellation windows after SQLite transition but before quota, active-count, health, registry, and analytics convergence.

### 3. Retryable-attempt cleanup is still request-task-owned

The retry loop currently performs these steps sequentially in the caller task:

1. `AttemptFinalizer.finalize_failed_attempt()`;
2. in-memory quota reservation removal;
3. active-request decrement;
4. health/effects application;
5. retry selection.

Cancellation between these awaits can strand partial in-memory ownership even though durable attempt state has transitioned. This path must remain attempt-scoped because the request itself is not terminal, but the complete failed-attempt cleanup command must have retained ownership until convergence.

### 4. Post-commit selection compensation is only partly shielded

`_compensate_or_rollback_claim()` shields the durable attempt-finalization call, while active-count rollback, quota rollback where applicable, and health-slot release remain ordinary caller-task awaits.

A cancellation during post-commit publication failure can therefore preserve committed reservation/attempt facts while leaving runtime state partially published or partially compensated.

### 5. `StreamEOFDecision.retryable` is not actionable in the current generator stage

`classify_stream_eof()` returns `retryable=True` when no downstream body bytes have been emitted. The stream generator ignores that field and always finalizes premature EOF as `MIDSTREAM_ERROR`.

More importantly, the generator executes after the `StreamingResponse` has been returned. HTTP response headers may already be committed even when zero body bytes have been yielded. Retrying at this point is not generally safe or transparent.

The implementation must choose and document one truthful model:

- **Preferred narrow model:** all event-stream EOF decisions made inside the handed-off generator are terminal for that selected attempt and are never retried; remove the misleading retryable field/claim from this stage.
- **Optional alternative only if clearly simpler in the existing code:** prefetch enough protocol evidence before returning the `StreamingResponse` so a zero-byte premature EOF can be retried before response handoff. Do not add a buffering subsystem or delay normal streaming materially to achieve this.

The preferred model should be used unless the alternative is demonstrably smaller.

### 6. Timeout policy exceeds established need

The implementation added provider-specific first-byte, idle, and total-lifetime timers on top of HTTPX transport timeouts. The original defect required distinguishing clean EOF from an actual read timeout and allowing provider-specific tuning. It did not establish that all three independent timers were necessary.

The current machinery is acceptable only where it has a distinct operational purpose:

- provider `read_timeout_s` or an explicit idle timeout can address long inter-chunk gaps;
- clean EOF classification addresses silent truncation;
- a total generation lifetime cap is not needed for the reported MiniMax failure and can terminate healthy long-running generations.

The corrective pass should remove or de-emphasize `max_lifetime_s` unless an existing supported use depends on it. Backward-compatible parsing may be retained for one release if removal would break configuration, but it should not drive the stream loop by default.

Do not globally increase the 300-second read timeout. Provider-specific configuration is the intended escape hatch.

### 7. Nested thinking-control policy is inconsistent

For effort/budget contracts, nested `thinking` adaptation can strip unsupported fields and return a mapped/dropped result even when `unsupported_control="reject"`. It also does not consistently validate nested `thinking.effort` values against `accepted_efforts`.

The behavior must match the configured policy:

- `reject`: any present unsupported or unknown selectable control raises `CapabilityError` before upstream dispatch;
- `warn_drop`: unsupported fields are removed and reported;
- `map_if_known`: recognized aliases are mapped; unmappable values are rejected.

Historical reasoning content remains unrelated to selectable controls and must pass through unchanged.

## Scope constraints

### In scope

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/attempt_finalizer.py` only if a small retained attempt-cleanup helper belongs there
- `src/eggpool/request/stream_completion.py`
- `src/eggpool/models/config.py`
- `src/eggpool/providers/client_pool.py` only if timeout simplification requires matching transport configuration
- `src/eggpool/transcoder/provider_adaptation.py`
- focused existing tests under `tests/unit/`, `tests/integration/`, and `tests/smoke/`
- concise corrections to `AGENTS.md`, `README.md`, or plan status text where they currently claim stronger closure than the code provides

### Explicitly out of scope

- new GitHub Actions jobs or matrices;
- restoring full-suite execution on every push;
- soak, stability-duration, or resource-plateau runners;
- retained evidence bundles or JSON evidence schemas;
- a second finalization supervisor or generic task orchestration framework;
- database schema migrations;
- dashboard redesign;
- changes to retry count, provider health policy, quarantine policy, or routing strategy;
- broad refactoring of `RequestFinalizer`;
- adding a global timeout increase;
- exhaustive protocol × provider × policy Cartesian tests.

## Required implementation sequence

The phases below are ordered. Each phase should be committed independently when practical so a smaller implementation model can recover cleanly if a later phase fails.

## Phase A — Remove orphaned stream-job registration

### Goal

Ensure that a streaming request does not create a supervisor job until an actual terminal outcome and `FinalizationData` are known.

### Changes

1. Remove eager `register_or_get(..., "pending_stream")` from `_build_stream_generator()`.
2. Remove the mutable pattern where the cancellation branch later assigns `fin_job.finalization_data` and dependencies.
3. Replace all generator terminal calls with the coordinator's canonical `_finalize_terminal(context, selected, data)` helper.
4. The following paths must call `_finalize_terminal()` exactly once:
   - canonical OpenAI/Anthropic stream completion;
   - compatibility completion accepted by provider policy;
   - client cancellation;
   - clean premature EOF;
   - malformed EOF;
   - first-byte, idle, or transport midstream failure after response handoff;
   - generic generator exception.
5. Preserve the existing no-retry-after-response-started behavior.
6. Preserve `StreamDiagnostics` outcome recording, but diagnostics must not own or duplicate finalization.

### Important control-flow rule

Do not call `_finalize_terminal()` in both a specific exception branch and a broad outer exception branch for the same exception. Use explicit exception ordering and either:

- finalize in the specific branch and re-raise through an outer branch that explicitly excludes already-finalized terminal exceptions; or
- build a terminal command in branches and submit it once in a single `finally`/terminal section.

Prefer the smallest readable change. A local boolean such as `terminal_submitted` is acceptable if it prevents duplicate submission, but do not build a new state machine inside the generator.

### Acceptance criteria

- After one successful stream, `RequestFinalizationSupervisor.active_count == 0` after the terminal task completes.
- The same is true after premature EOF, generic midstream error, and cancellation.
- Supervisor history contains one record for the selected attempt, with the actual terminal outcome rather than `pending_stream`.
- Repeating at least 300 short successful mock streams does not increase active supervisor count and does not trigger saturation. This should be implemented as a fast loop over a small unit/integration harness, not a timed soak.
- No direct `finalizer.finalize()` call remains in streaming terminal branches when a coordinator supervisor is available.

## Phase B — Retain failed-attempt cleanup as one bounded command

### Goal

Make retryable pre-body attempt cleanup cancellation-safe without incorrectly marking the overall request terminal.

### Design

Create one narrow attempt-scoped retained cleanup operation. It may be implemented as either:

1. a small `AttemptCleanupJob` retained by the existing `RequestFinalizationSupervisor` under an attempt-specific API; or
2. a coordinator-owned task registry dedicated to in-flight failed-attempt cleanup, provided it is bounded and substantially smaller than extending the supervisor.

Prefer extending the existing supervisor only when it can represent a non-terminal attempt without weakening its terminal identity semantics. Do not overload `FinalizationOutcome` with a fake request-terminal value.

The retained operation owns, in order:

1. durable failed-attempt transition and durable reservation release;
2. in-memory quota reservation removal when the durable reservation transitioned;
3. router active-request decrement when the durable reservation transitioned;
4. health/effects application exactly once when the attempt transitioned;
5. health probe release where the selected failure has no penalty but still owns a probe slot.

The retry loop may proceed only after this operation reports convergence. If it fails, do not select another account while the previous attempt still owns runtime reservation state.

### Idempotency

Use `(proxy_request_id, attempt_id)` as the identity. Duplicate callers must join the existing retained operation. Existing `AttemptFinalizer` transition checks remain the durable last line of defense.

### Error behavior

- Cancellation of the request waiter must not cancel the retained cleanup task.
- A cleanup exception should abort further retries for the request and surface an upstream/system error; it must not silently select another account.
- No unbounded automatic retry loop is required. One retained execution plus the existing startup/stale recovery safety net is sufficient for this private-deployment product.

### Acceptance criteria

- Cancellation immediately after durable failed-attempt transition still results in zero active count, zero in-memory quota reservation, and a released health probe.
- Cancellation during quota removal or active-count decrement does not produce duplicate release when cleanup is rejoined.
- The next request can select the same healthy account where policy permits; no restart or database deletion is needed.
- Only two focused cancellation tests are required: one cancellation after durable transition and one duplicate/rejoin case.

## Phase C — Make post-commit claim compensation atomic from the caller's perspective

### Goal

Ensure a failure or cancellation during runtime publication cannot leave a committed attempt/reservation paired with partial runtime ownership.

### Changes

1. Extract the complete compensation sequence in `_compensate_or_rollback_claim()` into one retained task created before the first compensation await.
2. The task owns:
   - decrementing active count if it was incremented;
   - removing the in-memory quota reservation if it was added;
   - finalizing the durable attempt/reservation as `PostCommitInterrupted`;
   - releasing the health/circuit probe slot;
   - recording compensation diagnostics.
3. Track whether quota publication completed independently of active-count publication. The current single `active_count_was_increased` flag is insufficient if quota addition fails after active-count increment or if cancellation lands between the two publication calls.
4. Return or record a small structured publication receipt, for example:

```python
@dataclass(slots=True)
class RuntimePublicationReceipt:
    active_count_added: bool = False
    quota_reservation_added: bool = False
```

5. Compensation uses the receipt and releases only acquired components.
6. Do not add database rows or a new recovery table.

### Acceptance criteria

- Failure after active-count increment but before quota addition leaves neither component acquired.
- Failure after quota addition leaves neither component acquired.
- Cancellation of the caller while compensation is running does not cancel compensation.
- The durable attempt is terminal with `post_commit_interrupted`, the durable reservation is released, and the health slot is free.
- One table-driven test over the two publication boundaries is sufficient.

## Phase D — Resolve EOF retry semantics and simplify timeout policy

### Goal

Make stream semantics truthful and remove unnecessary timing machinery.

### EOF changes

Use the preferred narrow model unless implementation inspection proves the alternative smaller:

1. Rename or simplify `StreamEOFDecision` so generator-stage decisions do not expose an unused `retryable` field.
2. Document that once `PreparedProxyResponse` with a stream iterator is returned, protocol EOF detected by that iterator is terminal for the selected attempt.
3. Continue distinguishing:
   - canonical completion;
   - provider-compatible completion based on usage evidence;
   - empty EOF;
   - premature EOF after payload;
   - malformed/incomplete-frame EOF.
4. Preserve the rule that missing terminal evidence is never silently converted to success under strict policy.
5. Do not synthesize `[DONE]` or `message_stop` after premature EOF.

### Timeout changes

1. Retain provider-specific first-byte and idle/read controls only where each maps to an observable outcome.
2. Remove `max_lifetime_s` from the active stream loop unless an existing test or documented operator workflow demonstrates required behavior.
3. For backward compatibility, one of these is acceptable:
   - remove `max_lifetime_s` from configuration immediately and make old config fail clearly; or
   - keep it deprecated/parsed but ignored, with a warning and removal note.
4. Do not add new timer categories.
5. Ensure HTTPX's transport read timeout does not fire earlier than a configured provider idle timeout.
6. Keep default behavior unchanged for providers with no stream timeout overrides.

### Acceptance criteria

- Strict clean EOF without terminal evidence is finalized as `MIDSTREAM_ERROR`, never `COMPLETED`.
- Canonical `[DONE]` and `message_stop` streams complete.
- Compatible usage-complete streams complete only under compatible/permissive policy.
- No code path advertises a retry that cannot safely occur after response handoff.
- A provider-specific idle timeout can be longer than the default transport read timeout without the transport firing first.
- No total-lifetime timer runs by default.

## Phase E — Correct nested thinking-control policy

### Goal

Make nested Anthropic-style thinking controls obey the same typed policy semantics as top-level controls.

### Changes

1. Refactor `_adapt_thinking_block()` to classify each present selectable field:
   - `thinking.type`;
   - `thinking.effort`;
   - `thinking.budget_tokens`.
2. Validate `thinking.effort` case-insensitively against `accepted_efforts` and `effort_aliases`.
3. Apply policy consistently:
   - `reject`: raise through a `rejected` field adaptation;
   - `warn_drop`: remove unsupported fields and return `dropped`;
   - `map_if_known`: map known aliases, otherwise reject.
4. A partially valid block may retain valid fields while dropping invalid fields only in `warn_drop` mode.
5. Ensure `ProviderRequestAdaptation.decision`, `requested_controls`, `emitted_controls`, and warnings describe the actual provider-bound body.
6. Preserve non-control reasoning content in messages.

### Acceptance criteria

The following focused cases are sufficient:

| Contract | Input | Policy | Expected |
|---|---|---|---|
| effort | `thinking.effort="med"` | map_if_known | emits `medium` |
| effort | `thinking.effort="extreme"` | reject | `CapabilityError` |
| effort | `thinking.budget_tokens=4096` | warn_drop | budget removed, valid fields retained |
| budget | `thinking.effort="high"` | reject | `CapabilityError` |
| fixed | type-only block | warn_drop | entire selectable block removed |
| fixed | any selectable field | reject | `CapabilityError` |

No additional full protocol matrix is required.

## Phase F — Focused verification and documentation correction

### Test budget

Do not restore deleted Plan 023/029/030 apparatus. Prefer modifying existing files. The entire corrective pass should add no more than approximately 8–10 focused tests unless an existing table can absorb cases with no new fixture complexity.

Required behavioral coverage:

1. successful stream leaves supervisor active count zero;
2. premature EOF leaves supervisor active count zero and request non-completed;
3. cancellation leaves supervisor active count zero;
4. repeated successful streams do not saturate the registry;
5. retryable attempt cancellation converges runtime ownership;
6. post-commit publication compensation covers both partial-publication boundaries;
7. EOF policy table reflects truthful no-retry-after-handoff semantics;
8. nested thinking-control policy table above.

### Smoke suite

Keep the existing smoke suite small. At most one existing smoke test should be strengthened to assert finalization-supervisor convergence. Do not add a long repeated-stream test to smoke; keep that in a focused unit/integration test.

### CI

No `.github/workflows/ci.yml` changes are expected. CI remains:

```text
ruff format --check
ruff check
pyright
pytest tests/smoke/
```

### Documentation

Correct statements that currently claim every selected terminal path uses a retained job. Once the implementation is fixed, the claim may remain, but it must specifically distinguish:

- request-terminal retained finalization;
- attempt-scoped retained cleanup before retry;
- stale/startup recovery as a safety net rather than the normal path.

Update Plan 047 status from closed to superseded/corrected by Plan 055 until all acceptance criteria pass. Update Plans 048/049 only where retry or lifetime-timeout wording becomes false.

## Recommended implementation shape

The intended end-state control flow is:

```text
select and persist attempt
  -> publish runtime receipt
  -> dispatch upstream

pre-body retryable failure
  -> retained attempt cleanup
  -> await convergence
  -> select next attempt

request-terminal outcome
  -> build FinalizationData once
  -> _finalize_terminal()
  -> retained RequestFinalizationJob
  -> RequestFinalizer durable transition
  -> RequestFinalizer runtime convergence
  -> bounded supervisor history

stream generator terminal outcome
  -> build FinalizationData once
  -> _finalize_terminal()
  -> diagnostics only after/around the canonical submission
```

There must not be a pre-registered placeholder terminal job with mutable outcome data.

## Validation commands

Use changed-path checks during implementation:

```bash
uv run ruff format --check src/eggpool/request src/eggpool/transcoder tests/
uv run ruff check src/eggpool/request src/eggpool/transcoder tests/
uv run pyright src/eggpool/request src/eggpool/transcoder
```

Run only the focused affected tests plus smoke:

```bash
uv run pytest \
  tests/unit/test_request_finalization_state_machine.py \
  tests/unit/test_runtime_ownership_token.py \
  tests/unit/test_plan_046_thinking_control_normalization.py \
  tests/unit/test_stream_completion.py \
  tests/integration/test_plan_046_request_path_body_capture.py \
  tests/smoke/test_failure_recovery_smoke.py \
  tests/smoke/test_premature_eof_smoke.py \
  -q --tb=short --maxfail=1

uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

If an exact named file above differs in the current tree, use the existing nearest owner test file rather than creating duplicate plan-numbered files.

A full test-suite run is optional local confidence, not an acceptance requirement and not a release artifact.

## Final acceptance criteria

Plan 055 is complete only when all statements below are true:

1. No successful, cancelled, prematurely closed, or failed stream leaves an active supervisor job after finalization convergence.
2. There is no `pending_stream` placeholder job lifecycle.
3. Every selected request-terminal streaming path calls the canonical retained `_finalize_terminal()` path exactly once.
4. Residual retryable failed-attempt cleanup convergence is tracked by Plan 056.
5. Residual post-commit publication compensation convergence is tracked by Plan 056.
6. Residual waiter-cancellation ownership convergence is tracked by Plan 056.
7. Stream EOF semantics do not advertise an unsafe retry after response handoff.
8. Strict missing-terminal EOF remains a visible error; canonical and explicitly compatible completion remain successful.
9. No total-lifetime stream timer runs by default, and no global timeout increase is introduced.
10. Nested thinking controls honor `reject`, `warn_drop`, and `map_if_known` consistently.
11. The focused tests and smoke suite pass.
12. CI remains the reduced single-job smoke model from Plan 054.
13. No soak runner, evidence bundle, new workflow, schema migration, or generalized lifecycle framework is added.
14. Documentation and plan statuses no longer claim closure beyond the implemented behavior.

## Handoff notes

Start with Phase A. It is independently valuable and closes the active supervisor leak even if later phases are delayed.

Keep edits local to the listed files. Resist the temptation to redesign `RequestFinalizer` or merge request-terminal and attempt-retry concepts into one large abstraction. The target is a small number of canonical helper calls with truthful ownership, not more infrastructure.
