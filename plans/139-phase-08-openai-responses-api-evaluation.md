# Plan 139 — Phase 8: OpenAI Responses API Evaluation

## Objective

After local-provider and multimodal content work stabilizes, determine whether EggPool should expose/proxy OpenAI's Responses API for practical client compatibility. This plan is a decision/evaluation gate, not a presumption that Responses must be implemented.

## Why last

Ollama, LM Studio, llama.cpp, and vLLM increasingly expose Responses-compatible surfaces. Coding-agent clients may therefore benefit from an EggPool Responses endpoint. Implementing it before the narrow content IR and local provider work would risk creating a third independent pairwise transcoder and substantial scope creep.

## Evaluation questions

1. Which target clients actually require Responses rather than Chat Completions?
2. Which bundled/local providers expose a sufficiently compatible Responses endpoint?
3. Can same-protocol Responses requests be proxied/routed with minimal transformation first?
4. Which Responses features are necessary for the target clients: text, tools, images/files, reasoning, previous-response IDs, streaming events?
5. Which stateful semantics conflict with EggPool's current stateless request routing or failover model?
6. Would cross-protocol Responses ↔ Anthropic translation require lossy semantics beyond the existing content IR?

## Preferred implementation shape if approved

Stage A: native Responses passthrough/routing only for providers explicitly declaring Responses support. Reuse existing provider selection, auth, client pool, failure isolation, and response handoff.

Stage B: only after real client need is demonstrated, add narrowly scoped compatibility translation that reuses the content IR. Do not build a general three-protocol canonical request object.

Stateful provider response identifiers must not be silently routed to a different upstream/provider if that would make them invalid. If safe failover cannot be proven, fail closed or pin appropriately rather than guessing.

## Explicit rejection criteria

Do not implement Responses if:

- current target clients remain fully functional with Chat Completions/Anthropic Messages;
- provider support is too inconsistent to route safely;
- implementing required state semantics would introduce a substantial persistent state machine solely for one endpoint;
- the feature would materially expand CI/provider matrices without proportional user value.

## Deliverable

Produce a short architecture decision in the implementing commit/architecture docs stating one of:

- defer Responses;
- native passthrough only;
- native passthrough plus a narrowly enumerated translation subset.

If implementation is approved and materially larger than a focused change, create a new roadmap only then. Do not extend Plan 139 into an unbounded implementation document.

## Acceptance criteria

- The decision is based on current client/provider behavior rather than API fashion.
- Any approved endpoint reuses existing routing/client/failure machinery.
- No independent pairwise multimodal model is introduced.
- Stateful response/provider affinity semantics are explicitly addressed.
- Deferral is considered a successful outcome if value does not justify complexity.
- No new mandatory CI job is created by the evaluation itself.

## Closure record

Status: complete.

Decision:

```text
responses_api: defer
```

### Evaluation answers

1. **Which target clients actually require Responses rather than Chat Completions?**
   Plan 141 re-evaluation (correction): Responses is a real, currently
   used protocol surface. OpenAI's Codex, OpenAI's Responses API, the
   OpenAI Agents SDK, and the OpenAI Python client (`openai>=1.40`) all
   advertise `/v1/responses` and `client.responses.create(...)` as
   first-class features. The earlier plan-139 answer overstated the
   gap; the correct observation is that **EggPool's bundled
   integration targets** (OpenCode, Aider, Cline, Continue, Codex,
   Qwen Code, Kilo, Roo Code, Goose, OpenHands) speak Chat Completions
   today, so no operator deployment currently depends on Responses.

2. **Which bundled/local providers expose a sufficiently compatible Responses endpoint?**
   Plan 141 re-evaluation (correction): the upstream ecosystem does
   provide stateless Responses surfaces. Ollama 0.13.3+ ships a
   stateless `/v1/responses` endpoint (no `previous_response_id` /
   `conversation` support). vLLM documents a `/v1/responses` route on
   its OpenAI-compatible server for text-generation models. EggPool's
   bundled templates, however, still advertise `openai` or `anthropic`
   protocol only; the `SUPPORTED_PROTOCOLS` frozenset in
   `catalog/protocols.py` has exactly two values, and no provider
   template declares Responses support or carries Responses catalog
   metadata.

