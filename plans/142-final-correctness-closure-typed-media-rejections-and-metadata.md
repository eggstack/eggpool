# Plan 142 — Final Correctness Closure: Typed Media Rejections, Provider Metadata, and Boundary Tests

Status: ready for implementation

Baseline reviewed: `14ad0227af8a39292fc43a88725284ab5d26a547`

Related plans: 131–141

## Purpose

Plan 141 put the important architecture in the correct shape: provider-sensitive cross-protocol media is now translated after `SelectedAttempt` exists, provider-bound size rejection renders 413, retry translation restarts from the original client payload, and the lean CI posture remains intact.

The post-implementation review found three narrow closure defects:

1. expected post-selection transcoder rejections are currently caught by a broad `except Exception` and converted into `_LocalDispatchError`, which turns a client/capability incompatibility into an internal 500-class outcome;
2. bundled local capability metadata still contains verified false negatives for URL-image input on Ollama and llama.cpp;
3. the Plan 141 regression suite is too helper-oriented at several critical seams, so it did not catch the typed-error regression despite adding substantial test code.

This plan closes only those defects. It must not reopen the broader coordinator, transcoder, catalog, database, or CI architecture.

## Required outcome

After this plan lands:

- `CapabilityError` and `TranscodeLossError` raised during definitive selected-provider translation remain typed client errors and are never converted to `_LocalDispatchError`;
- selected-attempt durable/runtime ownership is converged before a client-facing 400-class rejection is returned;
- a durable finalization failure on a selected capability/transcode rejection fails closed instead of being logged and ignored;
- no provider health, backoff, quarantine, quota-failure, or retry effect is applied to local representability failures;
- Ollama and llama.cpp URL-image metadata matches their current documented OpenAI-compatible surfaces;
- the existing Plan 141 tests are tightened at the real composition boundaries rather than expanded into another integration apparatus;
- CI, dependencies, routing architecture, and protocol surface remain unchanged by this plan.

---

# Scope constraints

## In scope

Expected files:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/providers/_templates.toml`
- `tests/unit/test_plan_141_corrective_closure.py` or the closest existing focused request/transcoder test modules
- existing architecture/provider documentation only where it repeats corrected capability facts

Small edits to an existing helper module are acceptable if required to preserve typed error rendering, but do not introduce a new subsystem.

## Explicitly out of scope

Do **not**:

- implement `/v1/responses` in this plan; Plan 143 owns Codex/Responses compatibility;
- add another protocol value to `ProtocolName`;
- add provider SDKs;
- add a provider plugin framework;
- add capability-aware rerouting or a new provider-selection algorithm;
- retry another account merely because the selected provider cannot represent a requested media form;
- add a new request/finalization state machine;
- weaken reserve-before-dispatch durability;
- reintroduce semantic compression or the removed content IR;
- rewrite the pairwise OpenAI/Anthropic transcoders;
- add live-provider CI, provider matrices, new GitHub Actions jobs, OS matrices, Python matrices, soak tests, or benchmark gates;
- create another broad test harness.

If a proposed fix requires any item above, stop and choose the smaller correction.

---

# Workstream A — Preserve typed selected-provider transcode rejections

## Current defect

Plan 141 invokes `_apply_selected_provider_transcode()` inside the attempt loop after `SelectedAttempt` exists. That ordering is correct.

However, the call is currently guarded by a broad exception boundary equivalent to:

```python
try:
    await self._apply_selected_provider_transcode(...)
except asyncio.CancelledError:
    raise
except Exception as err:
    raise self._local_dispatch_error(
        context=context,
        selected=selected,
        stage="selected_provider_transcode",
        error=err,
    ) from err
