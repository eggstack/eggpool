# Plan 143 — Codex Compatibility Closure: Stateless Same-Protocol Responses Passthrough

Status: ready for implementation after Plan 142

Baseline reviewed: `14ad0227af8a39292fc43a88725284ab5d26a547`

Related plans: 131–142, especially 139–142

## Purpose

EggPool currently advertises Codex as a supported `configsetup` integration, but the integration is no longer compatible with current Codex.

The current upstream Codex provider model has one wire API: OpenAI Responses. `wire_api = "chat"` is explicitly rejected and the custom-provider configuration lives under `model_providers`, while EggPool's current renderer still emits the older `[provider.eggpool]` / `api_key` / provider-local model-table shape.

This changes the value calculation from Plan 139/141. A stateless `/v1/responses` path is no longer speculative API parity; it is required to make an advertised EggPool integration truthful again.

This plan adds the **smallest useful Responses surface**:

```text
POST /v1/responses
    -> stateless validation
    -> existing EggPool admission/routing/reservation/failure isolation
    -> provider explicitly declaring a Responses path
    -> byte/JSON passthrough to that provider
    -> non-streaming or SSE passthrough back to the client
```

It does **not** add Responses ↔ Anthropic transcoding, response-ID persistence, conversation affinity, a third protocol family, or general OpenAI API parity.

## Primary decision

Implement:

```text
responses_stateless_same_protocol: yes
responses_cross_protocol: no
responses_stateful: no
responses_websocket: no
responses_background_jobs: no
```

The implementation must stay smaller than a generalized Responses architecture. If the work starts requiring a third canonical protocol model or persistent response state, stop; that is outside this line of work.

---

# Upstream facts that drive this plan

At the reviewed baseline:

- current Codex defines only `WireApi::Responses` and rejects `wire_api = "chat"`;
- current Codex custom providers are selected through the `model_providers` map;
- Codex provider configuration uses `base_url`, `env_key`/supported auth fields, and `wire_api` rather than EggPool's current `[provider.eggpool]` / `api_key` shape;
- current OpenAI Responses streaming uses typed SSE events such as `response.completed` and `response.failed`, not the Chat Completions `[DONE]` completion contract;
- current Ollama and llama.cpp expose Responses-compatible routes, and other local/OpenAI-compatible runtimes may as well.

Re-check official upstream sources immediately before implementation. Do not preserve a stale claim merely because this plan records the reviewed state.

---

# Scope constraints

## In scope

Expected surfaces:

- `src/eggpool/api/responses.py` — new narrow endpoint wrapper
- `src/eggpool/api/proxy_request.py` — only the minimum endpoint/surface branching needed to reuse admission/context construction
- `src/eggpool/app.py` — register `POST /v1/responses`
- `src/eggpool/models/config.py` — optional provider Responses path declaration
- `src/eggpool/providers/_templates.toml` — declare Responses paths only for currently verified providers
- `src/eggpool/request/coordinator.py` and/or one existing request helper — choose Responses upstream URL and avoid Chat-specific provider transforms
- `src/eggpool/request/limits.py` — recognize Responses `max_output_tokens` if needed by existing limit enforcement
- existing stream completion/observer code — minimal Responses terminal-event recognition; do not build a streaming transcoder
- `src/eggpool/integrations/codex.py` — current Codex configuration shape
- existing integration/request/provider tests
- README/integration docs only where support claims need correction

## Explicitly out of scope

Do **not**:

- add `"responses"` to `ProtocolName`;
- add a third protocol to `SUPPORTED_PROTOCOLS`;
- implement Responses ↔ Anthropic translation;
- translate Responses ↔ Chat Completions;
- create a Responses content IR or general canonical request object;
- persist or route `previous_response_id`;
- persist Responses conversation state;
- implement provider/session affinity for Responses;
- implement `GET /v1/responses/{id}`;
- implement delete/cancel/retrieve Responses endpoints;
- implement Responses WebSocket transport;
- implement `background = true` jobs;
- emulate OpenAI hosted tools;
- add provider SDK dependencies;
- add a Responses provider plugin framework;
- add a new router or retry subsystem;
- add new GitHub Actions jobs, provider matrices, live-provider CI, OS/Python matrices, soak tests, or benchmark gates.

If current Codex cannot work with the stateless subset under these constraints, stop and remove/disable the advertised Codex integration rather than expanding scope.

---

# Architectural rule — Responses is an OpenAI endpoint surface, not a third protocol

EggPool's existing protocol type represents translation families:

```python
ProtocolName = Literal["openai", "anthropic"]
```

