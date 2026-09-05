# W009 — Selected-Profile Codec Runtime Boundary

Status: dependency-ready; W008 closure accepted

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w009--selected-profile-codec-runtime-boundary`

Primary class: capability/invariant

Hard dependency: W008 accepted closure.

## 1. Objective

Compose W002-W008 behind the smallest practical Rust runtime facade M7 can consume. The caller supplies an already-selected concrete wire profile and secret-free provider adaptation facts; M6 performs deterministic request/response/stream transformation and returns typed semantic evidence. This is integration, not a coordinator preview.

## 2. Runtime facade

Provide cohesive operations for client admission to canonical request, canonical request preparation for a supplied upstream profile, finite upstream decoding and client encoding, stream adapter creation/push/finalize, usage/terminal evidence, and pure M5 fact/affinity adapters.

## 3. Explicit selected profile

Upstream profile is a mandatory caller input. W009 may validate compatibility but may not call a dynamic resolver or choose another profile based on runtime history. Multiple static possibilities remain M7's concern.

## 4. Adaptation context

Use one bounded secret-free context containing only static facts M6 needs: provider ID/kind where body behavior differs, client/upstream profile IDs, selected canonical/upstream model identity, capability/loss policy inputs, request surface, and static profile flags. Exclude API keys, proxy credentials, HTTP clients, durable attempt IDs, health/retry state.

## 5. Prepared request and finite response results

Prepared requests return bounded encoded body/value, semantic content metadata, profile/model identity, adaptation outcome/warnings, byte facts, and stream intent. Finite response results separate canonical success, valid provider-error evidence, malformed codec response, usage, warnings, and optional client-encoded body. No retryability/health classification.

## 6. Streaming runtime

Expose per-attempt bounded stream state from W008, independent of sockets and downstream ownership. M7 supplies bytes and decides when/how to write outputs.

## 7. Error layering

Keep client admission/local validation, adaptation/loss rejection, profile/config mismatch, malformed upstream response, explicit provider error, and incomplete/malformed terminal evidence as distinct typed layers. Do not collapse to generic strings or final HTTP/retry outcomes.

## 8. Model-router selector readiness

Prove that the future M7 semantic selector's small request/response can pass through normal canonical/profile operations when M7 supplies a profile. W009 must not invoke the selector or recursively call a coordinator.

## 9. Ownership/concurrency

Registry/profile data should be immutable/shareable. Per-request/stream state is independent and future-thread-safe enough for M8. No mutable global codec state, learned preference, or async `Drop` cleanup.

## 10. Observability/redaction

Expose only profile IDs, transformation/warning reason codes, byte counts, usage status, and terminal category. Do not expose raw prompts/media/tool schemas/provider bodies, sessions, credentials, or proxy data by default.

## 11. Required integration tests

Cover request preparation to every profile, finite upstream-to-client conversions, selected-profile mismatch, W006 warnings/rejections, W007 bounds, W008 adversarial streaming, provider-error vs malformed distinction, terminal/usage preservation, pure M5 bridge no-mutation, selector-style payload support, concurrent independent instances, and secret/raw-content-free diagnostics.

## 12. Verification

Run all Rust targets, all M6 migration observations, targeted Python request/wire/transcoder/SSE suites, M5 routing regression, format/lint/type checks, and `git diff --check`.

## 13. Acceptance criteria

W009 closes only if M7 can use one stable facade for finite/stream transformations, profile selection is caller-owned, typed evidence is sufficient for M7 without reparsing raw bodies, no DB/network/retry/finalization behavior exists, state is bounded, and W010 can qualify M6 without reaching into codec internals.

## 14. Stop conditions

Do not close if the facade chooses/retries profiles, needs an API key/HTTP client, reparses provider bodies in multiple layers, owns downstream handoff, invokes model-router inference, or introduces mutable global state.

## 15. Closure evidence

Create `migration-rs/closure/canonical-wire/009-status.md` with facade ownership/API summary, integration matrix, error-layer evidence, verification, and registry transition promoting W010.
