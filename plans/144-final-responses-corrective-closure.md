# Plan 144 — Final Responses Corrective Closure

Status: ready for implementation

Baseline reviewed: `50edfa96fad8b418ac327589821485614f93cc0b`

Related plans: 131–143, especially 142–143

## Purpose

Plans 142 and 143 put the local-provider, multimodal, and Codex/Responses work into the intended architecture:

- selected-provider capability/transcode rejections are typed client outcomes rather than generic local 500s;
- `/v1/responses` exists as an OpenAI-family request surface rather than a third transcoding protocol;
- providers opt into Responses through `responses_path`;
- the Codex renderer uses the current `model_providers` / Responses wire shape;
- the single-job CI posture and dependency set remain lean.

The post-implementation review found a small set of correctness gaps that must be closed before Plans 131–144 can be considered complete:

1. the bundled Ollama OpenAI-compatible image metadata still records a false-negative URL-image capability if current upstream documentation continues to list Image URL support;
2. the Responses endpoint still enters generic transcode preflight / endpoint-transcode resolution, so an OpenAI Responses request can conceptually fall toward an Anthropic `/messages` route despite the stated same-protocol-only contract;
3. Responses terminal SSE events are not classified correctly: `response.failed` and `response.incomplete` can currently satisfy the generic `saw_terminal_event => complete` rule and therefore be durably recorded as success;
4. stateless admission is incomplete: string-valued conversation references bypass the current dictionary-only check, and omission of `store` does not guarantee the advertised stateless/no-provider-retention behavior;
5. Responses still enters the generic provider transform pipeline, whose thinking-control adapter is not explicitly Chat-only. A passthrough surface must not silently acquire Chat/Anthropic body mutations.

This plan closes only those defects. It is the final corrective pass for this line of work; it must not become a broader Responses implementation.

---

# Required end state

After Plan 144 lands:

- `/v1/responses` is strictly OpenAI-family, same-protocol passthrough;
- a Responses request can never select or construct an Anthropic `/messages` upstream route and can never invoke the OpenAI↔Anthropic transcoder;
- provider-bound Responses JSON is unchanged except the already-required EggPool model/provider suffix normalization and ordinary transport/header handling;
- stateful Responses references are rejected before durable account selection or upstream I/O;
- the stateless contract has an explicit `store` rule rather than relying on provider defaults;
- `response.completed` is the only successful canonical Responses terminal event;
- `response.failed` and `response.incomplete` are terminal non-success outcomes and are never persisted as successful completions;
- failed/incomplete Responses events are still forwarded unchanged to the client and do not trigger an unsafe retry after response handoff;
- Ollama/llama.cpp/vLLM local image source-form metadata contains no known false-negative claim after re-verification;
- Plan 142 typed-error/fail-closed semantics remain unchanged;
- the Codex renderer and `responses_path` design remain unchanged unless a correction is strictly necessary for these defects;
- no new dependency, provider SDK, persistent response store, provider affinity subsystem, CI job, matrix, live-provider CI, or general Responses framework is introduced.

---

# Scope constraints

## Expected production files

Implementation should normally be limited to:

- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/upstream_helpers.py`
- `src/eggpool/request/coordinator.py` only if the real attempt-loop seam requires a narrow guard/finalization adjustment
- `src/eggpool/request/transform_pipeline.py`
- `src/eggpool/proxy/sse_observer.py`
- `src/eggpool/request/stream_completion.py`
- `src/eggpool/providers/_templates.toml`
- existing focused tests, primarily:
  - `tests/unit/test_responses_passthrough.py`
  - `tests/unit/test_plan_141_corrective_closure.py`
  - existing stream-completion tests if a better home already exists
- README / architecture text only where the corrected behavior changes an existing claim.

A smaller file set is preferred if the acceptance criteria can be met safely.

## Explicitly out of scope

Do **not**:

- add `responses` to `ProtocolName`;
- add a third protocol family or canonical Responses IR;
- implement Responses ↔ Anthropic translation;
- implement Responses ↔ Chat Completions translation;
- add `GET /v1/responses/{id}`, delete, cancel, compact, or other Responses endpoints;
- persist `response.id`, `previous_response_id`, conversation IDs, or provider turn/session state;
- add sticky routing/provider affinity for Responses;
- proxy or emulate Responses WebSocket transport;
- implement `background=true` jobs;
- emulate provider-hosted tools;
- add a provider SDK;
- build a provider-specific Responses plugin framework;
- redesign the router, retry classifier, finalizer, SSE decoder, or request coordinator;
- add new GitHub Actions jobs, Python/OS matrices, live-provider tests, soak tests, or benchmark gates;
- add a new plan-specific mega-suite when the existing focused files can be corrected.

If one of the fixes appears to require any item above, stop and reassess the assumption. Do not expand Plan 144 to preserve an unnecessary compatibility claim.

---

# Invariants to preserve

These are non-negotiable throughout implementation.

## I1 — Existing protocol model

```python
ProtocolName = Literal["openai", "anthropic"]
```

Responses remains an OpenAI request **surface**, not a protocol.

## I2 — Same-protocol Responses only

For `request_surface == "responses"`:

```text
client protocol       = openai
upstream protocol     = openai
transcode_required    = false
BodyTranscoder        = none
StreamingTranscoder   = none
provider route        = responses_path
```

There is no valid Responses→Anthropic or Responses→Chat translation path.

## I3 — No hidden request-body rewrite

After EggPool strips its provider suffix from `model`, the Responses payload is passthrough data.

Allowed mutations:

- canonical exposed model ID -> upstream base model ID;
- transport/header/auth mechanics that are outside the JSON body.

Not allowed:

- Chat `stream_options.include_usage` injection;
- Anthropic thinking-budget rewrites;
- Chat/Anthropic request-shape transforms;
- silent deletion of unsupported stateful fields;
- provider-specific Responses body normalization invented by EggPool.

## I4 — Stateful features fail before durable selection

A request rejected for stateful Responses semantics must not:

- create/select a provider attempt;
- reserve quota;
- touch provider health/backoff/quarantine;
- send upstream I/O.

## I5 — Terminal event is not synonymous with success

`response.completed`, `response.failed`, and `response.incomplete` are all terminal evidence, but only `response.completed` is successful completion evidence.

## I6 — No unsafe retry after downstream handoff

Once a Responses stream has emitted data downstream, a terminal failed/incomplete event must not trigger failover to another provider/account. The terminal event itself is the client-visible outcome.

## I7 — Plan 142 semantics stay intact

Do not regress:

- typed `CapabilityError` / `TranscodeLossError` client handling;
- canonical selected-attempt finalization;
- fail-closed durable-finalization behavior;
- provider-health isolation for local representability failures;
- provider-bound 413 behavior.

---

# Workstream A — Correct local image source-form metadata

## Goal

Remove the remaining false-negative source-form metadata without introducing speculative capability claims.

## A1 — Re-verify official upstream documentation first

Immediately before implementation, re-check current official documentation for:

- Ollama OpenAI compatibility;
- llama.cpp `llama-server` OpenAI-compatible image input;
- vLLM OpenAI-compatible multimodal input.

Do not use secondary blog posts or stale issue comments when official documentation is available.

## A2 — Ollama correction

At the reviewed baseline, the bundled Ollama template says:

```toml
image_input = { base64 = true, url = false }
```

and claims URL images are unsupported on the OpenAI-compatible surface.

If the current official Ollama OpenAI-compatibility documentation still lists Image URL support, change the template to:

```toml
image_input = { base64 = true, url = true }
```

and correct the nearby comment.

This is **endpoint source-form metadata**, not a claim that every loaded Ollama model is multimodal. Model/mmproj capability remains independently discovered/unknown.

If official documentation has changed and no longer supports URL images, retain `false` and record the current official evidence in the implementation commit/updated test comment. Do not guess.

## A3 — Preserve verified llama.cpp / vLLM values

Do not modify the current `url = true` declarations for llama.cpp or vLLM unless current official documentation contradicts them.

## Acceptance criteria — Workstream A

- no local template contains a known false-negative image URL source-form claim;
- no template asserts that every model loaded by a runtime supports images;
- no speculative serialized request-size limit is reintroduced;
- tests assert only facts backed by current official provider documentation.

---

# Workstream B — Make Responses strictly same-protocol passthrough

## Goal

Make the code enforce the architectural statement already made in Plan 143: `/v1/responses` is OpenAI-family same-protocol passthrough only.

## B1 — Skip transcode preflight at the API boundary

In `src/eggpool/api/proxy_request.py`, the Responses surface must not call `_prepare_transcode_preflight()`.

Preferred behavior:

```python
if endpoint.request_surface == "responses":
    preflight = None
    prepared_transcode = None
