# Plan 200: Agent model catalog and client configuration lifecycle

> **Status:** READY FOR IMPLEMENTATION
>
> **Parent:** Plan 198
>
> **Baseline context:** current native integrations boundary in `rust/src/operations/integrations.rs`; standard `/v1/models` remains unchanged
>
> **External audit baselines:** OpenAI Codex `4701aa4b4239c70063ab6f2fcb835324f9c109f4`; OpenCodex `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`; OpenCode provider docs reviewed 2026-09-16
>
> **Priority:** P0/P1 — highest-leverage remaining usability work for coding-agent use
>
> **Scope:** derive conservative agent-facing model facts from Eggpool's existing catalog/model-info/routing data; render a current Codex model catalog and richer OpenCode provider/model configuration; add safe, idempotent setup lifecycle commands without changing the standard OpenAI model-list API.

## Executive summary

Eggpool already knows substantially more about models than it exposes to coding clients. `IntegrationModel` currently carries:

- model/public ID and base model ID;
- optional provider identity;
- display name;
- capabilities/source metadata;
- context/input/output token limits.

Current Codex can load a complete local model catalog through the root `model_catalog_json` config field. Current OpenCode accepts custom OpenAI providers and uses per-model context/output limits. Today Eggpool's Codex setup intentionally requires an explicit model/alias and does not provide rich picker discovery; OpenCode setup similarly leaves some model metadata under-specified.

The goal is to turn existing Eggpool facts into a stable provider-neutral integration projection, then keep Codex/OpenCode schema churn inside small renderers. The user-facing endpoint becomes close to:

```bash
eggpool configsetup codex --apply
eggpool configsetup opencode --apply
```

with corresponding `--check`, `--sync`, `--remove`, and `--dry-run` behavior where safe.

This plan deliberately supersedes Plan 197's earlier non-goal of a Codex config-file mutator. Plan 197 was correct for closing the basic protocol path. The new scope is an explicit usability feature with ownership, rollback, and drift detection rather than an incidental mutation hidden inside snippet generation.

---

# Research findings

## Current Codex model catalog

At audit commit `4701aa4b...`, current Codex configuration accepts `model_catalog_json` as a startup-loaded full model catalog. Authority paths include:

- `codex-rs/config/src/profile_toml.rs`;
- `codex-rs/config/src/config_toml.rs`;
- `codex-rs/core/src/config/mod.rs`;
- `codex-rs/core/config.schema.json`;
- current model/catalog types under `codex-rs/protocol/src/openai_models.rs` and the models-manager crates.

Context-window metadata is not cosmetic: current Codex derives a default auto-compaction threshold from the resolved context window, normally 90% where no explicit value overrides it.

OpenCodex's current integration confirms the operational pattern: it owns a generated catalog, points Codex root configuration at it, synchronizes catalog changes, and treats config edits as an ownership/restore problem. Eggpool should copy the small safety lessons, not the whole product.

## Current OpenCode provider contract

Current OpenCode documentation distinguishes:

- Responses-backed OpenAI provider runtime (`@ai-sdk/openai`);
- generic OpenAI-compatible Chat Completions runtime (`@ai-sdk/openai-compatible`).

Eggpool's preferred coding-agent surface is `/v1/responses`, so the OpenCode renderer should use the Responses-capable runtime and include accurate per-model context/output limits. Do not make OpenCode depend on Codex catalog JSON.

---

# Workstream 1 — Define the provider-neutral agent model projection

Use the existing integrations/catalog boundaries rather than creating a second model database.

Primary existing authority:

- `rust/src/operations/integrations.rs` (`IntegrationModel`, `ModelLimits`, catalog loading/renderers);
- `rust/src/catalog/`;
- `rust/src/model_router.rs` and `rust/crates/eggpool-model-routing/`;
- `rust/src/db/repositories.rs` provider/global model metadata;
- model-info canonical data used by existing integrations.

Either extend `IntegrationModel` with a nested normalized capability structure or introduce a small sibling type used only by integration renderers. Avoid a Codex-specific model type in routing/catalog code.

Suggested semantic projection:

```rust
struct AgentModelCapabilities {
    context_tokens: Option<u64>,
    max_output_tokens: Option<u64>,
    input_text: bool,
    input_images: Option<bool>,
    reasoning: Option<AgentReasoningCapabilities>,
    function_tools: Option<bool>,
    freeform_tools: Option<bool>,
    deferred_tool_search: Option<bool>,
    responses: bool,
    websockets: bool,
}

struct AgentReasoningCapabilities {
    efforts: Vec<String>,
    default_effort: Option<String>,
    summaries: Option<bool>,
}
```