Keep that model.

Responses is an OpenAI-family wire endpoint that EggPool will pass through unchanged. The implementation should distinguish **endpoint surface** from **transcoding protocol**.

Preferred shape:

```python
OpenAIRequestSurface = Literal["chat_completions", "responses"]
```

or an equivalently small existing-field extension.

The exact type/name is implementation-defined, but the invariant is not:

- `context.protocol` / `context.upstream_protocol` remain `"openai"` for a Responses passthrough;
- `context.transcode_required` is false;
- no BodyTranscoder is selected;
- the request surface chooses the provider URL and stream completion policy;
- Chat Completions behavior remains the default and unchanged.

Do not overload `ProtocolName` with endpoint variants.

---

# Workstream A — Provider-level Responses declaration

## Goal

Route a Responses request only to providers that explicitly declare a compatible Responses endpoint.

## Configuration

Add one optional field to `ProviderConfig`, preferably:

```python
responses_path: str | None = None
```

Semantics:

- `None` = provider is not eligible for `/v1/responses`;
- non-null path = provider explicitly declares a stateless Responses-compatible POST endpoint relative to its existing `base_url`;
- `protocols` remains `['openai']` or the provider's existing OpenAI/Anthropic list;
- the path is an endpoint-surface declaration, not a new protocol.

Do not add both `supports_responses` and `responses_path` unless there is a concrete need; the path's presence should be sufficient.

## Validation

1. Include `responses_path` in the existing duplicate-version-prefix validation.
2. Compose it through `compose_provider_url()`; do not create a second URL joiner.
3. Keep auth/static header behavior identical to the provider's OpenAI path.
4. `check-config` must reject malformed path/base combinations the same way it rejects bad chat/messages paths.

## Bundled templates

At implementation time, verify official current documentation and add `responses_path = "/responses"` only to providers whose configured base/path combination genuinely exposes that route.

At minimum re-check:

- direct OpenAI;
- Ollama;
- llama.cpp;
- vLLM.

LM Studio or other providers may be included only if their current official documentation is equally clear. Do not turn this into a complete provider survey.

`custom-compatible` should remain usable by an operator who manually configures `responses_path`; do not build a new interactive wizard solely for this field.

## Acceptance criteria

- providers without `responses_path` are never selected for a Responses request;
- providers with `responses_path` retain their existing OpenAI protocol/catalog semantics;
- no `responses` protocol row appears in model/catalog persistence;
- no provider SDK or special provider client is introduced;
- URL composition remains centralized in `compose_provider_url()`.

---

# Workstream B — Add `POST /v1/responses` as a stateless OpenAI passthrough

## Endpoint wrapper

Add a small `src/eggpool/api/responses.py` analogous to `chat_completions.py`.

The endpoint configuration should identify:

- protocol family: `openai`;
- request surface: `responses`;
- OpenAI-shaped error renderer;
- request label: `response` or equivalent.

Register only:

```text
POST /v1/responses
```

Do not add retrieval/cancel/delete routes.

## Stateless request validation

Before durable account selection, reject stateful/asynchronous features that EggPool cannot safely fail over.

Reject locally with an OpenAI-shaped 400 if any of the following are requested:

- `previous_response_id` is non-null/non-empty;
- `conversation` is non-null/present with a real conversation reference;
- `store` is `true`;
- `background` is `true`.

Allow:

- fields absent;
- `store = false`;
- normal stateless `input`;
- `instructions`;
- model/tool/reasoning/text configuration that can be forwarded unchanged;
- `stream = true` or false.

Do not silently strip a stateful field and continue. Reject it explicitly so the client never believes provider state is being preserved.

## Model normalization

Reuse the existing EggPool model/provider suffix behavior:

- client may request `model` or `model/provider-id` under existing model exposure rules;
- route using the existing base model/provider parsing;
- send the upstream provider the base model ID, not the EggPool provider suffix.

Do not add a separate Responses model catalog.

## Admission and limits

Reuse:

- auth;
- server body-size limit;
- JSON/model validation;
- generation lease;
- model context/input/output limit enforcement where valid;
- reservation estimation;
- durable request/attempt/reservation creation;
- routing/account health;
- finalization supervisor;
- request ID propagation.

Responses uses `max_output_tokens`. Update the existing output-limit helper so Responses requests can enforce provider/model output limits without adding a second limit engine. Keep Chat/Anthropic key behavior unchanged.

Do not attempt to tokenize Responses Items exactly; the current decoded-JSON structural estimate and body-based reservation estimate are sufficient for this local/SBC scope.

