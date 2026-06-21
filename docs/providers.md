# Provider Catalog

EggPool supports multiple upstream AI providers behind a unified API. This document describes the provider roster, their status, and how to configure them.

## Provider Status Definitions

| Status | Meaning |
|--------|---------|
| `verified` | API-key auth and endpoints confirmed working |
| `live-verification-required` | Template present but base URL, model listing, or auth needs live testing before production use |
| `unverified` | Template present but not yet tested against live endpoints |

## Verified Providers

These providers have clean API-key auth and OpenAI/Anthropic-compatible endpoints. They are safe to configure with real API keys.

| Provider | ID | Base URL | Protocols | Auth | API Key Env |
|----------|----|----------|-----------|------|-------------|
| OpenCode Go | `opencode-go` | `https://opencode.ai/zen/go/v1` | OpenAI + Anthropic | Bearer | `API_KEY` |
| OpenAI | `openai` | `https://api.openai.com/v1` | OpenAI | Bearer | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | `https://api.anthropic.com/v1` | Anthropic | API Key (`x-api-key`) | `ANTHROPIC_API_KEY` |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1` | OpenAI | Bearer | `OPENROUTER_API_KEY` |
| DeepSeek | `deepseek` | `https://api.deepseek.com` | OpenAI | Bearer | `DEEPSEEK_API_KEY` |
| Together AI | `together` | `https://api.together.ai/v1` | OpenAI | Bearer | `TOGETHER_API_KEY` |
| Fireworks AI | `fireworks` | `https://api.fireworks.ai/inference/v1` | OpenAI | Bearer | `FIREWORKS_API_KEY` |
| Groq | `groq` | `https://api.groq.com/openai/v1` | OpenAI | Bearer | `GROQ_API_KEY` |
| DeepInfra | `deepinfra` | `https://api.deepinfra.com/v1/openai` | OpenAI | Bearer | `DEEPINFRA_TOKEN` |
| Google Gemini | `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | OpenAI | Bearer | `GEMINI_API_KEY` |
| xAI | `xai` | `https://api.x.ai/v1` | OpenAI | Bearer | `XAI_API_KEY` |
| Mistral | `mistral` | `https://api.mistral.ai/v1` | OpenAI | Bearer | `MISTRAL_API_KEY` |
| SiliconFlow | `siliconflow` | `https://api.siliconflow.cn/v1` | OpenAI | Bearer | `SILICONFLOW_API_KEY` |
| Alibaba Qwen | `alibaba` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI | Bearer | `ALIBABA_API_KEY` |
| Ollama (local) | `ollama-local` | `http://localhost:11434/v1` | OpenAI | None | N/A |

## Experimental Providers

These providers are present as templates but require live verification before production use. Run the verifier to confirm they work with your credentials:

```bash
set -a; source .env; set +a
uv run python scripts/verify_upstream_auth.py --config config.toml --provider <provider-id>
```

| Provider | ID | Base URL | Protocols | Notes |
|----------|----|----------|-----------|-------|
| Z.AI (ZhipuAI) | `zai` | `https://api.z.ai/api/paas/v4` | OpenAI | Confirm base URL and model listing |
| Novita AI | `novita` | `https://api.novita.ai/openai` | OpenAI | Base URL may need correction |
| MiniMax International | `minimax` | `https://api.minimax.io/v1` | OpenAI | 401 observed; auth needs correction |
| MiniMax China | `minimax-cn` | `https://api.minimaxi.com/v1` | OpenAI | 401 observed; auth needs correction |
| GeneralCompute | `generalcompute` | `https://api.generalcompute.com/v1` | OpenAI | Uses POST for model listing |
| NeuralWatt | `neuralwatt` | `https://api.neuralwatt.com/v1` | OpenAI | Energy-based pricing; verify endpoints |
| Ollama (cloud) | `ollama-cloud` | `https://ollama.com/v1` | OpenAI | Confirm cloud auth and model listing |
| Cerebras | `cerebras` | `https://api.cerebras.ai/v1` | OpenAI | Fast inference; verify model listing |
| SambaNova Cloud | `sambanova` | `https://api.sambanova.ai/v1` | OpenAI | Enterprise hosted models |
| Hyperbolic | `hyperbolic` | `https://api.hyperbolic.xyz/v1` | OpenAI | Open-model inference |
| Featherless AI | `featherless` | `https://api.featherless.ai/v1` | OpenAI | Serverless open-model API |
| Moonshot / Kimi | `moonshot` | `https://api.moonshot.ai/v1` | OpenAI | Direct Kimi models |

