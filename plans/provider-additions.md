# Provider addition plan

## Objective

Expand EggPool's default provider roster with API-key-based LLM providers that fit the current OpenAI/Anthropic-compatible proxy architecture. Avoid OAuth-only consumer subscription integrations and avoid providers whose primary integration path requires browser login, device-code flows, or vendor-specific signed cloud auth until EggPool has explicit adapter support for those flows.

This plan focuses on providers that can be added cleanly through provider templates, config examples, verifier fixtures, and documentation. It does not implement the providers directly; it defines the corrective and additive work needed for a small implementation model to execute safely.

## Current default provider roster in the repo

The current `config.example.toml` already contains commented default/template entries for the following providers:

| Provider | Current status in config | Protocols | Notes |
|---|---|---:|---|
| `opencode-go` | primary/manual example | OpenAI + Anthropic | Current default upstream remains `https://opencode.ai/zen/go/v1`. Has MiniMax M3 context override example. |
| `deepseek` | verified | OpenAI | Useful direct Chinese model provider. Config comment says official docs confirm OpenAI + Anthropic compatibility, but current template only enables OpenAI. |
| `openrouter` | verified | OpenAI | Broad aggregator, optional attribution headers already documented. |
| `together` | verified | OpenAI | Good third-party open-model inference provider. |
| `fireworks` | verified | OpenAI | Good third-party open-model inference provider; already present and should remain high-priority. |
| `zai` | live-verification-required | OpenAI | Useful for GLM/Zhipu models, but base URL and model endpoint should remain verifier-gated. |
| `alibaba` | verified | OpenAI | DashScope compatible-mode endpoint with regional alternatives already documented. |
| `novita` | live-verification-required | OpenAI | Useful but should verify base URL and model listing before marking verified. |
| `minimax` | live-verification-required | OpenAI + Anthropic | Auth/header currently suspect because 401 was observed. Do not mark verified until the MiniMax auth correction plan is completed and verifier passes. |
| `generalcompute` | unverified | OpenAI | Uses POST model listing via `models_method = "POST"` and `/models/list`; keep behind verifier. |
| `neuralwatt` | live-verification-required | OpenAI | Promising third-party backend; verify `/models`, streaming, and usage shape. |
| `ollama-local` | verified | OpenAI | Local no-auth endpoint. Technically not an API-key SaaS provider but useful for development. |
| `ollama-cloud` | unverified | OpenAI | Keep unverified until auth and base URL are live-tested. |

The current config model can already express most API-provider needs:

- Bearer auth, API-key header auth, raw Authorization, and no-auth modes via `ProviderAuthConfig`.
- Static provider headers via `ProviderStaticHeaderConfig`.
- Provider-level `base_url`, protocol list, `openai_path`, `anthropic_path`, and model endpoint method/path/body/query via `ProviderConfig` and `ProviderModelsEndpointConfig`.
- Provider-specific model overrides for protocol, context limits, output limits, pricing, and cache prices.

This means most additions below should be template + documentation + tests, not core proxy rewrites.

## Exclusions

Do not add these in this provider-expansion pass:

1. OAuth-only or consumer-subscription integrations such as ChatGPT web, Claude Pro/Max web, Gemini consumer web, Cursor consumer auth, or other browser/device-code flows.
2. Cloud APIs that require request signing or deployment-specific resource paths unless they expose a simple API-key OpenAI-compatible endpoint. This excludes first-pass support for AWS Bedrock native, Azure OpenAI deployment-specific routing, Vertex AI native, Baidu Qianfan native, Tencent Cloud native, and similar managed-cloud adapters.
3. Providers whose only practical path is a client SDK with hidden transport semantics and no stable HTTP API docs.
4. Providers that primarily sell non-chat media generation unless their chat-completion API is OpenAI-compatible and model-listing is sane.
5. Providers that cannot be represented using API keys, Bearer/API-key headers, and normal HTTP(S) endpoints.

## Addition strategy

Implement provider expansion in three layers.

### Layer 1: Add immediately as default templates