The exact Rust shape should follow existing capability types and avoid string duplication where enums already exist.

### Rules

- derive only from validated catalog/profile/model-info facts;
- unknown stays `None`/unknown, not optimistic true;
- do not infer capability from model ID substrings;
- `websockets` remains false until a real Eggpool Responses WebSocket path exists;
- remote compaction remains separate from ordinary context limits and is not advertised until Plan 199 closes;
- provider-private source metadata must not leak through the public projection unintentionally.

---

# Workstream 2 — Collapse aliases/selectors conservatively

A public Eggpool model/alias can route to heterogeneous provider/model targets. The client-visible capability must describe what Eggpool can **guarantee** for that public ID.

Default aggregation:

- context window: minimum known guaranteed context across eligible targets;
- output limit: minimum known guaranteed output across eligible targets;
- boolean required feature: intersection across eligible targets;
- reasoning efforts: set intersection;
- input modalities: intersection;
- unknown on any required candidate remains unknown/conservative unless routing can prove that candidate is excluded for the requested semantic.

Do not publish the union of possible features. A union would invite Codex/OpenCode to construct a request that some routes cannot satisfy.

If the existing semantic model router already performs feature-aware route selection with a hard guarantee, that fact may justify a broader advertised capability. Prove it with routing tests rather than assuming it.

### Static direct model/provider IDs

For an unambiguous direct target, retain the provider/model facts as-is after normalization.

### Empty or conflicting metadata

Prefer a usable conservative entry over dropping a model from the catalog entirely when safe. For example, an unknown context window can remain absent if the target schema allows it. If Codex's strict catalog schema requires a numeric field, use a documented conservative policy based on an explicit Eggpool default only if current Codex accepts that semantics. Do not fabricate provider precision.

---

# Workstream 3 — Render a current Codex model catalog

Add a deterministic renderer owned by the integrations boundary.

The renderer must be built from the implementation-time Codex model schema, not from stale examples in this plan.

### Required behavior

- emit valid current Codex catalog JSON;
- include every Eggpool public model/alias that is safe to expose;
- use the Eggpool public ID as the selectable slug/model ID;
- carry display name and context/output/reasoning facts where known;
- choose only tool/shell capability values that match Eggpool's qualified semantics;
- keep unsupported WebSocket/remote-compaction features disabled;
- deterministic ordering for stable diffs/hashes;
- bounded total catalog size;
- no API keys or provider credentials;
- no raw source metadata blob unless a field is explicitly required and sanitized.

### Strict-parser compatibility

Current Codex model parsing can be stricter than a generic JSON consumer. Add a source-provenanced fixture derived from current Codex's own model types/fixtures.

Do not blindly clone one bundled OpenAI model object and change its slug. That can falsely advertise OpenAI-only shell/tool/reasoning behavior.

If current Codex requires fields Eggpool cannot semantically know, document and centralize a conservative fallback policy in the renderer. Keep those defaults out of routing/catalog storage.

### Output location

Use an Eggpool-owned generated artifact by default, for example under the user's Eggpool state/config integration area, rather than pretending the file belongs to Codex.

The final path should be resolved by `operations::paths`/XDG conventions. The Codex config receives an absolute `model_catalog_json` path.

When `CODEX_HOME` is explicitly set, respect it for locating Codex configuration, but keep Eggpool ownership metadata separate unless the implementation-time Codex contract requires the catalog itself inside `CODEX_HOME`.

---

# Workstream 4 — Upgrade the Codex provider renderer

Current `build_codex_toml_snippet()` already correctly emits:

```toml
model_provider = "eggpool"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://.../v1"
wire_api = "responses"
supports_websockets = false
env_key = "EGGPOOL_API_KEY"
```

Retain those semantics.

Add root-level:

```toml
model_catalog_json = "/absolute/path/to/generated/eggpool-codex-models.json"
```

when rendering/applying a managed catalog.

An explicit `model = "..."` may still be selected when requested. With a rich catalog installed, lack of a top-level model is no longer an error: the user can choose from Codex's model UI/CLI.

Never embed the resolved Eggpool server key into Codex TOML.

---

# Workstream 5 — Render richer OpenCode configuration from the same projection

Keep OpenCode-specific schema in its renderer.

At implementation time verify the current OpenCode config schema and package/runtime naming. The intended semantics are:

- Eggpool base URL ends at `/v1`;
- use the current OpenAI Responses-capable SDK/runtime;
- expose Eggpool public IDs as selectable model IDs;
- set model display names;
- set `limit.context` and `limit.output` when known;
- expose reasoning/model variants only where current OpenCode schema has an exact compatible representation;
- do not claim image/tool capability that Eggpool cannot guarantee across an alias route.

