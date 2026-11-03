# Embedding Routing Roadmap

Status: proposed

Long-term references:

- plans/000-long-term-specification.md §§1–5
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md §§1, 4–5
- plans/003-planning-process.md

Related ADRs:

- plans/adrs/ADR-0008-embedding-inference-surface-boundary.md (accepted)

## 1. Purpose and ownership boundary

Expose opt-in, bounded embedding inference through EggPool while retaining provider routing, identity, quota, selection and finalization. The new operation uses dedicated ingress decoding/response validation, shared principal and coordinator admission, verified provider wire contracts, and the existing single SQLite gate. It does not own embedding model inference, tokenization engines, document ingest, vector persistence, reranking or retrieval.

## 2. Work classification

**Invariants:** default Chat/Responses/Messages untouched; no model-policy bypass or hard-budget overclaim; explicit eligibility; no request text/vector persistence; one runtime generation and cancellation authority.

**Capabilities:** a verified OpenAI-compatible POST /v1/embeddings endpoint for configured and policy-visible embedding models.

**Infrastructure:** typed embedding input/output and bounded provider capability metadata; operation-specific accounting and error mapping.

**Polish:** examples for Ollama/OpenAI-compatible local providers, footprint and model discovery diagnostics.

## 3. Non-goals

No vector DB, embedding cache, ingestion, hosted files, audio/image embeddings, reranking, automatic model downloads, local embedding execution, or broad unqualified provider parity.

## 4. Current state

rust/src/server/mod.rs exposes only Chat/Responses/Messages inference and compact. docs/api-reference.md has no embeddings path. rust/src/wire/ir.rs re-exports dialogue IR; rust/src/catalog/ and rust/src/providers/ own model/endpoint data. Managed-model filtering and budgets already exist; they must be reused rather than recreated.

## 5. Target architecture

A typed bounded embedding operation enters through the ordinary authenticated ingress, routes only to explicitly qualified embedding-capable model/provider surfaces, forwards to a verified OpenAI-compatible embedding endpoint, validates an indexed data array plus usage, and settles accounting through existing request lifecycle. There is no secondary stateless-orchestrator API; it is another inference operation. Provider capability discovery and model policy remain in the existing authority, without a global guess based on chat support.

## 6. Dependency graph

- E001: hard accepted ADR-0008; interface stable current managed identity/budget and coordinator contracts; operational one reachable local embedding backend for live qualification.
- Tool Integration T001–T004: no hard dependency either way.
- Retrieval Interoperability R001: soft/interface embedding support; external retrieval service may provide embeddings elsewhere.
- CodeGG or PrivateGPT adoption is downstream qualification, not an upstream product contract.

## 7. Milestones

### E001 — Embeddings route, qualification and accounting
Class: capability. Bounded end-to-end route, verified eligible models, per-user grants/quotas and usable API error/usage contract. Exit: local live embeddings and negative authorization/unsupported-model/size/unknown-price tests; default/no-default parity and normal protocol regression tests. Deferred: advanced embedding formats and vector search.

## 8. Cross-cutting requirements

Storage: no vector/text storage; additive accounting only if needed; migration/restore qualified. Protocol: OpenAI finite embeddings subset with documented dimensions/encoding policy. Security: early bounded admission and same user authorization as other inference. Concurrency: provider health and finalization intact; no new SQLite connection. Observability: numbers/error classes only, no input/output content. Performance: no large tokenizer dependency or default local model service.

## 9. Verification strategy

Focused request decoding/response conformance, a deterministic fake upstream, a real compatible embedding backend, mixed chat+embedding contention, alias/user policy, unknown-cost hard-limit, size and malformed response, cancellation, restore, serial default/no-default workspace suites, and footprint measurements.

## 10. Risks and decision points

Existing cost/admission assumptions may be output-token centered; do not silently exempt embeddings from hard budgets. Upstream OpenAI-compatible implementations vary in dimensions and usage semantics: start with a narrow verified matrix. ADR-0008 must be accepted before the public route ships.

## 11. Completion definition

E001 closure record proves that embeddings use normal identity, policy, quota, failure and finalization controls, with actual live compatibility and no native chat regression.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| E001 embeddings | blocked | plans/implementation/embedding-routing/001-openai-compatible-embeddings.md | pending | ADR-0008 acceptance |
