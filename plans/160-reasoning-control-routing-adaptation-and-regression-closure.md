# Plan 160 — Reasoning-Control Routing, Adaptation, and Regression Closure

Date: 2026-09-03
Status: complete
Parent roadmap: `plans/157-provider-bound-reasoning-control-discovery-roadmap.md`
Depends on:
- `plans/158-compositional-reasoning-capability-schema-and-metadata-normalization.md`
- `plans/159-reasoning-capability-source-precedence-and-static-assumption-removal.md`
Planning baseline: `df64a5e3e33964b1c811f04e2ed79e12473a3db4`
Priority: P0 end-to-end correctness / closure
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Make routing and provider-bound request adaptation consume the exact compositional reasoning-control contract produced by Plans 158–159, then close the original MiniMax/OpenCode Go failure with focused deterministic and credentialed acceptance coverage.

The key behavior change is simple:

> A provider/model that supports reasoning is not automatically eligible for every reasoning control.

EggPool must distinguish a request that merely needs a reasoning-capable model from a request that asks for a specific toggle, effort value, or budget. It must then select/adapt only against the selected provider/model's verified control contract.

This phase must not reopen wire negotiation, failure classification, health/backoff, or retry architecture.

---

## Current behavior to correct

`src/eggpool/routing/eligibility.py` currently checks thinking capability status and, for effort requests, `candidate_supports_requested_effort()`.

Legacy behavior treats an empty `supported_efforts` list as effectively unknown/allow rather than known unsupported. That is necessary under the old ambiguous representation but incorrect once Plan 158 can distinguish:

```text
effort = unknown
```

from:

```text
effort = unsupported
```

The current post-selection adaptation path likewise reasons through the old single-mode contract. This permits an effort request to reach a provider that only supports toggle/fixed reasoning, or encourages compatibility mapping that fabricates semantics.

OpenCode Go MiniMax-M3 is the concrete regression:

```text
verified provider contract: reasoning supported; toggle supported; effort unsupported
historical EggPool contract: effort supported [low, medium, high]
```

An OpenAI client request such as `reasoning_effort="high"` must no longer be translated into a supposed MiniMax high-effort control.

---

## Governing request semantics

Use the existing canonical reasoning-intent representation in `src/eggpool/wire/ir.py` where possible. Do not create a second request-level reasoning model if `ReasoningIntent` can be extended narrowly.

Routing/adaptation must distinguish at least:

1. no caller reasoning preference;
2. explicit reasoning enable/disable through a binary toggle;
3. explicit named effort, including an effort value such as `none` where the source API defines it;
4. explicit reasoning budget;
5. response-only historical reasoning fields, which are not permission to enable new thinking controls.

The request intent should preserve the client's semantic control kind, not only a generic boolean `thinking_required`.

### No caller reasoning preference

A request with no reasoning/thinking control should not exclude a model merely because the model reasons by default or has no caller control.

Do not manufacture an enable/disable field.

### Explicit binary toggle

A client request that semantically asks for reasoning on/off may be routed to a provider/model only when:

- the target has a verified toggle control; or
- the selected target has another control that can faithfully represent the same binary semantic under an existing explicitly documented mapping.

Do not assume effort `low` is equivalent to toggle-on or that omitting a field is equivalent to toggle-off unless the target contract explicitly establishes it.

### Explicit effort

An effort request requires:

- `effort = supported`; and
- the requested normalized value to appear in the exact accepted effort set, unless the operator has configured an explicit alias/mapping.

A toggle-only target is not effort-capable.

### Explicit disable through effort `none`

If the source API defines `none` as an effort value, preserve it as effort intent. A target may satisfy it through:

- the same verified effort value; or
- a verified semantic mapping to the target's disable toggle if the existing adapter/policy explicitly supports such mapping.

Do not map `none` to omission when omission may mean default/on/unknown.

### Explicit budget

A budget request requires verified budget support and valid provider/model bounds. Do not use `max_tokens` or generic output limits as reasoning-budget evidence.

---

## Workstream A — Refactor routing requirement classification

Audit current owners of:

- `ThinkingRequestRequirement` or its current equivalent;
- `classify_thinking_requirement()` / `client_requests_thinking()` helpers;
- `candidate_supports_requested_effort()`;
- `check_candidate_thinking_eligibility()`;
- `routing/eligibility.py` provider loop;
- selected-provider post-routing validation.

Replace effort-only special casing with a compact requirement shape derived from canonical request intent.

A reasonable target is conceptually:

```text
ReasoningRequirement:
    needs_reasoning: bool
    control_kind: none | toggle | effort | budget
    requested_toggle: bool | None
    requested_effort: str | None
    requested_budget: int | None
```

The implementation may reuse `ReasoningIntent` directly instead of adding this dataclass if doing so keeps module boundaries clean.