## Acceptance criteria

- valid stateless Responses request reaches the normal durable selection path;
- forbidden stateful/background fields return 400 before selection/upstream I/O;
- provider-suffixed model IDs normalize correctly;
- no transcode preflight or BodyTranscoder is invoked;
- no Chat Completions body rewrite is applied;
- existing auth/body/context limits still apply.

---

# Workstream C — Responses-aware provider selection and upstream URL

## Selection

A Responses request must retain EggPool's normal account routing while excluding providers that lack `responses_path`.

Add the smallest surface predicate to the existing eligibility path. Preferred implementation options, in order:

1. pass the request surface into the existing router/coordinator eligibility check and exclude accounts whose provider lacks the required endpoint path;
2. if the router already accepts a provider predicate/set, compute the eligible provider IDs once from generation-owned config and pass that set.

Do not create a parallel Responses router.

For collapsed models served by multiple providers, the Responses request may route only among response-capable providers while Chat Completions continues to use the normal provider set.

## URL selection

Extend the existing upstream URL resolver so:

```text
openai + chat_completions -> provider.openai_path
openai + responses        -> provider.responses_path
anthropic                 -> provider.anthropic_path
```

Compose all of them through `compose_provider_url()`.

If a selected provider somehow lacks the required `responses_path`, fail locally before upstream I/O; do not guess `/responses`.

## Provider transforms

Responses passthrough must **not** run Chat-specific payload mutation.

In particular, the existing OpenAI streaming transform that injects:

```json
{"stream_options": {"include_usage": true}}
```

is a Chat Completions transform and must be skipped for the Responses surface.

Thinking/media/cache translation remains inactive because no cross-protocol transcode occurs. Responses fields should pass through unchanged unless EggPool must normalize the model suffix or reject stateful features.

## Acceptance criteria

- response-capable providers participate in normal routing/scoring;
- non-capable providers are excluded without health penalty;
- Chat and Anthropic routing behavior is unchanged;
- Responses URL is selected from `responses_path` rather than hardcoded globally;
- no Chat `stream_options` mutation appears in a Responses request;
- no new router class/subsystem is added.

---

# Workstream D — Non-streaming Responses passthrough

For non-streaming requests:

1. send the selected provider the final stateless Responses payload;
2. preserve the upstream status/body and safe response headers through the existing response handoff path;
3. classify transport/provider failures through the existing retry/failure-effects machinery;
4. retry only under the same pre-handoff conditions already permitted by EggPool;
5. never retry after downstream response handoff;
6. preserve provider request IDs where the current header extraction supports them.

## Usage/cost accounting

Current Responses JSON carries an OpenAI-style `usage` object with `input_tokens`, `output_tokens`, and `total_tokens` plus details.

First test the existing normalized usage parser against the current Responses object. If it already handles the shape, reuse it unchanged.

If it does not, extend the existing OpenAI usage normalizer with the **smallest shape recognition required**. Do not create a separate Responses accounting subsystem.

Provider-reported cost precedence and existing cost-finalization semantics remain unchanged.

## Acceptance criteria

- a successful provider Responses object is returned without Chat/Anthropic translation;
- usage accounting is preserved when the provider reports it;
- ordinary upstream 4xx/5xx/transport handling reuses existing failure classification;
- no response object is persisted for future stateful reuse.

---

# Workstream E — Streaming Responses passthrough and completion evidence

## Problem

Responses streaming is SSE but its terminal evidence is not Chat Completions `[DONE]`.

Do not route Responses streams through a Chat completion translator or blindly treat clean EOF as success.

## Minimal completion observer

Extend the existing stream-completion/observer machinery with a request-surface policy for Responses.

Recognize current terminal events at minimum:

- `response.completed` — successful terminal evidence;
- `response.failed` — terminal provider failure;
- `response.incomplete` / equivalent current terminal incomplete event if present in the official API surface at implementation time;
- explicit SSE `error` event — terminal error.

The exact event set must be verified against current official OpenAI Responses streaming docs immediately before implementation.

Do not transform event payloads. Observe enough to classify completion and usage, then relay bytes/events unchanged.

If the stream reaches EOF without recognized terminal evidence, use EggPool's existing premature/malformed EOF safety semantics; do not record it as successful merely because the TCP body ended cleanly.

## Retry/handoff

- before `http.response.start`, existing transport retry semantics may apply;
- after downstream response start, never retry;
- a provider `response.failed` after handoff finalizes as upstream/midstream failure according to existing lifecycle conventions;
- client cancellation uses the existing streaming cancellation path.

## Usage

