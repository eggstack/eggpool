# Provider-Bound Thinking-Control Normalization

Date: 2026-07-25
Status: completed

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`

## Objective

Add an explicit provider-bound request-contract layer so thinking/reasoning controls are validated and normalized after the provider/account has been selected, regardless of whether protocol transcoding is required. Close the concrete MiniMax-M3 through OpenCode Go failure without special-casing request cleanup or weakening provider-health policy.

The result must distinguish “model can produce reasoning” from “this provider deployment accepts client-selectable effort or budget controls.” It must support native OpenAI-compatible requests, native Anthropic-compatible requests, and both transcoding directions.

## Core design requirement

Thinking behavior must no longer be represented by one overloaded capability status plus an optionally empty effort list. Introduce a contract that explicitly answers:

1. Does the model/provider produce reasoning?
2. May the client control reasoning for this deployment?
3. Which wire fields are accepted?
4. Which effort labels are accepted?
5. Is an explicit token budget accepted?
6. Which aliases or mappings are safe?
7. What happens when the requested control cannot be represented?
8. Is historical reasoning content accepted or required independently of new reasoning controls?

## Scope

### In scope

- Additive provider/model capability schema.
- Backward-compatible parsing and serialization.
- Post-selection provider-bound request normalization.
- Native and transcoded request paths.
- OpenCode Go MiniMax-M3 capability contract.
- Strict, reject, warn/drop, and mapped adaptation policy.
- Optional one-time allowlisted compatibility retry before response bytes.
- Request tracing and counters for adaptation decisions.
- Configuration overrides and validation.
- Tests against the Plan 023 mock upstream.

### Out of scope

- General failure-effects redesign; Plan 025 owns shared-state consequences.
- Process-owned finalization; Plan 026 owns cleanup.
- Automatic database recovery; Plan 027 owns connection replacement.
- Broad automated inference of provider contracts from arbitrary error text.
- Compatibility retry after response bytes or stream chunks.

## Workstream A — Extend the capability schema

Add a structured control contract beneath `ThinkingCapability`. Suggested shape:

```python
class ThinkingControlContract(BaseModel):
    mode: Literal["unknown", "none", "fixed", "effort", "budget", "effort_or_budget"]
    request_fields: list[str] = []
    accepted_efforts: list[str] = []
    effort_aliases: dict[str, str] = {}
    effort_to_budget_tokens: dict[str, int] | None = None
    explicit_budget_min: int | None = None
    explicit_budget_max: int | None = None
    historical_reasoning_content: Literal["unknown", "accepted", "required", "rejected"] = "unknown"
    source: CapabilitySource = "unknown"
```

The exact naming may differ, but the following semantics are mandatory:

- `unknown`: metadata is absent; policy decides whether best-effort routing is allowed.
- `none`: reasoning controls are not accepted and reasoning is not available.
- `fixed`: the model may reason, but the client cannot select effort or budget.
- `effort`: a named effort field is accepted.
- `budget`: an explicit token budget is accepted.
- `effort_or_budget`: both are accepted.

Do not retain an ambiguity where an empty effort list means both “unknown metadata” and “known fixed behavior.”

Existing fields must deserialize into a conservative inferred contract:

- `status=unsupported` -> `mode=none`.
- non-empty `supported_efforts` -> `mode=effort`.
- budget bounds without efforts -> `mode=budget` only when an accepted budget field is known; otherwise `unknown`.
- `status=supported` with no control fields -> `mode=unknown`, not `fixed`, unless a manual/built-in source says fixed.

## Workstream B — Define provider-bound adaptation results

Create a typed pure result, for example:

```python
@dataclass(frozen=True, slots=True)
class ProviderRequestAdaptation:
    payload: Mapping[str, Any]
    changed: bool
    decision: Literal["passthrough", "mapped", "dropped", "rejected"]
    requested_controls: tuple[str, ...]
    emitted_controls: tuple[str, ...]
    warnings: tuple[AdaptationWarning, ...]
    retry_signature: str | None = None
