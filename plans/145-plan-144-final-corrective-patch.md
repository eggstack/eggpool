# Plan 145 — Plan 144 Final Corrective Patch

Status: ready for implementation

Baseline reviewed: `c1e7d02fab7498f51eb330dd606c7f07a85d9427`

Related plans: 131–144, especially Plan 144

## Purpose

Plan 144 substantially landed the intended stateless OpenAI Responses surface, but the post-implementation review found two concrete correctness defects and one verification gap that prevent the 131–144 line from being closed:

1. `response.failed` / `response.incomplete` are correctly classified and initially finalized as non-success, but the streaming generator then falls through into the ordinary success path, records a completed-stream diagnostic, and attempts a second `COMPLETED` terminal finalization;
2. the bundled Ollama OpenAI-compatible multimodal metadata still advertises `image_input.url = false` and retains the stale comment that URL images are unsupported, despite current upstream OpenAI-compatibility documentation requiring re-verification and correction;
3. Plan 144 added good observer/classifier unit coverage, but did not add the requested coordinator streaming seam test or real ASGI stateless-admission test, allowing the stream fallthrough defect to survive.

This plan is a **surgical patch**, not another architecture milestone. It should require only a small production diff and a few focused tests.

When this plan's acceptance criteria pass, treat Plans 131–145 as closed. Do not create another automatic closure plan unless a new reproduced defect or requirement appears.

---

# Required end state

After Plan 145 lands:

- `response.completed` remains the only successful canonical Responses stream terminal event;
- `response.failed` and `response.incomplete` each produce exactly one non-success terminal finalization;
- neither failed nor incomplete can fall through to `STREAM_OUTCOME_COMPLETED_*` diagnostics or `FinalizationOutcome.COMPLETED`;
- failed/incomplete terminal events remain forwarded unchanged to the client;
- no provider/account retry occurs after the Responses terminal event has been handed downstream;
- the existing finalization supervisor sees one compatible terminal submission, not a conflicting second terminal outcome;
- Ollama image URL source-form metadata matches current official Ollama OpenAI-compatibility documentation immediately before implementation;
- the fix is proven at the real coordinator streaming seam, not only at `IncrementalSSEObserver` / `classify_stream_eof()` helper level;
- stateless Responses rejection is proven at the real ASGI endpoint boundary before coordinator execution;
- no new dependency, provider SDK, protocol family, stream state machine, retry subsystem, test harness, CI job, or matrix is added.

---

# Scope

## Expected production files

The production diff should normally be limited to:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/providers/_templates.toml`

A production change outside those files requires a concrete reason tied directly to an acceptance criterion below.

## Expected test files

Prefer existing focused tests:

- `tests/unit/test_responses_passthrough.py`
- the existing coordinator/stream lifecycle test file that already has the best fixture for consuming a real `PreparedProxyResponse.stream_iterator`
- the existing provider-template/capability metadata test file, if one already asserts Ollama/llama.cpp/vLLM source forms

If no existing coordinator stream test file can express the regression cleanly, adding **one small focused test file** is acceptable. Do not create a reusable fake-provider framework or new test tier.

## Documentation

No broad documentation rewrite is expected.

Only update documentation if necessary to remove the stale Ollama URL-image claim. The Plan 144 Responses documentation already describes failed/incomplete as non-success; the implementation must be brought into conformance with that documentation rather than rewriting the contract.

---

# Explicitly out of scope

Do **not** use this patch to:

- redesign `RequestCoordinator`;
- redesign `FinalizationSupervisor` or `RequestFinalizer`;
- create a new stream state machine;
- add a new `FinalizationOutcome` unless the existing `MIDSTREAM_ERROR` representation is proven insufficient;
- add Responses-to-Chat or Responses-to-Anthropic translation;
- add stateful Responses support;
- add `previous_response_id` persistence or conversation affinity;
- add Responses retrieval/cancel/delete/background/WebSocket APIs;
- change the `ProtocolName = Literal["openai", "anthropic"]` model;
- alter the `responses_path` provider eligibility design;
- change the explicit `store: false` requirement;
- change the Plan 142 typed capability/transcode-loss behavior;
- add provider SDKs;
- add dependencies;
- add CI jobs or OS/Python matrices;
- add live external-provider tests;
- broaden multimodal capability detection beyond correcting the proven Ollama source-form fact;
- perform unrelated cleanup/refactoring in the 260k-line coordinator module.

If the implementation starts expanding beyond the narrow control-flow and metadata correction described below, stop and simplify.

---

# Invariants to preserve

## I1 — Responses remains same-protocol OpenAI passthrough

Keep all Plan 144 routing behavior intact:

```text
request_surface      = responses
client protocol      = openai
upstream protocol    = openai
transcode_required   = false
BodyTranscoder       = none
StreamingTranscoder  = none
route                = provider.responses_path
```

No `/messages` fallback and no Chat fallback.

## I2 — Stateless admission remains strict

Preserve:

- any non-`None` `previous_response_id` -> local 400;
- any non-`None` `conversation` -> local 400;
- omitted `store` -> local 400;
- `store: true` -> local 400;
- `store: false` -> allowed;
- `background: true` -> local 400.

These rejections occur before durable provider/account selection and before upstream I/O.

## I3 — Responses body remains passthrough

Preserve the Plan 144 transform gates:

- thinking-control normalization skipped;
- Chat `stream_options.include_usage` injection skipped;
- no transcoder mutation;
- only canonical provider suffix / base model normalization may alter the JSON body.

## I4 — One terminal owner per selected attempt

For a selected Responses streaming attempt, one upstream terminal outcome must produce one terminal submission.

Valid examples:

```text
response.completed
    -> FinalizationOutcome.COMPLETED