else:
    # existing Chat / Anthropic preflight
```

Keep the normal decoded payload, body-size, context-limit, model/provider parsing, and reservation-token estimation paths as applicable.

Do not create a dummy Responses transcoder.

## B2 — Fail closed in endpoint/protocol validation

`validate_endpoint_or_transcode()` currently allows protocol mismatch to resolve through an available transcodable protocol.

Add a narrow surface-aware rule before that generic fallback:

```text
if request_surface == responses:
    require native OpenAI model/protocol support
    never call resolve_upstream_protocol() for Anthropic fallback
    leave transcode_required = false
    leave upstream_protocol = openai
```

Use the existing typed local protocol/model error style for failure. Do not return a generic internal 500.

The exact exception may reuse `ProtocolMismatchError` or another existing 4xx model/protocol error if it already represents this condition accurately.

## B3 — Make upstream URL resolution impossible for non-OpenAI Responses

In `upstream_helpers.py`, surface selection must take precedence over the generic Anthropic branch.

Required rule:

```text
request_surface == responses AND protocol != openai
    => local typed error / impossible-state failure
```

Never return `/messages` for `request_surface == "responses"`.

Delete or reverse any test that currently expects:

```text
Responses surface + anthropic protocol -> /messages
```

That expectation contradicts the product contract.

## B4 — Keep provider eligibility explicit

Preserve the existing `responses_path` requirement.

A provider is eligible for Responses only when:

- it is an OpenAI provider for the selected model/account; and
- it declares a non-null `responses_path`.

Do not infer Responses support from `openai_path` alone.

## B5 — No implicit Chat fallback

A provider without `responses_path` must not receive the request at `/chat/completions`.

If all candidates lack `responses_path`, return the existing local no-eligible/model-unavailable style outcome. Do not rewrite the request to Chat Completions.

## Acceptance criteria — Workstream B

- `_prepare_transcode_preflight()` is not invoked for Responses requests;
- `PreparedTranscode` is never created for Responses;
- `context.transcode_required` remains false throughout a Responses request;
- `context.upstream_protocol` remains `openai`;
- no BodyTranscoder or streaming transcoder is selected;
- `get_upstream_url(... request_surface="responses")` cannot return `/messages` or `/chat/completions`;
- a model/provider combination that only has Anthropic support is rejected locally before upstream I/O;
- a provider without `responses_path` is never selected for Responses;
- existing Chat Completions↔Anthropic transcoding behavior is unchanged.

---

# Workstream C — Enforce real passthrough body semantics

## Goal

Ensure the new surface does not bypass the transcoder only to be modified by generic Chat/Anthropic provider transforms.

## C1 — Skip Chat/Anthropic body transforms for Responses

The current provider transform pipeline includes:

- selected-provider thinking-control normalization;
- Chat `stream_options.include_usage` injection.

The stream-options adapter already skips `request_surface == "responses"`.

Make this policy explicit for the entire body-transform layer:

```text
Responses:
    model base-ID normalization: allowed
    auth/static/forwarded headers: allowed
    URL selection: allowed
    body adapters: skipped
