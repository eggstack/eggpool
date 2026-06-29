# Plan: final fix pass for expanded `configsetup` support

## Objective

Close the remaining correctness gaps after the first closure patch for expanded coding-agent config generation. The current code now has broad target coverage and several good fixes, but still appears to contain a likely runtime `NameError`, an incomplete transcoder persistence path, inconsistent `api_key_env` handling for `claude-code`, and model-required metadata that is present but not enforced.

This pass should be small, surgical, and test-driven.

## Current state

The previous patch addressed several important items:

- `resolve_server_api_key()` now distinguishes inline, env-var, and generated server API keys.
- New shared integration setup uses `resolve_server_api_key()`.
- Codex TOML now quotes model IDs with unsafe table-key characters.
- Continue YAML output now uses conventional list indentation and omits ambiguous empty model values.
- `--base-url` gets basic URL validation.
- `_output_snippet()` accepts explicit `contains_secret` and creates backups on forced overwrite.
- Duplicate `_transaction_owner` assignment appears to have been removed.

Remaining issues are concentrated and should not require broad redesign.

## Blocking issue 1: `_restart_after_integration_context_mutation` is called but not defined

### Problem

Several configsetup commands now call:

```python
_restart_after_integration_context_mutation(config_path, ctx_data)
```

`configsetup opencode` also calls it when `ctx_data.config_mutated or ctx_data.transcoder_mutated` is true. I could not find the function definition in `src/eggpool/cli_full.py`. If it is actually absent, any configsetup command that reaches that line will raise `NameError` after emitting or writing the snippet.

### Required fix

Add a small helper near `_restart_after_configsetup_mutation()` or near the configsetup helpers:

```python
def _restart_after_integration_context_mutation(
    config_path: str,
    ctx_data: Any,
) -> None:
    """Restart/apply guidance after configsetup mutates EggPool config."""
    if getattr(ctx_data, "config_mutated", False) or getattr(
        ctx_data, "transcoder_mutated", False
    ):
        _restart_after_configsetup_mutation(config_path)
```

Prefer importing `IntegrationContext` under `TYPE_CHECKING` and using a real annotation if clean:

```python
if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext
```

Then:

```python
def _restart_after_integration_context_mutation(
    config_path: str,
    ctx_data: IntegrationContext,
) -> None:
    ...
```

### Acceptance criteria

- `eggpool configsetup aider --no-clipboard --print-secret` exits 0 with an existing inline key.
- The same command exits 0 when it has to generate a new server key.
- A command that triggers `transcoder_mutated=True` exits 0 and prints restart/apply guidance.
- No configsetup command raises `NameError` for the helper.

## Blocking issue 2: missing `[transcoder]` section is not persisted

### Problem

`_persist_transcoder_enabled()` calls `update_section_value(... append_missing_section=True)`, but then returns `False` without writing when both `result.section_found` and `result.key_found` are false.

That condition is exactly what happens when `[transcoder]` was absent and `update_section_value()` appended it. The current helper therefore discards the appended TOML and fails to persist transcoder enablement for missing-section configs.

### Required fix

Update `_persist_transcoder_enabled()` so it writes whenever `result.lines` differs from the original lines or whenever append/update was requested and produced a changed document.

Suggested implementation:

```python
def _persist_transcoder_enabled(config_path: str, config: AppConfig) -> bool:
    if config.transcoder.enabled:
        return False
    if not _openai_client_needs_transcoder(config):
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
    if result.lines == lines:
        raise OSError(
            f"Failed to persist [transcoder].enabled = true to {config_path}"
        )
    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")
    return True
```

Do not rely on `section_found`/`key_found` for append detection. Those flags describe the original file, not whether the returned lines changed.

### Acceptance criteria

- Config with no `[transcoder]` section gets a new section appended.
- Config with `[transcoder]` but no `enabled` key gets `enabled = true` inserted.
- Config with `[transcoder].enabled = false` gets updated to true.
- Config with `[transcoder].enabled = true` is not rewritten.
- If `_openai_client_needs_transcoder(config)` is false, no file mutation happens.

## Blocking issue 3: `claude-code` still bypasses resolved server key handling

### Problem

`configsetup claude-code` still uses `_read_server_api_key()` and `write_server_api_key()` directly. This keeps the old `api_key_env` failure mode alive for Claude Code while the new shared commands use `resolve_server_api_key()`.

If `[server].api_key_env` is configured, the command can still emit an empty, generated, or non-authoritative key rather than the actual env-provided key.

### Required fix

Port `configsetup_claude_code()` to the same key resolution path used by integration context.