```

The adaptation function must be pure with respect to runtime health, database state, routing, and logging. It receives:

- original parsed client payload;
- client protocol;
- selected provider/model/account identity;
- selected provider's capability contract;
- transcoder output if transcoding was required;
- operator adaptation policy.

It returns a provider-bound decoded payload or raises a typed `CapabilityError` before upstream dispatch.

## Workstream C — Insert a common post-selection normalization stage

Add one shared stage invoked by both streaming and non-streaming execution before `client.build_request()`.

Required order:

1. Select account/provider.
2. Resolve the provider-specific model capability contract.
3. Obtain decoded provider-bound payload.
4. Re-resolve effort/budget using original client intent.
5. Normalize or reject provider controls.
6. Apply synthetic cache controls and other provider-bound transforms.
7. Inject streaming usage options where applicable.
8. Serialize once.
9. Build and send the upstream request.

The normalization stage must run when:

- client and upstream protocols are equal;
- transcoding is required;
- the payload was preflight-transcoded and reused;
- the selected provider differs from the provider assumed by collapsed preflight metadata.

The current `context.transcode_required` guard must not determine whether provider-specific thinking normalization runs.

## Workstream D — Preserve original client intent

Store a normalized immutable `ThinkingRequestIntent` in `ProxyRequestContext`, including:

- requested effort and original spelling;
- requested explicit budget;
- requested fields;
- whether historical reasoning content was present;
- whether the client explicitly requested new reasoning or only replayed history;
- client protocol.

Provider adaptation must use this original intent rather than re-reading already-translated fields. This prevents an intermediate fallback budget from becoming falsely authoritative.

## Workstream E — Add the OpenCode Go MiniMax-M3 contract

Establish the contract from deterministic repository fixtures and, optionally, documented/live evidence outside CI. The built-in contract must be narrowly scoped by:

- canonical provider base URL or provider kind;
- canonical model identity and known aliases;
- protocol endpoint;
- accepted request fields and effort labels.

Do not mark every MiniMax-M3 provider identically. MiniMax's native endpoint is known to behave differently and must retain its own contract.

At minimum, the implementation must encode one of these explicit outcomes for OpenCode Go MiniMax-M3:

- fixed reasoning: remove effort only under warn/drop policy, reject under strict policy; or
- effort-controlled reasoning: map accepted labels and reject unsupported labels locally.

The contract must include an operator override mechanism so upstream changes do not require a code release.

## Workstream F — Define adaptation policy

Extend configuration with an additive policy, for example:

```toml
[transcoder.provider_control_policy]
unsupported_control = "reject"       # reject | warn_drop | map_if_known
unknown_contract = "reject"          # reject | allow_with_warning
allow_compatibility_retry = false
```

Required behavior:

- `reject`: local protocol-appropriate HTTP 400 capability response; no upstream attempt.
- `warn_drop`: remove only the unsupported optional control; preserve reasoning history; emit bounded warning.
- `map_if_known`: use only explicit contract aliases/mappings; otherwise reject.
- unknown contract under `allow_with_warning`: forward unchanged but record a decision; do not infer support.

Strict transcoding/loss policy takes precedence over any warn/drop setting.

Configuration validation must forbid unsupported values and inconsistent combinations.

## Workstream G — Optional one-time compatibility retry

A compatibility retry is optional and should be implemented only if it remains narrow and testable.

If implemented, it must satisfy all of the following:

- Disabled by default initially.
- Only for allowlisted provider/model/error signatures.
- Only before any response bytes are emitted.
- At most one adaptation retry per request.
- Same selected account unless the normal retry policy independently selects another account.
- Does not increment account failure or circuit-breaker counters.
- Does not persist backoff or model quarantine.
- Finalizes the first attempt as a compatibility/client-validation outcome, not upstream-health failure.
- Records the exact removed/mapped field category without request content.
- Strict policy disables the retry.

Do not implement generic “retry after removing unknown fields.”

## Workstream H — Observability

Add bounded counters by provider/model/protocol and decision:

- thinking control requested;
- provider contract known/unknown;
- passthrough;
- mapped;
- dropped;
- locally rejected;
- compatibility retry attempted/succeeded/failed;
- historical reasoning content accepted/rejected.

Update the existing `thinking_trace` or replace it with a typed trace representation that can serialize to the current database field.

Trace data must include:

- client fields;
- selected provider;
- contract source and mode;
- requested effort/budget category;
- emitted upstream fields;
- adaptation decision;
- whether a compatibility retry occurred.

Do not persist raw prompt or reasoning content.

## Workstream I — Testing

Suggested files:

- `tests/unit/test_plan_024_thinking_control_contract.py`
- `tests/unit/test_plan_024_provider_request_adaptation.py`
- `tests/unit/test_plan_024_native_provider_normalization.py`
- `tests/unit/test_plan_024_transcoded_provider_normalization.py`
- `tests/integration/test_plan_024_opencode_minimax_contract.py`
- `tests/integration/test_plan_024_compatibility_retry.py`
- `tests/unit/test_plan_024_thinking_trace.py`

Required matrix:

- OpenCode Go MiniMax-M3 native OpenAI path.
- MiniMax native MiniMax-M3 path.
- Same collapsed model routed to providers with different contracts.
- `low`, `med`, `medium`, `high`, unsupported, unknown, omitted.
- Explicit Anthropic budget below/within/above bounds.
- Historical reasoning content with no new requested effort.
- Streaming and non-streaming.
- Preflight-transcoded payload reuse and recompute.
- Strict, reject, warn/drop, map-if-known, and unknown-contract policies.
- Configuration and built-in override precedence.

## Compatibility and migration notes

Any persisted capability JSON must continue to parse. Additive fields should default conservatively. If a database migration is required for provider capability snapshots, it must preserve raw source observations and mark inferred control modes with explicit provenance.

Manual overrides must outrank built-ins. Provider catalog/model-info observations may update built-ins only under deterministic merge rules. A “supported” aggregate model must not erase provider-specific differences.

The `/models` endpoint should expose compact provider-specific control metadata where existing response size policy allows it. Existing clients that ignore the new fields must remain unaffected.

## Acceptance criteria

### Schema

- [ ] Capability metadata distinguishes reasoning support from controllability.
- [ ] `fixed`, `effort`, `budget`, `none`, and `unknown` are represented unambiguously.
- [ ] Existing capability records deserialize without failure.
- [ ] Manual overrides deterministically outrank built-in and discovered metadata.
- [ ] Collapsed models retain provider-specific contracts.

### Request lifecycle

- [ ] Provider-bound normalization runs for native and transcoded paths.
- [ ] It runs after provider selection and before upstream request construction.
- [ ] Original client intent is preserved and used for final resolution.
- [ ] Common execution code is shared by streaming and non-streaming paths.
- [ ] No provider-specific normalization branch mutates health or database state.

### MiniMax-M3/OpenCode Go

- [ ] The Plan 023 unsupported-thinking reproducer no longer forwards an invalid control.
- [ ] Behavior is explicit: local reject, known mapping, or configured drop.
- [ ] The same model through MiniMax's native provider retains its distinct accepted behavior.
- [ ] An unrelated request succeeds immediately after the rejected/adapted request.
- [ ] A subsequent MiniMax-M3 request without thinking controls succeeds.
- [ ] No account, model, circuit, catalog, or durable backoff state changes.

### Policy and retry

- [ ] Strict policy never silently changes or removes a requested control.
- [ ] Warn/drop removes only contract-declared optional controls.
- [ ] Mapping uses only explicit contract mappings.
- [ ] Unknown contract handling is configuration-driven and observable.
- [ ] If compatibility retry is implemented, it is one-shot, allowlisted, pre-body, and health-neutral.
- [ ] Generic unknown-field retry is absent.

### Observability

- [ ] Every adaptation decision has a bounded counter and trace category.
- [ ] Trace includes selected provider and contract provenance.
- [ ] No prompt or reasoning content is persisted.
- [ ] Existing thinking metrics remain backward compatible or have documented replacements.

### Verification

- [ ] Plan 023 baseline tests continue to pass.
- [ ] Focused Plan 024 tests pass on Python 3.11 and 3.12.
- [ ] Native/transcoded streaming and non-streaming request-path suites pass.
- [ ] Standard non-slow suite passes.
- [ ] Ruff format, Ruff check, Pyright, and xfail/skip audit pass.
- [ ] No live credentials are required.

## Closure evidence

Update this plan to `Status: completed` only when the exact implementation SHA, focused commands, and a contract table for OpenCode Go MiniMax-M3 versus MiniMax native MiniMax-M3 are recorded. The evidence must include the state-audit diff proving zero shared-state effects for local capability rejection and any compatibility retry.
