# Retrieval Interoperability Milestone R001 — Closure Status

Status: blocked

Source implementation plan:

- `plans/implementation/retrieval-interoperability/001-external-rag-contract-and-reference.md`

Source subsystem roadmap:

- `plans/subsystems/retrieval-interoperability-roadmap.md#7-milestones`

Repository baseline reviewed: `088a1379`

Implementation commits or pull requests:

- `088a1379` — E001 embedding route and initial solo PrivateGPT interoperability guidance.

## 1. Executive finding

The plan is not complete. The solo deployment pattern is documented and a
PrivateGPT 1.0.1 instance was started against EggPool with an authorized local
Ollama chat and embedding model. Direct model discovery and direct
EggPool→Ollama embeddings succeeded. PrivateGPT's actual document ingestion
failed when its embedding request received EggPool's `Request could not be
admitted` response, so no ingest→retrieve→citation cycle or source-grounding
claim is made. Pattern B is also unimplemented: EggPool's supported MCP gateway
is deliberately fixed to the vetted Eggsearch catalog and supplies no
document-search/fetch adapter or per-user document delegation contract.

The failed smoke leaves two material acceptance requirements unmet: a real
retrieval application must ingest and return a resolvable citation through the
documented inference setup, and team retrieval requires backend-enforced
per-user ACLs with disjoint-principal negative tests. This closure record
captures the work and leaves R001 blocked rather than claiming completion.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Pin and document a real PrivateGPT interface/version | PrivateGPT 1.0.1 package and its official installation, embeddings, ingestion, messages, and release docs; `docs/retrieval-interoperability.md` | pass | Python 3.11, OpenAI-compatible chat/embedding base URLs, and API paths were confirmed. |
| EggPool chat and embedding endpoints work directly | EggPool model discovery and direct chat; direct authenticated `POST /v1/embeddings` returned two ordered 768-dimensional Ollama vectors | pass | This proves endpoint interoperability only, not PrivateGPT RAG completion. |
| PrivateGPT document ingest→retrieve→citation resolves | `POST /v1/artifacts/ingest` with a small synthetic text fixture | fail | PrivateGPT returned HTTP 500 after EggPool returned HTTP 400 `Request could not be admitted` during its embedding call. `/v1/messages` retrieval and citation resolution were not reached. |
| Team retrieval search/fetch enforces caller ACLs and delegation | No selected adapter or backend delegation interface exists; EggPool gateway catalog is fixed to Eggsearch 0.4.3 tools | not implemented | No arbitrary MCP registration or shared administrator retrieval credential was added. |
| Two-principal and source-revision security suite | No backend fixture/adapter exists | not run | Foreign source IDs, omitted delegation, spoofed principal, revoked access, stale revision, and malicious retrieved text are unqualified. |
| EggPool has no document/vector persistence | E001 changes only finite inference; no retrieval storage or migration was added | pass | The EggPool SQLite schema remains untouched by R001. |
| Docs clearly distinguish solo and managed-team support | `docs/retrieval-interoperability.md` | pass | The solo pattern is explicitly not represented as a team isolation design. |

## 3. Production implementation evidence

R001 added documentation and exercised the E001 inference surface. It added no
retrieval adapter, MCP tool, document persistence, vector index, or new EggPool
runtime route. This matches the ownership boundary but does not deliver either
complete acceptance path.

PrivateGPT's local-only Qdrant configuration was changed to in-memory mode to
avoid its local file lock, and `qdrant-client` was pinned to the version range
compatible with the tested PrivateGPT package implementation. Ingestion then
reached the EggPool embedding path but failed at admission. Repeated direct
embedding calls against the same authorized local setup succeeded; the
specific PrivateGPT request/admission discrepancy remains undiagnosed.

## 4. Verification executed

### Commands and operations

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test embeddings_api -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test mcp_gateway -- --test-threads=1
```

External qualification used PrivateGPT 1.0.1, Python 3.11, Qdrant in-memory
mode, EggPool on loopback, and Ollama 0.17.7 with `qwen3.5:4b` and
`nomic-embed-text:latest`. A small synthetic document was submitted to
PrivateGPT's documented `/v1/artifacts/ingest` endpoint. The ingestion failed
as described above. Temporary services were stopped after qualification; no
credentials or document contents are recorded here.

### Results

- Rust default suite: 94 targets, 1,241 passed, 4 ignored, 0 failed.
- Rust no-default suite: 94 targets, 1,242 passed, 4 ignored, 0 failed.
- Focused embedding API suite: 2 passed, 1 ignored, 0 failed.
- Focused MCP gateway suite: 6 passed, 2 ignored, 0 failed.
- No-default check and strict Clippy passed. These are local results, not CI
  claims.