3. **Can same-protocol Responses requests be proxied/routed with minimal transformation?**
   Yes for the **stateless** subset: a Responses request without
   `previous_response_id`, `conversation`, or `store` is replayable
   across distinct accounts, and the existing pre-handoff retry path
   preserves request semantics. Stateless Responses passthrough reuses
   `compose_provider_url()`, the existing client pool, the catalog
   fetcher, and the finalization supervisor. No third protocol
   transcoder or Responses ↔ Anthropic translation is required.
   Stateful Responses requests (with `previous_response_id`,
   `conversation`, or `store`) cannot be safely routed because
   failover would invalidate the response ID.

4. **Which Responses features are necessary for target clients?**
   Unknown. No current bundled integration target depends on
   `/v1/responses`. If a future target does, the minimum viable
   subset would be text generation, tool calls, streaming events, and
   stateless responses. File/image input, reasoning summaries, and
   `previous_response_id` are not in EggPool's scope.

5. **Which stateful semantics conflict with EggPool's current stateless routing or failover model?**
   `previous_response_id`, `conversation`, and `store` tie a request
   to a specific upstream provider's response and require either (a)
   provider affinity pinning, which conflicts with quota-based
   routing, or (b) a persistent store of response mappings, which
   conflicts with the stateless proxy design. A stateless Responses
   passthrough can reject these locally without changing routing
   semantics.

6. **Would cross-protocol Responses ↔ Anthropic translation require lossy semantics beyond the existing content IR?**
   Yes. Responses Items, output objects, and streaming event types
   have no Anthropic Messages equivalent. The existing content IR
   handles Chat Completions ↔ Messages translation; Responses would
   require a third independent pairwise transcoder or a general
   three-protocol canonical request object, both explicitly rejected
   by Plan 139's scope constraints.

### Decision rationale

The decision is **scope/value proportionality**, not the absence of
external protocol support. Stateless Responses passthrough is
technically narrow — it can reuse the existing routing/client pool —
and the upstream ecosystem does provide compatible surfaces. What
blocks implementation today is the lack of measured project/operator
value:

- No bundled integration target depends on `/v1/responses`.
- No bundled provider template declares Responses support or carries
  Responses catalog metadata.
- Adding the endpoint still requires a new route surface, a new
  protocol token or stateless-passthrough flag in
  `catalog/protocols.py`, explicit local rejection of
  `previous_response_id` / `conversation` / `store`, and the
  per-provider capability/catalog plumbing for the Responses fields
  EggPool would forward.

Those costs exceed the deferred value today. If a future bundled
provider ships Responses support and a target client demonstrates
need, the implementation must remain narrowly scoped to stateless
same-protocol passthrough and reject stateful fields locally. No
`/v1/responses` endpoint, transcoder, state store, or CI matrix is
added by Plan 141.

### Rejection criteria check

- [x] No current bundled integration target depends on Responses (the
      deferral is by value proportionality, not by absence of
      protocol support).
- [x] No bundled provider template declares Responses support or
      carries Responses catalog metadata.
- [x] Stateful semantics would require a persistent state machine
      or provider affinity solely for one endpoint.
- [x] The feature would materially expand CI/provider matrices
      without proportional user value.

### Relationship to Plan 130

Plan 130 already established `openai_scope: chat_completions` and
updated all public documentation to explicitly exclude
`/v1/responses` parity. Plan 139 confirms the decision through
provider/client evaluation, not through claims that the protocol
does not exist externally. README, AGENTS.md, architecture docs, and
skills already reflect the narrowed scope and require no further
public-surface changes.

### Verification

No code changes were made. Existing docs already reflect the correct
scope from Plan 130. The full CI gate passes unchanged:

```text
uv run ruff format --check src/ tests/ scripts/     -> passed
uv run ruff check src/ tests/ scripts/              -> passed
uv run pyright src/ scripts/                        -> passed
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
  -> passed
```

---

## Plan 140 re-evaluation: stateless same-protocol Responses passthrough

Plan 140 re-opened only the narrowest question Plan 139 deferred:
would a **stateless, same-protocol OpenAI-compatible
`/v1/responses` passthrough** justify a small endpoint surface? The
full Responses semantic parity, stateful provider affinity, and
Responses ↔ Anthropic translation remain explicitly out of scope.

Plan 141 refines that evaluation: the upstream ecosystem does expose
stateless Responses surfaces (Ollama 0.13.3+, vLLM), but no bundled
provider template in EggPool declares Responses support and no
bundled integration target depends on it. The deferral therefore
stands on value proportionality, not on an inaccurate claim that the
protocol is unsupported externally.

### Evidence