If OpenCode requires API key material in its provider config, prefer its supported environment-variable interpolation/reference mechanism when available. Do not regress existing secret-safe delivery policy.

---

# Workstream 6 — Add explicit config lifecycle commands

Extend the existing `configsetup` target arguments rather than inventing an unrelated command family.

Desired user contract:

```text
eggpool configsetup codex --apply
eggpool configsetup codex --sync
eggpool configsetup codex --check
eggpool configsetup codex --remove
eggpool configsetup codex --dry-run

eggpool configsetup opencode --apply
eggpool configsetup opencode --sync
eggpool configsetup opencode --check
eggpool configsetup opencode --remove
eggpool configsetup opencode --dry-run
```

If the shared Clap argument shape makes target-specific flags awkward, introduce a small typed lifecycle argument shared by targets that support managed installation. Do not expose flags on targets where they are meaningless.

### Semantics

`--apply`
: create/update Eggpool-owned generated artifacts and make the minimum safe client-config changes.

`--sync`
: recompute model catalog/provider facts and converge only Eggpool-owned state. Refuse unsafe drift.

`--check`
: read-only validation. Report whether client config, generated catalog, base URL, environment-key reference, and hashes are current. No mutations.

`--remove`
: remove only Eggpool-owned generated files/fields when current state still matches ownership evidence; restore previously captured values where safely possible.

`--dry-run`
: render the exact proposed diff/actions without writing.

Keep the existing snippet/clipboard/output workflow for users who do not want automatic changes.

---

# Workstream 7 — Safe Codex config mutation

OpenCodex's current experience is useful here: editing `config.toml` safely requires root-key placement, ownership, drift detection, and crash-safe restoration. Eggpool needs a much smaller version of that discipline.

### Do not rewrite arbitrary TOML through a lossy serializer

A parse-and-reserialize of the user's entire Codex config can remove comments/reorder formatting. Avoid that unless the repository intentionally adopts a preserving TOML editor and accepts the dependency/maintenance cost.

A small targeted writer can instead own only these facts:

- root `model_provider` when the user asks Eggpool to be the active provider;
- root `model_catalog_json`;
- optional root `model` only when explicitly requested;
- `[model_providers.eggpool]` table.

Root keys must be placed in the TOML root before the first table. An appended root assignment after an unrelated table header would belong to that table and is invalid for this purpose.

### Ownership manifest

Store a small Eggpool-owned manifest in Eggpool state, containing only:

- target client/config path;
- pre-edit hash;
- post-edit hash;
- which fields/table Eggpool owns;
- previous values for those exact fields where restoration is safe;
- generated catalog path/hash;
- Eggpool version/schema version.

No secrets.

### Atomicity

- read and validate before writing;
- create a same-directory temporary file;
- fsync/atomic rename using existing project primitives where available;
- retain one bounded backup or previous-value manifest;
- if current client config changed unexpectedly since Eggpool's last write, `--sync`/`--remove` must refuse rather than clobber user changes unless an explicit `--force` policy is designed and documented.

Do not invent a complex journal if the existing `operations::config_mutation` primitives plus atomic backup are enough. Keep this proportional to a local-user tool.

---

# Workstream 8 — Safe OpenCode config mutation

First verify the current config file format and whether comments/JSONC are accepted.

If a normal serde JSON round trip would destroy supported comments or non-JSON syntax, do not silently rewrite an existing user file.

Preferred hierarchy:

1. use an official OpenCode command/config API if one exists and is stable;
2. use a current documented composition/include mechanism if one exists;
3. otherwise implement a preserving narrow mutation strategy with ownership/drift tests;
4. if none is safe without a disproportionate parser dependency, keep `--apply` limited to absent/new files and make existing-file users use generated output until a safe mutator is available.

Ease of use does not justify corrupting a developer's primary OpenCode config.

---

# Workstream 9 — Optional remote model-projection endpoint

Local `configsetup` can initially render from Eggpool's local config/database. LAN deployments may eventually want a client machine to synchronize against a remote Eggpool server.

If needed, add one explicit non-standard, authenticated, read-only endpoint carrying the **Eggpool canonical agent projection**, not Codex JSON directly.

Possible shape/path (implementation can rename to fit server conventions):

```text
GET /api/integrations/models
```

Response requirements:

- schema version;
- deterministic model list;
- sanitized guaranteed capabilities/limits;
- generation/catalog version or ETag source;
- no credentials;
- bounded response;
- normal Eggpool server-key authentication, not public-dashboard policy.