These providers have API-key style auth and clean HTTP/OpenAI-compatible chat endpoints. Add them to the default provider examples and, preferably, to a first-class provider template registry if that exists by the time implementation starts.

#### OpenAI direct

Rationale: Baseline direct provider, useful for comparison and fallback. Even if users prefer not to route through OpenAI, EggPool should support it because it is the reference OpenAI-compatible endpoint.

Template:

```toml
# OpenAI — direct OpenAI API
# Status: verified — reference OpenAI-compatible provider
# [providers.openai]
# id = "openai"
# base_url = "https://api.openai.com/v1"
# protocols = ["openai"]
#
# [[providers.openai.accounts]]
# name = "default"
# api_key_env = "OPENAI_API_KEY"
#
# [providers.openai.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.openai.verify]
# probe_model = "gpt-5.5-mini"
# probe_protocol = "openai"
# require_models = true
```

Use the current small/cheap OpenAI model at implementation time rather than hard-coding a stale probe model if the listed model has changed.

#### Anthropic direct

Rationale: EggPool already has an Anthropic-compatible downstream path. Direct Anthropic support is a clean API-key integration if the required version header is present.

Template:

```toml
# Anthropic — direct Claude API
# Status: verified — Anthropic Messages API with x-api-key auth and required version header
# [providers.anthropic]
# id = "anthropic"
# base_url = "https://api.anthropic.com/v1"
# protocols = ["anthropic"]
# anthropic_path = "/messages"
#
# [[providers.anthropic.accounts]]
# name = "default"
# api_key_env = "ANTHROPIC_API_KEY"
#
# [providers.anthropic.auth]
# mode = "api_key"
# header = "x-api-key"
#
# [[providers.anthropic.headers]]
# name = "anthropic-version"
# value = "2023-06-01"
```

Verifier:

```toml
# [providers.anthropic.verify]
# probe_model = "claude-sonnet-4-5"
# probe_protocol = "anthropic"
# require_models = false
```

Implementation note: Anthropic's model listing behavior may not map cleanly to OpenAI `/models` depending on endpoint availability and auth scope. Start with `require_models = false` if direct model listing is not stable, then use static overrides or manual catalog entries only if the current catalog code supports that. Do not fake model availability without a clear catalog path.

#### Groq

Rationale: Strong low-latency open-model backend, clean OpenAI compatibility, useful for cheap/fast tiers.

Template:

```toml
# Groq — low-latency OpenAI-compatible inference
# Status: verified — official docs use https://api.groq.com/openai/v1 with OpenAI clients
# [providers.groq]
# id = "groq"
# base_url = "https://api.groq.com/openai/v1"
# protocols = ["openai"]
#
# [[providers.groq.accounts]]
# name = "default"
# api_key_env = "GROQ_API_KEY"
#
# [providers.groq.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.groq.verify]
# probe_model = "openai/gpt-oss-20b"
# probe_protocol = "openai"
# require_models = true
```

Compatibility notes:

- Groq is mostly OpenAI-compatible, but some OpenAI parameters are unsupported. The verifier should avoid `logprobs`, `logit_bias`, `top_logprobs`, `messages[].name`, and `n != 1`.
- Treat 400s from unsupported optional fields as non-retryable provider/user request errors, not transient provider failures.

#### DeepInfra

Rationale: Strong broad open-model provider with simple OpenAI-compatible base URL and token auth. Useful as a western third-party fallback for open weights.

Template:

```toml
# DeepInfra — OpenAI-compatible open-model inference
# Status: verified — official docs use https://api.deepinfra.com/v1/openai
# [providers.deepinfra]
# id = "deepinfra"
# base_url = "https://api.deepinfra.com/v1/openai"
# protocols = ["openai"]
#
# [[providers.deepinfra.accounts]]
# name = "default"
# api_key_env = "DEEPINFRA_TOKEN"
#
# [providers.deepinfra.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.deepinfra.verify]
# probe_model = "deepseek-ai/DeepSeek-V3"
# probe_protocol = "openai"
# require_models = true
```

Compatibility notes:

- Model IDs are commonly provider/repo style, e.g. `org/model`.
- Some models support priority tiers or model-specific parameters; EggPool should pass unknown JSON fields through unchanged but should not add them by default.

