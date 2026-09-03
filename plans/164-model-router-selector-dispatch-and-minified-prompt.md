# Plan 164 — Model-Router Selector Dispatch and Deterministic Minified Prompt

Date: 2026-09-03
Status: ready for implementation
Planning baseline: `525189763a3a6d506e9e8001e2426c9bd9a247fe`
Parent roadmap: `plans/162-optional-llm-model-router-selection-roadmap.md`
Depends on: `plans/163-model-router-config-registry-and-virtual-foundations.md`
Priority: P1 request correctness / local-selector latency
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Implement the semantic selector as a bounded internal concrete EggPool request, with a deterministic tiny prompt, strict output parsing, at most one repair attempt, and unconditional configured-default fallback when selector execution cannot produce an accepted choice.

This phase deliberately does **not** yet make client-facing virtual model IDs alter the shared proxy request path. The selector service should be independently callable/testable so Plan 166 can integrate a mature component rather than adding classification, internal dispatch, failure handling, and client routing in one patch.

---

## Core design constraint: reuse `RequestCoordinator.execute()`

Do not HTTP-call EggPool's own `/v1/chat/completions` endpoint and do not create a selector-specific provider client.

A selector inference is a real concrete-model inference and should use the same lifecycle as every other EggPool request:

```text
ModelRouterSelector
    |
    | concrete selector_model
    v
internal concrete request preparation
    |
    v
RequestCoordinator.execute()
    |
    +-- existing concrete model routing
    +-- account health/backoff
    +-- quota/fairness/reservation
    +-- provider-bound transcoding/wire selection
    +-- request/attempt persistence
    +-- cancellation/finalization
    v
bounded non-streaming selector response
```

This gives selector models ordinary provider/account aggregation automatically and prevents the feature from becoming a second proxy implementation.

Selector requests use their own child request UUID. Do not reuse the parent client request ID. They may be visible in normal usage/cost/request accounting because they consume real inference resources.

---

## 1. Internal concrete request preparation

The existing public proxy path currently performs model parsing, canonicalization, model-specific checks/transcoding, then constructs `ProxyRequestContext` for `RequestCoordinator.execute()`.

The selector needs a small reusable way to construct an equivalent concrete non-streaming request without routing through FastAPI/HTTP. Do not make `model_router.selector` reach deeply into private API-handler functions or duplicate provider-bound request semantics.

Preferred implementation direction:

- extract or add a narrow helper under `src/eggpool/request/` (for example `request/internal_dispatch.py` or `request/preparation.py`) that can prepare a known-concrete model request for the existing coordinator;
- use the existing canonical request/transcoder/provider protocol machinery;
- keep the public concrete request behavior characterized before extraction;
- avoid broad refactoring of `proxy_request.py` unless required to share one correctness-critical preparation function;
- if extraction changes existing public code, make it behavior-preserving and land characterization tests in the same phase.

The internal selector client surface should be OpenAI Chat Completions semantics with `stream = false`. The current transcoder/provider machinery can adapt that concrete request to an Anthropic-compatible or other supported upstream surface exactly as it does for ordinary requests.

Do not require the selector provider itself to be OpenAI-native.

---

## 2. Selector prompt compiler

Add a dedicated deterministic module, likely `src/eggpool/model_router/prompt.py`.

It has two conceptual inputs:

1. immutable compiled router policy from Plan 163;
2. a bounded semantic view of the current client request.

It returns a compact selector request payload; it must not perform network I/O, LLM summarization, tokenization, database lookup, or provider routing.

### Static prefix

Use the precompiled route policy from `CompiledModelRouter`, with compact route IDs and descriptions. Keep instructions terse and before variable user content for local prefix/KV-cache reuse.

Conceptual content:

```text
choose id;reply id only
0=Use for the most difficult queries.
1=Use for implementation/debugging/code.
2=Use for straightforward latency-sensitive queries.
```

The exact format is an internal versioned protocol. Prefer readability sufficient for tests/debugging over exotic compression. Minification should remove useless tokens, not make the prompt brittle.

The route's concrete upstream model name does **not** need to be shown to the selector unless implementation evidence demonstrates that names improve routing quality. The operator description is the semantic policy; hiding concrete names also avoids model-family priors overriding explicit descriptions.

### Variable semantic view

Build this only for a configured virtual request. Feature-off/concrete requests must not pay this cost.

Extract enough semantic information for specialization/difficulty routing while excluding high-volume context. Initial policy:

- include a bounded portion of system/developer instruction when available because it can establish task domain;
- include the latest user-visible text as the primary classification input;
- exclude assistant history except where no user text exists and a safe minimal fallback is needed;
- exclude tool definitions/schemas;
- exclude tool outputs/results;
- exclude binary/image/PDF data and base64;
- convert presence of tools/images/PDF/reasoning controls into tiny feature flags only when useful;
- never include request authentication, provider metadata, EggPool headers, or account state.

