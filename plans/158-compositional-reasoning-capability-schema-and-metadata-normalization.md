# Plan 158 — Compositional Reasoning Capability Schema and Metadata Normalization

Date: 2026-09-03
Status: complete
Parent roadmap: `plans/157-provider-bound-reasoning-control-discovery-roadmap.md`
Depends on planning baseline: `df64a5e3e33964b1c811f04e2ed79e12473a3db4`
Priority: P0 semantic correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Replace the current single-mode thinking-control abstraction with one canonical compositional provider/model contract, then teach catalog normalization to preserve the complete meaning of explicit reasoning metadata.

This phase establishes representation and parsing only. Do not yet broaden routing behavior or add runtime capability learning. Plan 159 removes stale/static sources after the new representation exists; Plan 160 updates routing/adaptation and closes the behavior matrix.

---

## Current defect

At the planning baseline, `src/eggpool/catalog/capabilities.py` represents control shape with:

```text
unknown | none | fixed | effort | budget | effort_or_budget
```

This creates three problems.

First, it cannot naturally represent valid combinations such as toggle-only, toggle+effort, toggle+budget, or toggle+effort+budget without growing the enum.

Second, legacy fields duplicate the same facts in multiple forms:

- `ThinkingCapability.supported_efforts`;
- `ThinkingCapability.budget_tokens_min/max`;
- `ThinkingCapability.effort_to_budget_tokens`;
- `ThinkingCapability.control_contract`.

Merge code can therefore preserve stale values when a higher-authority source explicitly removes a control.

Third, `src/eggpool/catalog/normalizer.py::_extract_efforts_from_reasoning_options()` only reads `type = "effort"`. It silently ignores `toggle` and `budget_tokens`, even though current provider metadata uses all three.

A concrete failure follows directly:

```text
OpenCode Go / MiniMax-M3 metadata:
reasoning = true
reasoning_options = [{ type = "toggle" }]

current EggPool extraction:
reasoning supported
supported_efforts = []
no representation of toggle
```

Later code can then fill the missing control facts with incorrect built-in `low/medium/high` values.

---

## Governing design

### Separate feature support from caller-control support

Keep the existing top-level `ThinkingCapability.status` or an equivalent capability status for whether reasoning itself exists:

```text
supported | unsupported | unknown
```

Collapsed/aggregate entries may still use `mixed/conflicting` where the existing UI/catalog requires them, but provider-bound entries should resolve to supported/unsupported/unknown.

Replace `ThinkingControlContract.mode` with independent control dimensions. Prefer extending the existing `ThinkingControlContract` rather than inventing a second reasoning type hierarchy.

A minimal target shape is:

```python
ControlSupport = Literal["supported", "unsupported", "unknown"]

class ThinkingControlContract(BaseModel):
    toggle: ControlSupport = "unknown"
    effort: ControlSupport = "unknown"
    budget: ControlSupport = "unknown"

    accepted_efforts: list[str] = Field(default_factory=list)
    effort_aliases: dict[str, str] = Field(default_factory=dict)
    effort_to_budget_tokens: dict[str, int] | None = None
    explicit_budget_min: int | None = None
    explicit_budget_max: int | None = None

    request_fields: list[str] = Field(default_factory=list)
    historical_reasoning_content: ...
    source: CapabilitySource = "unknown"
```

The exact field names may vary if current call sites make another spelling substantially cleaner, but the semantics must remain independent dimensions rather than a combination enum.

Do not add nested classes merely for architectural purity unless validation genuinely requires them. This should remain a compact Pydantic model suitable for SBC/local deployments.

### Derived labels are allowed; derived truth is not

If dashboards/logging benefit from a human-readable summary such as:

```text
fixed
toggle
effort
toggle+budget
```

provide a computed helper/property. Do not persist or route on that derived label.

`reasoning supported + toggle/effort/budget all explicitly unsupported` is the canonical representation for provider-controlled/fixed/no-caller-control reasoning.

`reasoning unsupported` remains different from fixed/no-caller-control reasoning.

---

## Workstream A — Inventory and collapse duplicate truth

Before editing, locate all production readers/writers of:

