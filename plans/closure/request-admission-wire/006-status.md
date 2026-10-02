# Request Admission and Wire Milestone 006 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/006-codegg-external-wire-consumer-contract.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-006--external-semantic-producer-consumer-contract`

Repository baseline reviewed: `597d522dd2b7f1281c5a793ad8a107547360ea79`

Implementation commits or pull requests:

- `8192678f` — activate M006 and register its implementation handoff.
- `f05b18b7358d9a4125d1e20c491151eec265e403` — implement the external semantic-producer contract and complete the required verification.

## 1. Executive finding

M006 is complete. `eggpool-wire` now accepts a canonical request with no
fabricated client-wire origin, exposes a built-in surface encoder with closed
protocol options, and reconstructs completed tool calls from bounded
canonical stream events. EggPool continues to own admission, runtime profile
selection, routing, native Responses preservation, and transport. The
standalone consumer fixture compiles using only the public `eggpool-wire` API,
and the full default workspace suite plus required no-default and release
checks pass. No medium-or-higher finding remains.

The downstream CodeGG integration can pin immutable revision
`f05b18b7358d9a4125d1e20c491151eec265e403`. This milestone makes no new crate
release or semver-stability promise. The repository confirms that
`eggpool-wire 0.1.0` is already available on crates.io.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Construct canonical semantics without a fake source surface | `CanonicalRequest::from_canonical`; crate tests assert `RequestOrigin::Canonical` maps to no source | pass | Wire decoders retain `RequestOrigin::ClientWire` for Chat, Responses, and Messages |
| Keep notices, errors, and fidelity source identity truthful | adaptation tests cover source-free canonical notices/errors and preserve each decoded source; `plan_canonical_request_translation` derives optional source from origin | pass | Responses-only request behavior is keyed to actual Responses origin |
| Encode for every built-in target without runtime path/priority metadata | `encode_request_for_surface`; crate test encodes canonical requests for all five `WireSurface` values | pass | Uses the closed embedded registry and existing codec implementation |
| Make OpenAI Chat streaming usage explicit and opt-in | crate tests cover defaults, non-stream request, explicit stream opt-in, and ignored option on other surfaces | pass | Default fixtures remain unchanged |
| Reconstruct bounded completed calls from canonical stream events | `tool_calls.rs` tests cover interleaving, index fallback, stop/content completion, response completion, malformed identities, conflicts, limits, provider error, incomplete response, EOF, and post-terminal data | pass | Five actual adapters are replayed at every two-chunk byte split |
| Keep data out of diagnostics and bound retained state | redacted `Debug` implementations; limits are 128 active calls, 1 MiB per call, and 4 MiB aggregate; errors clear state | pass | No global state or second SSE parser |
| Keep the external package boundary source-isolated and sans-I/O | `consumer-fixture` compiles with only a path dependency on `eggpool-wire`; boundary and extraction tests pass; package listing excludes the fixture | pass | Direct dependency set remains Serde, Serde JSON, SHA-2, Thiserror, and TOML |
| Preserve EggPool request/wire behavior | 12 focused integration targets and the full serial workspace suite pass | pass | No public HTTP, persistence, routing policy, or provider transport change |
| Reconcile publication and consumer documentation | crate rustdoc and `architecture/deep-dive-transcoder.md`; `cargo info eggpool-wire` reports crates.io 0.1.0 | pass | No version bump or publication action in M006 |

## 3. Production implementation evidence

- `rust/crates/eggpool-wire/src/ir.rs` adds `RequestOrigin::{Canonical,
  ClientWire}` and the semantic constructor. Structural decoders stamp their
  exact source surface.
- `adaptation.rs`, `additional_codecs.rs`, `codecs.rs`, and `fidelity.rs` use
  optional source identity. `plan_canonical_request_translation` derives its
  source from the request; the existing explicit-source planner remains
  available for compatibility.
- `codec.rs` adds closed `RequestEncodeOptions`; `codecs.rs` adds the OpenAI
  Chat `stream_options.include_usage` opt-in. `lib.rs` exports
  `encode_request_for_surface` and documents the semantic-producer flow.
- `tool_calls.rs` adds a synchronous bounded accumulator over canonical events
  with typed failures and redacted diagnostics. `rust/src/wire/tool_calls.rs`
  is a facade; the kernel boundary test scans it and the extracted module.
- EggPool admission/coordinator joins read decoded client origin explicitly.
  Same-surface native Responses behavior remains gated by a real Responses
  origin.
- `consumer-fixture/` is a compile-only sibling package; Cargo package listing
  confirms it is excluded from the published crate archive.
- `architecture/deep-dive-transcoder.md` and crate rustdoc now state the
  published 0.1.0 status and the immutable Git pin contract.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo test --manifest-path rust/crates/eggpool-wire/Cargo.toml