- The PrivateGPT ingestion operation failed; no retrieval or citation test was
  run after it.

## 5. Invariant review

- EggPool stores no document text, vectors, chunks, or retrieval index.
- The solo PrivateGPT setup is documented as single-owner only and is not
  presented as team authorization.
- No shared document-admin key or client-supplied principal header is used.
- Retrieved text remains untrusted; EggPool makes no citation-validity claim.
- The EggPool gateway remains scoped to its validated Eggsearch tools.

## 6. Failure and recovery review

The observed ingest failure was returned as a bounded request/admission error;
no document was accepted and no RAG result was produced. The cause is not yet
known. No data migration or persistent EggPool retrieval state exists. A
future qualification must distinguish unavailable index, no matching source,
stale source, and authorization denial rather than treating them as empty
successes.

## 7. Migration and compatibility review

No EggPool migration, route, dependency, or default behavior was added for
retrieval. E001 is a separate opt-in inference capability. Existing
PrivateGPT-owned settings and content remain outside EggPool. The temporary
test install and its Qdrant data were outside the repository.

## 8. Security review

No team retrieval capability was exposed. The missing delegation/ACL adapter
is a hard security boundary, not a documentation-only gap. Before any team
claim, the selected backend must authenticate signed or scope-limited caller
delegation and enforce ACLs independently on both search and fetch; tests must
prove foreign IDs, spoofed identity, missing/revoked delegation, and stale
revisions fail closed. No query, document body, URL, citation, or credential
was added to EggPool persistence or telemetry.

## 9. Documentation and operations

`docs/retrieval-interoperability.md` gives the solo topology, operator-owned
token handling, pinned PrivateGPT target, and explicit team limitation. It
states that EggPool does not provide ingestion, vector storage, private
document search, or source verification. The reference is not described as a
qualified full RAG deployment while its ingest smoke fails.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| high | PrivateGPT full ingestion/retrieval has not completed; the original EggPool admission incompatibility is corrected by E002, while the latest isolated ingest hit a Qdrant vector-width mismatch. | The solo end-to-end RAG capability and citation path are not qualified. | Re-run with a clean PrivateGPT profile whose collection width matches the selected 768-dimensional embedding model; complete ingest→retrieve→citation and unrelated-query checks. |
| high | No vetted retrieval adapter with per-user document delegation/ACL enforcement exists. | Managed-team retrieval would risk cross-user document disclosure if implemented with a shared identity. | Select or build a separately owned adapter/backend with caller delegation and two-principal negative tests; retain default-deny until then. |
| medium | No source revision, stale citation, truncation, index outage, revocation, malicious-document, or cancellation qualification was run. | Source integrity and failure semantics are unproven. | Complete those cases against the selected backend as part of a successor implementation plan. |

## 11. Roadmap disposition

Blocked. The base64 embedding admission incompatibility is corrected and
covered by E002. Full pattern-A ingestion/retrieval/citation remains
unqualified due to the temporary PrivateGPT Qdrant width mismatch, while
pattern B lacks its required secure backend contract. Do not add arbitrary
retrieval credentials or claim team support. Resume with a clean compatible
PrivateGPT profile and a separately reviewed document delegation/ACL design.

## 12. Registry updates

The registry and Retrieval Interoperability roadmap now point to this blocked
record. ADR-0007 and ADR-0008 are accepted. E001 provides an optional embedding
interface, and Tool Integration M003 remains conditionally closed; neither
removes R001's backend ACL or PrivateGPT ingest blockers.

## 13. Post-closure evidence update — 2026-11-06

E002 captured the PrivateGPT 1.0.1 request shape without retaining its text or
credential: `POST /v1/embeddings`, JSON keys `encoding_format`, `input`, and
`model`, four input strings, and `encoding_format: "base64"`. The sanitized
replay fixture is
`rust/tests/fixtures/privategpt_1_0_1_embeddings_request.sanitized.json`.
EggPool now accepts that shape, returns little-endian base64 vectors, and the
capture proxy observed HTTP 200 from EggPool on the synthetic ingest attempt.

After that response, PrivateGPT attempted to upsert 768-wide vectors into a
1024-wide local Qdrant collection. A fresh isolated profile auto-detected the
correct 768 width but its startup probe selected the chat model for an
embedding request, which EggPool correctly rejected as embeddings-unsupported.
These are temporary PrivateGPT profile/setup issues, not evidence of an
EggPool admission failure. No successful ingest, retrieval, or citation is
claimed. Team retrieval still has no backend-enforced document delegation
interface. E002 adds the regression fixture and compatibility correction;
R001 remains blocked pending a successful full solo RAG cycle and the secure
team adapter qualification.
