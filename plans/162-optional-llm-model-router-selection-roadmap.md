# Plan 162 — Optional LLM Model-Router Selection Roadmap

Date: 2026-09-03
Status: ready for implementation
Planning baseline: `525189763a3a6d506e9e8001e2426c9bd9a247fe`
Priority: P1 optional routing capability / regression-sensitive request-path work
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Add an entirely optional semantic model-routing layer that lets an operator expose a configured virtual model name to clients and use a small/local LLM to choose one concrete EggPool model from arbitrary operator-described routes.

The feature is intentionally separate from EggPool's existing provider/account router. The new layer answers **which model should serve this conversation?** The existing `routing.Router` continues to answer **which provider/account should serve this concrete model request?**

A representative operator configuration should ultimately look like:

```toml
[model_routers.implementer]
selector_model = "qwen3-0.6b/local"
default_model = "muse-spark-1.3"
sticky = true
affinity_ttl_s = 43200
selector_timeout_s = 2.0
max_input_bytes = 2048
repair_attempts = 1

[model_routers.implementer.routes.Implementer-hard]
model = "muse-spark-1.3"
description = "Use for the most difficult queries."

[model_routers.implementer.routes.Implementer-code]
model = "gpt-5.6-luna"
description = "Use for implementation, debugging, and code changes."

[model_routers.implementer.routes.Implementer-fast]
model = "gemini-3.8-flash"
description = "Use for straightforward requests where latency matters."
```

A client sends `model = "implementer"`. EggPool resolves that virtual ID to one configured concrete target, then resumes the ordinary model/provider/account path.

---

## Current architecture and why this must be a separate layer

At this baseline:

- `src/eggpool/api/chat_completions.py`, `messages.py`, and `responses.py` converge on the shared `handle_proxy_request()` path;
- `src/eggpool/api/proxy_request.py` validates the client model, calls `parse_model_provider()`, normalizes the concrete model, builds canonical semantic intent, applies model-specific context/capability/transcoding checks, constructs `ProxyRequestContext`, and calls `RequestCoordinator.execute()`;
- `RequestCoordinator.execute()` owns the correctness-critical concrete request lifecycle: account selection, reservation publication, request/attempt persistence, quota accounting, health/backoff, provider-bound request construction, upstream dispatch, cancellation cleanup, finalization, and streaming handoff;
- `src/eggpool/routing/router.py` is already a large and highly constrained concrete provider/account selector;
- `architecture/deep-dive-routing.md` explicitly guarantees that cache/policy do not enter `QuotaFairScorer`;
- `RuntimeGeneration` owns generation-scoped routing/catalog/coordinator state while `ProcessRuntime` owns process-lifetime shared resources;
- staged `rehash` already has a precedent for process-owned learned state that is reused only while a configuration fingerprint remains compatible;
- `/v1/responses` is deliberately stateless, so affinity cannot depend on upstream `previous_response_id`/conversation state.

Do not extend `routing.Router` with prompt classification or cache affinity. Doing so would conflate semantic model selection with provider/account selection and put the existing scoring/failover invariants at risk.

Target request flow:

```text
client request
    |
    | model = concrete-id ------------------------------+
    |                                                   |
    | model = configured virtual-id                     |
    v                                                   |
ModelRouterService                                      |
    |                                                   |
    +-- affinity hit -> concrete target                 |
    |                                                   |
    +-- miss -> deterministic bounded selector prompt   |
              -> concrete selector request              |
              -> exact route-id parse                   |
              -> one bounded repair at most             |
              -> configured default on selector failure |
    |                                                   |
    +---------------- concrete model -------------------+
                            |
                            v
                    existing model parsing
                    context/capability checks
                    transcoding
                    RequestCoordinator.execute()
                            |
                            v
                    existing provider/account router
```

---

## Non-negotiable invariants

