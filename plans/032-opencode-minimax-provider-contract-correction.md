# OpenCode Go MiniMax-M3 Provider Contract Correction

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Implementation baseline:

- `cb7407b2114eb8aab5bc536d5b1e3b200afcaa56`

## Objective

Correct provider-bound thinking-control resolution for MiniMax-M3 so the OpenCode Go deployment is recognized from the actual selected provider identity and configured endpoint, not from a backend URL pattern that Eggpool does not use for OpenCode Go dispatch.

This plan must also separate the OpenCode Go contract from the native MiniMax contract. The same model may expose different thinking-control behavior through different providers, and one built-in rule must not shadow the other.

## Confirmed defect

Current built-in resolution identifies the OpenCode Go MiniMax-M3 contract with a base-URL pattern matching `api.minimax.io`. Eggpool's OpenCode Go default provider URL is `https://opencode.ai/zen/go/v1`. Runtime resolution reads the selected provider model entry's `base_url`, so the intended built-in contract can fail to match the actual OpenCode Go configuration.

The current native MiniMax and OpenCode Go URL patterns also overlap. Tests work around that overlap with an explicit capability override, which does not prove correct built-in provider separation.

## Scope

### In scope

- Provider-contract key structure and matching precedence.
- Passing selected provider identity into contract resolution.
- Correct OpenCode Go MiniMax-M3 built-in contract.
- Correct native MiniMax-M3 built-in contract.
- Model aliases and collapsed model IDs already supported by Eggpool.
- Native and transcoded request paths.
- Streaming and non-streaming request paths at the transform boundary.
- Focused unit and integration tests.
- Configuration compatibility and operator overrides.
- Accurate documentation of contract precedence.

### Out of scope

- Failure-effects behavior beyond proving no side effect from local rejection.
- Database recovery.
- Full ASGI/runtime harness construction; Plan 033 owns that.
- Payload pipeline refactoring; Plan 035 owns that.
- Live provider calls.
- Adding new thinking levels not accepted by a provider.
- General model capability discovery redesign.

## Required design

### 1. Key contracts by selected provider identity

Extend `ProviderContractKey` or replace it with an equivalent immutable key that can match at least:

- canonical provider ID;
- optional provider kind/family;
- model ID pattern;
- upstream protocol;
- optional base-URL fallback.

The selected attempt's provider ID is authoritative. URL matching may remain as a compatibility fallback for custom providers, but it must not be the only way to identify a built-in first-party provider contract.

Recommended fields:

```python
@dataclass(frozen=True, slots=True)
class ProviderContractKey:
    provider_id_pattern: str | None = None
    provider_kind_pattern: str | None = None
    provider_base_url_pattern: str | None = None
    model_id_pattern: str = ".*"
    protocol: str = ""
    priority: int = 0
```

The implementer may choose an equivalent typed representation. Do not use unstructured dictionary matching.

### 2. Define deterministic precedence

Required precedence:

1. Explicit operator capability override.
2. Exact/specific built-in provider-ID or provider-kind match.
3. Built-in base-URL fallback match.
4. Inferred legacy capability fields.

If multiple built-ins match at the same precedence and priority, fail closed during configuration/test construction rather than relying on declaration order.

Add a validation helper that detects ambiguous built-in rules. The validation may run at import time in tests or through a dedicated function invoked by tests; avoid adding expensive work to every request.

### 3. Use actual provider identities

Inspect current configuration and registry normalization before choosing literals. At minimum, tests must cover the canonical IDs used by Eggpool for:

- OpenCode Go;
- native MiniMax.

The OpenCode Go rule must match the default OpenCode Go URL `https://opencode.ai/zen/go/v1` and its canonical provider ID even when the provider model entry reports no backend MiniMax URL.

The native MiniMax rule must match native MiniMax configuration without requiring an explicit per-model override.

Do not infer provider identity from account names.

### 4. Pass selected identity into resolution

Update `resolve_control_contract(...)` and all production callers to provide the selected provider ID and, where available, provider kind.

