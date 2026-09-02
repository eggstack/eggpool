# Plan 147 — Dynamic Wire-Surface Negotiation Roadmap

Date: 2026-09-02
Status: ready for implementation
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f` (v0.7.3)
Priority: P0 API correctness / routing resilience
Execution target: GPT-5.6 Luna or comparable implementation model
Related plans: 105, 121, 123, 130, 139, 143–145

## Purpose

Replace EggPool's current static provider/protocol/endpoint assumptions with a small, generic runtime layer that can select, validate, learn, and relearn the **wire surface** used for a provider/model pair.

The immediate trigger is the current OpenCode Go regression, but the architecture must not special-case OpenCode Go. Current upstream ecosystems increasingly expose the same provider — and sometimes the same model — through multiple incompatible request/response surfaces. The selected surface can also change over time without the model ID changing.

The required invariant is:

> Provider identity, model identity, client API, upstream wire surface, authentication shape, and reasoning capability are separate facts. EggPool may use static/configured facts as hints, but successful runtime dispatch is the strongest non-fixed evidence of the currently usable wire profile.

A model that worked through `/responses` yesterday must be allowed to move to `/chat/completions`, `/messages`, or another configured compatible surface tomorrow without requiring an EggPool release, database wipe, or process restart.

At the same time, negotiation must be bounded. EggPool must not turn one request into an uncontrolled Cartesian product of accounts × surfaces, and concurrent requests must not stampede an upstream while relearning a changed surface.

---

# Why the current abstraction is insufficient

At the reviewed baseline:

- `ProviderConfig` has broad `protocols`, `openai_path`, `anthropic_path`, and the later `responses_path` field;
- `request/upstream_helpers.py` chooses the URL from broad protocol plus the incoming request surface;
- Responses was intentionally implemented by Plans 143–145 as a narrow, same-protocol OpenAI endpoint surface;
- `transcoder/streaming.py` translates OpenAI Chat Completions ↔ Anthropic Messages and does not model Responses streaming as another upstream wire grammar;
- provider authentication is provider-wide, with secondary headers rendered on every dispatch, rather than selected per wire surface;
- the failure classifier still treats an otherwise-unclassified HTTP 401 as durable authentication failure and retries another account;
- reasoning/thinking contracts can influence broad protocol selection even though reasoning semantics and transport surface are independent.

Those choices were locally reasonable for earlier scope, but mixed-surface providers make them unsafe as the long-term routing model. Plan 143's rule that Responses remains an OpenAI-family endpoint rather than a third `ProtocolName` remains useful; what must be superseded is the assumption that the client endpoint and upstream endpoint surface must match and that provider/model surface eligibility is static.

---

# Current upstream facts verified for this roadmap

Verification date: 2026-09-02.

Implementers must re-check official documentation at execution time because this roadmap is specifically intended to tolerate upstream contract changes.

## OpenCode Go

Current OpenCode Go documentation assigns different models to different wire endpoints under the same provider:

- OpenAI Responses (`/v1/responses`) for models including GPT-5.6 Luna and Muse Spark 1.2 Contributor;
- OpenAI Chat Completions (`/v1/chat/completions`) for models including current GLM, Kimi, DeepSeek and MiMo offerings;
- Anthropic Messages (`/v1/messages`) for models including current MiniMax and Qwen offerings.

The current `/v1/models` response exposes model IDs but does not provide enough endpoint/surface metadata to derive this mapping dynamically before a request.

Reference: <https://dev.opencode.ai/docs/go/>

## OpenAI

OpenAI currently maintains both Chat Completions and Responses. Responses has a distinct request grammar and typed SSE event grammar; it cannot be treated as Chat Completions merely because both are OpenAI-family APIs.

Reference: <https://platform.openai.com/docs/api-reference/responses>

## Anthropic

Direct Claude model access uses Messages (`POST /v1/messages`). Streaming uses named SSE events ending in `message_stop` or an error rather than OpenAI Chat's chunk grammar.

Reference: <https://platform.claude.com/docs/en/api/messages>

## Google Gemini

As of June 2026, Google's **Interactions API** is GA and recommended for new Gemini applications. It uses `POST /v1/interactions` (or the versioned equivalent), can stream typed SSE events, and is stateful by default unless `store=false` is sent. The older `generateContent` / `streamGenerateContent` API remains fully supported but is now described as legacy.

References:

- <https://ai.google.dev/gemini-api/docs/interactions-overview>
- <https://ai.google.dev/api/interactions-api-v1>
- <https://ai.google.dev/gemini-api/docs/streaming>
- <https://ai.google.dev/api/generate-content>

## Amazon Bedrock

Current Bedrock documentation explicitly exposes several API families on the same service: native Invoke, native Converse, OpenAI-compatible Chat Completions, OpenAI-compatible Responses, and Anthropic Messages. This validates the need to model provider and wire surface independently.

References:

- <https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html>
- <https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html>

## Cohere

Cohere has a native `/v2/chat`, but also has an official OpenAI-compatible endpoint at `https://api.cohere.ai/compatibility/v1` with Chat Completions, streaming, tools and structured output. Native Cohere therefore does not need to become a first-wave EggPool codec merely to support Cohere.

