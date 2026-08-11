# Plan 105 — OpenAI/Anthropic Transcode Parity

Date: 2026-08-11
Status: implemented
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`
Depends on:

- `plans/104-local-exposure-and-log-redaction.md` only for safe observability conventions; protocol implementation may otherwise proceed independently.

## Purpose

Bring EggPool's OpenAI↔Anthropic request transcoding in line with current provider-native structured-output, strict-tool, parallel-tool, and reasoning-control semantics while keeping provider capability handling narrow and explicit.

The goal is not perfect feature equivalence between two different protocols. The goal is to use native controls when both source intent and target capability are known, and to classify genuinely unrepresentable semantics as explicit loss/rejection rather than silently approximating them through prompt text.

## Current gaps confirmed by review

### OpenAI → Anthropic

- OpenAI `response_format` / JSON-schema intent is currently represented through a system-prompt instruction when structured-output translation is enabled rather than Anthropic's native structured-output field.
- OpenAI tool `strict` is treated as unsupported/lost.
- OpenAI `parallel_tool_calls = false` is treated as unsupported/lost rather than mapping to Anthropic's parallel-tool-disable control.
- Tool translation appears active in body conversion even though `TranscoderFeatures.tools` describes tool translation as optional/default-off, so configuration contract and implementation need reconciliation.

### Anthropic → OpenAI

- Anthropic tool extensions beyond the currently recognized field set can be dropped; strict-tool semantics need an explicit mapping to OpenAI function strictness where target capability permits it.
- Anthropic parallel-tool disabling should map to OpenAI `parallel_tool_calls = false` where applicable.
- Thinking/reasoning translation is conservative and currently drops controls where no verified mapping exists. That caution is correct, but current OpenAI reasoning levels/support should be represented through capability-aware mappings rather than global assumptions.

## External-semantics rule

Provider APIs change. Before implementation, re-check only official OpenAI and Anthropic API/model documentation for:

- current structured-output field names and supported schema subset;
- strict tool/function calling fields;
- parallel-tool disabling field placement;
- reasoning/thinking controls and model support;
- whether Chat Completions and/or Messages surfaces differ from the planning snapshot.

Record the documentation date/assumption in the plan closure. Do not build a live docs scraper or capability-discovery service.

## Governing constraints

1. Preserve explicit loss-policy behavior (`ignore`/`warn`/`reject` or current equivalents).
2. Prefer native target-protocol controls over prompt injection/coercion when verified support exists.
3. Do not silently claim lossless translation when schema/control semantics differ.
4. Do not infer support solely from protocol family for arbitrary OpenAI-/Anthropic-compatible providers.
5. Extend the existing capability/static-model contract minimally; do not create a generic provider feature registry or remote capability-discovery protocol.
6. Preserve provider-bound transforms as local preparation. Local transcode failure must not penalize a provider/account or trigger provider retry.
7. Preserve streaming handoff/retry/finalization boundaries.
8. Do not change routing, quota, backoff, database, rehash, or provider client-pool architecture.
9. Do not add a new dependency solely for JSON Schema conversion.
10. Do not attempt a general JSON Schema transpiler. Support the subset actually representable by both protocols and classify the rest explicitly.
11. Never log schema contents or tool arguments merely to explain a transcode loss.

## Workstream A — Reconcile the existing feature/capability contract

Discover current configuration and call sites:

```bash
rg -n \
  'TranscoderFeatures|structured_outputs|tools|thinking|reasoning|capabilit|loss_policy|static_models' \
  src/eggpool/transcoder src/eggpool tests config*.toml architecture AGENTS.md