rtk cargo check --manifest-path rust/crates/eggpool-wire/consumer-fixture/Cargo.toml
rtk cargo tree --manifest-path rust/crates/eggpool-wire/Cargo.toml -e features
rtk cargo test --manifest-path rust/Cargo.toml --test wire_extraction_contract -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_kernel_boundary -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_multimodal -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo deny --manifest-path rust/Cargo.toml check
rtk cargo build --manifest-path rust/Cargo.toml --locked --release
rtk cargo package --manifest-path rust/crates/eggpool-wire/Cargo.toml --list --allow-dirty
rtk cargo info eggpool-wire
```

### Results

- Formatting passed.
- `eggpool-wire` package tests: 43 passed; isolated consumer check passed.
- Focused EggPool targets passed: extraction 15, kernel boundary 4, codecs 11,
  stream 18, runtime 8, qualification 16, adaptation 6, profiles 9,
  multimodal 7, canonical request 12, Responses compatibility 15, and compact
  Responses compatibility 13.
- Default workspace Clippy passed; serial workspace suite passed 810 tests in
  65 suites.
- No-default workspace check and strict Clippy passed.
- `cargo deny` passed advisory, bans, licenses, and sources. It printed the
  repository's existing duplicate-version warnings; no new dependency was
  added and the guard exited successfully.
- Locked release build passed. Package listing contained `src/tool_calls.rs`
  and did not contain `consumer-fixture`.
- `cargo info` confirmed the existing crates.io `eggpool-wire 0.1.0` artifact.

## 5. Invariant review

- The extracted crate remains synchronous and sans-I/O; no runtime, network,
  credential, database, filesystem, clock, or logging dependency was added.
- EggPool remains the continuously qualified consumer and the single codec
  implementation. Root modules remain facades.
- Canonical origin is provenance only. It is not target selection, provider
  routing, or an application identity.
- Cross-surface encoders continue constructing fresh payloads from canonical
  intent. Native Responses preservation still belongs to EggPool and requires
  a decoded Responses origin.
- Default encode options emit no `stream_options` field; existing wire and
  compatibility suites pass.
- Tool-call retained data is bounded, state is per accumulator, and terminal
  completion is explicit. No full stream buffering or ID/name fabrication is
  introduced.

## 6. Failure and recovery review

The kernel owns no asynchronous work or cancellation/retry state. Dropping an
accumulator releases retained memory. Malformed identity, duplicate/conflicting
identity, missing name, per-call/aggregate overflow, provider error, incomplete
response, and EOF produce distinct typed outcomes. Any accumulator error
clears retained state; response completion closes active calls, while EOF
without a response terminal is not success. Data after terminal completion is
rejected.

## 7. Migration and compatibility review

No database, configuration, endpoint, or public HTTP migration is involved.
EggPool's internal joins were migrated to `RequestOrigin`; focused contracts
and all 810 workspace tests pass. The public `CanonicalRequest` origin field
replaces the former mandatory client surface. This is an intentional 0.1
consumer API change; the downstream integration should use the new constructor
and pin the recorded Git revision. M006 does not change crates.io contents or
promise semver-1.0 stability.

## 8. Security review

The accumulator has explicit active-call, per-call argument, and aggregate
argument ceilings. It does not parse tool JSON, authorize tool calls, execute
tools, or log argument contents. Accumulator and completed-call `Debug`
implementations redact IDs, names, and arguments. Codec errors and notices
retain structural fields and optional source/target identities only.

## 9. Documentation and operations

Updated crate rustdoc with the no-I/O semantic-producer example and API
contract, `architecture/deep-dive-transcoder.md` with the sibling-consumer
seam and publication truth, the request-wire roadmap, the active registry,
and this closure record. The boundary guard now scans the new module and
checks its facade.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M006 correctness, security, migration, or operational finding | none | none |

The existing duplicate-version warnings from `cargo deny` are unchanged
repository dependency-policy output and did not fail the guard.

## 11. Roadmap disposition

Milestone closed; the request-admission-wire roadmap has no registered
successor and returns to closed status. The blocked-work audit found no
in-repository plan with a dependency on M006. The previously blocked external
CodeGG adoption now has its required consumer API and immutable pin, so it may
proceed in the downstream repository. No downstream plan is registered in
this repository to receive a status edit. Provider-transport M002 and the
dashboard dependency chain remain blocked by their independent prerequisites.

## 12. Registry updates

`plans/registry.md` and the source roadmap mark M006 closed, remove the request
wire stream from active implementation/roadmap tables, add the closure to
recently closed, and record the CodeGG unblock audit. No unrelated blocked
plan was promoted.