Reference: <https://docs.cohere.com/docs/compatibility-api>

## Local runtimes

Current Ollama and vLLM expose common OpenAI-compatible surfaces, including Chat Completions and Responses. vLLM also exposes other compatibility APIs. This again argues for a small common-surface registry rather than runtime-specific adapters.

References:

- <https://docs.ollama.com/api/openai-compatibility>
- <https://docs.vllm.ai/en/latest/serving/online_serving/>

---

# Primary architecture decision

Implement a **wire-profile layer** between account/model selection and outbound HTTP dispatch.

A wire profile is the concrete contract necessary to send one request:

```text
WireProfile
  surface identity
  request path/template
  streaming path/template when different
  authentication/header shape
  request codec
  response codec
  streaming grammar/terminal policy
```

The initial surface identities are:

```text
openai_chat_completions
openai_responses
anthropic_messages
gemini_interactions
gemini_generate_content
```

Do not add these values to the existing `ProtocolName = Literal["openai", "anthropic"]`. `ProtocolName` may remain as compatibility/catalog metadata while upstream dispatch moves to the richer wire-surface type.

Do not implement a runtime protocol-description language. TOML should select from built-in, typed codec IDs; Python code owns request/response semantics and safe failure classification.

---

# Default-surface scope decision

## Implement in this roadmap

1. **OpenAI Chat Completions** — still the broadest compatibility surface across hosted and local providers.
2. **OpenAI Responses** — increasingly required by current OpenAI/Codex-style clients and providers; distinct SSE and reasoning structure.
3. **Anthropic Messages** — required for Claude-native providers and many compatible providers.
4. **Gemini Interactions** — Google's current recommended/GA interface; use only the stateless model subset for failover-safe routing.
5. **Gemini generateContent** — retain because it remains widely deployed and officially supported even though Google now calls it legacy.

## Do not implement as first-wave core surfaces

- OpenAI legacy `/v1/completions`: prompt-oriented/text-generation semantics do not safely accept arbitrary chat/tool requests without model-specific prompt formatting.
- OpenAI Realtime, Gemini Live, WebSocket/bidirectional APIs: stateful full-duplex transports require session affinity and different retry guarantees.
- Bedrock Converse/Invoke: useful future provider adapters, but current Bedrock already offers Chat/Responses/Messages and native AWS semantics/authentication would materially broaden this line of work.
- Cohere native `/v2/chat`: Cohere's official OpenAI compatibility endpoint covers the primary EggPool use case.
- vendor-specific batch/async APIs: incompatible with EggPool's current synchronous proxy lifecycle.
- embeddings/rerank/audio/image-generation surfaces: separate product/API families rather than alternate chat/model wire surfaces.

The registry must be extensible so one of these can be added later without modifying the negotiator.

---

# Configuration decision

Use a separate packaged developer-facing registry:

```text
src/eggpool/providers/_wire_profiles.toml
```

This file should contain:

- built-in wire-surface definitions that reference whitelisted codec IDs;
- default ordering/metadata needed to seed selection;
- optional low-authority provider/model hints that can be updated without adding provider branches to Python.

Provider-specific candidate endpoint paths/auth remain in `providers/_templates.toml` / ordinary provider config because the same semantic surface can live at different URLs or require different headers.

The registry is **not** an operator-facing arbitrary codec DSL. `check-config` must reject unknown codec IDs, invalid path templates, duplicate surface IDs, and hints that reference surfaces the provider cannot use.

Bundled hints are only hints. They must be revocable by runtime evidence.

---

# Runtime learning decision

Keep learned wire state **process-local and in memory** in the first implementation.

Do not add a SQLite migration merely to remember a performance hint. On restart, EggPool can reseed from current config/docs and relearn on the first incompatible request. This avoids durable stale protocol truth and avoids new SBC write pressure.

Use a process-owned cache that survives generation swaps where safe. Cache keys must include enough provider configuration identity/fingerprint that a rehash changing candidate paths/auth invalidates stale learned entries.