```

Answer before editing:

1. Does `TranscoderFeatures.tools` actually gate request-body tool translation in both directions?
2. Does it gate stream/result adaptation separately?
3. Are tool definitions/tool calls considered baseline protocol compatibility rather than optional feature translation now?
4. Where are per-provider/per-model protocol capabilities currently stored?

Choose one coherent contract:

- if tool translation is baseline required compatibility, remove/deprecate the stale optional flag cleanly and update callers/tests/docs;
- if the flag is intentionally supported, make body and streaming behavior obey it consistently.

Do not retain a configuration switch whose documented effect differs from production behavior.

## Workstream B — Minimal target capability flags

Extend existing static/provider/model capability data only as needed for these native mappings. Candidate boolean/enumerated facts:

- native structured output / JSON schema;
- strict tool/function definitions;
- parallel-tool disabling;
- supported reasoning-control family/levels.

Requirements:

- capability defaults must be conservative for generic compatible providers;
- known first-party/static-model entries may opt into verified support;
- provider/model capability is evaluated before emitting native fields;
- no database migration is required unless capabilities are already durably persisted there and there is no simpler static/catalog path; a migration solely for this plan is strongly disfavored.

Prefer extending the existing static model/capability representation over adding a second parallel registry.

## Workstream C — OpenAI structured output → Anthropic native structured output

For source requests expressing JSON-schema structured output:

1. identify the canonical OpenAI source representation accepted by EggPool;
2. normalize only enough to inspect type/name/schema/strict semantics;
3. when target capability explicitly supports Anthropic native structured output, emit the native target field rather than a system-prompt instruction;
4. preserve schema name/description only where the target field supports or needs them;
5. validate/transform only the schema constructs needed for target compatibility;
6. if the schema contains unsupported constructs, follow configured loss policy rather than silently weakening constraints.

Do not send both a native structured-output field and the old prompt-coercion instruction for the same intent.

If prompt coercion remains as a compatibility fallback for a target that lacks native support, it must be explicitly classified as lossy and only used under a policy that permits loss. Prefer rejecting/warning rather than pretending it is equivalent.

### Schema subset handling

Use a small explicit validator/normalizer for known unsupported constructs if needed. Do not introduce a general schema rewriting engine.

Tests should distinguish:

- fully representable schema;
- unsupported schema keyword/shape;
- target capability absent;
- loss-policy warn vs reject;
- already-native same-protocol passthrough not affected.

## Workstream D — Anthropic structured output → OpenAI native structured output

If Anthropic source requests can express a structured-output schema that maps to OpenAI's JSON-schema structured output:

- emit OpenAI's native structured-output representation when target capability supports it;
- preserve strictness where semantics match;
- classify schema subset differences explicitly;
- do not invent schema names/descriptions if the target requires fields the source does not have; use a deterministic minimal name only if the OpenAI protocol requires one and document that it is representational metadata, not semantic content.

If current EggPool endpoint scope does not accept Anthropic structured-output input yet, document that asymmetry rather than broadening endpoint parsing unnecessarily.

## Workstream E — Strict tool/function mapping

### OpenAI → Anthropic

Map OpenAI function/tool `strict: true|false` to Anthropic tool strictness when target capability verifies support.

Requirements:

- preserve tool name/description/input schema exactly under existing translation rules;
- do not treat `strict` as an unknown extension after the change;
- when target lacks strict-tool support, classify the semantic loss according to policy;
- strictness must not be approximated by adding prose to tool descriptions.

### Anthropic → OpenAI

Recognize Anthropic tool `strict` as a first-class known field and emit OpenAI function `strict` where supported.

Add golden round-trip fixtures where the common subset can round-trip without semantic loss.

## Workstream F — Parallel tool-call control mapping

### OpenAI → Anthropic

Map:

```text
parallel_tool_calls = false
```

into the verified Anthropic target location for disabling parallel tool use, preserving any existing `tool_choice` semantics.

Do not overwrite an existing source-derived target `tool_choice`; merge compatible fields carefully and reject contradictory source intent if necessary.

### Anthropic → OpenAI

Map Anthropic parallel-tool disabling to:

```text
parallel_tool_calls = false
```

when target capability supports it.

For `true`/unspecified cases, preserve target defaults unless source semantics require an explicit value. Avoid emitting fields merely to make payloads symmetrical.

Tests must cover interaction with tool-choice variants, including any/auto/specific tool selection if those are supported by current EggPool translation.

## Workstream G — Reasoning/thinking capability-aware translation

Inventory current budget/effort handling:

```bash
rg -n 'ThinkingBudgetDefaults|reasoning_effort|thinking|budget_tokens|xhigh|max|medium|high|low' \
  src/eggpool tests config*.toml
