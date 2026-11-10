# Retrieval Interoperability Roadmap

Status: closed

Long-term references:

- plans/000-long-term-specification.md §§1–5
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md §§4–5
- plans/003-planning-process.md

Related ADRs:

- plans/adrs/ADR-0009-downstream-application-boundary.md (accepted; ends this workstream)
- plans/adrs/ADR-0008-embedding-inference-surface-boundary.md (accepted; embeddings stay)
- plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (superseded)

## 1. Purpose and ownership boundary

ADR-0009 ends this workstream. EggPool routes inference and embeddings.
The calling application owns documents, indexes, citations, and ACLs.
R002 is closed and was documentation only. The paragraphs
below through section 10 are the historical R001 contract except where
this section and sections 7, 11, and 12 say otherwise. Do not implement
them. The live operator contract is `docs/retrieval-interoperability.md`.

## 2. Work classification

**Invariants:** default single-user install unchanged; no vectors/documents in EggPool's metrics SQLite; upstream text stays untrusted and is not elevated to instructions; user identity and source ACLs enforce independently in the retrieval backend.

**Capabilities:** an explicit opt-in local retrieval MCP service contract and a verified reference integration with an existing retrieval application/adapter.

**Infrastructure:** typed citation/provenance envelope, ownership/ACL mapping, bounded tool response projection and integration tests.

**Polish:** deployment guide for PrivateGPT interoperability, local model/embedding endpoint qualification, negative security cases.

## 3. Non-goals

No new EggPool document DB, vector index or background ingestion process, no file upload endpoint, no built-in indexing worker, no enterprise content connectors, no SQL/code execution, no implicit search of private documents.

## 4. Current state

EggPool offers inference APIs, an opt-in embeddings route, and a fixed Eggsearch MCP gateway, but no indexed private-document retrieval. Eggsearch's vetted catalog is not a persistent private-document RAG collection. PrivateGPT 1.0.1 was configured to use EggPool; direct model discovery and embedding calls succeeded, but document ingestion failed when PrivateGPT's embedding request received `Request could not be admitted`. No full RAG or citation claim is made. No assertion is made that PrivateGPT exposes an MCP retrieval tool directly.

## 5. Target architecture

Two supported deployment patterns: (A) external retrieval app calls EggPool inference/embeddings endpoints itself while retaining its own users/docs/indexes; (B) an operator-supplied retrieval MCP adapter is registered behind the scoped EggPool tool gateway and returns bounded source metadata + citation anchors; grants and backend ACLs are intersected. External service identity and caller authorization must be explicit; do not forward a single shared retrieval credential as a substitute for per-user document ACLs.

## 6. Dependency graph

- R002: **hard** accepted ADR-0009. No Rust dependency. Application-boundary M001 is **soft**; R002 deletes gateway retrieval instructions even if M001 has not merged.
- R001 is superseded for execution. Its historical dependency on tool-integration M003 is vacated with that gateway.

## 7. Milestones

### R001 — External RAG contract and reference qualification
Class: capability. Historical milestone, superseded for execution by R002.
Its blocked closure stays as evidence. Do not implement it.

### R002 — Withdraw external retrieval scope
Class: polish. Record ADR-0009 in the operator doc: embeddings remain a
routed inference API, and document ingest, indexes, citations, and ACLs
stay with the calling application. Exit: `docs/retrieval-interoperability.md`
matches that contract, R001 is not a handoff, and `rust/` is unchanged.

## 8. Cross-cutting requirements

Storage: retrieval service owns all content and index, EggPool stores no source text. Protocol: explicit tool names/arguments and stable citation metadata, not pretending OpenAI hosted file-search parity. Security: backend-enforced per-user ACL, strict delegation of principal (signed/scope-limited backend identity or distinct per-user delegated credential), default-deny if delegation not qualified; no shared administrative retrieval key. Concurrency: bounded calls and cancellation via tool gateway; backend failures do not poison models. Observability: IDs/error class/counts only, no document content or paths.

## 9. Verification strategy

Contract tests for search and fetch; authorized/unauthorized source fixtures; cross-user and revoked-credential cases; malicious retrieved instructions; attribution, cancellation, overlength result truncation; a runnable reference PrivateGPT/inference/embedding combination. No changes to EggPool's default runtime or on-disk schema. Qualify with default/no-default and physical/SBC absence mode.

## 10. Risks and decision points

A retrieval service unable to enforce per-user document ACLs must be explicitly classified as single-owner only and never advertised to team users. Do not claim PrivateGPT itself provides an MCP interface unless proved by its version under test. The citations contract must carry source/locator/revision robust enough to detect stale or fabricated references.

## 11. Completion definition

The workstream completes when R002's closure record is accepted. R001's
blocked closure remains historical. No application-level RAG capability is
claimed from embeddings or from Eggsearch.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| R001 external retrieval | superseded | plans/implementation/retrieval-interoperability/001-external-rag-contract-and-reference.md | plans/closure/retrieval-interoperability/001-status.md | Historical blocked record. Execution superseded by R002. |
| R002 withdraw external retrieval scope | implemented | plans/implementation/retrieval-interoperability/002-withdraw-external-retrieval-scope.md | plans/closure/retrieval-interoperability/002-status.md | none |
