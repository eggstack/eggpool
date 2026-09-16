# Plan 213: Codex and OpenCode portable client adapters

> **Status:** ready for implementation
>
> **Baseline:** EggPool `main` at `6997cdbb4cee4ec69c71cab15b53b039c7e3309a` (2026-09-16)
>
> **Parent:** Plan 209
>
> **Depends on:** Plans 210–212
>
> **Current qualified clients:** Codex CLI 0.154.0 and OpenCode 1.18.30 from Plan 206; re-check current client source/docs immediately before implementation
>
> **Primary authority:** `rust/crates/eggpool-client-config/` after Plan 210, current `rust/src/operations/integrations.rs`, `docs/agent-configuration.md`, `docs/codex-compatibility-smoke.md`
>
> **Priority:** P0/P1 — make the transactional helper actually safe across current client config formats
>
> **Scope:** implement loss-minimizing, version-aware Codex/OpenCode adapters for both same-host `configsetup` and `eggpool-connect`, preserving user configuration and current client-native qualification behavior.

## Objective

The remote connection profile intentionally contains no final Codex/OpenCode file. The receiving machine knows which client/version/schema is actually installed, so the portable client-config crate must render and mutate the correct local representation at install time.

Codex is already close to the desired end state. OpenCode needs more work because the current EggPool managed lifecycle refuses JSONC-commented configs and current OpenCode V2 documentation is changing provider schema names/packaging.

This plan upgrades both adapters without allowing client schema details to leak into routing, server catalog persistence, or the portable connection-profile format.

---

# Workstream 1 — Establish implementation-time client baselines

Before editing adapters, record the exact current upstream/client evidence used for implementation.

For Codex, verify at minimum:

- current config schema/type owning `model_provider` and `model_catalog_json`;
- current custom provider fields and accepted `wire_api` values;
- current model catalog required fields;
- `CODEX_HOME` path semantics;
- current `codex debug models` / `codex doctor --json` or replacement validation commands.

For OpenCode, verify at minimum:

- stable/current V1 config shape used by installed versions still in support scope;
- current V2 config shape and migration rules;
- exact package/runtime identifier for an OpenAI Responses-compatible custom provider;
- provider/model field names and environment interpolation semantics;
- `OPENCODE_CONFIG` and platform-global path behavior;
- current command that lists/validates configured providers/models.

Save small source-derived fixtures or comments with upstream commit/version provenance. Do not vendor large upstream source trees.

The adapter should support **qualified variants**, not a guessed continuous version range.

---

# Workstream 2 — Codex document editing

Current `integrations.rs` uses a narrow line-oriented TOML mutator that deliberately preserves unrelated comments/tables. It is qualified and should not be destabilized casually. However, a general cross-platform desktop configurator benefits from a real format-preserving TOML document model.

Evaluate `toml_edit` at implementation time. At planning time it is the preferred candidate because it is designed for format-preserving TOML editing.

### Dependency gate

Before adoption:

- verify current crate version/MSRV against EggPool's Rust toolchain policy;
- run `cargo deny` license/advisory/source checks;
- inspect duplicate versions/features;
- measure release-binary impact;
- ensure it can live in `eggpool-client-config` without adding unrelated runtime facilities.

If the existing narrow mutator proves safer/smaller and all required edge cases can be covered, it may remain. The acceptance criterion is preservation/correctness, not a mandated dependency.

### Owned Codex fields

Preserve the current ownership boundary:

```text
root model_provider
root model_catalog_json
root model             only when EggPool explicitly manages a selected model
[model_providers.eggpool]
generated EggPool Codex model catalog artifact
```

Do not modify unrelated root keys, profiles, MCP config, approval/sandbox settings, feature flags, notices, history, telemetry, or other providers.

### Provider contract

Retain current qualified semantics unless implementation-time Codex changed them:

```toml
model_provider = "eggpool"
model_catalog_json = "/absolute/local/path/to/eggpool-codex-models.json"

[model_providers.eggpool]
name = "EggPool"
base_url = "https://pool.example/v1"
wire_api = "responses"
supports_websockets = false
env_key = "EGGPOOL_API_KEY"
```

Never insert the resolved API key into TOML.

### Generated catalog

Continue using the provider-neutral sanitized projection. Preserve current strict fields discovered during Plan 206, including `supported_reasoning_levels` and `base_instructions`, unless implementation-time Codex schema requires a documented replacement.

Do not copy OpenAI-specific built-in model metadata merely to satisfy parsing. Unknown capability remains conservative.

---

# Workstream 3 — Codex inspection, drift, and removal