A local `configsetup ... --sync --base-url ...` can fetch this projection and render the current client-specific schema locally.

Do **not** overload `/v1/models` and do not serve a Codex-private schema as though it were OpenAI-standard.

This endpoint is optional for initial closure if local config generation satisfies the deployment model.

---

# Workstream 10 — Conformance and drift detection

## Codex fixtures

Add deterministic fixtures pinned to the implementation-time Codex source commit:

- generated catalog parses under the expected schema;
- context/output limit values survive parsing;
- reasoning-effort fields survive parsing;
- model IDs/display names appear correctly;
- unsupported WebSockets/remote compaction are not advertised;
- aliases use conservative intersection values;
- unknown facts do not become optimistic capabilities.

Where feasible, use a small test helper that mirrors current Codex required fields without importing Codex.

## OpenCode fixtures

Assert current expected provider/runtime and per-model limit shapes. Keep version-sensitive fields centralized.

## Config mutation fixtures

At minimum cover:

- empty/missing client config;
- user config with comments and unrelated root keys/tables;
- existing non-Eggpool model provider;
- existing `[model_providers.eggpool]` from an older Eggpool version;
- `model_catalog_json` owned by another tool/user;
- repeated `--apply` idempotency;
- `--sync` after Eggpool catalog change;
- external user edit after Eggpool apply -> safe drift refusal;
- `--remove` restores only owned fields;
- interruption-safe atomic write simulation where current test helpers allow it;
- no API key appears in Codex files/manifests/catalogs.

---

# Expected source changes

Likely files, adjusted to implementation-time tree:

```text
rust/src/cli.rs
    configsetup lifecycle flags/args

rust/src/runtime.rs
    dispatch and operator-visible messages

rust/src/operations/integrations.rs
    normalized agent model facts
    Codex catalog renderer
    richer OpenCode renderer
    target lifecycle orchestration

rust/src/operations/config_mutation.rs
    reuse/extend only the narrow atomic mutation primitives needed

rust/src/operations/paths.rs
    generated integration artifact/manifest paths if a reusable path owner is needed

rust/src/server/health.rs or a small integration endpoint adapter
    only if remote projection sync is implemented

rust/src/server/mod.rs
    optional route registration

rust/tests/operations_o005.rs
    configsetup integration contract

README.md
docs/agent-configuration.md
architecture/ docs only if a new persistent ownership boundary is introduced
```

If `integrations.rs` becomes unwieldy, a focused split such as `operations/integrations/{mod,codex,opencode,models}.rs` is acceptable. Do not refactor all integration targets merely to implement two renderers.

No new production dependency should be added unless preserving user config safely cannot be achieved with existing primitives. Any parser/editor dependency must be justified against binary size and maintenance cost.

---

# Verification

Run the focused integration contract first:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
```

If server-side remote projection is added, also run the relevant server/health/catalog/runtime tests.

Then run the full serial workspace suite and locked release build. If Cargo dependencies change, also run `cargo deny` and feature-tree/duplicate checks per the development skill.

---

# Acceptance criteria

1. Eggpool has one provider-neutral coding-agent model projection derived from existing catalog/model-info/profile facts.
2. Aliases/selectors advertise conservative guaranteed capabilities, not unions of heterogeneous targets.
3. Current Codex can load an Eggpool-generated `model_catalog_json` without parser errors.
4. Codex model entries carry accurate/conservative context and output limits and reasoning facts where known.
5. Eggpool continues to configure Codex with `wire_api = "responses"`, `supports_websockets = false`, and `env_key = "EGGPOOL_API_KEY"`.
6. No resolved Eggpool API key appears in Codex TOML, generated catalog, ownership manifest, or logs.
7. OpenCode's generated provider uses the current Responses-capable runtime and receives per-model limits from the same projection.
8. `eggpool configsetup codex --apply|--sync|--check|--remove|--dry-run` has a documented idempotent ownership contract, or the exact supported subset is explicitly documented if a flag proves unsafe on a current client format.
9. Equivalent managed OpenCode setup is implemented only to the extent it can preserve existing user configuration safely.
10. Repeated apply/sync is idempotent.
11. Drift in a user-edited client config causes a safe refusal rather than silent clobbering.
12. Remove only deletes/restores Eggpool-owned state.
13. Standard `/v1/models` remains unchanged and OpenAI-compatible.
14. Any richer remote projection uses a separate authenticated Eggpool-specific endpoint/schema.
15. No model capability is inferred from its name.
16. No Codex/OpenCode runtime dependency is added.