1. **Feature-off identity.** `model_routers` is absent/empty by default. In that state EggPool must preserve current concrete-model behavior, `/v1/models` output, request persistence, metrics, retry budgets, background-task inventory, and provider/account scoring. No selector request, affinity allocation, timer, DB write, or network operation may occur.
2. **Semantic routing is pre-routing, not account scoring.** Cache affinity and model-selection policy never enter `QuotaFairScorer`, `FairnessRotor`, account eligibility, routing priority, or provider backoff semantics.
3. **Reuse the concrete request lifecycle.** Selector LLM calls must execute through EggPool's existing internal `RequestCoordinator.execute()` path with a concrete selector model. Do not HTTP-loop back into EggPool and do not create a second provider client/routing/retry implementation.
4. **No recursive routers in v1.** A `selector_model` or route target may not refer to another configured virtual model. The implementation must reject such configuration structurally, eliminating cycles and recursion.
5. **Default means selector fallback, not semantic target failover.** Selector transport failure, timeout, malformed output, or exhausted repair falls back to the configured `default_model`. Once a concrete target has been selected and upstream submission begins, ordinary provider/account failover applies to that model only; EggPool must not spray the same client turn across different semantic target models after ambiguous upstream execution.
6. **Client cancellation remains cancellation.** Never swallow `asyncio.CancelledError` from the parent request and continue with the default target. An internal selector timeout may fall back only after the selector coordinator has completed its normal cancellation/cleanup path.
7. **Small-model prompt discipline.** Selector input is deterministic, bounded, protocol-independent, and intentionally excludes full conversation history, tool schemas, tool outputs, binary/image payloads, and other large transport material. Static route policy precedes variable request text to maximize local prefix/KV-cache reuse.
8. **Strict output language.** Compile configured routes to tiny internal IDs (`0`, `1`, ... / compact deterministic IDs). The selector returns exactly one accepted ID. Do not use fuzzy model-name parsing, prose extraction, schema-repair frameworks, or another LLM to interpret the answer.
9. **Sticky by default.** When a stable session key is available and `sticky = true`, route once and reuse the same concrete model until affinity expiry, router-configuration fingerprint change, or router removal. This is intentionally stronger than a per-turn preference because preserving a model's prompt-cache locality is a primary objective.
10. **No new persistence subsystem.** Initial affinity is a bounded in-memory process-owned TTL/LRU cache. Do not add a SQLite table, migration, sweeper, cross-process lease, external cache, or durable conversation state.
11. **No new mandatory dependency.** Use the standard library and existing EggPool abstractions only. Do not add tokenizer/model SDK/cache dependencies.
12. **No new permanent CI matrix.** Add focused unit/integration regression coverage to existing suites and run the repository's existing Ruff/Pyright/Pytest gates.
13. **Virtual routing is not a security boundary.** Strict output parsing prevents arbitrary target injection, but a user's prompt is intentionally an input to model selection. Documentation must not describe selector policy as an authorization mechanism.

---

## Configuration contract

Add a top-level `model_routers: dict[str, ModelRouterConfig]` with an empty default. TOML table names are the client-visible virtual model IDs.

Initial per-router fields:

- `selector_model: str` — concrete EggPool model reference, optionally provider-qualified using existing syntax;
- `default_model: str` — concrete route target used when the selector cannot produce a valid decision;
- `routes: dict[str, ModelRouteConfig]` — arbitrary operator labels mapped to concrete model + concise description;
- `sticky: bool = true`;
- `affinity_ttl_s: float = 43200` (bounded);
- `selector_timeout_s: float = 2.0` (bounded; exact default may be adjusted during implementation if existing local-provider tests justify it);
- `max_input_bytes: int = 2048` (bounded variable semantic text budget);
- `repair_attempts: Literal[0, 1] = 1`.

Route fields:

- `model: str`;
- `description: str`.

Validation requirements:

- virtual IDs are non-empty, bounded, have no control characters, and do not use `/` because that conflicts with the existing model/provider suffix grammar;
- at least one route exists;
- descriptions are non-empty and bounded;
- `default_model` exactly matches at least one configured route target;
- no selector or target names any configured virtual ID;
- the selector's compiled static policy is bounded by a hard implementation ceiling so an accidental configuration cannot turn a tiny router into a huge-context request;
- do not impose a small arbitrary route-count limit: accept as many routes as fit the bounded compiled policy;
- concrete targets are not required to be currently healthy/discovered at TOML parse time because provider catalogs may be transient or refresh later;
- exact configured virtual IDs take precedence over a later-discovered unsuffixed concrete ID collision. Emit a bounded warning/operational diagnostic and keep provider-qualified concrete entries reachable rather than destabilizing the running config.

`model_routers` should be generation-owned and classified `LIVE` for staged rehash once phases 163–165 provide a complete atomic candidate generation. Old affinity may be reused only when the router fingerprint matches.

---

## Selector protocol

Compile route labels in a deterministic order (lexicographic label order is sufficient) to compact internal IDs. Human labels remain available for configuration and diagnostics, but the model emits only the compact ID.

