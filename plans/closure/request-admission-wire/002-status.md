# Request Admission and Wire Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/002-wire-kernel-extraction-seam-and-contract-freeze.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-002--wire-kernel-extraction-seam-and-contract-freeze`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Repository baseline reviewed: `61470ef788e287c49b4a51062eaba439049d46dc`

Implementation commits or pull requests:

- `ca3d16b3` — Implement request-admission-wire M002 extraction seam

## 1. Executive finding

M002 makes the wire implementation mechanically extractable without moving it
yet. The pure kernel (`wire::{ir, adaptation neutral, codec, codecs,
additional_codecs, decode, registry neutral, stream}`) no longer imports
EggPool routing, catalog, config, request-runtime, model-router, provider,
database, server, coordinator, Tokio, Axum, Hyper, TLS, or transport types.
All EggPool conversions live in `wire::adapters` plus thin
`request::admission` wrappers with identical acceptance. The deterministic
`wire_extraction_contract` corpus (14 tests) and `wire_kernel_boundary` guard
(2 tests) freeze pre/post-extraction semantics. No public wire capability,
request limit, loss policy, native preservation behavior, terminal rule,
config/storage schema, or provider surface changed. M003 is unblocked.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Canonical/routing separation; identical ThinkingRequirement facts | `ir::ReasoningIntent::to_thinking_facts` + `wire::adapters::thinking_requirement_from_intent` + `request::thinking_requirement_from_intent` delegate; `contract_thinking_facts_match_routing_adapter` | pass | Removed `to_thinking_requirement` from `ir.rs`; single caller `routing_request_facts_from_parts` uses adapter |
| Capability-policy separation; preserve statuses + default policy | `NeutralCapabilityStatus/NeutralThinkingCapability`, `reasoning_capability_notices_neutral` kernel + adapters wrapper; `contract_neutral_capability_mapping_preserves_statuses` | pass | Mixed/unknown/unsupported distinctions preserved; default Reject/Reject/WarnDrop unchanged |
| Request-decoder separation; one-parse + stateless policy retained | `wire::decode` (`DecodeLimits::current`, `DecodeError`, structural decoder moved from admission); `admission::{canonical_request_from_value, canonical_request_from_object}` enforce stateless first then delegate with 1:1 error map | pass | `canonical_request`, Codex, admission, codecs, multimodal outcomes + error precedence unchanged |
| Native preservation separation; byte/code equivalent | `NativeSummaryFacts` kernel + `native_summary_notices`; adapters `neutral_native_summary`/`native_preservation_notices`; `contract_native_preservation_decisions_are_stable` | pass | Request layer still owns parsed Value lifetime; blocker/notice behavior identical |
| Registry/config separation | Neutral `WireProfileRegistry` (no `crate::config`); adapters `SurfaceConfigFacts`, `configured_profiles_from_facts`, `validate_provider_references_neutral`, `compaction_capabilities_from_surface_config`; callers updated in `endpoints.rs`, `config.rs`, `finite.rs`, `attempt.rs` | pass | `wire_profiles`, reload/config, runtime profile selection unchanged |
| Explicit decode limits equal current constants | `DecodeLimits::current()`; `contract_decode_limits_equal_current_constants`; `request::limits` re-exports kernel media constants and delegates validators | pass | No broadened/tightened acceptance |
| Contract corpus: pairings, exact/adapted/rejected, ordered codes | `wire_extraction_contract`: native pairings exact, all client/upstream compatibility paths, ordered adaptation codes, warn/reject agreement | pass | 14/14 pass |
| Contract corpus: finite conversion, usage/errors, stable IDs, tools | Same corpus: usage counters, error-envelope evidence, deterministic `stable_tool_call_id`, function/freeform/deferred-search wrap notices | pass | — |
| Contract corpus: multimodal, presence, native preservation | Same corpus: presence Missing/Null/Value distinct; native decisions stable | pass | — |
| Contract corpus: SSE splits incl. UTF-8, terminal, native observation | Same corpus: every-split agreement incl. multi-byte char; terminal boundary (EOF-before/partial distinct from success) | pass | Within existing decoder framing model |
| Dependency-boundary guard | `wire_kernel_boundary`: scans `use` lines of 8 kernel files for 24 forbidden owners | pass | 2/2 pass; doc prose excluded |
| Existing focused wire/Codex integration | canonical_request 12, wire_adaptation 6, wire_codecs 11, wire_multimodal 7, wire_profiles 9, wire_qualification 16, wire_runtime 8, wire_stream 18, codex_responses_compat 15, codex_compaction_compat 13; coordinator boundaries 5, finalization 10, publication 6, C008 29, C009 13, C011 17 | pass | All green serial |
| Docs | `architecture/deep-dive-transcoder.md` seam paragraph; `deep-dive-request-lifecycle.md` decode ownership note | pass | Long-term spec unchanged; `ir.rs` remains canonical facade |

