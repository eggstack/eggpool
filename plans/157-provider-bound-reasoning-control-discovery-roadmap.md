# Plan 157 — Provider-Bound Reasoning-Control Discovery Roadmap

Date: 2026-09-03
Status: complete
Planning baseline: `df64a5e3e33964b1c811f04e2ed79e12473a3db4`
Priority: P0 correctness / provider interoperability
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Replace EggPool's remaining model-family and built-in assumptions about thinking/reasoning controls with a truthful provider/model-scoped capability contract derived from explicit metadata.

The current architecture is already close to the desired shape: catalog entries are provider/model scoped, routing consults the selected provider's capability entry, models.dev enrichment exists, and wire selection is provider/model aware. The remaining defect is semantic. EggPool still partially conflates:

1. whether a provider/model deployment can produce reasoning; and
2. which caller controls that specific deployment accepts.

That conflation currently permits false capability claims. At the planning baseline:

- `ThinkingControlContract.mode` compresses control behavior into `unknown | none | fixed | effort | budget | effort_or_budget`;
- `normalizer.py` only parses `reasoning_options` entries of `type = "effort"`;
- `models_dev.py` synthesizes effort sets from model-family names when explicit values are absent;
- `models/config.py` seeds canonical OpenCode Go models with hard-coded effort sets, including `minimax-m3 -> low/medium/high`;
- those built-in provider overrides can outrank dynamically discovered metadata;
- an empty effort list is still ambiguous in some legacy code paths and may be treated as "unknown, allow" instead of an explicit statement that no effort control exists.

This is now demonstrably wrong for real provider/model combinations. Current models.dev provider metadata establishes host-specific reasoning contracts rather than a universal model-family contract. In particular, as verified on 2026-09-03:

- `providers/opencode-go/models/minimax-m3.toml` declares `reasoning = true` with `reasoning_options = [{ type = "toggle" }]`;
- `providers/opencode-go/models/mimo-v2.5.toml` declares `reasoning = true` with `reasoning_options = []`, meaning no caller reasoning control on that host;
- `providers/opencode-go/models/muse-spark-1.3-contributor.toml` declares the exact effort set `minimal, low, medium, high, xhigh`.

Relevant upstream references:

- <https://github.com/anomalyco/models.dev/blob/dev/AGENTS.md#reasoning-options>
- <https://github.com/anomalyco/models.dev/blob/dev/providers/opencode-go/models/minimax-m3.toml>
- <https://github.com/anomalyco/models.dev/blob/dev/providers/opencode-go/models/mimo-v2.5.toml>
- <https://github.com/anomalyco/models.dev/blob/dev/providers/opencode-go/models/muse-spark-1.3-contributor.toml>

models.dev's current contributor contract is especially important: `reasoning_options` is provider/host-specific; an explicit empty list means no caller control, not uncertainty; `toggle`, `effort`, and `budget_tokens` are independent control dimensions; and consumers must not invent a universal `low/medium/high` effort scale.

The implementation must align EggPool with that model.

---

## Relationship to earlier plans

This roadmap supersedes only the conflicting semantic assumptions in completed/historical thinking plans. Do not rewrite their historical status or closure evidence.

Relevant prior plans include:

- `plans/024-provider-bound-thinking-control-normalization.md` — introduced the current single-mode contract;
- `plans/032-opencode-minimax-provider-contract-correction.md` — encoded a provider-specific MiniMax contract under earlier assumptions;
- `plans/123-reasoning-effort-and-thinking-semantics-correction.md` — correctly established that effort labels must not be guessed during cross-protocol translation;
- `plans/156-wire-classifier-and-minimax-thinking-final-closure.md` — still described OpenCode Go MiniMax-M3 as accepting `low/medium/high`, which current provider metadata disproves.

Preserve the useful invariants from those plans: provider-bound truth, no guessed effort-to-budget equivalence, local capability failures do not poison account health, and wire negotiation remains separate from reasoning semantics.

---

## Target architecture

EggPool should answer two separate questions for each `(provider_id, model_id)`:

```text
Can this deployment reason?
    supported | unsupported | unknown

Which client controls can this deployment accept?
    toggle: supported | unsupported | unknown
    effort: supported | unsupported | unknown
      accepted values: exact provider values when known
    budget: supported | unsupported | unknown
      min/max: only when explicitly known
```

