# Plan 046 — Provider Thinking-Control Normalization and Contract Resolution

Date: 2026-07-30
Status: implementation handoff
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Make provider-bound thinking/reasoning control handling total, typed, and deterministic before upstream dispatch, with specific closure for OpenCode Go MiniMax-M3 while preserving native MiniMax behavior.

This phase fixes the request controls themselves. It must not redesign finalization, stream completion, timeout policy, or the general provider-bound payload lifecycle; those are owned by Plans 047 through 051.

## Confirmed defects to close

1. The fixed-contract adapter removes `thinking.type` in a temporary copy but does not mark the payload changed unless `budget_tokens` was present, allowing a type-only block to pass through unchanged.
2. The fixed-contract adapter does not remove `thinking.effort`.
3. Unsupported `reasoning_effort` values and already-valid values share the same `None` return shape, so unsupported values can be falsely reported as mapped while remaining in the payload.
4. Built-in contract selection applies global priority before specificity despite documentation that provider ID is more specific than provider kind or URL.
5. The OpenCode Go rule depends on exact provider ID only and lacks a compatibility endpoint matcher for equivalent configured identities.
6. Adaptation warnings and `emitted_controls` can claim a field was mapped or emitted when no valid transformation occurred.

## Ownership boundary

Primary modules:

- `src/eggpool/transcoder/provider_adaptation.py`
- `src/eggpool/transcoder/builtin_contracts.py`
- `src/eggpool/catalog/capabilities.py`
- the narrow coordinator adapter that invokes provider adaptation
- focused unit/integration tests for provider contracts and captured upstream bodies

Do not alter request finalization ownership, SSE stream state, provider timeout classes, routing algorithms, or database schemas in this phase.

## Required design

### 1. Replace ambiguous adaptation returns with typed dispositions

Every field-level adapter must return an explicit result. A suitable internal shape is:

```python
@dataclass(frozen=True, slots=True)
class ControlFieldAdaptation:
    disposition: Literal[
        "unchanged",
        "mapped",
        "dropped",
        "rejected",
        "not_present",
    ]
    payload: dict[str, Any]
    requested_field: str | None = None
    emitted_field: str | None = None
    warning: AdaptationWarning | None = None
```

The exact class name may differ, but these distinctions must not collapse into `dict | None`.

Rules:

- `not_present`: the request did not contain the field.
- `unchanged`: the field is present and already accepted by the contract.
- `mapped`: an explicit alias or contract mapping changed the value/field.
- `dropped`: policy authorized removing an unsupported control.
- `rejected`: policy requires a local capability error.

### 2. Enumerate the complete supported control surface

The provider-bound normalizer must deliberately handle:

- top-level `reasoning_effort`
- top-level `thinking_budget`
- `thinking.type`
- `thinking.effort`
- `thinking.budget_tokens`
- empty `thinking` objects after field removal
- non-dict `thinking` values without crashing or silently inventing semantics

Historical reasoning content fields must remain independent from client-selectable control fields.

### 3. Fixed contract behavior

For `mode="fixed"`, client-selectable controls are not accepted.

Under `unsupported_control="reject"`:

- return a local `CapabilityError` before upstream dispatch;
- preserve the original request for error reporting;
- do not mutate shared health/backoff/quarantine state.

Under `unsupported_control="warn_drop"`:

- remove every client-selectable control listed above;
- remove the `thinking` object when it becomes empty;
- preserve unrelated keys inside `thinking` only when they are explicitly historical content rather than controls;
- emit one bounded warning with the exact removed fields;
- never claim a removed field was emitted.

Under `unsupported_control="map_if_known"`:

- use only explicit contract aliases/mappings;
- a fixed contract has no selectable target, so unmappable controls must follow a documented deterministic fallback, preferably local rejection rather than silent passthrough;
- do not infer a mapping from global defaults.

### 4. Effort contract behavior

For `mode="effort"`:

- accept values listed in `accepted_efforts` case-insensitively while preserving the canonical emitted spelling;
- map only aliases in `effort_aliases`;
- reject or drop unknown effort values according to policy;
- do not leave an unsupported value in the payload while returning `mapped`;
- remove incompatible budget controls unless an explicit effort-to-budget conversion is required by the target wire protocol and authorized by the contract.

### 5. Budget and effort-or-budget behavior

- Enforce explicit minimum/maximum bounds using the existing budget resolution policy.
- Do not apply provider-independent mappings after the selected provider contract is known unless the contract lacks a mapping and the existing documented fallback authorizes it.
- Ensure clamped, mapped, dropped, and rejected decisions remain distinguishable in the thinking trace.

### 6. Correct contract ordering

Built-in contract resolution must choose in this order:

1. highest match specificity: provider ID, then provider kind, then base URL;
2. lowest priority number within that specificity;
3. reject ambiguity when specificity and priority are equal.

Do not choose a lower-specificity rule because it has a numerically lower priority.

### 7. OpenCode Go and native MiniMax matching

The canonical rules must preserve these distinct contracts:

- OpenCode Go MiniMax-M3: fixed thinking, no client-selectable control.
- Native MiniMax MiniMax-M3: provider-supported control contract as currently documented/configured.

Add an endpoint compatibility matcher for OpenCode Go's known base URL so a user-chosen provider ID pointing to that service does not lose the fixed contract. Exact provider ID must remain more specific than URL matching.