response.failed
    -> FinalizationOutcome.MIDSTREAM_ERROR

response.incomplete
    -> FinalizationOutcome.MIDSTREAM_ERROR
```

A single attempt must never submit both `MIDSTREAM_ERROR` and `COMPLETED`.

## I5 — Terminal non-success is not transport EOF failure

`response.failed` and `response.incomplete` are valid provider-level terminal events, not missing-terminal transport failures.

Therefore preserve the Plan 144 distinction:

- no `PrematureStreamEOFError` for these two terminal events;
- no failover/retry after the event is downstream-visible;
- no false canonical success;
- no second terminal submission.

## I6 — No blanket health penalty for terminal Responses failure/incomplete

Keep the existing behavior whereby the non-success terminal event is durably represented without automatically treating it like a transport outage, authentication failure, rate limit, or provider circuit-breaker event.

Do not introduce provider health/backoff/quarantine penalties solely because the provider emitted `response.failed` or `response.incomplete`.

## I7 — Existing Chat and Anthropic stream completion is untouched

Preserve:

- OpenAI Chat `[DONE]` success behavior;
- Anthropic `message_stop` success behavior;
- strict/compatible markerless EOF behavior;
- existing timeout and malformed/premature EOF behavior.

---

# Workstream A — Stop Responses non-success stream fallthrough

## A1 — Root cause

At the reviewed baseline, the streaming generator performs the following sequence after upstream EOF:

1. `classify_stream_eof()` returns `terminal_failure` or `terminal_incomplete`;
2. the coordinator records the terminal non-success diagnostic;
3. the coordinator calls `_finalize_terminal(... FinalizationOutcome.MIDSTREAM_ERROR ...)`;
4. the coordinator intentionally does **not** raise `PrematureStreamEOFError` for these provider-level terminal events;
5. execution then continues past the non-success branch;
6. the ordinary completion block records `STREAM_OUTCOME_COMPLETED_CANONICAL`;
7. the ordinary completion block calls `_finalize_terminal(... FinalizationOutcome.COMPLETED ...)`.

That fallthrough violates the Plan 144 contract and conflicts with the finalization supervisor's one-terminal-semantic invariant.

## A2 — Preferred minimal correction

After the non-success terminal event has been:

- observed/classified;
- forwarded downstream by the existing raw SSE streaming path;
- recorded in stream diagnostics;
- durably finalized as `MIDSTREAM_ERROR`;
- recorded as the upstream midstream diagnostic if that diagnostic remains desired;

**terminate the async stream generator immediately**.

Preferred code shape:

```python
if eof_decision.classification in {
    "terminal_failure",
    "terminal_incomplete",
}:
    ...
    await self._finalize_terminal(
        ...,
        FinalizationData(
            outcome=FinalizationOutcome.MIDSTREAM_ERROR,
            ...,
        ),
    )
    ...diagnostics...
    return