#### Google Gemini OpenAI compatibility

Rationale: Direct Google API-key provider without OAuth when using the Gemini API OpenAI-compatible surface. Adds western direct model diversity and useful long-context models.

Template:

```toml
# Google Gemini — OpenAI-compatible Gemini API surface
# Status: verified — official docs use OpenAI clients with Gemini API keys
# [providers.gemini]
# id = "gemini"
# base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
# protocols = ["openai"]
#
# [[providers.gemini.accounts]]
# name = "default"
# api_key_env = "GEMINI_API_KEY"
#
# [providers.gemini.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.gemini.verify]
# probe_model = "gemini-3.5-flash"
# probe_protocol = "openai"
# require_models = true
```

Compatibility notes:

- Keep trailing slash handling covered by path-composition tests. The repo's duplicate-version validator already guards common `/v1` mistakes, but Gemini's base URL includes `/v1beta/openai`, which must combine with `/chat/completions` as `.../openai/chat/completions`.
- Do not add Google-specific `extra_body.google.thinking_config` defaults. Users can pass those through if needed.
- Verify streaming and `/models` with the actual API key because Google has changed model names frequently.

#### xAI

Rationale: Direct API-key access to Grok models. Useful as a western direct provider and coding/general reasoning option.

Template:

```toml
# xAI — direct Grok API
# Status: verified — API-key OpenAI client usage with https://api.x.ai/v1
# [providers.xai]
# id = "xai"
# base_url = "https://api.x.ai/v1"
# protocols = ["openai"]
#
# [[providers.xai.accounts]]
# name = "default"
# api_key_env = "XAI_API_KEY"
#
# [providers.xai.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.xai.verify]
# probe_model = "grok-4.3"
# probe_protocol = "openai"
# require_models = true
```

Compatibility notes:

- xAI also documents Responses API and compaction endpoints. Do not attempt to support those in this provider-template pass because EggPool's data plane currently exposes OpenAI chat completions and Anthropic messages, not `/v1/responses`.
- If `/v1/chat/completions` model support differs from `/v1/responses`, prefer a chat-compatible probe model.

#### Mistral direct

Rationale: Direct European provider with simple Bearer auth, chat completions, model listing, streaming, and useful coding/general models.

Template:

```toml
# Mistral — direct Mistral API
# Status: verified — /v1/chat/completions with Bearer auth
# [providers.mistral]
# id = "mistral"
# base_url = "https://api.mistral.ai/v1"
# protocols = ["openai"]
#
# [[providers.mistral.accounts]]
# name = "default"
# api_key_env = "MISTRAL_API_KEY"
#
# [providers.mistral.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.mistral.verify]
# probe_model = "mistral-small-latest"
# probe_protocol = "openai"
# require_models = true
```

Compatibility notes:

- Mistral exposes Mistral-native parameters such as `safe_prompt`, `prompt_mode`, and `random_seed`. EggPool should pass request bodies through rather than filtering.
- Usage may be `{}` in examples; verifier should not require token counts for success.

#### SiliconFlow

Rationale: High-value Chinese third-party aggregator with OpenAI-compatible chat endpoint, Bearer auth, and access to GLM/Qwen/DeepSeek/Tencent-style models. Good complement to direct Chinese providers already present.

Template:

```toml
# SiliconFlow — OpenAI-compatible Chinese model aggregator
# Status: verified — /v1/chat/completions with Bearer auth; verify model listing live
# [providers.siliconflow]
# id = "siliconflow"
# base_url = "https://api.siliconflow.cn/v1"
# protocols = ["openai"]
#
# [[providers.siliconflow.accounts]]
# name = "default"
# api_key_env = "SILICONFLOW_API_KEY"
#
# [providers.siliconflow.auth]
# mode = "bearer"
```

Verifier:

```toml
# [providers.siliconflow.verify]
# probe_model = "Pro/zai-org/GLM-4.7"
# probe_protocol = "openai"
# require_models = true
```

Compatibility notes:

- Model IDs often include provider prefixes and slashes. Ensure provider-suffixed exposure does not produce ambiguous IDs when a base model ID itself contains `/`.
- If EggPool uses slash suffixes like `model/provider`, it should prefer the existing parser behavior but test IDs like `Pro/zai-org/GLM-4.7/siliconflow` explicitly.

### Layer 2: Add as experimental/default-disabled templates after live verification

These are worth adding, but should not be labeled verified until `scripts/verify_upstream_auth.py` passes for auth, model listing, non-streaming chat, and streaming chat.

#### Cerebras Inference

Rationale: Very fast hosted inference for select open models. Useful latency-tier provider.

Suggested template:

```toml
# Cerebras — fast OpenAI-compatible inference
# Status: live-verification-required
# [providers.cerebras]
# id = "cerebras"
# base_url = "https://api.cerebras.ai/v1"
# protocols = ["openai"]
#
# [[providers.cerebras.accounts]]
# name = "default"
# api_key_env = "CEREBRAS_API_KEY"
#
# [providers.cerebras.auth]
# mode = "bearer"
```

Verifier target: current small supported chat model from their model list. Do not hard-code a model name until docs/live `/models` confirms it.

#### SambaNova Cloud

Rationale: Enterprise-ish hosted open models, generally API-key based and OpenAI-compatible in current public examples. Useful western provider diversity.

Suggested template:

```toml
# SambaNova Cloud — OpenAI-compatible hosted open models
# Status: live-verification-required
# [providers.sambanova]
# id = "sambanova"
# base_url = "https://api.sambanova.ai/v1"
# protocols = ["openai"]
#
# [[providers.sambanova.accounts]]
# name = "default"
# api_key_env = "SAMBANOVA_API_KEY"
#
# [providers.sambanova.auth]
# mode = "bearer"
```

Verifier target: current default chat model from `/models` or provider docs.

#### Hyperbolic

Rationale: Open-model third-party inference provider; useful for lower-cost broad-model routing if model listing and streaming are stable.

Suggested template:

```toml
# Hyperbolic — OpenAI-compatible open-model inference
# Status: live-verification-required
# [providers.hyperbolic]
# id = "hyperbolic"
# base_url = "https://api.hyperbolic.xyz/v1"
# protocols = ["openai"]
#
# [[providers.hyperbolic.accounts]]
# name = "default"
# api_key_env = "HYPERBOLIC_API_KEY"
#
# [providers.hyperbolic.auth]
# mode = "bearer"
```

Verification must confirm `/models` and SSE behavior before adding to recommended docs.

#### Featherless AI

Rationale: Broad serverless open-model catalog. Potentially useful for niche models and fallback, but should be experimental because catalog and model availability may change rapidly.

Suggested template:

```toml
# Featherless AI — OpenAI-compatible serverless open-model API
# Status: live-verification-required
# [providers.featherless]
# id = "featherless"
# base_url = "https://api.featherless.ai/v1"
# protocols = ["openai"]
#
# [[providers.featherless.accounts]]
# name = "default"
# api_key_env = "FEATHERLESS_API_KEY"
#
# [providers.featherless.auth]
# mode = "bearer"
```

Verification must confirm endpoint base URL, model listing, streaming, and whether usage fields are populated.

#### Moonshot / Kimi direct

Rationale: Direct Chinese Kimi provider. Valuable if users want Kimi-family models without routing through an aggregator.

Suggested template:

```toml
# Moonshot AI / Kimi — direct OpenAI-compatible API
# Status: live-verification-required
# [providers.moonshot]
# id = "moonshot"
# base_url = "https://api.moonshot.ai/v1"
# protocols = ["openai"]
#
# [[providers.moonshot.accounts]]
# name = "default"
# api_key_env = "MOONSHOT_API_KEY"
#
# [providers.moonshot.auth]
# mode = "bearer"
```

Verification target: current Kimi chat model. Confirm whether international accounts and China-region accounts use the same endpoint and auth semantics.

#### Volcengine Ark OpenAI-compatible mode

Rationale: Direct Chinese provider with useful model access, but likely needs model/deployment identifiers and regional endpoint details.

