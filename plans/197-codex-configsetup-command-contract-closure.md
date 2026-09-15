# Plan 197: Codex `configsetup` command contract closure

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` at `18af1fcdc60625a413020abbcea4335d65710868`
>
> **Parent context:** Plans 192–196
>
> **Scope:** reconcile `eggpool configsetup codex` with the now-qualified Codex Responses integration. Keep the generated provider configuration generic and safe, remove misleading secret-handling behavior, add exact contract tests, and make the model-selection/documentation behavior match what the command actually does.

## Executive summary

The current Codex renderer is already structurally correct for the supported HTTP/SSE Responses path. `build_codex_toml_snippet()` emits:

```toml
model_provider = "eggpool"
model = "<optional explicit model-or-alias>"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://<eggpool-host>:<port>/v1"
wire_api = "responses"
supports_websockets = false
env_key = "EGGPOOL_API_KEY"
```

That matches the architecture established by Plans 192–195: explicit custom provider, Responses wire API, HTTP/SSE only, standard Eggpool `/v1` base URL, environment-key authentication, and no Codex-private model-discovery contract.

The remaining problem is the surrounding command contract.

`Target::contains_secret()` currently returns `true` for Codex even though the Codex snippet does **not** contain the resolved API-key value; it contains only the literal environment-variable name `EGGPOOL_API_KEY`. As a consequence, generic delivery suppresses stdout or claims a secret was copied unless `--print-secret` is supplied. The documentation reinforces this by saying `--print-secret` includes the Codex API key, but the renderer never embeds the key.

There is also a model-selection documentation mismatch. `resolve_model()` only auto-selects when exactly one model is available. For Codex, if more than one model exists and the user omits `--model`, the provider block can be generated without a top-level `model`. That is valid as a provider definition, but current docs claim a best/default model is automatically selected. The qualified Codex integration path should continue recommending explicit `--model`, while not unnecessarily forbidding users from selecting a model later with Codex's own CLI/config.

This plan fixes those command-level inconsistencies without changing Responses routing, model discovery, authentication policy, or Codex itself.

---

# Goals

1. `eggpool configsetup codex --model <alias>` produces a complete, current Codex provider block for Eggpool Responses.
2. The generated TOML never embeds Eggpool's resolved API-key value.
3. Normal Codex config generation does not require `--print-secret` merely to display a non-secret snippet.
4. The command tells the operator that `EGGPOOL_API_KEY` must be set in Codex's environment and points to `eggpool getkey` as the retrieval mechanism.
5. `--model` behavior and docs accurately reflect the implementation: explicit model is recommended/qualified; exactly one catalog model may be filled automatically; otherwise the provider config may omit `model` rather than inventing a choice.
6. Exact tests lock the Codex-specific fields and prevent regression back to Chat Completions or WebSockets.
7. No new production dependencies, Codex runtime dependency, or model-discovery endpoint is introduced.

---

# Non-goals

Do **not**:

- alter `/v1/responses` request or stream handling;
- change the native-observed/translated Responses architecture from Plans 193–195;
- add WebSocket Responses support;
- add a Codex/OpenCodex dependency;
- embed the Eggpool API key in `~/.codex/config.toml`;
- add automatic rich Codex model discovery;
- change Eggpool's standard `/v1/models` schema;
- force a globally configured Codex model if the operator intentionally wants to select `-m`/`--model` per invocation;
- add a bespoke Codex config-file mutator that risks overwriting unrelated `~/.codex/config.toml` content;
- broaden this into a general rewrite of all `configsetup` targets.

---

# Workstream 1 — Correct Codex secret classification

Primary file:

- `rust/src/operations/integrations.rs`

## 1.1 Treat the Codex TOML snippet as non-secret

Today `Target::contains_secret()` returns `true` for every integration target. The inline comment specifically calls out Codex as following the same fail-closed delivery behavior even though the Codex renderer references an environment variable instead of embedding the key.

Change the target classification so Codex returns `false` for snippet-secret purposes.

Conceptually:

```rust
pub const fn contains_secret(self) -> bool {
    !matches!(self, Self::Codex)
}
```

Use a more explicit match if that is clearer with the existing target list.

The invariant is:

```text
contains_secret == true
    only when the generated artifact itself contains the resolved secret value