These dimensions are compositional. A provider may expose:

- no reasoning at all;
- reasoning with no caller control / effectively fixed behavior;
- binary on/off only;
- effort only, including `none` as one valid effort;
- budget only;
- toggle + effort;
- toggle + budget;
- effort + budget;
- all three;
- reasoning support with control details still unknown.

Do not encode those combinations as an expanding enum.

Semantic capability and wire encoding must remain separate. The capability contract says that a toggle, effort, or budget exists; the selected wire/provider encoder decides whether that becomes `reasoning_effort`, `thinking`, `thinking.type`, `enable_thinking`, `thinking_budget`, `chat_template_kwargs`, or another verified field.

---

## Source-of-truth order

Use explicit, provider-scoped facts. The intended authority order is:

1. explicit operator override for that provider/model — highest authority by design;
2. exact capability controls advertised by the live provider's own catalog/model metadata;
3. verified provider-scoped external metadata such as models.dev when EggPool can identify the provider unambiguously;
4. unknown.

Important merge rule: a higher-authority explicit negative/empty declaration must be able to clear lower-authority positive data. For example, an authoritative `reasoning_options = []` must not inherit stale `low/medium/high` values merely because merge helpers historically preferred non-empty lists.

Do not use model-name/family inference as a capability source. Model identity may be used to join provider metadata to the correct row, but not to invent control semantics.

Do not treat protocol compatibility as evidence that a provider implements every protocol-native reasoning control.

---

## Scope discipline

This roadmap is intentionally narrower than runtime feature negotiation.

In scope:

- one canonical compositional reasoning-control contract;
- parsing `toggle`, `effort`, and `budget_tokens` metadata;
- preserving the distinction between absent metadata and explicit no-control metadata;
- provider/model-scoped source precedence;
- removal of OpenCode Go built-in effort tables and family-name effort inference;
- routing eligibility based on the exact requested control, not only `thinking = supported`;
- provider-bound adaptation/encoding that never fabricates unsupported effort levels;
- backward-compatible handling of existing config/cached capability shapes where practical;
- focused regression coverage and a bounded live OpenCode Go acceptance check.

Out of scope unless implementation evidence proves it is strictly necessary:

- background capability probing;
- startup probe matrices;
- synthetic requests solely to discover capabilities;
- persisted learned capability state;
- a second resolver parallel to `WireProfileResolver`;
- fuzzy interpretation of arbitrary upstream 4xx prose;
- provider-wide static model tables;
- a new database migration solely for capability semantics;
- new dependencies;
- live-provider CI;
- large benchmark/soak infrastructure;
- broad redesign of multimodal, cache, structured-output, or tool capabilities.

If metadata remains incomplete after this roadmap, prefer `unknown` plus existing operator override/policy rather than introducing automatic probing during this workstream.

---

## Phase ordering

### Phase 1 — Plan 158: compositional capability schema and metadata normalization

Create one canonical internal representation of reasoning controls and teach catalog normalization to consume explicit `reasoning_options` completely.

Key result:

```text
reasoning=true + [{type=toggle}]
    -> reasoning supported; toggle supported; effort/budget explicitly unsupported

reasoning=true + []
    -> reasoning supported; all caller-control kinds explicitly unsupported

reasoning=true + [{type=effort, values=[...]}]
    -> reasoning supported; exact effort set; no invented toggle/budget

reasoning=true with no reasoning_options field
    -> reasoning supported; control dimensions unknown
```

The phase must also eliminate ambiguous internal representations in which an empty effort list can mean either unknown or unsupported.

### Phase 2 — Plan 159: authoritative source precedence and static-assumption removal

Remove the code paths that manufacture OpenCode-compatible effort values and ensure explicit metadata can override/clear older facts.

This phase removes:

- `_OPENCODE_GO_THINKING_MODELS`;
- `_OPENCODE_GO_THINKING_EFFORTS`;
- `_default_opencode_go_thinking_capabilities()` and equivalent built-in semantic seeds;
- family-name/default effort inference in `derive_opencode_go_supported_efforts()`;
- misleading `source = "provider_catalog"` provenance for hard-coded data.

