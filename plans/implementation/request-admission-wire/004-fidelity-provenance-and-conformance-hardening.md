# Request Admission and Wire Milestone 004 — Fidelity, Provenance, and Conformance Hardening

Status: active

Repository baseline: `61470ef788e287c49b4a51062eaba439049d46dc`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-004--fidelity-provenance-and-conformance-hardening`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#4-protocol-and-compatibility`
- `plans/001-terminology-and-domain-model.md`

Applicable ADRs:

- None required for an additive, pure library API that preserves EggPool behavior. If EggPool's routing/dispatch decisions, public API behavior, or canonical semantic meaning would change, stop and create a separate ADR/milestone.

Primary class: infrastructure

## 1. Objective

After M003 proves the internal extraction, harden the reusable wire kernel around the ecosystem gap that is actually differentiated: explicit preflight fidelity, bounded source-native provenance, strict terminal evidence/native observation, and a reusable protocol conformance corpus.

The new APIs are additive. EggPool must continue producing the same externally observable request/response/stream behavior and the same warn/reject decisions unless a separately reviewed capability change is approved.

## 2. Why this milestone is ready

Blocked on the hard dependency: M003 must close first. The public API should be designed against the actual extracted crate and its proven EggPool adapters, not against the pre-extraction module layout.

No external provider or crate is a hard dependency.

## 3. Current implementation and ecosystem evidence

EggPool already has several ingredients:

- `AdaptationOutcome::{Exact, Adapted, Rejected}`;
- bounded `AdaptationNotice` with machine-readable code/reason/field/source/target;
- `LossPolicy::{Warn, Reject}`;
- native Responses preservation with typed cross-surface blockers and explicit extension notices;
- `Presence<T>` for missing/null/value semantics;
- neutral reasoning intent and function/freeform/deferred-search tool kinds;
- strict stream terminal evidence and distinct EOF/malformed/incomplete outcomes;
- native stream observe-and-forward that collects bounded terminal/usage facts without re-encoding source bytes.

The September 2026 Rust landscape means a generic "canonical LLM IR" is not by itself a useful differentiator:

- `llm-api` 0.1.0 exposes typed conversion warnings/strict conversion and round-trip extras/metadata across OpenAI, Anthropic, and Google formats.
- `cortexfs-protocol` 0.1.7 exposes native request IRs, an owned semantic IR, and explicit transcode paths across Chat, Responses, Anthropic, and Gemini.
- `llm-dialect` 0.1.x provides sans-I/O canonical translation and SSE state machines for Anthropic/OpenAI Chat/OpenAI Responses.
- other gateway code, including `systemprompt::models::wire`, also centralizes canonical provider codecs.

Therefore M004 must not spend scope recreating SDK/provider-client abstractions. Its reusable value is auditable fidelity and provenance around conversion while retaining EggPool's stronger terminal/native-path guarantees.

## 4. Invariants that must not regress

- All M002/M003 wire and runtime invariants remain binding.
- Semantic IR remains provider-neutral and does not become a bag of arbitrary provider JSON.
- Source-native provenance is bounded, separately typed, redaction-safe in `Debug`, and never persisted/logged by EggPool.
- The planner and encoder use one semantic decision engine; a preflight result may not claim exactness if actual encoding emits a loss notice/rejection.
- Existing EggPool `LossPolicy::Warn/Reject` behavior remains stable or is wrapped by a compatibility layer proven equivalent on the full corpus.
- Unknown/provider-native fields are never silently dropped on a path that claims exactness.
- Unsupported native semantic items remain blockers rather than being converted to user text or fabricated functions.
- Provider-owned signatures, encrypted reasoning, IDs/status values, or terminal events are never invented merely to satisfy a target grammar.
- Native same-surface forwarding remains available and is preferred when current EggPool semantics require it.
- Transport EOF remains distinct from successful terminal evidence.
- All provenance/plan limits are deterministic and immune to unbounded attacker-controlled growth.

## 5. Scope

### In scope

- Add a pure translation-planning API over canonical request/response semantics.
- Define a stable fidelity vocabulary sufficient to distinguish exact preservation from representation-only normalization, semantic equivalence, material loss, and unsupported conversion.
- Replace/augment free-form adaptation notices with typed effect classification while retaining current notice compatibility.
- Generalize the Responses-specific preservation concept into a bounded `WireProvenance`/equivalent that is separate from the semantic IR.
- Encode explicit provenance completeness/truncation and source-surface identity.
- Allow same-surface codecs to use provenance to retain unknown/native structure where safe and bounded; never require cross-surface codecs to understand arbitrary provider fields.
- Publish protocol-only conformance vectors/tests as part of the workspace crate source.
- Add deterministic arbitrary-chunk-boundary stream cases, including UTF-8 boundaries and interleaved tool/reasoning/text events supported by current surfaces.
- Add public rustdoc describing guarantees and non-guarantees of fidelity classes, provenance, loss policy, and terminal outcomes.
- Keep the crate sans-I/O and runtime-free.

