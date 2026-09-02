# Plan 148 — Wire-Profile Registry and Provider Contracts

Date: 2026-09-02
Status: ready after Plan 147
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f`
Priority: P0 foundational correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Create the smallest typed configuration/data model needed for EggPool to treat upstream wire surface as a first-class property independent of provider identity, model identity and the historical `ProtocolName` value.

This phase is intentionally configuration/model work. It must not yet add automatic alternate-surface retries. The goal is to give later phases one unambiguous representation of what a provider **can potentially speak**, how each surface is reached/authenticated, and which model/surface pairing should be tried first as a revocable hint.

---

# Baseline defects this plan addresses

Current `ProviderConfig` contains:

- `protocols: list[ProtocolName]`;
- `openai_path`;
- `anthropic_path`;
- `responses_path`;
- one provider-wide `auth` object plus `auth.additional` headers sent on every dispatch;
- provider-wide static headers.

Current `request/upstream_helpers.py` then derives the URL from `protocol` plus `request_surface`.

That model cannot accurately describe providers where:

- different models use different endpoints;
- two OpenAI-family surfaces require different request/stream grammars;
- different endpoints require different auth/header shapes;
- one model is accepted on more than one surface;
- the provider changes a model's preferred surface after EggPool has already cached/cataloged it.

The current OpenCode Go template demonstrates the auth problem directly: it renders both `x-api-key` and `Authorization: Bearer` on every dispatch because auth is provider-wide even though endpoint expectations differ.

---

# Primary decision — add `WireSurfaceName` and `WireProfile`

Create a new small package, preferably:

```text
src/eggpool/wire/
  __init__.py
  types.py
  registry.py
```

Do not add surface names to `catalog.protocols.ProtocolName`.

Suggested typed identity:

```python
WireSurfaceName = Literal[
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "gemini_interactions",
    "gemini_generate_content",
]
```

If the codebase already has an enum convention that is materially cleaner, use it; do not create parallel enum/string representations.

A resolved runtime `WireProfile` should be immutable and contain only structural dispatch facts, approximately:

```python
@dataclass(frozen=True, slots=True)
class WireProfile:
    surface: WireSurfaceName
    request_codec: str
    response_codec: str
    stream_codec: str
    path_template: str
    stream_path_template: str | None
    auth: ResolvedAuthShape
    headers: tuple[...]
```

Do not store account secrets in the profile object. The profile describes how an account credential is rendered; the account/secret remains owned by the existing account/config/auth machinery.

---

# Packaged developer-facing registry

Add:

```text
src/eggpool/providers/_wire_profiles.toml
```

This is packaged project data, analogous to `_templates.toml`, intended to make current surface definitions and low-authority model hints easy for maintainers to update without provider-specific Python branches.

The file must not define executable behavior. It may only reference built-in codec IDs registered in Python.

Recommended shape:

```toml
[profiles.openai_chat_completions]
request_codec = "openai_chat"
response_codec = "openai_chat"
stream_codec = "openai_chat_sse"

[profiles.openai_responses]
request_codec = "openai_responses"
response_codec = "openai_responses"
stream_codec = "openai_responses_sse"

[profiles.anthropic_messages]
request_codec = "anthropic_messages"
response_codec = "anthropic_messages"
stream_codec = "anthropic_messages_sse"

[profiles.gemini_interactions]
request_codec = "gemini_interactions"
response_codec = "gemini_interactions"
stream_codec = "gemini_interactions_sse"

[profiles.gemini_generate_content]
request_codec = "gemini_generate_content"
response_codec = "gemini_generate_content"
stream_codec = "gemini_generate_content_sse"
```

Optional bundled current model hints may live in the same file:

```toml
[[hints]]
provider_id = "opencode-go"
model_id = "muse-spark-1.2-contributor"
preferred_surface = "openai_responses"
verified_on = "2026-09-02"
source = "provider_docs"
```

Use exact model IDs initially. Do not add regex/pattern matching unless the existing catalog already exposes a simple safe matcher that can be reused. Explicit rows are easier to audit and less likely to silently capture future model names.

`verified_on` and `source` are maintenance metadata, not runtime trust guarantees.

### Registry validation

At startup/config load:

- reject duplicate profile IDs;
- reject unknown codec IDs;
- reject malformed surface names;
- reject hints referencing unknown profiles;
- reject obviously malformed model/provider IDs;
- ignore/deny unsupported extra TOML keys according to the project's normal strict config style;
- never dynamically import a class/module from a TOML string.

The Python registry should map a closed codec identifier to an implementation factory. The TOML selects among those identifiers only.

---

# Provider candidate-surface configuration

Extend `ProviderConfig` with an explicit candidate-surface map.

Preferred shape:

```python
class ProviderWireSurfaceConfig(BaseModel):
    path_template: str
    stream_path_template: str | None = None
    priority: int = 100
    auth: ProviderAuthConfig | None = None
    headers: list[ProviderHeaderConfig] = Field(default_factory=list)

class ProviderConfig(BaseModel):
    ...
    wire_surfaces: dict[WireSurfaceName, ProviderWireSurfaceConfig] = ...