Suggested status: do not add as verified. Add only if docs confirm a stable OpenAI-compatible API-key base URL that does not require cloud request signing.

Template placeholder only after verification:

```toml
# Volcengine Ark — OpenAI-compatible mode
# Status: research-required; add only if API-key compatible endpoint is confirmed
# [providers.volcengine-ark]
# id = "volcengine-ark"
# base_url = "<confirmed-openai-compatible-base-url>"
# protocols = ["openai"]
```

### Layer 3: Keep existing experimental providers but harden them

#### MiniMax

Current state: already present, but marked live-verification-required because 401 was observed.

Required before expansion:

1. Complete the MiniMax auth correction plan.
2. Confirm whether the correct endpoint is `api.minimax.io`, `api.minimaxi.com`, or a region/account-specific endpoint for the user's account.
3. Confirm whether auth is standard `Authorization: Bearer <key>`, raw token, `GroupId` + key, or another header combination.
4. Confirm OpenAI path, Anthropic path, and model list path independently.
5. Add fixture tests for the exact headers EggPool emits.

Do not add more MiniMax examples until this is resolved; otherwise the provider catalog will accumulate misleading defaults.

#### NeuralWatt

Current state: already present and worth keeping, but marked live-verification-required.

Required verification:

1. `GET /models` or equivalent model-list endpoint.
2. `POST /chat/completions` non-streaming.
3. `POST /chat/completions` streaming SSE.
4. Terminal `usage` object shape and whether energy/cost metadata appears in response headers or body.
5. Whether pricing can be estimated with existing microdollar-per-token config or needs a separate energy-unit price field later.

Until verified, keep the provider template commented and experimental.

#### GeneralCompute

Current state: already present and uses POST for model listing.

Required verification:

1. Ensure current `models_method = "POST"` + `models_path = "/models/list"` composes to exactly `/v1/models/list` with no duplicated `/v1`.
2. Confirm required model-list body. If the provider requires a JSON body, move from legacy `models_method`/`models_path` to `[providers.generalcompute.models_endpoint]` with `method`, `path`, and `body`.
3. Confirm whether `POST /chat/completions` is exactly OpenAI-shaped.

#### Novita

Current state: already present but base URL is probably incomplete if provider docs expect `/openai/v1` rather than `/openai`.

Required verification:

1. Confirm whether `base_url = "https://api.novita.ai/openai"` plus default `/chat/completions` is correct, or whether it should be `https://api.novita.ai/openai/v1`.
2. Confirm `/models` path composition.
3. Confirm streaming SSE terminator and usage fields.

#### Z.AI

Current state: already present as `https://api.z.ai/api/paas/v4`.

Required verification:

1. Confirm current base URL and whether default OpenAI paths produce the documented endpoint.
2. Confirm model-list support.
3. Add GLM model context overrides for known expensive/slow long-context models after model IDs are discovered.

#### Ollama Cloud

Current state: present but unverified.

Required verification:

1. Confirm `https://ollama.com/v1` supports `/models` and `/chat/completions` for cloud accounts with Bearer auth.
2. If model listing is account/catalog-specific, keep `require_models = false` and document manual model configuration only if EggPool supports it.

## Provider registry refactor

The default provider list is currently embedded as commented examples in `config.example.toml`. That works for manual setup, but it is poor as a growing roster. Add a first-class bundled provider registry.

Recommended file:

```text
src/eggpool/_share/provider_templates.toml
```

Suggested schema:

```toml
[providers.groq]
id = "groq"
display_name = "Groq"
status = "verified" # verified | experimental | broken-auth | research-required
category = "direct" # direct | aggregator | local
region = "US"
base_url = "https://api.groq.com/openai/v1"
protocols = ["openai"]
recommended = true
notes = "Mostly OpenAI-compatible; avoid unsupported optional OpenAI params in verifier."

[providers.groq.auth]
mode = "bearer"

[providers.groq.account]
name = "default"
api_key_env = "GROQ_API_KEY"

[providers.groq.verify]
probe_model = "openai/gpt-oss-20b"
probe_protocol = "openai"
require_models = true
```

