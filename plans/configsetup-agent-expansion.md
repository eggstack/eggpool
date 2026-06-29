# Plan: expand `eggpool configsetup` to popular coding agents

## Objective

Add automated configuration generation for the following targets:

- `aider`
- `codex`
- `qwen-code`
- `kilo`
- `continue`
- `cline`
- `roo-code`
- `goose`
- `openhands`

The goal is to make EggPool usable as a local OpenAI-compatible routing endpoint for the common coding-agent ecosystem, while preserving the current safety properties of `configsetup`: do not print persistent secrets unnecessarily, do not generate one-time API keys that are not actually written to disk, and do not silently overwrite user-owned agent configuration without an explicit opt-in.

## Current repository shape

The existing implementation lives in `src/eggpool/cli_full.py` under the `@cli.group()` named `configsetup`.

Current targets:

- `eggpool configsetup opencode`
- `eggpool configsetup claude-code`

Relevant existing behavior:

- `configsetup opencode` auto-generates and persists `[server].api_key` if missing.
- It reads the configured EggPool server port, detects a LAN IP, and emits a base URL of the form `http://<lan-ip>:<port>/v1`.
- It loads model catalog data from the SQLite database when available.
- It handles `[models].collapse_models` and provider-suffixed model IDs.
- It applies effective limits through `ModelLimitResolver` and conservative limit merging.
- It enables `[transcoder].enabled = true` when OpenCode needs OpenAI-compatible access to Anthropic-only providers.
- JSON rendering for OpenCode is already isolated in `src/eggpool/integrations/opencode.py`.
- `configsetup claude-code` currently emits a smaller direct snippet and avoids writing the full secret to terminal scrollback when clipboard copy succeeds.

The main structural problem before adding more agents is that model catalog loading, key generation, base-url generation, transcoder handling, clipboard behavior, and command-specific rendering are interleaved in `cli_full.py`. Adding nine more targets directly to this file would create a large, fragile CLI module.

## Design decision

Implement a shared integration/configsetup layer first, then add thin Click command wrappers.

The core shape should be:

```text
src/eggpool/integrations/
  __init__.py
  common.py                 # shared endpoint/key/catalog/context helpers
  opencode.py               # existing renderer; keep and adapt
  aider.py
  codex.py
  qwen_code.py
  kilo.py
  continue_dev.py           # avoid keyword-ish/confusing module name `continue.py`
  cline.py
  roo_code.py
  goose.py
  openhands.py
```

`cli_full.py` should retain only the Click command definitions, option parsing, user-facing output, and calls into the integration builders.

## Shared configsetup context

Create a new helper module, probably `src/eggpool/integrations/common.py`, with a dataclass like:

```python
@dataclass(frozen=True)
class IntegrationContext:
    config_path: str
    api_key: str
    base_url: str              # http://host:port/v1
    base_url_root: str         # http://host:port, for tools that append /v1 themselves
    host: str
    port: int
    models: list[dict[str, Any]]
    collapse_models: bool
    config_mutated: bool
    transcoder_mutated: bool
```

Add a resolver function:

```python
def build_integration_context(
    *,
    config_path: str,
    require_catalog: bool = False,
    enable_transcoder_for_openai_clients: bool = True,
) -> IntegrationContext:
    ...
```

This should centralize the current `configsetup opencode` behavior:

1. Read `[server].api_key`.
2. If missing, generate a new key and persist it with `write_server_api_key()`.
3. Abort if the key cannot be persisted.
4. Read server port with `_read_server_port()`.
5. Detect LAN IP with `_detect_lan_ip()`.
6. Build `base_url = http://<lan-ip>:<port>/v1`.
7. Build `base_url_root = http://<lan-ip>:<port>`.
8. Load `AppConfig`.
9. Load catalog models from the database when available.
10. Add static models from provider config when needed.
11. Resolve effective limits.
12. Preserve the existing collapse/non-collapse behavior.
13. Optionally enable transcoder if OpenAI-compatible clients would otherwise be unable to reach Anthropic-only providers.
14. Return whether the EggPool config was mutated so the CLI wrapper can restart or print restart guidance.