```

The exact placement may differ to preserve existing diagnostics, but the invariant is simple:

```text
terminal_failure / terminal_incomplete
    MUST NOT reach streaming_transcoder.finish()
    MUST NOT reach completed-stream diagnostics
    MUST NOT reach FinalizationOutcome.COMPLETED
```

Since Responses never has a `StreamingTranscoder`, skipping `streaming_transcoder.finish()` for these terminal events is consistent with the same-protocol passthrough contract.

## A3 — Do not solve this by swallowing terminal conflicts

Do **not** change `FinalizationSupervisor` to tolerate incompatible duplicate terminal submissions.

The supervisor is correctly detecting an invalid lifecycle. The defect is the coordinator submitting two different terminal outcomes.

Do **not** weaken `TerminalConflictError` or durable terminal identity checks.

## A4 — Do not turn terminal events into retry exceptions

Do **not** fix fallthrough by raising `PrematureStreamEOFError` after `response.failed` / `response.incomplete`.

That would misclassify a valid provider-level terminal event as transport truncation and could reopen account failover/retry behavior after downstream handoff.

The terminal event is the client's outcome; finalize once and end the iterator normally.

## A5 — Preserve the client-visible SSE event

The existing loop yields upstream chunks before EOF classification. Keep that behavior.

For a stream containing:

```text
event: response.created
...
event: response.failed
...
```

or:

```text
event: response.incomplete
...
```

all upstream bytes through the terminal event must still reach the client unchanged.

The patch controls only what EggPool does **after** observing transport EOF.

## Acceptance criteria — Workstream A

- `response.completed` reaches exactly one `COMPLETED` finalization;
- `response.failed` reaches exactly one `MIDSTREAM_ERROR` finalization;
- `response.incomplete` reaches exactly one `MIDSTREAM_ERROR` finalization;
- failed/incomplete never reach the ordinary completed-stream block;
- failed/incomplete never emit `STREAM_OUTCOME_COMPLETED_CANONICAL` or `STREAM_OUTCOME_COMPLETED_COMPATIBILITY`;
- failed/incomplete do not raise `PrematureStreamEOFError`;
- failed/incomplete do not cause provider/account reselection after downstream handoff;
- the raw terminal SSE event remains client-visible;
- no finalization conflict is generated by the normal failed/incomplete path;
- Chat `[DONE]`, Anthropic `message_stop`, premature EOF, malformed EOF, and timeout paths remain unchanged.

---

# Workstream B — Correct Ollama image URL source-form metadata

## B1 — Re-verify official Ollama documentation immediately before editing

Use the current official Ollama OpenAI-compatibility documentation as the source of truth.

Verify specifically whether the OpenAI-compatible Chat Completions image-content shape accepts:

- base64/data image content;
- remote Image URL content.

Do not use secondary blogs, issue comments, or cached assumptions when official documentation is available.

## B2 — Correct only the source-form fact

If current official Ollama documentation still advertises Image URL support, update:

`src/eggpool/providers/_templates.toml`

from:

```toml
image_input = { base64 = true, url = false }
```

to:

```toml
image_input = { base64 = true, url = true }
```

and remove/correct the stale adjacent comment claiming URL images are unsupported.

## B3 — Keep model capability semantics conservative

This metadata describes whether the **endpoint/source form** can carry an image URL.

It must not imply that every model served by Ollama is multimodal.

Preserve the existing distinction between:

- endpoint/source-form representability; and
- actual loaded-model multimodal capability.

Do not add speculative PDF/audio/tool-result capability claims.

## B4 — Do not invent size ceilings

Preserve the current no-speculative-provider-request-limit policy.

Do not add an Ollama serialized-size ceiling unless the provider publishes an authoritative fixed limit that EggPool can represent correctly.

## Acceptance criteria — Workstream B

- Ollama `image_input.url` matches current official Ollama OpenAI-compatible endpoint documentation;
- adjacent comments match the configured value;
- base64 support remains correct;
- no claim is made that every Ollama model supports images;
- llama.cpp/vLLM metadata is untouched unless current official documentation independently proves it wrong;
- no request-size limit is invented.

---

# Workstream C — Add the missing coordinator streaming regression proof

## Goal

Test the lifecycle defect at the seam where it occurred.

Observer/classifier unit tests are still useful, but they cannot prove that the coordinator stops after non-success finalization.

## C1 — Drive the real coordinator stream iterator

Use the smallest existing coordinator streaming fixture capable of producing a `PreparedProxyResponse` with a real `stream_iterator`.

Consume the iterator to EOF.

Feed a Responses SSE stream containing at minimum:

```text
event: response.created
data: {...}