1. **Target clients.** No bundled integration target (OpenCode,
   Aider, Cline, Continue, Codex, Qwen Code, Kilo, Roo Code, Goose,
   OpenHands) requires `/v1/responses`. The earlier plan-139 wording
   overstate the gap; the accurate statement is that EggPool's
   bundled targets speak Chat Completions today.

2. **Bundled local runtime support.** Ollama 0.13.3+ ships a
   stateless `/v1/responses` endpoint (no `previous_response_id` /
   `conversation`). vLLM documents `/v1/responses` for
   text-generation models. EggPool's bundled templates still
   advertise `openai` or `anthropic` protocol only; no template
   declares Responses support or carries Responses catalog metadata.

3. **Stateless replayability.** A Responses request without
   `previous_response_id`, `conversation`, or `store` is replayable
   across distinct accounts; the existing pre-handoff retry path
   preserves request semantics. This is the only condition under
   which the existing routing/client/failure-isolation machinery
   can carry a Responses payload unmodified.

4. **Stateful fields.** `previous_response_id`, `conversation`, and
   `store` must be rejected locally or explicitly documented
   unsupported. Without provider affinity or a persistent response-ID
   store, any cross-account retry on these payloads would be
   incorrect.

5. **Routing reuse.** A same-protocol Responses passthrough reuses
   `compose_provider_url()`, the existing client pool, the catalog
   fetcher, and the finalization supervisor. No third protocol
   transcoder is required. No Responses ↔ Anthropic translation is
   introduced.

### Decision

```text
responses_stateless_passthrough: defer
```

The decision is **scope/value proportionality**, not the absence of
external protocol support. Stateless Responses passthrough is
technically narrow and the upstream ecosystem does provide
compatible surfaces, but:

- No bundled integration target depends on it.
- No bundled provider template declares Responses support.
- Adding the endpoint still requires a new route surface, a new
  protocol token (or a dedicated stateless-passthrough flag), explicit
  local rejection of `previous_response_id` / `conversation` /
  `store`, and the per-provider capability/catalog plumbing that
  today's bundled providers do not ship.

Those costs exceed the deferred value today. Plan 139's deferral is
**confirmed** for stateless same-protocol passthrough. Plan 141
records the corrected rationale but introduces no code change for
`/v1/responses`.

If a future bundled provider declares Responses support and a target
client demonstrates need, the implementation must remain narrowly
scoped to the stateless same-protocol path and reject stateful fields
locally. Cross-protocol Responses ↔ Anthropic translation, persistent
response-ID stores, conversation routing state, new SDK
dependencies, and expanded CI matrices remain out of scope unless a
separate plan is written.

### Plan 143 superseding closure

Plan 143 was written when current Codex shipped only `WireApi::Responses`
and rejected `wire_api = "chat"`. Codex's custom provider configuration
moved to `[model_providers.<id>]` and EggPool's bundled Codex
integration was no longer advertising a configuration current Codex
would accept. Plan 143 therefore implements the narrow stateless
same-protocol `/v1/responses` passthrough that Plan 139 deferred:

* `ProviderConfig.responses_path` declares eligibility per provider;
  bundled templates add `responses_path = "/responses"` only for
  providers whose documentation genuinely exposes that route
  (openai, ollama-local, llamacpp-local, vllm-local).
* `POST /v1/responses` is registered as a stateless same-protocol
  endpoint with `protocol = "openai"` and `request_surface = "responses"`.
* Stateful Responses fields (`previous_response_id`, `conversation`,
  `store=true`, `background=true`) are rejected locally with HTTP 400
  before any provider selection or upstream I/O. They are *never*
  silently stripped.
* `compose_provider_url()` remains the only URL joiner; the Responses
  path is composed via the same helper that builds chat and messages
  URLs.
* Chat Completions-specific transforms (`stream_options.include_usage`
  injection) are skipped for the Responses surface; the streaming
  observer recognises `response.completed` and `response.failed` as
  terminal events instead of Chat's `[DONE]` marker.
* `eggpool configsetup codex` now emits a current Codex
  `[model_providers.eggpool]` block with `wire_api = "responses"` and
  `env_key = "EGGPOOL_API_KEY"`; the legacy `[provider.eggpool]` /
  embedded `api_key` / provider-local model subtables are removed.

Plan 143 deliberately does *not* implement Responses ↔ Anthropic
translation, response-ID persistence, conversation affinity, retrieval,
cancellation, background jobs, or WebSocket transport. The line-of-work
completion standard in Plan 143 treats Plans 131–143 as the closure of
the local-provider / multimodal / Codex compatibility work; no further
automatic closure plan is anticipated for this area.
