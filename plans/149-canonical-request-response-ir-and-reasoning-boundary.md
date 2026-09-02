# Plan 149 — Canonical Request/Response IR and Reasoning Boundary

Date: 2026-09-02
Status: ready after Plan 148
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Depends on: `plans/148-wire-profile-registry-and-provider-contracts.md`
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f`
Priority: P0 API correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Introduce a deliberately small canonical semantic boundary so EggPool can translate among multiple upstream wire surfaces without growing an N² matrix of pairwise transcoders.

The boundary must preserve current supported Chat Completions ↔ Anthropic Messages behavior, add a truthful path for Responses and Gemini codecs, and separate reasoning/thinking **intent** from the concrete wire encoding used by a selected provider/model.

This is not a plan to emulate every OpenAI, Anthropic or Gemini feature. The canonical representation should contain only the semantic subset EggPool already proxies or explicitly chooses to support for cross-surface failover.

---

# Problem

Current translation is primarily organized as pairwise protocol conversions:

```text
OpenAI Chat -> Anthropic Messages
Anthropic Messages -> OpenAI Chat
```

The streaming layer in `src/eggpool/transcoder/streaming.py` is also explicitly Chat/Messages-shaped.

Adding Responses produces at least six directional pairs. Adding Gemini Interactions and generateContent would grow the matrix further and make correctness dependent on which pair happens to own a field.

That design also encourages a second defect: transport facts and semantic capability facts become mixed. Current reasoning controls can be converted to an Anthropic token budget before the final provider/surface is known, and the wrong protocol classification can therefore change the route itself.

The desired pipeline is:

```text
client wire request
      |
      v
canonical request intent
      |
      v
provider/account/model + wire-profile selection
      |
      v
selected-surface encoder
      |
      v
upstream

upstream response/events
      |
      v
canonical response/events
      |
      v
client-surface encoder
```

Same-surface traffic should retain a passthrough/low-copy fast path where no semantic adaptation is needed.

---

# Primary decision — minimal IR, not a universal message framework

Create a compact package under the wire/transcoder boundary, for example:

```text
src/eggpool/wire/ir.py
src/eggpool/wire/codecs/base.py
```

The exact file layout may follow current project conventions. Avoid a deep class hierarchy.

Recommended immutable/slotted types:

```text
CanonicalRequest
CanonicalMessage
CanonicalContentBlock
CanonicalTool
CanonicalToolChoice
ReasoningIntent
CanonicalResponse
CanonicalOutputBlock
CanonicalUsage
CanonicalEvent
```

Use existing project dataclasses/types where they already express the same concept correctly. Do not duplicate the current tool-ID map, usage normalizer or media types merely to rename them.

---

# Canonical request subset

The IR must cover the features EggPool needs for ordinary coding-agent/chat traffic:

## Request identity/control

- canonical model ID;
- stream boolean;
- max output tokens;
- temperature;
- top-p;
- stop sequence(s) where representable;
- structured-output/JSON-schema intent where currently supported;
- client-surface identity retained as metadata for response encoding;
- bounded metadata only where it has a defined cross-surface meaning.

## Messages/content

Represent chronological roles and typed content blocks sufficient for:

- system/developer instructions;
- user text;
- assistant text;
- image input where current EggPool capability rules allow it;
- document/media references already supported by current transcoder rules;
- tool call;
- tool result;
- reasoning/thinking blocks that are intentionally replayable/representable;
- refusal/stop metadata where needed to preserve current client behavior.

Do not force every surface's role vocabulary into the canonical type. Normalize semantically, then let codecs choose `system`, `developer`, `instructions`, Anthropic top-level system blocks, Gemini `system_instruction`, etc.

## Tools

Preserve:

- function name;
- description;
- JSON-schema parameters;
- tool choice/auto/required/specific function where current surfaces support it;
- tool-call ID mapping through the existing ID-map machinery;
- tool result association.

Do not add hosted/server-side tools such as OpenAI web search or Gemini Google Search to the portable IR in this phase. Same-surface passthrough may preserve vendor-native fields only when no cross-surface translation/failover is required.

---

# Reasoning intent is a semantic object

Create or consolidate one canonical reasoning request type before target selection.

Suggested semantics:

```python
@dataclass(frozen=True, slots=True)
class ReasoningIntent:
    requested: bool | None
    mode: Literal[
        "unspecified",
        "effort",
        "fixed_budget",
        "adaptive",
        "toggle",
    ]
    effort: str | None = None
    budget_tokens: int | None = None