The portable adapter should separate:

- document parse/inspection;
- semantic owned-field comparison;
- proposed mutation;
- previous owned values;
- generated artifact plan.

Avoid relying only on a whole-file hash. A user may safely edit unrelated Codex settings after EggPool installation.

A sync/install is safe when unrelated fields changed but EggPool-owned fields are either unchanged or their drift is explicitly reviewed. Capture previous values at first ownership so removal restores them where possible.

If a pre-existing `[model_providers.eggpool]` table existed before EggPool ownership, preserve enough data to restore it exactly/semantically rather than blindly deleting it.

Removal must not reset a user's unrelated `model` or `model_provider` if EggPool did not own that field.

---

# Workstream 4 — Codex client-native validation

After local parse validation, `eggpool-connect` should run current non-inference Codex checks through its process runner.

At the Plan 206 baseline:

```bash
codex debug models
codex doctor --json
```

were the useful checks. Re-verify names/semantics before implementation.

Qualification must prove:

- `config.toml` parses;
- generated model catalog parses under Codex's strict parser;
- EggPool provider has the intended Responses wire API;
- WebSockets are not advertised;
- expected models are discoverable;
- `EGGPOOL_API_KEY` availability is reported correctly when provided to the child process.

Do not run a prompt/inference as part of normal install validation. Live inference remains a separate opt-in smoke test.

---

# Workstream 5 — OpenCode schema variants

Do not maintain one hard-coded JSON object for all OpenCode versions.

At planning time, EggPool's qualified OpenCode 1.18.30 path uses a V1-style provider object with:

```text
provider
npm
options
models
```

Current OpenCode V2 documentation moves custom-provider configuration toward:

```text
providers
package
settings
models
```

and exposes Responses-capable provider packages in its current provider stack.

Represent these as explicit adapter variants, for example:

```rust
enum OpenCodeSchemaVariant {
    V1,
    V2,
}
```

Variant selection must be based on implementation-time verified client version/config behavior. If a version is unknown/ambiguous, refuse automatic mutation and provide a read-only generated plan/manual fragment.

Do not write both V1 and V2 keys into one file in the hope that one is ignored.

---

# Workstream 6 — JSONC-preserving OpenCode mutation

Current `opencode_lifecycle()` intentionally refuses to rewrite a config containing JSONC comments because `serde_json` would destroy comments. Remote setup should remove this limitation safely.

Adopt a comment/trivia-preserving JSONC parser/editor or implement an equally robust narrow structural editor.

At planning time, `jsonc-parser` is a candidate; implementation must re-check its current version, MSRV, license, maintenance, transitive dependencies, manipulation API, and binary-size impact before adoption.

### Required preservation

Given an existing OpenCode config with:

- line/block comments;
- trailing commas;
- arbitrary indentation;
- unrelated providers;
- plugins/agents/commands/permissions/themes/settings;
- user-chosen ordering;

adding or updating EggPool must not discard or normalize the whole document unnecessarily.

Mutation should touch only the target provider entry and, if absolutely required by the current schema, narrowly scoped supporting keys owned by EggPool.

### Invalid JSONC

If the existing file cannot be parsed under the actual OpenCode JSONC grammar, refuse before backup/write and show the parse location/reason in bounded form. Do not fall back to a full generated replacement.

---

# Workstream 7 — OpenCode ownership and restoration

Fix the existing restoration weakness around a pre-existing provider named `eggpool`.

On first apply, capture the previous exact/semantic provider entry for the relevant schema path:

```text
V1: provider.eggpool
V2: providers.eggpool
```

On remove:

- restore the previous entry if one existed;
- otherwise remove only EggPool's owned entry;
- preserve all other config/comments;
- remove an empty parent provider object only if doing so does not remove comments/formatting or change semantics unexpectedly.

If the provider entry changed externally after installation, treat it as owned-field drift. Do not delete it automatically.

The backup system from Plan 212 remains the last-resort exact recovery path, but normal `remove` should use narrow ownership semantics.

---

# Workstream 8 — OpenCode model projection

Both V1 and V2 renderers consume the same `AgentIntegrationProfileV1` model projection.

For each model expose only current-schema fields that match proven EggPool capabilities:

- public model ID;
- display name;
- context/output limits when known;
- text input/output;
- image input only when guaranteed;
- reasoning support/variants only when guaranteed and exactly representable;
- tool capabilities only when the current OpenCode schema uses them and EggPool can guarantee the semantic.

Do not infer capability from model names.

Do not expose provider-private metadata.