```

The existing transcoders deliberately raise typed expected errors, especially `TranscodeLossError` when `loss_policy = "reject"` and the selected provider cannot represent a protected media/cache boundary. Selected-provider thinking/capability checks may likewise raise `CapabilityError`.

Those exceptions are client/request incompatibilities, not internal defects. Wrapping them as `_LocalDispatchError` converts them into a 500-class local preparation failure and bypasses the existing API renderers for typed client errors.

## Required behavior

The selected-provider transcode boundary must distinguish three classes:

1. `CapabilityError` — expected selected-provider capability rejection;
2. `TranscodeLossError` — expected client request that cannot be represented under the configured loss policy;
3. all other ordinary exceptions — unexpected local implementation faults.

Only class 3 belongs in `_LocalDispatchError`.

## Implementation steps

1. In the attempt loop, catch `CapabilityError` and `TranscodeLossError` **before** the broad `Exception` clause.
2. For `CapabilityError`:
   - use the existing selected capability-rejection finalization path;
   - finalize as `FinalizationOutcome.CLIENT_ERROR` with HTTP status 400;
   - preserve its typed exception and re-raise it after successful convergence so the API layer renders the existing protocol-specific capability error;
   - do not retry another account;
   - do not apply provider health/backoff/quarantine effects.
3. For `TranscodeLossError`:
   - add the smallest selected-attempt client-rejection finalization path needed to converge the request/attempt/reservation/runtime ownership;
   - finalize as `FinalizationOutcome.CLIENT_ERROR` with HTTP status 400;
   - re-raise the original `TranscodeLossError` after successful convergence so `proxy_request.py` uses the existing invalid-request renderer;
   - do not mutate provider health/backoff/quarantine;
   - do not retry another account.
4. Keep `_LocalDispatchError(stage="selected_provider_transcode")` only for genuinely unexpected exceptions.
5. Do not expose internal warning payloads, request media bytes, provider secrets, or stack traces in the client-facing response.

## Finalization implementation guidance

The existing `_finalize_selected_capability_rejection()` contains thinking-specific trace/counter work, so blindly reusing it for every `TranscodeLossError` would produce misleading observability.

Prefer one of these two small shapes, choosing the one with less code and fewer semantics changes:

### Option A — narrow transcode-loss helper

Add a private `_finalize_selected_transcode_loss_rejection()` that mirrors only the terminalization portion required for a 400 client error and does not emit thinking-specific metrics.

### Option B — tiny shared client-rejection terminalizer

If it **reduces** duplicated code, extract only the common `_finalize_terminal(... CLIENT_ERROR ...)` call into a small private helper used by the capability and transcode-loss wrappers. Keep capability-specific thinking metrics in the existing capability wrapper.

Do not create a hierarchy, registry, strategy object, or new finalization subsystem.

## Fail-closed correction for capability rejection

`_finalize_selected_capability_rejection()` currently logs `DatabaseError` and then allows the typed client rejection to continue. That can report a clean 400 to the caller while durable/runtime convergence is unknown.

Correct this in the same pass:

- `AcceptedFinalizationInvariantError` remains propagated;
- `DatabaseError` must also propagate into the existing fail-closed request/restart/reconciliation path unless the existing finalization supervisor has already accepted ownership and can prove the handoff;
- do not add a second cleanup owner;
- no marker should claim successful cleanup before convergence is established.

Use the already-correct oversize finalization behavior from Plan 141 as the semantic reference.

## Acceptance criteria

- selected-provider `CapabilityError` returns the existing 400-class capability response, never `_LocalDispatchError`/500;
- selected-provider `TranscodeLossError` returns the existing 400 invalid-request response, never `_LocalDispatchError`/500;
- the selected attempt/request/reservation/runtime ownership is converged before the typed 400 is rendered on the healthy finalization path;
- a simulated database/finalization failure does not continue as though the selected rejection was cleanly finalized;
- no retry occurs for these local selected-provider representability failures;
- no upstream HTTP request is built/sent after the typed rejection;
- no provider health, suppression, quarantine, circuit, or durable backoff effect is applied;
- unexpected local exceptions still remain contained as `_LocalDispatchError` and cannot crash the proxy worker.

---

# Workstream B — Correct verified local URL-image capability metadata

## Current defect

The bundled templates still state:

- Ollama: `image_input = { base64 = true, url = false }`;
- llama.cpp: `image_input = { base64 = true, url = false }`.

Current official documentation for both OpenAI-compatible serving surfaces supports URL-backed image input in addition to base64/data input. These are provider protocol-surface capabilities; they do **not** imply that every loaded model is multimodal.

vLLM was corrected in Plan 141 and should remain unchanged unless current official documentation has changed again.

## Implementation steps

1. Re-check the official Ollama OpenAI-compatibility documentation at implementation time.
2. Re-check the official `ggml-org/llama.cpp` `llama-server` documentation at implementation time.
3. If the documented behavior remains as reviewed, set:

```toml
[providers.ollama-local.model_capabilities.default.multimodal]
image_input = { base64 = true, url = true }
```

and:

```toml
[providers.llamacpp-local.model_capabilities.default.multimodal]
image_input = { base64 = true, url = true }
```

4. Update comments to state that the provider endpoint accepts the source form but the loaded model/mmproj must still support image input.
5. Keep document/audio/tool-result declarations conservative unless current official documentation provides an equally clear provider-level contract that EggPool can safely encode.
6. Do not infer additional capabilities from one example model.
7. Do not reintroduce universal serialized-request ceilings.
8. Do not broaden this pass into an audit of every provider template; change only verified false facts needed for closure.

## Acceptance criteria

- Ollama URL-image metadata matches current official OpenAI-compatible documentation;
- llama.cpp URL-image metadata matches current official `llama-server` documentation;
- vLLM remains correct;
- comments distinguish endpoint source-form support from loaded-model modality support;
- no speculative document/audio/request-size claims are added;
- template parsing and `check-config` remain valid.

---

# Workstream C — Replace weak helper assertions with real seam tests

## Problem

Plan 141 added a large focused test file, but several tests prove helpers in isolation rather than the behavior that previously failed. Examples include directly calling protocol error renderers and manually resetting `ProviderBoundRequest` instead of exercising the coordinator/API composition that owns the invariant.

The goal is **not more tests**. The goal is fewer, stronger boundary tests.

## Test policy

- Prefer editing/replacing tests inside `tests/unit/test_plan_141_corrective_closure.py` rather than adding another large module.
- Delete redundant helper-only assertions when a stronger seam test supersedes them.
- Net test LOC should stay approximately flat or decrease; do not answer this bug with another 500-line closure file.
- Do not add live provider calls.
- Do not add a new fixture framework.

## Required focused regressions

### C1. Actual API-bound provider 413

Replace the direct `openai_error_response()` / `anthropic_error_response()` assertions with a test that reaches the actual proxy exception boundary and makes the coordinator raise `RequestTooLargeError` after request context construction.

Prove:

- status is 413;
- it is not 500;
- OpenAI and Anthropic error shape is correct when cheap to parameterize;
- the generation lease is released exactly once on the error path.

Use existing request/runtime test doubles. Do not stand up a full external server.

### C2. Actual selected-provider transcode loss

Drive the attempt-loop seam far enough that `_apply_selected_provider_transcode()` raises `TranscodeLossError` after a `SelectedAttempt` exists.

Prove:

- terminal outcome is `CLIENT_ERROR` / 400;
- API-visible exception remains `TranscodeLossError` and renders 400;
- `_LocalDispatchError` is not created;
- no `_execute_upstream()` call occurs;
- no retry occurs;
- no provider failure effect is applied.

A small fake transcoder is acceptable. Do not reproduce all translator fixtures.

### C3. Selected capability rejection finalization failure

Inject `DatabaseError` through the existing finalization owner and prove:

- the error propagates/fails closed;
- the request is not returned as a clean 400 while convergence is unknown;
- no provider penalty is introduced for the original capability mismatch.

### C4. Retry source-generation authority

Upgrade the existing manual `ProviderBoundRequest` reset test so the coordinator helper is actually invoked for provider A and provider B.

Prove:

- capability lookup observes A then B using each `selected.provider_id`;
- each translation input is the original client payload generation;
- B never receives A's translated payload as its source.

This can remain a unit-level helper test; it does not need database/network machinery.

### C5. Provider template facts

Keep a tiny registry/template test that asserts the reviewed URL-image flags for Ollama, llama.cpp, and vLLM.

## Acceptance criteria

- the exact Plan 141 API-413 composition bug is guarded at the real exception boundary;
- the exact typed selected-media rejection bug is guarded at the attempt-loop/API seam;
- retry translation authority is exercised through `_apply_selected_provider_transcode()`, not only through `ProviderBoundRequest` primitives;
- redundant helper-only tests are removed where superseded;
- no new CI job or broad integration test harness is introduced.

---

# Workstream D — Documentation closure

Update only documentation that states the corrected facts:

- selected-provider media rejection is a typed client/capability failure, not a local 500;
- Ollama and llama.cpp endpoint-level URL-image support is accurately described;
- model-level multimodal availability is still discovered/constrained separately and is not guaranteed by the provider template.

Do not append another historical architecture essay. Prefer correcting existing paragraphs/comments.

---

# Ordered implementation sequence

Implement in this order:

1. preserve `CapabilityError` / `TranscodeLossError` at the selected-provider boundary;
2. make capability/transcode-loss finalization fail closed;
3. add/replace the actual boundary tests for A;
4. correct Ollama and llama.cpp template facts;
5. update the tiny template regression and existing docs;
6. run the normal lightweight verification gates.

Do not start Plan 143 until Plan 142's typed-error path is green. Responses/Codex should build on a known-correct request lifecycle.

---

# Verification

Run the existing normal gates:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Run only the focused tests changed for this closure, for example:

```bash
uv run pytest \
  tests/unit/test_plan_141_corrective_closure.py \
  tests/unit/test_transcoder/test_multimodal.py \
  tests/unit/test_provider_registry.py \
  -q --tb=short
```

Adjust filenames to existing retained modules if tests are consolidated. Do not add a new mandatory full-suite or live-provider gate.

---

# Closure checklist

Plan 142 is complete only when all of the following are true:

- [ ] selected `CapabilityError` remains a typed 400 client rejection;
- [ ] selected `TranscodeLossError` remains a typed 400 client rejection;
- [ ] neither is wrapped as `_LocalDispatchError`;
- [ ] both converge selected durable/runtime ownership before clean client rendering;
- [ ] capability/transcode finalization failure fails closed;
- [ ] no retry/provider penalty occurs for representability rejection;
- [ ] unexpected local transcode defects remain contained as 500-class local faults;
- [ ] Ollama URL-image flag is correct;
- [ ] llama.cpp URL-image flag is correct;
- [ ] vLLM remains correct;
- [ ] boundary tests exercise real composition seams rather than only render/helper primitives;
- [ ] redundant Plan 141 helper tests are reduced where superseded;
- [ ] CI remains the existing single lightweight job;
- [ ] no dependency, SDK, router subsystem, protocol type, or state machine is added.

When this checklist passes, the local/multimodal correctness portion of Plans 131–142 is closed. The only remaining item in this line of work is the separately bounded Codex/Responses compatibility closure in Plan 143.
