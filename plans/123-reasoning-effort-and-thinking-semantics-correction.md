# Plan 123 — Reasoning-Effort and Thinking-Semantics Correction

Date: 2026-08-14
Status: complete
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Priority: P0 correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Correct a current semantic defect in OpenAI → Anthropic thinking translation.

`src/eggpool/transcoder/budget_resolver.py` currently treats only `low`,
`medium`, and `high` as known effort names and uses fixed fallback token budgets.
Unknown effort names in lenient mode fall back to the medium-like 4096-token
budget. Current OpenAI reasoning-capable models may expose additional effort
values such as `none`, `xhigh`, or `max`, and future models/providers may add
others. Passing those values through the current fallback can silently change
client intent—for example, a request asking for no reasoning can become an
Anthropic thinking request.

The fix must make capability facts authoritative and avoid inventing semantics
when OpenAI effort labels cannot be faithfully represented by an Anthropic token
budget.

## Governing constraints

1. Verify current OpenAI and Anthropic semantics against **official provider
   documentation at implementation time**. Do not rely solely on this plan's
   examples because provider enums evolve.
2. Preserve the existing model/provider capability system. Do not introduce a
   second reasoning-capability registry.
3. `ThinkingCapability.effort_to_budget_tokens` and verified provider/model
   overrides remain the authoritative source for nontrivial effort→budget
   mappings.
4. Never silently map an unknown effort label to an arbitrary token budget.
5. `none` or any verified disable-reasoning value must not enable Anthropic
   thinking.
6. Do not guess numeric mappings for `xhigh`, `max`, or future values unless a
   current verified target capability explicitly supplies them.
7. Preserve existing strict/lenient policy intent, but redefine lenient behavior
   where necessary so it means "preserve request safely with explicit loss"—not
   "invent medium reasoning".
8. Local preparation/capability failures must not penalize provider/account
   health or trigger retry to another account as though the upstream failed.
9. Preserve native same-protocol behavior.
10. No new dependency, database migration, CI job, network call on the request
    hot path, or general provider-semantics framework.

## Workstream A — Inventory current reasoning control paths

Before editing, inspect production callers and tests for:

- `resolve_thinking_budget()`;
- `_KNOWN_EFFORTS` and hard-coded fallback maps;
- `OpenAIToAnthropic.encode_request()` reasoning-effort handling;
- post-selection thinking adaptation in `RequestCoordinator`;
- `ThinkingRequestRequirement` and routing eligibility;
- `ThinkingCapability.effort_to_budget_tokens`;
- provider/model capability overrides and built-in contracts;
- `budget_resolution_policy` strict/lenient behavior;
- loss-policy warning/rejection plumbing;
- streaming/non-streaming response reasoning fields.

Answer before implementation:

1. Which path handles OpenAI client `reasoning_effort` during preflight?
2. Which path may recompute/adapt controls after account selection?
3. Can the same effort value be resolved twice with different capability facts?
4. Is routing eligibility evaluated before a faithful target mapping is known?
5. Which current providers/models ship verified effort maps?

Do not commit a permanent inventory artifact. Add only the final relevant
findings to this plan's closure record.

## Workstream B — Define truthful semantic categories

Represent source effort intent using existing capability/request-intent types
where possible. The implementation should distinguish at least these cases:

### 1. Explicit reasoning disabled

For a verified source value such as `none`:

- do not emit Anthropic `thinking` merely because the generic thinking feature
  is enabled;
- remove/omit provider thinking controls when translating to an Anthropic target;
- preserve any unrelated client fields;
- if the target/provider has a distinct verified "disabled" control, use only
  that existing verified contract;
- do not assign a token budget.

### 2. Source effort has explicit verified target mapping

When the selected capability has `effort_to_budget_tokens[effort]`:

- resolve using that value;
- apply existing min/max validation/clamping policy;
- preserve existing warning/reject behavior for clamping;
- record provenance as capability mapping.

### 3. Legacy well-known fallback values

For `low`/`medium`/`high`, retain existing global-default/fallback behavior only
if it remains consistent with current documented project policy. The fallback is
a compatibility convenience, not evidence that every provider shares identical
semantics.

Do not broaden the hard-coded table merely because OpenAI adds new labels.

### 4. Source effort is valid upstream but target mapping is unknown

Examples may include `xhigh`, `max`, or future values.

Required behavior:

- never silently substitute medium;
- if loss policy/strict policy says reject, return a local capability/loss error;
- if lenient policy allows lossy translation, choose a conservative behavior
  that does not falsely claim equivalence. Preferred order:
  1. provider capability mapping if known;
  2. explicit configured global mapping if project config supports arbitrary
     effort keys safely;
  3. omit unsupported target control and emit structured bounded loss warning;
- do not fabricate a target budget from lexical ordering or guessed ratios.

