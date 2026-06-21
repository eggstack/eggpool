# Provider Contract Corrective Plan

## Context

EggPool currently proxies multiple LLM providers through OpenAI- and Anthropic-compatible local endpoints. The current failure split is provider-specific: OpenCode Go works, MiniMax returns `401 Unauthorized`, and GeneralCompute returns `404 Not Found`.

The current implementation still contains OpenCode-Go-shaped assumptions in the shared proxy path. `src/eggpool/proxy/client.py` always strips local credential headers and injects exactly `Authorization: Bearer <api_key>`. `src/eggpool/catalog/fetcher.py` uses the same fixed auth shape for model listing. `ProviderConfig` can express `base_url`, `openai_path`, `anthropic_path`, `models_method`, and `models_path`, but it cannot express provider-specific auth header names, auth schemes, no-auth local providers, static provider headers, model-list request bodies, query parameters, or verification metadata.

This plan turns provider defaults into explicit provider contracts, verifies bundled provider templates with fixture tests, and upgrades the live verifier so provider auth/path errors are diagnosed before runtime.

## Research summary

### Confirmed contracts from official docs

DeepSeek documents OpenAI and Anthropic API compatibility. Its OpenAI base URL is `https://api.deepseek.com`, its Anthropic base URL is `https://api.deepseek.com/anthropic`, and its OpenAI-format curl example calls `https://api.deepseek.com/chat/completions` with `Authorization: Bearer ${DEEPSEEK_API_KEY}`. Practical EggPool contract: `base_url = "https://api.deepseek.com"`, `openai_path = "/chat/completions"`, `anthropic_path = "/anthropic/messages"` if using one provider entry for both protocols, and Bearer auth. If DeepSeek is configured as two logical providers, the Anthropic entry can use `base_url = "https://api.deepseek.com/anthropic"` with `anthropic_path = "/messages"`.

OpenRouter documents `POST https://openrouter.ai/api/v1/chat/completions` and `Authorization: Bearer <OPENROUTER_API_KEY>`. It also supports optional attribution headers `HTTP-Referer`, `X-OpenRouter-Title`, and `X-OpenRouter-Categories`. Practical EggPool contract: `base_url = "https://openrouter.ai/api/v1"`, `openai_path = "/chat/completions"`, `models_path = "/models"`, Bearer auth, and optional static headers.

Together documents OpenAI compatibility with `base_url = "https://api.together.ai/v1"`; SDK calls route to `POST /v1/chat/completions`. Practical EggPool contract: `base_url = "https://api.together.ai/v1"`, `openai_path = "/chat/completions"`, `models_path = "/models"`, Bearer auth.

Fireworks documents `POST https://api.fireworks.ai/inference/v1/chat/completions` with `Authorization: Bearer <token>`, and states Bearer authentication is required. Practical EggPool contract: `base_url = "https://api.fireworks.ai/inference/v1"`, `openai_path = "/chat/completions"`, `models_path = "/models"` if model listing is supported through that OpenAI-compatible surface, Bearer auth.

Alibaba Model Studio documents OpenAI compatibility and region-specific base URLs including `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`, `https://dashscope-us.aliyuncs.com/compatible-mode/v1`, and `https://dashscope.aliyuncs.com/compatible-mode/v1`, with full HTTP endpoints ending `/chat/completions`. Practical EggPool default should avoid hardcoding only the China endpoint; use `base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"` or a region-specific equivalent, `openai_path = "/chat/completions"`, and `models_path = "/models"` only if live verification confirms it.

Ollama documents OpenAI compatibility. Practical local contract: `base_url = "http://localhost:11434/v1"`, `openai_path = "/chat/completions"`, `models_path = "/models"`, and no required upstream auth for local Ollama. Ollama Cloud can keep Bearer auth if its documented endpoint requires it.

### Unverified or weakly documented contracts

MiniMax public docs were not reliably discoverable through the available web search surface. EggPool currently documents MiniMax as `base_url = "https://api.minimaxi.com"`, `openai_path = "/v1/chat/completions"`, `anthropic_path = "/anthropic/v1/messages"`, and `models_path = "/v1/models"`. This is internally consistent only when the base URL does not already include `/v1`. The observed `401` points more toward wrong key class, wrong product/region endpoint, or auth/header mismatch than toward routing. The implementation must make auth configurable and the verifier must print resolved URL plus redacted auth header shape.

