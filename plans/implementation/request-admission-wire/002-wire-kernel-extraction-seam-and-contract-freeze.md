# Request Admission and Wire Milestone 002 — Wire-Kernel Extraction Seam and Contract Freeze

Status: ready

Repository baseline: `61470ef788e287c49b4a51062eaba439049d46dc`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-002--wire-kernel-extraction-seam-and-contract-freeze`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` — canonical wire boundary, generation-owned execution, secret-free diagnostics, and no-default parity.
- `plans/000-long-term-specification.md#4-protocol-and-compatibility` — current public surfaces, bounded adaptation notices, and terminal-evidence requirement.
- `plans/001-terminology-and-domain-model.md` — admission and Canonical IR ownership.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — sustaining compatibility while dependencies and internals evolve.

Applicable ADRs:

- None required. This milestone preserves the current ownership decision and changes no public protocol, config, storage, transport, or runtime owner. If implementation requires moving runtime/admission authority into the reusable kernel, stop and write an ADR rather than expanding scope.

Primary class: invariant

## 1. Objective

Make EggPool's current wire implementation mechanically extractable without moving it yet: remove EggPool-only imports from the intended sans-I/O kernel, separate generic wire decoding from EggPool admission/policy, and freeze a deterministic compatibility corpus that detects any semantic or streaming regression before the source move.

## 2. Why this milestone is ready

Request-admission-wire M001 is closed and the server/coordinator body boundary is stable. The existing wire suite already covers all current surfaces and the extraction requires no external dependency or upstream API. M003 depends hard on this closure; no later extraction work should begin against an ambiguous seam.

## 3. Current implementation evidence

At `61470ef788e287c49b4a51062eaba439049d46dc`:

- `rust/src/wire/ir.rs` defines the canonical request/response/event vocabulary, `Presence<T>`, reasoning intent, tool kinds, usage, provider-error evidence, and content/media blocks. Its only direct runtime leak is the conversion from reasoning intent into `crate::routing::ThinkingRequirement`.
- `rust/src/wire/adaptation.rs` owns bounded adaptation notices and `LossPolicy`, but imports `crate::catalog::{CapabilityStatus, ThinkingCapability}` and `crate::request::NativeRequestPreservation`.
- `rust/src/wire/codecs.rs` and `additional_codecs.rs` own the finite surface grammars but invoke `request::canonical_request_from_value` and request-owned media validators.
- `rust/src/request/admission.rs` currently combines several distinct responsibilities: bounded raw-body parsing/depth, EggPool stateless Responses policy, canonical wire decoding, native Responses preservation, token/context estimation, routing-fact projection, and affinity projection.
- `rust/src/wire/registry.rs` combines neutral wire/profile types with constructors over EggPool config types.
- `rust/src/wire/stream.rs` is already close to the desired pure boundary: incremental SSE framing, provider-specific event decoding, canonical event encoding, usage normalization, terminal evidence, and native observation.
- `rust/src/wire/runtime.rs` intentionally joins EggPool request/routing/profile/runtime state and is not an extraction target.
- Regression coverage exists in `rust/tests/wire_*.rs`, `canonical_request.rs`, `codex_responses_compat.rs`, `codex_compaction_compat.rs`, coordinator boundary tests, and `tests/fixtures/wire/`.

Current ecosystem research also means the extraction should not optimize for "a canonical IR exists"; crates such as `llm-api`, `cortexfs-protocol`, `llm-dialect`, and other gateway wire modules already occupy that space. The contract worth protecting is EggPool's combination of bounded loss reporting, native preservation, null-vs-missing presence, Gemini Interactions coverage, strict terminal evidence, and native stream observation.

## 4. Invariants that must not regress

- `rust/src/wire/ir.rs` remains the EggPool canonical boundary, even if it later becomes a re-export facade over an internal workspace crate.
- Codecs continue to build target payloads from canonical semantics; translated payloads are never chained through another provider grammar.
- Chat Completions, Responses, Responses Compact, Anthropic Messages, Gemini Interactions, and Gemini generateContent behavior remains unchanged.
- Native Responses same-surface requests retain their current parsed-envelope preservation semantics, including native-only item/tool blockers for cross-surface dispatch.
- Native Responses streaming continues to forward valid source bytes while observing bounded usage/terminal evidence; no arbitrary native stream buffering is introduced.
- Transport EOF is never synthesized into success.
- Existing `AdaptationNotice` codes, `CodecReasonCode`, `LossPolicy::Warn/Reject`, and max-notice behavior remain stable in M002.
- Missing, explicit null, and concrete values remain distinguishable wherever `Presence<T>` currently preserves that distinction.
- Provider-owned signatures/encrypted reasoning/metadata are never fabricated.
- EggPool's exact current request media/depth/count/size acceptance and rejection behavior remains unchanged.
- Request body/resource admission, token/context estimates, routing/catalog/config policy, compaction execution, retries, transport, publication, and finalization remain EggPool-owned.
- No prompt, raw body, schema payload, credential, encrypted reasoning, or tool arguments are added to logs/diagnostics.
- `--no-default-features` parity remains intact.

## 5. Scope

### In scope

