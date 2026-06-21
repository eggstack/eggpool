# MiniMax URL/Auth Correction Plan

## Context

The provider-contract corrective pass has been implemented, but direct retesting still returns HTTP 401 for MiniMax. Review of the current implementation shows that the contract work is only partially applied:

- `src/eggpool/providers/contract.py` now centralizes auth header construction and absolute URL composition.
- `src/eggpool/catalog/fetcher.py` uses `compose_provider_url()` for model listing when `provider_cfg` is present.
- `src/eggpool/request/coordinator.py` uses provider-aware headers for generation, but still sends only `openai_path` / `anthropic_path` to `httpx.AsyncClient.build_request()` for chat and streaming.
- The MiniMax template still defaults to `https://api.minimaxi.com`, which is the China-side MiniMax host in common integrations. For an international `minimax.io` token, the expected OpenAI-compatible host is `https://api.minimax.io/v1`.

This means MiniMax can still fail for two independent reasons:

1. The configured endpoint is the wrong region/product host for the token.
2. Chat and streaming dispatch are not using the same absolute URL composition as model listing.

The goal of this pass is to remove those ambiguities, make MiniMax international the default, keep a separate MiniMax China template, wire absolute URL composition into all outbound paths, and improve verifier/config checks so 401s become actionable.

## Required corrections

### 1. Split MiniMax into international and China templates

Update both `config.example.toml` and `src/eggpool/_share/config.example.toml`.

The default `minimax` provider should target the international host:

```toml
# MiniMax International — OpenAI compatible
# Status: live-verification-required; use for keys from minimax.io
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax.accounts]]
name = "default"
api_key = "sk-your-minimax-key"

[providers.minimax.auth]
mode = "bearer"
```

Add a separate China-region template instead of overloading the international provider:

```toml
# MiniMax China — OpenAI compatible
# Status: live-verification-required; use for keys from the China MiniMax console
[providers.minimax-cn]
id = "minimax-cn"
base_url = "https://api.minimaxi.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax-cn.accounts]]
name = "default"
api_key = "sk-your-minimax-cn-key"

[providers.minimax-cn.auth]
mode = "bearer"
```

Do not keep the current ambiguous default:

```toml
base_url = "https://api.minimaxi.com"
openai_path = "/v1/chat/completions"
```

That host-level-with-versioned-path shape is internally consistent, but it encourages users with `minimax.io` keys to hit the wrong host and receive 401.

### 2. Apply absolute URL composition to chat and streaming dispatch

`RequestCoordinator._execute_non_streaming()` and `_execute_streaming()` currently do this:

```python
upstream_path = self._get_upstream_path(context.protocol, selected.provider_id)
upstream_request = client.build_request(
    "POST",
    upstream_path,
    headers=headers,
    content=context.original_body,
)
```

Replace this with a provider-aware URL resolver:

```python
def _get_upstream_url(self, protocol: str, provider_id: str | None = None) -> str:
    if provider_id and self._config is not None:
        provider_cfg = self._config.providers.get(provider_id)
        if provider_cfg is not None:
            from eggpool.providers.contract import compose_provider_url

            path = (
                provider_cfg.anthropic_path
                if protocol == "anthropic"
                else provider_cfg.openai_path
            )
            return compose_provider_url(provider_cfg, path)

    if protocol == "anthropic":
        return "/messages"
    return "/chat/completions"
```

Then update both dispatch paths:

```python
upstream_url = self._get_upstream_url(context.protocol, selected.provider_id)
upstream_request = client.build_request(
    "POST",
    upstream_url,
    headers=headers,
    content=context.original_body,
)
```

Also update variable names from `upstream_path` to `upstream_url` in logs/tests to avoid hiding path-vs-absolute-url bugs.

Acceptance criteria:

- `https://api.minimax.io/v1` + `/chat/completions` dispatches to `https://api.minimax.io/v1/chat/completions`.
- `https://api.minimaxi.com/v1` + `/chat/completions` dispatches to `https://api.minimaxi.com/v1/chat/completions`.
- `https://opencode.ai/zen/go/v1` + `/chat/completions` dispatches to `https://opencode.ai/zen/go/v1/chat/completions`.
- Catalog fetch and chat dispatch use the same URL composition rules.

### 3. Add a hard guard against accidentally storing `Bearer ...` as the API key