Minimal approach:

```python
from eggpool.config_utils import resolve_server_api_key

try:
    key_resolution = resolve_server_api_key(config_path)
except (OSError, SystemExit) as exc:
    click.echo(f"Error: {exc}", err=True)
    sys.exit(1)

key = key_resolution.api_key
if key_resolution.config_mutated:
    click.echo("Generated new server API key.", err=True)
```

Better approach:

- Add a small shared helper in `cli_full.py` that resolves key and emits consistent messages.
- Use it from `claude-code` and, if practical, the shared integration context error path.

### Acceptance criteria

- `claude-code` uses inline key when present.
- `claude-code` uses env var value when `api_key_env` is configured and present.
- `claude-code` exits non-zero with a clear error when `api_key_env` is configured but absent.
- `claude-code` generates and persists a key only when neither inline key nor env key is configured.

## Issue 4: `SystemExit` from key resolution is an awkward library boundary

### Problem

`resolve_server_api_key()` is in `config_utils.py`, but it raises `SystemExit` for absent env vars. That couples a library/helper module to CLI process control and makes error handling inconsistent. `_build_ctx_with_overrides()` catches `OSError` only, so `SystemExit` bypasses the normal `Error: ...` formatting.

### Required fix

Introduce a domain-specific exception or reuse an existing config error type.

Preferred:

```python
class ConfigKeyResolutionError(RuntimeError):
    pass
```

or use existing `ConfigError` if appropriate.

Then:

```python
raise ConfigKeyResolutionError(
    "[server].api_key_env is set to ..."
)
```

Update callers:

- `build_integration_context()` should allow the exception to propagate or wrap it consistently.
- `_build_ctx_with_overrides()` should catch it and print `Error: ...`.
- `configsetup_claude_code()` should catch it similarly.
- Tests should assert non-zero CLI exit and clear error text.

### Acceptance criteria

- No non-entrypoint config utility raises `SystemExit`.
- CLI errors remain user-readable and non-zero.
- Unit tests can call `resolve_server_api_key()` directly and assert a normal exception.

## Issue 5: `TARGET_SPECS` exists but model-required behavior is not enforced

### Problem

`TARGET_SPECS` marks Codex, Continue, Goose, and OpenHands as requiring models, but `require_model_for_target()` ignores target specs and only returns explicit model, single-catalog model, or `None`.

This leaves metadata present but ineffective. It is better either to enforce it or remove it. Since the plan intended enforcement, implement enforcement in the CLI layer where write/snippet mode is known.

### Required fix

Add a helper that can see `do_write` and the target:

```python
def _resolve_model_for_configsetup(
    target: str,
    explicit_model: str | None,
    ctx_data: IntegrationContext,
    *,
    do_write: bool,
) -> str | None:
    from eggpool.integrations.common import TARGET_SPECS, require_model_for_target

    resolved = require_model_for_target(target, explicit_model, ctx_data)
    spec = TARGET_SPECS.get(target)
    if spec is None:
        return resolved

    if spec.requires_model and resolved is None:
        message = (
            f"{target} config works best with an explicit model. "
            "Pass --model <model-id>."
        )
        if do_write:
            click.echo(f"Error: {message}", err=True)
            sys.exit(1)
        click.echo(f"Warning: {message}", err=True)
    return resolved
```

Use this helper in all configsetup commands instead of direct `require_model_for_target()` calls.

### Acceptance criteria

- In snippet mode, model-required targets emit a warning when model is ambiguous or unavailable.
- In write mode, model-required targets fail when model is ambiguous or unavailable.
- Non-required targets keep current behavior.
- Tests cover Codex/Continue/Goose/OpenHands write mode without model and many-model catalog.

## Issue 6: Codex TOML quoting is improved but still not robust for quotes/backslashes

### Problem

The Codex renderer now quotes model IDs containing `/`, `.`, `:`, or spaces by embedding the raw ID between double quotes. That handles common provider-suffixed IDs but does not escape quotes or backslashes in arbitrary model IDs.

### Required fix

Use `render_toml_string()` for quoted TOML key segments, or add `render_toml_key_segment()` that returns either a bare key or a properly escaped quoted key.

Suggested helper:

```python
def _toml_key_segment(value: str) -> str:
    bare_allowed = re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
    if bare_allowed:
        return value
    return render_toml_string(value)
```

Then:

```python
table_key = _toml_key_segment(mid)
lines.append(f"[provider.eggpool.models.{table_key}]")
```

### Acceptance criteria