- Define an explicit "extractable wire kernel" module boundary inside the current package.
- Move EggPool-specific conversion helpers out of canonical types; e.g. reasoning-to-routing conversion belongs in the request/routing adapter.
- Introduce neutral capability facts needed by adaptation policy and map EggPool catalog facts into them at the boundary.
- Move/define native feature summary/provenance facts in the wire boundary while leaving raw-body lifetime/admission ownership in `request/`.
- Split generic canonical request decoding from EggPool's stateless Responses product policy and token/context/resource accounting. EggPool must still apply the same policy before dispatch.
- Separate neutral wire/profile vocabulary from `Config` conversion; config-to-profile construction remains in EggPool.
- Establish explicit decode limits/options for protocol parsing only where necessary to remove request-module imports. EggPool adapters must pass values exactly equal to the current constants; do not broaden or tighten acceptance.
- Add a dependency-boundary guard that makes forbidden runtime imports from the extractable modules fail CI/tests.
- Freeze protocol-only fixtures/contract vectors sufficient to compare pre/post extraction semantics.

### Explicitly out of scope

- Creating the new workspace crate or moving source files; that is M003.
- Publishing a crate, creating a new repository, or changing package ownership.
- New provider surfaces, new tool semantics, broader media support, or looser Responses stateless policy.
- Replacing EggPool's current native-preservation representation with the generalized provenance API; that is M004.
- Changing notice names, loss-policy behavior, stream terminal rules, buffering strategy, request limits, routing decisions, or provider profile selection.
- Moving `wire/runtime.rs`, coordinator code, HTTP types, transport clients, retry state, config, database, or runtime-generation state into reusable code.
- Adding Tokio/Axum/Hyper/Reqwest to the extraction boundary.

## 6. Required production changes

### Canonical/routing separation

Remove the direct routing conversion from `ReasoningIntent`. Provide an EggPool-owned adapter that produces the identical `ThinkingRequirement` facts from a canonical request. Routing tests must prove byte/value-equivalent facts before and after the move.

### Capability-policy separation

Replace `adaptation.rs` dependencies on catalog structs with a neutral capability DTO owned by the wire boundary. EggPool's catalog adapter must preserve all current statuses and the current default `ReasoningCapabilityPolicy` behavior, including mixed/unknown/unsupported distinctions.

### Request-decoder separation

Factor the structural wire decoder used by `canonical_request_from_value` away from admission-side concerns. The wire decoder may validate protocol structure and bounded media/tool/content shapes. EggPool retains:

- raw-body size/depth admission and one-parse ownership;
- stateless Responses restrictions such as `store`, `previous_response_id`, conversation/background policy and compact-specific restrictions;
- token/context/reservation estimation;
- generation/resource ownership;
- routing and affinity projection.

If moving a validator would change error precedence or the accepted language, preserve the existing order at the EggPool wrapper and add a regression case.

### Native preservation separation

Make the redaction-safe summary facts consumed by adaptation independent of `request::NativeRequestPreservation`. The request layer may still own the parsed `Value` lifetime in M002. Cross-surface blocker/extension notice behavior must remain byte-for-byte/code-for-code equivalent.

### Registry/config separation

Keep neutral codec family, wire surface, codec ID, configured profile data, and compatibility-path concepts in the extraction boundary. Move constructors that consume `ProviderWireSurfaceConfig` or `ModelWirePreference` to an EggPool-owned adapter.

### Contract corpus and dependency guard

Create deterministic fixtures/expectations for:

- every current client/upstream surface pairing used by qualification;
- exact/adapted/rejected request outcomes and ordered adaptation codes;
- finite response conversion, usage/cache/reasoning counters, provider errors, and stable tool IDs;
- function/freeform/deferred-search tool definitions and calls;
- text/image/document/audio rejection/translation cases already supported;
- `Presence::Missing` vs `Null` vs `Value`;
- native Responses extension/native-item/native-tool preservation decisions;
- arbitrary SSE byte splits including UTF-8 splits within the existing decoder's supported framing model;
- terminal success/incomplete/provider-error/malformed/EOF-before/EOF-after-partial outcomes;
- native observation and translated streaming equivalence.

Use existing fixture material where possible; do not copy prompts or credentials into new artifacts.

The guard must prove the intended kernel does not depend on EggPool routing, catalog, config, request-runtime/resource, model-router, provider, database, server, coordinator, Tokio, Axum, Hyper, TLS, or transport types.

## 7. Ordered work packages

### Work package A — Freeze compatibility expectations

Intent: make regressions observable before refactoring.

Required changes:

- Inventory existing wire fixtures/tests and add only the missing deterministic cases above.
- Record semantic JSON equality where object ordering is not contractually meaningful; require exact bytes only where EggPool currently promises/depends on native byte forwarding.
- Add direct assertions for ordered adaptation notice codes/reasons/fields and terminal summaries.

Acceptance evidence:

- New contract cases pass against the pre-extraction implementation.
- No production source move is required to make the corpus pass.

### Work package B — Invert canonical/runtime dependencies

Intent: make canonical types and adaptation logic independent of EggPool routing/catalog/request/config structs.

Required changes:

- Move conversion adapters to EggPool-owned modules.
- Introduce neutral capability and native-summary facts.
- Preserve public/internal re-exports needed by current callers.

Acceptance evidence:

- Forbidden-import guard passes.
- Routing/capability/native-preservation regression tests produce the same results.

### Work package C — Separate generic decode from admission policy

Intent: let finite codecs consume a pure canonical decoder without importing EggPool request runtime.

Required changes:

- Isolate protocol structural decoding and protocol-safe validation.
- Keep one-parse body ownership and EggPool stateless policy in `request/admission.rs`.
- Parameterize only limits that must cross the boundary and pass current constants explicitly.

Acceptance evidence:

- `canonical_request.rs`, Codex Responses/compact tests, request admission, wire codecs, and multimodal tests preserve all success/failure outcomes and error precedence.

### Work package D — Separate neutral profile vocabulary from config

Intent: eliminate the final root-config dependency from the extractable boundary.

Required changes:

- Keep data-only wire profile structs/IDs neutral.
- Map EggPool configuration into those structs outside the kernel.

Acceptance evidence:

- `wire_profiles.rs`, reload/config tests, and runtime profile selection remain unchanged.

## 8. Failure, cancellation, restart, contention semantics

This is a pure-boundary refactor. It must not change cancellation, generation, retry, publication, or provider-client lifetimes. A failed decode/adaptation still fails before upstream submission at the same logical boundary. Native stream observation retains the existing bounded incremental parser and terminal summary. No new global state, cache, worker, lock, async task, or waiter is permitted.

If factoring the decoder requires retaining additional request content beyond the existing admitted request lifetime, stop: that violates the intended extraction boundary.

## 9. Compatibility and migration

No external migration. Keep current EggPool module paths/re-exports stable for M002. No persisted/config schema changes.

Compatibility is semantic and error-path compatibility, not merely compilation. Preserve:

- successful JSON payload meaning and current explicitly native-preserved fields;
- current adaptation notice ordering/codes;
- current typed rejection categories;
- current stream grammar and terminal classification;
- current current-tool wrapper semantics and stable IDs;
- exact current public HTTP behavior through the integration tests.

## 10. Required tests

At minimum, run and extend:

- `rust/tests/canonical_request.rs`
- `rust/tests/wire_adaptation.rs`
- `rust/tests/wire_codecs.rs`
- `rust/tests/wire_multimodal.rs`
- `rust/tests/wire_profiles.rs`
- `rust/tests/wire_qualification.rs`
- `rust/tests/wire_runtime.rs`
- `rust/tests/wire_stream.rs`
- `rust/tests/codex_responses_compat.rs`
- `rust/tests/codex_compaction_compat.rs`
- relevant coordinator boundary/execution tests that assert admitted request and translated dispatch behavior.

Tests must run serial where the repository requires `--test-threads=1`. No sleep/yield-based synchronization may be introduced.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Also run the focused test targets listed in §10 during development.

## 12. Documentation updates

- Update `architecture/deep-dive-transcoder.md` to distinguish the pure extraction seam from EggPool admission/runtime ownership.
- Update `architecture/deep-dive-request-lifecycle.md` if canonical decoder ownership moves.
- Keep `plans/000-long-term-specification.md` unchanged in M002; `rust/src/wire/ir.rs` remains the canonical facade/path.
- Add no standalone-crate marketing/publication docs yet.

## 13. Acceptance criteria

- The extraction contract corpus passes before and after the seam refactor.
- Intended extractable modules have no forbidden EggPool/runtime imports.
- EggPool admission still performs one bounded parse and exact current stateless Responses policy.
- Every current finite/streaming surface and cross-surface path has unchanged success/adaptation/rejection behavior.
- Native Responses preservation and native observation semantics are unchanged.
- No transport/runtime/config/storage/dependency capability is moved into the kernel.
- Full default/no-default workspace gates and dependency audit pass.

## 14. Stop conditions

Stop and report rather than improvise if:

- preserving current behavior requires changing an external/public wire contract;
- decoder separation changes accepted/rejected inputs or error precedence without a clear compatibility shim;
- a neutral type would need credentials, transport state, config, routing state, or unbounded raw payload ownership;
- native Responses forwarding would need to be canonicalized to fit the seam;
- a new dependency is needed merely to make the refactor convenient;
- repository state has changed enough that the baseline dependency graph is no longer accurate.

## 15. Closure evidence required

The closure record must contain:

- before/after import/dependency-boundary evidence;
- a table mapping each removed root dependency to its EggPool adapter replacement;
- contract-corpus pass evidence and the existing focused wire/Codex test results;
- default/no-default full workspace results;
- cargo-deny and dependency/feature graph results;
- explicit statement that no public wire capability, request limit, loss policy, native preservation behavior, terminal rule, config/storage schema, or provider surface changed;
- residual findings and whether M003 is unblocked.

## 16. Handoff notes

Do not move files into a new crate in this milestone. The purpose is to make the move boring and mechanically reviewable. Preserve current user changes and the repository's serial-test requirement. Do not "improve" wire semantics while establishing the seam; record improvement opportunities for M004 instead.
