# Plan 232 — Compact Production Dual-View Memory Cleanup

Date: 2026-09-21
Status: complete
Planning baseline: 3b9b63861e554161c152520491e0bd050c864f02
Parent roadmap: plans/230-residual-native-runtime-efficiency-roadmap.md
Prerequisite: Plan 231 complete
Priority: P1/P2 evidence-gated large-request peak-allocation reduction

## Purpose

Investigate and, only if it can be done without public API regression, remove the second source-native JSON-tree clone retained by the production remote-compaction execution path.

Plan 229 correctly preserved the public FiniteRequest shape. The current production constructor therefore stores both:

- FiniteRequest.admitted: AdmittedRequest;
- FiniteRequest.compact_admission: Option<CompactAdmittedRequest>.

For compact requests, constructing admitted clones CompactAdmittedRequest.native_preservation, including the full parsed serde_json::Value, while compact_admission retains the original. Large histories can therefore occupy two independently allocated parsed trees for the lifetime of the finite request.

This plan must not solve that by changing public field types.

## Authority

- rust/src/request/admission.rs
- rust/src/coordinator/endpoints.rs
- rust/src/coordinator/finite.rs
- rust/src/coordinator/attempt.rs
- rust/src/wire/runtime.rs
- rust/tests/codex_compaction_compat.rs
- rust/tests/coordinator_c008.rs
- rust/tests/coordinator_c009.rs
- rust/tests/coordinator_c011.rs
- rust/tests/coordinator_boundaries.rs
- rust/tests/wire_runtime.rs
- rust/tests/wire_qualification.rs

## Public compatibility boundary

Do not change:

- FiniteRequest field names, visibility, or types;
- AdmittedRequest field names/types;
- CompactAdmittedRequest field names/types;
- NativeRequestPreservation.parsed from serde_json::Value;
- FiniteRequest::new;
- FiniteRequest::new_compact;
- FiniteRequest::from_compact_admitted;
- public admission/wire helpers.

Compatibility callers and tests may continue to construct the public FiniteRequest representation and pay its existing compatibility cost.

The optimization target is the production endpoint-to-coordinator path only.

## Investigation first

Establish whether production compact execution actually needs both complete public views after Plan 231.

Trace reads of:

- request.admitted;
- request.compact_admission;
- request.routing_facts;
- request.raw_body;
- request.client_surface;
- request.operation;

through finite coordination, attempt preparation, wire dispatch, publication, retry, response adaptation, and finalization.

Classify each read as:

1. ordinary-generation only;
2. compact only;
3. shared semantic field;
4. public compatibility helper/test only.

Record the result in the closure evidence.

## Preferred implementation direction

Introduce a crate-private execution representation owned by the finite coordinator, for example an enum or internal struct that can represent:

~~~text
Generate
  -> one AdmittedRequest

Compact
  -> one CompactAdmittedRequest
  -> derived canonical/routing scalar references as needed
~~~

The production endpoint may construct that internal representation directly after final admission.

The public FiniteRequest remains available and may be converted into the internal representation for compatibility callers.

A suitable architecture is:

~~~text
public FiniteRequest compatibility surface
            |
            v
internal FiniteExecutionInput
            ^
            |
production endpoints
  generate -> AdmittedRequest once
  compact  -> CompactAdmittedRequest once
~~~

Do not maintain two separate finite coordinator implementations.

## Required properties of the internal representation

It must expose enough shared information for existing finite lifecycle code without duplicating the preservation tree:

- canonical request reference;
- native preservation reference when relevant;
- compact admission reference when operation == Compact;
- reservation/context/raw byte counters;
- routing facts;
- request/correlation IDs;
- incoming headers;
- client surface;
- original owned Bytes;
- operation.

Prefer accessor methods over cloning an AdmittedRequest just to satisfy shared code.

AttemptPreparation remains borrowed and synchronous. PreparedUpstreamAttempt remains the owned pre-await boundary.

## Wire behavior

Compact wire dispatch must remain exactly as established by Plan 229:

- native/no-model-rewrite sends the existing owned Bytes allocation;
- a required EggPool-owned model rewrite clones/mutates the single preserved Value and bounded-serializes once;
- unsupported compact targets fail before provider submission;
- no translated compaction fallback is introduced.

Ordinary generation behavior must remain unchanged.

## Measurement

Use an ephemeral deterministic compact fixture with at least:

- small history;
- approximately 1 MiB JSON history;
- a larger history practical under the configured request bound.

Measure peak RSS and/or allocator-observable allocation only if existing local tools make that practical. Do not add a production allocator, jemalloc dependency, or permanent benchmark crate.

Structural evidence is acceptable: after construction of the production compact input there must be one authoritative NativeRequestPreservation.parsed tree, not two.

## Tests

Add regression coverage proving:

- the public FiniteRequest::from_compact_admitted compatibility constructor still has its historical observable fields;
- the production execute_compact_finite path uses the internal single-owner compact representation;
- direct, provider-qualified, and virtual compact routing is unchanged;
- provider pin is unchanged;
- compact admission validation is unchanged;
- compact attempt preparation/wire bytes are unchanged;
- retry/finalization behavior remains shared with ordinary finite execution;
- no source-native content reaches diagnostics/persistence.

Do not add pointer-identity assertions for serde_json::Value. Test the internal ownership boundary through construction APIs plus behavioral parity.

## Focused qualification

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
~~~

Then run the shared Plan 230 closure gates.

## Stop conditions

Close this plan with no production change if any of the following is true:

1. eliminating the duplicate requires changing a public FiniteRequest, AdmittedRequest, CompactAdmittedRequest, or NativeRequestPreservation field type;
2. it requires two finite coordinators or duplicates retry/finalization logic;
3. it makes borrowed request state cross provider I/O await boundaries;
4. it weakens native preservation or compact capability checks;
5. the production path still needs two independent mutable parsed trees for a demonstrated semantic reason;
6. the measured/structural benefit is negligible compared with the additional lifecycle complexity.

Do not use Arc<Value> in public structures as a shortcut.

## Completion criteria

- [x] production compact execution owns one preserved parsed JSON tree;
- [x] the public FiniteRequest compatibility surface is unchanged;
- [x] ordinary finite execution remains unchanged;
- [x] Plan 229 native Bytes ownership remains true;
- [x] compact routing/retry/finalization/wire tests pass;

The production endpoint now transfers `CompactAdmittedRequest` to a private
`FiniteExecutionInput` admission enum. The public `FiniteRequest` fields and
constructors remain unchanged and still provide the historical compatibility
view, while production compact retries, publication, wire preparation, and
finalization share one finite loop without cloning the preserved JSON tree.

Focused evidence: `codex_compaction_compat`, `codex_responses_compat`,
`coordinator_c008`, `coordinator_c009`, `coordinator_c011`,
`coordinator_boundaries`, `coordinator_finalization`,
`coordinator_publication`, `wire_runtime`, and `wire_qualification` all pass
with serial tests. The ownership boundary is structural; no pointer-identity
assertion or public field-type change was introduced.

Implementation and validation commit: `30d2ba7`.