Do not use an endpoint matcher broad enough to capture native MiniMax.

### 8. Trace and warning truthfulness

`provider_control_decision`, warning records, removed fields, and emitted fields must describe the final upstream payload, not the original request or an attempted transformation.

Required invariants:

- `emitted_controls` is a subset of fields present in the final provider payload.
- `requested_controls` reflects the client's actual intent.
- `dropped` includes exact removed fields.
- `mapped` includes source and destination/value mapping.
- `rejected` never produces an upstream request.

## Implementation sequence

### Workstream A — Characterization tests

Add failing tests for at least:

```json
{"reasoning_effort":"high"}
{"reasoning_effort":"xhigh"}
{"thinking":{"type":"enabled"}}
{"thinking":{"effort":"high"}}
{"thinking":{"budget_tokens":4096}}
{"thinking":{"type":"enabled","budget_tokens":4096}}
{"thinking_budget":4096}
```

Run each against fixed, effort, budget, effort-or-budget, none, and unknown contracts where meaningful, under all provider-control policy modes.

### Workstream B — Typed field adaptation

Introduce the typed field result and update the aggregate adapter to derive one final `ProviderRequestAdaptation` without ambiguous `None` semantics.

### Workstream C — Contract resolver ordering

Correct specificity/priority ordering and ambiguity detection. Add compatibility endpoint matching for OpenCode Go and a negative native MiniMax case.

### Workstream D — Full request-path capture

Through the real/in-process Eggpool proxy path available in the repository, capture the body received by a deterministic mock upstream.

Assert that:

- fixed-contract reject sends no request;
- fixed-contract drop sends a body with no unsupported control;
- native MiniMax preserves/maps its accepted controls;
- unrelated payload fields are unchanged.

### Workstream E — Documentation

Update provider-control documentation and configuration examples only where current behavior is misstated. Do not add a broad compatibility catalog.

## Required tests

### Unit tests

- Every control spelling and policy disposition.
- Type-only `thinking` block is actually changed/dropped.
- `thinking.effort` is handled.
- Unsupported effort cannot be reported as mapped.
- `emitted_controls` exactly matches final payload fields.
- Specificity outranks priority.
- Equal-specificity/equal-priority ambiguity fails deterministically.
- Exact provider ID outranks URL compatibility match.
- OpenCode Go URL compatibility match does not capture native MiniMax.

### Integration tests

- OpenAI client to OpenCode Go MiniMax fixed contract.
- Anthropic client to OpenCode Go MiniMax fixed contract.
- OpenAI client to native MiniMax effort-capable contract.
- Streaming and non-streaming request construction use identical adaptation decisions.
- Reject path opens no upstream connection.
- Warn-drop path sends sanitized body.

### Negative tests

- Non-dict `thinking` does not crash.
- Unknown contract follows `unknown_contract` policy and does not claim a known mapping.
- Historical reasoning content is not removed merely because selectable controls are fixed.
- A provider ID resembling `minimax` but using the OpenCode URL resolves by specificity as designed.

## Acceptance criteria

- [ ] No fixed-contract request can forward `reasoning_effort`, `thinking_budget`, `thinking.type`, `thinking.effort`, or `thinking.budget_tokens` contrary to policy.
- [ ] A type-only `thinking` block is observably removed or rejected rather than returned unchanged.
- [ ] Unsupported effort and accepted effort produce different typed dispositions.
- [ ] No unsupported effort is labeled mapped while remaining in the final payload.
- [ ] Contract resolution evaluates specificity before priority.
- [ ] OpenCode Go resolves the fixed contract by canonical ID and known endpoint compatibility match.
- [ ] Native MiniMax does not resolve the OpenCode fixed contract.
- [ ] Reject mode performs zero upstream dispatches.
- [ ] Warn-drop mode preserves all unrelated payload fields byte-semantically after JSON normalization.
- [ ] Streaming and non-streaming request construction produce the same provider-control decision.
- [ ] Provider-control traces and warnings describe the final upstream body truthfully.
- [ ] Focused tests pass under the repository's supported Python versions.
- [ ] Repository lint, formatting, and type checks pass for touched modules.

## Explicit rejection conditions

Do not mark Plan 046 complete if:

- any control field is handled by incidental dict cleanup rather than an explicit decision;
- `dict | None` still conflates unsupported and unchanged effort values;
- a compatibility URL rule can override a more-specific provider ID;
- tests invoke only private helpers and never capture the body sent through Eggpool;
- the fix depends on user configuration overrides for the canonical OpenCode Go case;
- reject mode dispatches upstream before returning the error;
- the phase changes terminal cleanup or timeout semantics outside narrowly required compile/test updates.

## Handoff record

The implementation handoff must state:

- implementation commit SHA;
- exact files changed;
- the final control decision table;
- focused test commands and counts;
- captured upstream-body cases;
- whether any provider behavior remains unknown;
- any follow-up defect assigned to another roadmap phase.

## Definition of done

Plan 046 is complete when provider-bound thinking controls have total typed semantics, OpenCode Go MiniMax-M3 receives no unsupported selectable control, native MiniMax retains its distinct supported behavior, contract matching follows documented specificity, and the complete behavior is proven through both decision-table tests and captured real Eggpool upstream requests.