# Plan 141 — Final Corrective Closure: API 413, Selected-Provider Multimodal Enforcement, and Evidence Cleanup

Status: ready for implementation

Baseline reviewed: `b38c6f8cd91bd80771e79dfa5f34b5003d71cc69`

Related plans: 131–140

## Purpose

Plan 140 materially improved the local-provider and multimodal work, but the post-implementation review found a small number of correctness gaps that prevent the line of work from being considered closed.

This is a **final corrective pass**, not a new roadmap. The implementation should be narrow, mechanical, and biased toward deleting ambiguity rather than introducing more abstractions.

The intended deployment remains a private/LAN, single-node SBC-oriented proxy. Preserve the existing lightweight CI posture, durable request lifecycle, provider failure isolation, account routing, and dependency footprint.

## Required outcome

After this plan lands:

1. A provider-bound serialized request-size rejection returns the correct client-facing HTTP 413 after the selected attempt is cleanly finalized.
2. A failed durable finalization cannot be mistaken for a completed oversize finalization or silently strand selected runtime/durable ownership.
3. Provider-sensitive cross-protocol media translation is definitively evaluated **after `SelectedAttempt` exists**, using `selected.provider_id`.
4. A collapsed unsuffixed model can no longer borrow another provider's multimodal capability row during the final provider-bound translation.
5. Text-only requests retain the existing `PreparedTranscode` reuse optimization.
6. Same-protocol native requests remain passthrough and are not canonicalized through a new abstraction.
7. Bundled local multimodal metadata is corrected where current provider behavior is verified, without inventing limits or model-level guarantees.
8. The Responses API deferral record is factually accurate while keeping Responses implementation out of scope.
9. No new CI job, provider SDK, state machine, generic provider framework, or broad test apparatus is introduced.

---

# Scope constraints

## In scope

Expected files include only the narrow surfaces needed for the corrections:

- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/coordinator.py`
- existing request/finalization helpers if needed
- `src/eggpool/transcoder/sensitive_media.py`
- `src/eggpool/transcoder/prepared.py` only if diagnostics need adjustment
- `src/eggpool/providers/_templates.toml`
- `plans/139-phase-08-openai-responses-api-evaluation.md`
- Plan 140/architecture/provider docs only where they currently state behavior that is not true
- a small number of focused unit/contract tests

## Explicitly out of scope

Do **not**:

- add a third protocol transcoder;
- implement `/v1/responses`;
- add Responses ↔ Anthropic conversion;
- add `previous_response_id`, conversation-state storage, affinity, or response-ID routing;
- add provider SDK dependencies;
- add LAN scanning, mDNS, zeroconf, or local-runtime discovery daemons;
- introduce a generic provider-plugin framework;
- introduce a new request state machine or finalization subsystem;
- weaken reserve-before-dispatch durability;
- add an in-memory/no-SQLite dispatch mode;
- reintroduce semantic compression or segmentation work;
- build a canonical all-protocol request IR;
- add capability-driven routing architecture beyond the minimum needed to make the selected-provider boundary correct;
- add new GitHub Actions jobs, OS/Python matrices, live-provider CI, soak tests, provider matrices, or benchmark gates;
- create a broad end-to-end test harness for this closure pass.

If a proposed change needs one of these items, stop and choose the smaller correction.

---

# Workstream A — Make provider-bound oversize rejection a real API 413

## Current defect

Plan 140 correctly added selected-provider serialized-size validation and a post-selection finalization helper. However, the execution path still re-raises `RequestTooLargeError` after finalization.

The API handler around `coordinator.execute()` does not currently catch that exception. The outer ordinary-exception containment therefore converts the provider-bound oversize path into HTTP 500 even though the durable attempt may have been finalized as a local 413 outcome.

Mapping `RequestTooLargeError` in `error_status_code()` is insufficient because this exception bypasses that renderer on the current API path.

## Implementation

1. Add an explicit `RequestTooLargeError` handler around `coordinator.execute()` in `src/eggpool/api/proxy_request.py`.
2. Render it as HTTP 413 using the current endpoint-specific error renderer.
3. Use a bounded client-safe message such as `Request body too large` or `Serialized request body too large`.
4. Use the normal invalid-request/client-error type for the active protocol. Do not expose internal serialized byte counts or provider configuration in the response body unless that information is already intentionally public elsewhere.
5. Preserve the existing earlier `read_body_limited()` 413 path. The two 413 sources are distinct:
   - ingress/server body limit before routing;
   - provider-bound serialized body limit after selection/translation.
6. Do not convert `RequestTooLargeError` into `UpstreamError`.
7. Do not retry another account solely because a provider-bound size ceiling rejected the request in this pass.
8. Do not apply provider health, backoff, quarantine, or quota-failure penalties.

## Acceptance criteria

- A provider-bound serialized-size rejection returned from the coordinator renders HTTP 413, not 500.
- OpenAI-facing requests receive the existing OpenAI-shaped invalid-request response.
- Anthropic-facing requests receive the existing Anthropic-shaped invalid-request response.
- The ingress `read_body_limited()` 413 behavior remains unchanged.
- No upstream request is built or sent after provider-bound size validation fails.
- No retry or provider health transition occurs for this local client-validation failure.

---

# Workstream B — Make oversize finalization fail closed and prove convergence

## Current defect

The new `_finalize_selected_oversize_rejection()` sets `_oversize_finalized` before durable finalization is proven and catches/logs `DatabaseError`.

That ordering is unsafe: a database failure can leave the metadata flag claiming terminalization succeeded, and later exhausted-path logic may skip a convergence attempt because the flag is already set.

The closure requirement is not merely "call the finalizer". It is: either canonical finalization converges selected durable/runtime ownership, or the failure remains visible to the existing fail-closed recovery path.

## Implementation

1. Treat `_oversize_finalized` as a **proof-of-convergence marker**, not an intent marker.
2. Do not set it until the canonical terminalization call has completed successfully and the existing finalization owner has established the required durable/runtime convergence.
3. Reuse the existing finalization/finalization-supervisor mechanism. Do not create a second cleanup path.
4. Do not silently swallow a `DatabaseError` in a way that allows normal 413 rendering to continue while convergence is unknown.
5. Choose the smallest behavior consistent with existing finalization semantics:
   - if the existing finalization supervisor already takes ownership and can prove handoff, use that path;
   - otherwise propagate the finalization failure into the existing fail-closed request containment/recovery path.
6. Preserve idempotency. A successful oversize finalization must not be finalized again by `_handle_exhausted()`.
7. Preserve the existing selected attempt/reservation/runtime release ownership rules.
8. Do not apply provider failure effects for `FinalizationOutcome.CLIENT_ERROR` caused by local oversize validation.

## Acceptance criteria

On the healthy path:

- request row is terminal client error;
- selected attempt is terminal;
- reservation is terminal/released;
- in-memory active-request/reservation ownership is released through the canonical owner;
- `_oversize_finalized` is set only after that convergence is established;
- API returns 413;
- provider health/backoff/quarantine is untouched.

On a simulated durable-finalization failure:

- `_oversize_finalized` is **not** left set merely because finalization was attempted;
- the request does not proceed as if a clean 413 terminalization occurred;
- the failure is propagated or transferred to the existing finalization supervisor according to current project invariants;
- no new ad-hoc cleanup subsystem is introduced;
- provider health is still not penalized for the original oversize condition.

---

# Workstream C — Move definitive provider-sensitive transcode to the selected-provider boundary

## Current defect

Plan 140 correctly detects provider-sensitive media and prevents reuse of a prepared transcode. However, the recompute still occurs before the selection/retry loop.

That recompute reads capability metadata using `context.provider_id`. For an unsuffixed collapsed model, `context.provider_id` is not necessarily the provider eventually chosen by routing. The actual `SelectedAttempt` is created later.

Therefore the current implementation can still perform its definitive multimodal translation against pre-selection/global/provider-hint metadata rather than the provider that will receive the request.

## Required ordering

For cross-protocol requests with provider-sensitive media, the definitive sequence must be:

```text
parse client request
    ↓
identify that provider-sensitive media is present
    ↓
select + persist SelectedAttempt
    ↓
resolve capabilities using selected.provider_id
    ↓
translate/validate provider-sensitive content from the original client payload
    ↓
apply existing selected-provider transforms
    ↓
serialize final provider payload
    ↓
selected-provider size validation
    ↓