## 3. Production implementation evidence

Landed ownership (no source move yet, per plan):

- `rust/src/wire/ir.rs`: removed routing conversion; added neutral
  `ThinkingFacts` + `to_thinking_facts()`.
- `rust/src/wire/adaptation.rs`: removed `crate::catalog`/`crate::request`
  imports; added neutral `NeutralCapabilityStatus`,
  `NeutralThinkingCapability`, `NativeSummaryFacts`,
  `native_summary_notices()`, `reasoning_capability_notices_neutral()`.
- `rust/src/wire/adapters.rs` (new, EggPool-owned): catalog/request/routing/
  config adapters preserving pre-extraction signatures
  (`reasoning_capability_notices`, `native_preservation_notices`,
  `thinking_requirement_from_intent`, `configured_profiles`,
  `validate_provider_references`,
  `compaction_capabilities_from_surface_config`) plus neutral
  `configured_profiles_from_facts`/`validate_provider_references_neutral`.
- `rust/src/wire/decode.rs` (new, kernel): `DecodeLimits::current()`,
  `DecodeError`, media validators, and the full limit-parameterized
  structural decoder moved from admission (stateless enforcement removed;
  EggPool wrapper enforces it first).
- `rust/src/wire/codecs.rs`, `additional_codecs.rs`: consume only
  `wire::decode` with `DecodeLimits::current()`; `AdmissionError` mapping
  replaced by 1:1 `DecodeError` mapping.
- `rust/src/wire/registry.rs`: neutral only; config constructors moved to
  adapters. `rust/src/wire/mod.rs`: kernel re-exports + adapter re-exports
  preserving `crate::wire::{reasoning_capability_notices,
  native_preservation_notices, configured_profiles, ...}` paths; new
  `decode` re-exports.
- `rust/src/request/admission.rs`: `canonical_request_from_value/object`
  enforce stateless policy then delegate to kernel; 41 dead structural
  helpers deleted; `thinking_requirement_from_intent` delegates to adapters.
- `rust/src/request/limits.rs`: media constants re-exported from kernel;
  validators delegate to kernel with `LimitError` mapping; token estimates
  stay EggPool-owned.
- Callers updated: `coordinator/endpoints.rs` (`configured_profiles`),
  `config.rs` (`validate_provider_references`), `coordinator/finite.rs` +
  `attempt.rs` (`compaction_capabilities_from_surface_config`),
  `wire/runtime.rs` unchanged path via facade, `tests/wire_profiles.rs`.
- New tests: `rust/tests/wire_extraction_contract.rs` (14),
  `rust/tests/wire_kernel_boundary.rs` (2).

Planned-but-absent (by design): no workspace crate, no file move, no
publication, no provenance/fidelity API (M003/M004).

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Focused targets (serial) also run during development; see matrix.

### Results

Local (all serial `--test-threads=1`):

- `cargo fmt --check` — pass (after one `cargo fmt` normalization).
- `cargo clippy --workspace --all-targets -- -D warnings` — pass (after
  fixing one `needless_borrow` in `config.rs`).
- `cargo check --workspace --all-targets --no-default-features` — pass.
- `cargo clippy --workspace --all-targets --no-default-features` — pass.
- Focused wire/Codex/contract/guard/coordinator suites — all pass with
  counts in §2 (includes new 14 + 2).
- Full default workspace suite — 745 passed, 0 failed.
- Full `--no-default-features` workspace suite — 746 passed, 0 failed.
- `cargo deny check` — advisories/bans/licenses/sources ok.
- `cargo tree -e features` / `--duplicates` — ran; no new workspace member;
  expected duplicate `webpki-roots`/`universal-hash` entries only; dependency
  graph unchanged (no `Cargo.toml`/`Cargo.lock` change).
- No Python tooling change; `uv`/Ruff/Pyright/pytest not re-run (no
  `scripts/`/`tests/tooling/` change). CI is the remaining truth for hosted
  lanes; no CI run claimed here.

## 5. Invariant review

- `wire/ir.rs` remains the EggPool canonical boundary (facade path kept;
  re-export location unchanged for callers). Evidence: `mod.rs` re-exports;
  all canonical construction flows through kernel types with type identity
  (no JSON bridge).
- Codecs build from canonical semantics; no translated-payload chaining:
  `encode_*` inputs are `CanonicalRequest/Response`; no codec output feeds
  another provider grammar. Evidence: unchanged encode paths + contract
  cross-surface notice tests.
- All six surfaces unchanged: finite/streaming contract + existing
  wire/Codex/coordinator suites green.