```

At minimum, the thinking-control adapter must not mutate a Responses payload.

Preferred minimal implementation:

- have `build_provider_transforms()` / `run_provider_transforms()` return only transforms valid for the current request surface; or
- make the thinking adapter explicitly return `SKIPPED` for Responses.

Do not build a separate Responses transform pipeline.

## C2 — Preserve provider suffix normalization

EggPool still needs to accept exposed model IDs such as:

```text
model/provider-id
```

and send the upstream provider its base model ID.

That normalization does not violate passthrough semantics.

## C3 — Do not silently strip stateful fields

If a stateful Responses field is unsupported, reject the request. Do not mutate the provider payload by deleting the field and continuing.

## Acceptance criteria — Workstream C

For an accepted Responses request, a focused test proves:

```text
client JSON payload
    == upstream JSON payload
```

except for the documented `model` provider-suffix/base-ID normalization.

The test must include at least one field that the generic Chat/Anthropic transform code might otherwise inspect, so it proves the surface gate rather than only comparing a trivial payload.

---

# Workstream D — Tighten stateless admission

## Goal

Make “stateless Responses” an enforced request contract rather than a partial field heuristic.

## D1 — Conversation references

The current code only rejects non-empty dictionaries.

Responses conversation references can be represented by more than that single shape. Reject any real conversation binding, including:

```json
{"conversation": "conv_123"}
```

and populated object forms.

Preferred narrow rule:

```python
conversation = payload.get("conversation")
if conversation is not None:
    reject
```

This intentionally also rejects `{}` rather than trying to distinguish malformed-but-empty conversation objects from stateful ones. The surface is simpler and safer when the field is absent/null only.

## D2 — Previous response ID

Continue rejecting non-null `previous_response_id`.

There is no need to preserve an empty-string exception merely to forward a value that is not useful stateless state. Prefer a simple absent/null-only contract if it reduces branching:

```python
if payload.get("previous_response_id") is not None:
    reject
```

## D3 — `store` must be explicit

The public contract says EggPool's Responses surface is stateless and does not preserve provider response state.

Do not rely on each provider's default when the key is omitted.

Preferred policy for this closure:

```text
store == false  -> allowed
store absent    -> reject 400 with message requiring store=false
store == true   -> reject 400
other value     -> reject through normal validation/error handling
```

Rationale:

- it preserves Plan 143's no-silent-rewrite principle;
- it gives all providers the same explicit stateless request contract;
- current Codex sends `store=false`, so the intended Codex integration remains compatible;
- EggPool does not need to mutate user JSON to force a provider default.

Do **not** silently inject `store=false` unless implementation-time evidence shows current Codex/providers require omission and a plan amendment explicitly records that tradeoff. The default implementation should require explicit false.

## D4 — Background mode

Continue rejecting `background == true`.

`background == false` or absence may remain accepted because it does not create asynchronous response state.

## D5 — Admission ordering

All D1–D4 checks must execute before:

- routing;
- durable request/attempt/reservation creation;
- provider health interaction;
- upstream URL/client construction;
- upstream I/O.

## Acceptance criteria — Workstream D

API-bound tests prove HTTP 400 and zero coordinator/upstream invocation for:

- string `conversation`;
- object `conversation`;
- non-null `previous_response_id`;
- `store=true`;
- omitted `store` under the explicit-false policy;
- `background=true`.

And prove acceptance for:

- `store=false`;
- no conversation/reference fields;
- `background=false` or absent.

Do not stop at direct unit tests of `_validate_responses_stateless()`; exercise at least one real ASGI/API boundary rejection so ordering is proven.

---

# Workstream E — Correct Responses terminal stream semantics

## Goal

A terminal SSE event must carry both **terminality** and **success/failure meaning**.

The current observer records a `terminal_kind`, but the generic EOF classifier treats every `saw_terminal_event` as success unless parser errors occurred. That makes `response.failed` and `response.incomplete` eligible to become durable successful completions.

Fix this without redesigning the SSE subsystem.

## E1 — Observer terminal kinds

Use distinct terminal kinds for at least:

```text
response.completed   -> responses_completed
response.failed      -> responses_failed
response.incomplete  -> responses_incomplete
```

Do not map `response.incomplete` to the same terminal kind as `response.completed`.

The exact string names are implementation-defined; semantic distinction is mandatory.

Chat `[DONE]` and Anthropic `message_stop` behavior must remain unchanged.

## E2 — Classifier success rule

`classify_stream_eof()` must not use:

```python
if snapshot.saw_terminal_event:
    complete