For each `(provider_id, canonical_model_id, candidate_fingerprint)` retain only bounded structural facts:

```text
preferred surface/profile
last success time
source/confidence
per-candidate last deterministic rejection
per-candidate suppress-until
```

Never persist request bodies, response bodies, tool arguments, or credentials in the wire cache.

A successful ordinary request refreshes the current preference. TTL expiration lowers confidence but does **not** trigger a background probe or force gratuitous alternate-surface traffic.

---

# Negotiation safety rule

Alternate-surface dispatch is allowed only when EggPool has evidence that the previous attempt was rejected before model inference could have begun.

Safe examples:

- HTTP 404/405 indicating endpoint/path absence, after excluding structured model-absence evidence;
- structured unsupported-endpoint/surface response;
- structured request-schema rejection that specifically indicates a different known API dialect;
- explicit wire-auth/header mismatch where the configured credential itself has not been proven invalid.

Unsafe/ambiguous examples that must **not** trigger a new surface attempt:

- 429 or quota exhaustion;
- 5xx;
- write/read timeout after the request may have been transmitted;
- connection reset after transmission;
- malformed/truncated response after response generation begins;
- midstream failure;
- any failure after downstream response handoff.

This protects against duplicate billing, duplicate model generation and duplicate tool side effects.

---

# Retry/negotiation composition rule

Do not multiply account retry and surface retry.

Expected behavior:

```text
surface mismatch
  -> same account, next safe candidate surface

rate limit
  -> same known-good surface, existing other-account routing

confirmed invalid credential
  -> disable only that account; same surface on another account

model-scoped provider failure
  -> existing model/account/provider failure policy
```

All upstream submissions must consume one shared per-request attempt budget. Reuse the intent of the existing `routing.max_retries_before_stream` instead of adding an independent unlimited negotiation retry count.

The default current setting of three retries after the first request means four total upstream submissions. Negotiation and account retry together must remain within that bound unless a later plan explicitly changes the product setting.

---

# Concurrency/rate-limit decision

Negotiation is reactive control-plane work and must be single-flight.

Implement:

- one single-flight negotiation per `(provider_id, model_id)`;
- at most one active negotiation dispatch per provider by default;
- a small minimum interval between new negotiation attempts for the same provider;
- per-profile deterministic-rejection cooldown/negative cache;
- provider `Retry-After` / 429 immediately stops negotiation and delays further negotiation attempts for that provider.

Do **not** create background probe workers.

The leader should hold negotiation ownership only until the alternate dispatch is accepted/rejected at the HTTP boundary, not for the entire inference body. Followers can then use the newly accepted profile without waiting for the leader's model generation to complete.

---

# Reasoning/thinking separation

Wire negotiation must not infer a wire surface from reasoning capability and must not use reasoning controls as protocol labels.

Normalize client intent independently:

```text
ReasoningIntent
  requested/disabled/unspecified
  mode: effort | fixed_budget | adaptive | toggle | unspecified
  effort: optional string
  budget_tokens: optional integer
```

After provider/account/model/wire-profile selection, encode that semantic intent according to the selected model capability and wire codec.

Preserve Plan 123's core rule: unknown effort mappings must never silently become guessed token budgets.

---

# Roadmap phases

## Phase 1 — Plan 148: Wire-profile registry and provider contracts

Introduce the typed surface/profile model, packaged `_wire_profiles.toml`, provider candidate paths/auth overrides, legacy config synthesis, validation, and bundled low-authority hints.

No runtime negotiation yet.

## Phase 2 — Plan 149: Canonical request/response/event boundary

Create the minimal canonical semantic representation needed to avoid N² pairwise transcoders. Keep same-surface passthrough fast. Separate reasoning intent from transport.

## Phase 3 — Plan 150: Runtime learning, single-flight and negotiation governor

Add bounded process-local preference learning, deterministic rejection caching, config-fingerprint invalidation, provider concurrency control, and evidence-driven candidate enumeration.

## Phase 4 — Plan 151: Negotiation-safe failure effects and shared attempt budgeting

Repair 401 semantics, introduce explicit wire-negotiation failure effects/retry scopes, protect account health, and prevent account×surface retry multiplication.

This phase contains an early P0 substep: remove the current `401 -> disable_auth` fallback when no explicit invalid-credential evidence exists. Implementers may land that narrow safety correction before the rest of the roadmap if needed to stop current account poisoning.

## Phase 5 — Plan 152: Default wire codecs and stream semantics

Complete/route the five default surface codecs, including Responses and Gemini typed SSE terminal handling, per-surface auth/path behavior, OpenCode Go candidate/hint correction, and provider-template migration.

