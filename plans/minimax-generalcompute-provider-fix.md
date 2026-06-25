# MiniMax and GeneralCompute Provider Correctness Plan

## Problem Statement

`minimax.io` and `generalcompute` are currently failing in EggPool despite operator-verified working API keys. The failures appear to be provider-contract mismatches rather than credential failures.

The design goal for this pass is to make EggPool's provider contracts match the known-working behavior from `codegg` and the provider surfaces actually used by the operator:

- MiniMax token-plan keys from `minimax.io` should use the MiniMax Anthropic-compatible surface, not the currently configured OpenAI-compatible `/v1/chat/completions` surface.
- GeneralCompute PAYG should initially be treated as a normal OpenAI-compatible provider, matching codegg, rather than using custom `POST /models/list` discovery logic unless live docs/tests prove that endpoint is required.
- Catalog discovery failure must not mask a working chat endpoint when a provider has known static models or optional model-list semantics.
- Verifier output should make endpoint/auth mismatches obvious without leaking credentials.

This is intentionally a focused provider-contract correction, not a router rewrite.

## Current Evidence

### EggPool MiniMax current contract

EggPool currently advertises MiniMax International approximately as:

```toml
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[providers.minimax.auth]
mode = "bearer"
```

This causes EggPool to send requests to:

```text
POST https://api.minimax.io/v1/chat/completions
Authorization: Bearer <token-plan-key>
```

Observed failure: HTTP 401.

### codegg MiniMax current contract

The working codegg provider does not use that OpenAI-compatible surface. It constructs MiniMax with `AnthropicProvider`, sets base URL to `https://api.minimax.io/anthropic`, and uses static MiniMax model IDs. The Anthropic provider path appends `/v1/messages`, sends the key as `x-api-key`, and includes `anthropic-version: 2023-06-01`.

Effective codegg request shape:

```text
POST https://api.minimax.io/anthropic/v1/messages
x-api-key: <token-plan-key>
anthropic-version: 2023-06-01
content-type: application/json
```

This strongly suggests that the operator's `minimax.io` token-plan key is valid for the Anthropic-compatible endpoint, not the EggPool OpenAI-compatible endpoint currently configured.

### EggPool GeneralCompute current contract

EggPool currently configures GeneralCompute approximately as:

```toml
[providers.generalcompute]
id = "generalcompute"
base_url = "https://api.generalcompute.com/v1"
protocols = ["openai"]
models_method = "POST"
models_path = "/models/list"

[providers.generalcompute.auth]
mode = "bearer"
```

Observed failure: HTTP 404.

### codegg GeneralCompute current contract

The working codegg provider treats GeneralCompute as a plain OpenAI-compatible provider:

```rust
OpenAiCompatibleProvider::simple_with_credential(
    "generalcompute",
    "GeneralCompute",
    credential,
    "https://api.generalcompute.com/v1",
)
```

That implies:

```text
GET  https://api.generalcompute.com/v1/models
POST https://api.generalcompute.com/v1/chat/completions
Authorization: Bearer <key>
```

There is no codegg evidence for custom `POST /models/list` behavior. The current EggPool `/models/list` setting should therefore be considered suspect until live docs/tests prove it is necessary.

## Design Decisions

### MiniMax

Use the MiniMax Anthropic-compatible endpoint as the default for `minimax.io` token-plan keys.

Configure MiniMax as an Anthropic-compatible provider:

```toml
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/anthropic"
protocols = ["anthropic"]
anthropic_path = "/v1/messages"

[providers.minimax.auth]
mode = "api_key"
header = "x-api-key"

[[providers.minimax.headers]]
name = "anthropic-version"
value = "2023-06-01"

[providers.minimax.models_endpoint]
method = "DISABLED"
required = false
```

Rationale:

- `base_url = https://api.minimax.io/anthropic` plus `anthropic_path = /v1/messages` composes to the codegg-equivalent URL `https://api.minimax.io/anthropic/v1/messages`.
- `api_key` auth mode sends the raw key in `x-api-key` instead of adding a Bearer scheme.
- Static `anthropic-version` aligns the provider with the Anthropic-compatible transport.
- Disabling model discovery avoids blocking route eligibility on an endpoint codegg does not depend on.

Do not retain the old `minimax` template as OpenAI-compatible unless documentation confirms a separate valid endpoint/key class. If retained for power users, name it explicitly, for example `minimax-openai`, mark it experimental, and do not make it the default `minimax.io` token-plan path.

### MiniMax China

Do not blindly apply the same change to `minimax-cn` unless documentation or live testing confirms the China endpoint has the same Anthropic-compatible path and auth semantics.

For this pass:

- Leave `minimax-cn` experimental.
- Add notes that `minimax-cn` still requires live verification.
- If implementing an analogous China Anthropic-compatible block, give it a separate ID such as `minimax-cn-anthropic` until tested.

### GeneralCompute

Treat GeneralCompute PAYG as plain OpenAI-compatible by default:

```toml
[providers.generalcompute]
id = "generalcompute"
base_url = "https://api.generalcompute.com/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_method = "GET"
models_path = "/models"

[providers.generalcompute.auth]
mode = "bearer"
```

Rationale:

- This matches codegg's known-working `OpenAiCompatibleProvider::simple_with_credential` construction.
- It avoids the currently suspicious `POST /models/list` path that plausibly causes the observed 404.
- It preserves ordinary OpenAI-compatible request semantics for chat completions.

If provider docs later prove `POST /models/list` is required for some account type, implement it as an opt-in alternate template, for example `generalcompute-models-list`, not as the default PAYG behavior.

### Catalog Resilience

A provider can have working chat completions even if model listing fails, is absent, or is intentionally disabled. EggPool should support this explicitly.

For providers with `models_endpoint.method = "DISABLED"` or `models_endpoint.required = false`, the catalog refresh should not mark the provider/account as broken solely because no live model list was fetched. Instead, use one of these sources:

1. Static provider models declared in config/template.
2. Model overrides or known model seed entries.
3. Previously cached catalog entries if still within staleness policy.

If EggPool does not currently have a first-class static provider model mechanism, add one in a small, generic way rather than hardcoding MiniMax in catalog logic.

Suggested config shape:

```toml
[[providers.minimax.static_models]]
id = "minimax/minimax-2.7"
display_name = "minimax/minimax-2.7"
protocol = "anthropic"
max_context_tokens = 204800
max_output_tokens = 32000
supports_tools = true
supports_vision = false

[[providers.minimax.static_models]]
id = "minimax/minimax-2.7-highspeed"
display_name = "minimax/minimax-2.7-highspeed"
protocol = "anthropic"
max_context_tokens = 204800
max_output_tokens = 32000
supports_tools = true
supports_vision = false
```

The exact schema can be adjusted to fit existing model override/metadata structures, but the implementation should be provider-generic.

Initial MiniMax static model seed list should mirror the working codegg list unless newer provider documentation gives better names:

- `minimax/minimax-2.7`
- `minimax/minimax-2.7-highspeed`
- `minimax/minimax-2.5`
- `minimax/minimax-2.5-highspeed`
- `minimax/minimax-2.1`
- `minimax/minimax-2.1-highspeed`

Use the current operator-preferred M-series names only if live verification proves the token-plan endpoint accepts those exact IDs. Avoid silently renaming models in a way that makes upstream reject the request.

## Implementation Plan

### Phase 1: Update Provider Templates

Modify `src/eggpool/providers/_templates.toml`.

For `[providers.minimax]`:

- Change `base_url` to `https://api.minimax.io/anthropic`.
- Change `protocols` to `["anthropic"]`.
- Remove `openai_path` unless retaining a separate OpenAI-compatible template.
- Add `anthropic_path = "/v1/messages"`.
- Change auth to:

```toml
[providers.minimax.auth]
mode = "api_key"
header = "x-api-key"
```

- Add static header:

```toml
[[providers.minimax.headers]]
name = "anthropic-version"
value = "2023-06-01"
```

- Add model discovery disablement:

```toml
[providers.minimax.models_endpoint]
method = "DISABLED"
required = false
```

- Update metadata:
  - `status = "experimental"` until live verified through EggPool.
  - `notes = "Uses MiniMax Anthropic-compatible endpoint for minimax.io token-plan keys; static model seed required"`.

For `[providers.generalcompute]`:

- Remove `models_method = "POST"`.
- Remove `models_path = "/models/list"`.
- Add or rely on defaults:

```toml
openai_path = "/chat/completions"
models_method = "GET"
models_path = "/models"
```

- Update notes to: `Plain OpenAI-compatible PAYG endpoint; verify /models and chat completions live`.

### Phase 2: Update Example Config and Docs

Modify `config.example.toml`.

- Make MiniMax International match the new Anthropic-compatible contract.
- Make GeneralCompute match plain OpenAI-compatible behavior.
- Remove comments claiming GeneralCompute uses `POST /models/list` unless that remains as a documented alternate, non-default provider.
- Update any status labels from “401 auth issue unresolved” to “Anthropic-compatible token-plan path; live verification required”.

Modify `docs/providers.md`.

- Move MiniMax International from “OpenAI” protocol notes to Anthropic-compatible notes.
- Explain that `minimax.io` token-plan keys are expected to use the Anthropic-compatible endpoint.
- Explain that GeneralCompute PAYG is treated as plain OpenAI-compatible unless live provider docs prove otherwise.
- Add a troubleshooting note:
  - MiniMax 401 on `/v1/chat/completions` usually means the wrong endpoint family/auth header was used.
  - GeneralCompute 404 on `/models/list` usually means the non-default model listing endpoint was used.

### Phase 3: Add or Reuse Static Model Support

Inspect existing model override/catalog structures before adding new schema. Prefer the smallest provider-generic extension.

Add to `ProviderConfig` if not already present:

```python
class ProviderStaticModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str | None = None
    protocol: ProtocolName | None = None
    max_context_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
```

Then add:

```python
static_models: list[ProviderStaticModelConfig] = Field(default_factory=list)
```

Catalog behavior:

- During refresh, before or after live fetch, merge `provider_cfg.static_models` into the account/provider model set for each enabled account on that provider.
- Static models should look like normalized model rows so the rest of catalog processing, protocol resolution, limits, and persistence remain shared.
- If live `/models` is disabled, static models should still populate the catalog.
- If live `/models` is enabled and returns a model with the same ID, live metadata can augment but must not erase explicit static protocol/limit fields.

Suggested normalized shape:

```python
{
    "model_id": static.id,
    "display_name": static.display_name or static.id,
    "protocol": static.protocol,
    "protocol_source": "static_config" if static.protocol else None,
    "capabilities": {
        "supports_tools": static.supports_tools,
        "supports_vision": static.supports_vision,
    },
    "source_metadata": {
        **static.source_metadata,
        "source": "static_config",
    },
}
```

Limit handling:

- Either map static `max_context_tokens`/`max_output_tokens` into capabilities/source metadata that `ModelLimitResolver` already understands, or apply them as provider-specific effective limits before cache update.
- Keep existing `[providers.<id>.model_overrides]` precedence higher than static model defaults.

### Phase 4: Verify Request Dispatch Path Uses Configured Endpoint Paths

Confirm that `RequestCoordinator` dispatches OpenAI and Anthropic requests using `provider_cfg.openai_path` and `provider_cfg.anthropic_path` through `compose_provider_url`.

If not already correct, update dispatch logic so:

```python
if context.protocol == "openai":
    endpoint_path = provider_cfg.openai_path
elif context.protocol == "anthropic":
    endpoint_path = provider_cfg.anthropic_path
url = compose_provider_url(provider_cfg, endpoint_path)
```

This is critical for MiniMax because the desired URL is:

```text
https://api.minimax.io/anthropic/v1/messages
```

