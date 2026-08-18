# Plan 140 — Corrective Closure: Local Providers, Multimodal Boundaries, and Provider-Bound Request Safety

Status: ready for implementation

Baseline reviewed: `42258ed1b3b5961d5d09e575ec5ae2b2a8879c08`

Related plans: 131–139

## Purpose

Plans 131–139 materially improved EggPool: semantic compression was removed, the request coordinator was reduced, the SQLite path remained conservative, local provider presets were added, multimodal capability metadata was introduced, and CI remained small. A follow-up review found a narrow set of correctness gaps in the newly added local-provider and multimodal paths.

This plan is a **corrective closure pass**, not a new architecture campaign. The goal is to make the new local/multimodal functionality correct at the provider boundary, preserve EggPool's durable request/failure-isolation invariants, and remove abstractions or metadata that are not earning their complexity.

The intended deployment remains a private/LAN SBC-oriented proxy. Do not add production-scale infrastructure, discovery daemons, provider SDKs, new CI matrices, generalized plugin systems, or additional persistence machinery.

## Required outcome

After this pass:

1. Local Ollama model discovery uses a URL and response shape EggPool's existing catalog fetcher actually supports.
2. Provider-bound serialized-size rejection is a clean local 413 outcome, not a 500, and cannot strand selected request/reservation/attempt state.
3. Multimodal capability and request-size decisions are resolved against the **selected provider**, not a global first-seen model row.
4. Prepared-transcode reuse cannot bypass selected-provider multimodal capability checks.
5. Bundled multimodal capability declarations contain only verified claims; speculative universal local limits are removed.
6. The narrow content IR is either used by the live multimodal/tool-result translation path or deleted. No dead canonical abstraction remains.
7. Plan 139 is revisited only to evaluate a minimal stateless same-protocol `/v1/responses` passthrough. No cross-protocol Responses transcoding or stateful routing is introduced by this plan.
8. The existing single-job CI shape remains unchanged.

---

# Scope constraints

## In scope