event: response.failed
data: {...}

```

Prefer parameterizing the same seam test over:

- `response.failed` -> `terminal_failure`;
- `response.incomplete` -> `terminal_incomplete`.

Do not build a live HTTP provider or Docker fixture; use the existing mocked upstream response/iterator seam.

## C2 — Capture terminal finalization calls

Spy/mock the coordinator's existing `_finalize_terminal()` seam or the retained finalization owner immediately below it.

For each failed/incomplete case assert:

```text
number of terminal finalization submissions == 1
outcome == MIDSTREAM_ERROR
COMPLETED submissions == 0
```

Also assert the iterator ends normally after that terminal finalization.

If the finalization spy can capture `error_detail`, assert it matches:

- `terminal_failure`; or
- `terminal_incomplete`.

Do not overfit to incidental logging strings.

## C3 — Assert no completed diagnostic

Where the existing `StreamDiagnostics` test double supports it, assert:

- the specific terminal failure/incomplete outcome is recorded;
- `STREAM_OUTCOME_COMPLETED_CANONICAL` is not recorded;
- `STREAM_OUTCOME_COMPLETED_COMPATIBILITY` is not recorded.

This assertion directly protects the fallthrough location.

## C4 — Assert no retry/reselection after handoff

Use the existing selection/upstream doubles to prove that a failed/incomplete terminal event does not trigger a second provider/account attempt once the downstream response has started.

At minimum assert one of:

- account-selection call count remains `1`;
- upstream dispatch call count remains `1`;
- no `_RetryableUpstreamError` path is entered;
- no second selected attempt exists.

Prefer whichever assertion is easiest with the existing fixture.

Do not add a new retry harness solely for this test.

## C5 — Assert terminal bytes are forwarded

Consume the stream and verify the raw output still contains the terminal event bytes.

The test should establish both sides of the contract:

```text
client sees provider terminal event
AND
EggPool does not mark the request successful afterward
```

## C6 — Keep helper tests

Keep the existing observer/classifier tests for:

- `response.completed -> complete`;
- `response.failed -> terminal_failure`;
- `response.incomplete -> terminal_incomplete`;
- missing terminal -> premature EOF.

They are useful unit coverage; the new coordinator test supplements rather than replaces them.

## Acceptance criteria — Workstream C

- at least one real coordinator stream test consumes the stream through the failed/incomplete terminal path;
- preferably one parameterized test covers both failed and incomplete;
- exactly one non-success finalization is observed;
- zero success finalizations are observed;
- zero completed-stream diagnostics are observed;
- no retry/reselection occurs after downstream handoff;
- terminal bytes are still yielded to the client;
- the test fails against baseline `c1e7d02...` because of the current fallthrough and passes after the patch.

---

# Workstream D — Add the missing real ASGI stateless-admission proof

## Goal

Plan 144 tightened the stateless helper correctly, but the existing focused tests primarily call `_validate_responses_stateless()` directly.

Add one small real API-boundary test so future handler reordering cannot accidentally perform coordinator/provider work before rejection.

## D1 — Use the existing ASGI application/test construction

Use the project's existing FastAPI/ASGI test fixture and `httpx.ASGITransport` or equivalent existing test helper.

Do not create a new server process.

## D2 — Rejection cases

Drive `POST /v1/responses` with at least:

```json
{
  "model": "test-model",
  "input": "hello",
  "conversation": "conv_123",
  "store": false
}
```

and:

```json
{
  "model": "test-model",
  "input": "hello"
}
```

The second case proves omitted `store` is rejected.

Assert:

- HTTP 400;
- coordinator `execute()` was not called;
- upstream dispatch was not called;
- no provider selection/reservation side effect occurred if the fixture exposes that counter.

## D3 — Accepted control case

Include one control request with:

```json
{
  "model": "test-model",
  "input": "hello",
  "store": false
}
```

The test only needs to prove the stateless admission gate permits it to proceed to the mocked coordinator seam.

It does not need a live provider response.

## D4 — Do not duplicate every helper permutation at ASGI level

The helper unit tests already cover string/object/empty conversation, previous response ID, background, and store variants.

At ASGI level, keep only the representative cases needed to prove ordering and integration.

## Acceptance criteria — Workstream D

- a real `/v1/responses` request with a string conversation ID returns local 400 before coordinator execution;
- a real request with omitted `store` returns local 400 before coordinator execution;
- a real `store: false` control reaches the mocked coordinator seam;
- no external network call is made;
- no new test server framework is introduced.

---

# Workstream E — Compact metadata regression test

## Goal

Ensure the corrected Ollama metadata does not silently regress again.

## E1 — Prefer an existing provider-template test

If an existing test already parses `_templates.toml` or asserts local provider multimodal defaults, extend it.

Otherwise add one compact test in the most relevant existing provider/config test file.

## E2 — Assert the supported source forms only

After current documentation re-verification, assert the known local runtime values that Plan 144/145 intentionally maintain.

At minimum cover Ollama:

```text
image_input.base64 == true
image_input.url    == true   # only if current official docs still confirm it
```

If the same test already covers llama.cpp/vLLM, retain their verified `url = true` assertions.

Do not duplicate the full provider template in test literals.

## Acceptance criteria — Workstream E

- the test reads/parses the real bundled template configuration;
- the test fails when the corrected Ollama URL source-form flag is reverted;
- the test does not imply model-level multimodal support;
- no snapshot framework is added.

---

# Workstream F — Regression safety

## F1 — Preserve Plan 142 selected-provider error semantics

No code in this patch should alter the selected-provider transcode/capability path.

Verify that existing tests continue to prove:

- `CapabilityError` remains a typed client 4xx;
- `TranscodeLossError` remains a typed client 4xx;
- selected rejection converges through canonical finalization;
- finalization database failure remains fail-closed;
- provider health is not penalized for local representability rejection;
- provider-bound serialized oversize remains HTTP 413.

Do not rewrite that machinery while fixing the Responses stream fallthrough.

## F2 — Preserve Responses routing/stateless behavior

Existing Plan 144 tests must remain green for:

- skipped transcode preflight;
- Anthropic-only model local rejection;
- no `/messages` URL construction;
- missing `responses_path` ineligibility;
- thinking-control skip;
- stream-options skip;
- explicit `store:false` requirement;
- conversation/previous-response rejection;
- output token limit parsing.

## F3 — Preserve Chat/Anthropic stream behavior

Run the existing stream completion tests proving:

- OpenAI `[DONE]`;
- Anthropic `message_stop`;
- strict premature EOF;
- compatible markerless completion;
- malformed/incomplete frame handling.

No expected result in those tests should need to change.

---

# Ordered implementation sequence

## Step 1 — Reproduce and pin the stream fallthrough

Before editing production code, add or sketch the coordinator-level failed/incomplete test and confirm baseline behavior reaches an unwanted completed finalization/diagnostic or terminal conflict.

Do not spend time building a larger fixture than needed.

## Step 2 — Apply the one-control-flow stream fix

Modify the Responses non-success branch so it terminates the stream iterator after successful non-success finalization/diagnostics.

Prefer a clear `return` over new flags or state variables.

Do not touch finalization supervisor semantics.

## Step 3 — Re-verify and correct Ollama metadata

Check current official Ollama documentation, correct `image_input.url` and its comment if still supported, and update/add the compact template test.

## Step 4 — Add real ASGI admission coverage

Add the string-conversation, omitted-store, and `store:false` control cases at the real endpoint boundary with coordinator call assertions.

## Step 5 — Run focused regression checks

Run the Responses, coordinator stream, provider metadata, stream completion, and Plan 142 corrective tests.

## Step 6 — Run the existing repository verification only

Run the current lint/typecheck/smoke commands. Do not add CI infrastructure.

## Step 7 — Inspect final diff for scope creep

Expected conceptual production diff:

```text
coordinator.py
    one narrow control-flow termination