Use the decoded request and existing canonical semantic representation where practical. It is acceptable for the **virtual-only** branch to build an early canonical semantic view and then later let the existing concrete path rebuild/normalize its own authoritative request. Do not reorder the normal concrete path to save this optional duplicate work.

### Byte bounding

`max_input_bytes` bounds the variable semantic portion, not the static route policy. Enforce the aggregate static-policy ceiling from Plan 163 separately.

Truncate deterministically on UTF-8 byte boundaries. Preserve both beginning and end of oversized user text because problem statements often introduce context early and place the actual requested change/error late. A fixed head/tail allocation such as ~75/25 is acceptable if documented and golden-tested.

Never import a selector tokenizer solely for truncation.

### Whitespace normalization

Specify exactly what is normalized. Recommended: normalize CRLF/CR to LF, collapse repeated horizontal ASCII whitespace outside unavoidable content structure, bound repeated blank lines, trim edges. Do not Unicode-normalize user content in a way that changes identifiers/code. The classifier sees user semantics; correctness is more important than shaving a handful of bytes.

---

## 3. Conservative generation controls

The selector output language is intentionally tiny. Build a non-streaming request with a small output-token budget (for example 8–16 tokens, chosen based on existing provider abstractions).

Do not blindly send optional controls that some models reject. In particular:

- `stream = false` is required;
- small max-output control should use the normal canonical/provider adaptation;
- use `temperature = 0` only when the provider/model capability contract says the control is accepted; otherwise omit it;
- do not expose arbitrary selector generation options in TOML in v1;
- do not request reasoning/thinking from the selector unless a future explicit feature requires it.

The model should be guided to deterministic output primarily by the compact prompt and exact parser, not a fragile stack of optional sampling controls.

---

## 4. Exact output parser

The selector must never be able to return an arbitrary model name into EggPool's routing path.

Parsing algorithm:

1. Bound the response body/content before semantic parsing.
2. Decode the normal OpenAI-chat-shaped non-streaming response produced by the internal client surface.
3. Extract the text content only.
4. `strip()` outer whitespace.
5. Accept only exact IDs present in `CompiledModelRouter.route_by_id`.
6. Reject JSON wrappers, Markdown fences, sentences, labels, model names, multiple IDs, punctuation-suffixed IDs, or substring matches unless the exact internal protocol deliberately includes them.

Do not implement fuzzy matching or "helpful" recovery such as scanning a sentence for a known route. The fallback contract is safer and simpler.

Return a typed result such as:

```python
@dataclass(frozen=True, slots=True)
class ModelSelection:
    virtual_model: str
    route_id: str
    route_label: str
    concrete_model: str
    source: Literal["selector", "default"]
    selector_attempts: int
    selector_latency_ms: float | None
```

Plan 165 may extend the source enum with `affinity` without changing selector parsing.

---

## 5. Repair attempt

If the first inference completes successfully but its semantic text is invalid and `repair_attempts = 1`, perform exactly one repair inference.

Keep repair content tiny and deterministic, for example:

```text
invalid;reply only:0|1|2
```

Do not copy an unbounded raw first response into the repair prompt. The first response is already output-bounded, but there is no semantic need to repeat it.

The repair request uses the same concrete selector model and ordinary coordinator path. It consumes normal quota/cost/account resources and therefore may route to another eligible **account/provider serving the same selector concrete model**, as existing failover allows. It may not become a different semantic selector model.

If repair output remains invalid, use `default_model`.

`repair_attempts = 0` skips this second inference.

---

## 6. Selector failure taxonomy and default fallback

Default fallback is required for any failure that prevents a valid semantic decision, including:

- selector model not currently available/eligible;
- catalog/model absence at request time;
- selector provider/account exhaustion;
- rate-limit/quota/server/transport failure after the existing coordinator exhausts its allowed concrete-model failover;
- selector-specific timeout;
- malformed/non-JSON response after normal response handling;
- accepted response shape with invalid route text;
- repair failure/invalid output.

Do not expose the selector's failure to the client if dispatch to `default_model` later succeeds. Record bounded diagnostics/counters only.

Important distinctions:

- `default_model` is chosen **before** the parent concrete target request begins;
- if `default_model` itself is unavailable, allow the existing concrete request path to produce its ordinary model/account error;
- once a selector successfully chooses target A, do not switch to B merely because A later returns 429/5xx/transport failure after submission. Existing provider/account failover for A remains authoritative;
- no parallel selector/default execution and no speculative target dispatch.

---

## 7. Timeout and cancellation correctness

Wrap the internal selector execution in the configured `selector_timeout_s` using modern asyncio timeout semantics, but preserve coordinator cleanup.

