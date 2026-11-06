# Embedding Routing E002 — PrivateGPT base64 client compatibility

Status: implemented

Repository baseline: `87e5d755` on `plan/optional-tools-privategpt-parity-20261103`

Source roadmap: `plans/subsystems/embedding-routing-roadmap.md#7-milestones`

Original plan and closure:

- `plans/implementation/embedding-routing/001-openai-compatible-embeddings.md`
- `plans/closure/embedding-routing/001-status.md`

Long-term requirements: `plans/000-long-term-specification.md` §§1–5;
`plans/001-terminology-and-domain-model.md`; `plans/002-long-term-roadmap.md`
§§1, 4–5; `plans/003-planning-process.md`.

Applicable ADR: `plans/adrs/ADR-0008-embedding-inference-surface-boundary.md`.

Primary class: capability.

## 1. Objective

Accept the OpenAI-compatible `encoding_format: "base64"` request used by
PrivateGPT 1.0.1, return the same finite float32 vectors encoded as
little-endian base64, and preserve existing admission, validation, and usage
accounting.

## 2. Why this milestone is ready

E001 is implemented and conditionally closed. A sanitized PrivateGPT request
capture showed that its client sends a text array with
`encoding_format: "base64"`; EggPool rejected it during admission before an
upstream attempt. No new provider or storage contract is required.

## 3. Current implementation evidence

`rust/src/request/embeddings.rs` accepts only `float` or `base64`, sends the
qualified provider request using `float`, validates the upstream vectors, and
encodes base64 responses. `rust/src/coordinator/endpoints.rs` maps unsupported
formats to a stable client error. `rust/tests/fixtures/privategpt_1_0_1_embeddings_request.sanitized.json`
retains request shape and byte lengths but no client text or credentials.

## 4. Invariants that must not regress

- Request text and vectors remain absent from logs and persistence.
- Provider capability remains explicit; no arbitrary embedding route is
  inferred from chat support.
- Upstream response dimensions, finiteness, ordering, bounds, and usage
  semantics remain validated by the existing E001 path.
- Existing float responses and inference accounting remain unchanged.

## 5. Scope

### In scope

- Base64 request/response compatibility for finite OpenAI embeddings.
- Sanitized capture replay fixture and stable error behavior for unsupported
  encodings.
- Documentation and E001/R001 evidence corrections.

### Explicitly out of scope

- PrivateGPT storage, collection dimensions, retrieval, citations, or settings.
- Team document search/fetch, delegated ACLs, and any EggPool RAG persistence.
- Physical SBC footprint claims.

## 6. Required production changes

Extend the request encoding enum and response renderer. Keep upstream wire
requests in the currently qualified float form. Convert only validated finite
float32 values, using the byte order expected by the OpenAI Python client.
Return a stable 400 error for any other format. Do not retain or log content.

## 7. Ordered work packages

### Work package A — Wire compatibility

Intent: implement base64 client output without changing upstream provider
semantics.

Required changes: bounded parser enum, validated float32 serialization,
explicit unsupported-encoding error, API documentation.

Acceptance evidence: captured request replay returns one valid base64 vector
per input; unsupported format is rejected before provider submission.

### Work package B — Integration evidence

Intent: establish the real client request shape and separate EggPool admission
from downstream PrivateGPT storage configuration.

Required changes: sanitized fixture and updated R001 record.

Acceptance evidence: local capture of the synthetic PrivateGPT ingest shows
HTTP 200 from EggPool for the four-input base64 embedding request. Any
subsequent PrivateGPT storage failure is recorded separately.

## 8. Failure, cancellation, restart, contention semantics

The operation continues through the existing finite coordinator lifecycle.
Unsupported format and malformed response fail before successful response
delivery; normal terminal finalization and budget settlement remain in force.
No request retry or content buffering is added.

## 9. Compatibility and migration

No database or config migration. `float` remains supported and the default;
`base64` is additive. No-default builds keep the same provider eligibility
rules.

## 10. Required tests

- `rust/tests/embeddings_api.rs`: sanitized capture replay and unsupported
  encoding negative case.
- Serial default and no-default workspace suites.
- Live local Ollama hosted model→tool→model qualification is tracked
  separately from this embedding correction.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
```

## 12. Documentation updates

Update `docs/api-reference.md`, this plan, the E001 closure evidence, and the
R001 blocked evidence. Keep PrivateGPT retrieval/ACL claims explicitly
unqualified.

## 13. Acceptance criteria

The sanitized captured request is admitted and returns base64 vectors that
decode to the same finite float32 values. Unsupported encodings fail with a
stable 400 response before an upstream attempt. Default and no-default suites
and required dependency checks pass. No document persistence or retrieval
security claim is added.

## 14. Stop conditions

Stop if client byte order cannot be verified, base64 conversion cannot remain
bounded, or support requires changing provider selection or budget ownership.

## 15. Closure evidence required

Record fixture details, focused/full test results, dependency checks, live
capture result, documentation updates, security/migration review, and any
remaining PrivateGPT or SBC evidence separately.

## 16. Handoff notes

The captured body is sanitized. The local PrivateGPT setup and tokens are
temporary and must not be committed. Rust tests run serially.