If the terminal `response.completed` event contains a complete Responses object with usage, feed that usage into the existing normalized accounting path. Avoid retaining every SSE event.

## Acceptance criteria

- streaming bytes/events are passed through unchanged;
- `response.completed` is required for successful completion under the strict policy;
- `response.failed` is never recorded as success;
- EOF without terminal evidence is not recorded as success;
- downstream handoff still prohibits retry;
- no streaming transcoder or new buffering architecture is added.

---

# Workstream F — Repair the Codex integration renderer

## Current defect

`src/eggpool/integrations/codex.py` currently emits an obsolete provider shape similar to:

```toml
[provider.eggpool]
base_url = "..."
api_key = "..."
default_model = "..."
```

and provider-local model subtables.

Current Codex uses `model_providers` and a Responses wire API. The old snippet must not remain advertised as valid.

## Required renderer shape

Generate current Codex configuration semantics along these lines:

```toml
model_provider = "eggpool"
model = "<selected-model>"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://<eggpool-host>:11300/v1"
wire_api = "responses"
env_key = "EGGPOOL_API_KEY"
```

Exact field ordering is not important; current Codex schema compatibility is.

### Authentication

Prefer Codex's supported `env_key` mechanism rather than writing the EggPool server API key directly into `config.toml`.

- use a stable variable such as `EGGPOOL_API_KEY`;
- if EggPool authentication is enabled, `configsetup codex` must give a concise instruction showing that this environment variable must contain the EggPool server key;
- do not silently edit shell startup files;
- do not print secrets in logs;
- if the current integration framework cannot convey the environment instruction without broad work, emitting a valid Codex provider with `env_key` and a clear CLI note is sufficient.

Do not use the obsolete `api_key` field.

### Model configuration

- set current Codex top-level `model` / `model_provider` as required by the current schema;
- remove obsolete `[provider.eggpool.models.*]` subtables unless current Codex documentation explicitly supports them;
- do not invent per-model Codex schema fields to preserve old context-window output;
- EggPool remains the authority for model context enforcement.

### Version handling

`detect_codex_version()` may remain informational. Do not create a compatibility matrix across old Codex releases.

Support current Codex. If retaining legacy renderer compatibility materially complicates the code, remove the obsolete format rather than branch by version.

## Acceptance criteria

- generated TOML uses `[model_providers.eggpool]`;
- generated TOML explicitly uses or defaults correctly to `wire_api = "responses"`; explicit output is preferred for clarity;
- generated configuration selects `model_provider = "eggpool"` and the chosen model in current Codex syntax;
- no invalid `[provider.eggpool]` table remains;
- no unsupported `api_key`/`default_model`/provider-local model tables remain;
- server key is not unnecessarily embedded directly in Codex TOML;
- a current Codex configuration parser/schema accepts the generated snippet.

---

# Workstream G — Focused verification only

Do not create another broad closure suite. Extend existing modules or add one small Responses-focused module only if that is clearer than scattering tests.

## Required tests

### G1. Provider config/path

- `responses_path` absent -> provider not Responses-eligible;
- valid path composes correctly from `/v1` base;
- duplicate `/v1/v1/...` style config is rejected;
- existing chat/messages paths are unchanged.

### G2. Stateless admission

Parameterize local rejection for:

- `previous_response_id`;
- `conversation`;
- `store = true`;
- `background = true`.

Prove 400 and no selection/upstream I/O.

Also prove `store = false` is accepted.

### G3. Provider eligibility

For one collapsed model served by providers A and B:

- A has `responses_path`;
- B does not;
- Responses selects only A;
- Chat Completions remains free to use A or B under normal routing.

### G4. Non-stream passthrough

Drive a stateless Responses request through the request boundary with a fake upstream and prove:

- request goes to provider `responses_path`;
- model suffix is normalized;
- representative Responses fields are preserved unchanged;
- no transcoder is selected;
- no Chat `stream_options` field is injected;
- upstream response body is returned unchanged.

### G5. Streaming completion

Use a tiny synthetic SSE sequence:

```text
response.created
response.output_text.delta
response.completed
```

Prove success finalization.

Use a second sequence ending without `response.completed` and prove it is not recorded as success.

Add `response.failed` only if it can be covered cheaply in the same parameterized test.

### G6. Codex renderer

Snapshot/parse the generated TOML and prove:

- `model_providers.eggpool` exists;
- `wire_api = "responses"`;
- `model_provider = "eggpool"`;
- chosen `model` is present;
- obsolete `provider.eggpool`, `api_key`, and provider-local model tables are absent.