Required behavior:

- an **internal timeout** cancels the child selector request; wait for/allow normal coordinator cancellation/finalization cleanup; then return configured default to the parent semantic resolver;
- a **parent/client cancellation** propagates out immediately. Do not catch `asyncio.CancelledError` and reinterpret it as selector failure/default fallback;
- no request/reservation/attempt row may remain pending solely because the selector timed out;
- do not use detached tasks that continue inference after the parent request has gone away;
- do not start a background retry loop.

Add explicit tests around timeout during selection, timeout while an upstream response is pending, and external cancellation.

---

## 8. Isolation from account health and semantic target health

A malformed selector answer is an application-level semantic classification failure. It must not mark the selector's account unhealthy unless the existing concrete request lifecycle already observed an upstream transport/protocol failure warranting that health action.

Likewise, the new layer must not write model-selection outcomes into provider/account scoring.

The selector service should return either a valid configured route or the configured default. It should not mutate `routing.Router` state directly.

---

## 9. Selector usage accounting

Because the selector call uses `RequestCoordinator.execute()`:

- its request should receive a normal request record;
- its provider/account attempt should receive normal attempt/accounting treatment;
- its token/cost usage should contribute to the selector model/account's real usage;
- its failure cleanup should use existing durable state semantics;
- semantic routing diagnostics should not double-count it as parent target usage.

Document this later: a local selector may be practically free, but a remotely billed selector consumes billable tokens and EggPool must not hide them.

No new `model_router_requests` table is needed.

---

## 10. Avoid selector recursion by construction

Plan 163 rejects virtual selector models. Additionally, the selector service should operate below the future public virtual-resolution hook: it receives a `CompiledModelRouter.selector_model` already known to be structurally concrete and constructs `ProxyRequestContext` directly for the coordinator.

Do not call the public `handle_proxy_request()` function from selector code. This makes recursive model-router invocation impossible even if future API wiring changes.

---

## Tests

Add deterministic unit tests for:

### Prompt compiler

- route order independent of TOML insertion order;
- exact golden bytes/text for representative routers;
- system/developer + latest user extraction across Chat Completions, Messages, and Responses canonical shapes;
- tool schemas/results excluded;
- image/PDF/base64 content excluded and replaced only by tiny feature indication;
- reasoning/tool/modality feature flags deterministic;
- UTF-8 byte ceiling respected for ASCII and multi-byte Unicode;
- head/tail truncation stable;
- no tokenizer dependency;
- unchanged router config produces identical static prefix.

### Output parser

- every exact configured ID accepted;
- surrounding whitespace accepted;
- model names/route labels rejected;
- prose/Markdown/JSON rejected;
- unknown ID rejected;
- multiple IDs rejected;
- oversized content rejected/bounded.

### Selector service

- first-attempt valid decision;
- invalid -> one repair -> valid;
- invalid -> repair invalid -> default;
- `repair_attempts = 0` -> immediate default;
- upstream/model unavailable -> default;
- transport/server/rate-limit exhaustion -> default after existing failover;
- internal timeout -> default with no leaked coordinator state;
- external cancellation propagates and does not dispatch default;
- selector accounting is normal concrete usage;
- selector call can transcode to a non-OpenAI-native provider through existing machinery;
- no semantic/account-router state mutation beyond the normal concrete selector request;
- no HTTP loopback.

Use fake/mock provider transports already present in the repository. Do not require a live local model in mandatory CI.

---

## Performance acceptance

For a selector call, the dominant cost should be the model itself. Locally added work must be bounded and simple:

- one precompiled static policy lookup;
- one bounded semantic extraction/truncation pass;
- one ordinary coordinator invocation;
- one exact tiny output parse;
- at most one repair invocation.

No whole-transcript reserialization solely for routing, no embedding call, no extra model summarizer, no database query to choose the semantic route, and no background classifier.

Feature-off performance remains Plan 166's responsibility, but this module must not create process-global tasks merely by being imported.

---

## Acceptance criteria

Plan 164 is complete when:

1. A compiled router can be invoked directly and resolves to a configured concrete target or configured default.
2. Selector inference runs through the existing `RequestCoordinator` concrete lifecycle without HTTP loopback or duplicated provider routing.
3. Prompt input is deterministic, bounded, small-model-oriented, and golden-tested.
4. Only exact compact route IDs are accepted.
5. One bounded repair attempt works as specified.
6. Selector failures/timeouts fall back cleanly while parent cancellation propagates.
7. Selector reservations/request attempts cannot leak under error/timeout/cancellation tests.
8. Existing concrete provider/account routing and scorer contracts are unchanged.
9. No new dependency, DB migration, permanent background task, or live-test requirement has been introduced.