Do not advertise WebSocket behavior EggPool does not implement.

Do not attempt to make OpenCode consume the Codex catalog format.

---

# Workstream 9 — OpenCode auth and provider runtime

Continue referencing the environment rather than embedding the key.

For V1, preserve the currently qualified environment interpolation mechanism where still valid.

For V2, use the current documented provider auth/env contract. If current V2 expects a list of environment variables instead of an interpolated field, render that schema exactly.

Select a Responses-capable provider package/runtime. Do not silently downgrade to generic Chat Completions merely because it is easier to configure; that would regress EggPool's qualified Responses behavior and coding-agent semantics.

If OpenCode current version only provides a compatible generic Responses wrapper rather than the previous `@ai-sdk/openai` package, use the documented current package and capture that as version-specific adapter policy.

---

# Workstream 10 — Cross-platform path rules

Move receiving-machine path resolution out of server assumptions.

### Codex

Respect `CODEX_HOME` first. Resolve the documented platform default otherwise.

### OpenCode

Respect `OPENCODE_CONFIG` first. Resolve the current documented platform-specific global config path otherwise, including Windows. XDG is not a universal Windows rule.

### EggPool-connect state/catalog

Generated client-side catalogs/manifests/backups go under the helper's user state root, not under the remote server's paths and not into arbitrary client directories unless the client contract requires it.

Every generated catalog path written into client config must be an absolute receiving-machine path.

---

# Workstream 11 — Fixtures and regression matrix

Add representative fixtures for each qualified client schema variant.

Codex fixtures should include:

- empty config;
- comments and unrelated tables;
- pre-existing another model provider;
- pre-existing EggPool provider;
- profiles/MCP/unrelated nested tables;
- explicit user model;
- external edits after EggPool install.

OpenCode fixtures should include:

- empty V1/V2;
- comments before/inside/after provider object;
- line + block comments;
- trailing commas;
- unrelated providers and nested settings;
- pre-existing `eggpool` provider;
- external edits to unrelated keys;
- external edits to EggPool-owned entry;
- malformed JSONC.

Tests should assert semantic correctness **and** preservation of designated comments/unrelated text. Avoid snapshots that permit a wholesale formatting rewrite to pass unnoticed.

---

# Dependency/size verification

If format-preserving parser dependencies are added:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo build --manifest-path rust/Cargo.toml --locked --release
```

Compare before/after binary size for the proxy and helper. If a dependency is only needed by `eggpool-connect`, structure workspace features/crates so it does not unnecessarily inflate the SBC proxy binary.

Do not sacrifice safe JSONC/TOML preservation merely to save a trivial amount of binary size; do avoid pulling an entire scripting/runtime ecosystem for document editing.

---

# Focused qualification

Run current local integration tests:

```bash
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
```

Run new portable adapter fixture tests and Plan 212 transaction/rollback tests.

Live/client qualification belongs in Plan 214 but this plan is not complete until at least one current local Codex and OpenCode install/check/remove sequence passes in isolated temp homes.

---

# Acceptance criteria

1. Codex and OpenCode schema-specific code lives in portable adapters, not routing/catalog persistence or connection-profile fields.
2. Codex preserves its current qualified Responses provider semantics and strict catalog requirements.
3. Codex mutation preserves comments/unrelated TOML and restores captured previous owned values on remove.
4. OpenCode automatic mutation supports JSONC comments/trailing commas without lossy whole-file serialization.
5. OpenCode has explicit qualified schema variants for current supported V1/V2 contracts rather than guessing one universal shape.
6. Unknown/unsupported OpenCode versions fail closed to plan/manual output.
7. A pre-existing `eggpool` provider entry is captured and restored rather than discarded.
8. V1/V2 provider packages/settings select a current Responses-capable path.
9. Both clients reference `EGGPOOL_API_KEY`/current environment auth semantics and never embed the resolved key.
10. Model capabilities remain conservative and come from one shared provider-neutral projection.
11. Receiving-machine paths are platform-aware and honor documented client overrides.
12. Same-host `eggpool configsetup` and remote `eggpool-connect` use the same renderer/mutation policy for equivalent client variants.
13. Format-preserving dependencies, if added, pass dependency/security review and have measured footprint.
14. Current isolated Codex/OpenCode apply/check/remove smoke sequences pass without touching real user configs.

## Handoff note

Treat upstream client schemas as fast-moving renderer contracts. Re-check them at implementation time and pin evidence in tests/comments. If Codex/OpenCode change again, adapt the client layer; do not mutate EggPool's routing/wire architecture merely to mirror a client config schema.