Do not model every source-protocol spelling. Parse protocol-specific request fields into canonical intent before eligibility.

---

## Workstream B — Make provider eligibility exact

For each candidate account/provider/model, obtain the exact provider-bound capability entry as EggPool already does.

Evaluate against the canonical contract:

### Feature requirement

If the request actually requires reasoning output, `ThinkingCapability.status` must be supported under existing supported/unknown policy.

### Toggle requirement

Known `toggle = unsupported` means ineligible for a binary toggle request unless an explicit faithful adapter mapping exists.

Known `toggle = supported` is eligible subject to target wire encoding.

`toggle = unknown` follows the existing unknown-control policy, but it must remain unknown in diagnostics and must not be rewritten as supported.

### Effort requirement

Known `effort = unsupported` means ineligible.

Known `effort = supported` requires exact accepted value/explicit alias.

Known `effort = supported` with an empty accepted list is malformed metadata/config and must not become permissive.

`effort = unknown` follows unknown-control policy without invented values.

### Budget requirement

Known `budget = unsupported` means ineligible.

Known supported budget validates explicit min/max where present.

Unknown bounds do not imply arbitrary numeric acceptance if provider policy requires a known bound; preserve current safe policy or document the bounded behavior.

### No explicit control

Do not exclude a reasoning-capable fixed/no-control provider merely because it lacks toggle/effort/budget controls when the client did not request one.

---

## Workstream C — Define known mismatch versus unknown policy clearly

Preserve the existing separation between capability-status policy and provider-control policy, but remove ambiguity created by the old mode enum.

Recommended default behavior:

- known unsupported requested control -> local reject/filter according to existing `unsupported_control = "reject"` semantics;
- unknown requested control -> preserve existing `unknown_contract` policy (`allow_with_warning` unless project configuration has changed), but never claim the control is supported;
- explicit operator policy may allow a lossy mapping/drop where existing configuration already supports it;
- no automatic model-family compatibility mapping.

When multiple accounts/providers serve the same model, prefer candidates that can faithfully honor the requested control over unknown/lossy candidates using the existing eligibility/ranking mechanisms rather than adding a new scoring subsystem.

If current routing only supports binary eligibility and cannot express preference without architecture churn, filter known mismatches and preserve existing account ordering among remaining candidates. Do not build a capability ranking engine in this pass.

---

## Workstream D — Provider-bound adaptation must preserve semantic kind

Audit `adapt_thinking_controls()` and coordinator integration after account selection.

Required rules:

### Toggle-only target

For a verified toggle request, encode only the verified target toggle form.

For a client effort request, do not convert `low`, `medium`, `high`, `xhigh`, etc. into toggle-on merely because the target can reason. That loses requested intensity semantics.

### Effort target

Pass/normalize only accepted values and configured aliases.

Do not derive target budgets from generic effort names unless an explicit provider/model mapping exists or an already-documented cross-protocol compatibility policy from Plan 123 applies. Keep any compatibility fallback clearly separate from capability truth.

### Fixed/no-control target

When the client sends no reasoning control, leave the provider request free of fabricated controls.

When the client explicitly asks to change reasoning state/effort/budget, reject/filter or apply an explicitly configured loss policy. Do not inject a field the provider does not advertise.

### Budget target

Honor explicit verified budget bounds/mappings only.

### Unknown contract

If policy allows forwarding unknown controls, preserve the client's structurally compatible field only when the selected wire codec supports that field shape. Do not translate unknown effort semantics into a different control kind.

Warnings must state that support is unknown, not that compatibility was detected.

---

## Workstream E — Keep semantic capability separate from wire encoding

Do not reintroduce model-specific wire field paths into capability inference.

The selected provider/wire adapter remains responsible for request grammar. The compositional contract authorizes a semantic control kind; the adapter encodes it.

Examples:

```text
semantic toggle
    -> target adapter may encode thinking.type, thinking, enable_thinking,
       chat_template_kwargs, or another verified field

semantic effort
    -> target adapter may encode reasoning_effort or a verified target effort field
```

Rules:

- model ID does not choose the field syntax by itself;
- provider/wire selection must already be resolved through the existing wire-profile architecture;
- reasoning capability mismatch is not wire-surface evidence;
- an unsupported reasoning field must not trigger blind alternate-wire enumeration;
- do not modify Plan 147–156 wire-learning invariants unless a compile-level API change is unavoidable.

For OpenCode Go MiniMax-M3 specifically, the current provider metadata identifies the provider package/surface as Anthropic-compatible and the reasoning option as toggle. The final outbound shape must be determined by the existing selected wire/provider adapter and verified target semantics, not by a new `if model_id == "minimax-m3"` branch.

---

## Workstream F — Failure isolation and retry behavior

A capability/control mismatch detected locally is a request-local preparation/capability outcome.