_templates.toml
    one Ollama source-form flag/comment correction
```

If the production diff becomes materially broader, reassess before committing.

---

# Verification commands

Use the repository's current environment and existing commands.

Minimum:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/unit/test_responses_passthrough.py -q
uv run pytest tests/unit/test_stream_completion.py -q
uv run pytest tests/unit/test_plan_141_corrective_closure.py -q
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Also run the exact coordinator streaming test file and provider-template metadata test file modified by this patch.

If inexpensive in the implementation environment, running the broader unit suite is useful but is **not** a reason to add another CI gate:

```bash
uv run pytest tests/unit/ -q
```

No live Ollama instance or external provider credential is required.

---

# Explicit acceptance checklist

Plan 145 is complete only when every applicable item below is true.

## Stream lifecycle

- [ ] `response.completed` produces exactly one `FinalizationOutcome.COMPLETED` terminal submission.
- [ ] `response.failed` produces exactly one non-success terminal submission.
- [ ] `response.incomplete` produces exactly one non-success terminal submission.
- [ ] Failed/incomplete use the existing `MIDSTREAM_ERROR` representation unless a concrete incompatibility proves otherwise.
- [ ] Failed/incomplete never reach the completed-stream diagnostic block.
- [ ] Failed/incomplete never submit `FinalizationOutcome.COMPLETED`.
- [ ] Failed/incomplete do not produce `TerminalConflictError` during the normal path.
- [ ] Failed/incomplete do not raise `PrematureStreamEOFError`.
- [ ] Failed/incomplete do not retry/reselect after downstream handoff.
- [ ] Failed/incomplete terminal SSE bytes remain forwarded unchanged.
- [ ] No new provider health/backoff/quarantine penalty is introduced solely for these terminal event kinds.
- [ ] Chat `[DONE]` behavior remains unchanged.
- [ ] Anthropic `message_stop` behavior remains unchanged.
- [ ] Existing premature/malformed/compatible EOF behavior remains unchanged.

## Ollama metadata

- [ ] Current official Ollama OpenAI-compatible image-source documentation was re-verified immediately before implementation.
- [ ] Ollama `image_input.base64` matches that evidence.
- [ ] Ollama `image_input.url` matches that evidence.
- [ ] The adjacent template comment matches the configured value.
- [ ] Endpoint source-form support is not confused with loaded-model multimodal capability.
- [ ] No speculative request-size limit or PDF/audio claim is added.

## Real seam tests

- [ ] At least one coordinator streaming regression test consumes the actual failed/incomplete stream path through finalization.
- [ ] The coordinator stream test proves exactly one non-success terminal finalization.
- [ ] The coordinator stream test proves zero successful terminal finalizations.
- [ ] The coordinator stream test proves zero completed-stream diagnostics for failed/incomplete.
- [ ] The coordinator stream test proves terminal bytes remain client-visible.
- [ ] The coordinator stream test proves no retry/reselection after handoff.
- [ ] A real ASGI `/v1/responses` test rejects `conversation: "conv_123"` before coordinator execution.
- [ ] A real ASGI `/v1/responses` test rejects omitted `store` before coordinator execution.
- [ ] A real ASGI control with `store:false` reaches the mocked coordinator seam.
- [ ] A compact real-template test protects corrected Ollama source-form metadata.

## Regression safety

- [ ] Responses still skips transcode preflight.
- [ ] Responses still cannot construct an Anthropic `/messages` route.
- [ ] Responses still cannot select BodyTranscoder/StreamingTranscoder.
- [ ] Thinking-control and Chat stream-options transforms remain skipped.
- [ ] `ProtocolName` remains `openai | anthropic` only.
- [ ] `responses_path` eligibility behavior is unchanged.
- [ ] Plan 142 typed capability/transcode-loss behavior remains green.
- [ ] Provider-bound 413 behavior remains green.

## Scope and project posture

- [ ] No new runtime dependency.
- [ ] No provider SDK.
- [ ] No new stream/finalization architecture.
- [ ] No persistent Responses state.
- [ ] No sticky provider affinity.
- [ ] No Responses WebSocket/background/retrieval API.
- [ ] No new CI job or matrix.
- [ ] No live-provider CI.
- [ ] No new broad test harness.
- [ ] Existing single-job CI configuration remains unchanged.

---

# Closure rule

When Plan 145 passes the acceptance checklist, the Plans 131–145 local-provider/multimodal/Responses correctness line is **closed**.

Do not create Plan 146 merely for another generic polish pass.

A future plan should require a new concrete input, such as:

- a reproduced request lifecycle defect after this patch;
- an upstream provider contract change;
- a current Codex request shape outside the intentionally supported stateless subset;
- an explicit decision to support stateful Responses or another wire transport;
- a separately identified performance/resource regression.

Absent one of those inputs, move on from this closure sequence.