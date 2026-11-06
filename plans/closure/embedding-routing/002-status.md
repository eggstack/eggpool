# Embedding Routing Milestone E002 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/embedding-routing/002-privategpt-base64-compatibility.md`

Source subsystem roadmap:

- `plans/subsystems/embedding-routing-roadmap.md#7-milestones`

Repository baseline reviewed: `87e5d755` plus the implementation commit recorded below.

Implementation commits or pull requests:

- Pending commit — PrivateGPT base64 embedding compatibility and sanitized
  capture regression.

## 1. Executive finding

The compatibility correction is implemented. PrivateGPT 1.0.1 sends a base64
embedding request by default; EggPool now returns validated finite float32
vectors in the expected little-endian base64 format. This closes the specific
admission incompatibility. It does not close R001's full PrivateGPT
ingest/retrieve/citation or team-document isolation requirements.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Establish actual client wire shape without retaining prompt text | `rust/tests/fixtures/privategpt_1_0_1_embeddings_request.sanitized.json` | pass | Records method, path, field names, model, input count/byte lengths, and base64 format. Text and credential are redacted. |
| Accept captured base64 request and return base64 vectors | `rust/tests/embeddings_api.rs::privategpt_base64_embeddings_capture_is_sanitized_and_supported` | pass | Decodes returned values to the expected float32 test vector; provider receives float format. |
| Reject unsupported formats before upstream submission | `rust/tests/embeddings_api.rs::malformed_or_unqualified_embedding_requests_fail_before_provider_submission` | pass | The negative case uses `binary`; provider request list remains empty. |
| Correctly distinguish input encoding failure from generic admission | `rust/src/coordinator/endpoints.rs::InvalidEmbeddingRequest` | pass | Stable HTTP 400 response code `invalid_embedding_request`. |
| Local PrivateGPT ingest | Captured synthetic ingest attempt | partial | After the EggPool binary update, the captured four-input `/v1/embeddings` request returned HTTP 200. PrivateGPT then failed in its temporary Qdrant fixture because 768-wide vectors were sent to a 1024-wide collection; a separate fresh profile also selected the chat model for an embedding startup probe. No successful retrieve/citation result is claimed. |
| Schema/migration impact | Migration v65 unchanged; no new migration | pass | No persistence change. |

## 3. Production implementation evidence

`rust/src/request/embeddings.rs` accepts `float` and `base64`, keeps the
upstream request in float form, validates provider rows, and encodes each
finite float32 vector as little-endian bytes. `rust/src/coordinator/endpoints.rs`
maps other formats to a stable 400. API documentation names the supported
formats. The sanitized capture fixture contains no client content or secret.

## 4. Verification executed

### Commands run

```bash
cargo test --manifest-path rust/Cargo.toml --test embeddings_api -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test mcp_gateway live_ollama_hosted_model_tool_model_with_mcp_fixture -- --ignored --test-threads=1
```

### Results

- Focused embeddings suite: 3 passed, 1 ignored (the separate live embedding
  test), 0 failed.
- Live Ollama model→tool→model operation: passed in 40.95 seconds with two
  model turns and one local MCP fixture invocation. The model used the current
  256-token per-turn output limit and 120-second operation deadline; no
  truncation or timeout occurred. No limit change is indicated by this run.
- Full default/no-default and locked dependency checks are recorded after the
  merge candidate is committed; this closure does not claim them prematurely.

## 5. Invariant review

- Existing provider qualification, user/model policy, admission, budget,
  health, cancellation, and finalization remain the E001 authorities.
- Base64 mode changes only the client response representation.
- Request text and response vectors are not logged or persisted.
- No inference/output limit change was needed for the live hosted operation.

## 6. Failure and recovery review

Malformed and unsupported encodings fail before provider submission. Invalid
or non-finite provider vectors continue to fail as provider-response errors.
The new encoding path adds no retries, background tasks, or durable state.

## 7. Migration and compatibility review

No schema migration, backup/restore change, or config migration. The float
response remains the default; base64 is additive. Migration v65 is unchanged.

## 8. Security review

The sanitized request fixture contains only structural metadata and synthetic
replay values. No auth value, input text, provider key, or vector output from
the live capture was retained. Existing exact-model eligibility and request
bounds remain enforced. A focused review of migration v65, effective tool
grants, MCP session binding, and per-turn nested budget settlement found no
cross-user authorization or settlement bypass; findings and code references
are summarized in the PR review.

## 9. Documentation and operations

`docs/api-reference.md` documents both formats and little-endian float32
encoding. The R001 record remains blocked pending a clean PrivateGPT
ingest→retrieve→citation path and an explicitly selected backend-enforced
document ACL/delegation design. Physical SBC footprint evidence remains a
release gate only for device-specific footprint claims.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | PrivateGPT local collection used a 1024 dimension while EggPool returned the model's discovered 768 dimensions | R001 ingest/retrieval not yet qualified; embedding admission is fixed | Re-run with a clean PrivateGPT profile whose collection dimension matches the selected embedding model; verify retrieval and citation resolution. |
| medium | EggPool has no backend-enforced private-document ACL/delegation adapter | Team RAG remains unsupported | Select and qualify an adapter with two-principal negative tests under R001. |
| low | Physical SBC absent-provider footprint evidence is unavailable | No lightweight deployment footprint claim | Perform as a release qualification gate only for that claim. |

## 11. Roadmap disposition

E002's bounded compatibility objective and regression evidence are complete.
The full Rust/dependency verification remains the named closure condition.
R001 remains blocked on end-to-end retrieval/citation evidence
and secure document delegation. The SBC measurement does not block unrelated
correctness changes.

## 12. Registry updates

Register E002 as closed in `plans/registry.md` and
`plans/subsystems/embedding-routing-roadmap.md`; preserve E001's prior closure
record and status.