- `ThinkingControlContract.mode`;
- `ThinkingCapability.supported_efforts`;
- `ThinkingCapability.budget_tokens_min`;
- `ThinkingCapability.budget_tokens_max`;
- `ThinkingCapability.effort_to_budget_tokens`;
- `infer_control_contract()`;
- `candidate_supports_requested_effort()`;
- `adapt_thinking_controls()` and provider-control policy code;
- config conversion helpers for thinking capability overrides;
- serialization/deserialization helpers `dict_to_model_capabilities()` and `model_capabilities_to_dict()`;
- collapsed capability aggregation and dashboard/API output.

Classify each legacy field as:

1. canonical production input;
2. compatibility/config input that should normalize into the new contract;
3. derived output for compatibility/UI;
4. redundant internal state that should be deleted.

By the end of this roadmap there must be one production source of truth for control support. Avoid a transition in which routing reads the new contract while transcoding still mutates legacy fields independently.

Preferred disposition:

- canonical internal truth lives in `ThinkingControlContract`;
- existing operator config keys such as `supported_efforts`, budget bounds, and effort maps remain accepted for backward compatibility and normalize into that contract;
- old persisted/catalog dictionaries are tolerated through a bounded legacy decoder;
- redundant `ThinkingCapability` control fields are removed if doing so does not require a database migration;
- if fields must remain serialized temporarily for API compatibility, generate them from the canonical contract rather than merging them independently.

Do not add a SQL migration solely for this type change. Catalog capability blobs should be backward-decoded or safely refreshed.

---

## Workstream B — Define exact metadata completeness semantics

The parser must preserve whether metadata is absent or explicitly complete.

For provider metadata using the recognized `reasoning_options` field:

### Field absent

```text
reasoning = true
reasoning_options key absent
```

Result:

```text
reasoning.status = supported
toggle = unknown
effort = unknown
budget = unknown
```

Do not infer fixed behavior. Do not infer low/medium/high.

### Explicit empty list

```text
reasoning = true
reasoning_options = []
```

For a source whose schema defines this list as the complete host control set, including models.dev, result:

```text
reasoning.status = supported
toggle = unsupported
effort = unsupported
budget = unsupported
```

This represents reasoning with no caller control.

Current models.dev guidance explicitly states that an empty provider `reasoning_options` list means no caller control, not uncertainty. Re-verify this contract at implementation time before relying on it.

### Toggle only

```text
reasoning_options = [{"type": "toggle"}]
```

Result:

```text
toggle = supported
effort = unsupported
budget = unsupported
accepted_efforts = []
```

This is the required OpenCode Go MiniMax-M3 shape at the current metadata revision.

### Effort only

```text
reasoning_options = [
  {"type": "effort", "values": ["none", "low", "medium", "high"]}
]
```

Result:

```text
toggle = unsupported
effort = supported
budget = unsupported
accepted_efforts = exact normalized values
```

Do not add a separate toggle merely because `none` disables reasoning. The caller selects disable through the effort control itself.

### Budget only

```text
reasoning_options = [{"type": "budget_tokens"}]
```

Result:

```text
toggle = unsupported
effort = unsupported
budget = supported
```

Populate min/max only if the same authoritative metadata provides actual reasoning-budget bounds. Do not reuse generic output-token limits.

### Combinations

When multiple option records are present, independently mark each corresponding control supported. Unlisted control kinds are unsupported when the source declares the list complete.

Examples:

```text
[toggle, effort] -> toggle supported; effort supported; budget unsupported
[toggle, budget_tokens] -> toggle supported; effort unsupported; budget supported
```

### `reasoning = false`

Result:

```text
reasoning.status = unsupported
toggle = unsupported
effort = unsupported
budget = unsupported
```

Any contradictory positive control metadata should be treated as malformed/conflicting metadata, not silently merged into support.

---

## Workstream C — Build one shared `reasoning_options` parser

Replace the effort-only helper with a shared parser owned by the catalog capability layer or normalizer.

Required recognized types:

- `toggle`;
- `effort`;
- `budget_tokens`.

Parser behavior:

- ignore malformed option rows individually rather than failing the entire `/models` response;
- normalize effort values deterministically (`med -> medium` only if this remains an intentional EggPool compatibility alias);
- preserve unknown future option types in bounded source metadata if already retained, but do not authorize them;
- do not infer an existing known control from an unknown option type;
- deduplicate effort values without reordering their first appearance;
- do not assign numeric effort budgets merely because an effort label is recognized;
- distinguish `reasoning_options` absent from present-but-empty;
- accept a `complete`/source-semantic input if needed so only schemas known to define a complete control list turn omitted option kinds into `unsupported`.

Do not keep a second copy of reasoning-option parsing in `models_dev.py`. Plan 159 will route models.dev metadata through this parser.

---

## Workstream D — Remove generic effort-to-budget fabrication from discovery

`src/eggpool/catalog/normalizer.py` currently contains `_EFFORT_BUDGET_DEFAULTS` mapping labels such as `low`, `medium`, `high`, `xhigh`, and `max` to token budgets during capability extraction.

Capability discovery must stop treating those values as provider facts.

Rules:

- discovering an effort label proves only that the provider accepts that label;
- `effort_to_budget_tokens` may be populated only from an explicit verified provider/model mapping or operator override;
- cross-protocol fallback policy from Plan 123, if still retained, belongs in translation policy and must be clearly labeled compatibility behavior, not provider metadata;
- discovered source provenance must never imply the provider published a numeric budget when it did not.

Remove `_EFFORT_BUDGET_DEFAULTS` from catalog discovery if it has no remaining legitimate use there.

---

## Workstream E — Make merge semantics capable of clearing stale controls

The current merge pattern favors non-empty values, which is unsafe for explicit negative capability facts.

Required semantic merge behavior per control dimension:

```text
higher-authority supported   -> wins
higher-authority unsupported -> wins and clears lower-authority values/bounds/maps
higher-authority unknown     -> does not erase a lower-authority known fact unless the entire source is explicitly authoritative-reset by operator intent
```

Specific requirements:

- `effort = unsupported` clears `accepted_efforts`, effort aliases that only belong to that provider contract, and provider-derived effort mappings;
- `budget = unsupported` clears provider-derived budget min/max;
- `toggle = unsupported` removes no unrelated effort/budget controls;
- an explicit empty `reasoning_options` list clears all lower-authority control support;
- status `unsupported` clears all controls;
- a later operator override may intentionally replace any of these facts.

Do not use generic `supported > unsupported` status priority to resolve contradictory facts from different authority layers. Authority/source precedence belongs in Plan 159; this phase should expose merge primitives that can apply the higher-authority value deterministically.

---

## Workstream F — Backward-compatible config and catalog decoding

Preserve the existing operator-facing config surface wherever practical.

At minimum:

- existing `supported_efforts = [...]` means `effort = supported` with that exact set;
- an explicitly configured empty effort list should be handled deliberately rather than treated as omitted;
- existing budget min/max or explicit effort-to-budget maps normalize into budget/effort contract facts without inventing missing support;
- `status = unsupported` normalizes all control kinds to unsupported;
- current config with no new control fields remains valid.

Add the smallest new override surface needed to express a binary toggle or explicit no-control contract. Prefer one compact nested control block or a few clearly named optional fields; do not create a provider semantic DSL.

If old serialized capability dictionaries contain `control_contract.mode`, provide a bounded conversion:

```text
none              -> reasoning unsupported / all controls unsupported where status agrees
fixed             -> reasoning supported / all caller controls unsupported
effort            -> effort supported
budget            -> budget supported
effort_or_budget  -> effort + budget supported
unknown           -> all control dimensions unknown
```

Historical `mode` conversion is compatibility input only. Do not serialize new provider truth back into the old enum as the canonical representation.

---

## Workstream G — Aggregate/collapsed model behavior

Provider-bound entries are authoritative for routing. Collapsed entries are presentation/best-effort summaries only.

Update aggregation so control dimensions can express divergence across providers without erasing it.

Examples:

```text
provider A: toggle supported
provider B: effort supported
collapsed: reasoning supported; controls mixed/summary only
```