- `src/eggpool/providers/_templates.toml`
- provider/model catalog discovery wiring needed to correct local presets
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/coordinator.py` and existing extracted request helpers
- `src/eggpool/transcoder/prepared.py`
- `src/eggpool/transcoder/content.py`
- `src/eggpool/transcoder/openai_to_anthropic.py`
- `src/eggpool/transcoder/anthropic_to_openai.py`
- capability lookup helpers in the existing catalog/capability modules
- narrowly relevant docs/tests
- a narrow written re-evaluation of Plan 139

## Explicitly out of scope

Do **not**:

- create a generic provider-plugin framework;
- add provider SDK dependencies;
- add LAN scanning/mDNS/zeroconf discovery;
- add per-account base URLs;
- introduce another request state machine or finalization subsystem;
- weaken durable pre-dispatch persistence;
- add an alternate in-memory/no-SQLite dispatch mode;
- reintroduce semantic prompt compression or segmentation;
- build a general three-protocol canonical request object;
- implement Responses ↔ Anthropic translation;
- implement `previous_response_id` affinity, conversation persistence, or provider pinning;
- add new GitHub Actions jobs, OS/Python matrices, live-provider CI, benchmarks as CI gates, or broad contract suites;
- add large fixture corpora for this closure pass.

If a proposed fix requires one of the above, stop and choose a smaller correction.

---

# Workstream A — Correct Ollama catalog discovery

## Problem

The bundled Ollama preset currently combines a versioned OpenAI base URL with a native Ollama `/api/tags` model endpoint. EggPool's existing URL composer appends endpoint paths to `base_url`, and its catalog fetcher expects an OpenAI-style model response with a `data` list. This makes the preset internally inconsistent.

## Implementation

1. Keep Ollama's provider request surface on the OpenAI-compatible base URL used for chat requests.
2. Configure model discovery through the compatible `/models` endpoint relative to that base URL.
3. Do **not** add an Ollama-specific `/api/tags` response adapter merely to preserve the current template.
4. Keep model discovery dynamic; do not restore a hardcoded `llama3.2` or other fixed probe model.
5. Verify custom Ollama instance URLs continue to work through the existing `connect` custom-ID/base-URL flow.
6. Ensure malformed/empty model responses remain non-destructive according to the existing catalog refresh policy.

## Focused tests

Add or adjust only the smallest tests needed to exercise the real contract path:

- Ollama template + `compose_provider_url()` yields the correct model-list URL.
- A representative OpenAI-compatible `/models` response is accepted through `fetch_models_for_account()` for an Ollama-configured provider.
- No test should depend on a live Ollama process.

## Acceptance criteria

- [ ] Bundled Ollama discovery does not construct `/v1/api/tags`.
- [ ] Ollama discovery uses the same response shape already supported by the generic catalog fetcher.
- [ ] No Ollama-specific catalog parser is introduced.
- [ ] No fixed probe model is required.
- [ ] Existing empty/failed refresh preservation behavior remains intact.

---

# Workstream B — Make provider-bound size rejection a canonical local 413

## Problem

`max_serialized_request_bytes` is checked after durable account selection. A `RequestTooLargeError` raised there is a local preparation failure, but it currently sits outside the normal selected-attempt terminalization path and can escape through the generic request boundary incorrectly. The selected durable request/reservation/attempt and runtime quota/active ownership must converge immediately.

## Required invariant

Once a selected attempt has committed, **every local failure before upstream dispatch must have exactly one terminal owner** that converges durable state and runtime ownership before the request completes.

A provider-bound size rejection must:

- return HTTP 413 to the client;
- create no upstream request;
- apply no account health penalty;
- persist no provider backoff/quarantine;
- release reservation/quota/active-count/probe ownership exactly once;
- leave no pending request or incomplete attempt requiring restart-time repair.

## Implementation guidance

Prefer using the existing selected-local-preparation/finalization ownership paths. Do not create a second finalizer.

A suitable shape is:

1. Serialize/freeze the provider-bound request.
2. Resolve the selected provider's serialized request limit.
3. On oversize, terminalize the selected attempt/request through the existing canonical local-failure path with a local `RequestTooLargeError`/413 classification.
4. Return or propagate a typed error that `handle_proxy_request()` explicitly renders as 413.
5. Ensure this error never enters upstream failure classification/effects.

If the existing local-dispatch error wrapper always maps to 500, extend it narrowly with an HTTP/client-error classification or add a dedicated selected-local-rejection helper. Do not generalize into a new error framework.

## Focused tests

One end-to-end-ish coordinator/API regression test is more valuable than multiple unit tests of `_validate_serialized_request_size()`:

- construct a selected request whose final serialized provider body exceeds a configured provider limit;
- verify 413;
- verify upstream client `send()` is never called;
- verify durable request/attempt/reservation rows are terminal/released;
- verify active/quota ownership returns to baseline;
- verify health/backoff/quarantine state is unchanged.

Cover one non-streaming path. Add a streaming-specific test only if the implementation has materially separate cleanup code after selection.

## Acceptance criteria

- [ ] Provider-bound oversize returns 413, never generic 500.
- [ ] No upstream dispatch occurs.
- [ ] The selected request/attempt/reservation are terminalized synchronously through the canonical owner.
- [ ] No runtime ownership leaks remain.
- [ ] No provider health/backoff/quarantine penalty occurs.
- [ ] Cancellation/failure ownership invariants are not weakened.

---

# Workstream C — Resolve multimodal and size capabilities against the selected provider

## Problem

Collapsed models can be served by multiple providers with different media capabilities and request-size ceilings. Global `get_model(model_id)` metadata is therefore not authoritative for provider-bound decisions.

## Implementation

1. Any decision made **after provider selection** about:
   - `max_serialized_request_bytes`;
   - image source forms;
   - document source forms;
   - audio support;
   - non-text tool-result support;
   must resolve capabilities using `(model_id, selected.provider_id)` through the existing provider-scoped catalog lookup.
2. A provider-scoped row may fall back through the existing catalog semantics only when that fallback is already explicitly supported; do not silently borrow another provider's capability declaration.
3. Keep `unknown` conservative. Unknown capability must not authorize a transformation the target may not support.
4. Do not add per-account capability duplicates unless there is an actual account-level difference represented by existing configuration.

## Regression scenario

Create one focused test with two providers advertising the same collapsed model but different multimodal/request-size capabilities. Select each provider deterministically and assert EggPool applies that provider's own contract.

## Acceptance criteria

- [ ] No provider-bound request-size decision uses global first-seen model metadata when a provider is selected.
- [ ] Multimodal translation/gating uses selected-provider capability metadata.
- [ ] Two providers exposing the same model may safely have different limits/source-form support.
- [ ] Unknown capability remains conservative rather than inheriting an unrelated provider's support.

---

# Workstream D — Prevent `PreparedTranscode` reuse from bypassing provider-specific gating

## Problem

Preflight translation happens before account/provider selection. A cached `PreparedTranscode` can later be reused based on protocol/features, but provider-specific multimodal capability state is not part of that validity decision. That optimization must not allow a request translated under generic/unknown assumptions to bypass the selected provider's media restrictions.

## Preferred corrective strategy

Choose the simplest safe solution.

### Preferred option: conditional recompute

1. Detect whether the request contains provider-sensitive multimodal content.
2. For text-only/tool-schema-only requests, retain the existing prepared-transcode reuse fast path.
3. For provider-sensitive media/tool-result content, force the final body translation after provider selection using selected-provider multimodal capabilities.
4. Record an existing-style bounded recompute reason such as `provider_multimodal_capability_required`.

This is preferred over putting a large capability fingerprint into `PreparedTranscode` because preflight does not yet know which provider will be selected.

### Acceptable alternative

If implementation proves smaller, permit preflight translation for structural/context checks but mark the result non-reusable whenever media/tool-result capability gating could differ by provider.

## Do not

- serialize the full capability object into prepared diagnostics;
- add provider selection to API preflight;
- disable prepared-transcode reuse globally;
- duplicate full translation twice for ordinary text requests.

## Acceptance criteria

- [ ] A media-bearing request cannot dispatch a preflight translation that was never validated against the selected provider's capabilities.
- [ ] Ordinary text requests retain prepared-transcode reuse.
- [ ] The correction does not move provider selection earlier in the lifecycle.
- [ ] Loss-policy behavior is consistent between preflight and final selected-provider translation.

---

# Workstream E — Audit and correct bundled multimodal capability metadata

## Problem

Plan 134 introduced provider multimodal fixtures with several broad assumptions, including a universal 5 MiB serialized request ceiling for local runtimes and provider-specific image-source claims. Capability metadata is part of routing/translation correctness and must not encode guessed limits.

## Implementation

1. Audit current bundled multimodal declarations for:
   - Ollama;
   - LM Studio;
   - llama.cpp;
   - vLLM;
   - LocalAI;
   - direct Anthropic.
2. Verify only against current official provider/runtime documentation or clearly defined EggPool compatibility behavior.
3. Remove `max_serialized_request_bytes` when no provider-defined request limit is verified. Do not invent a conservative local ceiling merely to bound memory; inbound EggPool request limits already serve that purpose.
4. Correct source-form support declarations where official compatibility surfaces support URL or base64 media.
5. Keep model-dependent modalities conservative. If a runtime's audio/PDF support depends on the loaded model and EggPool cannot discover that reliably, leave the granular capability unknown/false rather than claiming universal support.
6. For Anthropic, encode the direct Messages API serialized-request limit only if verified for the direct API. Do not copy a partner-platform limit into the direct provider template.
7. Prefer omission/unknown over brittle version-specific claims.

## Documentation

Add a brief note near the template/capability documentation that bundled local-runtime capabilities represent verified protocol-surface behavior, not guarantees that every loaded model supports the modality.

## Acceptance criteria

- [ ] No universal local 5 MiB serialized-request ceiling remains unless backed by an actual runtime contract.
- [ ] Direct Anthropic uses its verified direct-API request ceiling or leaves the field unset if not safely representable.
- [ ] Image URL/base64 declarations match current compatibility surfaces.
- [ ] Model-dependent audio/document claims remain conservative.
- [ ] No new provider-specific SDK or live CI is added.

---

# Workstream F — Resolve the content IR: use it narrowly or remove it

## Problem

`transcoder/content.py` currently defines a narrow content IR, but the live OpenAI↔Anthropic transcoders still walk protocol dictionaries directly. Keeping an unused canonical representation adds conceptual and test surface without reducing pairwise complexity.

## Decision rule

Inspect the actual implementation cost before changing behavior.

### Option 1 — Wire the IR narrowly

Choose this only if the change is modest and clearly removes duplicated multimodal/tool-result conversion code.

Permitted IR scope:

- text content;
- image content/source type;
- document content/source type;
- audio content;
- tool-use content;
- tool-result nested content;
- thinking/redacted-thinking content where already naturally represented.

Keep these outside the IR:

- sampling parameters;
- cache-control policy;
- thinking-budget control translation;
- tool-choice semantics;
- structured output;
- provider extensions;
- response envelope/streaming protocol.

The ideal path is:

`OpenAI content blocks -> narrow content IR -> Anthropic content blocks`

and the reverse, while top-level protocol translation stays pairwise.

### Option 2 — Delete the IR

Choose this if wiring it would add adapters/visitors or materially increase total code. Remove `content.py`, its IR-only tests, and docs claiming it is the canonical content representation. Keep the current pairwise translators and the granular capability model.

For only two supported protocols, deletion is preferable to a framework that does not materially reduce code.

## Acceptance criteria

Exactly one of the following must be true:

- [ ] live multimodal/tool-result conversion uses the narrow content IR and duplicate conversion logic is measurably reduced; **or**
- [ ] the unused content IR and its dedicated dead-surface tests/docs are removed.

Additionally:

- [ ] No general request IR is introduced.
- [ ] No Responses object model is added through this workstream.
- [ ] Net complexity must decrease or remain approximately flat.

---

# Workstream G — Re-evaluate Plan 139 only for stateless same-protocol Responses passthrough

## Purpose

Plan 139 deferred `/v1/responses` based partly on assumptions about target clients and local runtime support. Revisit the decision with current evidence, but constrain the evaluation to a substantially smaller feature than the one Plan 139 rejected.

## Question to answer

Would **stateless, same-protocol, OpenAI-compatible `/v1/responses` passthrough** provide enough compatibility value to justify a small endpoint/routing surface?

This means:

- client speaks Responses;
- selected upstream also exposes a compatible Responses endpoint;
- EggPool does not translate Responses to/from Anthropic;
- EggPool does not persist or remap response IDs;
- unsupported stateful conversation features are rejected locally or explicitly documented unsupported;
- ordinary pre-response failover is allowed only where request semantics remain stateless/replayable.

## Required research/evaluation

At implementation time, verify from primary/current sources:

1. Which target coding clients actually use `/v1/responses` today, especially Codex-like clients.
2. Which bundled local runtimes expose a sufficiently compatible stateless Responses surface.
3. Whether their `/v1/responses` request/stream event shapes are close enough for transparent passthrough.
4. Which stateful fields must be rejected to preserve safe failover.
5. Whether routing can reuse the existing provider/account machinery without introducing a third protocol transcoder.

## Decision outcomes

Record one of:

- `responses_stateless_passthrough: implement`
- `responses_stateless_passthrough: defer`

### If `defer`

Update Plan 139's closure record with corrected evidence and stop. No code is required.

### If `implement`

Do **not** implement it as part of an opportunistic edit inside this closure pass unless the code change is demonstrably small. If it is more than a focused endpoint/provider-contract addition, write a new narrowly scoped implementation plan first.

Any future implementation must still reject:

- cross-protocol Responses ↔ Anthropic translation;
- persistent `previous_response_id` mappings;
- provider-affinity databases/state machines created solely for Responses;
- hidden fallback that sends a Responses payload to Chat Completions.

## Acceptance criteria

- [ ] Plan 139's evidence is updated using current primary sources.
- [ ] The evaluation distinguishes stateless passthrough from full Responses semantic parity.
- [ ] No stateful provider-affinity architecture is introduced by this plan.
- [ ] No Responses↔Anthropic transcoder is introduced.
- [ ] Any implementation decision larger than a focused change receives a separate plan.

---

# Workstream H — Minimal verification and cleanup

## Required test philosophy

This closure pass must not recreate the test/CI bloat removed in earlier work.

Add only regression tests that guard the identified failure boundaries. Prefer extending existing test modules rather than creating new directories/frameworks.

Target new/changed coverage:

1. Ollama compatible model-discovery URL + response shape.
2. One selected-provider oversize lifecycle test proving 413 + no leaked ownership/provider penalty.
3. One collapsed-model test proving provider-specific capability/limit resolution.
4. One prepared-transcode media test proving selected-provider gating cannot be bypassed.
5. IR tests only if the IR remains live; delete IR-only tests if the IR is removed.

Do not add live-provider tests to CI.

## Required commands

Run the existing project gates:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Run only the focused affected unit/contract tests in addition to smoke, for example:

```bash
uv run pytest \
  tests/unit/test_connect.py \
  tests/unit/test_contract_urls.py \
  tests/unit/test_catalog_exposure_collapse.py \
  tests/unit/test_transcoder/test_multimodal.py \
  -q --tb=short