## Configuration

### Interactive Setup (Recommended)

```bash
# List available providers
uv run eggpool connect list

# Connect to a provider interactively
uv run eggpool connect

# Connect to a specific provider
uv run eggpool connect groq
```

### Manual Configuration

Add a provider block to `config.toml`:

```toml
[providers.groq]
id = "groq"
base_url = "https://api.groq.com/openai/v1"
protocols = ["openai"]

[[providers.groq.accounts]]
name = "default"
api_key_env = "GROQ_API_KEY"

[providers.groq.auth]
mode = "bearer"
```

Set the API key in your environment or `.env` file:

```bash
export GROQ_API_KEY="gsk_..."
```

### Anthropic-Specific Configuration

Anthropic uses `api_key` auth mode (not `bearer`) and requires a version header:

```toml
[providers.anthropic]
id = "anthropic"
base_url = "https://api.anthropic.com/v1"
protocols = ["anthropic"]
anthropic_path = "/messages"

[[providers.anthropic.accounts]]
name = "default"
api_key_env = "ANTHROPIC_API_KEY"

[providers.anthropic.auth]
mode = "api_key"
header = "x-api-key"

[[providers.anthropic.headers]]
name = "anthropic-version"
value = "2023-06-01"
```

## Verification

Verify a provider's auth, model listing, and chat endpoints:

```bash
# Set API keys
set -a; source .env; set +a

# Verify config is valid
uv run eggpool --config config.toml check-config

# Verify a specific provider
uv run python scripts/verify_upstream_auth.py --config config.toml --provider groq

# Verify all providers
uv run python scripts/verify_upstream_auth.py --config config.toml --all

# Verbose output with resolved URLs
uv run python scripts/verify_upstream_auth.py --config config.toml --provider groq --verbose
```

## Provider-Specific Notes

### Groq

- Mostly OpenAI-compatible, but some OpenAI parameters are unsupported (e.g., `logprobs`, `logit_bias`, `n != 1`).
- 400s from unsupported optional fields are non-retryable user errors, not transient failures.
- Model IDs may use `org/model` format (e.g., `openai/gpt-oss-20b`).

### DeepInfra

- Model IDs use `org/model` format (e.g., `deepseek-ai/DeepSeek-V3`).
- Pass unknown JSON fields through unchanged; do not add provider-specific fields by default.

### Google Gemini

- Base URL includes `/v1beta/openai`; path composition must produce `.../openai/chat/completions`.
- Do not add Google-specific `extra_body.google.thinking_config` defaults.
- Model names change frequently; verify with live API key.

### xAI

- Also documents Responses API and compaction endpoints; EggPool only supports chat completions.
- Use a chat-compatible probe model for verification.

### Mistral

- Exposes native parameters (`safe_prompt`, `prompt_mode`, `random_seed`); EggPool passes request bodies through.
- Usage may be `{}` in responses; verifier should not require token counts.

### SiliconFlow

- Model IDs often include provider prefixes and slashes (e.g., `Pro/zai-org/GLM-4.7`).
- Ensure provider-suffixed exposure does not produce ambiguous IDs.

### Anthropic Direct

- Model listing may not map cleanly to OpenAI `/models`. Start with `require_models = false`.
- Uses `x-api-key` header for auth, not `Authorization: Bearer`.

## OAuth / Consumer Subscription Exclusion

This provider catalog intentionally excludes:

- OAuth-only integrations (ChatGPT web, Claude Pro/Max web, Gemini consumer web)
- Browser login or device-code flows
- Cloud APIs requiring request signing (AWS Bedrock native, Azure OpenAI deployment-specific, Vertex AI native)
- Provider SDKs with hidden transport semantics

These require adapter support that EggPool does not currently implement.