```

Names may change to fit the codebase, but preserve these semantics.

### Why `path_template`

Most common surfaces use a fixed relative path, but Gemini `generateContent` encodes the model in the path:

```text
/models/{model}:generateContent
/models/{model}:streamGenerateContent
```

Support exactly the placeholder(s) EggPool needs, initially `{model}`. Do not implement general Python-format expressions, conditionals, functions, arbitrary query templates or environment interpolation.

Validation must reject unknown placeholders.

### Streaming path

`stream_path_template = None` means streaming uses the same endpoint and the request codec toggles streaming in the body/query as appropriate.

A distinct streaming template is necessary for `gemini_generate_content`.

### Per-surface auth

If `wire_surfaces.<surface>.auth` is absent, use existing provider-wide auth.

If present, render the same account credential through that surface-specific auth shape.

This solves cases such as:

```text
provider/model -> /responses -> Authorization: Bearer ...
provider/model -> /messages  -> x-api-key: ...
```

without emitting both credential headers on every request.

Do not create a new secret store or named credential subsystem. Surface auth overrides only change header rendering of the existing account credential.

### Per-surface static headers

Surface-specific headers should be additive to provider-wide non-auth static headers, with the same duplicate/proxy-managed-header validation already used by `ProviderConfig`.

A surface-specific static header must not override the selected auth header or proxy-managed headers.

---

# Candidate preference is not truth

`priority` determines the provider-level fallback order when EggPool has no stronger model-specific knowledge. Lower numeric value may mean higher preference if that matches existing project conventions; choose one rule and document/test it.

Runtime success and model hints will be layered by Plan 150.

Do not encode permanent assumptions such as:

```python
if provider_id == "opencode-go" and model_id == "muse-spark-1.2-contributor": ...
```

in URL selection or dispatch code.

---

# Optional operator model preference/fix

Provide one small operator override structure if it can be added without broad config churn:

```python
class ModelWirePreference(BaseModel):
    preferred_surface: WireSurfaceName
    fixed: bool = False
```

Example:

```toml
[providers.example.model_wire."model-x"]
preferred_surface = "openai_responses"
fixed = false
```

Semantics:

- `fixed = false`: high-authority starting preference but runtime deterministic rejection may overturn it;
- `fixed = true`: operator explicitly disables alternate-surface negotiation for this model and pins dispatch to that surface.

If adding this object materially complicates the config parser, defer the operator per-model override and retain provider-level candidate priority plus bundled hints. Do **not** compromise the main profile model to support this convenience.

---

# Backward compatibility / migration synthesis

Existing user configurations must continue to load.

When `wire_surfaces` is absent, synthesize candidate surfaces from the current fields:

```text
"openai" in protocols + openai_path
    -> openai_chat_completions

responses_path is not None
    -> openai_responses

"anthropic" in protocols + anthropic_path
    -> anthropic_messages
```

Use the provider-wide auth and headers for synthesized candidates.

This synthesis is a compatibility bridge; later plans should make dispatch consume the synthesized/resolved `wire_surfaces` representation rather than continue branching on legacy path fields.

Do not remove `openai_path`, `anthropic_path`, `responses_path` or `protocols` in this roadmap. Removing them would create unnecessary config breakage and expand scope.

### Bundled templates

Migrate bundled templates that need genuinely different per-surface auth/path behavior to explicit `wire_surfaces` blocks.

At minimum, re-check and update:

- `opencode-go`;
- `openai`;
- `anthropic`;
- `openrouter`;
- `ollama-local`;
- `llamacpp-local`;
- `vllm-local`;
- `custom-compatible` comments/examples.

Add a direct Gemini template only if EggPool already has enough catalog/auth support to make it operational in Plan 152 without creating unrelated connection UX work. Otherwise Plan 152 may add it once its codec is ready.

---

# OpenCode Go configuration disposition

This plan should correct the **configuration facts** without adding runtime special cases.

Current documented candidates under the provider base are:

```text
openai_responses       -> /responses
openai_chat_completions -> /chat/completions
anthropic_messages     -> /messages
```

Use surface-specific auth instead of rendering both auth headers universally when live verification confirms the current documented requirements.

The current Muse Spark 1.2 Contributor bundled hint must prefer `openai_responses`, not `anthropic_messages`.

MiniMax M3 current OpenCode Go hint should prefer `anthropic_messages` only if current docs/live verification still say so at implementation time.

Do not make any hint fixed.

---

# Interaction with catalog protocol metadata

This phase must **not** attempt a risky catalog schema rewrite.

Existing model `protocol` metadata can remain for:

- public model metadata compatibility;
- old routing paths during migration;
- existing capability/transcoding tests.

Add wire-surface hints beside it rather than overloading it.

Later dispatch code should stop treating `protocol` as sufficient proof of the concrete upstream endpoint.

If upstream `/models` metadata eventually provides a usable surface hint, normalize it into the same `ModelWirePreference`/hint structure with source metadata rather than introducing another path.

---

# Routing configuration for later negotiation

Define the configuration object now so later phases do not scatter constants.

Recommended small configuration under `RoutingConfig`:

```python
class WireNegotiationConfig(BaseModel):
    enabled: bool = True
    max_concurrent_per_provider: int = 1
    min_negotiation_interval_s: float = 1.0
    rejection_cooldown_s: float = 300.0
    learned_preference_ttl_s: float = 86400.0
    cache_max_entries: int = 2048
