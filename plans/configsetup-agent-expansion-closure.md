# Plan: configsetup agent expansion closure pass

## Objective

Close the first implementation pass for expanded `eggpool configsetup` support. The current repo has good breadth: commands and renderers exist for Aider, Codex, Qwen Code, Kilo, Continue, Cline, Roo Code, Goose, and OpenHands. The closure pass should make the implementation correct, durable, and defensible before release.

This pass is not primarily about adding more targets. It is about fixing correctness bugs in shared setup, hardening secret handling, making generated snippets syntactically valid, and tightening tests so they catch the failure modes that matter.

## Current state summary

Since the original plan, `main` added:

- `src/eggpool/integrations/common.py`
- `src/eggpool/integrations/aider.py`
- `src/eggpool/integrations/codex.py`
- `src/eggpool/integrations/qwen_code.py`
- `src/eggpool/integrations/kilo.py`
- `src/eggpool/integrations/continue_dev.py`
- `src/eggpool/integrations/cline.py`
- `src/eggpool/integrations/roo_code.py`
- `src/eggpool/integrations/goose.py`
- `src/eggpool/integrations/openhands.py`
- broad CLI wiring in `src/eggpool/cli_full.py`
- `src/eggpool/config_utils.py`
- substantial unit and integration tests

The direction is correct: shared integration context exists, the CLI has common options, and snippets can be produced for all requested agents. The remaining work is concentrated around correctness and schema maturity.

## Blocking issues to fix

### 1. Transcoder enablement is currently not persisted

The old OpenCode configsetup path wrote `[transcoder].enabled = true` back to the TOML when OpenCode needed the OpenAI-compatible transcoder to reach Anthropic-only providers. The new shared path only mutates the loaded `AppConfig` object in memory:

```python
config.transcoder.enabled = True
```

That change does not persist to `config.toml`. As a result, generated configs for OpenAI-compatible clients can look valid while EggPool remains unable to route to Anthropic-only providers through the OpenAI surface.

Required fix:

- Move transcoder persistence into `src/eggpool/integrations/common.py` or a small shared config mutation helper.
- Use the existing TOML editing utility pattern instead of mutating `AppConfig` in memory.
- Write `[transcoder].enabled = true` when all of the following are true:
  - the target uses EggPool's OpenAI-compatible `/v1` surface;
  - at least one enabled provider/account is Anthropic-only or otherwise requires transcoding for OpenAI-compatible clients;
  - `[transcoder].enabled` is currently false.
- Return `transcoder_mutated=True` only when the TOML file was actually changed.
- Fail loudly if a config file mutation is required but cannot be written.

Implementation sketch:

```python
def persist_transcoder_enabled(config_path: str, config: AppConfig) -> bool:
    if config.transcoder.enabled:
        return False
    if not openai_client_needs_transcoder(config):
        return False

    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    result = update_section_value(
        lines,
        "transcoder",
        "enabled",
        "true",
        insert_missing_key=True,
        append_missing_section=True,
    )
    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")
    return True
```

Do not simply enable transcoder for every configsetup invocation. Preserve the decision logic that only enables it when OpenAI-compatible clients would otherwise miss reachable providers.

Required CLI change:

- Restart/apply guidance must trigger on `ctx_data.config_mutated or ctx_data.transcoder_mutated`.
- The helper should avoid calling restart twice when both key generation and transcoder persistence happened.

Suggested helper:

```python
def _restart_after_integration_context_mutation(config_path: str, ctx_data: IntegrationContext) -> None:
    if ctx_data.config_mutated or ctx_data.transcoder_mutated:
        _restart_after_configsetup_mutation(config_path)
```

Use that helper in `opencode` and all new configsetup commands.

Acceptance criteria:

- A config with `[transcoder].enabled = false` and an enabled Anthropic-only provider is changed to `true` after running an OpenAI-compatible configsetup target.
- The server restart guidance appears when transcoder was changed, even if no API key was generated.
- A config that already has transcoder enabled is not rewritten.
- A config with only OpenAI-compatible providers does not enable transcoder unnecessarily.

### 2. `api_key_env` can generate unusable client configs

`config_utils.write_server_api_key()` returns success with a warning when `[server].api_key_env` exists, but it does not write the generated inline key. `build_integration_context()` currently ignores that warning and continues using the newly generated key. If the server uses an environment-provided key, the generated agent config may contain a key that EggPool will not accept.

Required fix:

- Treat `[server].api_key_env` as a first-class case.
- Prefer resolving the effective key from `AppConfig.server.resolved_api_key` if available.
- If the env var is configured but not present in the current process, abort with a clear error:

```text
[server].api_key_env is set to EGGPOOL_API_KEY, but that environment variable is not available to this process. Export it before running configsetup, or run eggpool newkey to switch to an inline key.
```

