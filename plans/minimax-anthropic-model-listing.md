# MiniMax Anthropic model listing fix plan

## Goal

Keep MiniMax International on the Anthropic-compatible transport, but make its documented model listing endpoint populate EggPool's catalog so `eggpool configsetup opencode` can export direct MiniMax models.

## Current behavior

The bundled MiniMax template uses `https://api.minimax.io/anthropic` and `anthropic_path = "/v1/messages"`, but disables model discovery with `models_endpoint.method = "DISABLED"`. Because `configsetup opencode` reads persisted catalog rows from SQLite, MiniMax models only appear if static seeds were copied into the active config and persisted by a catalog refresh.

## Required change 1: enable live model listing in the MiniMax template

Edit `src/eggpool/providers/_templates.toml` in `[providers.minimax]`.

Change the notes to say the provider uses documented Anthropic model discovery, not that static seeds are required.

Add or keep these fields:

```toml
models_method = "GET"
models_path = "/v1/models"
```

Replace the disabled endpoint block:

```toml
[providers.minimax.models_endpoint]
method = "DISABLED"
required = false
```

with:

```toml
[providers.minimax.models_endpoint]
method = "GET"
path = "/v1/models"
required = true
```

Update verification to use a model ID returned by the live endpoint:

```toml
[providers.minimax.verify]
probe_model = "MiniMax-M3"
probe_protocol = "anthropic"
require_models = true
```

Keep auth as API-key style for now because the known-working model listing call uses `X-Api-Key`/`x-api-key`:

```toml
[providers.minimax.auth]
mode = "api_key"
header = "x-api-key"
```

Keep the `anthropic-version` static header unless later live request testing proves MiniMax rejects it for inference.

## Required change 2: update static fallback seeds

Keep static models as fallback only, but update them to docs-native model IDs rather than older prefixed IDs.

Use at least:

```toml
[[providers.minimax.static_models]]
id = "MiniMax-M3"
display_name = "MiniMax-M3"
protocol = "anthropic"
supports_tools = true
supports_vision = false

[[providers.minimax.static_models]]
id = "MiniMax-M2.7"
display_name = "MiniMax-M2.7"
protocol = "anthropic"
supports_tools = true
supports_vision = false

[[providers.minimax.static_models]]
id = "MiniMax-M2.5"
display_name = "MiniMax-M2.5"
protocol = "anthropic"
supports_tools = true
supports_vision = false
```

Only include hard-coded context/output limits if we have a documented source for the exact limits. Otherwise let catalog limit overrides handle operator-specific caps.

## Required change 3: make Anthropic model-list normalization robust

Inspect `src/eggpool/catalog/normalizer.py`. It already has `normalize_anthropic_models`, but auto-detection currently depends mainly on Anthropic's native top-level `type = "list"` marker. MiniMax's response may be Anthropic-shaped but omit that marker.

Add a helper that treats the response as Anthropic-compatible when any of these are true:

- `raw_response.get("type") == "list"`
- top-level keys include `first_id`, `has_more`, or `last_id`
- any item in `data` has `display_name` and does not have OpenAI's `object` field

Then call `normalize_anthropic_models(raw_response)` when that helper returns true.

This ensures live MiniMax rows persist with `protocol = "anthropic"` rather than becoming OpenAI-shaped rows with unresolved protocol.

## Validation

After implementing, run:

```bash
python -m compileall src/eggpool/catalog/normalizer.py
python -m compileall src/eggpool/providers src/eggpool/catalog
```

With a config containing an enabled MiniMax account, run:

```bash
eggpool --config /path/to/config.toml models refresh
```

Then verify persistence:

```bash
sqlite3 /path/to/usage.sqlite3 '
select provider_id, model_id, protocol, resolution_status
from provider_model_metadata
where provider_id = "minimax"
order by model_id;
'
```

Expected rows should include `MiniMax-M3`, `MiniMax-M2.7`, and `MiniMax-M2.5` with `provider_id = "minimax"` and `protocol = "anthropic"`.

Finally verify OpenCode export:

```bash
eggpool --config /path/to/config.toml configsetup opencode | grep -i minimax
```

Expected output should include provider-suffixed direct MiniMax model IDs such as:

```text
MiniMax-M3/minimax
MiniMax-M2.7/minimax
MiniMax-M2.5/minimax
```

## Operator caveat

Updating the bundled template does not automatically rewrite an existing local `config.toml` provider block. Existing users with `[providers.minimax.models_endpoint] method = "DISABLED"` must reconnect MiniMax or manually update their config block before `models refresh` can discover live MiniMax models.

## Non-goal

Do not solve the separate `insufficient_balance_error (1008)` inference issue in this change. Model listing works and should populate the catalog. Upstream inference errors should remain runtime health/backoff signals rather than blocking catalog visibility.