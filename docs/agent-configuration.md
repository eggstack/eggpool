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
| `--print-secret` | Print the resolved API key for targets whose generated artifact embeds it; it does not change Codex TOML |

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

# Codex — print the non-secret Responses provider block
eggpool configsetup codex --model <eggpool-model-or-alias> --no-clipboard

# Roo Code — write JSON profile
eggpool configsetup roo-code --write
```

## Output Behavior

- Generated JSON, TOML, YAML, and shell snippets escape catalog/config values for the target format, including provider-suffixed model IDs.
- The `--model` flag sets an explicit model or EggPool alias. If exactly one model is available, the shared resolver can fill it automatically; when multiple models are available, EggPool does not invent a preference.
- `--write` writes to a sensible default location for the target (see the table above). `--output` always takes precedence.
- Without `--write` or `--output`, the output is printed to stdout and copied to the clipboard (unless `--no-clipboard`).

## Codex Integration

Codex uses EggPool through the HTTP/SSE Responses path. Start with an explicit
EggPool model or alias; the standard `/v1/models` endpoint intentionally keeps
its OpenAI-compatible schema and is not a Codex remote-catalog endpoint.

The current generated block is equivalent to:

```toml
model = "<eggpool-model-or-alias>"
model_provider = "eggpool"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://127.0.0.1:11300/v1"
env_key = "EGGPOOL_API_KEY"
wire_api = "responses"
supports_websockets = false
```

Set `EGGPOOL_API_KEY` to EggPool's server key in the environment used to run
Codex. Retrieve the current key without putting it in the TOML with:

```bash
export EGGPOOL_API_KEY="$(eggpool getkey)"
```

The default server port is `11300`; use `--base-url` when the server is
configured differently. Generate a model-specific snippet with:

```bash
eggpool configsetup codex --model <eggpool-model-or-alias>
```

The generated TOML contains `env_key = "EGGPOOL_API_KEY"`, never the resolved
server key, so Codex output is printed normally without `--print-secret`.
Passing `--print-secret` does not embed the key in Codex TOML. Use `--model` for
the qualified setup path, or select the model explicitly when invoking Codex;
automatic Codex model-picker discovery is deferred. For a real current CLI
check, see [Codex compatibility smoke](codex-compatibility-smoke.md).

## OpenCode Integration

`eggpool configsetup opencode` generates an OpenCode-compatible JSON configuration. When thinking/reasoning capabilities are discovered for a model, the output includes `"thinking": "supported"` annotations so OpenCode's model picker can surface them.

Provider-scoped model IDs are used when `models.collapse_models = false` (the default), so OpenCode can disambiguate providers serving the same upstream model.