```

The current global low/medium/high defaults must not be blindly expanded into universal semantics for every provider.

Implement the smallest capability-aware mapping that satisfies current first-party/provider contracts:

- preserve explicit OpenAI reasoning effort values only for target models/providers known to accept them;
- support current higher efforts such as `xhigh`/`max` only where verified and without forcing them through a low/medium/high token-budget table that cannot represent them;
- map Anthropic thinking budgets/controls to OpenAI reasoning only when there is an explicitly documented semantic rule;
- otherwise retain the existing conservative loss/reject behavior.

Do not claim token-budget equivalence between Anthropic thinking budgets and OpenAI effort levels unless the provider publishes such a relationship. A qualitative capability mapping is acceptable; fabricated numeric conversion is not.

## Workstream H — Provider-bound and streaming consistency

Verify body translation, request dispatch, SSE/event adaptation, and final response adaptation all consume the same resolved capability decisions.

Requirements:

- no body path claims a feature is disabled while stream path assumes it is enabled;
- local capability rejection happens before upstream dispatch;
- capability rejection remains a client/local error and does not create provider health penalties;
- the downstream handoff boundary is unaffected;
- usage/finalization observer behavior remains unchanged unless a native protocol field materially changes response shape.

## Workstream I — Golden compatibility fixtures

Update existing capability/transcoder tests rather than adding plan-numbered suites.

Minimum fixture matrix:

1. OpenAI JSON schema → Anthropic native structured output, supported target.
2. Same source → target capability absent, warn policy.
3. Same source → target capability absent/unsupported schema, reject policy.
4. Anthropic structured output → OpenAI native representation if source endpoint supports it.
5. OpenAI strict tool → Anthropic strict tool.
6. Anthropic strict tool → OpenAI strict function.
7. OpenAI `parallel_tool_calls=false` → Anthropic parallel disable merged with tool choice.
8. Anthropic parallel disable → OpenAI `parallel_tool_calls=false`.
9. Contradictory/unrepresentable tool-choice combination produces explicit loss/rejection.
10. Generic compatible provider without capability flag does not receive unsupported native fields.
11. Known capable static model/provider does receive them.
12. Existing normal tool calls/tool results still transcode.
13. Existing streaming tool-call/result adaptation remains correct.
14. Reasoning effort unsupported target remains explicit loss/rejection, not provider failure.
15. Current verified reasoning effort values are passed only to supported targets.

Prefer compact parameterization around semantic differences; do not build a Cartesian provider/model matrix.

## Workstream J — Documentation

Update only active compatibility documentation/config comments:

- transcode feature flags if `tools` semantics change;
- capability/static-model fields introduced by this plan;
- supported structured-output/tool-control mappings;
- explicit lossy cases.

Do not promise universal feature parity for all compatible third-party providers.

## Verification

Run the directly affected existing transcoder/capability/streaming tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Also run the provider contract suites named in `AGENTS.md` if capability/static-model representation changes:

```bash
uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -q --tb=short --maxfail=1
```

Use official-provider live calls only as optional manual confidence checks. They are not required acceptance evidence and must not become CI.

## Acceptance criteria

- [ ] `TranscoderFeatures.tools` documentation/configuration and actual body/stream behavior agree; stale semantics are removed or corrected.
- [ ] OpenAI JSON-schema structured-output intent maps to Anthropic native structured-output controls when target capability is verified.
- [ ] Native structured-output translation does not also inject the old prompt-coercion instruction.
- [ ] Unsupported schema constructs or target capability absence follow explicit loss policy rather than silent weakening.
- [ ] Anthropic structured output maps to OpenAI native structured output where the accepted source surface and target capability make that mapping valid.
- [ ] OpenAI tool/function `strict` maps to Anthropic strict-tool semantics for supported targets.
- [ ] Anthropic strict-tool semantics map to OpenAI function strictness for supported targets.
- [ ] OpenAI `parallel_tool_calls=false` maps to Anthropic parallel-tool disable without clobbering compatible tool-choice intent.
- [ ] Anthropic parallel-tool disable maps to OpenAI `parallel_tool_calls=false` where supported.
- [ ] Generic compatible providers do not receive native fields solely because their protocol string is `openai` or `anthropic`.
- [ ] Capability defaults are conservative and existing known/static provider models can opt into verified support without a generic discovery framework.
- [ ] Reasoning/thinking controls are capability-aware and do not fabricate numeric equivalence across providers.
- [ ] Current verified higher OpenAI reasoning efforts are not incorrectly rejected solely because legacy defaults know only low/medium/high.
- [ ] Local transcode/capability failure remains non-retryable and never penalizes provider/account health.
- [ ] Streaming handoff, tool-call/result adaptation, usage observation, and finalization remain correct.
- [ ] Golden common-subset round trips are lossless where the protocols genuinely overlap.
- [ ] Explicit lossy cases are covered under warn/reject policies.
- [ ] No new JSON-schema library, capability service, DB migration, CI job, or generalized translation framework is added.
- [ ] Focused transcode/capability/provider contract tests pass.
- [ ] Ruff, Pyright, smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- structured output remains prompt-only for a verified native-capable Anthropic target;
- native fields are emitted to arbitrary compatible providers without capability evidence;
- unsupported schema features are silently deleted or weakened while claiming lossless translation;
- tool strictness is approximated through prompt/tool-description prose;
- parallel-tool disabling overwrites or contradicts existing tool-choice semantics without an explicit error/loss result;
- reasoning effort is converted to fabricated thinking-token budgets or vice versa;
- local transcode rejection is reclassified as provider failure/retryable error;
- a broad capability-discovery framework, schema compiler, dependency, migration, or CI expansion is introduced.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103, Plan 104 redaction conventions, this plan, `AGENTS.md`, transcode architecture, and current capability/static-model code.
2. Re-check official OpenAI and Anthropic docs and record the current field/support assumptions.
3. Reconcile `TranscoderFeatures.tools` before layering new mappings onto an inconsistent contract.
4. Add only the minimal capability facts required for native structured output, strict tools, parallel disable, and reasoning controls.
5. Implement structured-output mapping and focused schema-subset loss handling.
6. Implement strict-tool mappings in both directions.
7. Implement parallel-tool-disable mappings with tool-choice merge/contradiction tests.
8. Make reasoning control capability-aware without inventing numeric equivalence.
9. Verify body and streaming paths consume the same capability decisions.
10. Update compact golden/contract tests and active docs.
11. Run focused tests, provider contract tests where applicable, then the ordinary repository gate.
12. Record implementation SHA, external-doc assumptions/date, exact supported/lossy mappings, and verification results in this plan.
13. Stop; do not broaden into full provider feature parity.

## Closure

Implementation commit: `a462c1e`.

External API assumptions were checked against the official provider
documentation on 2026-08-11:

- OpenAI Chat Completions uses `response_format.type = "json_schema"`,
  `json_schema.strict`, function `strict`, top-level `parallel_tool_calls`,
  and model-specific `reasoning_effort` values. See the [OpenAI API
  reference](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal?lang=python).
- Anthropic Messages uses `output_config.format` with `type = "json_schema"`,
  tool-level `strict`, and `tool_choice.disable_parallel_tool_use`. Manual
  thinking uses `thinking.type = "enabled"` with `budget_tokens`; no numeric
  equivalence to OpenAI effort was assumed. See [Anthropic structured
  outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
  [strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use),
  and [extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking).

Implemented mapping and loss rules:

- `ModelCapabilities.transcoding` is the minimal static/provider/model
  capability surface. Empty fields are unknown and generic compatible
  providers receive no native fields.
- OpenAI `json_schema` → Anthropic `output_config.format` when
  `native_structured_outputs = ["anthropic"]`; otherwise the existing prompt
  fallback remains explicitly lossy. Anthropic `json_object` remains prompt
  based because it has no schema.
- Anthropic `output_config.format` → OpenAI `response_format` when
  `native_structured_outputs = ["openai"]`, with deterministic representational
  name `eggpool_structured_output`.
- OpenAI/Anthropic strict tool fields map in both directions only when
  `strict_tools` verifies the target protocol.
- OpenAI `parallel_tool_calls = false` maps to Anthropic
  `tool_choice.disable_parallel_tool_use`; the reverse maps to OpenAI
  `parallel_tool_calls = false`, both capability-gated and contradiction-aware.
- Anthropic adaptive `thinking.effort` maps to OpenAI `reasoning_effort` only
  for an explicit target effort capability. Manual Anthropic token budgets are
  not converted to fabricated OpenAI effort values; OpenAI-to-Anthropic budget
  conversion continues to use the existing target thinking contract.
- Tool translation is baseline compatibility. The old
  `[transcoder.features].tools` setting remains parseable but is a documented
  no-op.

Verification completed:

- Focused transcoder/tool/streaming/unit suites: 472 passed.
- Provider contract suite: 141 passed; transcoder contract suite: 13 passed.
- Smoke suite: 14 passed.
- `ruff format --check`, `ruff check`, and `pyright src/ scripts/`: passed.
- `config.example.toml` and `config.sbc.example.toml` check-config: passed.
- A repository-wide pytest run was started but intentionally stopped after
  reaching 6% without failures because it is substantially larger than the
  CI gate; the exact CI smoke gate and all affected suites passed.