It must not:

- disable credentials;
- increment provider failure counters as an upstream outage;
- trip circuits;
- quarantine an account;
- suppress the model/provider for 30 minutes;
- mutate wire-profile learned state;
- require restart or DB deletion to recover.

If an actual upstream request is made and returns a deterministic control-specific 4xx, preserve the existing failure-classification safety rules. Do not add runtime control learning in this roadmap.

A compatibility retry that already exists for provider-control policy may remain only if its semantics are still valid and bounded before downstream handoff. Do not add another retry loop.

If the now-truthful metadata prevents the known bad MiniMax request before dispatch, that is preferable to learning by failure.

---

## Workstream G — API/dashboard capability rendering

Audit the current `/models`/dashboard representation of thinking capabilities only as needed to prevent false claims.

Required output behavior:

- MiniMax toggle-only must not render effort levels low/medium/high;
- fixed/no-control reasoning must not render as "thinking unsupported";
- unknown controls should remain visibly unknown where the current UI exposes control detail;
- collapsed model views must not union different provider controls into a false single-provider contract.

Do not redesign the dashboard. Minimal field/render changes only.

If the API has compatibility clients relying on legacy fields, emit derived compatibility fields from the canonical contract where truthful. An empty legacy `supported_efforts` value must not be used to conceal whether effort is unknown versus unsupported when the richer provider entry is available.

---

## Workstream H — Focused deterministic regression matrix

Use existing routing, thinking matrix, provider-bound adaptation, transcoder, and coordinator tests. Do not create a new testing framework or large combinatorial matrix.

Required cases:

### H1 — MiniMax toggle-only

Provider contract:

```text
reasoning supported
toggle supported
effort unsupported
budget unsupported
```

Assertions:

- binary reasoning enable/disable request is eligible when wire adapter can represent it;
- `reasoning_effort="high"` is not accepted as a known supported MiniMax control;
- no `low/medium/high` provider capability is synthesized;
- local mismatch does not affect account health.

### H2 — MiMo fixed/no-control

Contract:

```text
reasoning supported
all caller controls unsupported
```

Assertions:

- ordinary request remains eligible;
- EggPool does not inject reasoning controls;
- explicit effort/toggle/budget request is a known mismatch under strict/default policy;
- a failed mismatched request does not poison the next ordinary request.

### H3 — Muse exact efforts

Contract:

```text
effort supported
minimal, low, medium, high, xhigh
```

Assertions:

- all exact advertised values are accepted;
- an unadvertised value is rejected/filtered according to policy;
- no budget or toggle support is invented.

### H4 — Effort with `none`

Synthetic effort contract containing `none` proves explicit disable remains effort semantics and is not silently converted to an unrelated toggle unless a verified mapping exists.

### H5 — Toggle + budget

Synthetic provider proves combined dimensions can be routed/adapted independently.

### H6 — Unknown controls

Reasoning supported with absent control metadata:

- explicit requested control follows `unknown_contract` policy;
- warning/diagnostic reports unknown;
- no effort list appears from fallback inference.

### H7 — Same model/provider divergence

Same model ID on two providers:

```text
A: toggle-only
B: effort [low, high]
```

Assert effort request excludes A and may use B; toggle request excludes B if no faithful toggle representation exists.

### H8 — Operator override

Operator override changing the exact provider/model contract immediately governs routing/adaptation without DB wipe.

### H9 — Local failure isolation

After a deterministic local capability rejection, a valid request through the same account/provider succeeds without restart, rehash, or DB reset.

### H10 — No wire-learning contamination

A local reasoning mismatch must not update `WireProfileResolver` candidate rejection state.

---

## Workstream I — Credentialed OpenCode Go closure

Extend the existing manual live OpenCode Go suite rather than adding CI or a new live harness.

Re-verify current OpenCode Go/models.dev MiniMax-M3 control metadata immediately before the live check because provider contracts can change.

Required live cases, using small prompts/output ceilings:

### I1 — MiniMax valid thinking form

Send one request through EggPool using the currently verified MiniMax binary thinking form.

Assert:

- selected provider/account remains healthy;
- request uses the selected Anthropic-compatible MiniMax wire surface;
- no `reasoning_effort=low|medium|high` is fabricated;
- the upstream accepts the request or, if the provider has changed, the sanitized observed failure is documented and metadata is re-verified before code is changed.

### I2 — MiniMax invalid effort is prevented or isolated

Exercise an OpenAI-style client request that previously produced a bad MiniMax effort mapping, for example `reasoning_effort="high"` if the public API accepts that client field.

Expected behavior under the verified toggle-only contract:

- EggPool does not claim MiniMax supports `high`;
- the request is locally rejected/filtered or handled by an explicitly configured lossy policy before an invalid provider payload is sent;
- account/provider health remains immediately usable afterward.