GeneralCompute public docs were not reliably discoverable through the available web search surface. EggPool currently documents `base_url = "https://api.generalcompute.com/v1"` and `models_path = "/v1/models/list"`, which likely resolves to a duplicate-version URL under intended composition. The observed `404` is consistent with this. Fix the template to use either a `/v1` base URL with non-versioned paths or a host-only base URL with versioned paths; never mix both.

NeuralWatt, Novita, and Z.AI should be marked live-verification-required until fixtures or live probes confirm their model-list and chat endpoints. Treat their current templates as best-effort OpenAI-compatible entries, not verified contracts.

## Root causes to fix

1. Auth construction is not provider-aware. `build_upstream_auth_headers()` always returns `Authorization: Bearer <key>`. Providers requiring `X-Api-Key`, raw authorization, no auth, custom schemes, or extra static headers cannot be represented.

2. Model listing is not provider-contract-aware. `fetch_models_for_account()` hardcodes Bearer auth and only supports GET/POST with an empty JSON object for POST. Providers requiring a non-empty body, query parameters, account/org headers, or disabled listing cannot be represented.

3. URL composition is not validated. Current templates can mix versioned base URLs and versioned paths. EggPool should reject common duplicate-prefix combinations before operators see upstream 404s.

4. Provider templates lack verification metadata. There is no distinction between official-doc-verified, live-verified, and best-effort community templates.

5. The direct verifier exists but is not contract-aware enough. `scripts/verify_upstream_auth.py` should validate resolved URLs, auth header names/schemes, model-list method/body, and optional chat probes for every provider/account.

## Target design

### Add provider contract config

Extend `src/eggpool/models/config.py` with backwards-compatible provider contract types:

```python
class ProviderAuthConfig(BaseModel):
    mode: Literal["bearer", "api_key", "raw_authorization", "none"] = "bearer"
    header: str = "Authorization"
    scheme: str = "Bearer"

class ProviderStaticHeaderConfig(BaseModel):
    name: str
    value: str | None = None
    value_env: str | None = None

class ProviderModelsEndpointConfig(BaseModel):
    method: Literal["GET", "POST", "DISABLED"] = "GET"
    path: str = "/models"
    body: dict[str, Any] | None = None
    query: dict[str, str] = {}
    required: bool = True

class ProviderVerifyConfig(BaseModel):
    probe_model: str | None = None
    probe_protocol: Literal["openai", "anthropic"] = "openai"
    require_models: bool = True
```

Add to `ProviderConfig`:

```python
auth: ProviderAuthConfig = ProviderAuthConfig()
headers: list[ProviderStaticHeaderConfig] = []
models_endpoint: ProviderModelsEndpointConfig | None = None
verify: ProviderVerifyConfig = ProviderVerifyConfig()
```

Keep legacy `models_method` and `models_path`; synthesize `models_endpoint` when omitted so existing configs still load.

### Centralize contract rendering

Create `src/eggpool/providers/contract.py`:

```python
@dataclass(frozen=True)
class RenderedProviderRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: object | None = None
    query: dict[str, str] | None = None


def compose_provider_url(provider: ProviderConfig, endpoint_path: str) -> str:
    base = provider.base_url.rstrip("/")
    path = endpoint_path.strip("/")
    return f"{base}/{path}"


def build_auth_headers(provider: ProviderConfig, api_key: str) -> dict[str, str]:
    auth = provider.auth
    if auth.mode == "none":
        return {}
    if auth.mode in ("api_key", "raw_authorization"):
        return {auth.header: api_key}
    return {auth.header: f"{auth.scheme} {api_key}"}
```

Split `filter_request_headers()` into `sanitize_request_headers()` plus provider-contract merging. The proxy/client layer should remove downstream/local credentials and hop-by-hop headers, but it should not decide upstream auth.

### Harden URL composition