```

Interpretation:

- `requested is None`: client expressed no reasoning preference;
- `requested is False`: explicit disable intent;
- `requested is True`: reasoning requested;
- `mode="effort"`: preserve a named effort such as low/high/xhigh without inventing a token budget;
- `fixed_budget`: preserve an explicit numeric budget;
- `adaptive`: preserve adaptive-thinking intent where a client surface can express it;
- `toggle`: client can only communicate enabled/disabled.

Use existing `ThinkingRequestRequirement` / capability types if they can be cleanly extended instead of introducing two representations. The invariant matters more than the type name.

### Encoding rule

Only after the actual target provider/model/wire profile is selected:

1. resolve the selected provider/model reasoning capability;
2. choose the target surface's supported control representation;
3. emit that representation;
4. reject or warn according to current strict/lenient loss policy when semantics cannot be represented.

Never perform `effort -> guessed budget -> effort` round-trips.

Preserve Plan 123:

- `none`/explicit disable cannot enable thinking;
- unknown effort values cannot silently become 4096 or another guessed budget;
- provider/model mappings are authoritative only when explicitly verified/configured.

---

# Response IR

For non-streaming cross-surface translation, represent only the common response semantics needed by existing clients:

```text
response/model ID
ordered output blocks
text
reasoning/thinking block where safely representable
tool calls
finish/stop reason
usage
provider request ID metadata when already captured safely
```

Do not attempt to preserve provider-private internal fields across unrelated surfaces unless the project already has a verified replay mechanism for that field.

Vendor-native response fields may pass through unchanged on the same-surface fast path.

---

# Canonical streaming events

Avoid translating Responses SSE into fake Chat chunks and then translating those chunks again.

Define a small event vocabulary such as:

```text
response_start
content_start
text_delta
reasoning_start
reasoning_delta
reasoning_stop
tool_call_start
tool_call_arguments_delta
tool_call_stop
content_stop
usage
response_complete
response_incomplete
error
```

Names are implementation-defined, but the event model must support:

- OpenAI Chat chunk deltas;
- OpenAI Responses typed events;
- Anthropic named message/content events;
- Gemini Interactions step events;
- Gemini generateContent streaming chunks;
- tool-call argument accumulation;
- a terminal success/failure signal independent of raw transport EOF.

A codec should decode upstream frames to canonical events; the client codec emits the client's expected wire grammar.

Do not retain the whole stream. Incremental event conversion must remain bounded in memory, with only the state required for tool-call argument assembly/IDs/usage and terminal recognition.

---

# Same-surface fast path

Cross-surface correctness must not make ordinary native traffic expensive.

When:

- client surface equals selected upstream surface;
- model suffix normalization is complete;
- no selected-provider semantic adaptation is needed;
- no locally rejected stateful field is present;

allow the existing direct/passthrough path to remain in use.

Examples:

```text
OpenAI Chat client -> OpenAI Chat upstream
Responses client -> Responses upstream
Anthropic client -> Messages upstream
```

Provider-specific safe request mutation such as model ID normalization/auth/header rendering may still occur outside the body codec.

Do not serialize a request into a canonical object and back out merely to prove that an IR exists if byte/JSON passthrough is already correct.

---

# Stateful feature boundary

Dynamic failover is safe only for stateless model turns.

Preserve/reinforce local rejection of stateful features when cross-provider/surface failover cannot preserve them.

Examples:

## OpenAI Responses

Cross-surface/failover requests must not depend on:

- `previous_response_id`;
- provider-owned conversation state;
- `background=true`;
- retrieval/cancel semantics;
- stored response IDs EggPool cannot route affinely.

## Gemini Interactions

The Interactions API stores by default. For a portable stateless canonical request:

- emit `store=false`;
- do not use `previous_interaction_id`;
- reject/background-disable provider-owned asynchronous interaction features;
- do not route managed Gemini agents through this generic model codec.

If same-surface passthrough later supports stateful affinity, that requires a separate plan. Do not quietly forward stateful IDs through a retryable multi-provider route.

---

# Vendor extensions / loss policy

Define a clear rule:

1. Same-surface passthrough may preserve unknown/vendor extension fields if current EggPool policy already allows safe passthrough.
2. Cross-surface translation only carries fields represented by the canonical IR or an explicit existing capability adapter.
3. Unsupported source fields are handled through the existing strict/lenient transcoder/loss policy.
4. Lenient handling must emit bounded structural warnings and must never invent semantic equivalence.

Do not add a generic `dict[str, Any] vendor_blob` that codecs can reinterpret arbitrarily across surfaces; that recreates hidden pairwise behavior.

---

# Migration from existing transcoders

Do not delete the current Chat↔Messages implementation in one risky rewrite.

Recommended sequence:

1. create canonical request/response/event types;
2. implement Chat and Messages adapters backed by existing tested helpers where possible;
3. run current body/stream parity tests through the new boundary;
4. keep a temporary compatibility wrapper exporting current transcoder entry points;
5. only remove duplicate pairwise code after focused parity is demonstrated.

Existing imports such as `select_streaming_transcoder()` may temporarily delegate to the new codec registry so the coordinator change can be staged.

Avoid maintaining two independent implementations long-term.

---

# Request-context ownership

Update `ProxyRequestContext` / `TranscodeContext` with explicit concepts rather than overloading `protocol`:

```text
client_surface
selected_wire_surface / wire_profile
canonical reasoning intent
transcode_required / semantic_adaptation_required
```

Keep historical `protocol` and `upstream_protocol` fields during migration if needed by router/catalog code, but document them as compatibility-family metadata rather than final endpoint selection.

A later phase should be able to choose another wire profile without mutating client intent or re-parsing a partly translated body.

This is important: **all alternate-surface attempts must encode from the original canonical request**, never from the payload emitted for the previously failed surface.

---

# Usage/accounting normalization

Reuse existing usage normalization rather than create per-surface accounting stores.

Canonical usage should accommodate at least:

- input tokens;
- output tokens;
- total tokens;
- cache read/write/input token details already used by EggPool;
- reasoning token details where provider-reported and currently modeled.

Each response codec maps its provider shape into the existing normalized usage path. Cost calculation remains provider/account/model-owned and unchanged.

Do not persist canonical response/event objects.

---

# Expected code surfaces

Likely files:

- new `src/eggpool/wire/ir.py`;
- new `src/eggpool/wire/codecs/` with base protocol/interfaces;
- `src/eggpool/transcoder/openai_to_anthropic.py`;
- `src/eggpool/transcoder/anthropic_to_openai.py`;
- `src/eggpool/transcoder/streaming.py`;
- `src/eggpool/transcoder/context.py`;
- `src/eggpool/transcoder/provider_adaptation.py`;
- `src/eggpool/request/thinking_adaptation.py`;
- request context/model definitions;
- current usage/tool-ID/media helpers;
- focused transcoder tests.

Do not touch routing health/failure retry policy in this phase except where type plumbing is unavoidable.

---

# Required focused tests

## Body parity

For representative existing Chat↔Messages cases, compare old expected behavior with the new canonical path for:

- plain text;
- system/developer instruction;
- tool declaration;
- tool call and tool result;
- structured output supported by both surfaces;
- image input already supported by current capability policy;
- max-output limit mapping;
- stop/finish reasons;
- usage.

Do not duplicate hundreds of low-value fixtures; reuse/parameterize existing high-value cases.

## Reasoning

Verify:

- explicit disabled intent remains disabled through any intermediate IR;
- effort string remains an effort until selected-target encoding;
- fixed budget remains numeric intent and is not reinterpreted as an effort tier;
- an unsupported effort produces local strict/lenient handling, not provider health mutation;
- encoding two different target surfaces from the same canonical request does not reuse a transformed payload from attempt one.

## Streaming

Prove that canonical event sequences can represent:

- Chat text + `[DONE]`/finish;
- Anthropic `message_start` -> content deltas -> `message_stop`;
- tool-call argument deltas;
- explicit error terminal;
- usage terminal event.

Responses/Gemini concrete decoder tests belong mainly to Plan 152, but the event vocabulary must be sufficient here.

## Fast path

Assert that same-surface passthrough does not invoke cross-surface decode/re-encode machinery when no semantic adaptation is required.

---

# Acceptance criteria

- [ ] EggPool has one canonical request intent for cross-surface encoding rather than pairwise payload chaining.
- [ ] Alternate target payloads can always be rebuilt from the original canonical request.
- [ ] A minimal canonical response/event vocabulary exists for cross-surface output.
- [ ] Existing Chat↔Messages supported behavior passes focused parity tests.
- [ ] Same-surface traffic retains a passthrough/low-overhead path.
- [ ] Reasoning effort, fixed budget, adaptive/toggle intent and explicit disable are distinguishable before target selection.
- [ ] Reasoning capability no longer decides the upstream wire surface.
- [ ] Unknown effort labels are not converted to guessed budgets.
- [ ] Stateful Responses/Interactions features are excluded from generic failover semantics.
- [ ] Streaming conversion is incremental and bounded; no full-response buffering is introduced for SSE.
- [ ] Existing usage/cost and tool-call-ID infrastructure is reused.
- [ ] No provider SDK or new runtime dependency is introduced.
- [ ] No database schema change is introduced.

---

# Rejection conditions

Reject implementation if it:

- creates a generic, recursive API schema for every vendor field;
- buffers full streams to simplify translation;
- routes every same-surface request through encode/decode overhead unnecessarily;
- retains pairwise transforms as the permanent source of truth for each new surface;
- uses a previously transformed outbound payload as input for another surface attempt;
- invents reasoning budgets/effort equivalence;
- silently forwards stateful provider IDs through retryable cross-provider routing;
- creates another usage/cost subsystem;
- adds dependencies for data modeling/serialization already handled by Python/Pydantic/project utilities.

---

# Verification

Run focused transcoder/body/stream/reasoning tests, then normal lint/type/smoke gates. Do not add live-provider CI in this phase.

A useful implementation checkpoint is to run the existing Chat↔Messages tests both before and after switching their public selector wrappers to the canonical implementation and record any intentional semantic differences.

---

# Handoff

1. Read Plans 147–148, Plan 123, and current transcoder/stream tests.
2. Inventory currently supported semantic fields; do not expand scope merely because a provider has more fields.
3. Add minimal IR and codec interfaces.
4. Normalize reasoning intent before target selection.
5. Adapt Chat/Messages body and streaming paths using current tested helpers.
6. Preserve same-surface passthrough.
7. Add focused parity/fast-path tests.
8. Run the ordinary project gate and record implementation SHA/results here.