```

Suggested validation:

- concurrency `1..8`, default `1`;
- minimum interval `>= 0`, default `1s`;
- rejection cooldown bounded to a reasonable local maximum such as 30 minutes;
- learned TTL positive but it controls confidence/eviction only, not forced probing;
- bounded cache size.

Do not add a second independent `max_attempts` setting. Later negotiation must share `routing.max_retries_before_stream`.

If the implementation can keep one or more of these as safe internal constants without harming operator control, prefer fewer public knobs. The non-negotiable public behavior is bounded concurrency and no background probing, not the exact number of settings.

---

# Expected code surfaces

Primary files/packages likely touched:

- `src/eggpool/models/config.py`;
- `src/eggpool/providers/_templates.toml`;
- new `src/eggpool/providers/_wire_profiles.toml`;
- new small `src/eggpool/wire/types.py`;
- new small `src/eggpool/wire/registry.py`;
- provider config loading/packaging code;
- `config.example.toml` / `config.sbc.example.toml` only where the new routing knob should be visible;
- `pyproject.toml` package-data declaration only if required for the new TOML file;
- focused config/provider tests.

Do not change request dispatch/retry behavior in this phase except the narrowest compatibility plumbing needed to construct resolved profiles for tests.

---

# Focused tests

Add/adjust focused tests for:

1. legacy provider config synthesizes Chat/Responses/Messages surfaces correctly;
2. explicit surface config overrides legacy synthesis for that provider;
3. per-surface auth uses the same account key but the configured header/scheme;
4. provider-wide auth remains fallback when surface auth is absent;
5. surface-specific header collision is rejected;
6. `{model}` path template renders a safe canonical model ID;
7. unknown template placeholders are rejected by `check-config`;
8. unknown codec ID in `_wire_profiles.toml` fails closed;
9. bundled hint referencing a missing provider surface is rejected/ignored according to one documented rule;
10. OpenCode Go resolves all three candidate profiles without provider-specific Python branches;
11. old `responses_path` configs still pass `check-config`;
12. rehash/config-generation creation produces a deterministic candidate fingerprint used later for cache invalidation.

Do not create a matrix covering every bundled provider. Test the generic synthesizer plus one or two representative explicit providers.

---

# Acceptance criteria

- [ ] A typed `WireSurfaceName` exists independently of `ProtocolName`.
- [ ] `ProviderConfig` can represent multiple candidate wire surfaces with distinct paths and optional surface-specific auth/headers.
- [ ] Path templates support the minimal `{model}` use case and reject arbitrary placeholders.
- [ ] Existing provider configs synthesize equivalent Chat/Responses/Messages candidates without migration.
- [ ] `_wire_profiles.toml` is packaged and validated as data, not executable configuration.
- [ ] TOML may reference only built-in registered codec IDs.
- [ ] Bundled provider/model hints are low-authority and contain source/verification metadata.
- [ ] OpenCode Go is representable as three candidates without a provider-specific dispatch branch.
- [ ] Muse Spark's bundled current preference is Responses when current docs still confirm that mapping.
- [ ] Surface-specific auth can avoid sending both Bearer and `x-api-key` on every OpenCode Go request.
- [ ] No account secret is copied into learned/profile metadata.
- [ ] No database migration is introduced.
- [ ] No network request, background worker, negotiation retry or request-body transcoding is added by this phase.
- [ ] `check-config` covers malformed surface configuration.
- [ ] Existing `config.example.toml` and SBC example remain valid.

---

# Rejection conditions

Reject the implementation if it:

- creates provider-specific URL branches for OpenCode Go or another named provider;
- makes `ProtocolName` enumerate every wire API;
- permits TOML to import/execute arbitrary codec classes;
- introduces a generic templating engine;
- stores credentials in the wire registry/cache;
- removes legacy provider path fields and breaks user configs during this correctness pass;
- makes bundled model hints permanent truth;
- adds database persistence or background discovery before runtime negotiation is implemented;
- adds new dependencies solely for config/profile handling.

---

# Verification

Run focused config/provider tests, then the normal lean gate used by the repository at implementation time. At minimum:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No live provider traffic is required for this configuration phase.

---

# Handoff

1. Read Plan 147 and current provider/config models.
2. Re-check official OpenCode Go endpoint/auth documentation.
3. Add the closed surface type and packaged registry loader.
4. Add provider surface config plus legacy synthesis.
5. Add surface-specific auth/header validation.
6. Add bounded negotiation config object without enabling negotiation behavior.
7. Migrate only bundled templates that benefit from explicit surfaces.
8. Add focused tests and run the normal gate.
9. Record implementation SHA and any schema deviations in this file's closure section when complete.
