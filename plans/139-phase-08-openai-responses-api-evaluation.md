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
