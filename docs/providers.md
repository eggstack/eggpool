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

## Routing Priority and Model Collapse

When several providers can serve the same base model, EggPool exposes two
configuration knobs that decide *which* provider gets a given request and *how*
the model appears in `/v1/models`:

- **`routing_priority`** — per-provider integer (default `0`, must be `>= 0`).
  Higher values are preferred. Accounts inside the same priority tier are still
  load-balanced by the existing `QuotaFairScorer`.
- **`collapse_models`** — top-level `[models]` flag (default `false`). When
  `false`, the catalog exposes one provider-suffixed entry per
  `(model_id, provider_id)` (e.g. `minimax-m2.7/generalcompute`,
  `minimax-m2.7/minimax`, `minimax-m2.7/opencode-go`). When `true`, the same
  base model collapses to a single unsuffixed `minimax-m2.7` ID.

The two knobs are independent. `collapse_models` changes the *catalog shape*;
`routing_priority` changes the *selection order* inside that shape.

### Worked example

Three providers all expose `minimax-m2.7`. The desired order is
`generalcompute` first, `minimax` second, `opencode-go` last, with three
`opencode-go` API keys load-balancing within their tier:

```toml
[models]
# collapse_models = false  # default; emit one suffixed entry per provider

[providers.opencode-go]
routing_priority = 0  # 3 API keys load balance within this tier

[providers.minimax]
routing_priority = 2  # tried after generalcompute, before opencode-go

[providers.generalcompute]
routing_priority = 3  # tried first
```

With `collapse_models = false` and the priorities above, `/v1/models` emits:

- `minimax-m2.7/generalcompute` — `routing_priority = 3`
- `minimax-m2.7/minimax` — `routing_priority = 2`
- `minimax-m2.7/opencode-go` — `routing_priority = 0`

A request for `minimax-m2.7/generalcompute` first hits the `generalcompute`
accounts (load balanced by `QuotaFairScorer` inside the tier). If every
`generalcompute` account is unhealthy, exhausted, or fails pre-body, the
coordinator retries against `minimax` accounts, then `opencode-go` accounts.

A request for `minimax-m2.7/opencode-go` only ever routes against
`opencode-go` accounts, regardless of priority. Priority only orders the
account set inside a single suffixed (or unsuffixed) model ID.

When `collapse_models = true`, the same three providers collapse to a single
`minimax-m2.7` entry. The router still picks one provider per request, with
the same priority ordering. Each suffixed entry's `/v1/models` response
carries an `eggpool.routing_priority` extension field for observability.

### Defaults and migration

The defaults are `collapse_models = false` and `routing_priority = 0`. Existing
deployments that used the unsuffixed `minimax-m2.7` ID should either:

- Set `collapse_models = true` to keep the old single-ID exposure, or
- Rewrite the client to use the suffixed `minimax-m2.7/<provider>` IDs.

Either change requires a service restart; live reload is intentionally not
supported.

### Rebalancing providers

`eggpool connect` writes `routing_priority = 0` on every newly created provider
block. The value is left untouched on existing blocks, so adding more accounts
to an already-configured provider does not disturb the operator's tier choice.
Operators can rebalance later by editing a single number in
`[providers.<id>].routing_priority` and restarting the service.

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