- Do not generate a throwaway key when `api_key_env` is configured.
- Do not print or write snippets containing a key that is not actually accepted by the server.

Implementation details:

- Add a utility that returns structured key resolution metadata, not just a string:

```python
@dataclass(frozen=True)
class ServerKeyResolution:
    api_key: str
    source: Literal["inline", "env", "generated"]
    env_var: str | None = None
    config_mutated: bool = False
```

- Update `build_integration_context()` to use this helper.
- Keep `eggpool getkey` behavior consistent with this policy. If `getkey` currently only reads inline keys, decide whether to extend it to resolved env keys or print a clear env-var-specific message. Prefer extending it to resolved env keys because configsetup and getkey should agree.

Acceptance criteria:

- Inline `api_key` is reused.
- Missing inline key generates and persists a new key.
- `api_key_env` with env var present uses the env var value and does not mutate config.
- `api_key_env` with env var absent exits non-zero and emits a clear error.
- No configsetup command emits a generated-but-unaccepted key.

### 3. TOML emitted by Codex can be syntactically invalid

The current Codex renderer emits table names containing raw model IDs:

```toml
[provider.eggpool.models.gpt-4o/openai]
```

Model IDs can contain `/`, `.`, `:`, `-`, spaces, and provider suffixes. TOML dotted table segments must be valid bare keys or quoted. The current output is not safe.

Required fix:

Choose one of these safe structures:

Option A: quoted table segments.

```toml
[provider.eggpool.models."gpt-4o/openai"]
context_window = 128000
```

Option B: array of model tables.

```toml
[[provider.eggpool.models]]
id = "gpt-4o/openai"
context_window = 128000
```

Option B is usually safer and more schema-flexible unless Codex explicitly requires a keyed table.

Implementation details:

- Add a TOML string/key rendering helper or use existing `render_toml_string()` for values.
- Avoid hand-concatenating unescaped model IDs into table headers.
- Add tests that parse generated TOML with `tomllib.loads()`.
- Include model IDs with `/`, `.`, `:`, and spaces in the parseability tests.

Acceptance criteria:

- Codex snippets with provider-suffixed model IDs parse successfully via `tomllib.loads()`.
- Arbitrary EggPool model IDs do not break TOML syntax.
- Tests no longer assert invalid raw table headers.

### 4. Continue YAML rendering has malformed list indentation

The current Continue renderer constructs:

```python
return f"models:\n- {_render_yaml_dict(props)}"
```

`_render_yaml_dict()` returns multiple lines already indented by two spaces. Prefixing only the first line with `- ` produces a fragile shape:

```yaml
models:
-   title: EggPool
  provider: openai
  model: ...
```

This might work inconsistently or be rejected depending on YAML parser expectations and exact whitespace.

Required fix:

Render a conventional YAML list block:

```yaml
models:
  - title: EggPool
    provider: openai
    model: "..."
    apiBase: "http://host:port/v1"
    apiKey: "..."
```

Implementation details:

- Update the manual YAML renderer to handle list item indentation explicitly.
- Add a parser test. If PyYAML is not a dependency, use a minimal structural assertion plus optional test guarded by import availability. Do not add a runtime dependency solely for tests unless the project already accepts test-only dependencies.
- Ensure empty ambiguous model is either omitted or explicitly rendered as `model: ""` with a CLI warning. Prefer omitting `model` unless Continue requires it.

Acceptance criteria:

- Continue snippet has conventional YAML indentation.
- `apiBase` and `apiKey` remain quoted safely.
- Ambiguous model selection does not silently create a misleading empty model in write mode.

### 5. Model selection policy is too permissive for write mode

The plan called for conservative model selection. The current helpers return `None` when ambiguous, but most renderers then emit snippets without a model. That can be acceptable for dynamic-discovery clients, but it is not acceptable for targets that require a model to run.

Required fix:

- Define target capability metadata in one place:

```python
@dataclass(frozen=True)
class ConfigsetupTargetSpec:
    name: str
    requires_model: bool
    supports_dynamic_models: bool
    supports_direct_write: bool
    default_write_path: str | None
    mode: Literal["env", "json", "toml", "yaml", "instructions"]
```

- For targets that require a model and cannot discover dynamically, fail in `--write` mode if the catalog has multiple models and `--model` was not supplied.
- For snippet-only mode, print a clear warning instead of silently emitting an empty/defaultless model field.

Suggested initial classification:

- Aider: model recommended, not strictly required for env-only snippet; require for one-shot command examples.
- Continue: model required/recommended; fail in write mode if ambiguous.
- OpenHands: model required/recommended; fail in write mode if ambiguous.
- Codex: version-sensitive; do not claim direct correctness without explicit model unless installed schema supports discovery.
- Kilo: can often discover `/v1/models`; model optional.
- Qwen Code: model optional only if target supports selection/discovery; otherwise require for write mode.
- Cline/Roo Code: model recommended; snippet can omit because user can paste/select manually.
- Goose: model recommended; require for write mode unless verified dynamic discovery works.

Acceptance criteria:

- Write mode never emits an unusable config for model-required targets without warning or failure.
- Ambiguous model cases have deterministic, explicit behavior.
- Tests cover one model, many models, and no model.

## Secondary issues to fix

### 6. Secret-output detection is heuristic and incomplete

`_output_snippet()` detects secret-containing snippets by searching for strings like `api_key`, `apikey`, and `apiKey`. This can miss env vars such as `OPENAI_API_KEY`, `LLM_API_KEY`, or `GOOSE_PROVIDER__API_KEY` depending on case and spelling.

Required fix:

- Stop inferring secret presence from snippet text.
- Renderer functions or command wrappers should pass `contains_secret=True` explicitly.
- Default all current configsetup snippets to `contains_secret=True` because they include the EggPool key.

Implementation sketch:

```python
def _output_snippet(..., contains_secret: bool = True) -> None:
    ...
```

Acceptance criteria:

- No secret-containing snippet is printed by default merely because the heuristic missed the key spelling.
- `--print-secret` remains the explicit opt-in to terminal output for new commands.
- Existing OpenCode behavior can remain backward-compatible for now, but document it as a legacy exception if unchanged.

### 7. File write behavior should create backups when overwriting

The current `_output_snippet()` refuses overwrite unless `--force` is passed. With `--force`, it overwrites directly. The original plan preferred backups for existing user files.

Required fix:

- If target exists and `--force` is passed, create a timestamped backup before replacing it.
- Skip backup only if the existing file content is identical.
- Print backup path to stderr.

Suggested backup format:

```text
<target>.eggpool.bak.20260629T213455
```

Acceptance criteria:

- `--force` does not destroy the only copy of an existing config fragment.
- Tests verify backup creation and no-backup-on-identical-content.

### 8. Host/base-url override validation is weak

`--base-url` is accepted as any string. A malformed URL can silently produce unusable configs.

Required fix:

- Validate `--base-url` with `urllib.parse.urlparse()`.
- Require scheme and netloc.
- Warn or normalize trailing slash.
- For OpenAI-compatible clients, require or append `/v1` only if the target expects `/v1`.
- Keep `base_url_root` consistent.

Acceptance criteria:

- `--base-url not-a-url` exits non-zero.
- `--base-url http://host:11300/v1` works.
- `--base-url http://host:11300` either appends `/v1` or clearly documents target-specific root behavior.

### 9. Renderer schemas need target-specific validation notes

Some current renderers are plausible but not fully verified against live target config schemas. This is acceptable only if the CLI and docs distinguish between:

- fully automated config;
- safe standalone fragment;
- paste-this-into-UI profile;
- experimental/version-sensitive snippet.

Required fix:

- Add per-target status metadata to docs and possibly CLI help output.
- For Codex, explicitly mark as version-sensitive until verified against current Codex config schema.
- For Cline/Roo Code, explicitly mark as UI/profile snippet only; do not imply direct extension config mutation.
- For Qwen Code and Kilo, verify field names against current upstream docs/source before claiming `--write` as safe.

Acceptance criteria:

- README or docs table accurately states automation level for each target.
- CLI output includes paste/write guidance specific to each target.
- No target claims direct setup if the code only emits a generic fragment.

### 10. Duplicate `_transaction_owner` definition in `Database.__init__`

`Database.__init__` currently assigns `_transaction_owner` twice. This is probably harmless but should be cleaned up.

Required fix:

- Remove the earlier duplicate assignment.
- Keep the assignment with the better explanatory comment.
- Run DB transaction tests afterward.

Acceptance criteria:

- Only one `_transaction_owner` assignment remains.
- Existing DB transaction tests still pass.

## Test plan

### Unit tests: shared integration context

Add or update tests for:

- inline API key reuse;
- missing inline key generation and persistence;
- `api_key_env` with env var present;
- `api_key_env` with env var absent;
- transcoder persistence when required;
- no transcoder persistence when not required;
- restart/apply flag behavior when only transcoder changed;
- base URL override validation;
- host override behavior;
- catalog static model merge still works.

### Unit tests: renderers

Add parseability tests:

- Codex TOML parses with `tomllib.loads()` for model IDs containing `/`, `.`, `:`, `-`, and spaces.
- Continue YAML has conventional indentation. If no YAML parser is available, assert exact line prefixes.
- JSON renderers parse with `json.loads()` and include expected fields.
- Shell env snippets use safe quoting for values with spaces or shell-sensitive characters.