The coordinator already has `SelectedAttempt.provider_id`; use it directly. Resolve provider kind from the existing provider configuration/catalog helper only if needed. Do not perform a new database query.

Expected call shape:

```python
contract = resolve_control_contract(
    capability=thinking_capability,
    provider_id=selected.provider_id,
    provider_kind=resolved_provider_kind,
    provider_base_url=provider_url,
    model_id=context.model_id,
    protocol=context.upstream_protocol or context.protocol,
)
```

All legacy callers must continue to work through defaults or be updated in the same phase.

### 5. Correct OpenCode Go behavior

For MiniMax-M3 through OpenCode Go, encode the verified deployment behavior explicitly. The prior implementation models it as fixed reasoning with no client-selectable effort or explicit budget control.

Under policy:

- `unsupported_control = "reject"`: reject locally before upstream dispatch;
- `unsupported_control = "warn_drop"`: remove only unsupported new-thinking controls and dispatch the otherwise equivalent request;
- any compatibility-retry option must not be used as the primary known-contract path;
- historical assistant reasoning content must remain preserved according to the contract.

No provider-health, model-health, quarantine, circuit, or backoff effect is allowed for the local rejection.

### 6. Preserve native MiniMax behavior

Native MiniMax-M3 must resolve its own built-in contract. It must not be forced to use an operator override merely because the OpenCode Go rule matches the same backend domain.

Tests must prove:

- same model ID;
- different selected provider IDs;
- different resulting contracts;
- expected accepted/rejected controls for each contract.

Do not assume all MiniMax endpoints accept the same controls. Encode only behavior already supported by repository configuration and prior plan intent.

### 7. Preserve operator overrides

An explicit `ThinkingControlContract` supplied through model capability configuration remains highest precedence.

Add tests proving an operator override can intentionally replace either built-in contract without mutating the global built-in registry.

## Files expected to change

Primary:

- `src/eggpool/transcoder/builtin_contracts.py`
- `src/eggpool/request/coordinator.py`
- provider configuration/catalog helper only if provider kind is not already accessible
- focused tests under `tests/unit/` and `tests/integration/`

Possible documentation:

- `architecture/README.md`
- `AGENTS.md`
- `config.example.toml` only if operator syntax changes

Do not modify failure, database-recovery, dispatch-writer, or finalization modules in this phase.

## Required tests

Create narrowly named tests, preferably:

- `tests/unit/test_plan_032_provider_contract_keying.py`
- `tests/unit/test_plan_032_builtin_contract_ambiguity.py`
- `tests/integration/test_plan_032_opencode_minimax_actual_identity.py`

### Unit matrix

Cover:

1. OpenCode Go canonical provider ID + default OpenCode Go URL + MiniMax-M3 + Anthropic upstream protocol resolves `fixed`.
2. OpenCode Go canonical provider ID still resolves correctly if URL is empty.
3. OpenCode Go URL fallback resolves only when provider identity is unavailable and the fallback is unambiguous.
4. Native MiniMax canonical provider ID resolves the native contract.
5. Native MiniMax is not shadowed by OpenCode Go.
6. Unknown provider returns inferred/unknown behavior unchanged.
7. Explicit operator override wins over built-in.
8. Collapsed and provider-qualified MiniMax-M3 IDs resolve consistently.
9. Non-MiniMax model on OpenCode Go does not inherit the MiniMax-M3 contract.
10. Wrong protocol does not match an Anthropic-only rule.
11. Two equal-priority ambiguous built-ins are rejected by validation.
12. Rule matching is case-insensitive only where current provider/model normalization is case-insensitive.

### Integration boundary matrix

Exercise the production adaptation method with a real `SelectedAttempt`-shaped object and production resolver:

- native OpenAI client request selected to OpenCode Go/Anthropic MiniMax-M3;
- Anthropic client request selected to OpenCode Go/Anthropic MiniMax-M3;
- streaming flag true and false;
- strict reject and warn-drop policy;
- subsequent plain MiniMax-M3 request;
- native MiniMax selected provider.