models.dev enrichment should feed the same canonical parser used by provider metadata rather than maintaining a second effort-only interpretation.

### Phase 3 — Plan 160: routing/adaptation semantics and regression closure

Make routing and post-selection adaptation consume the compositional contract.

A request requiring a specific effort may route only to a provider/model whose effort control is known to accept that value, subject to an explicitly configured lossy policy. A toggle-only provider must not be advertised as `low/medium/high`. A fixed/no-control provider must not be treated as able to honor explicit disable or effort requests.

Close with a small provider matrix proving at least:

- OpenCode Go MiniMax-M3: toggle-only;
- OpenCode Go MiMo V2.5: reasoning supported, no caller control;
- OpenCode Go Muse Spark 1.3: exact effort set;
- unknown metadata remains unknown rather than being guessed;
- same model on two providers may legitimately expose different controls.

---

## Cross-phase invariants

1. Capability belongs to `(provider, model)` and, where relevant, selected wire surface — never to the bare model family alone.
2. `reasoning supported` does not imply `effort supported`.
3. Explicit empty/no-control metadata is a fact, not missing data.
4. Missing metadata is unknown, not unsupported and not supported-by-default.
5. No code may invent an effort list from model ID substrings.
6. No code may invent numeric effort-to-budget mappings solely from generic labels.
7. Operator overrides remain intentionally authoritative.
8. Dynamic/provider metadata must not be masked by lower-authority built-in defaults.
9. A local capability mismatch must not disable, quarantine, suppress, or cooldown an otherwise healthy account.
10. Capability rejection and wire-surface rejection remain separate failure domains.
11. No capability retry may occur after downstream response handoff.
12. Existing request ownership/freeze and wire single-flight invariants remain unchanged.
13. Existing config should continue to parse unless a field is demonstrably unsafe; compatibility aliases should normalize into the new internal contract rather than create dual truth.
14. No new CI apparatus is required.

---

## Verification strategy

Use existing behavior-oriented test modules. Do not create a parallel capability-testing framework.

Each phase should run focused owning tests first. Final closure runs the repository's normal lightweight gate:

```bash
uv sync --frozen --extra ci
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

The full retained suite remains optional/manual unless the implementation touches an unexpectedly broad shared primitive.

A credentialed OpenCode Go check belongs in the existing live test surface and remains manual, not CI.

---

## Roadmap acceptance criteria

- [ ] One canonical internal contract independently represents reasoning support, toggle support, effort support/values, and budget support/bounds.
- [ ] Explicit `reasoning_options` metadata can express toggle-only, no-control, effort-only, budget-only, and combinations without enum proliferation.
- [ ] Missing control metadata remains distinguishable from explicit no-control metadata.
- [ ] Current OpenCode Go MiniMax-M3 metadata resolves to toggle-only and never to `low/medium/high` unless an operator explicitly overrides it.
- [ ] Current OpenCode Go MiMo V2.5 metadata resolves to reasoning-supported/no-caller-control.
- [ ] Current OpenCode Go Muse Spark 1.3 metadata resolves to its exact advertised effort values.
- [ ] All model-family/default effort inference is removed from production capability determination.
- [ ] Built-in OpenCode Go semantic capability seeding no longer outranks dynamic metadata.
- [ ] Operator overrides remain the explicit highest-authority escape hatch.
- [ ] Routing evaluates the exact requested reasoning control against the exact provider/model contract.
- [ ] Known control mismatches fail locally or follow an explicit loss policy without provider-health penalties.
- [ ] No background probes, persisted learned-capability subsystem, new dependency, DB migration, or expanded CI infrastructure is introduced.
- [ ] Focused regressions plus the ordinary project gate pass.
- [ ] Active docs/config examples no longer claim that MiniMax-M3 on OpenCode Go accepts low/medium/high effort controls.

## Stop condition

Stop this workstream when Plans 158–160 satisfy the criteria above. Do not create another compatibility layer or runtime capability-learning system merely because some providers publish incomplete metadata. Record such a provider as `unknown` and use the existing operator override surface unless a concrete post-closure failure demonstrates that passive runtime learning is necessary.