Do not hardcode `/v1/messages` or `/chat/completions` in request dispatch. The provider contract must be authoritative.

### Phase 5: Improve Verifier Diagnostics

Update `scripts/verify_upstream_auth.py` so `--verbose` prints, before each check:

- Provider ID/account name.
- Protocol family being checked.
- Resolved URL with query values redacted.
- Auth shape, e.g. `Authorization: Bearer ***` or `x-api-key: ***`.
- Static header names with values redacted.
- Whether model listing is required, optional, or disabled.

Ensure the verifier supports `models_endpoint.method = "DISABLED"` cleanly:

- It should report `SKIP` for models rather than `FAIL`.
- If `require_models = false`, disabled model discovery should not fail the provider check.
- If static models are configured, print the static model count.

For MiniMax, verifier should exercise Anthropic chat using the configured `anthropic_path` and a configured probe model, for example:

```toml
[providers.minimax.verify]
probe_model = "minimax/minimax-2.7"
probe_protocol = "anthropic"
require_models = false
```

For GeneralCompute, verifier should exercise OpenAI chat using:

```toml
[providers.generalcompute.verify]
probe_protocol = "openai"
require_models = false
```

If a known PAYG model ID exists, add `probe_model`. If not, allow `--openai-model <id>` from the CLI during live verification.

### Phase 6: Tests

Add unit tests for provider URL/auth composition.

Suggested files:

- `tests/test_provider_contracts.py`
- or extend existing provider/config tests if present.

Required assertions:

1. MiniMax config parses with `protocols == ["anthropic"]`.
2. MiniMax chat URL composes to `https://api.minimax.io/anthropic/v1/messages`.
3. MiniMax auth headers contain `x-api-key: <raw key>` and do not contain `Authorization`.
4. MiniMax static headers include `anthropic-version: 2023-06-01`.
5. MiniMax model discovery disabled does not produce a failed fetch.
6. GeneralCompute model URL composes to `https://api.generalcompute.com/v1/models`.
7. GeneralCompute chat URL composes to `https://api.generalcompute.com/v1/chat/completions`.
8. GeneralCompute auth headers contain `Authorization: Bearer <key>`.
9. No provider template composes duplicate version prefixes such as `/v1/v1/`.
10. Static provider models populate catalog entries when model discovery is disabled.

Add verifier tests if feasible:

- Config with `models_endpoint.method = "DISABLED"` reports skip, not failure.
- Config with `require_models = false` can pass if chat probe passes.

Add a regression test for the GeneralCompute bug specifically:

- Template-derived GeneralCompute config must not call `/models/list` by default.

### Phase 7: Migration and Operator Notes

Because existing user configs may already contain the old broken provider blocks, template updates alone are insufficient for deployed users.

Add documentation and/or a CLI warning for stale provider contracts:

MiniMax stale contract detection:

```text
provider.id == "minimax"
base_url == "https://api.minimax.io/v1"
protocols includes "openai"
auth.mode == "bearer"
```

Warn:

```text
MiniMax provider appears to use the old OpenAI-compatible minimax.io contract. Token-plan keys usually require the Anthropic-compatible endpoint. Update base_url to https://api.minimax.io/anthropic, protocols to ["anthropic"], auth to x-api-key, and anthropic_path to /v1/messages.
```

GeneralCompute stale contract detection:

```text
provider.id == "generalcompute"
models_method == "POST"
models_path == "/models/list"
```

Warn:

```text
GeneralCompute provider appears to use the old POST /models/list catalog endpoint. PAYG should be tested first as plain OpenAI-compatible with GET /models and POST /chat/completions.
```

Possible implementation locations:

- `eggpool check-config` warning output.
- `scripts/verify_upstream_auth.py --config ... --provider ... --verbose` warnings.
- Docs-only warning if code warning is too much for this pass.

Do not mutate user config automatically in this pass unless adding an explicit migration command. Silent provider-contract migration could break users who intentionally configured a different endpoint class.

