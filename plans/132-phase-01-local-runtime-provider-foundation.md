# Plan 132 — Phase 1: Local Runtime Provider Foundation

## Objective

Make local OpenAI/Anthropic-compatible servers first-class provider instances using the existing provider, catalog, and routing architecture.

## In scope

Primary presets:

- Ollama;
- LM Studio;
- llama.cpp `llama-server`;
- vLLM;
- LocalAI.

Optional only if trivial after the generic path exists: NVIDIA NIM.

Also add a generic custom compatible-endpoint onboarding mode supporting OpenAI Chat Completions, Anthropic Messages, or both when explicitly declared/probed.

## Design rules

- A local host is a provider instance, not an account variant. Example IDs: `ollama-mac`, `ollama-rpi5`, `vllm-gpu`.
- Do not add `base_url` to `AccountConfig` for this work.
- Reuse `ProviderConfig`, `_templates.toml`, `providers/connect.py`, catalog refresh, `ProviderClientPool`, `routing_priority`, account weight, and `models.collapse_models`.
- Do not add network discovery/mDNS scanning.
- Do not add provider-specific Python SDKs.

## Work items

### 1. Correct Ollama's contract

Review the current `ollama-local` template. Remove dependence on a fixed `probe_model = "llama3.2"`. Verification should first call the model-list endpoint, accept any valid installed model set, and only run a generation probe when an actual discovered model is available and a generation probe is required.

Represent Ollama's currently supported OpenAI and Anthropic compatibility surfaces accurately. Capability metadata must remain conservative for unsupported Anthropic features rather than marking the entire protocol as feature-complete.

### 2. Add local runtime templates

Add templates for LM Studio, llama.cpp, vLLM, and LocalAI using current documented default endpoint conventions. Each template must define only verified paths/auth defaults and should use `auth.mode = "none"` where local defaults genuinely require no authentication.

Do not hardcode a model ID unless the runtime itself guarantees it.

### 3. Support named instances in `eggpool connect`

The connect flow should allow a preset to be instantiated under a user-selected provider ID and base URL. Defaults should make the common localhost case easy, but the operator must be able to enter a LAN hostname/IP.

Suggested interaction:

1. choose Local / Hosted / Aggregator / Custom category;
2. choose runtime preset;
3. enter provider instance ID;
4. accept/edit default base URL;
5. configure auth only when needed;
6. probe `/models` or configured model-list path;
7. show detected models/protocol result;
8. write config and use the existing validated live-rehash/restart path.

Do not make `connect` silently restart a healthy server when live control is unavailable.

### 4. Generic compatible endpoint

Provide a bounded custom flow requiring the operator to supply:

- instance ID;
- base URL;
- protocol(s);
- auth mode/header if any;
- endpoint paths only when they differ from defaults.

Probe capabilities conservatively. Unknown capability remains unknown, not supported.

### 5. Verify pooling through existing routing

Add focused tests demonstrating two named local provider instances exposing the same model can be represented and, with `collapse_models=true`, routed through existing provider/model routing. No special local balancing strategy is allowed.

## Tests

Add/adjust tests in existing provider/connect/catalog/routing suites. Minimum coverage:

- Ollama verification succeeds with an arbitrary installed model name and no `llama3.2`;
- empty model list produces a clear onboarding result rather than a false provider failure;
- no-auth local provider emits no authorization header;
- named local instance preserves custom provider ID and base URL;
- two local instances with the same model participate in normal collapsed-model selection;
- generic custom endpoint rejects malformed URLs/protocol declarations;
- current cloud provider onboarding remains unchanged.

No new CI job or local-runtime daemon is required in CI. Use fixture/respx contracts; live runtime probes remain opt-in developer checks.

## Acceptance criteria

- `eggpool connect` can create distinct Ollama, LM Studio, llama.cpp, vLLM, and LocalAI provider instances.
- Local onboarding does not require a particular model to be installed.
- Authentication is optional for no-auth local servers.
- Existing `ProviderClientPool` and router handle local instances without special-case routing code.
- `collapse_models=true` can pool a shared model across multiple local instances.
- `check-config`/rehash validation still protects config application.
- No mandatory dependency or CI expansion.

## Out of scope

LAN auto-discovery, load telemetry from GPU runtimes, automatic model pulling/loading, embeddings/audio APIs, and per-account endpoint URLs.
