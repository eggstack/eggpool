# Embedding Routing E001 — OpenAI-compatible embeddings and model eligibility

Status: blocked

Repository baseline: `0e021c1ffe55803aff5f325033e65313dca9f1dc` (planning, 2026-11-03)

Source roadmap: plans/subsystems/embedding-routing-roadmap.md#7-milestones

Long-term requirements: plans/000-long-term-specification.md ``1–5; plans/001-terminology-and-domain-model.md; plans/002-long-term-roadmap.md ``1, 4–5; plans/003-planning-process.md

Applicable ADR: plans/adrs/ADR-0008-embedding-inference-surface-boundary.md (proposed, hard gate)

Primary class: capability

## 1. Objective

Implement a typed finite OpenAI-compatible `POST /v1/embeddings` route that selects only explicitly qualified embedding models, applies existing per-user authentication/model restrictions and provider quota/health/budget controls, validates outputs, and attributes usage through the existing coordinator lifecycle. No vector index or document storage.

## 2. Readiness and dependencies

**Hard:** ADR-0008 acceptance. **Interface:** existing canonical routing model, generation lease, user budgets (hard-vs-estimated), provider profile authority, provider catalog, error mapping and request finalization. **Operational:** one real known-compatible local embedding upstream and a deterministic synthetic upstream. No hard dependency on Eggsearch tool plans.

## 3. Current implementation evidence

rust/src/server/mod.rs exposes Chat/Responses/Messages/compact only. rust/src/server/inference.rs delegates to rust/src/coordinator/ without independently finalizing requests. rust/src/wire/ir.rs re-exports eggpool-wire's dialogue IR; it should not be reused as a fictional chat representation for embeddings. rust/src/providers/, rust/src/catalog/, rust/src/model_policy.rs, rust/src/instance_budget.rs, rust/src/user_budget.rs and rust/src/db/ own routing/capability/policy/admission/accounting. docs/api-reference.md does not advertise embeddings and docs/stateless-responses.md deliberately excludes unrelated stateful API behavior.

## 4. Non-regression invariants

Embedding calls cannot bypass model visibility, credential revocation, hard user/instance budgets, provider quota accounting, health gates, virtual aliases or startup/reload consistency. Model/input bytes and vector output are never logged/persisted. Existing four inference endpoints and their streaming/caching semantics remain unchanged. No second SQLite gate, implicit tokenizer download, vector DB, model executor, default service or blanket provider feature inference.

## 5. Scope

**In:** typed OpenAI embedding request input (`str` or array of strings with explicit finite maxima), model and supported optional dimensions/encoding_format, concrete model capability registry, eligible upstream endpoint mapping, validated indices/float vectors or supported encoding result, usage and safe errors, user attribution and published model discoverability. Start with one qualified OpenAI-compatible downstream/upstream format.

**Out:** images/audio embeddings, reranking, batch background jobs, hosted file upload, local embedding inference, cross-provider embedding transcoding without verified contract, similarity retrieval, content cache or multi-modal vectors.

## 6. Required production changes

Create a dedicated embedding request/response semantic type in the wire/request layer, with JSON parser limits before forwarding, input count/cumulative bytes, valid nonempty string and array shape, finite dimensional parameter limits and explicit output encoding policy. Choose safe defaults and document precise maxima; enforce body limits before parsing. Record no raw input. Register `POST /v1/embeddings` in the existing ingress admission/auth classification and reuse existing principal attribution, hard budget reservations, cancellation and generation-lease ownership, including on early rejection.

Introduce a narrowly scoped embedding wire-surface capability on provider profiles and catalog entries; never treat a Chat Completions-enabled model as embedding-qualified. Route only to configured and verified local or OpenAI-compatible embedding endpoints. If provider naming/URL templating differs, reject it until explicitly qualified rather than forwarding to a guessed path. Require exact model-policy decision after alias resolution and prevent per-user `/v1/models` from leaking forbidden embeddings. Keep output ordering and indexes stable and validate dimensions, vector count, finite JSON floats, response byte cap, ID/model/usage schema; misbehaving upstreams produce typed provider errors.