Avoid relying on HTTPX root-relative behavior. Compose absolute URLs explicitly and pass the absolute URL to `client.build_request()` / `client.get()` / `client.post()`.

Add config validation:

- `base_url` ending `/v1` plus path beginning `/v1/` is an error.
- `base_url` containing `/api/v1` plus path beginning `/api/v1/` is an error.
- `base_url` containing `/compatible-mode/v1` plus path beginning `/compatible-mode/v1/` is an error.
- Empty endpoint paths are errors except when model listing is disabled.
- Header names must match a conservative HTTP token regex.
- Static header values must use exactly one of `value` or `value_env`.

### Improve diagnostics

Add debug logs that show resolved contract without secrets:

```text
provider=minimax account=default protocol=openai method=POST url=https://api.minimaxi.com/v1/chat/completions auth=Authorization: Bearer ***
provider=generalcompute account=default models method=POST url=https://api.generalcompute.com/v1/models/list auth=Authorization: Bearer ***
```

For upstream 401/403/404, record redacted diagnostics when `persist_redacted_error_detail` is enabled: provider id, account, method, resolved URL, auth mode/header/scheme, status code, and a redacted response excerpt.

## Provider template corrections

Update both `config.example.toml` and `src/eggpool/_share/config.example.toml`.

### opencode-go

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]
openai_path = "/chat/completions"
anthropic_path = "/messages"
models_path = "/models"

[providers.opencode-go.auth]
mode = "bearer"
header = "Authorization"
scheme = "Bearer"
```

### deepseek

```toml
[providers.deepseek]
id = "deepseek"
base_url = "https://api.deepseek.com"
protocols = ["openai", "anthropic"]
openai_path = "/chat/completions"
anthropic_path = "/anthropic/messages"
models_path = "/models"

[providers.deepseek.auth]
mode = "bearer"
```

### openrouter

```toml
[providers.openrouter]
id = "openrouter"
base_url = "https://openrouter.ai/api/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.openrouter.auth]
mode = "bearer"

# Optional attribution headers:
# [[providers.openrouter.headers]]
# name = "HTTP-Referer"
# value = "https://example.local"
# [[providers.openrouter.headers]]
# name = "X-OpenRouter-Title"
# value = "EggPool"
```

### together

```toml
[providers.together]
id = "together"
base_url = "https://api.together.ai/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.together.auth]
mode = "bearer"
```

### fireworks

```toml
[providers.fireworks]
id = "fireworks"
base_url = "https://api.fireworks.ai/inference/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.fireworks.auth]
mode = "bearer"
```

### zai

```toml
[providers.zai]
id = "zai"
base_url = "https://api.z.ai/api/paas/v4"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.zai.auth]
mode = "bearer"
```

Mark as live-verification-required until model list and chat probe pass.

### alibaba

```toml
[providers.alibaba]
id = "alibaba"
base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.alibaba.auth]
mode = "bearer"
```

Document regional alternatives:

```toml
# US Virginia: https://dashscope-us.aliyuncs.com/compatible-mode/v1
# China Beijing: https://dashscope.aliyuncs.com/compatible-mode/v1
# Hong Kong: https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1
```

### novita

Keep commented and mark as live-verification-required until docs or live probes confirm the exact base URL and model-list endpoint.

### minimax

Keep host-level base URL with versioned paths unless the live verifier proves otherwise:

```toml
[providers.minimax]
id = "minimax"
base_url = "https://api.minimaxi.com"
protocols = ["openai", "anthropic"]
openai_path = "/v1/chat/completions"
anthropic_path = "/anthropic/v1/messages"
models_path = "/v1/models"

[providers.minimax.auth]
mode = "bearer"
```

If live verification shows MiniMax requires `X-Api-Key`, switch only its auth block:

```toml
[providers.minimax.auth]
mode = "api_key"
header = "X-Api-Key"
```

### generalcompute

Fix the likely doubled version path:

```toml
[providers.generalcompute]
id = "generalcompute"
base_url = "https://api.generalcompute.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_method = "POST"
models_path = "/models/list"