The static policy must be byte-for-byte reproducible for the same router configuration. A conceptual form is:

```text
pick id;reply id only|0=Use for the most difficult queries.|1=Use for implementation/debugging/code.|2=Use for straightforward latency-sensitive queries.
```

The variable semantic payload should be extracted only for virtual requests. Use the decoded request and existing canonical semantics where practical; do not mutate the normal concrete path merely to build selector input. Prefer bounded system/developer intent plus the latest user-visible text, with deterministic whitespace normalization and deterministic head/tail truncation. Represent modalities/tools/reasoning requirements with tiny feature flags when they materially aid selection; never copy tool schemas or binary content.

For oversized variable input, truncate by UTF-8 bytes with valid-boundary repair. Tokenizer-specific truncation is explicitly out of scope because the selector may be any local/remote concrete model and EggPool must remain dependency-light.

Use a conservative non-streaming selector request and a very small output allowance. Optional controls such as `temperature = 0` should only be sent when the selected provider/model contract says they are accepted; do not make selector reliability depend on unsupported generation knobs.

First malformed semantic output may receive one tiny repair request such as `invalid;reply only:0|1|2`. After `repair_attempts` is exhausted, use `default_model`. Selector response bodies must be bounded before parsing.

---

## Affinity contract

Support an explicit client affinity header:

```text
X-EggPool-Route-Session: <opaque session id>
```

Hash the value immediately; never persist or log the raw header. Existing provider request construction must continue to prevent this EggPool-only header from being forwarded upstream, and regression coverage must prove it.

When the header is absent, derive a stable automatic fingerprint only when the request surface contains enough repeated conversation prefix to do so cheaply (for example system/developer context + first user turn). Do not hash the entire evolving transcript because that would create a new affinity key every turn. A stateless Responses request may have no safe automatic session identity; documentation must recommend the explicit header in that case.

Affinity key inputs:

```text
virtual model id
+ router configuration fingerprint
+ hashed explicit session id OR stable canonical conversation-prefix fingerprint
```

Affinity value includes at least concrete target, human route label, decision source, and expiry. A default fallback is a legitimate route decision and may be sticky for that session; a new session will attempt the selector normally.

Use bounded LRU/TTL storage plus per-key single-flight so concurrent first requests for one session do not launch duplicate selector requests.

---

## Observability contract

Expose enough diagnostics to understand semantic selection without storing prompts:

- requested virtual model;
- resolved concrete model;
- configured route label;
- source: `selector`, `affinity`, `default`, or deterministic bypass such as `single_candidate` if implemented;
- selector attempt count and latency;
- counters for affinity hits/misses, selector success, malformed output, repair success/failure, timeout/transport fallback, and default resolution.

Selector calls are real upstream/local inference consumption and must remain visible to existing concrete request/quota/cost accounting. Do not hide their resource use. Avoid a DB migration solely for semantic routing traces; bounded logs/metrics plus existing selector request records are sufficient for this local/LAN scope unless implementation evidence proves otherwise.

Never persist the minified prompt, raw session header, full user prompt, or selector raw output by default.

---

## Client model catalog behavior

When configured, expose each virtual model in `/v1/models` as a synthetic EggPool-owned model with namespaced metadata indicating dynamic semantic routing. Do not fabricate one concrete target's context window, price, or capabilities as if those were universal to the virtual model.

When `model_routers` is absent/empty, `/v1/models` must remain byte/structure-equivalent to the current logical output for the same catalog state.

After virtual resolution, existing context-limit, capability, reasoning-control, protocol, and transcoding checks run against the resolved concrete target and remain authoritative.

---

## Phase breakdown

### Plan 163 — Configuration, registry, runtime ownership, and virtual-model foundations

Add typed configuration, structural validation, deterministic router fingerprints/route compilation, generation wiring, live-reload classification, and feature-off characterization tests. Establish virtual-ID collision rules without yet changing request dispatch.

### Plan 164 — Selector dispatch and deterministic minified prompt

Implement bounded semantic extraction/prompt compilation, strict route-ID parsing/repair, internal concrete selector execution through the existing coordinator, timeout/cancellation behavior, and default fallback. Keep this independently testable before client requests are redirected through it.

### Plan 165 — Affinity, cache locality, concurrency, and rehash continuity

Add the bounded process-owned affinity cache, explicit session header hashing, safe automatic prefix fingerprinting, per-key single-flight, TTL/LRU behavior, and fingerprint-gated reuse across staged generation swaps.

