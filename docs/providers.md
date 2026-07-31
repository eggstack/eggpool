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
| MiniMax International | `minimax` | `https://api.minimax.io/anthropic` | Anthropic | Anthropic-compatible endpoint for token-plan keys; live model discovery via `/v1/models` |
| MiniMax China | `minimax-cn` | `https://api.minimaxi.com/v1` | OpenAI | Live verification required before production use |
| GeneralCompute | `generalcompute` | `https://api.generalcompute.com/v1` | OpenAI | Plain OpenAI-compatible PAYG; verify `/models` and chat live |
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

# Require the upstream protocol terminal marker (the default).
# stream_completion_policy = "strict"
```

Set the API key in your environment or `.env` file:

```bash
export GROQ_API_KEY="gsk_..."
```

`stream_completion_policy` is provider-bound and controls clean upstream EOF:
`strict` requires OpenAI `[DONE]` or Anthropic `message_stop`; `compatible`
allows markerless completion only when a complete usage signal is present; and
`permissive_observe` preserves that compatibility behavior while exposing the
missing-marker diagnostic. Use compatibility modes only for a named provider
whose terminal convention has been verified. A stream with payload but no
valid terminal evidence is otherwise recorded as an incomplete/midstream
failure and never emits a synthetic client terminal marker.

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

### MiniMax International (Anthropic-Compatible Token-Plan Endpoint)

Token-plan API keys from `minimax.io` are valid for the MiniMax
Anthropic-compatible surface, **not** the OpenAI-compatible
`/v1/chat/completions` endpoint. The bundled `minimax` template configures
the Anthropic-compatible contract by default and uses Anthropic-style
model discovery (`GET /v1/models`):

```toml
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/anthropic"
protocols = ["anthropic"]
anthropic_path = "/v1/messages"
models_method = "GET"
models_path = "/v1/models"

[[providers.minimax.accounts]]
name = "default"
api_key = "sk-your-minimax-key"

[providers.minimax.auth]
mode = "api_key"
header = "x-api-key"

[[providers.minimax.headers]]
name = "anthropic-version"
value = "2023-06-01"

[providers.minimax.models_endpoint]
method = "GET"
path = "/v1/models"
required = true
```

The composed upstream URL is
`https://api.minimax.io/anthropic/v1/messages`, sent with
`x-api-key: <token-plan-key>` and `anthropic-version: 2023-06-01`.
Live model discovery fetches the catalog from the MiniMax `/v1/models`
endpoint using the documented Anthropic-compatible listing. No static
seeds ship with the international template; the provider already accepts
the anthropic value produced by the family mapping, and live discovery is
the source of truth.

`minimax-cn` (China console) is intentionally configured as plain
OpenAI-compatible in the bundled template because the China endpoint
family and auth shape have not been confirmed against `api.minimaxi.com`.
Do not assume parity with the international Anthropic-compatible
template without live testing. `minimax-cn` ships with
`[[providers.minimax-cn.static_models]]` rows pinning `MiniMax-M3`,
`MiniMax-M2.7`, and `MiniMax-M2.5` to `protocol = "openai"` so the
global `FAMILY_PROTOCOLS["minimax-"] = "anthropic"` mapping does not
clear the protocol at the provider constraint check. Route MiniMax
through `minimax-cn` on `/v1/chat/completions` (OpenAI), not
`/v1/messages`.

### Endpoint routing by MiniMax provider

| Provider            | Base URL                                | Endpoint                | Auth style   |
| ------------------- | --------------------------------------- | ----------------------- | ------------ |
| `minimax`           | `https://api.minimax.io/anthropic`      | `POST /v1/messages`     | `x-api-key`  |
| `minimax-cn`        | `https://api.minimaxi.com/v1`           | `POST /chat/completions`| `Bearer`     |

Requests that hit the wrong endpoint receive a `400 ProtocolMismatchError`
("Model 'MiniMax-M3' uses the X protocol. Use /v1/..."). Hit the row
that matches the provider you configured.

### GeneralCompute PAYG (Plain OpenAI-Compatible)

GeneralCompute PAYG is treated as a standard OpenAI-compatible provider.
The bundled `generalcompute` template uses `GET /models` and
`POST /chat/completions` with Bearer auth:

```toml
[providers.generalcompute]
id = "generalcompute"
base_url = "https://api.generalcompute.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_method = "GET"
models_path = "/models"

[[providers.generalcompute.accounts]]
name = "default"
api_key = "sk-your-generalcompute-key"

[providers.generalcompute.auth]
mode = "bearer"
```

A previous default of `POST /models/list` was suspected of causing
catalog 404s and is no longer wired into the bundled template. If live
provider docs later prove `POST /models/list` is required for some
account type, implement it as an opt-in alternate template (for example
`generalcompute-models-list`) rather than as the default PAYG behavior.

### Static Model Seeds

When a provider's live model discovery is unavailable (e.g. the endpoint
does not expose a `/models` listing, or discovery is temporarily down),
static model seeds act as a fallback. Declare them under
`[[providers.<id>.static_models]]`. The international MiniMax template
ships without static seeds because live discovery is the source of
truth; providers like `minimax-cn` that need to pin a specific protocol
(where the family mapping would otherwise clear it) ship seeds as part
of the bundled template:

```toml
[[providers.minimax-cn.static_models]]
id = "MiniMax-M3"
display_name = "MiniMax-M3"
protocol = "openai"
supports_tools = true
supports_vision = false
```

Always mirror the live `id` / `display_name` exactly when adding fallback
seeds so the cache stays consistent with dynamic loading (for example,
`id = "MiniMax-M2.7-highspeed"` ships from the live endpoint as
`display_name = "MiniMax-M2.7-Highspeed"`).