### I3 — Ordinary follow-up

Immediately send a normal MiniMax or sibling OpenCode Go request and prove the prior mismatch did not poison routing.

Live credentials must never enter logs, fixtures, plan closure text, or commits.

If credentials are unavailable during implementation, deterministic tests may merge, but final roadmap closure should record the live acceptance as blocked rather than falsely claiming verification.

---

## Workstream J — Documentation and historical assumption correction

Update active documentation after behavior is proven.

At minimum remove current claims equivalent to:

```text
OpenCode Go MiniMax-M3 accepts low/medium/high thinking levels
```

Replace with a general rule:

> EggPool derives reasoning controls from the selected provider/model contract. The same underlying model may be fixed, toggle-controlled, effort-controlled, budget-controlled, or expose a combination depending on the upstream.

Do not rewrite historical plan closure text. Plan 157 can be cited by active docs if necessary to explain the newer semantic model.

---

## Expected production files

Likely owners:

- `src/eggpool/routing/eligibility.py`;
- `src/eggpool/catalog/capabilities.py` request/candidate helpers;
- `src/eggpool/wire/ir.py` only if canonical reasoning intent needs a narrow extension;
- coordinator/provider thinking-control adaptation modules;
- transcoder/provider codecs only where they consume the new contract;
- API/dashboard serializers minimally as needed;
- active docs/config examples.

Do not touch health/backoff/database/failure classifier/wire resolver unless necessary to preserve existing interfaces after the capability type migration.

---

## Verification

Run focused tests for:

- capability request classification;
- routing eligibility;
- provider-bound thinking adaptation;
- OpenAI/Anthropic cross-protocol reasoning semantics;
- selected-provider local failure isolation;
- model API serialization/rendering where touched;
- wire resolver non-contamination.

Then run the ordinary lightweight gate:

```bash
uv sync --frozen --extra ci
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Run only the existing credentialed OpenCode Go live subset manually. Do not add it to CI.

The full retained suite remains optional/manual unless focused failures show a shared primitive changed more broadly than planned.

---

## Explicit acceptance criteria

- [ ] Request intent distinguishes no control, toggle, effort, and budget semantics without source-protocol string checks in routing.
- [ ] Routing uses the exact selected provider/model's compositional reasoning contract.
- [ ] Reasoning support alone never authorizes an effort, toggle, or budget control.
- [ ] Known unsupported control kinds are filtered/rejected locally under existing policy and never treated as provider outages.
- [ ] Unknown control kinds remain unknown and follow explicit unknown-control policy without fabricated capability claims.
- [ ] Toggle-only providers are not advertised or routed as effort-capable.
- [ ] Fixed/no-caller-control reasoning providers remain usable for ordinary requests but cannot falsely satisfy explicit control requests.
- [ ] Exact effort values are enforced per provider/model.
- [ ] Explicit `none` effort semantics are preserved and not automatically reinterpreted as a different control kind.
- [ ] Provider-bound adaptation encodes only verified semantic control kinds on the selected wire surface.
- [ ] No model-specific MiniMax branch is required for correct toggle behavior.
- [ ] A local reasoning-control mismatch cannot disable/quarantine/suppress an account, alter provider health, or contaminate wire-profile learning.
- [ ] MiniMax-M3 on current OpenCode Go no longer receives fabricated `low/medium/high` thinking controls.
- [ ] MiniMax mismatch followed by a valid request works without restart, rehash, or DB deletion.
- [ ] MiMo no-control and Muse exact-effort cases behave according to current provider metadata.
- [ ] Same-model/different-provider contracts route independently.
- [ ] Active API/dashboard output does not falsely display MiniMax effort levels.
- [ ] Focused tests and normal project gate pass.
- [ ] Existing credentialed OpenCode Go live test verifies current MiniMax behavior, or the closure record explicitly states why that manual check was blocked.
- [ ] No background probe system, persisted runtime reasoning-learning state, extra retry loop, new dependency, DB migration, or expanded CI apparatus is introduced.

## Closure rule

When these criteria pass, mark Plans 157–160 complete and stop. If a future provider publishes incomplete metadata and a real request demonstrates that metadata + operator override are insufficient, create a separate narrowly evidenced plan for passive runtime control learning. Do not preemptively add that complexity here.

## Implementation closure (2026-09-03)

- Deterministic and full retained suites pass locally: `7818 passed, 42 skipped`.
- The CI-equivalent format, lint, type, smoke, and standard/SBC config checks
  pass locally under `PYTHONHASHSEED=0 TZ=UTC`.
- Credentialed OpenCode Go verification is blocked in this environment because
  `EGGPOOL_E2E_OPENCODE_GO_API_KEY` is unavailable. The live suite remains
  opt-in and skipped; no live success is claimed here.