build/send upstream request
```

Text-only cross-protocol requests may continue to use the existing prepared-transcode fast path.

Same-protocol native requests remain passthrough.

## Implementation

### C1. Pre-selection behavior

1. Keep `request_has_provider_sensitive_media()` as the simple request-level signal.
2. For a cross-protocol request carrying provider-sensitive media, do **not** treat any pre-selection translation as the definitive provider payload.
3. Prefer the simplest implementation: do not create/reuse a `PreparedTranscode` for provider-sensitive media if doing so would only be discarded after selection.
4. Pre-selection parsing and provider-independent validation may remain where useful, but it must not reject or transform based on a provider-specific multimodal capability row that has not yet been selected.
5. Keep text-only prepared-transcode behavior unchanged.

### C2. Post-selection definitive transcode

1. Add a narrow coordinator helper for the final selected-provider cross-protocol translation. The name is implementation-defined; avoid a new framework.
2. Invoke it **inside the retry loop after `_select_and_persist_attempt()` returns `selected` and before `_execute_upstream()` begins provider-bound transforms/network I/O**.
3. Resolve model capability metadata with:

```python
catalog.cache.get_model_for_provider(context.model_id, selected.provider_id)
```

4. Pass that provider's `multimodal`, `transcoding`, and relevant thinking capability into the existing transcoder.
5. Translate from the stable original client/request generation, not from a payload previously translated for another provider.
6. If retry selection later chooses a different provider, rebuild the provider-bound translated generation from the original client payload for that provider. Never stack provider B's translation on provider A's translated payload.
7. Continue to use the existing `ProviderBoundRequest` ownership/COW boundaries; do not create another request graph abstraction.
8. After selected-provider translation, run the existing selected-provider transform pipeline in its current relative order unless a narrow correctness dependency requires a documented adjustment.
9. Keep final serialized-size validation after all provider-specific translation/transforms so it measures the exact bytes that would be sent.

### C3. Local capability rejection

If the selected provider cannot represent a required media form:

1. Treat the condition as a local capability/client-request incompatibility, not an upstream failure.
2. No upstream I/O should occur.
3. Do not apply provider health/backoff/quarantine penalties.
4. Reuse the existing selected capability-rejection finalization path.
5. Do not add a new capability-aware provider-routing subsystem in this pass.

A future enhancement may pre-filter/reroute among heterogeneous providers, but Plan 141 is only responsible for ensuring the selected provider's contract is authoritative and never bypassed.

## Acceptance criteria

- A media-bearing cross-protocol request using an unsuffixed collapsed model does not use the global/first-seen model capability row for its final translation.
- The capability lookup that governs final media translation uses `selected.provider_id`.
- If provider A and provider B advertise different image source-form support, selecting A uses A's capability row and selecting B uses B's capability row.
- A retry that changes selected providers reconstructs the translation from the original client payload rather than reusing/mutating the prior provider's translated graph.
- Unsupported selected-provider media fails locally before upstream I/O and without provider penalty.
- Text-only prepared transcodes still reuse the existing fast path.
- Native same-protocol requests remain passthrough.
- No new canonical protocol IR or provider plugin layer is introduced.

---

# Workstream D — Correct local multimodal capability metadata without speculation

## Problem

Plan 140 removed speculative request-size ceilings, which was correct, but some remaining local template comments/flags are still overly conservative or factually stale.

The bundled capability metadata must describe only behavior supported by the provider's documented OpenAI-compatible surface. It must not imply that every loaded model supports a modality.

## Implementation

1. Re-check current official documentation for each bundled local runtime before editing capability flags.
2. Correct verified source-form support where the current templates are wrong.
3. At minimum, explicitly verify the current OpenAI-compatible image URL behavior for:
   - Ollama;
   - vLLM.
4. If official documentation confirms URL-image input on the supported endpoint, set the corresponding `image_input.url = true` and update comments.
5. Do not infer document/audio/tool-result support merely because a provider supports some multimodal models.
6. Keep uncertain or model-dependent capabilities conservative/unset.
7. Do not reintroduce universal serialized request ceilings unless the provider documents an enforceable whole-request limit appropriate to EggPool's transport boundary.
8. Make comments distinguish **provider protocol-surface capability** from **loaded model capability**.

## Acceptance criteria

- No bundled local template contains a known false-negative image source-form claim after verification.
- Ollama and vLLM URL-image declarations match their current official OpenAI-compatible documentation.
- No speculative whole-request byte limit is reintroduced.
- Model-dependent document/audio capabilities remain conservative unless the provider contract supplies reliable model-level metadata EggPool already consumes.
- Registry tests assert the corrected template facts without adding a provider matrix.

---

# Workstream E — Correct the Responses API deferral rationale; do not implement Responses

## Problem

Plan 139/140's implementation decision may remain `defer`, but parts of the written rationale are too strong. The record currently implies that Codex/local runtimes do not meaningfully use or expose Responses support.

The correct architectural conclusion is narrower:

- Responses support exists in current clients/providers;
- a stateless same-protocol passthrough is technically feasible;
- EggPool still lacks measured project/operator value sufficient to justify adding another endpoint surface now;
- stateful Responses semantics and cross-protocol translation remain clearly outside EggPool's current scope.

## Implementation

1. Update `plans/139-phase-08-openai-responses-api-evaluation.md` so it no longer states as fact that Codex does not use/need Responses or that relevant local runtimes lack Responses support.
2. Record the current decision as:

```text
responses_stateless_passthrough: defer
```

3. State the reason as **scope/value proportionality**, not absence of external protocol support.
4. Preserve the already useful constraint that a future implementation, if justified, should begin with stateless same-protocol passthrough only.
5. Preserve explicit exclusions for:
   - Responses ↔ Anthropic translation;
   - persistent response-ID storage;
   - `previous_response_id` affinity;
   - conversation routing/state;
   - new SDK dependencies;
   - expanded CI matrices.
6. Update architecture/provider docs only if they repeat the inaccurate support claims.
7. Do not add `/v1/responses` code under this plan.

## Acceptance criteria

- Documentation acknowledges that Responses is a real current protocol surface used/supported externally.
- The defer decision is justified by EggPool's present scope and measured value, not inaccurate ecosystem claims.
- No Responses endpoint, state store, routing mode, transcoder, dependency, or test matrix is added.

---

# Workstream F — Focused regression tests only

The test gap is specifically at boundary composition. Do not respond by creating a new integration framework.

## Required tests

### F1. API-level provider-bound 413

Add one small parameterized or equivalent focused test that drives the API error boundary around `coordinator.execute()` and proves:

- `RequestTooLargeError` -> HTTP 413;
- not HTTP 500;
- correct protocol-shaped error for OpenAI and Anthropic endpoints if the existing test helpers make both cheap to cover.

This test must exercise the actual API exception handler, not merely `error_status_code()`.

### F2. Healthy selected oversize finalization

Exercise enough of the canonical finalization boundary to prove:

- CLIENT_ERROR / 413 finalization;
- request/attempt/reservation convergence or the existing finalization result representing it;
- `_oversize_finalized` set only after successful convergence;
- no provider failure effects;
- no retry/upstream I/O.

Do not duplicate the entire database suite if the finalizer already has focused test helpers.

### F3. Failed oversize finalization

Inject a `DatabaseError` or existing equivalent finalization failure and prove:

- `_oversize_finalized` is not falsely set;
- the failure propagates or is handed to the canonical supervisor;
- the normal successful-413 path is not falsely reported;
- no provider penalty is introduced.

### F4. Selected-provider multimodal authority

Use two provider-scoped model entries for one collapsed model with different multimodal source-form capabilities. Prove the definitive transcode uses the provider selected for the attempt.

The test should fail if the implementation accidentally uses:

- global `get_model()`;
- `context.provider_id` before selection;
- another provider's capability row.

### F5. Retry/source-generation safety

If inexpensive with existing fixtures, add one test that changes the selected provider between attempts and verifies the second provider translation begins from the original client payload rather than the first provider's translated payload.

If this would require a large harness, cover the invariant through a smaller helper-level test and keep the implementation obvious.

### F6. Fast-path preservation

Retain/adjust an existing prepared-transcode test proving text-only requests still reuse `PreparedTranscode`.

### F7. Provider metadata

Add or update only the existing registry/template tests needed to pin the corrected Ollama/vLLM image URL flags.

## Test proportionality target

This closure should normally require approximately **3–6 focused test cases or compact parameterized groups**, not dozens of new tests.

Do not add new test directories, fixture corpora, live-provider calls, subprocess servers, or CI jobs.

---

# Suggested implementation order

Execute in this order so each step closes a concrete invariant before the next one:

1. **Oversize finalization ordering**
   - fix `_oversize_finalized` semantics;
   - make database/finalization failure fail closed.
2. **API 413 renderer**
   - add the explicit post-coordinator `RequestTooLargeError` handler.
3. **Post-selection multimodal boundary**
   - prevent provider-sensitive preflight from becoming definitive;
   - perform the final cross-protocol media translation after `SelectedAttempt` exists;
   - use `selected.provider_id`.
4. **Provider metadata correction**
   - verify official docs;
   - correct only confirmed source-form flags/comments.
5. **Responses documentation correction**
   - retain deferral, fix rationale.
6. **Focused regression tests and docs**
   - add only the boundary tests listed above;
   - update architecture text to match the actual ordering.
7. **Run the existing verification surface**
   - do not expand CI.

---

# Implementation invariants

The following invariants are non-negotiable.

## Request lifecycle

- Reserve/select/persist before provider-bound dispatch remains unchanged.
- A selected local validation failure must not strand request, attempt, reservation, or runtime ownership.
- A finalization failure must not be represented as successful convergence.
- Finalization remains idempotent and owned by the existing canonical machinery.

## Failure isolation

- Client/media/size validation errors do not count as provider failures.
- No health suppression, backoff, quarantine, or quota punishment from local oversize/capability rejection.
- No malformed client request can poison routing state for subsequent requests.

## Provider authority

- Before selection, global/aggregated model metadata may be used only for exposure/preflight hints.
- After selection, the selected provider's model row is authoritative for provider-bound capability and serialized-size decisions.
- A collapsed model must never borrow another provider's final media capability contract.

## Transcoding

- Text-only prepared translation optimization stays.
- Provider-sensitive media is finalized after provider selection.
- Retries translate from the original client generation.
- Same-protocol native traffic remains passthrough.
- No third protocol/canonical IR architecture is added.

## Scope/resource discipline

- No new runtime dependency.
- No provider SDK.
- No new CI job or matrix.
- No broad test framework.
- No new background process.
- No Responses endpoint.

---

# Verification commands

Use the repository's existing environment and commands. Do not add verification infrastructure.

Minimum targeted checks should include the exact files/tests touched by the implementation, for example:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/

uv run pytest tests/unit/test_oversize_413_lifecycle.py -q
uv run pytest tests/unit/test_transcoder/test_multimodal.py -q
uv run pytest tests/unit/test_transcoder/test_sensitive_media.py -q
uv run pytest tests/unit/test_prepared_transcode.py tests/unit/test_prepared_transcode_reuse.py -q
uv run pytest tests/unit/test_provider_registry.py -q
```