Static rows participate in the same protocol, limit, and exposure
machinery as live-discovered entries. When the provider's
`models_endpoint.method = "DISABLED"` (or when live refresh returns no
rows), static entries still populate the catalog so routes can dispatch.
Live refreshes may augment static rows but must not erase explicit
static `protocol`, `supports_tools`, or `supports_vision` fields.

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

Either change currently requires a service restart.  `eggpool rehash`
validates the new config against the same contract as `eggpool
check-config` (see `docs/live-config-rehash.md`) and reports whether the
running process can pick up the change without a restart — but the live
control plane is not yet available, so operators should run `eggpool
restart` (or `systemctl restart eggpool`) to apply these field-level
changes.

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

## Troubleshooting

### MiniMax 401 on `/v1/chat/completions`

A 401 against `https://api.minimax.io/v1/chat/completions` with the
bundled template's old contract usually means the wrong endpoint family
or auth header was used. Token-plan keys from `minimax.io` are valid
for the Anthropic-compatible transport at
`https://api.minimax.io/anthropic/v1/messages` with `x-api-key` (not
`Authorization: Bearer`) and the `anthropic-version: 2023-06-01` header.
Update `base_url` to `https://api.minimax.io/anthropic`, `protocols`
to `["anthropic"]`, `auth.mode` to `api_key`, `auth.header` to
`x-api-key`, `anthropic_path` to `/v1/messages`, and add the
`anthropic-version` static header. MiniMax now supports live model
discovery via `GET /v1/models`, so the bundled template uses
`models_endpoint.method = "GET"` by default. Static model seeds are
only a fallback if live discovery is unavailable.

### GeneralCompute 404 on `/models/list`

A 404 against `https://api.generalcompute.com/v1/models/list` means the
non-default `POST /models/list` catalog endpoint was configured. PAYG
should be tested first as plain OpenAI-compatible with `GET /models`
and `POST /chat/completions` (the bundled template's default).

## OAuth / Consumer Subscription Exclusion

This provider catalog intentionally excludes:

- OAuth-only integrations (ChatGPT web, Claude Pro/Max web, Gemini consumer web)
- Browser login or device-code flows
- Cloud APIs requiring request signing (AWS Bedrock native, Azure OpenAI deployment-specific, Vertex AI native)
- Provider SDKs with hidden transport semantics

These require adapter support that EggPool does not currently implement.

## High-Concurrency HTTP Client Profiles

The default provider HTTPX limits (`max_connections=100`,
`max_keepalive=20`, `read_timeout_s=300`, `pool_timeout_s=30`) are
calibrated for low-power SBC/Raspberry Pi deployments. The runtime
settings split into three independent axes that are easy to confuse:

| Setting | Scope | Effect |
|---------|-------|--------|
| `server.threads` | Granian worker threads inside one process | CPU-bound request handling parallelism |
| `database.worker_threads` | Read-only stats DB connections | Dashboard / metrics concurrency |
| `<provider>.max_connections` | HTTPX connection pool per provider | Outbound HTTP connection parallelism |

Increasing `server.threads` does **not** raise HTTPX connection limits,
and increasing `max_connections` does not raise SQLite worker threads.
Each axis must be tuned for its bottleneck.

### Low-power default

Suitable for Raspberry Pi 4 / 5 or any single-board computer. Matches
the shipped defaults and minimises RSS, file descriptors, and TLS
state:

```toml
[server]
threads = 1

[database]
worker_threads = 2

[providers.opencode-go]
max_connections = 100
max_keepalive = 20
connect_timeout_s = 5
read_timeout_s = 300
write_timeout_s = 30
pool_timeout_s = 30
```

### High-concurrency coding-agent streaming

For OpenCode / Claude Code / Aider-style agents that keep many long
SSE streams open at once. Doubles the HTTPX pool size, raises the
keepalive window so upstreams reuse TLS sessions, and lengthens the
read timeout to absorb slow model first-token latencies:

```toml
[server]
threads = 1

[database]
worker_threads = 2

[providers.opencode-go]
max_connections = 256
max_keepalive = 128
connect_timeout_s = 5
read_timeout_s = 900
write_timeout_s = 30
pool_timeout_s = 60
```

Keep `server.threads` bounded — it helps the single worker multiplex
dashboard work alongside active streams, but it does not raise HTTPX
pool capacity. The real upstream-concurrency lever is
`max_connections`.

### Diagnostic low-noise mode

When reproducing an incident, drop the trace noise floor so the only
writes hitting SQLite are correctness-critical:

```toml
[routing.trace]
mode = "off"

[providers.opencode-go]
read_timeout_s = 1800
```

### Tuning warnings

- **Memory:** each `max_connections` slot keeps an open TLS state on
  warm idle. Going from 100 → 256 doubles the long-lived TLS object
  count. On a Raspberry Pi 4 with 4 GB RAM, do not exceed 200
  connections per provider.
- **File descriptors:** each connection holds one socket. Bump the
  process `nofile` ulimit to at least `2 × max_connections` plus headroom
  for SQLite, the ASGI server, and DNS.
- **Provider throttling:** some upstreams rate-limit aggressively when
  they see bursty TLS handshakes. Raise `keepalive_timeout_s` to keep
  the pool warm rather than relying on short-lived connections.
- **Worker count:** Granian runs with `workers=1` by design. Adding
  workers creates multiple EggPool processes that each open their own
  HTTPX pool, which multiplies the connection budget and can push you
  past upstream per-IP rate limits.
