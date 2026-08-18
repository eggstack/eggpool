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
   None. All bundled integration targets (OpenCode, Aider, Cline, Continue, Codex,
   Qwen Code, Kilo, Roo Code, Goose, OpenHands) use Chat Completions. No repository
   example, test, integration generator, or project guidance requires
   `/v1/responses`, `responses.create`, `previous_response_id`, or Responses
   conversation state.

2. **Which bundled/local providers expose a sufficiently compatible Responses endpoint?**
   None. All 22 bundled templates declare `openai` or `anthropic` protocol. The
   `SUPPORTED_PROTOCOLS` frozenset in `catalog/protocols.py` has exactly two values.
   No provider template claims Responses support. While Ollama, LM Studio, llama.cpp,
   and vLLM may expose Responses-compatible surfaces in newer versions, their EggPool
   templates use Chat Completions, and no operator evidence demonstrates Responses
   routing through EggPool.

3. **Can same-protocol Responses requests be proxied/routed with minimal transformation?**
   Not safely. Responses requests carry `previous_response_id` and optional
   conversation state that EggPool's stateless routing cannot preserve. Failover to
   a different upstream would invalidate the response ID. There is no provider
   affinity or session pinning in the current architecture, and adding one solely for
   Responses would introduce a persistent state machine disproportionate to the
   value.

4. **Which Responses features are necessary for target clients?**
   Unknown and currently irrelevant — no target client requires Responses. If a
   future client demonstrates need, the minimum viable subset would be text
   generation, tool calls, and streaming events. File/image input, reasoning
   summaries, and previous-response IDs would require additional discovery.

5. **Which stateful semantics conflict with EggPool's current stateless routing or failover model?**
   `previous_response_id` ties a request to a specific upstream provider's response.
   EggPool's failover model retries across distinct accounts on transport failure
   before response handoff. After handoff, no retry is possible. Responses
   conversation state would require either (a) provider affinity pinning, which
   conflicts with quota-based routing, or (b) a persistent store of response
   mappings, which conflicts with the stateless proxy design.

6. **Would cross-protocol Responses ↔ Anthropic translation require lossy semantics beyond the existing content IR?**
   Yes. Responses Items, output objects, and streaming event types have no
   Anthropic Messages equivalent. The existing content IR handles Chat Completions
   ↔ Messages translation; Responses would require a third independent pairwise
   transcoder or a general three-protocol canonical request object, both explicitly
   rejected by Plan 139's scope constraints.

### Rejection criteria check

- [x] Current target clients remain fully functional with Chat Completions/Anthropic Messages.
- [x] Provider support is too inconsistent to route safely (no bundled provider declares Responses).
- [x] Required state semantics would introduce a persistent state machine solely for one endpoint.
- [x] The feature would materially expand CI/provider matrices without proportional user value.

All four rejection criteria are met. Deferral is the correct outcome.

### Relationship to Plan 130

Plan 130 already established `openai_scope: chat_completions` and updated all
public documentation to explicitly exclude `/v1/responses` parity. Plan 139
confirms this decision through provider/client evaluation rather than product-scope
reasoning alone. No additional documentation changes are required — README,
AGENTS.md, architecture docs, and skills already reflect the narrowed scope.

### Verification

No code changes were made. Existing docs already reflect the correct scope from
Plan 130. The full CI gate passes unchanged:

```text
uv run ruff format --check src/ tests/ scripts/     -> passed
uv run ruff check src/ tests/ scripts/              -> passed
uv run pyright src/ scripts/                        -> passed
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
  -> passed
```