Add only the narrow API/coordinator test file(s) created by this implementation to the targeted invocation.

Then run the existing smoke gate exactly as CI does:

```bash
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

If existing nearby contract tests cover the proxy/transcoder boundary, run those targeted files as well. Do not promote the full contract suite into a permanent CI gate solely for Plan 141.

---

# Final acceptance checklist

Plan 141 is complete only when all of the following are true:

- [ ] Provider-bound serialized oversize returns API HTTP 413 rather than falling through to 500.
- [ ] Both streaming and non-streaming provider-bound size checks occur before upstream I/O.
- [ ] Successful oversize finalization converges request/attempt/reservation/runtime ownership through the canonical owner.
- [ ] `_oversize_finalized` is set only after successful/proven finalization ownership, never before.
- [ ] Simulated database/finalization failure cannot masquerade as a successful 413 cleanup.
- [ ] Oversize/capability client errors do not alter provider health, backoff, quarantine, or quota-failure state.
- [ ] Provider-sensitive cross-protocol media translation is definitively performed after `SelectedAttempt` exists.
- [ ] Final multimodal capability lookup uses `selected.provider_id`.
- [ ] Collapsed models cannot borrow another provider's capability row for provider-bound translation.
- [ ] Retry to a different provider rebuilds translation from the original client payload/generation.
- [ ] Text-only `PreparedTranscode` reuse still works.
- [ ] Same-protocol native requests remain passthrough.
- [ ] Ollama/vLLM local image source-form metadata matches verified current provider behavior.
- [ ] No speculative whole-request size ceilings are reintroduced.
- [ ] Plan 139/140 Responses documentation is factually corrected while implementation remains deferred.
- [ ] No Responses endpoint/state/transcoder is added.
- [ ] No new dependency, provider SDK, CI job, CI matrix, live-provider test, or generalized framework is added.
- [ ] Ruff format/lint, Pyright, focused regression tests, and the existing smoke suite pass.

## Closure condition

When every item above is satisfied, the remaining defects from the Plan 140 review are closed. Do not open another broad optimization/hardening phase from this plan unless new evidence identifies a separate correctness problem.