Avoid importing Click inside the shared renderer modules. Use return values or typed exceptions and keep terminal output in `cli_full.py`.

## Command-line interface

Keep the existing command names and add the new ones:

```text
eggpool configsetup opencode
eggpool configsetup claude-code
eggpool configsetup aider
eggpool configsetup codex
eggpool configsetup qwen-code
eggpool configsetup kilo
eggpool configsetup continue
eggpool configsetup cline
eggpool configsetup roo-code
eggpool configsetup goose
eggpool configsetup openhands
```

Recommended shared options for every new target:

```text
--host HOST
    Override the detected LAN host used in generated URLs.

--base-url URL
    Override the full generated OpenAI-compatible base URL.
    If provided, this takes precedence over --host and server port detection.

--model MODEL
    Optional default model to place in config when the target needs one.
    If omitted, pick a deterministic best-effort default from catalog only for targets
    that require a default. Otherwise leave model selection to the agent.

--write
    Actually write the target config file where safe and supported.
    Default should print or copy snippets only.

--output PATH
    Write generated config/snippet to a specific path.
    This should imply --write but must still refuse destructive overwrite unless
    --force is also supplied.

--force
    Allow overwriting an existing generated block or file.
    Never use this to clobber an unrelated user file without backup.

--no-clipboard
    Do not attempt clipboard copy.

--print-secret
    Permit printing the API key to stdout when the target config necessarily includes it.
    Default behavior should avoid secret terminal output when possible.
```

Do not require all options to be implemented in the first patch if that would make the change too large. The minimum acceptable first pass is `--write`, `--output`, `--force`, `--host`, and `--base-url` for every new command.

## Target-specific generation details

### 1. Aider

Target command:

```text
eggpool configsetup aider
```

Suggested outputs:

- `.env` snippet using OpenAI-compatible endpoint variables.
- Optional `.aider.conf.yml` snippet if the installed Aider version supports stable config keys.

Primary snippet:

```sh
export OPENAI_API_KEY="<eggpool-key>"
export OPENAI_API_BASE="http://<host>:<port>/v1"
aider --model openai/<model-id>
```

Implementation details:

- Aider commonly works well with OpenAI-compatible endpoints via environment variables.
- Prefer env snippet by default because it is robust across Aider versions.
- If `--write` is passed, write `.env.eggpool` in the current working directory unless `--output` is provided.
- Never overwrite an existing `.env` automatically.
- If `--model` is omitted, print a comment showing how to choose a model from `eggpool models list` or the `/v1/models` endpoint.

Renderer module:

```text
src/eggpool/integrations/aider.py
```

Functions:

```python
def build_aider_env_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
def default_aider_path(cwd: Path) -> Path: ...
```

### 2. Codex CLI

Target command:

```text
eggpool configsetup codex
```

Suggested outputs:

- TOML snippet for Codex provider/profile configuration if the local Codex config format supports custom providers.
- Conservative fallback: shell environment snippet using an OpenAI-compatible API key/base URL only if supported by the installed version.

Implementation details:

- Treat Codex CLI as version-sensitive. Its configuration surface has changed and should not be silently mutated without detection.
- Add best-effort version detection via `shutil.which("codex")` and `codex --version` with a short timeout.
- The first implementation should print a clearly delimited provider/profile snippet and instructions rather than directly editing Codex config by default.
- If `--write` is implemented, write a separate `~/.codex/eggpool-config.toml` or user-specified path rather than merging into the primary config unless a stable merge strategy is verified.
- Use `base_url = http://<host>:<port>/v1` and the EggPool API key.
- Prefer no automatic model default unless `--model` is passed.

Renderer module:

```text
src/eggpool/integrations/codex.py
```

Functions:

```python
def build_codex_toml_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
def detect_codex_version() -> str | None: ...
```

Risk level: medium. Keep write mode conservative.

### 3. Qwen Code

Target command:

```text
eggpool configsetup qwen-code
```

Suggested output:

- JSON or settings snippet for Qwen Code model provider configuration using OpenAI-compatible mode.

Implementation details:

- Generate an OpenAI-compatible provider named `eggpool`.
- Use `baseUrl` or the exact casing expected by the target config surface after verifying against current Qwen Code docs/source.
- Include the EggPool API key.
- Include a default model only if `--model` is passed or if the target requires one.
- If the config file supports multiple profiles/providers, write an isolated EggPool provider block rather than replacing the whole file.
- If `--write` cannot safely merge, write a standalone snippet file and print manual paste instructions.

Renderer module:

```text
src/eggpool/integrations/qwen_code.py
```

Functions:

```python
def build_qwen_code_provider_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
def default_qwen_code_path(home: Path) -> Path: ...
```

Risk level: low-to-medium, depending on config file stability.

### 4. Kilo

Target command:

```text
eggpool configsetup kilo
```

Suggested output:

- OpenAI-compatible provider profile with Base URL, API key, and model discovery.

Implementation details:

- Kilo-style integrations usually want a Base URL and API key.
- Since EggPool exposes `/v1/models`, this should be one of the better targets.
- Prefer a provider block named `EggPool`.
- Preserve the generated `/v1` suffix in `base_url` unless Kilo expects the root and appends `/v1` internally; verify before implementation.
- If Kilo has a CLI-specific config path, support `--write`; otherwise print UI-driven setup instructions plus a machine-readable snippet.

Renderer module:

```text
src/eggpool/integrations/kilo.py
```

Functions:

```python
def build_kilo_openai_compatible_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
```

Risk level: medium because Kilo config details may vary across CLI/editor variants.

### 5. Continue

Target command:

```text
eggpool configsetup continue
```

Suggested output:

- YAML model block for Continue configuration.

Likely shape:

```yaml
models:
  - name: EggPool
    provider: openai
    model: <model-id>
    apiBase: http://<host>:<port>/v1
    apiKey: <eggpool-key>
```

Implementation details:

- Continue is a strong target because it has a declarative config file.
- If `--model` is omitted, either:
  - require `--model` for write mode, or
  - choose a deterministic catalog default and print which model was selected.
- Safer first pass: print a YAML block and write it to `~/.continue/eggpool.yaml` if `--write` is passed.
- Do not mutate the user's main Continue config automatically until a robust YAML merge helper exists.
- If a merge helper is later added, it should preserve comments where possible or at least create a timestamped backup.

Renderer module:

```text
src/eggpool/integrations/continue_dev.py
```

Functions:

```python
def build_continue_yaml_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
```

Risk level: low for snippet generation; medium for direct merge.

### 6. Cline

Target command:

```text
eggpool configsetup cline
```

Suggested output:

- JSON profile or instruction block for Cline's OpenAI-compatible provider UI.

Implementation details:

- Treat this as an extension-profile target rather than a guaranteed file-write target.
- Do not edit VS Code extension global storage in the first implementation.
- Print:
  - provider type: `OpenAI Compatible`
  - base URL: `http://<host>:<port>/v1`
  - API key: copied to clipboard or available through `eggpool getkey`
  - model ID guidance
- If `--write` is passed, write a local `cline-eggpool.json` snippet only; do not inject into VS Code storage.

Renderer module:

```text
src/eggpool/integrations/cline.py
```

Functions:

```python
def build_cline_profile_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
```

Risk level: medium. Avoid direct extension storage mutation.

### 7. Roo Code

Target command:

```text
eggpool configsetup roo-code
```

Suggested output:

- JSON profile or instruction block for Roo Code's OpenAI-compatible provider UI.

Implementation details:

- Similar to Cline.
- Do not assume Roo Code CLI and Roo Code extension share the same config format.
- First pass should generate a UI-fillable profile and optional standalone JSON file.
- Fields:
  - provider: OpenAI Compatible
  - base URL: EggPool `/v1`
  - API key: EggPool key
  - model: optional

Renderer module:

```text
src/eggpool/integrations/roo_code.py
```

Functions:

```python
def build_roo_code_profile_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
```

Risk level: medium. Avoid direct extension storage mutation.

### 8. Goose

Target command:

```text
eggpool configsetup goose
```

Suggested output:

- Goose provider configuration snippet or `goose configure` instructions for OpenAI-compatible provider.

Implementation details:

- Generate a provider named `eggpool`.
- Use the EggPool OpenAI-compatible base URL and API key.
- If Goose supports environment variables cleanly, emit env snippet as the default because it is less brittle than editing Goose config.
- If `--write` is supported, write a standalone Goose config fragment rather than replacing the user's config.
- Model should be optional if Goose can list models; otherwise require or recommend `--model`.

Renderer module:

```text
src/eggpool/integrations/goose.py
```

Functions:

```python
def build_goose_config_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
def build_goose_env_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
```

Risk level: medium because custom OpenAI-compatible protocol strictness should be tested.

### 9. OpenHands

Target command:

```text
eggpool configsetup openhands
```

Suggested output:

- OpenHands LLM config snippet for a custom OpenAI-compatible endpoint.

Implementation details:

- OpenHands is more platform-like than CLI-like, so support it as an advanced target.
- Generate TOML/YAML/env snippet depending on current OpenHands config surface.
- Use:
  - custom model name
  - base URL `http://<host>:<port>/v1`
  - API key
- Require `--model` for write mode unless OpenHands can discover models dynamically in the relevant config path.
- Avoid editing Docker compose files directly in the first pass. Print `.env` or config snippets that can be passed into the OpenHands runtime.

Renderer module:

```text
src/eggpool/integrations/openhands.py
```

Functions:

```python
def build_openhands_config_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
def build_openhands_env_snippet(ctx: IntegrationContext, model: str | None) -> str: ...
```

Risk level: low for snippet generation; medium for direct write because deployments vary.

## Secret-handling policy

Use one consistent policy across all new targets.

Default behavior:

- Generate and persist an EggPool server API key if missing.
- Prefer copying secret-containing snippets to clipboard.
- Print non-secret instructions to stderr.
- Do not print the API key to stdout unless `--print-secret` is passed or the existing command already does so intentionally.
- If clipboard copy fails and `--print-secret` is not passed, print instructions using `eggpool getkey` instead of dumping the key.

For commands that currently print secrets, consider preserving current behavior for backward compatibility but add a follow-up cleanup task to make behavior consistent. Do not break existing `configsetup opencode` workflows without a deprecation note.

## File-write policy

Default mode should remain non-destructive.

`--write` mode rules:

1. Create parent directories as needed.
2. Refuse to overwrite an existing file unless `--force` is passed.
3. If modifying an existing user config becomes necessary, create a backup first:
   `path + ".eggpool.bak.<timestamp>"`.
4. Prefer writing separate `eggpool.*` fragment files over editing primary agent configs.
5. Print the final path written.
6. Do not write into VS Code extension global storage for Cline/Roo in the first implementation.

Suggested default write targets:

```text
Aider:      ./.env.eggpool
Codex:      ~/.codex/eggpool.toml or printed-only until schema verified
Qwen Code:  ~/.qwen/eggpool.json or printed-only until schema verified
Kilo:       ~/.kilo/eggpool.json or printed-only until schema verified
Continue:   ~/.continue/eggpool.yaml
Cline:      ./cline-eggpool.json
Roo Code:   ./roo-code-eggpool.json
Goose:      ~/.config/goose/eggpool.yaml or printed-only until schema verified
OpenHands:  ./openhands-eggpool.env or ./openhands-eggpool.toml
```

Exact paths should be verified against each agent's current documentation/source before implementing write mode. Snippet generation can land first even when write paths remain conservative.

## Model selection policy

Some tools can discover models from `/v1/models`; others want a default model in config.

Implement helper functions:

```python
def list_catalog_model_ids(ctx: IntegrationContext) -> list[str]: ...
def select_default_model(ctx: IntegrationContext) -> str | None: ...
def require_model_for_target(target: str, model: str | None, ctx: IntegrationContext) -> str | None: ...
```

Default model selection should be conservative:

1. Use `--model` if passed.
2. If the target requires a model and the catalog has exactly one model, use it.
3. If the catalog has multiple models, do not guess silently. Print a clear error with examples and ask the operator to pass `--model`.
4. If the target supports dynamic model discovery, omit the model when not supplied.