Then update:

1. `eggpool connect list` to read the registry instead of maintaining a separate hard-coded list.
2. `eggpool connect` to instantiate a selected provider from the registry.
3. `eggpool init-config` to keep `config.example.toml` compact while still referencing the full provider catalog.
4. Tests to parse every registry entry with the existing Pydantic config models.

If this refactor is too large for the provider-addition pass, implement it as a second phase and add the new providers to `config.example.toml` first.

## Implementation phases

### Phase 1: Template additions only

Files likely touched:

- `config.example.toml`
- `src/eggpool/_share/config.example.toml` if the package bundles a separate copy
- `docs/deployment.md` or a new `docs/providers.md`
- `tests/unit/` provider config parsing tests

Tasks:

1. Add commented provider blocks for `openai`, `anthropic`, `groq`, `deepinfra`, `gemini`, `xai`, `mistral`, and `siliconflow`.
2. Keep API keys as `api_key_env` in examples, not inline placeholder keys.
3. For direct Anthropic, include `auth.mode = "api_key"`, `auth.header = "x-api-key"`, and static `anthropic-version` header.
4. For Gemini, ensure base URL is exactly `https://generativelanguage.googleapis.com/v1beta/openai` without a trailing slash in TOML examples, because EggPool path composition should own the separator.
5. For providers whose docs use a trailing slash in SDK setup, add a path-composition unit test rather than copying the trailing slash blindly.
6. Do not mark Cerebras, SambaNova, Hyperbolic, Featherless, Moonshot, Volcengine Ark, MiniMax, NeuralWatt, GeneralCompute, Novita, Z.AI, or Ollama Cloud as verified unless live verifier evidence exists.

### Phase 2: Provider verification harness hardening

Files likely touched:

- `scripts/verify_upstream_auth.py`
- `src/eggpool/providers/` if verifier reuses provider client construction
- `tests/unit/` and `tests/integration/`

Tasks:

1. Extend `verify_upstream_auth.py` to print the final resolved URLs for model listing and chat probes.
2. Print redacted request headers so auth/header mistakes are visible without leaking keys.
3. Support `[providers.<id>.models_endpoint]` fully: method, path, query, body, and `required`.
4. Add a non-streaming chat probe and a streaming chat probe.
5. For Anthropic providers, use the provider's `probe_protocol = "anthropic"` and send a minimal Messages request.
6. Classify verifier failures into `auth_failed`, `models_failed`, `chat_failed`, `stream_failed`, and `usage_missing`.
7. Do not fail the entire verifier on `usage_missing`; report it because many providers omit usage on streams.
8. Add `--provider <id>` and `--account <name>` filters if they do not already exist.

### Phase 3: Path/auth contract tests

Add table-driven tests that instantiate minimal provider configs and assert exact outbound behavior.

Required cases:

```python
cases = [
    ("openai", "https://api.openai.com/v1", "/chat/completions", "https://api.openai.com/v1/chat/completions"),
    ("groq", "https://api.groq.com/openai/v1", "/chat/completions", "https://api.groq.com/openai/v1/chat/completions"),
    ("deepinfra", "https://api.deepinfra.com/v1/openai", "/chat/completions", "https://api.deepinfra.com/v1/openai/chat/completions"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "/chat/completions", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("xai", "https://api.x.ai/v1", "/chat/completions", "https://api.x.ai/v1/chat/completions"),
    ("mistral", "https://api.mistral.ai/v1", "/chat/completions", "https://api.mistral.ai/v1/chat/completions"),
    ("siliconflow", "https://api.siliconflow.cn/v1", "/chat/completions", "https://api.siliconflow.cn/v1/chat/completions"),
    ("anthropic", "https://api.anthropic.com/v1", "/messages", "https://api.anthropic.com/v1/messages"),
]
```

Auth/header assertions:

- Bearer providers emit `Authorization: Bearer <redacted>`.
- Anthropic direct emits `x-api-key: <redacted>` and `anthropic-version: 2023-06-01`.
- No provider emits duplicate Authorization headers.
- Static headers do not overwrite auth headers unless explicitly configured.