Assert exact emitted payload fields. Do not assert only the resolver's mode string.

## Negative tests

- An `xhigh` effort must never reach the mock OpenCode Go upstream under strict reject.
- Unsupported controls removed under warn-drop must not remove ordinary messages, tools, system prompts, model ID, temperature, or historical reasoning content.
- A local capability rejection must not invoke retry classification.
- A local capability rejection must not call `EffectsApplier`.
- OpenCode Go matching must not depend on an account being named `opencode`.
- Native MiniMax matching must not require `control_contract` in operator configuration.

## Compatibility requirements

- Existing configs without provider-control overrides remain valid.
- Existing explicit overrides retain precedence.
- Unknown custom providers retain current unknown-contract policy.
- No database migration is introduced.
- No new required configuration field is introduced.
- Provider ID aliases, if supported, must be normalized in one existing canonicalization location rather than duplicated in contract matching.

## Implementation steps

1. Inventory canonical provider IDs/kinds from existing config parsing and defaults.
2. Add provider identity fields to the contract key and resolver signature.
3. Implement deterministic match scoring/precedence.
4. Add ambiguity validation.
5. Replace the OpenCode Go MiniMax-M3 rule with an actual provider-identity rule.
6. Separate the native MiniMax rule.
7. Pass selected provider identity from the coordinator.
8. Update all resolver callers and tests.
9. Add focused unit tests.
10. Add production-boundary integration tests using the actual OpenCode Go URL.
11. Run the focused and affected existing suites.
12. Record evidence in `artifacts/plan-032-evidence.md`.

## Focused verification commands

```bash
uv run pytest \
  tests/unit/test_plan_032_provider_contract_keying.py \
  tests/unit/test_plan_032_builtin_contract_ambiguity.py \
  tests/integration/test_plan_032_opencode_minimax_actual_identity.py \
  tests/unit/test_plan_024_builtin_contracts.py \
  tests/unit/test_plan_024_provider_request_adaptation.py \
  tests/integration/test_plan_024_opencode_minimax_contract.py \
  -q --tb=short

uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
```

## Acceptance criteria

### Provider identity

- [ ] OpenCode Go MiniMax-M3 resolves from the actual canonical provider ID.
- [ ] The default `https://opencode.ai/zen/go/v1` configuration resolves the OpenCode Go contract.
- [ ] Resolution succeeds even when no MiniMax backend URL is present in catalog metadata.
- [ ] Native MiniMax resolves a distinct built-in contract without an explicit override.
- [ ] Built-in ambiguity is detected deterministically.

### Request behavior

- [ ] Strict policy rejects unsupported OpenCode Go MiniMax-M3 controls before upstream dispatch.
- [ ] Warn-drop removes only unsupported controls.
- [ ] Historical reasoning content remains intact.
- [ ] Plain follow-up requests remain valid.
- [ ] Streaming and non-streaming use the same contract decision.
- [ ] Native and transcoded paths use the same selected-provider identity.

### Side effects

- [ ] Local rejection does not call retry classification or failure-effects application.
- [ ] No health/quarantine/backoff mutation is introduced by this phase.
- [ ] No database migration or new database write is introduced.

### Compatibility and quality

- [ ] Old configurations remain valid.
- [ ] Explicit operator overrides remain highest precedence.
- [ ] Unknown providers retain configured fallback behavior.
- [ ] Focused tests pass on Python 3.11 and 3.12.
- [ ] Ruff and Pyright are clean.
- [ ] `artifacts/plan-032-evidence.md` records the exact implementation commit and focused results.

## Explicit rejection conditions

Do not mark this plan complete if:

- the OpenCode Go rule still requires an `api.minimax.io` URL;
- tests call the OpenCode Go rule with a MiniMax-native URL and label it sufficient;
- native MiniMax still requires an operator override to escape the OpenCode rule;
- contract precedence depends only on declaration order;
- the test proves only resolver output without asserting the emitted provider payload;
- the fix special-cases account names or request model strings inside the coordinator;
- unsupported controls can still reach upstream under strict policy.