- TOML parses for model IDs containing `/`, `.`, `:`, spaces, quotes, and backslashes.
- Test uses `tomllib.loads(snippet)` instead of only substring assertions.

## Issue 7: backup path suffix is a little awkward

### Problem

`target.with_suffix(f".eggpool.bak.{ts}{target.suffix}")` turns `foo.json` into something like `foo.eggpool.bak.TIMESTAMP.json`. That is acceptable, but the original plan suggested `foo.json.eggpool.bak.TIMESTAMP`, which more clearly preserves the original full filename.

This is not blocking, but if touched, simplify to:

```python
backup = target.with_name(f"{target.name}.eggpool.bak.{ts}")
```

### Acceptance criteria

- Forced overwrite creates a backup beside the target.
- Backup path is deterministic enough for tests via glob.
- Identical content does not create a backup.

## Tests to add or strengthen

### Runtime helper coverage

Add a test that runs each configsetup command with an existing inline key and verifies exit code 0. This likely already exists, but make sure it would catch the missing restart helper by using a config that forces mutation:

- no inline key, so key generation occurs;
- or Anthropic-only provider with transcoder disabled, so `transcoder_mutated=True` occurs.

### Transcoder persistence tests

Add unit tests around `_persist_transcoder_enabled()` or integration-context behavior:

1. missing `[transcoder]` section -> appended and `transcoder_mutated=True`;
2. existing `[transcoder] enabled = false` -> changed and `transcoder_mutated=True`;
3. existing enabled true -> unchanged and `transcoder_mutated=False`;
4. no Anthropic-only providers -> unchanged.

If constructing a full `AppConfig` with providers is cumbersome, use a minimal TOML config fixture with static provider/account blocks that matches existing config parser expectations.

### API-key env tests

For `resolve_server_api_key()`:

- inline key wins;
- env key is used when `api_key_env` is set and env var exists;
- normal exception is raised when env var is absent;
- generation happens only when neither inline nor env key is configured.

For CLI:

- `configsetup claude-code` works with `api_key_env` present;
- `configsetup claude-code` fails with `api_key_env` absent;
- at least one shared target, such as `aider`, behaves the same.

### Model enforcement tests

Use a many-model `IntegrationContext` or CLI fixture. Verify:

- `continue --write` without `--model` exits non-zero when ambiguous;
- `continue` snippet mode without `--model` emits warning but exits zero;
- `kilo --write` without `--model` still exits zero if classified as dynamic/optional;
- explicit `--model` always passes through.

### TOML parse tests

Use `tomllib.loads()` against Codex snippets with these model IDs:

```text
gpt-4o/openai
provider.model:v1
model with spaces
model"quote
model\\slash
```

The test should fail if model IDs are interpolated into table headers without proper escaping.

### Write-mode backup tests

Verify:

- existing file + no `--force` exits non-zero;
- existing file + `--force` writes new file and creates backup;
- existing identical file + `--force` writes or leaves file but does not create backup.

## Documentation updates

Update the integration table after fixes land:

- Note that `claude-code` supports inline and env-var server keys.
- Clarify that model-required targets need `--model` for write mode when catalog selection is ambiguous.
- Clarify that Cline/Roo remain profile/UI snippets and do not mutate VS Code extension storage.
- Clarify that Codex is still version-sensitive and the generated TOML is a fragment unless direct schema validation is completed.

## Suggested patch order

### Patch 1: prevent runtime failure

- Add `_restart_after_integration_context_mutation()`.
- Add/adjust CLI tests that would fail without it.

### Patch 2: finish key/transcoder correctness

- Fix `_persist_transcoder_enabled()` missing-section behavior.
- Replace `SystemExit` in key resolver with a normal exception.
- Port `claude-code` to key resolver.
- Add tests.

### Patch 3: enforce target model policy and TOML safety

- Add `_resolve_model_for_configsetup()`.
- Use it in command wrappers.
- Harden Codex TOML escaping and parse tests.

### Patch 4: polish write/docs behavior

- Adjust backup path if desired.
- Strengthen write-mode tests.
- Update README/docs integration table.

## Success criteria

This follow-up pass is complete when:

- No expanded configsetup command can raise `NameError` for missing restart helper.
- Transcoder enablement is persisted for missing, partial, and disabled `[transcoder]` sections when needed.
- `claude-code` and all shared integration commands resolve server keys consistently for inline and `api_key_env` configs.
- No utility module raises `SystemExit` as control flow.
- Model-required target metadata has observable CLI behavior.
- Codex TOML parse tests cover hostile-but-valid model IDs.
- Write mode remains non-destructive and covered by tests.
