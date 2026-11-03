# Retrieval Interoperability R001 — External RAG contract and reference deployment

Status: blocked

Repository baseline: `0e021c1ffe55803aff5f325033e65313dca9f1dc` (planning, 2026-11-03)

Source roadmap: plans/subsystems/retrieval-interoperability-roadmap.md#7-milestones

Long-term requirements: plans/000-long-term-specification.md ``1–6; plans/001-terminology-and-domain-model.md; plans/002-long-term-roadmap.md ``4–5; plans/003-planning-process.md

Applicable ADRs: plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (proposed); plans/adrs/ADR-0008-embedding-inference-surface-boundary.md (proposed; interface only for EggPool embedding route)

Primary class: capability

## 1. Objective

Qualify two realistic interoperability paths without a built-in RAG system: (A) a PrivateGPT-like document application using EggPool for OpenAI-compatible chat inference (and EggPool embeddings if E001 closes, else an independently configured embedding backend); (B) an optional retrieval-specific MCP adapter serving authenticated search and fetch via the EggPool tools gateway. Create a small source-grounded citation and access-control contract for path B, plus a reproducible deployment/compatibility fixture.

## 2. Readiness and dependencies

**Hard:** Tool Integration M003 gateway closure for any public MCP retrieval capability and ADR-0007 acceptance. **Interface:** Embedding Routing E001 if the reference chooses EggPool as its embeddings endpoint; not a hard dependency for external embeddings. **Operational:** an actual local document retrieval service with a governed two-user test fixture. Path A exploration/documentation can precede closure but does not mark R001 complete. Do not claim the upstream application exposes an MCP server unless verified in that exact version.

## 3. Current implementation evidence

EggPool README documents Chat/Responses/Messages and managed-user inference; docs/api-reference.md has no ingestion, files, vector collections or embeddings path at this baseline. Eggsearch README and docs/tool-matrix.md describe web/repository evidence tools; these are not a persistent private-document collection. Current rust/src/db/ owns request/accounting data and stays a single gate. PrivateGPT's external OpenAI-compatible inference can be configured to use a router, but embedding endpoint and model compatibility need independent validation.

## 4. Invariants

EggPool stores neither user documents nor embeddings/index chunks. Source citations must resolve to actual backend-owned versioned locations, not hallucinated URLs. A retrieved document remains untrusted for instruction priority. A user's EggPool model/tool privilege does not imply document ACL access; retrieval service enforces document access independently. No admin/shared retrieval credential may unintentionally expose another user's documents. Solo/offline EggPool continues to operate without a retrieval service.

## 5. Scope

**In:** a minimum MCP tool contract with namespaced `documents.search` and `documents.fetch` (or clearly versioned equivalents), bounded inputs/results, opaque source ID, collection/owner scope, immutable revision/hash or ETag where available, fetched/source flags, citation span/locator and explicit truncation/failure; per-user backend delegation or unequivocally single-owner mode; adapter/config examples; PrivateGPT reference deployment through EggPool inference and compatible embedding provider; negative security/compatibility suite.

**Out:** native uploads, PDF/OCR ingestion, chunker/embedding worker, SQLite vector extension, general-purpose vector DB, new local model runtime, arbitrary MCP backends, SQL agents, autonomous file crawlers, merged tenant stores, enterprise connectors, chat memory and streaming answer synthesis.

## 6. Required production changes

Prefer an external reference adapter and documentation rather than new EggPool Rust runtime code. If the M003 tool gateway lacks a necessary safe per-user delegation mechanism, create a bounded follow-up tool-integration milestone; do not embed a retrieval-specific credential or ACL bypass in generic gateway code. Operator TOML registers only a vetted local retrieval service; its backend identity cannot be supplied by the client. The tool invocation supplies a trusted downstream principal context via authenticated delegation (mutually authenticated service identity plus scope-restricted per-user token or equivalent), not an unsignable `X-User` header. The retrieval service itself denies cross-user source IDs and enforces its own collection ACL on search **and** fetch. If no secure delegation mechanism exists, support *only* a documented solo-owner integration without team exposure and mark the team portion blocked.

The contract should treat citation metadata as first-class structured data, not freeform prose: source ID, title, backend-specific document ID, revision, chunk/span offsets, immutable locator, confidence/coverage if measured, evidence role, trust marker, and fetch/truncation status. A cite resolves with the same principal and revision; stale or inaccessible source IDs cannot be fetched by another user. Enforce small source/result caps, stable IDs and explicit no-match versus failed-index distinction. User-visible answer synthesis may be performed by the consuming application, not automatically by EggPool. Never log the document content, source URL or query in EggPool.