- Native Responses preservation + streaming observation unchanged:
  preservation summary mapping 1:1; `native_summary_notices` same blocker/
  notice order; stream decoder untouched (`stream.rs` has zero EggPool
  imports both before and after).
- Transport EOF never success: stream decoder untouched; contract asserts
  partial/EOF-before distinct from terminal success.
- Adaptation codes, `CodecReasonCode`, `LossPolicy::Warn/Reject`, max
  notices stable: no enum/string change; warn/reject agreement test passes.
- `Presence` Missing/Null/Value distinct: untouched type + new contract
  assertions.
- No fabricated signatures/encrypted reasoning/metadata: no codec change
  beyond import source; no-fabrication paths untouched.
- Exact media/depth/count/size acceptance: `DecodeLimits::current()` equals
  pre-extraction constants (contract asserts); `request::limits` delegates.
- Admission/token/routing/catalog/config/compaction/retry/transport/
  publication/finalization remain EggPool-owned: only adapters convert;
  `wire/runtime.rs` ownership untouched.
- Secret-free diagnostics intact: no new logging; Debug impls unchanged and
  redaction-safe; contract adds no prompts/credentials.
- `--no-default-features` parity: full no-default suite green (746 passed).

## 6. Failure and recovery review

Pure-boundary refactor; no new global state, cache, worker, lock, async
task, or waiter. Failed decode/adaptation still fails before upstream
submission at the same logical boundary with the same typed reason/field
(1:1 error maps in codecs + admission). Native stream observation retains
the existing bounded incremental parser and terminal summary. No additional
request content retained beyond the admitted lifetime; `NativePreservation`
lifetime unchanged. Cancellation/generation/retry/publication lifetimes
untouched; coordinator boundary/finalization/publication suites green.

## 7. Migration and compatibility review

No external migration. Module paths/re-exports stable (`crate::wire::*`
facade preserved; `crate::request::*` wrappers preserved). No
persisted/config schema change. Compatibility preserved and asserted:

- successful JSON meaning + native-preserved fields (contract + Codex suites);
- adaptation notice ordering/codes (contract ordered-codes test);
- typed rejection categories (1:1 error maps);
- stream grammar + terminal classification (decoder untouched + contract);
- current-tool wrapper semantics + stable IDs (determinism test);
- public HTTP behavior (coordinator boundary/C008/C009/C011 suites).

Rollback: revert `ca3d16b3` restores pre-seam layout; no lockfile change to
unwind.

## 8. Security review

Auth enforcement untouched (admission stays below auth). No secret handling
change; no new persistence/logging of prompts, raw bodies, credentials,
encrypted reasoning, or tool arguments. `NativeSummaryFacts` carries only
counts/field names/truncation flags (same as before). DoS bounds unchanged:
same depth/collection/media/notice ceilings via `DecodeLimits::current()`.
Secret-free: guard + contract add no secret material; fixtures reuse existing
shape-only data.

## 9. Documentation and operations

- `architecture/deep-dive-transcoder.md`: seam paragraph (kernel list,
  forbidden owners, adapters/runtime/admission ownership, decode limits,
  contract corpus).
- `architecture/deep-dive-request-lifecycle.md`: decode ownership note.
- `plans/000-long-term-specification.md`: unchanged, per plan.
- No standalone-crate docs (explicitly out of scope for M002).
- No ops change: no new config, metric, dashboard, or runbook surface.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | `is_client_tool_search_declaration` exists in both `wire::decode` (structural) and `request::admission` (preservation summary) | Bounded 15-line predicate duplicated across the seam | M003/M004 to unify under the provenance vocabulary; no behavior risk (both covered by tests) |
| low | `MediaLimitError` (kernel) vs `LimitError` (EggPool) are parallel enums with a mapping shim | Two error types for the same media bounds | M003 to decide whether the crate exposes one error or keeps the EggPool mapping; no acceptance change |

No medium-or-higher findings. No public capability, limit, policy,
preservation, terminal, schema, or surface change.

## 11. Roadmap disposition

Milestone closed; hard dependency for M003 satisfied. M003
(`003-sans-io-wire-kernel-extraction-and-eggpool-cutover.md`) may proceed
against the qualified seam and contract corpus — promote from `blocked` to
`ready` in the same commit. M004 remains blocked on M003 closure. No
provider-transport blocked work is promoted.

## 12. Registry updates

Changes applied in the same commit:

- `plans/implementation/request-admission-wire/002-...md`: `ready` → `closed`.
- `plans/registry.md`: M002 ready → closed; M003 blocked → ready; unblock
  audit notes M002 closure unblocking M003.
- `plans/subsystems/request-admission-wire-roadmap.md`: M002 ready → closed;
  M003 blocked → ready.
