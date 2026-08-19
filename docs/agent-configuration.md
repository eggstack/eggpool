# Agent Configuration

`eggpool configsetup` generates configuration snippets for popular coding agents. Each target produces format-appropriate output (JSON, TOML, YAML, or shell exports) that references your running EggPool instance.

## Supported Targets

| Target | Command | Output Format | `--write` Default | Model |
|--------|---------|---------------|-------------------|-------|
| OpenCode | `eggpool configsetup opencode` | JSON provider config | N/A (clipboard) | auto |
| Claude Code | `eggpool configsetup claude-code` | JSON snippet | N/A (clipboard) | N/A |
| Aider | `eggpool configsetup aider` | Shell env exports | `.env.eggpool` | recommended |
| Codex | `eggpool configsetup codex` | TOML `[model_providers.eggpool]` block (Responses wire API) | N/A (printed) | recommended |
| Qwen Code | `eggpool configsetup qwen-code` | JSON provider block | N/A (printed) | optional |
| Kilo | `eggpool configsetup kilo` | JSON provider block | N/A (printed) | optional |
| Continue | `eggpool configsetup continue` | YAML model block | `~/.continue/eggpool.yaml` | usually yes |
| Cline | `eggpool configsetup cline` | JSON profile | `cline-eggpool.json` | recommended |
| Roo Code | `eggpool configsetup roo-code` | JSON profile | `roo-eggpool.json` | recommended |
| Goose | `eggpool configsetup goose` | Shell env exports | N/A (printed) | recommended |
| OpenHands | `eggpool configsetup openhands` | Shell env exports | N/A (printed) | recommended |

## Shared Options

| Option | Description |
|--------|-------------|
| `--host HOST` | Override the EggPool host (default: `localhost`) |
| `--base-url URL` | Override the full base URL |
| `--model MODEL` | Override the default model |
| `--write` | Write output to the default file for the target |
| `--output PATH` | Write output to a specific file |
| `--force` | Overwrite existing output file |
| `--no-clipboard` | Skip copying to clipboard |
| `--print-secret` | Include the API key in the output (for Codex env vars) |

## Examples

```sh
# OpenCode — print JSON config to stdout
eggpool configsetup opencode

# Aider — write .env.eggpool with a specific model
eggpool configsetup aider --model openai/gpt-4 --write

# Continue — write YAML to a custom path
eggpool configsetup continue --model claude-sonnet-4 --output ~/.continue/eggpool.yaml

# Cline — skip clipboard
eggpool configsetup cline --no-clipboard

# Codex — print TOML block with secret for env var reference
eggpool configsetup codex --print-secret

# Roo Code — write JSON profile
eggpool configsetup roo-code --write
```

## Output Behavior

- Generated JSON, TOML, YAML, and shell snippets escape catalog/config values for the target format, including provider-suffixed model IDs.
- The `--model` flag overrides the auto-detected model. Without it, the generator picks the best available model from the catalog.
- `--write` writes to a sensible default location for the target (see the table above). `--output` always takes precedence.
- Without `--write` or `--output`, the output is printed to stdout and copied to the clipboard (unless `--no-clipboard`).

## Codex Integration

The Codex integration emits a `[model_providers.eggpool]` TOML block with `wire_api = "responses"` and an `env_key = "EGGPOOL_API_KEY"` reference. Use `--print-secret` to include the actual API key in the output:

```bash
eggpool configsetup codex --print-secret
```

## OpenCode Integration

`eggpool configsetup opencode` generates an OpenCode-compatible JSON configuration. When thinking/reasoning capabilities are discovered for a model, the output includes `"thinking": "supported"` annotations so OpenCode's model picker can surface them.

Provider-scoped model IDs are used when `models.collapse_models = false` (the default), so OpenCode can disambiguate providers serving the same upstream model.