If current config validation only permits low/medium/high keys, decide whether a
small widening to arbitrary non-empty effort keys is useful. Widen only if it
reuses the same mapping structure and validation; do not create a provider
semantic DSL.

### 5. Source effort itself is invalid/unknown to current OpenAI contract

Treat this as client/capability validation according to current project policy.
Do not conflate "new but valid current OpenAI value" with "arbitrary typo" if
execution-time official docs can distinguish them.

## Workstream C — Keep routing and post-selection adaptation consistent

The same client request must not pass preflight under one semantic assumption and
then acquire a different meaning after account selection.

Ensure:

- `ThinkingRequestRequirement` can represent disabled reasoning separately from
  "thinking required" if needed;
- accounts are not excluded merely because a request explicitly disables
  reasoning;
- a transcode candidate requiring an unknown target mapping is either excluded
  before dispatch or locally rejected according to policy;
- post-selection normalization uses the exact selected provider/model capability
  rather than the generic fallback when that capability is available;
- a local mapping failure produces no provider health penalty, cooldown,
  quarantine, or retry-as-upstream-failure;
- native OpenAI→OpenAI requests retain the client's supported effort value
  unchanged unless a verified selected-provider contract requires adaptation.

Prefer a small extension to existing intent/capability types over string checks
scattered in router/coordinator/transcoder code.

## Workstream D — Warning/error semantics

Loss diagnostics must be bounded and content-free.

For unsupported/unrepresentable effort translation, use or extend existing
structured warnings/errors with fields like:

- `kind`;
- `field = "reasoning_effort"`;
- source effort label;
- provider/model identifier where already safe/available;
- reason such as `target_mapping_unknown` or `reasoning_disabled`.

Do not log request text, tool arguments, credentials, cache keys, or provider
response bodies.

Do not create new persisted observability tables for this correction.

## Workstream E — Focused regression coverage

Use existing transcode/capability/routing suites. Do not create plan-numbered test
files.

Required cases:

### `none`

OpenAI client → Anthropic target:

- output contains no enabled Anthropic thinking block solely due to source
  `reasoning_effort="none"`;
- no fallback budget appears;
- ordinary message/tool content remains unchanged;
- request routes to an otherwise eligible account;
- local normalization does not penalize provider health.

### `xhigh`/`max` with explicit capability mapping

For a synthetic verified capability mapping:

- exact mapped budget is emitted;
- min/max handling remains correct;
- provenance/warnings remain bounded;
- no global medium fallback is consulted.

### `xhigh`/`max` without mapping

Verify both configured policy behaviors:

- strict/reject: deterministic local capability/loss response before dispatch;
- lenient/warn: no invented 4096 budget; unsupported target control is omitted or
  otherwise handled according to the chosen truthful semantics with warning.

### Legacy low/medium/high

Preserve current compatible behavior and existing configured mappings.

### Native pass-through

A same-protocol OpenAI upstream that supports the current effort label receives
it unchanged when no provider-specific adaptation is required.

### Failure isolation

Immediately after a locally rejected unsupported mapping, a valid request through
the same process can route successfully without restart/database reset.

## Workstream F — Documentation/config updates

Update only active documentation that states or implies the old three-value
semantic model:

- `config.example.toml` / packaged config if needed;
- transcoder/provider documentation;
- `AGENTS.md` gotcha only if the invariant materially changes;
- architecture docs that describe capability reasoning.

Do not document guessed numeric budgets for new effort levels.

If official provider docs were consulted, record links/verification date in the
plan closure or relevant docs without copying large excerpts.

## Verification

Run focused tests for:

- budget resolver;
- OpenAI→Anthropic body translation;
- thinking-control adaptation;
- capability eligibility/routing;
- local failure isolation;
- native pass-through.

Then run ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No live-provider traffic is required for semantic correctness if deterministic
outgoing-payload tests prove the contract. One live check may be recorded if a
configured provider explicitly supports a mapped effort, but do not make it an
acceptance gate.

## Explicit acceptance criteria

- [x] Current official OpenAI/Anthropic reasoning-control semantics are checked at
  implementation time and the verification date/source is recorded.
- [x] `reasoning_effort="none"` cannot become an enabled Anthropic thinking
  budget.
- [x] Unknown/unmapped valid effort labels cannot silently fall back to 4096 or
  any other guessed medium budget.
- [x] Explicit provider/model `effort_to_budget_tokens` mappings remain
  authoritative.

## Closure record

Status: complete.

Implementation commit: `408fc194543ec3ad40cba05ac76777c1acf3f408`

Verification date: 2026-08-14.

### Provider semantics verified

The current provider documentation was checked during implementation:

- OpenAI reasoning guide: <https://developers.openai.com/api/docs/guides/reasoning>
  (verified 2026-08-14). Reasoning effort values are model-dependent; `none`
  means no reasoning, while other supported values depend on the model.
