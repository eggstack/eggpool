# ADR-0008: Bounded embedding inference surface and capability discovery

Status: proposed

Date: 2026-11-03

Decision owners: EggPool maintainers

Related specification sections:

- plans/000-long-term-specification.md ``1–5
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md ``1, 4–5
- plans/003-planning-process.md `2.2

Affected subsystem roadmaps:

- plans/subsystems/embedding-routing-roadmap.md
- plans/subsystems/retrieval-interoperability-roadmap.md

## Context

EggPool exposes Chat Completions, Responses and Messages, but not the OpenAI-compatible POST /v1/embeddings path. PrivateGPT-style retrieval applications require embeddings but should not require EggPool to own ingestion or a vector index. Embedding requests have different input/output, estimation, pricing, model eligibility, and error contracts from generative text. Making embedding requests look like chat or forwarding unvalidated JSON to arbitrary accounts would bypass existing safeguards.

## Decision drivers

- Keep embedding inference in EggPool's model-routing remit, not introduce an application/vector-DB layer.
- Model restrictions, user budgets, provider health, provider account quotas, response boundaries, and cancellation must apply as they do for ordinary inference.
- Avoid adding large tokenizers/model runtimes and avoid claiming broad embeddings-provider compatibility without evidence.
- Preserve native chat/Responses/Messages byte and SSE behavior and the single SQLite gate.

## Considered options

1. Generic untyped URL proxy for any /v1 path: rejected; bypasses validated capability and quota semantics.
2. Recast embedding text as Chat Completions or canonical conversation IR: rejected; wrong protocol and metering.
3. Explicit Embedding operation with typed admission/normalization and verified provider surfaces: chosen.

## Decision

- Introduce only the bounded finite OpenAI-compatible POST /v1/embeddings API as a separately typed embedding operation, sharing authentication, user/model policy, provider selection, quota and health, cancellation, generation leases, and finalization mechanisms. Keep embedding input/output IR separate from dialogue semantics; reuse primitives, not artificial chat messages.
- Initially qualify explicit OpenAI-compatible embedding provider endpoints only. No automatic declaration that every OpenAI-compatible *chat* provider supports embeddings; no multimodal embeddings unless reviewed separately. Unqualified models reject before upstream dispatch.
- Validate input text or text arrays, item limits, cumulative input bytes/tokens where provable, optional dimensions, encoding_format, model identity, and finite output response size. Unknown/surplus API fields are handled by a documented preservation or rejection policy, never silently mapped to inappropriate upstream semantics.
- Authenticate the same principal and apply the same concrete model-policy/virtual-alias restrictions. Use an explicit embedding-capability model catalog and deterministic error when incompatible. Embedding pricing and usage are operation-specific; report unknown measurements honestly. Hard budget reservations must prove a worst-case bound or deny, never estimate and then label it hard.
- Do not persist vectors, texts, prompts, or embedding response bodies. Do not create embeddings caching, document ingestion, similarity indexes, vector search, or RAG state in EggPool.
- No default runtime dependence on a local embedding server. All existing installations and client endpoints retain their behavior until an eligible provider/model is configured.

## Consequences

### Positive

Allows RAG services and retrieval clients to share EggPool's routing and governance without embedding a vector database in the router.

### Negative

Requires an explicit provider capability and contract qualification matrix, input estimation policy, per-operation usage accounting, and regression coverage for policy/quota enforcement.

### Deferred

Non-OpenAI embedding APIs, batch jobs, reranking, image/audio embeddings, persistent vectors, retrieval/index management, and hosted file APIs.

## Compatibility and migration

No mandatory new runtime process or schema migration. Any schema changes for operation-specific stats must be additive, keep legacy request counts meaningful, and preserve old backup/restore. Model discovery must not mislabel unqualified chat models as embedding models. Existing user policies remain restrictive across the new operation; no new open access because a route was added. Config/reload behavior uses the existing transition classifier.

## Security and reliability implications

Cap request arrays, nesting and strings before forwarding; prevent memory amplification from large numeric float vectors; do not permit expensive unsupported output encodings. Preserve cancellation and exactly-once finalization on failed provider responses and disconnects. Treat local endpoints with the same configured network/proxy policy used for other providers. Reject unknown models even when the provider's path exists.

## Verification

Require an actual local compatible embedding backend plus a scripted/mock provider contract; policy-denial, hard budget unknown-price, unsupported dimensions/encoding, array/size limits, alias bypass, use with no embedding provider, cancellation, mixed chat/embedding concurrency, and restoration tests. Test default and no-default builds, serial suite, and SBC footprint baseline. ADR acceptance is a hard gate before introducing the new public endpoint.

## Supersession

None.