Integrate embeddings into quotas, provider health, request kinds and cost/usage without claiming generative completion tokens. Missing usage must be represented honestly; conservative hard limits must be proven before dispatch using a model-specific input/token bound or fail closed, not rely on downstream reported usage after the fact. Ensure quota/health effects remain attempt-scoped and finalization exactly once. Any necessary additive metric schema migration must preserve older rows and backup/restore; avoid column repurposing that would alter historical dashboards.

## 7. Ordered work packages

A. **Capability/contract decision.** Freeze request shape, valid model identifiers, output format and supported endpoint matrix, with local real model evidence. Write unsupported-shape tests first.

B. **Admission/selection.** Add typed embedding operation, authenticated route, explicit surface/model selection and per-user grants. Integrate hard budgets before upstream submission. No new artificial dialogue IR.

C. **Upstream/result lifecycle.** Dispatch via existing transport/client pool, validate response and usage, settle request through current coordinator. Cover cancellation and provider errors before/after response handoff.

D. **Discovery/metrics.** Publish truthful model capability and correct per-operation usage counters without changing ordinary chat model metadata; test old/new stats and backups.

E. **Live and regression qualification.** A local compatible embedding server, default/no-default Rust suite, mixed chat/embedding load and disabled/no-provider behavior.

## 8. Failure, cancellation, restart and contention semantics

A missing eligible embedding provider returns typed capability/selection error before submission and consumes no provider attempts. Provider wrong response, wrong embedding count, NaN/inf, duplicate indexes, excessive vectors, unknown encoding or timeout fail safely and settle holds without raw output leakage. Client disconnect cancels/finishes through existing finalization contract; no detachable job. Provider backoff applies only where existing classifier considers a failure provider-owned; an invalid caller request is never provider unhealthiness. No new background polling or DB queue.

## 9. Compatibility and migration

No existing endpoint behavior changes; addition is a strictly bounded route. Existing legacy/shared or managed tokens retain only the model access to which their policy entitles them; do not accidentally expose new embedding models. Config omissions leave route present but without eligible models, returning a safe capability denial. All schema changes additive, with old database restore and migration evidence. Deployment requires operator-provided embedding upstream; EggPool never downloads weights.

## 10. Required tests

Add rust/tests/embeddings_api.rs and targeted provider wire tests. Include singleton/array input, optional dimensions and encoding, model policy denial, aliases, hard user+instance unknown price and tokens, explicit capability filter, malformed/infinite/oversize vector responses, index ordering, bad usage, body bounds, provider retry and quarantine classification, disconnect/cancel, mixed chat+embedding concurrency and restore. Re-run rust/tests/server_auth.rs, on_prem_budgets.rs, on_prem_global_budgets.rs, quota.rs, model_router.rs, coordinator_finalization.rs, wire_qualification.rs and database_compatibility.rs.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Run `cargo test --manifest-path rust/Cargo.toml --test embeddings_api -- --test-threads=1` plus recorded live local embedding backend with exact version and model ID. Measure absent-provider footprint on SBC-style config.

## 12. Documentation

docs/api-reference.md, docs/providers.md, docs/model-limits.md, docs/agent-configuration.md, docs/deployment.md and architecture/overview.md / relevant wire and admission deep dives. Publish exact supported embedding request shape and known qualified upstreams. Never label generic OpenAI API parity.

## 13. Acceptance criteria

A real OpenAI-compatible embeddings client can request an authorized model and receive complete correctly ordered vectors and usage. Unauthorized/unqualified models reject before upstream. Failures/cancellation reconcile budgets and provider metrics correctly. Normal Chat/Responses/Messages and no-default tests pass; no source text/vector retention.

## 14. Stop conditions

Stop before public endpoint changes if ADR-0008 remains proposed, if hard budget proofs cannot be maintained, if provider model-eligibility cannot be verified, if output shape is insufficiently bounded, or if embeddings require a new persistence/indexing subsystem. Do not silently weaken quotas to ship compatibility.

## 15. Closure evidence

Exact supported upstream matrix, fixture and live request/response shape test (redacted vector/text), authorization and budget matrix, failure and finalization correctness, migration/restore and no-backend evidence, deployment documentation, CI/default/no-default tests and footprint comparison.

## 16. Handoff notes

Keep embedding output and per-operation cost separate from generative dialogue token semantics. If a third-party backend only supports a nonstandard route, record it as deferred rather than adding a generic arbitrary URL tunnel.
