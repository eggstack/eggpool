# Plan 159 — Reasoning Capability Source Precedence and Static-Assumption Removal

Date: 2026-09-03
Status: complete
Parent roadmap: `plans/157-provider-bound-reasoning-control-discovery-roadmap.md`
Depends on: `plans/158-compositional-reasoning-capability-schema-and-metadata-normalization.md`
Planning baseline: `df64a5e3e33964b1c811f04e2ed79e12473a3db4`
Priority: P0 correctness / stale-metadata elimination
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Make explicit provider/model metadata authoritative for reasoning controls and remove production code that invents reasoning effort capabilities from model names, compatibility defaults, or bundled OpenCode Go tables.

Plan 158 supplies the canonical compositional contract. This phase determines where its facts come from and ensures a lower-quality source cannot overwrite better provider-specific truth.

---

## Current defects to remove

### 1. Built-in OpenCode Go capability seeding

`src/eggpool/models/config.py` currently contains a bundled model table roughly equivalent to:

```text
_OPENCODE_GO_THINKING_MODELS = {
    mimo-v2.5,
    minimax-m3,
    muse-spark-1.2-contributor,
    muse-spark-1.3-contributor,
}
```

with explicit Muse effort lists and a default `low/medium/high` effort set for the other seeded models.

`_seed_builtin_provider_capabilities()` inserts those values into canonical OpenCode Go provider config. Because provider overrides are a high-authority layer, this static seed can mask fresher dynamic metadata.

The current result is concretely wrong for MiniMax-M3: current models.dev metadata for OpenCode Go declares toggle-only reasoning, but EggPool can still claim `low/medium/high` because of its built-in table.

The seed also misstates provenance as `source = "provider_catalog"` even though it is code-authored metadata.

### 2. Model-family effort inference

`src/eggpool/catalog/models_dev.py::derive_opencode_go_supported_efforts()` currently contains provider/model-name branches such as:

```text
deepseek-v4 -> low/medium/high/max
glm-5.2 -> high/max
certain families -> no synthesized efforts
default -> low/medium/high
```

This is the wrong layer for those assumptions. A provider may expose a different control surface from the model's first-party host, and the same model can have different controls on different providers.

### 3. Duplicate metadata parsing

`models_dev.py` currently parses effort options separately from `normalizer.py`. This allows the two paths to drift and makes toggle/budget support easy to lose.

### 4. Merge precedence does not express authority cleanly

Existing merge code often reasons in terms of capability status priority or non-empty values rather than evidence authority. A higher-authority explicit negative/empty declaration must be able to replace a lower-authority positive declaration.

---

## Governing source order

For one exact `(provider_id, model_id)` contract, use the following authority order from highest to lowest:

1. **Explicit operator override** — intentional local truth and escape hatch.
2. **Explicit live provider catalog/model metadata** — data returned by the provider currently configured for that account/provider.
3. **Verified provider-scoped model-info metadata** — currently models.dev where provider identity is unambiguous.
4. **Unknown**.

Do not add a model-family heuristic tier.

### Nuance: merge by fact, not entire record

A live provider catalog may say only:

```text
reasoning = true
```

while provider-scoped models.dev may provide:

```text
reasoning_options = [{type="toggle"}]
```

The live source should remain authoritative for the fact it explicitly states (`reasoning supported`) but must not erase the lower-source control fact merely because the live source omitted the control field.

Conversely, if the live provider explicitly publishes a complete `reasoning_options` list, that exact list outranks models.dev.

Therefore source precedence should be applied to independently known facts/control dimensions rather than by replacing a whole capability object whenever any higher-source field exists.

Do not expand this into a generic evidence engine. A small capability-specific merge helper is sufficient.

---

## Workstream A — Remove bundled OpenCode Go semantic seeds

Delete the production semantic tables/functions whose purpose is to assert model-specific OpenCode Go reasoning controls, including the current equivalents of:

- `_OPENCODE_GO_THINKING_MODELS`;
- `_OPENCODE_GO_THINKING_EFFORTS`;
- `_OPENCODE_GO_EFFORT_BUDGETS` when used to manufacture provider facts;
- `_default_opencode_go_thinking_capabilities()`;
- `_seed_builtin_provider_capabilities()` for reasoning capability semantics.

If `_OPENCODE_GO_BASE_URL` is used elsewhere for provider identity or model-info enrichment, retain the identity constant only where needed. Do not remove unrelated canonical-provider behavior.

Operator-provided `providers.<id>.model_capabilities` remains supported and authoritative. The removal is specifically about code silently creating those overrides on the operator's behalf.

After this workstream, a clean default config must not contain a hidden model-specific reasoning contract that outranks the catalog.

---

## Workstream B — Eliminate model-ID/family reasoning inference

Refactor or delete `derive_opencode_go_supported_efforts()` so no production path derives an effort set from substrings such as:

- `deepseek`;
- `minimax`;
- `glm`;
- `kimi`;
- `qwen`;
- `muse`;
- or a catch-all default.

The only valid reasons to return effort values are:

- the exact provider metadata enumerated them; or
- an explicit operator override enumerated them.

Do not preserve a default `OPENCODE_COMPATIBLE_EFFORTS = [low, medium, high]` as a fallback under another function name.

If a current model/provider truly has `low/medium/high`, its provider-scoped metadata should say so. If it does not, the contract remains unknown/no-control according to the source semantics.

Model IDs may still be used to match the provider's model row and to locate an external provider-scoped metadata row. That is identity resolution, not capability inference.

---

## Workstream C — Route models.dev through the canonical parser

`src/eggpool/catalog/models_dev.py` should become an enrichment/fetch/merge layer, not a second capability semantics implementation.

Preferred flow:

```text
fetch provider-scoped models.dev row
    -> merge raw source metadata as appropriate
    -> canonical capability parser from Plan 158
    -> provider/model capability merge using explicit source authority
```

Remove duplicate effort-option parsing after the shared parser is available.

Required behavior for the current OpenCode Go provider rows:

### MiniMax-M3

Current models.dev source:

```toml
reasoning = true
reasoning_options = [{ type = "toggle" }]
```

EggPool result:

```text
reasoning = supported
toggle = supported
effort = unsupported
budget = unsupported
```

### MiMo V2.5

Current source:

```toml
reasoning = true
reasoning_options = []
```

EggPool result:

```text
reasoning = supported
toggle = unsupported
effort = unsupported
budget = unsupported
```

### Muse Spark 1.3 Contributor

Current source:

```toml
reasoning = true
reasoning_options = [{
    type = "effort",
    values = ["minimal", "low", "medium", "high", "xhigh"]
}]
```

EggPool result preserves exactly that effort set.

Do not add special branches for these IDs; they are regression fixtures for the generic parser.

---

## Workstream D — Verify provider identity mapping for model-info enrichment

The current model-info enrichment uses a canonical models.dev provider ID for OpenCode Go. Re-verify the provider ID and endpoint mapping at implementation time against the current models.dev API/repository rather than copying assumptions from historical code.

Requirements:

- external provider metadata must be joined only when EggPool can unambiguously identify the configured upstream/provider as that models.dev provider;
- canonical OpenCode Go may retain a narrow explicit provider-ID mapping because its base URL is known;
- do not infer a models.dev provider ID from arbitrary hostnames with fuzzy matching;
- if a provider has no known model-info mapping, continue using its live catalog/operator override and leave missing facts unknown;
- a model-info fetch failure remains non-fatal and must not affect request routing health.

Do not generalize this phase into a provider registry project. Support the mappings EggPool already owns and make them correct.

---

## Workstream E — Correct capability provenance

Use source labels truthfully.

At minimum:

- provider `/models` or equivalent explicit upstream metadata -> `provider_catalog`;
- models.dev/model-info enrichment -> `model_info`;
- operator config -> `manual_override`;
- no inferred facts should remain with `heuristic` for reasoning controls after this phase;
- aggregate/collapsed summaries -> existing aggregate provenance where appropriate.

If `CapabilitySource = "heuristic"` remains useful to another capability family, do not remove the enum value globally. The acceptance criterion is that reasoning-control support no longer depends on it.

Do not label transformed/guessed compatibility data as provider-published metadata.

---

## Workstream F — Implement field-level authority without overengineering

Use the smallest mechanism that makes explicit negative facts authoritative.

For each of these facts:

- reasoning status;
- toggle support;
- effort support + values;
- budget support + bounds;
- verified effort aliases/maps;
- wire/request field hints where already represented;

merge according to source authority only when the source explicitly knows the fact.

Examples:

### Lower models.dev fact fills live omission

```text
live: reasoning=supported, effort=unknown
models.dev: effort=supported [minimal, low, medium, high, xhigh]
result: reasoning=supported, effort=supported [exact values]
```

### Live explicit list clears external data

```text
live: reasoning_options=[] (complete)
models.dev: effort=supported [low, medium, high]
result: controls unsupported
```

### Operator override wins

```text
operator: toggle=supported
live: toggle=unsupported
result: toggle=supported, source manual_override
```

This may be implemented with existing merge helpers plus an explicit source-rank/knownness function. Do not introduce event sourcing, confidence scores, timestamps per field, or a generic rule engine.

---

## Workstream G — Stale cache/config behavior

Removing built-in seeds must take effect without requiring the user to wipe EggPool's database.

Audit where capability blobs are persisted and how provider/model entries are refreshed.

Required behavior:

- static config seed removal must immediately stop overriding catalog entries after normal config load/rehash;
- cached model-info/provider catalog data should be reparsed through the new schema when refreshed;
- if an old serialized capability carries legacy effort facts, the compatibility decoder from Plan 158 must not let those lower-authority stale values survive a newer explicit toggle/no-control declaration;
- no manual DB deletion may be required for MiniMax-M3 to become toggle-only;
- no SQL migration is preferred; use safe decode/refresh/invalidation unless the existing persistence format makes a tiny migration unavoidable.

If a migration unexpectedly becomes necessary, stop and document why before broadening scope. The default expectation is no migration.

---

## Workstream H — Active documentation/config cleanup

Update active statements that present hard-coded OpenCode Go reasoning contracts as project truth.

Likely locations:

- `AGENTS.md` gotchas describing bundled OpenCode Go thinking metadata;
- `config.example.toml` comments/examples;
- architecture/provider docs that imply all reasoning models accept effort controls;
- current tests/fixtures named around built-in OpenCode reasoning defaults.

Do not rewrite historical completed plan files. Add a note only if a historical plan is actively referenced as current guidance and would otherwise mislead implementation.

Documentation invariant:

> EggPool discovers reasoning support and caller controls per provider/model. It does not infer control levels from model family names. Operator overrides may explicitly replace discovered facts.

---

## Expected production files

Likely owners:

- `src/eggpool/models/config.py`;
- `src/eggpool/catalog/models_dev.py`;
- `src/eggpool/catalog/service.py`;
- `src/eggpool/catalog/capabilities.py` merge helpers;
- catalog/cache serialization only if needed for stale legacy values;
- active docs/config examples.

Do not change wire resolver or failure classifier in this phase.

---

## Focused regression coverage

Use existing catalog/model-info/config tests.

Required cases:

### No built-in MiniMax effort contract

Load the canonical OpenCode Go provider with no operator capability override. Assert config construction itself does not synthesize MiniMax `low/medium/high`.

### Current models.dev MiniMax row

Feed a current-shaped toggle-only row and assert exact toggle-only contract.

### Current models.dev MiMo row

Feed an explicit empty options row and assert no caller controls.

### Current models.dev Muse row

Assert exact effort list including `xhigh`, with no family-default branch.

### No catch-all default

Use a new synthetic reasoning model ID not present in any table with `reasoning=true` but no control metadata. Assert control dimensions remain unknown rather than becoming low/medium/high.

### Same model, different providers

Provider A metadata exposes toggle; Provider B metadata exposes effort. Assert provider-specific cache entries retain different contracts and collapsed data does not overwrite them.

### Explicit negative clearing

Lower-authority model-info effort values followed by a higher-authority live explicit empty list must clear effort support.

### Operator override precedence

An explicit provider/model operator override wins over both live and model-info metadata.

### Model-info outage

Failure to fetch models.dev leaves live/operator capability facts usable and does not suppress the provider/account.

### Rehash/no DB wipe

Where practical, simulate config/catalog refresh proving removal of built-in seed changes effective capability without deleting persistence.

Do not add internet-dependent unit tests. Mock/provider fixtures should contain only the metadata needed for semantics.

---

## Verification

Run focused model-info/catalog/config tests and the ordinary lightweight gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No live provider request is required until Plan 160.

---

## Explicit acceptance criteria

- [ ] Canonical OpenCode Go config no longer silently seeds model-specific reasoning effort capabilities.
- [ ] MiniMax-M3 is not assigned `low/medium/high` by default anywhere in production code.
- [ ] `derive_opencode_go_supported_efforts()` family/default inference is deleted or reduced to explicit metadata extraction only.
- [ ] No reasoning-control production path infers efforts from model-name substrings.
- [ ] models.dev reasoning metadata is parsed by the same canonical parser used for recognized provider metadata.
- [ ] Current OpenCode Go MiniMax metadata yields toggle-only.
- [ ] Current OpenCode Go MiMo metadata yields reasoning-supported/no-caller-control.
- [ ] Current OpenCode Go Muse Spark 1.3 metadata yields its exact advertised effort list.
- [ ] Explicit live provider control metadata outranks model-info for the facts it explicitly declares.
- [ ] Model-info fills only facts omitted/unknown by higher-authority sources.
- [ ] Explicit operator override remains highest authority.
- [ ] Explicit negative/empty higher-authority facts clear stale lower-authority positive values.
- [ ] Capability provenance distinguishes provider catalog, model-info, and manual override truthfully.
- [ ] Default behavior requires no DB wipe/restart cycle beyond normal reload/refresh semantics to shed the old MiniMax effort assumption.
- [ ] Active docs/config no longer present built-in model-family effort tables as authoritative behavior.
- [ ] No background probing, generic evidence engine, new dependency, live CI, or expanded provider registry is introduced.

## Handoff note

If a provider publishes incomplete reasoning metadata, leave the missing dimension `unknown`. Do not reintroduce a heuristic to make a test or dashboard look complete. Unknown is a valid and safer state.