[providers.generalcompute.auth]
mode = "bearer"
```

If the real provider expects host-level base URL, use this instead:

```toml
base_url = "https://api.generalcompute.com"
openai_path = "/v1/chat/completions"
models_path = "/v1/models/list"
```

The validator should reject the current broken combination: `base_url = ".../v1"` plus `models_path = "/v1/..."`.

### neuralwatt

Keep commented and mark as live-verification-required. If OpenAI-compatible verification passes:

```toml
[providers.neuralwatt]
id = "neuralwatt"
base_url = "https://api.neuralwatt.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.neuralwatt.auth]
mode = "bearer"
```

### ollama-local

```toml
[providers.ollama-local]
id = "ollama-local"
base_url = "http://localhost:11434/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.ollama-local.auth]
mode = "none"
```

Do not require a fake upstream API key for auth. If the account model still requires a key for identity/accounting, add `requires_api_key = false` or permit `api_key = ""` for providers with `auth.mode = "none"`.

### ollama-cloud

```toml
[providers.ollama-cloud]
id = "ollama-cloud"
base_url = "https://ollama.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.ollama-cloud.auth]
mode = "bearer"
```

## Implementation phases

### Phase 1 — Config model and backwards compatibility

1. Add `ProviderAuthConfig`, `ProviderStaticHeaderConfig`, `ProviderModelsEndpointConfig`, and `ProviderVerifyConfig` to `src/eggpool/models/config.py`.
2. Add these fields to `ProviderConfig` with backwards-compatible defaults.
3. Preserve `models_method` and `models_path`; synthesize `models_endpoint` when absent.
4. Add validators for duplicate version prefixes and invalid header definitions.
5. Add tests for old-style and new-style config loading.

Acceptance criteria:

- Existing configs continue to load.
- New `[providers.<id>.auth]` and `[[providers.<id>.headers]]` blocks load.
- Duplicate `/v1` configurations fail fast with a clear message.

### Phase 2 — Shared contract renderer

1. Add `src/eggpool/providers/contract.py`.
2. Move auth rendering out of `src/eggpool/proxy/client.py`.
3. Replace `filter_request_headers(headers, upstream_api_key)` with `sanitize_request_headers(headers)`, then merge provider auth/static headers in coordinator.
4. Update `_execute_non_streaming()` and `_execute_streaming()` to resolve `ProviderConfig` from the selected provider id and pass it to the renderer.
5. Update `fetch_models_for_account()` to accept the provider config or a rendered model-list request.
6. Ensure `auth.mode = "none"` sends no upstream auth and works for Ollama local.

Acceptance criteria:

- OpenCode Go still sends `Authorization: Bearer ***`.
- Ollama local sends no upstream credential header.
- A test provider configured with `auth.mode = "api_key"`, `auth.header = "X-Api-Key"` sends exactly that header.
- Static provider headers are included after local credential stripping.

### Phase 3 — URL composition hardening

1. Implement `compose_provider_url(provider_cfg, path)` and use it for chat, messages, and models.
2. Pass absolute URLs to HTTPX to avoid root-relative base path loss.
3. Add tests for Together, GeneralCompute, MiniMax, OpenRouter, Alibaba, and duplicate-version rejection.

Acceptance criteria:

- GeneralCompute resolves to `https://api.generalcompute.com/v1/models/list`, not `/v1/v1/models/list` and not host-root `/models/list`.
- Provider base paths are preserved.
- All endpoint URL tests pass.

### Phase 4 — Template corrections

1. Update root `config.example.toml`.
2. Update bundled `src/eggpool/_share/config.example.toml`.
3. Add explicit auth blocks for every provider.
4. Correct GeneralCompute path composition.
5. Mark providers as verified, unverified, or live-verification-required in comments.
6. Set Ollama local auth mode to `none`.

Acceptance criteria:

- No provider template double-includes `/v1`.
- Template linter passes.

### Phase 5 — Live verifier upgrade

Upgrade `scripts/verify_upstream_auth.py` as the canonical operator diagnostic path:

```bash
uv run python scripts/verify_upstream_auth.py --config config.toml --provider minimax --verbose
uv run python scripts/verify_upstream_auth.py --config config.toml --all --verbose
```

For each provider/account, print:

- provider id
- account name
- auth mode/header/scheme, redacted
- resolved model-list URL
- model-list method/body/query, redacted
- model-list status and parsed model count
- resolved chat URL
- optional minimal chat probe status when `verify.probe_model` is configured

Acceptance criteria:

- MiniMax `401` output identifies model-list versus chat failure and shows the redacted auth shape and resolved URL.
- GeneralCompute `404` output shows the resolved URL.
- The script exits nonzero if any required endpoint fails.

### Phase 6 — Test matrix

Add tests:

- `tests/providers/test_contract_auth.py`
- `tests/providers/test_contract_urls.py`
- `tests/catalog/test_fetcher_contract.py`
- `tests/api/test_provider_specific_auth_forwarding.py`
- `tests/config/test_provider_template_contracts.py`

Core fixtures:

```python
def test_generalcompute_models_url_has_single_v1():
    cfg = ProviderConfig(
        id="generalcompute",
        base_url="https://api.generalcompute.com/v1",
        openai_path="/chat/completions",
        models_path="/models/list",
    )
    assert compose_provider_url(cfg, cfg.models_path) == (
        "https://api.generalcompute.com/v1/models/list"
    )


def test_duplicate_v1_rejected():
    with pytest.raises(ConfigError):
        ProviderConfig(
            id="bad",
            base_url="https://api.example.com/v1",
            openai_path="/v1/chat/completions",
        )


def test_minimax_host_level_v1_path_ok():
    cfg = ProviderConfig(
        id="minimax",
        base_url="https://api.minimaxi.com",
        openai_path="/v1/chat/completions",
    )
    assert compose_provider_url(cfg, cfg.openai_path) == (
        "https://api.minimaxi.com/v1/chat/completions"
    )


def test_api_key_auth_header():
    cfg = ProviderConfig(
        id="example",
        base_url="https://api.example.com/v1",
        auth=ProviderAuthConfig(mode="api_key", header="X-Api-Key"),
    )
    assert build_auth_headers(cfg, "secret") == {"X-Api-Key": "secret"}
```

Also add a template linter that parses every uncommented provider example block from the bundled config, constructs `ProviderConfig`, and verifies composed URLs contain no duplicate version prefix.

### Phase 7 — Documentation

Update README and deployment docs:

- Explain provider contracts.
- Explain base-url versioning versus path versioning.
- Add troubleshooting:
  - `401`: wrong key, wrong auth mode/header, wrong endpoint/product, expired key.
  - `403`: permission/region/model access.
  - `404`: wrong base URL/path composition, model-list unavailable, typo in path.
  - `429`/`402`: rate limit or quota.
- Document `verify_upstream_auth.py` as first-line diagnosis.

## Immediate operator workarounds before code changes

For GeneralCompute, test:

```toml
[providers.generalcompute]
id = "generalcompute"
base_url = "https://api.generalcompute.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_method = "POST"
models_path = "/models/list"
```

For MiniMax, keep host-level base URL and versioned paths:

```toml
[providers.minimax]
id = "minimax"
base_url = "https://api.minimaxi.com"
protocols = ["openai", "anthropic"]
openai_path = "/v1/chat/completions"
anthropic_path = "/anthropic/v1/messages"
models_path = "/v1/models"
```

Then run the direct verifier. If MiniMax still returns `401`, inspect key class and auth header requirements before changing routing.

## Final acceptance checklist

- [ ] Provider auth is configurable and shared by chat, messages, and model listing.
- [ ] Provider static headers are supported and redacted in logs.
- [ ] URL composition preserves provider base paths and cannot silently create duplicate `/v1/v1` endpoints.
- [ ] GeneralCompute template no longer double-includes `/v1`.
- [ ] MiniMax verifier output shows whether the `401` is key/auth/header/path related.
- [ ] Ollama local can run without upstream auth.
- [ ] Every bundled provider has an explicit contract block.
- [ ] Every bundled provider is marked verified, unverified, or live-verification-required.
- [ ] `scripts/verify_upstream_auth.py --all --verbose` is the canonical provider onboarding diagnostic.
- [ ] Unit tests cover auth rendering, URL composition, catalog fetch, proxy dispatch, and template linting.