Because provider auth mode `bearer` prepends `Bearer ` at send time, a stored key that already starts with `Bearer ` results in:

```text
Authorization: Bearer Bearer <token>
```

That is a plausible source of 401 and currently not rejected.

Add validation in account credential loading or provider contract rendering. Prefer fail-fast validation in config startup, because it gives the operator a clear error before the server runs.

Suggested implementation in `AppConfig.validate_account_credentials()` or the account validation path:

```python
for provider_id, provider in self.providers.items():
    if provider.auth.mode == "bearer":
        for account in provider.accounts:
            key = account.api_key or os.environ.get(account.api_key_env, "")
            if key.strip().lower().startswith("bearer "):
                raise ConfigError(
                    f"Provider {provider_id!r} account {account.name!r}: "
                    "api_key must be the raw token, not 'Bearer <token>'. "
                    "EggPool adds the Bearer scheme automatically."
                )
```

Also add the same guard to `scripts/verify_upstream_auth.py`, so operators get an explicit verifier error rather than a misleading upstream 401.

Acceptance criteria:

- `api_key = "Bearer sk-..."` fails configuration validation for bearer-mode providers.
- The verifier reports `raw key must not include Bearer prefix` before making a network request.
- `auth.mode = "raw_authorization"` remains available for providers that truly require a raw authorization header.

### 4. Fix the verifier to use provider `verify` config

The upgraded verifier currently probes chat only if `--openai-model` or `--anthropic-model` is passed. It does not use `[providers.<id>.verify] probe_model`, even though the config model includes `ProviderVerifyConfig`.

Implement precedence:

1. CLI `--openai-model` or `--anthropic-model` wins.
2. If CLI model is absent, use `provider.verify.probe_model` and `provider.verify.probe_protocol`.
3. If no probe model is available, only model-list verification runs.

Suggested verifier behavior:

```python
verify_cfg = provider_cfg.get("verify", {})
probe_model = verify_cfg.get("probe_model")
probe_protocol = verify_cfg.get("probe_protocol", "openai")

resolved_openai_model = args.openai_model
resolved_anthropic_model = args.anthropic_model
if probe_model and not args.openai_model and not args.anthropic_model:
    if probe_protocol == "anthropic":
        resolved_anthropic_model = probe_model
    else:
        resolved_openai_model = probe_model
```

Add MiniMax examples:

```toml
[providers.minimax.verify]
probe_model = "MiniMax-M2.5"
probe_protocol = "openai"
```

Do not require a probe model in the example if model IDs are unstable, but document the field immediately below the template.

Acceptance criteria:

- `uv run python scripts/verify_upstream_auth.py --config config.toml --provider minimax --verbose` runs a chat probe when `verify.probe_model` is configured.
- The verbose output shows the resolved chat URL and redacted auth shape.
- The verifier distinguishes model-list failure from chat failure.

### 5. Improve MiniMax-specific request sanitization only if needed after URL/host fixes

Do not prematurely add provider-specific request mutation unless the corrected endpoint still fails with non-auth errors. MiniMax appears OpenAI-compatible and uses `Authorization: Bearer` in common integrations. However, if corrected endpoint tests produce `400` rather than `401`, inspect payload compatibility.

Known compatibility items to watch:

- MiniMax may require `temperature` in `[0, 1.0]`; clamp only if failures show this is needed.
- Avoid sending unsupported OpenAI extras from clients, such as `parallel_tool_calls`, `store`, `reasoning_effort`, `response_format`, `stream_options`, or unknown fields, unless MiniMax documents support.
- For streaming, EggPool currently injects `stream_options.include_usage = true` for all OpenAI-compatible providers. If MiniMax rejects `stream_options`, add a provider config toggle, for example:

```toml
[providers.minimax.compat]
inject_stream_options_include_usage = false
```

Do not add this compatibility layer in the first correction unless the response body confirms MiniMax rejects those fields. A 401 is upstream auth/endpoint, not request-body compatibility.

### 6. Add regression tests

Add or update tests in the existing unit test structure.

#### URL dispatch tests

Add coordinator-level tests proving chat dispatch uses absolute provider URL composition, not just path strings.

Test cases:

```python
@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        (
            "https://api.minimax.io/v1",
            "/chat/completions",
            "https://api.minimax.io/v1/chat/completions",
        ),
        (
            "https://api.minimaxi.com/v1",
            "/chat/completions",
            "https://api.minimaxi.com/v1/chat/completions",
        ),
        (
            "https://opencode.ai/zen/go/v1",
            "/chat/completions",
            "https://opencode.ai/zen/go/v1/chat/completions",
        ),
    ],
)
def test_generation_uses_composed_absolute_url(...):
    ...
```

The test should assert against the request URL observed by `respx` or the existing HTTPX mock transport.

#### MiniMax template tests

Add template linter assertions:

- `providers.minimax.base_url == "https://api.minimax.io/v1"`.
- `providers.minimax.openai_path == "/chat/completions"`.
- `providers.minimax-cn.base_url == "https://api.minimaxi.com/v1"`.
- Neither MiniMax provider composes a URL with `/v1/v1`.

#### Bearer-prefix tests

Add config validation tests:

```python
def test_bearer_mode_rejects_bearer_prefixed_key():
    ...
```

Also test the env-var path:

```python
def test_bearer_mode_rejects_bearer_prefixed_env_key(monkeypatch):
    ...
```

#### Verifier tests

Update `tests/unit/test_verify_upstream_auth.py`:

- Verifier uses `provider.verify.probe_model` when CLI model is absent.
- CLI `--openai-model` overrides provider verify config.
- Bearer-prefixed key is rejected before network call.
- Verbose output includes composed URL and redacted auth shape.

### 7. Operator validation after patch

After implementing the code/template fixes, test with the international MiniMax provider:

```bash
uv run eggpool --config config.toml check-config
uv run python scripts/verify_upstream_auth.py \
  --config config.toml \
  --provider minimax \
  --verbose
```

If no `verify.probe_model` is configured, run:

```bash
uv run python scripts/verify_upstream_auth.py \
  --config config.toml \
  --provider minimax \
  --openai-model MiniMax-M2.5 \
  --verbose
```

Expected verbose output should include:

```text
resolved_url=https://api.minimax.io/v1/chat/completions
auth=Authorization: Bearer ***
```

If this still returns 401 after confirming the key is raw and the URL is `api.minimax.io`, the problem is external to EggPool's request construction: wrong/expired key, key not enabled for API use, organization/project mismatch, or MiniMax-side account restriction.

Then run through EggPool:

```bash
curl -sS http://127.0.0.1:11300/v1/chat/completions \
  -H "Authorization: Bearer $EGGPOOL_SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "MiniMax-M2.5@minimax",
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 8
  }'
```

Confirm logs show the same resolved upstream URL as the verifier.

## Implementation checklist

- [ ] Change MiniMax default template to `https://api.minimax.io/v1` + `/chat/completions`.
- [ ] Add separate `minimax-cn` template using `https://api.minimaxi.com/v1`.
- [ ] Update both root and bundled example config files.
- [ ] Add `_get_upstream_url()` or equivalent in `RequestCoordinator`.
- [ ] Use `compose_provider_url()` for non-streaming chat dispatch.
- [ ] Use `compose_provider_url()` for streaming chat dispatch.
- [ ] Rename local variables from `upstream_path` to `upstream_url` where absolute URLs are used.
- [ ] Add bearer-prefixed API key validation for config and env-var credentials.
- [ ] Add bearer-prefix rejection to `scripts/verify_upstream_auth.py`.
- [ ] Make verifier consume `[providers.<id>.verify] probe_model` and `probe_protocol`.
- [ ] Add regression tests for MiniMax international URL composition.
- [ ] Add regression tests for MiniMax China URL composition.
- [ ] Add regression tests proving generation and catalog fetch share URL composition rules.
- [ ] Add template linter coverage for `minimax` and `minimax-cn`.
- [ ] Re-run direct verifier against MiniMax international.
- [ ] Re-run EggPool request against `MiniMax-M2.5@minimax` or another known-accessible MiniMax model.

## Expected final state

After this pass, a user with a `minimax.io` API token should not need to know about the `api.minimaxi.com` China host. The default provider should route to `https://api.minimax.io/v1/chat/completions`, send exactly `Authorization: Bearer <raw-token>`, and use the same URL composition path for verifier, model refresh, non-streaming generation, and streaming generation.

Remaining 401s after these corrections should be treated as actual credential/account problems rather than EggPool provider-contract ambiguity.