### CLI tests

Add tests for:

- every configsetup command exits zero with inline key;
- every configsetup command refuses absent `api_key_env`;
- every configsetup command honors `--base-url`;
- write mode creates files;
- write mode refuses overwrite without `--force`;
- write mode with `--force` creates backup;
- `--print-secret` gates stdout secrets;
- no clipboard path still provides useful non-secret instructions;
- model-required targets fail or warn correctly when ambiguous.

### Regression tests

Preserve/strengthen tests for:

- existing `configsetup opencode` provider JSON shape;
- existing `configsetup claude-code` behavior;
- OpenCode still includes catalog-derived model limits;
- OpenCode still handles collapsed/non-collapsed model IDs correctly;
- transcoder default behavior from integration tests.

## Manual verification matrix

After unit tests pass, verify manually against a local EggPool instance.

Minimum commands:

```sh
eggpool check-config
eggpool models refresh
eggpool configsetup opencode --no-clipboard
eggpool configsetup claude-code
eggpool configsetup aider --model <known-model> --no-clipboard --print-secret
eggpool configsetup codex --model <known-model> --no-clipboard --print-secret
eggpool configsetup qwen-code --model <known-model> --no-clipboard --print-secret
eggpool configsetup kilo --model <known-model> --no-clipboard --print-secret
eggpool configsetup continue --model <known-model> --no-clipboard --print-secret
eggpool configsetup cline --model <known-model> --no-clipboard --print-secret
eggpool configsetup roo-code --model <known-model> --no-clipboard --print-secret
eggpool configsetup goose --model <known-model> --no-clipboard --print-secret
eggpool configsetup openhands --model <known-model> --no-clipboard --print-secret
```

Specific manual cases:

1. Inline key config.
2. `api_key_env` config with env var exported.
3. `api_key_env` config without env var exported.
4. Anthropic-only provider with transcoder disabled.
5. OpenAI-compatible-only provider with transcoder disabled.
6. Model catalog populated.
7. Model catalog empty.
8. Multiple providers exposing the same base model with provider-suffixed IDs.

For any locally installed target agent, perform a real request through EggPool and confirm:

- request reaches EggPool;
- authentication succeeds;
- selected model ID arrives as expected;
- streaming works if the agent streams;
- tool-call payload behavior is acceptable;
- upstream errors are surfaced intelligibly.

## Documentation updates

Update README and/or integration docs with a table like:

```text
Target       Mode                 Write support       Model needed       Status
Aider        env snippet           .env.eggpool        recommended        stable
Codex        TOML fragment         fragment only       recommended        version-sensitive
Qwen Code    JSON fragment         fragment only       optional?          verify schema
Kilo         JSON fragment         fragment only       optional?          verify schema
Continue     YAML fragment         ~/.continue/...     usually yes        stable fragment
Cline        UI JSON profile       local fragment      recommended        paste into UI
Roo Code     UI JSON profile       local fragment      recommended        paste into UI
Goose        env snippet           fragment only       recommended        verify env vars
OpenHands    env snippet           local env file      usually yes        stable fragment
```

Do not overstate automation. A direct `--write` fragment is not the same as editing an agent's primary config and making it ready to run.

## Suggested patch order

### Patch 1: correctness blockers

- Fix `api_key_env` key resolution.
- Persist transcoder enablement correctly.
- Restart/apply on either key or transcoder mutation.
- Add tests for both.

### Patch 2: syntax and rendering safety

- Fix Codex TOML structure.
- Fix Continue YAML indentation.
- Replace heuristic secret detection with explicit `contains_secret`.
- Add parser/structure tests.

### Patch 3: write-mode hardening

- Add backups on `--force` overwrite.
- Validate `--base-url`.
- Add model-required target behavior.
- Add CLI tests for write/force/override/model ambiguity.

### Patch 4: docs and cleanup

- Update docs target-status table.
- Clarify fragment vs full automation.
- Clean duplicate `_transaction_owner` assignment.
- Run full test suite and targeted manual smoke tests.

## Success criteria

This closure pass is complete when:

- Configsetup never emits an unusable generated key for `api_key_env` configs.
- Transcoder enablement is persisted when required and restart guidance is shown.
- Codex TOML snippets parse successfully for real EggPool model IDs.
- Continue YAML snippets are structurally sane.
- Secret printing is controlled explicitly, not by text heuristics.
- Write mode is non-destructive and creates backups on forced overwrite.
- Ambiguous model selection has explicit target-specific behavior.
- Tests cover the previously missed failure modes.
- Documentation accurately describes each target's automation level.