- Anthropic extended thinking guide:
  <https://platform.claude.com/docs/en/build-with-claude/extended-thinking>
  (verified 2026-08-14). Manual thinking is enabled with an explicit
  `thinking` control and `budget_tokens`; omission does not enable it.

### Inventory findings

- OpenAI `reasoning_effort` is resolved during OpenAI-to-Anthropic preflight
  using the collapsed best-effort capability. After account selection, the
  coordinator re-resolves the original client control against the selected
  provider/model capability.
- The same request can therefore encounter different capability facts at
  those two stages; the selected capability is authoritative for the final
  payload. The resolver uses original client intent rather than the interim
  translated budget, so provider-specific mappings are not lost.
- Routing classifies the request before final provider selection. Disabled-only
  `reasoning_effort="none"` is not a thinking requirement, while a request
  needing target thinking remains capability-eligible only under the existing
  routing policy. Local post-selection mapping failures remain client/local
  failures and do not penalize provider health or trigger upstream retry.
- Built-in contracts retain the legacy `low`/`medium`/`high` mappings. Catalog
  normalization and development metadata provide explicit model/provider maps
  where verified, including current `xhigh`/`max` mappings such as the
  OpenCode-compatible metadata; no new reasoning registry was added.

### Final semantic disposition

- `none` produces no budget and no enabled Anthropic thinking block. It does
  not exclude an otherwise eligible account, and native same-protocol requests
  preserve it unchanged unless an explicit provider contract adapts it.
- Explicit `effort_to_budget_tokens` mappings remain authoritative and retain
  existing min/max and strict/lenient clamping behavior.
- The legacy `low`/`medium`/`high` compatibility defaults remain available.
  `xhigh`, `max`, and future values require a verified capability or configured
  mapping; no lexical or medium-budget guess is introduced.
- An unmapped effort is rejected before dispatch under strict policy. Under
  lenient policy the target thinking control is omitted and a bounded
  `unknown_effort` warning records `field="reasoning_effort"` and
  `reason="target_mapping_unknown"`.
- No dependency, database migration, CI job, request-hot-path network call, or
  persisted observability surface was added.

### Verification evidence

Focused reasoning/transcoding/routing/provider-contract tests:

```text
493 passed in 3.33s
```

The ordinary CI-equivalent gate passed:

```text
ruff format --check src/ tests/ scripts/  -> 700 files already formatted
ruff check src/ tests/ scripts/            -> All checks passed
pyright src/ scripts/                      -> 0 errors, 0 warnings, 0 informations
pytest tests/smoke/                        -> 14 passed
check-config config.example.toml           -> passed
check-config config.sbc.example.toml       -> passed
```

A retained full-suite run reached `1075 passed, 3 skipped` and one failure in
`tests/perf/test_comprehensive_baseline.py::TestComprehensiveBaseline::test_all_metrics_baseline`.
That manual performance fixture requires a generation finalization supervisor
that its fixture does not construct; it is outside the ordinary CI smoke gate
and unrelated to this correction. No live-provider traffic was required.
- [ ] Existing low/medium/high compatibility behavior remains where intentionally
  supported.
- [ ] Native same-protocol effort values remain pass-through unless a verified
  provider contract requires adaptation.
- [ ] Strict/reject behavior rejects unrepresentable target semantics before
  upstream dispatch.
- [ ] Lenient/warn behavior does not invent semantic equivalence and emits a
  bounded structural warning when information is lost.
- [ ] Local capability/transcode rejection does not penalize provider/account
  health, trigger cooldown/quarantine, or poison later requests.
- [ ] No runtime network lookup, new dependency, DB migration, or CI expansion is
  introduced.
- [ ] Focused tests and ordinary gate pass.
- [ ] Implementation SHA, exact verification commands/results, and final mapping
  disposition are appended to this plan; no separate closure plan is created.

## Rejection conditions

Reject implementation if it:

- simply adds `xhigh`/`max` to the hard-coded table with guessed token numbers;
- maps `none` to a positive Anthropic thinking budget;
- treats arbitrary unknown strings as medium reasoning;
- adds provider-specific string branches outside the existing capability model;
- creates a new reasoning registry/config language;
- performs provider documentation/network lookups during request handling;
- turns local mapping errors into provider failures/retries;
- weakens native pass-through;
- adds broad protocol features unrelated to reasoning effort.

## Handoff sequence

1. Read Roadmap 122, this plan, `AGENTS.md`, current transcoder capability docs,
   and owning tests.
2. Verify current official provider effort/thinking semantics.
3. Inventory all resolution/adaptation callers before changing types.
4. Define disabled/mapped/unmapped/invalid semantic branches.
5. Correct resolver and selected-provider adaptation with the smallest shared
   abstraction.
6. Add focused regression cases, especially `none` and unmapped higher efforts.
7. Run focused tests and ordinary gate.
8. Update only active docs/config comments that became inaccurate.
9. Append closure evidence to this file and stop.