For pattern A, create a reproducible local configuration and scripted smoke: PrivateGPT version/commit, EggPool base URL and user token, qualified inference model, a separate embeddings endpoint or E001 qualified model, ingest sample public fixture documents, ask a grounded question, and verify access/citations. Document that PrivateGPT handles its own authentication/storage independently; EggPool credentials only protect its inference surface. Keep environment variables placeholders and secrets outside checked-in files.

## 7. Ordered work packages

A. **Capability survey and interface choice.** Confirm PrivateGPT's supported API, external inference/embedding configuration and RAG model requirements against pinned upstream version. Choose a real compatible retrieval MCP adapter or build one in a *separate* repository/service if none exists; record why.

B. **Reference pattern A.** Write reproducible local deployment and negative model/embedding compatibility smoke; no EggPool runtime changes. A passing chat does not prove retrieval.

C. **MCP retrieval contract.** Define versioned tools, typed source/citation envelope and exact caller identity/delegation/ACL enforcement; two test principal fixtures with disjoint collection access and malicious document instructions.

D. **Gateway pattern B.** Register reference adapter through M003 without changing general MCP contracts. Verify list/call grants and backend per-document ACL are intersected; failed delegation denies rather than falling back to owner account.

E. **Qualification and documentation.** Exercise stale citations, truncated fetch, index unavailable, deleted document, revoked credential and cross-user probe; record sanitized results and expected no-backend behavior.

## 8. Failure/cancellation/restart/contention

If retrieval index is unready/temporarily unavailable, report unavailable rather than an empty verified result set. No content access after source revoke even if opaque ID is guessed. All fetch calls are bounded and cancellation propagates to the backend; no automatic mutation/reindex. If backend disconnects or caller revokes midway, subsequent search/fetch fails closed. Local backend restart may invalidate opaque references; report stale revision, do not silently return a different document under same ID.

## 9. Compatibility and migration

No EggPool on-disk data migration, default HTTP route or default dependency. Existing PrivateGPT installations can continue pointing at their present inference/embedding servers. Adding an EggPool base URL must not alter their document authorization model. Embedded personal inference token must never be emitted in example logs or committed to a sample .env. Remote retrieval services remain out of first version.

## 10. Required tests

Add a test harness for the reference service/adapter (location depends on chosen implementation repo) and an EggPool integration smoke if T003 introduces a public backend registration format. Two users/collections, direct fetch by foreign ID, omitted delegation, spoofed principal, stale citation, deleted file, malicious instruction in chunks, truncation/status, revocation, backend unready, malformed source IDs, cancellation. Test PrivateGPT actual ingest→retrieve→citation with pinned version; record whether citations are genuine and resolvable, not just model assertion.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Use `cargo test --manifest-path rust/Cargo.toml --test mcp_gateway -- --test-threads=1` for EggPool generic gateway regression (after M003), plus the reference service's own commands and a pinned PrivateGPT smoke. If this milestone makes no EggPool code changes, still record the EggPool no-backend regression and explicitly distinguish upstream-only tests.

## 12. Documentation

A dedicated docs/retrieval-interoperability.md or equivalent, docs/deployment.md, docs/agent-configuration.md, docs/api-reference.md (no false files/RAG route claim) and architecture/overview.md. Provide solo and managed-team topology, threat model and exact operator setup. Cross-link Eggsearch/MCP tool docs and distinguish public web search from private document search.

## 13. Acceptance criteria

An operator can run a documented PrivateGPT-style application using EggPool inference and a qualified embedding endpoint; sample documents ingest and citations resolve. With gateway pattern B, users cannot discover or fetch unauthorized documents despite a valid EggPool tool token; source integrity and failures are preserved. No EggPool file storage or indexing is introduced; absence leaves the router unaffected.

## 14. Stop conditions

Stop if the chosen retrieval service cannot enforce backend ACLs for teams, if PrivateGPT version has no verifiable compatible external embedding API, if a citation cannot resolve to a source, if delegation requires forwarding an admin key, or if changes require ingest/vector persistence in EggPool. Split into a separate companion-repo plan instead.

## 15. Closure evidence

Pinned upstream versions and exact setup steps, two-principal ACL negative tests, citation source/revision resolution, stale/truncation/error cases, absent-service regression, all executed test results, docs and known limitations. Do not mark closed based solely on an inference ping or an Eggsearch web_search success.

## 16. Handoff notes

R001 is deliberately an interoperability/reference milestone, not a promise to turn EggPool into PrivateGPT. It can have minimal EggPool runtime diff if backend integration and docs fulfill the contract. Promote it only when M003 closes and the backend delegation interface is demonstrably secure.