```

as a universal success rule.

Required semantics:

```text
Chat openai_done              -> complete
Anthropic message_stop        -> complete
Responses response.completed  -> complete
Responses response.failed     -> terminal non-success
Responses response.incomplete -> terminal non-success
```

The minimal implementation may extend `EOFClassification` with narrow values such as:

```text
terminal_failure
terminal_incomplete
```

or use an equivalently small explicit result field. Do not add a general event-state framework.

## E3 — Coordinator/finalizer handling

For `response.failed` or `response.incomplete`:

- forward the upstream SSE event unchanged to the client;
- do not durably finalize the request as success;
- do not record `STREAM_OUTCOME_COMPLETED_CANONICAL`;
- do not retry another provider/account after downstream response handoff;
- record a bounded non-success diagnostic/finalization outcome through existing lifecycle ownership;
- do not classify the mere existence of the terminal event as a parser/malformed error if the frame itself is valid.

Implementation should reuse the smallest existing terminal failure/finalization primitive that can represent this truthfully.

If adding `terminal_failure` / `terminal_incomplete` to `StreamEOFDecision` is simpler than overloading `PrematureStreamEOFError`, prefer the explicit narrow classifications. A provider explicitly saying “failed” is not the same fact as a transport EOF that arrived too early.

## E4 — Provider health effects

Do not automatically treat every valid `response.incomplete` event as provider infrastructure failure.

`response.failed` may contain provider/model failure detail, but Plan 144 should not build a new Responses error classifier.

Required conservative policy for this closure:

- finalization is non-success;
- no retry after handoff;
- no new broad provider penalty/backoff rule based solely on the terminal event type;
- preserve raw/bounded diagnostic detail already available for operator visibility.

A future evidence-backed provider-specific classifier is outside scope.

## E5 — Non-streaming Responses

Do not invent streaming terminal-event logic for non-streaming JSON responses.

Normal upstream HTTP status and existing non-streaming finalization rules continue to own non-streaming requests.

## Acceptance criteria — Workstream E

Focused tests prove:

1. `response.completed` -> canonical successful stream completion;
2. `response.failed` -> terminal non-success, never `complete`, never canonical-success diagnostics/finalization;
3. `response.incomplete` -> terminal non-success, never `complete`;
4. EOF after deltas with no terminal event remains premature/incomplete under the existing completion policy;
5. Chat `[DONE]` remains success;
6. Anthropic `message_stop` remains success;
7. a valid failed/incomplete event is forwarded and does not trigger retry after downstream handoff;
8. failed/incomplete handling does not accidentally apply a new provider backoff/quarantine effect.

---

# Workstream F — Focused test correction, not test expansion

## Goal

Close the real composition seams while reducing misleading helper assertions where possible.

The existing Plan 143 test file is already large. Do not create another 500-line plan-specific test file.

## F1 — Replace contradictory tests

Remove or rewrite tests that currently encode incorrect behavior, including any assertion equivalent to:

```text
responses + anthropic -> /messages
```

and any assertion that treats `response.incomplete` or `response.failed` as successful completion.

## F2 — Required seam tests

Prefer 4–6 focused tests across existing files rather than dozens of helper tests.

### Test 1 — API stateless boundary

Drive the real `/v1/responses` ASGI handler with at least:

- `conversation: "conv_123"`;
- omitted `store` under the explicit-false rule;
- `store: false` control case.

Assert rejection happens before coordinator execution/provider I/O.

### Test 2 — Same-protocol guard

Drive the real endpoint/coordinator validation seam with a model whose available route is Anthropic-only/transcodable.

Assert:

- local 4xx;
- no transcode preflight/BodyTranscoder invocation;
- no `/messages` URL construction;
- no upstream I/O.

### Test 3 — Responses payload passthrough

With a Responses-capable OpenAI provider, capture the provider-bound JSON just before send.

Assert exact payload equality except model suffix normalization.

Include a reasoning/thinking-like field so the test proves generic thinking-control transforms are skipped.

### Test 4 — terminal completed

Real observer + classifier path:

```text
response.completed -> complete
```

### Test 5 — terminal failed/incomplete

Real observer + classifier path:

```text
response.failed     -> non-success
response.incomplete -> non-success
```

At least one should drive far enough through the coordinator streaming seam to prove it is not durably finalized as success and is not retried after handoff.

### Test 6 — provider metadata

Keep one compact template assertion covering corrected Ollama plus llama.cpp/vLLM verified values.

## F3 — Plan 142 seam gap

If the implementation naturally touches the Plan 142 selected-transcode path, strengthen the existing transcode-loss test to drive the actual attempt-loop typed catch rather than only calling `_apply_selected_provider_transcode()` directly.

This is optional unless needed for the changed code. Do not expand Plan 144 solely to rewrite already adequate tests.

## F4 — Avoid test-framework growth

Do not add:

- live external provider tests;
- Docker/provider fixtures;
- a new fake provider server framework;
- snapshot tooling;
- hypothesis/property suites;
- new CI test tiers.

Use existing ASGI/httpx mocks, coordinator seams, and fixture utilities.

---

# Workstream G — Documentation truth pass

Update documentation only where the corrected behavior changes an existing statement.

At minimum make the Responses stateless contract precise:

```text
POST /v1/responses
- OpenAI-family same-protocol passthrough only
- provider must declare responses_path
- no Chat/Anthropic translation
- no stateful conversation/previous-response routing
- store=false is required if Workstream D adopts the preferred policy
- response.completed is success; failed/incomplete are terminal non-success
```

Do not add another long architectural essay. Prefer editing the existing Plan 143 paragraphs in:

- `README.md`;
- `AGENTS.md` if operational guidance is wrong;
- `architecture/deep-dive-request-lifecycle.md` / `architecture/README.md` only where necessary.

Historical plan files should not be rewritten except for a short closure note if the implementation process already uses that convention.

---

# Ordered implementation sequence

Implement in this order so each step narrows the surface before stream finalization changes are tested.

## Step 1 — Re-verify source-form facts

- check official Ollama/llama.cpp/vLLM docs;
- correct only proven metadata;
- update compact metadata tests.

## Step 2 — Lock Responses to native OpenAI

- skip transcode preflight in API path;
- make endpoint validation surface-aware;
- prohibit Responses URL resolution for non-OpenAI protocol;
- correct contradictory tests.

Do not proceed until a Responses request cannot reach the Anthropic transcoder or `/messages` route.

## Step 3 — Enforce passthrough transforms

- surface-gate thinking/body transforms;
- preserve model suffix normalization;
- add one provider-bound payload equality test.

## Step 4 — Tighten stateless admission

- conversation string/object rejection;
- previous-response rule simplification if chosen;
- explicit `store=false` requirement;
- background rejection;
- API-bound ordering test.

## Step 5 — Fix terminal event semantics

- distinct observer terminal kinds;
- classifier success/non-success distinction;
- coordinator/finalizer non-success handling;
- no retry after handoff;
- focused completed/failed/incomplete tests.

## Step 6 — Documentation and verification

- update only stale Responses/Ollama claims;
- run focused tests;
- run existing smoke/lint/typecheck commands;
- inspect diff for accidental CI/dependency/protocol expansion.

---

# Verification commands

Use the repository's existing environment and commands. Do not add a CI tier solely for Plan 144.

Minimum local verification:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/unit/test_responses_passthrough.py -q
uv run pytest tests/unit/test_plan_141_corrective_closure.py -q
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

If existing stream-completion tests are modified, run that focused file as well.

Optional broader verification if inexpensive in the implementation environment:

```bash
uv run pytest tests/contract/ -q
```

Do not make the broader suite or external-provider testing a new mandatory CI gate for this SBC/local project.

---

# Explicit acceptance checklist

Plan 144 is complete only when every item below is true.

## Metadata

- [ ] Current official Ollama OpenAI-compatible image URL support has been re-verified.
- [ ] Ollama `image_input.url` matches that evidence.
- [ ] llama.cpp and vLLM URL-image values match current official evidence.
- [ ] No local template reintroduces speculative request-size limits.

## Responses routing/protocol

- [ ] `/v1/responses` remains `protocol="openai"`, `request_surface="responses"`.
- [ ] `ProtocolName` remains exactly OpenAI/Anthropic.
- [ ] Responses skips `_prepare_transcode_preflight()`.
- [ ] Responses never constructs `PreparedTranscode`.
- [ ] Responses never sets `transcode_required=True`.
- [ ] Responses never selects BodyTranscoder/StreamingTranscoder.
- [ ] Responses with Anthropic-only model support is rejected locally.
- [ ] `get_upstream_url(... responses ...)` cannot produce `/messages`.
- [ ] Providers without `responses_path` remain ineligible.
- [ ] No Chat Completions fallback is introduced.

## Passthrough

- [ ] Accepted Responses JSON is unchanged except upstream base-model normalization.
- [ ] Thinking-control normalization is skipped for Responses.
- [ ] Chat stream-options injection remains skipped.
- [ ] No unsupported field is silently deleted.

## Stateless contract

- [ ] String conversation reference is rejected.
- [ ] Object conversation reference is rejected.
- [ ] Previous-response reference is rejected.
- [ ] `store=true` is rejected.
- [ ] Omitted `store` is rejected if the preferred explicit-false policy is implemented.
- [ ] `store=false` is accepted.
- [ ] `background=true` is rejected.
- [ ] Stateful rejection occurs before durable selection/upstream I/O.

## Streaming

- [ ] `response.completed` is canonical success.
- [ ] `response.failed` is terminal non-success.
- [ ] `response.incomplete` is terminal non-success.
- [ ] Neither failed nor incomplete records canonical success.
- [ ] Neither failed nor incomplete triggers provider/account failover after response handoff.
- [ ] No new blanket health/backoff penalty is attached solely to those terminal event types.
- [ ] Chat `[DONE]` behavior is unchanged.
- [ ] Anthropic `message_stop` behavior is unchanged.
- [ ] Missing terminal evidence still follows the existing strict/compatible completion policy.

## Plan 142 regression safety

- [ ] selected-provider CapabilityError/TranscodeLossError remain typed 4xx outcomes;
- [ ] durable finalization failure remains fail-closed;
- [ ] provider-bound 413 behavior is unchanged;
- [ ] local representability failure still does not penalize provider health.

## Scope/verification

- [ ] No new dependency/provider SDK.
- [ ] No response/conversation state store.
- [ ] No provider affinity/sticky Responses routing.
- [ ] No Responses WebSocket/background/retrieval endpoints.
- [ ] No new protocol family or content IR.
- [ ] No CI job/matrix expansion.
- [ ] Existing single-job CI workflow remains unchanged.
- [ ] Focused tests exercise real API/coordinator/stream seams rather than helper functions only.
- [ ] Ruff format/lint, Pyright, focused Plan 144 tests, and smoke tests pass locally.

---

# Completion rule for the 131–144 line of work

When all Plan 144 acceptance criteria are met, treat Plans 131–144 as **closed**.

Do not create another automatic closure plan merely because implementation touched nearby code.

A future plan is warranted only for a new concrete requirement or reproduced defect, such as:

- a current Codex request that still cannot traverse the stateless subset;
- a provider's documented Responses contract changing incompatibly;
- a reproduced durable/routing failure in the corrected stream lifecycle;
- an explicit product decision to support stateful Responses, WebSockets, or cross-protocol translation.

Those would be new work, not unfinished Plan 144 scope.