```

Do not weaken secret handling for OpenCode, Aider, Claude Code, or any target whose rendered artifact actually contains the API key.

## 1.2 Preserve `env_key = "EGGPOOL_API_KEY"`

Do not replace `env_key` with an inline token field.

The generated Codex provider must continue to contain:

```toml
env_key = "EGGPOOL_API_KEY"
```

The environment variable is the correct separation between Codex configuration and credentials.

## 1.3 Define `--print-secret` behavior for Codex

Because `ConfigsetupArgs` is shared across targets, there is no need to remove the parser flag for Codex.

For Codex it should simply be unnecessary/no-op with respect to the rendered TOML. Passing it must **not** cause the API key to be inserted into the TOML.

Do not add a second mixed TOML+shell output format just to give `--print-secret` meaning.

If desired, the command may emit a short informational message explaining that Codex uses `EGGPOOL_API_KEY` and the generated TOML intentionally does not contain the secret. Keep this optional and concise.

---

# Workstream 2 — Add a Codex credential/setup hint

Primary file:

- `rust/src/operations/integrations.rs`

Add a Codex-specific `paste_hint()` / delivery hint explaining the environment requirement without printing the secret.

Recommended wording, adjusted to project style:

```text
Set EGGPOOL_API_KEY in the environment used to launch Codex; retrieve the current key with `eggpool getkey`.
```

If shell guidance is useful, document rather than automatically execute:

```bash
export EGGPOOL_API_KEY="$(eggpool getkey)"
```

Do not execute a shell or mutate the user's shell profile from `configsetup`.

Do not print the resolved key as part of the hint.

---

# Workstream 3 — Lock the exact Codex renderer contract

Primary file:

- `rust/src/operations/integrations.rs` unit tests

Potential higher-level test file if an existing CLI integration target is more appropriate:

- `rust/tests/cli_contract.rs`
- or a small existing operations/configsetup test module

Do not create a large new harness if the unit boundary is sufficient.

## 3.1 Exact renderer assertions

Add a dedicated test for `build_codex_toml_snippet()` with an explicit provider/model alias.

Assert that the output contains exactly the compatibility-sensitive facts:

```toml
model_provider = "eggpool"
model = "<requested-model>"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://.../v1"
wire_api = "responses"
supports_websockets = false
env_key = "EGGPOOL_API_KEY"
```

Also assert it does **not** contain:

- the actual `context.api_key` value;
- `wire_api = "chat"` / Chat Completions settings;
- any WebSocket enablement;
- any OpenAI-owned API-key variable name that would bypass Eggpool's server key;
- any Codex-private model-catalog endpoint.

Prefer parsing the generated TOML where practical rather than relying exclusively on substring tests. Substring assertions are still appropriate for the absence of the actual secret.

## 3.2 Model omission behavior

Add a test for `build_codex_toml_snippet(context, None)`.

Prove that:

- the provider block is still valid;
- `model_provider = "eggpool"` remains present;
- no fabricated model is inserted by the renderer itself;
- the absence of `model` is intentional and distinguishable from an empty-string model.

Model resolution belongs to `resolve_model()`, not the TOML renderer.

## 3.3 Delivery-secret classification

Add a direct regression assertion:

```rust
assert!(!Target::Codex.contains_secret());
```

and retain/extend assertions that targets embedding keys still return `true`.

Where feasible, test delivery with `--no-clipboard` and no `--print-secret` so Codex's non-secret TOML is returned on stdout.

The intended user-visible contract is:

```text
eggpool configsetup codex --model my-alias --no-clipboard
```

prints the provider TOML without requiring `--print-secret`.

## 3.4 Parser contract

Retain support for:

```text
eggpool configsetup codex
    --model <MODEL>
    --base-url <URL>
    --host <HOST>
    --no-clipboard
    --print-secret