This avoids accidentally pinning users to a low-quality, expensive, or provider-suffixed model.

## Transcoder policy

Most of these targets will connect through EggPool's OpenAI-compatible `/v1` surface. If the user's configured providers include Anthropic-only accounts, the current OpenCode path enables `[transcoder].enabled = true` so OpenAI-compatible clients can use those routes.

Apply the same policy to all OpenAI-compatible configsetup targets:

- If any enabled provider is Anthropic-only and the target uses OpenAI-compatible requests, enable the transcoder.
- If enabling the transcoder fails, abort before emitting a config that appears valid but cannot route to those providers.
- If the config was mutated, call `_restart_after_configsetup_mutation(config_path)` from the CLI wrapper.

## Refactor steps

### Step 1: Extract current shared OpenCode setup logic

Move the following from `configsetup_opencode` into `src/eggpool/integrations/common.py` or a nearby helper:

- key read/generate/persist logic
- port/LAN IP/base URL construction
- catalog DB loading
- static model fallback population
- effective limits resolution
- conservative limit merging
- transcoder enablement decision

Keep behavior identical for `configsetup opencode` after the refactor.

Acceptance criteria:

- `eggpool configsetup opencode` output is byte-for-byte equivalent except for intentionally improved stderr wording.
- Existing tests pass.
- Add regression tests around no-key and existing-key behavior.

### Step 2: Add shared renderer/write utilities

Add helpers for:

- JSON rendering with deterministic key ordering where appropriate.
- YAML-ish rendering without adding a heavy dependency unless the repo already has one.
- shell snippet quoting via `shlex.quote()` or explicit safe double-quote escaping.
- safe file writing with overwrite refusal and optional backup.
- clipboard result handling without repeating the same code in every command.

Suggested module:

```text
src/eggpool/integrations/rendering.py
```

or keep it in `common.py` if small.

Acceptance criteria:

- New helpers have focused unit tests.
- Existing `_write_file()` can either delegate to the new helper or remain for deploy snippets if behavior differs.

### Step 3: Add low-risk targets first

Implement:

- `aider`
- `continue`
- `openhands` env/snippet mode
- `goose` env/snippet mode

These can be implemented mostly as deterministic text renderers and are less likely to require unsafe config mutation.

Acceptance criteria:

- Each command works without catalog data.
- Each command works with catalog data.
- Each command handles missing server API key by generating and persisting it.
- Each command supports `--base-url`, `--host`, `--model`, `--write`, `--output`, `--force`, `--no-clipboard`, and `--print-secret` where applicable.
- Secret-containing snippets are not printed by default unless already consistent with command semantics.

### Step 4: Add medium-risk CLI/config targets

Implement:

- `qwen-code`
- `kilo`
- `codex`

Before this step, verify each current config schema against upstream docs/source. For version-sensitive tools, prefer printed snippets and standalone fragment files.

Acceptance criteria:

- Commands are useful without mutating primary user config.
- `--write` writes only standalone fragment files unless a safe merge strategy is explicitly implemented.
- Codex command reports detected Codex version when available.
- Codex command does not claim full automatic setup when the installed version's config format is unknown.

### Step 5: Add editor-extension targets

Implement:

- `cline`
- `roo-code`

Acceptance criteria:

- Commands emit clear provider UI values and optional standalone JSON profile snippets.
- They do not mutate VS Code extension storage.
- They include model selection guidance.
- They document that the user may need to paste values into the extension UI depending on extension version.

### Step 6: Documentation

Update:

```text
README.md
```

and, if present or worth adding:

```text
docs/integrations.md
docs/configsetup.md
```

Documentation should include:

- supported targets table
- whether the target is fully automated, fragment-write only, or instructions-only
- default output path if `--write` is used
- whether `--model` is required/recommended
- secret-handling behavior
- troubleshooting notes for LAN IP/base URL overrides
- examples:

```sh
eggpool configsetup aider --model openai/gpt-oss-120b --write

eggpool configsetup continue --model minimax-m3/opencode-go --output ~/.continue/eggpool.yaml

eggpool configsetup cline --model claude-sonnet-4/opencode-go --no-clipboard
```