### Explicitly out of scope

- Provider HTTP clients, authentication, model catalogs, retries, routing, agents, tool execution, MCP, storage, or gateway server APIs.
- Automatically changing EggPool route selection based on the new fidelity planner.
- Loosening EggPool's current stateless Responses restrictions.
- Fabricating native-only metadata to make a conversion appear exact.
- General-purpose arbitrary JSON round-tripping with no size/shape budget.
- crates.io publication or repository split; those require a later release decision after internal qualification.
- Semver-1.0 commitment.
- Adding a dependency-heavy property-testing/fuzzing stack unless deterministic corpus testing cannot cover the required state-space; justify any new dev dependency separately.

## 6. Required production changes

### Translation plan and fidelity vocabulary

Introduce an API conceptually equivalent to:

```rust
pub struct TranslationPlan {
    pub source: WireSurface,
    pub target: WireSurface,
    pub fidelity: Fidelity,
    pub effects: Vec<AdaptationEffect>,
}

pub enum Fidelity {
    Exact,
    WireNormalized,
    SemanticallyEquivalent,
    Lossy,
    Unsupported,
}
```

Names may change after review, but semantics may not collapse material loss into a cosmetic warning. Define a total ordering/decision helper only if it is unambiguous and documented; otherwise expose explicit predicates instead of inviting callers to compare enum ordinals incorrectly.

The plan must be computable without transport or mutation and must share the same underlying checks as encoding.

### Typed adaptation effects

Add an effect class such as `Rewritten`, `Omitted`, `Approximated`, `Synthesized`, or `Blocked` where applicable. Do not mark a provider-owned fabricated value as `Synthesized` if fabrication is prohibited; it should remain unsupported.

Retain current EggPool notice codes as stable compatibility data during M004. If a new effect model replaces internal notice generation, provide deterministic mapping and prove the old `LossPolicy` outputs on the corpus.

### Bounded provenance

Introduce a source-native provenance object separate from `CanonicalRequest`/`CanonicalResponse`. Required properties:

- source surface/codec identity;
- bounded source fragments/shape metadata required for same-surface reconstruction;
- explicit completeness state and truncation reason;
- no credentials;
- redaction-safe `Debug`;
- configured hard ceilings on field count, nesting/bytes, and retained fragments;
- same-request/response lifetime by default; no persistence contract.

Migrate EggPool's current Responses preservation only after tests demonstrate the same native finite behavior and cross-surface blocker/notices. Other surfaces may begin with empty/partial provenance rather than pretending complete round-trip fidelity.

### Same-surface encode contract

Define when provenance may be consumed:

- Exact/native reconstruction may use provenance plus EggPool-owned model rewrite fields.
- Cross-surface encode operates from semantic IR plus typed plan/effects and does not blindly inject source extras.
- If provenance is incomplete/truncated, the plan cannot report `Exact` for fields outside the semantic model.

### Stream conformance

Keep stream provenance minimal: do not buffer complete streams. The existing native observation path should remain the model for exact forwarding—observe bounded protocol facts while returning source bytes. Add conformance vectors for terminal evidence, usage completion, malformed frames, post-terminal data, incomplete final frames, and arbitrary transport chunk splits.

## 7. Ordered work packages

### Work package A — Specify fidelity semantics against the corpus

Intent: define terms before changing code.

Required changes:

- Classify every current adaptation notice/path into fidelity/effect semantics.
- Mark native same-surface, canonical same-surface re-encode, wrapped freeform/deferred tools, reasoning-control drops, metadata/cache differences, tool-order collapse, and unsupported native items explicitly.
- Add tests asserting planner/encoder agreement.

Acceptance evidence:

- Every existing corpus case has an expected fidelity and ordered effects.
- No existing EggPool warn/reject decision changes.

### Work package B — Add bounded provenance

Intent: separate semantic meaning from wire-specific preservation.

Required changes:

- Introduce provenance types/limits.
- Adapt Responses preservation behind the new API with a compatibility wrapper for EggPool.
- Add partial/empty provenance semantics for other codecs without claiming unsupported round-trip guarantees.

Acceptance evidence:

- Native Responses finite tests and cross-surface blockers/notices remain identical.
- Redaction/limit/truncation negative tests pass.

### Work package C — Harden stream/native observation contract

Intent: make exact native forwarding a reusable protocol primitive rather than a gateway accident.

Required changes:

- Expose/document bounded native stream observation primitives needed to validate terminal/usage evidence without owning I/O.
- Preserve the split between byte forwarding (caller/runtime) and observation (kernel).
- Expand deterministic chunk-boundary fixtures.

Acceptance evidence:

- No full-stream buffering.
- Exact native bytes remain caller-owned/unchanged.
- Every terminal outcome remains distinguishable.

### Work package D — Conformance/public API hardening