```

Adjust file names only to match where the minimal regression tests actually land.

## CI constraint

`.github/workflows/ci.yml` should normally remain untouched.

If this plan results in a new CI job, matrix, live service, benchmark gate, or dependency installation solely for these fixes, the implementation has exceeded scope.

---

# Execution order

Implement in this order to minimize invalid intermediate states:

1. **A — Ollama discovery correction**
2. **B — Canonical provider-bound 413 lifecycle**
3. **C — Selected-provider capability/limit lookup**
4. **D — Prepared-transcode provider-sensitive reuse guard**
5. **E — Capability metadata audit/correction**
6. **F — Content IR use-or-remove decision**
7. **G — Narrow Responses re-evaluation**
8. **H — Final focused verification/document cleanup**

Do not start G until A–F are stable; Responses evaluation must not distract from correcting already-shipped paths.

---

# Global acceptance criteria

This corrective pass is complete only when all applicable criteria below are satisfied:

## Local provider behavior

- [ ] Ollama's bundled model discovery works through EggPool's existing generic compatible catalog path.
- [ ] LM Studio/llama.cpp/vLLM/LocalAI presets continue to parse and connect.
- [ ] Multiple named local provider instances can still expose the same collapsed model and route independently.
- [ ] No LAN auto-discovery or per-account URL model is introduced.

## Failure isolation and durable lifecycle

- [ ] A provider-bound serialized-size rejection returns 413.
- [ ] It performs zero upstream I/O.
- [ ] It does not penalize provider/account health.
- [ ] It leaves no pending request, active reservation, incomplete attempt, quota reservation, active-request count, or half-open probe behind.
- [ ] No restart/database repair is needed to recover from that request.

## Multimodal correctness

- [ ] Media translation is gated by the selected provider's capabilities.
- [ ] Collapsed models with heterogeneous providers cannot borrow another provider's media support/limits.
- [ ] Prepared transcode reuse cannot skip provider-specific media validation.
- [ ] Loss-policy `reject` still rejects protected multimodal loss before upstream dispatch and without provider penalty.
- [ ] Serialized request limits are based on final provider bytes, not decoded attachment size alone.

## Simplification

- [ ] Speculative capability ceilings/claims are removed or corrected.
- [ ] The content IR is either live and useful or gone.
- [ ] Semantic compression/segmentation remains removed.
- [ ] No new mandatory dependency is added.
- [ ] No new generic abstraction/framework is introduced for these fixes.

## Responses decision

- [ ] Plan 139 is re-evaluated specifically for stateless same-protocol passthrough using current primary evidence.
- [ ] Full Responses parity, stateful affinity, and cross-protocol Responses transcoding remain out of scope.

## Verification/CI

- [ ] Existing Ruff/Pyright/smoke gates pass.
- [ ] Focused regression tests for the four identified correctness boundaries pass.
- [ ] CI remains the existing single lightweight job.
- [ ] No broad provider/live/performance matrix is introduced.

---

# Handoff notes for the implementing model

This plan intentionally favors deletion and reuse over architecture growth.

When choosing between two fixes, prefer the one that:

1. reuses an existing ownership/finalization path;
2. keeps provider behavior declarative in the existing template/config model;
3. delays provider-specific decisions until the provider is actually selected;
4. removes guessed metadata rather than encoding uncertainty as false precision;
5. adds one boundary regression test instead of several implementation-detail tests;
6. reduces or preserves source/test surface rather than growing it.

Do not treat plan text or prior closure records as proof that a feature works. Verify the actual URL composition, selected-provider lookup, exception/finalization path, and transcode-reuse path in code.

The expected result is a **small corrective diff** relative to plans 131–139, not another broad refactor.