```

Do not add a Codex-specific argument parser unless a real current Codex requirement needs one.

---

# Workstream 4 — Make model-selection behavior explicit and accurate

Primary files:

- `rust/src/operations/integrations.rs`
- `docs/agent-configuration.md`
- `README.md` only if needed for consistency

## 4.1 Keep explicit `--model` as the qualified recommendation

The current README form is appropriate:

```bash
eggpool configsetup codex --model <eggpool-model-or-alias>
```

Retain it.

Current Codex compatibility qualification assumes an explicit Eggpool model/alias because automatic rich model-picker discovery is intentionally deferred.

## 4.2 Do not invent a model when several exist

Do not change `resolve_model()` to choose an arbitrary or lexicographically first model when more than one model is available.

Correct behavior remains:

- explicit `--model`: use exactly that value;
- exactly one available model: auto-fill it if existing shared behavior does so;
- multiple/zero available models for Codex: provider snippet may omit top-level `model` unless the user supplied one.

This keeps `configsetup` deterministic and avoids silently selecting the wrong provider/model in an aggregator.

## 4.3 Correct inaccurate documentation

`docs/agent-configuration.md` currently says, generically, that without `--model` the generator picks the best available model. That is not the actual shared resolver contract.

Replace that wording with the real behavior.

Suggested semantic wording:

```text
`--model` sets an explicit model or Eggpool alias. If exactly one model is available, some targets can fill it automatically. When multiple models are available, Eggpool does not invent a preference unless that target has an explicit selection rule.
```

For Codex, state clearly:

```text
Use `--model` for the qualified setup path, or select the model explicitly when invoking Codex.
```

Do not imply that `/v1/models` supplies Codex's rich remote model catalog.

---

# Workstream 5 — Reconcile `--print-secret` documentation

Primary file:

- `docs/agent-configuration.md`

Potential secondary files:

- `README.md`
- CLI help text/comments if any mention Codex-specific secret printing

Correct the current statements such as:

```text
--print-secret | Include the API key in the output (for Codex env vars)
```

and:

```text
Codex — print TOML block with secret for env var reference
```

Those statements conflict with the renderer.

Document instead:

- the Codex TOML contains `env_key = "EGGPOOL_API_KEY"`, not the secret itself;
- set that environment variable separately;
- `eggpool getkey` retrieves the current Eggpool server key;
- `--print-secret` remains relevant to integration targets whose generated artifact actually contains a key, but is not needed to display the Codex block.

Do not encourage storing the server key directly in `~/.codex/config.toml`.

---

# Workstream 6 — Confirm configsetup output against the live qualification path

Parent plan:

- Plan 196

The Plan 196 live Codex qualification must use configuration semantically equivalent to what `eggpool configsetup codex` now emits.

Before closing both plans, compare:

```text
configsetup renderer fields
vs.
scripts/smoke_codex_compat.sh --config overrides
```

They must agree on:

- provider ID: `eggpool`;
- base URL ending in `/v1`;
- `env_key = EGGPOOL_API_KEY`;
- `wire_api = responses`;
- `supports_websockets = false`;
- explicit model/alias for qualification.

If the smoke harness has to carry a field that `configsetup` lacks, decide whether that field is truly required by current Codex. If required, add it to the generic renderer and test it. Do not leave the smoke script and generated user config on divergent contracts.

---

# Expected source changes

Likely minimal diff:

```text
rust/src/operations/integrations.rs
    - Codex no longer classified as secret-containing output
    - Codex environment/setup hint
    - exact Codex renderer/delivery tests

docs/agent-configuration.md
    - correct --print-secret semantics
    - correct model-resolution wording
    - retain explicit Codex --model recommendation

README.md
    - only if wording needs synchronization; current explicit --model example is already correct
```

No production dependency changes are expected.

---

# Verification

Run at minimum:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract
```

If the exact test target spelling differs, use the repository's current target names.

Then run the normal workspace suite before closure:

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Also run the Codex-specific compatibility target from Plans 195–196:

```bash
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
```

When credentials/provider access are available, run Plan 196's live Codex smoke using a config generated or manually compared against `eggpool configsetup codex --model <alias>`.

---

# Acceptance criteria

Plan 197 is complete when all of the following are true:

1. `eggpool configsetup codex --model <alias>` generates the current HTTP/SSE Responses provider configuration.
2. The generated TOML contains `model_provider = "eggpool"`.
3. The generated provider block contains the configured Eggpool `/v1` base URL.
4. The generated provider block contains `wire_api = "responses"`.
5. The generated provider block contains `supports_websockets = false`.
6. The generated provider block contains `env_key = "EGGPOOL_API_KEY"`.
7. The generated TOML contains the explicit requested model/alias when supplied.
8. The generated TOML never contains the resolved Eggpool API-key value.
9. Codex config output can be printed normally without requiring `--print-secret`.
10. Codex delivery tells the operator to set `EGGPOOL_API_KEY` and references `eggpool getkey` without exposing the key.
11. `--print-secret` does not cause the key to be embedded into Codex TOML.
12. With no explicit model, the command does not fabricate a preference when multiple models exist.
13. Documentation accurately describes model resolution and Codex secret handling.
14. The Plan 196 smoke configuration and `configsetup` renderer agree on all compatibility-sensitive provider fields.
15. Existing integration targets retain their current secret-protection behavior.
16. Existing Codex Responses conformance tests remain green.
17. No Codex/OpenCodex runtime dependency, new production dependency, rich model-discovery endpoint, or Codex fork is introduced.

---

# Closure note

This plan is intentionally separate from the protocol closure in Plan 196. A failure here should normally be a renderer, delivery, CLI-contract, or documentation bug. It should not trigger a redesign of the Responses request/stream architecture unless the current unmodified Codex CLI demonstrates that the generated provider contract itself is insufficient.