### Plan 166 — Client integration, model catalog, documentation, and regression closure

Wire virtual resolution into the shared proxy request path before model-specific concrete checks, expose synthetic virtual models, add observability, update operator/API/architecture documentation with complete examples, and run cross-surface regression closure.

Execute 163 -> 164 -> 165 -> 166. Do not land partial client-visible virtual IDs before the selector/default/affinity semantics required by the corresponding phase are complete.

---

## Regression matrix required before closure

The implementation must explicitly prove:

1. Missing/empty `model_routers` preserves normal OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages concrete-model requests.
2. The feature-off path never invokes or allocates selector/affinity machinery and adds no DB writes or background tasks.
3. Provider-qualified concrete model parsing and `models.collapse_models` behavior are unchanged.
4. `/v1/models` is unchanged feature-off and gains only configured synthetic entries feature-on.
5. Context-limit checks and reasoning/capability validation use the resolved concrete target, including transcoded requests.
6. Existing provider/account priority, health, quota, fairness, backoff, wire negotiation, and retry semantics remain unchanged after concrete target resolution.
7. `QuotaFairScorer` has no new semantic-routing/cache inputs.
8. Selector request accounting/reservations are finalized on success, error, timeout, and cancellation with no leaked pending/reserved state.
9. Parent client cancellation propagates and does not trigger default-model work.
10. Invalid selector text cannot inject an arbitrary model; only configured IDs resolve.
11. Selector failure deterministically chooses `default_model` without surfacing selector failure to the client when default dispatch succeeds.
12. A selected target's ambiguous upstream failure never causes semantic failover to another configured model.
13. Sticky sessions reuse the same concrete model and concurrent first turns single-flight.
14. Rehash preserves affinity only for an unchanged router fingerprint and invalidates it after relevant route/selector/default changes or router removal.
15. `X-EggPool-Route-Session` is hashed locally, never logged raw, never persisted raw, and never forwarded upstream.
16. Selector prompt golden tests enforce deterministic bytes/order and configured size ceilings.
17. No new mandatory dependency, permanent CI job, DB migration, or external service is introduced.

Run focused suites during each phase, then the repository's existing final gates (`ruff format --check`, `ruff check`, `pyright`, and the normal pytest/smoke suite used by current CI). Do not invent a separate plan-numbered CI matrix.

---

## Documentation deliverables

Plan 166 must update at least:

- `config.example.toml` — commented, copyable configuration with selector/default/specialized routes;
- `docs/configuration.md` — complete field semantics and validation rules;
- new `docs/model-routing.md` — setup guide, route-description guidance, local selector guidance, sticky-session/cache behavior, failure/default behavior, troubleshooting, and complete examples;
- `docs/api-reference.md` — virtual `/v1/models` entry and `X-EggPool-Route-Session` contract;
- `docs/live-config-rehash.md` — fingerprint/affinity behavior across rehash;
- `architecture/deep-dive-routing.md` and/or request-lifecycle deep dive — explicit semantic-model-selection layer before the existing concrete provider/account router;
- `README.md` — concise optional-feature example/link;
- `CHANGELOG.md` when implementation lands.

Documentation must state clearly that the feature is disabled unless `[model_routers.*]` is configured, the selector is itself a normal concrete EggPool model whose usage is accounted, and `default_model` protects selector availability but is not a cross-model retry mechanism after target submission.

---

## Explicitly out of scope for this roadmap

- router-to-router nesting or recursive semantic policies;
- ensembles, voting, multi-selector consensus, or speculative parallel target execution;
- semantic fallback to another target after ambiguous upstream submission;
- persistent affinity across process restart;
- distributed/cross-node affinity;
- training or fine-tuning a selector;
- tokenizer dependencies or model-specific prompt compilers;
- an authorization/security policy engine;
- dashboard-heavy router editors or policy builders;
- cache-aware changes to provider/account scoring;
- production multi-tenant scheduling machinery.

These can be reconsidered only after the simple local/LAN design has real operational evidence that they are needed.

---

## Definition of done

This roadmap is complete when Plans 163–166 are implemented and their acceptance gates pass; an operator can opt into one or many virtual model routers; a small/local selector can choose among arbitrary described concrete models using a bounded deterministic prompt; malformed/unavailable selectors fall back to the configured default; stable sessions stay on one target for cache locality; rehash behaves predictably; all three client request surfaces work; documentation contains copyable examples; and a configuration with no model routers behaves like the pre-feature baseline.