Intent: make the internal library independently useful without forcing publication.

Required changes:

- Organize protocol-only fixtures and tests under the crate.
- Rustdoc the canonical/provenance/fidelity model, codec coverage, limits, MSRV, no-I/O guarantee, and unsupported semantics.
- Add guards against accidental runtime/network dependencies and unbounded `Debug` output.
- Keep EggPool integration tests consuming the same vectors or equivalent shared fixture source.

Acceptance evidence:

- Crate package tests pass independently.
- Root integration/full gates pass.
- API examples compile without Tokio/HTTP/config/provider clients.

## 8. Failure, cancellation, restart, contention semantics

The planner/provenance API is synchronous and pure. It must not change EggPool cancellation/retry/restart semantics. Provenance lifetime follows the request/response object; dropping it releases all retained source fragments. No background task, global cache, lock, external I/O, or persistence is allowed.

On provenance budget exhaustion, return explicit truncation/incompleteness facts. Do not silently discard data and continue to report exact fidelity.

## 9. Compatibility and migration

M004 is additive for the extracted crate and compatibility-preserving for EggPool.

EggPool may initially continue to call the existing compatibility methods, with those methods delegating to the new planner/provenance machinery. Routing must not start choosing providers based on `Fidelity` without a separate reviewed capability plan.

Keep:

- current `AdaptationNotice` serialized/string values if they are exposed to fixtures/diagnostics;
- current `LossPolicy` semantics;
- current `CodecReasonCode` categories;
- current native preservation/rejection decisions;
- current `WireSurface`/`WireCodecId` strings;
- current terminal outcome semantics.

No config, persistence, or wire migration is allowed.

## 10. Required tests

In addition to all M002/M003 tests:

- planner ↔ encoder agreement for every fixture;
- fidelity classification for exact/native, wire-normalized, equivalent, lossy, unsupported cases;
- notice/effect compatibility mapping;
- provenance size/count/depth/truncation limits;
- redaction-safe `Debug`;
- same-surface restoration with unknown bounded extensions;
- incomplete provenance cannot claim exactness;
- cross-surface encode never injects source-native extras blindly;
- deterministic chunk split matrix for every streaming dialect;
- terminal evidence and EOF/malformed/post-terminal cases;
- no-fabrication tests for reasoning signatures/encrypted payloads/provider IDs;
- package-level dependency-boundary guard.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Also run package-only doc tests/tests for the extracted crate.

## 12. Documentation updates

- Add crate-level rustdoc and examples explaining semantic IR vs provenance vs adaptation/fidelity.
- Update `architecture/deep-dive-transcoder.md` with the same three-layer model and exact EggPool ownership.
- Update `architecture/deep-dive-request-lifecycle.md` only if the provenance lifetime/ownership description changes.
- Document comparison boundaries: this is a protocol kernel, not an SDK/provider client, router, or agent framework.
- Do not add publication badges/claims until a later release milestone actually publishes the package.

## 13. Acceptance criteria

- The planner can preflight every supported source/target pairing and agrees with actual encode loss/rejection behavior.
- Provenance is separate, bounded, redaction-safe, and cannot cause an incomplete conversion to be labeled exact.
- EggPool's existing adaptation warnings/rejections remain compatible across the full corpus.
- Native Responses finite and streaming behavior remains unchanged.
- No provider-owned semantic metadata is fabricated.
- Stream terminal evidence remains strict and complete streams are never buffered merely for provenance.
- Protocol-only conformance vectors run at the crate level and EggPool integration remains green.
- Full default/no-default/release/dependency gates pass.
- No external publication is required for closure.

## 14. Stop conditions

Stop and report if:

- M003 is not closed;
- a fidelity class cannot be defined without changing current EggPool behavior;
- provenance requires unbounded raw payload/stream retention;
- planner and encoder would need separate semantic logic;
- preserving unknown fields would inject unsafe source extensions into another provider grammar;
- no-fabrication guarantees would need to be weakened;
- a routing/provider-selection behavior change is proposed;
- external publication/repository movement is required to finish the implementation.

## 15. Closure evidence required

The closure record must include:

- fidelity/effect taxonomy with representative fixture mapping;
- planner/encoder agreement results;
- old notice/loss-policy compatibility matrix;
- provenance bounds and negative-test evidence;
- native Responses finite/stream parity evidence;
- arbitrary-chunk/terminal stream conformance results;
- package-only dependency and docs/test results;
- full EggPool default/no-default/release/cargo-deny results;
- explicit statement that routing, admission, config, persistence, transport, retry, and public HTTP behavior did not change;
- residual API/publication questions for any later standalone release milestone.

## 16. Handoff notes

Do not use M004 as an excuse to redesign every provider API. Favor a small auditable kernel and conservative fidelity claims. "Unknown" or "unsupported" is preferable to a false exactness guarantee. Keep the package internal/unpublished until a later user-directed release decision.