### Phase 4: Documentation

Create or update `docs/providers.md` with:

1. Provider status table.
2. Explanation of `verified`, `experimental`, `broken-auth`, and `research-required`.
3. API-key environment variable names.
4. Known quirks per provider.
5. Explicit statement that OAuth/consumer subscription products are out of scope for this provider catalog.
6. Verification command examples:

```bash
set -a; source .env; set +a
uv run eggpool --config config.toml check-config
uv run python scripts/verify_upstream_auth.py --config config.toml --provider groq
uv run python scripts/smoke_test.py --base-url http://127.0.0.1:11300 --api-key "$EGGPOOL_API_KEY"
```

### Phase 5: Optional provider registry

If implementation time allows, move provider templates out of `config.example.toml` into a bundled provider registry as described above. Keep `config.example.toml` short and include only:

- Opencode Go primary example.
- Three canonical API providers: OpenAI direct, Anthropic direct, OpenRouter.
- A pointer to `eggpool connect list` / `docs/providers.md` for the full roster.

## Acceptance criteria

The provider expansion is complete when:

1. `config.example.toml` includes API-key-only examples for OpenAI, Anthropic, Groq, DeepInfra, Gemini, xAI, Mistral, and SiliconFlow.
2. Existing defaults remain present and their statuses are corrected rather than inflated.
3. MiniMax remains marked unverified/broken-auth until the 401 issue is resolved by live verifier evidence.
4. NeuralWatt remains present but experimental until live model-list/chat/stream verification passes.
5. `uv run eggpool --config config.example.toml check-config` still works under the expected placeholder-handling behavior, or the test suite has an explicit fixture for parsing commented examples.
6. Unit tests cover path composition for every default provider.
7. Unit tests cover Bearer auth, API-key header auth, static headers, and model-list endpoint overrides.
8. `scripts/verify_upstream_auth.py` can verify a single selected provider and reports the exact failure class.
9. Documentation states that OAuth-only providers are out of scope.
10. No provider template requires browser login, OAuth refresh tokens, cloud request signing, or manual cookie extraction.

## Suggested final default status matrix

| Provider | Final desired status after this pass |
|---|---|
| `opencode-go` | verified if current user tests remain green |
| `openai` | verified |
| `anthropic` | verified, with `require_models = false` if model listing is unavailable |
| `openrouter` | verified |
| `together` | verified |
| `fireworks` | verified |
| `groq` | verified |
| `deepinfra` | verified |
| `gemini` | verified after live key test |
| `xai` | verified after live key test |
| `mistral` | verified |
| `deepseek` | verified |
| `alibaba` | verified |
| `siliconflow` | verified after live key test |
| `zai` | experimental until live verifier passes |
| `novita` | experimental until base URL corrected/verified |
| `minimax` | broken-auth or experimental until 401 resolved |
| `generalcompute` | experimental until POST model-list verifier passes |
| `neuralwatt` | experimental until live verifier passes |
| `ollama-local` | verified local/dev |
| `ollama-cloud` | experimental until live verifier passes |
| `cerebras` | experimental after docs/live verification |
| `sambanova` | experimental after docs/live verification |
| `hyperbolic` | experimental after docs/live verification |
| `featherless` | experimental after docs/live verification |
| `moonshot` | experimental after docs/live verification |

## Reference URLs checked while preparing this plan

- Groq OpenAI compatibility: `https://console.groq.com/docs/openai`
- DeepInfra OpenAI-compatible chat completions: `https://docs.deepinfra.com/chat/overview`
- Google Gemini OpenAI compatibility: `https://ai.google.dev/gemini-api/docs/openai`
- xAI REST API reference: `https://docs.x.ai/developers/rest-api-reference/inference/chat`
- Mistral API chat completions: `https://docs.mistral.ai/api`
- Anthropic API versioning: `https://docs.anthropic.com/en/api/versioning`
- OpenAI Chat Completions API reference: `https://developers.openai.com/api/reference/resources/chat`
- SiliconFlow chat completions: `https://docs.siliconflow.cn/en/api-reference/chat-completions/chat-completions`