## Acceptance Criteria

### MiniMax

- `eggpool --config config.toml check-config` accepts the new MiniMax contract.
- `scripts/verify_upstream_auth.py --config config.toml --provider minimax --verbose --anthropic-model minimax/minimax-2.7` resolves:

```text
https://api.minimax.io/anthropic/v1/messages
x-api-key: ***
anthropic-version: *** or 2023-06-01 redacted/visible as non-secret
```

- The verifier does not attempt `GET https://api.minimax.io/v1/models` for this provider.
- EggPool `/v1/models` exposes the configured static MiniMax models after startup or `models refresh`.
- A client request to EggPool's Anthropic-compatible endpoint routes to MiniMax and receives upstream response bytes/SSE rather than local 404/503 caused by missing catalog state.
- MiniMax no longer fails with 401 due to `Authorization: Bearer` against the wrong endpoint family.

### GeneralCompute

- `eggpool --config config.toml check-config` accepts the new GeneralCompute contract.
- `scripts/verify_upstream_auth.py --config config.toml --provider generalcompute --verbose` resolves:

```text
GET  https://api.generalcompute.com/v1/models
POST https://api.generalcompute.com/v1/chat/completions
Authorization: Bearer ***
```

- The verifier does not call `POST https://api.generalcompute.com/v1/models/list` by default.
- EggPool no longer records GeneralCompute catalog 404s caused by `/models/list`.
- If `/models` is unavailable but chat works and `require_models = false`, the provider can still be used when models are statically seeded or cached.

### Regression

- Existing verified OpenAI-compatible providers still compose the same URLs as before.
- Existing Anthropic direct provider still uses `x-api-key` and `anthropic-version`.
- No static header can overwrite the configured auth header.
- No config path produces duplicated `/v1/v1` or `/anthropic/anthropic` style URL segments.

## Suggested Commit Breakdown

1. `fix providers: correct minimax and generalcompute templates`
   - `_templates.toml`
   - `config.example.toml`
   - `docs/providers.md`

2. `feat catalog: support static provider model seeds`
   - `ProviderConfig` schema
   - Catalog merge logic
   - Tests

3. `fix verifier: handle disabled optional model discovery`
   - `scripts/verify_upstream_auth.py`
   - Verifier tests

4. `test providers: lock minimax and generalcompute endpoint contracts`
   - URL/auth tests
   - Template regression tests

If keeping the patch small, implement commits 1 and 4 first. That alone should remove the most likely wrong endpoints. Static models can follow if MiniMax cannot appear in the catalog without live `/models` discovery.

## Manual Verification Commands

After updating local config:

```bash
set -a
source .env
set +a

uv run eggpool --config config.toml check-config

uv run python scripts/verify_upstream_auth.py \
  --config config.toml \
  --provider minimax \
  --anthropic-model minimax/minimax-2.7 \
  --verbose

uv run python scripts/verify_upstream_auth.py \
  --config config.toml \
  --provider generalcompute \
  --openai-model <known-generalcompute-model-id> \
  --verbose

uv run eggpool --config config.toml models refresh
uv run eggpool --config config.toml serve
```

Then from a client:

```bash
curl -sS http://127.0.0.1:11300/v1/models \
  -H "Authorization: Bearer $SERVER_API_KEY" | jq
```

For GeneralCompute OpenAI-compatible chat:

```bash
curl -sS http://127.0.0.1:11300/v1/chat/completions \
  -H "Authorization: Bearer $SERVER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<generalcompute-model-id>/generalcompute",
    "messages": [{"role": "user", "content": "Say ok."}],
    "stream": false
  }'
```

For MiniMax Anthropic-compatible messages:

```bash
curl -sS http://127.0.0.1:11300/v1/messages \
  -H "Authorization: Bearer $SERVER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "minimax/minimax-2.7/minimax",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say ok."}],
    "stream": false
  }'
```

Adjust the suffixed model ID depending on `collapse_models` and the final catalog exposure behavior.