Do not make a collapsed union such as `toggle + effort` look as though one provider supports both. Existing routing must continue to consult exact provider entries.

If the public model API cannot represent mixed per-control states compactly without broad schema churn, preserve provider-specific detail in existing provider entries and keep collapsed control summary conservative. Do not expand this phase into a dashboard redesign.

---

## Expected production files

Likely owners include:

- `src/eggpool/catalog/capabilities.py`;
- `src/eggpool/catalog/normalizer.py`;
- `src/eggpool/models/config.py` for compatibility decoding/override schema only;
- catalog serialization/cache helpers if needed;
- existing API/model serialization code only where the internal type change requires it.

Do not edit routing/coordinator/provider adaptation beyond compile-level migrations in this phase; semantic routing changes belong to Plan 160.

---

## Focused regression coverage

Use existing catalog/capability/config suites.

Required cases:

### Metadata absence versus empty list

Prove these differ:

```text
reasoning=true, reasoning_options absent
  -> controls unknown

reasoning=true, reasoning_options=[]
  -> controls explicitly unsupported for an authoritative complete-list source
```

### Toggle-only MiniMax shape

Synthetic/current OpenCode Go-shaped metadata:

```text
reasoning=true
reasoning_options=[{type="toggle"}]
```

Assert:

- reasoning supported;
- toggle supported;
- effort unsupported;
- budget unsupported;
- no accepted efforts;
- no invented budget map.

### Fixed/no-control MiMo shape

```text
reasoning=true
reasoning_options=[]
```

Assert reasoning supported and all control kinds unsupported.

### Exact Muse effort shape

```text
reasoning_options=[
  {type="effort", values=["minimal","low","medium","high","xhigh"]}
]
```

Assert exact effort set and no fabricated toggle/budget.

### Toggle + budget

Synthetic provider metadata with both types must retain both independently.

### Effort containing `none`

Prove `none` remains an effort value and does not synthesize a separate toggle.

### Contradiction

`reasoning=false` plus a positive control option must not produce a normal supported contract.

### Merge clearing

Start with lower-authority effort support and merge a higher-authority explicit no-control contract. Assert stale efforts and mappings are removed.

### Legacy decode

Decode representative existing `mode=fixed`, `mode=effort`, and `mode=effort_or_budget` shapes into the new canonical representation.

### Config compatibility

Existing example config and representative current override syntax continue to validate.

Do not create one test per combinatorial permutation. Cover each semantic dimension and one combination.

---

## Verification

Run focused catalog/capability/config tests, then the normal lightweight gate if the phase is implemented as an independently mergeable commit:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No live-provider request is required in this phase.

---

## Explicit acceptance criteria

- [ ] `ThinkingControlContract.mode` is no longer the canonical production representation.
- [ ] Toggle, effort, and budget support are independently representable as supported/unsupported/unknown.
- [ ] Reasoning support remains independent of caller-control support.
- [ ] Explicit no-control/fixed reasoning is representable without overloading an empty effort list.
- [ ] `reasoning_options` parsing supports `toggle`, `effort`, and `budget_tokens` through one shared parser.
- [ ] Absent `reasoning_options` remains distinct from explicit `reasoning_options = []`.
- [ ] Exact effort values are preserved without generic effort-to-budget fabrication.
- [ ] Higher-authority explicit negative/empty control facts can clear lower-authority positive data.
- [ ] Existing operator config remains backward-compatible or receives a narrowly documented compatibility conversion.
- [ ] Existing cached/serialized legacy capability shapes can be decoded or safely refreshed without a SQL migration.
- [ ] Collapsed capability summaries cannot make a union of different providers appear to be one provider's contract.
- [ ] Focused tests cover MiniMax toggle-only, MiMo no-control, Muse exact efforts, empty-vs-absent, combination, merge clearing, and legacy decode.
- [ ] No runtime probing, new dependency, DB migration, or new CI apparatus is introduced.

## Handoff note

Do not "fix" MiniMax in this phase by adding a MiniMax-specific branch. The MiniMax regression should pass because the generic metadata representation finally understands `toggle`.