Use realistic model examples from EggPool model listing behavior, but avoid hard-coding provider-specific examples that may not exist for every user.

### Step 7: Tests

Add tests in a new or existing CLI test module. Suggested names:

```text
tests/test_configsetup_integrations.py
tests/test_integrations_aider.py
tests/test_integrations_continue.py
tests/test_integrations_agent_snippets.py
```

Test categories:

1. Shared context
   - existing API key is reused
   - missing API key is generated and persisted
   - persistence failure aborts
   - host/base-url override behavior
   - catalog missing warning path
   - static model fallback behavior

2. Renderers
   - Aider env snippet has expected variables
   - Continue YAML contains provider/model/apiBase/apiKey
   - OpenHands env snippet contains expected values
   - Cline/Roo snippets include provider, base URL, and optional model
   - JSON outputs are deterministic

3. CLI behavior
   - each command exits 0 with minimal config
   - `--write` creates the expected file
   - existing output path refuses overwrite without `--force`
   - `--force` overwrites
   - `--print-secret` controls terminal secret output where applicable
   - `--no-clipboard` suppresses clipboard attempt

4. Regression
   - existing `opencode` command still emits the same provider structure
   - existing `claude-code` command still works
   - transcoder auto-enable remains correct

### Step 8: Manual verification matrix

Use a local EggPool instance with at least one OpenAI-compatible provider and one Anthropic-only provider.

Verify:

```sh
eggpool check-config
eggpool models refresh
eggpool configsetup opencode
eggpool configsetup claude-code
eggpool configsetup aider --model <known-model>
eggpool configsetup continue --model <known-model>
eggpool configsetup qwen-code --model <known-model>
eggpool configsetup kilo --model <known-model>
eggpool configsetup goose --model <known-model>
eggpool configsetup openhands --model <known-model>
eggpool configsetup cline --model <known-model>
eggpool configsetup roo-code --model <known-model>
eggpool configsetup codex --model <known-model>
```

For each tool that is locally installed, perform at least one real request through EggPool and confirm:

- request reaches EggPool
- request is authenticated by EggPool server key
- model name arrives as expected
- provider routing works
- tool-call payloads are either passed through or rejected predictably
- streaming works if the agent uses streaming
- errors from upstream are surfaced clearly

## Implementation cautions

- Do not add a large dependency solely for YAML unless there is a clear need. Simple YAML snippets can be rendered manually if they are small and deterministic.
- Do not mutate user config for VS Code extensions in the first implementation.
- Do not assume every target wants `/v1`; some clients may ask for the root URL and append `/v1` internally. Verify before finalizing each renderer.
- Do not silently choose a model from a large catalog.
- Do not print API keys to stdout by default for new commands.
- Keep `cli_full.py` from growing substantially. The command body should be thin.
- Preserve current behavior for existing commands unless explicitly changing it with tests and release notes.

## Proposed first patch boundary

A good first PR should include:

1. shared `IntegrationContext`
2. OpenCode refactor onto the shared context
3. Aider renderer and command
4. Continue renderer and command
5. tests for shared context, Aider, Continue, and OpenCode regression
6. documentation table with the remaining targets marked planned

A good second PR should add:

1. OpenHands
2. Goose
3. Qwen Code
4. Kilo
5. Codex conservative snippet mode

A good third PR should add:

1. Cline
2. Roo Code
3. direct write refinements where verified
4. manual compatibility notes from real agent testing

## Success criteria

The work is complete when:

- `eggpool configsetup --help` lists all requested targets.
- Every target can generate a useful snippet without requiring the agent to be installed.
- Safe targets can write standalone config fragments with `--write`.
- Unsafe or version-sensitive targets clearly state what is automated and what remains manual.
- Missing server API keys are generated and persisted before snippets are emitted.
- OpenAI-compatible targets enable the transcoder when needed.
- Existing OpenCode and Claude Code setup flows continue to work.
- Unit tests cover the shared context, renderer output, safe write behavior, and existing command regressions.