If a local current Codex binary is available in the implementation environment, an optional manual `codex` config parse/smoke is useful. It must not become CI or a required live dependency.

## Test proportionality

- no live provider network calls in CI;
- no OpenAI SDK test dependency;
- no Codex binary CI dependency;
- no provider matrix;
- no new GitHub Actions job;
- prefer 5–8 focused tests over a broad replay suite.

---

# Workstream H — Documentation and integration truthfulness

After implementation:

1. README may list `/v1/responses` only as a **stateless same-protocol passthrough**, not full Responses parity.
2. Document unsupported stateful features explicitly:
   - `previous_response_id`;
   - conversation state;
   - `store = true`;
   - background jobs;
   - retrieve/cancel/delete endpoints;
   - Responses WebSocket;
   - Responses ↔ Anthropic/Chat translation.
3. Update the Codex integration docs to show the current generated configuration and required `EGGPOOL_API_KEY` environment variable when authentication is enabled.
4. Correct Plan 139's closure status from pure deferral to note that Plan 143 implemented the narrow stateless path because current Codex made the compatibility value concrete.
5. Do not rewrite historical plans beyond the short superseding closure note needed to prevent contradictory architecture guidance.

---

# Ordered implementation sequence

Implement only after Plan 142 is green.

1. add optional provider `responses_path` and config validation;
2. mark only verified bundled provider templates;
3. add endpoint-surface field/plumbing without changing `ProtocolName`;
4. register `POST /v1/responses` and implement stateless field rejection;
5. filter selection to providers with `responses_path`;
6. choose Responses upstream URL and skip Chat-specific transforms;
7. implement non-stream passthrough and usage reuse;
8. add minimal Responses SSE completion evidence;
9. update Codex renderer to current `model_providers` + Responses format;
10. add focused tests and correct docs/Plan 139 closure note;
11. run existing lightweight gates.

Do not implement cross-protocol translation as a follow-up inside this plan.

---

# Verification commands

Normal retained gates:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Focused tests should use existing module names; a representative invocation is:

```bash
uv run pytest \
  tests/unit/test_contract_urls.py \
  tests/unit/test_integrations.py \
  tests/unit/test_responses_passthrough.py \
  -q --tb=short
```

If `test_responses_passthrough.py` is not necessary, keep the tests in existing request/API modules instead. Do not add a mandatory full-suite command.

Optional manual current-Codex check when a binary is already installed:

```bash
codex --version
# generate config with eggpool configsetup codex
# validate/start Codex against a local EggPool instance using a provider
# explicitly configured with responses_path
```

This manual check is evidence only, never a CI requirement.

---

# Final acceptance criteria

Plan 143 is complete only when all of the following are true:

- [ ] EggPool exposes `POST /v1/responses`;
- [ ] the endpoint is stateless same-protocol passthrough only;
- [ ] `ProtocolName` remains exactly the OpenAI/Anthropic translation families;
- [ ] no Responses transcoder exists;
- [ ] providers must explicitly declare `responses_path` to be eligible;
- [ ] stateful/background Responses fields are rejected locally and explicitly;
- [ ] provider-suffixed model normalization works;
- [ ] request/reservation/routing/finalization reuse existing EggPool lifecycle machinery;
- [ ] Chat-specific `stream_options` mutation is not applied to Responses;
- [ ] non-stream Responses body/status pass through correctly;
- [ ] streaming success requires Responses terminal evidence rather than Chat `[DONE]`;
- [ ] usage accounting reuses/extents the existing normalizer minimally;
- [ ] current Codex renderer emits `model_providers.eggpool` and Responses wire configuration;
- [ ] obsolete Codex provider syntax is removed;
- [ ] no persistent response-ID/conversation state exists;
- [ ] no provider affinity is added;
- [ ] no Responses retrieval/cancel/delete/WebSocket/background surface is added;
- [ ] no Responses ↔ Anthropic/Chat translation is added;
- [ ] no SDK or new mandatory runtime dependency is added;
- [ ] CI remains the existing single lightweight job;
- [ ] focused tests cover stateless rejection, provider eligibility, non-stream passthrough, stream completion evidence, and Codex config generation;
- [ ] README/integration docs describe the support narrowly and truthfully.

## Line-of-work completion standard

When Plan 142 and Plan 143 both satisfy their acceptance criteria, the local-provider/multimodal/Codex compatibility work spanning Plans 131–143 is complete.

Do **not** create another automatic closure plan after these land. Only reopen this area for a concrete reproduced bug, a current provider/client compatibility break, or measured SBC/runtime evidence that justifies additional work.