## Phase 6 — Plan 153: Live E2E, migration and closure

Use real provider credentials outside ordinary CI to prove live path selection, stale-profile relearning, failure isolation, reasoning encoding, streaming termination, and negotiation stampede protection.

---

# Dependency graph

```text
148 registry/config
      |
      v
149 canonical semantic boundary
      |
      +--------------+
      |              |
      v              v
150 negotiation    151 failure effects
      \              /
       \            /
        v          v
        152 codecs/providers
               |
               v
        153 live E2E/closure
```

Plan 151's narrow 401 poison-prevention patch may be implemented ahead of this strict order because it is independently safe and currently operationally important.

---

# Global acceptance criteria

- [ ] Provider identity and upstream wire surface are represented independently.
- [ ] OpenAI Chat, OpenAI Responses, Anthropic Messages, Gemini Interactions, and Gemini generateContent are built-in selectable surface identities.
- [ ] A provider may declare multiple candidate wire profiles for the same model/account.
- [ ] Static provider/model mappings are preferences unless explicitly configured fixed.
- [ ] A deterministically rejected learned surface can be invalidated and another candidate tried without restart or rehash.
- [ ] Ordinary successful requests refresh the learned preference without separate probe traffic.
- [ ] No background surface-probe worker exists.
- [ ] Concurrent failures for one provider/model result in bounded single-flight negotiation rather than N×M probes.
- [ ] 429/Retry-After stops negotiation and applies provider negotiation pressure without falsely changing model/surface knowledge.
- [ ] Bare/ambiguous 401 can no longer permanently disable an account.
- [ ] Confirmed invalid credentials remain account-local and can disable only the proven bad account.
- [ ] Surface/schema mismatch does not poison account health.
- [ ] Account retry and surface negotiation share one total request-attempt budget.
- [ ] No alternate-surface retry occurs after a failure that could represent already-started inference or after downstream handoff.
- [ ] Reasoning intent is normalized independently from wire surface and encoded only after target selection.
- [ ] Same-surface passthrough remains the lowest-overhead path.
- [ ] Existing Chat↔Messages behavior remains compatible where semantic translation is representable.
- [ ] Current Responses clients no longer require same-surface upstream routing when a supported cross-surface codec exists.
- [ ] No new runtime dependency is required for negotiation.
- [ ] No DB migration or new high-frequency persistence is required for learned wire state.
- [ ] No new CI matrix, provider matrix, soak job, or live-key GitHub Actions job is introduced.
- [ ] Real-key E2E is available as an explicit manual/release validation path.

---

# Resource and complexity constraints

EggPool is primarily intended for local/LAN deployment on small systems. This roadmap must not turn the proxy into a generic enterprise API gateway framework.

Explicit constraints:

- no plugin runtime for wire codecs;
- no provider SDK dependencies;
- no background protocol scanner;
- no periodic synthetic inference probes;
- no distributed negotiation state;
- no persistent event log for wire observations;
- no per-request documentation/network metadata lookup;
- no arbitrary TOML expression language;
- no dynamic Python imports from configuration;
- no broad public Gemini/Bedrock/Cohere API emulation unless separately planned;
- no support for stateful Responses/Interactions/Live sessions in failover routing;
- no explosion of test permutations across every provider/model/surface combination.

Focused deterministic tests should prove the generic state machine. A small real-provider matrix should prove that the abstractions match current upstream behavior.

---

# Required documentation disposition

When implementation lands, update architecture/config documentation so it no longer describes `protocol` as sufficient to choose an upstream endpoint. Document the distinction among:

- client surface;
- broad compatibility protocol metadata;
- upstream wire surface/profile;
- model semantic capabilities;
- account health.

Keep provider-specific current mappings in the packaged data file/config comments, not scattered through prose or Python branches.

---

# Handoff sequence

1. Read this roadmap plus Plans 148–153, current `AGENTS.md`, Plans 123 and 143–145, and the owning request/failure/transcoder tests.
2. Re-check current official OpenCode Go, OpenAI, Anthropic and Google endpoint/streaming semantics.
3. Implement the registry/config model first while retaining compatibility synthesis for existing configs.
4. Establish the canonical semantic boundary and same-surface fast path.
5. Repair failure effects before enabling automatic fallback broadly.
6. Add process-local learned selection and single-flight negotiation.
7. Wire in the five default codecs and provider templates/hints.
8. Run focused deterministic tests and the ordinary lean project gate.
9. Run explicit live E2E with supplied keys; do not put those credentials or live runs into default CI.
10. Append implementation SHA/results to the applicable final plan; do not create a new chain of closure plans unless genuinely new defects are found.
