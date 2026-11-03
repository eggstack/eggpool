# Retrieval Interoperability Roadmap

Status: proposed

Long-term references:

- plans/000-long-term-specification.md §§1–5
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md §§4–5
- plans/003-planning-process.md

Related ADRs:

- plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (proposed)
- plans/adrs/ADR-0008-embedding-inference-surface-boundary.md (proposed; optional interface)

## 1. Purpose and ownership boundary

Close the PrivateGPT-style retrieval interoperability gap without turning EggPool into a document store. EggPool owns only a governed tool/credential interface and model/embeddings routing; an operator-run retrieval companion owns ingestion, parsing, chunking, embeddings/index selection, ACL-enforced retrieval, deletions, and source content storage.

## 2. Work classification

**Invariants:** default single-user install unchanged; no vectors/documents in EggPool's metrics SQLite; upstream text stays untrusted and is not elevated to instructions; user identity and source ACLs enforce independently in the retrieval backend.

**Capabilities:** an explicit opt-in local retrieval MCP service contract and a verified reference integration with an existing retrieval application/adapter.

**Infrastructure:** typed citation/provenance envelope, ownership/ACL mapping, bounded tool response projection and integration tests.

**Polish:** deployment guide for PrivateGPT interoperability, local model/embedding endpoint qualification, negative security cases.

## 3. Non-goals

No new EggPool document DB, vector index or background ingestion process, no file upload endpoint, no built-in indexing worker, no enterprise content connectors, no SQL/code execution, no implicit search of private documents.

## 4. Current state

EggPool offers inference APIs and managed identities/budgets, not an embeddings route or indexed retrieval. Eggsearch offers web/repo/research tools and deterministic evidence bundles, but not a persistent private-document RAG collection. PrivateGPT can already consume an external OpenAI-compatible inference API; using EggPool as its LLM backend should be qualified independently of any EggPool-hosted MCP adapter. No assertion is made that PrivateGPT exposes an MCP retrieval tool directly.

## 5. Target architecture

Two supported deployment patterns: (A) external retrieval app calls EggPool inference/embeddings endpoints itself while retaining its own users/docs/indexes; (B) an operator-supplied retrieval MCP adapter is registered behind the scoped EggPool tool gateway and returns bounded source metadata + citation anchors; grants and backend ACLs are intersected. External service identity and caller authorization must be explicit; do not forward a single shared retrieval credential as a substitute for per-user document ACLs.

## 6. Dependency graph

- R001: hard Tool Integration M003 closure for pattern B; interface Embedding Routing E001 if using EggPool embeddings; pattern A may be qualified separately using external embeddings without waiting for E001. No hard dependency on hosted orchestration M004.
- Operational: fixture retrieval service with at least two disjoint principals/collections; working PrivateGPT instance or equivalent for pattern A.
- No migration/storage dependency on EggPool beyond tool policy already delivered by T002.

## 7. Milestones

### R001 — External RAG contract and reference qualification
Class: capability. Define one small MCP retrieval contract (search/fetch, citation IDs, bounded text), an operator configuration example, two-user ACL negative suite and a reference PrivateGPT deployment showing inferencing through EggPool. Exit: retrieved citations remain source-grounded and unauthorized documents are not discoverable or retrievable; no EggPool content storage. Deferred: local ingest implementation, richer indexing and generic connectors.

## 8. Cross-cutting requirements

Storage: retrieval service owns all content and index, EggPool stores no source text. Protocol: explicit tool names/arguments and stable citation metadata, not pretending OpenAI hosted file-search parity. Security: backend-enforced per-user ACL, strict delegation of principal (signed/scope-limited backend identity or distinct per-user delegated credential), default-deny if delegation not qualified; no shared administrative retrieval key. Concurrency: bounded calls and cancellation via tool gateway; backend failures do not poison models. Observability: IDs/error class/counts only, no document content or paths.

## 9. Verification strategy

Contract tests for search and fetch; authorized/unauthorized source fixtures; cross-user and revoked-credential cases; malicious retrieved instructions; attribution, cancellation, overlength result truncation; a runnable reference PrivateGPT/inference/embedding combination. No changes to EggPool's default runtime or on-disk schema. Qualify with default/no-default and physical/SBC absence mode.

## 10. Risks and decision points

A retrieval service unable to enforce per-user document ACLs must be explicitly classified as single-owner only and never advertised to team users. Do not claim PrivateGPT itself provides an MCP interface unless proved by its version under test. The citations contract must carry source/locator/revision robust enough to detect stale or fabricated references.

## 11. Completion definition

R001 closure includes a repeatable reference deployment, two-user security regression and documented capability limits. No application-level RAG capability is claimed simply from installing Eggsearch or adding embeddings.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| R001 external retrieval | blocked | plans/implementation/retrieval-interoperability/001-external-rag-contract-and-reference.md | pending | Tool Integration M003; retrieval ACL interface |
