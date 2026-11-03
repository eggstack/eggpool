# Embedding Routing Milestone E001 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/embedding-routing/001-openai-compatible-embeddings.md`

Source subsystem roadmap:

- `plans/subsystems/embedding-routing-roadmap.md#7-milestones`

Repository baseline reviewed: `088a1379`

Implementation commits or pull requests:

- `088a1379` — bounded OpenAI-compatible embeddings route, explicit capability config, shared coordinator admission/finalization, docs, and deterministic/live Ollama coverage.

## 1. Executive finding

The additive `POST /v1/embeddings` capability is implemented for explicitly
configured OpenAI-compatible provider surfaces. Deterministic API tests and a
real EggPool→Ollama 0.17.7 `nomic-embed-text:latest` smoke returned two ordered,
finite 768-dimensional vectors. Full default/no-default Rust suites and strict
Clippy pass. No database migration was needed. Conditional closure is due only
to the planned physical SBC absent-provider footprint/contention measurement,
which cannot be performed on this x86 host.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Bounded OpenAI input and response contract | `rust/src/request/embeddings.rs`; unit tests in the module | pass | Request count/bytes, item size, body size, dimensions, output bytes, finite vectors, indices, model, and usage are validated. |
| Explicit provider embedding eligibility and endpoint mapping | `rust/src/config.rs`, `rust/src/coordinator/attempt.rs`, `rust/src/coordinator/finite.rs` | pass | Provider must configure both an absolute embeddings path and exact model allowlist; defaults disable eligibility. |
| Shared identity/model policy, provider routing, quota, budgets, health, and finalization | `rust/src/server/inference.rs`, `rust/src/coordinator/endpoints.rs`, `rust/src/coordinator/finite.rs`; `server_auth`, `on_prem_budgets`, `on_prem_global_budgets`, `quota`, `model_router`, `coordinator_finalization` suites | pass | Embedding operation uses the existing authority; it does not add a public field to `FiniteRequest`. |
| Malformed, unsupported, and unqualified requests fail safely | `rust/tests/embeddings_api.rs::malformed_or_unqualified_embedding_requests_fail_before_provider_submission` | pass | Chat-only model, invalid input/streaming, and unsupported encoding do not reach the provider. |
| Real compatible upstream response | EggPool→Ollama 0.17.7 live local API run with `nomic-embed-text:latest`; ignored live test is `qualified_ollama_model_returns_live_embedding_vectors_through_eggpool` | pass | Two input items returned in order with 768 finite dimensions each. Vector contents were not retained in this record. |
| Default/no-default parity, migration and restore | Full workspace suites, migration/compatibility suites; no E001 schema change | pass | No migration or restore delta was introduced. |
| Physical SBC absent-provider footprint and contention | No physical SBC available in this environment | not run | Required only for the device-specific footprint claim; no runtime overhead claim is made. |

## 3. Production implementation evidence

The route has a dedicated bounded request/response type and uses the existing
inference request lifecycle. The server adapter authenticates and resolves the
principal; the coordinator performs route selection, admission, retries,
health effects, cancellation, and finalization. Exact embedding model
eligibility is separate from chat protocol capability. Missing provider usage
stays unknown instead of being fabricated. The client-visible model identifier
is preserved after provider response validation.

Configuration adds `embedding_models` and `embeddings_path` as a paired,
explicit opt-in. The example config remains disabled unless an operator adds
both values. No provider weights, tokenizer, content cache, or vector store is
introduced.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test embeddings_api -- --test-threads=1
python scripts/validate_runtime_package_boundary.py
python scripts/validate_release_docs.py
```

### Results

- Formatting and strict Clippy passed with default and no-default features;
  no-default `cargo check` passed.
- Full default suite: 94 targets, 1,241 passed, 4 ignored, 0 failed.
- Full no-default suite: 94 targets, 1,242 passed, 4 ignored, 0 failed.
- Focused embeddings API suite: 2 passed, 1 ignored, 0 failed. The ignored
  live test was separately exercised manually against the local Ollama server.
- Runtime package boundary validation passed.
- Release documentation validation passed.

These are local results, not CI claims.

## 5. Invariant review

- Existing Chat/Responses/Messages request shapes and behavior are unchanged.
- The endpoint does not accept virtual embedding aliases and does not infer
  embedding capability from Chat Completions support.
- Request text and vectors are neither logged nor persisted.
- One SQLite gate and the existing generation/coordinator lifecycle remain in
  use.
- Embedding usage is not mislabeled as generated completion tokens; absent
  upstream usage remains unknown.
- `--no-default-features` compiles and passes the serial workspace suite.

## 6. Failure and recovery review

Invalid body shape, excessive input/output, unsupported encoding, wrong model,
wrong count/index/dimension, non-finite vector, and malformed provider
response are rejected. Provider-owned failures flow through the established
retry and health classifier. Client cancellation and settlement remain owned
by the coordinator. The API test covers invalid/unqualified early rejection
and successful ordered responses; shared coordinator tests cover general
finalization, failure, and cancellation semantics.

## 7. Migration and compatibility review

No schema migration was added, so database restore and historical accounting
remain unchanged. Provider config omission disables embedding eligibility.
The sample configuration and manifest hash are synchronized. The endpoint is
an additive API and has no impact on older databases or deployments.

## 8. Security review

The route uses ordinary authenticated inference principal and model-policy
resolution. It requires exact operator-configured embedding model eligibility
and does not accept alias-based capability expansion. Body and response sizes,
item counts, dimensions, and item lengths are bounded. Inputs, vectors, and
credentials are not emitted to logs or metrics. No new secret is added to
provider-profile assets or checked-in configuration.

## 9. Documentation and operations

Updated `docs/api-reference.md`, `docs/providers.md`, `docs/model-limits.md`,
`docs/agent-configuration.md`, `docs/deployment.md`,
`architecture/overview.md`, and `architecture/deep-dive-transcoder.md`. The
documented surface is explicitly limited to configured OpenAI-compatible
providers and names the current Ollama test target. The runtime manifest hash
matches `rust/config.example.toml`.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Physical SBC absent-provider footprint and contention comparison was not run. | No device-specific footprint or solo/SBC overhead claim is supported. | Run the planned paired physical-SBC measurement before making that claim. |

## 11. Roadmap disposition

Conditionally closed. The user-visible route, explicit model eligibility,
accounting lifecycle, deterministic/live compatibility evidence, docs, and
default/no-default test requirements are complete. The only outstanding item
is external device-specific footprint evidence; no new runtime work is
authorized by this closure.

## 12. Registry updates

The registry and Embedding Routing roadmap now link this record and mark E001
conditionally closed. Retrieval Interoperability R001 can consume the
embedding interface but remains independently blocked on secure document
delegation and its failed full-reference ingest/retrieval smoke.
