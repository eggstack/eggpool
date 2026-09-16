# Plan 199: Responses remote-compaction compatibility

> **Status:** complete
>
> **Closed:** 2026-09-16 (implemented `94b710e6`; live qualification Plan 206;
> see Closure evidence below)
>
> **Baseline:** Plan 198 baseline plus preceding planning commits
>
> **Parent:** Plan 198
>
> **External audit baselines:** OpenAI Codex `4701aa4b4239c70063ab6f2fcb835324f9c109f4`; OpenCodex `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`
>
> **Priority:** P1 / forward compatibility. This is not a blocker for the current Eggpool custom-provider HTTP/SSE path because current Codex custom providers default to local compaction.
>
> **Scope:** add a bounded, explicit remote-compaction compatibility surface without weakening Eggpool's stateless Responses contract or bypassing routing/accounting/health ownership.

## Executive summary

Current Codex has two relevant remote-compaction generations:

- an older/historical endpoint form represented by `POST /v1/responses/compact` and still supported by OpenCodex for client compatibility;
- current Codex v2 provider capability `RemoteCompactionSupport::V2`, which uses `compaction_trigger` items over the normal Responses endpoint.

Current generic/custom Codex providers default to `RemoteCompactionSupport::Unsupported`, so Eggpool's existing generated provider configuration continues to use Codex's local compaction path. This plan therefore adds remote compaction as an opt-in/forward-compatible capability rather than changing the default provider contract prematurely.

The design must keep three invariants:

1. compaction is a distinct model-facing operation whose output replaces retained history; it is not an ordinary assistant completion;
2. the operation still participates in Eggpool's normal provider selection, failure isolation, health effects, quota/accounting, bounded-body rules, and cancellation ownership;
3. implementing compaction must **not** require Eggpool to persist conversations or accept general `previous_response_id` continuation.

---

# Research authority

At implementation time re-audit the exact current source, starting with:

OpenAI Codex:

- `codex-rs/model-provider/src/provider.rs` — `RemoteCompactionSupport` and provider capability defaults;
- `codex-rs/core/src/tasks/compact.rs` — local vs. remote decision;
- `codex-rs/core/src/compact_remote_v2.rs`;
- `codex-rs/core/src/compact_remote_v2_attempt.rs`;
- `codex-rs/core/src/compact_remote_history.rs`;
- `codex-rs/core/src/responses_metadata.rs`;
- relevant `core/tests` fixtures for request/result history replacement.

OpenCodex:

- current Responses router/compact implementation;
- current documentation describing v1 `/responses/compact` versus v2 `compaction_trigger` behavior.

Eggpool authority:

- `rust/src/server/mod.rs` and `rust/src/server/inference.rs` — public route/adaptor ownership;
- `rust/src/request/admission.rs`, `body.rs`, `limits.rs` — bounded request admission;
- `rust/src/coordinator/` — routing, attempt, retry, publication/finalization ownership;
- `rust/src/wire/` — native preservation and cross-surface codecs;
- `rust/src/providers/` — neutral HTTP transport;
- `rust/tests/codex_responses_compat.rs` — current Codex-derived wire conformance.

Do not implement from OpenCodex alone. Codex source/fixtures are the client contract; OpenCodex is a useful compatibility reference.

---

# Workstream 1 — Model compaction as an explicit semantic operation

Do not route a compact request through ordinary `responses()` code by changing only the URL. The response semantics differ: the result is replacement history/checkpoint material rather than a normal sampled assistant response.

Introduce the smallest explicit operation distinction that fits current architecture, for example conceptually:

```rust
enum InferenceOperation {
    Generate,
    Compact,
}
```

or a dedicated compact request type adjacent to the canonical request boundary.

The exact type location should follow the current request/coordinator ownership after implementation-time review. Avoid putting client-specific Codex names into the canonical routing domain.

The operation must carry only bounded semantic facts needed for routing/encoding, while source-native compact JSON is preserved separately when same-surface forwarding is legal.

### Required invariants

- body size/depth/collection limits apply before provider selection;
- model/alias resolution remains Eggpool-owned;
- provider/account eligibility remains ordinary routing policy;
- compaction must not open a second direct transport path;
- no retry is legal after a streaming/body handoff, consistent with normal coordinator rules;
- request/attempt/usage accounting must identify the operation without storing prompts or replacement history;
- failure classification still drives normal provider health effects where appropriate.

If the current DB schema needs an operation/category field for diagnostics, prefer an additive bounded enum/metadata field only if existing request records cannot represent the distinction safely. Do not add persistence merely to store compacted content.

---

# Workstream 2 — Support historical `POST /v1/responses/compact` as a distinct route

Add the route only after capturing current request/result fixtures.

Suggested topology:

```text
POST /v1/responses/compact
    -> thin server adapter
    -> bounded compact admission
    -> compact coordinator operation
    -> selected wire strategy
    -> bounded compact result
```

Likely source changes:

- `rust/src/server/mod.rs` — route registration;
- `rust/src/server/inference.rs` or a new small `rust/src/server/compaction.rs` if separation materially improves ownership;
- request/coordinator/wire modules as needed.

The server adapter must not perform provider selection, summarization, retry, or JSON shape reconstruction itself.

## 2.1 Native compact-capable upstream

When the selected upstream supports the same compact contract:

- preserve the source-native request exactly except for Eggpool-owned model alias rewrite and dispatch-time auth;
- preserve ordered history/reasoning/tool items and forward-compatible fields;
- validate/observe the result enough to classify success/failure and bound it, but do not canonicalize and rebuild a valid native result unnecessarily;
- retain normal timeout/cancellation/accounting behavior.

Do not assume every OpenAI-Responses-compatible provider supports `/responses/compact`; capability must be explicit.

## 2.2 Upstream without native compact support

Only add a fallback after exact Codex result semantics are captured.

A fallback may use the selected model/provider to create replacement history, but it must be a dedicated tool-free compaction request and must return the exact client compact result shape. Conceptually:

```text
compact history
   -> bounded canonical compact input
   -> selected upstream model
   -> compaction-only prompt/instructions
   -> validate bounded summary/checkpoint
   -> exact compact replacement-history output
```

This is not ordinary chat completion passthrough.

Requirements:

- disable/omit client tools for the summarization call;
- do not allow the model to invoke arbitrary user tools while compacting;
- preserve the facts Codex expects to survive replacement (including tool call/output relationships where required by the current contract);
- reject histories that cannot be represented safely rather than silently dropping semantically required items;
- hard-bound input history count/bytes/tokens and output summary size;
- use deterministic wrapper instructions owned by Eggpool, with tests that do not snapshot private user content;
- never persist compacted history in Eggpool solely for future continuation.

If exact replacement-history reconstruction cannot be made provider-neutral without high semantic risk, ship native forwarding first and leave the translated fallback explicitly unsupported. Correct rejection is preferable to a lossy compact result that breaks a long-running agent later.

---

# Workstream 3 — Handle current v2 `compaction_trigger` deliberately

Current Codex defines `RemoteCompactionSupport::V2` as compaction-trigger items over the normal Responses endpoint.

Before adding support, record exact request and streaming/result fixtures from the current Codex commit.

## Native Responses target

The existing source-native preservation path should preserve an unknown/current `compaction_trigger` item if admission allows it and the selected native surface advertises v2 remote compaction.

Do not expand the canonical IR merely to preserve a source-native item.

## Translated target

Do not silently pass a trigger through codecs that cannot represent its semantics.

Choose one of:

1. explicit translated v2 implementation that invokes the same compact semantic operation as Workstream 2 and emits the exact Responses lifecycle Codex expects; or
2. pre-dispatch `UnsupportedSemanticFeature` until that mapping is proven.

The implementation must never treat `compaction_trigger` as user text.

## Capability advertisement

Do **not** tell Codex that the Eggpool provider supports remote v2 compaction until both request and result paths are qualified end to end.

Current custom-provider default `Unsupported` is safe and should remain the advertised behavior until this plan closes.

---

# Workstream 4 — Keep stateful continuation out of scope

Do not change these existing admission decisions merely to implement compact:

- `previous_response_id` remains rejected;
- `store = true` remains rejected;
- conversation references remain rejected;
- `background = true` remains rejected.

Compaction receives the history/checkpoint input required for that operation and returns replacement material in the same request/response transaction.

A later WebSocket/stateful-continuation plan may introduce a bounded response-state cache if current client behavior requires it. That work must define TTL, memory ceilings, generation ownership, restart behavior, and missing-state error contracts independently.

---

# Workstream 5 — Capability plumbing

Add reusable wire/profile capability facts rather than a Codex flag.

Examples, adjusted to the current profile schema:

```text
supports_remote_compaction_v1
supports_remote_compaction_v2
```

or a small enum if mutually exclusive/versioned forms are clearer.

Capability sources:

- bundled provider profile facts for known providers;
- explicit operator configuration where necessary;
- never model-name guessing.

Routing must filter out a target before submission when the requested compact operation cannot be satisfied by either a native profile or a qualified fallback.

Aliases/selectors advertise remote compaction only when the route guarantees it across candidates or the router can make a capability-aware eligible selection before account claim.

---

# Workstream 6 — Accounting, health, and diagnostics

Compaction consumes upstream resources and can fail independently from normal generation.

Reuse current request/attempt/finalization boundaries so:

- successful compaction records provider/model/account usage when upstream usage is available;
- rate limits/auth failures affect health through the same narrow health effect rules;
- retry legality remains bounded by normal submission budget;
- compact failures are distinguishable in safe diagnostics without storing request history;
- metrics can count compact operations separately if the current metrics contract can do so without a schema explosion.

Do not treat a semantic compact-result validation error as a successful provider response.

Do not leak summary text/replacement history into events, logs, traces, or error strings.

---

# Workstream 7 — Deterministic conformance fixtures

Extend `rust/tests/codex_responses_compat.rs` or add one focused compact test target if keeping it separate improves test readability.

Pin fixture provenance to the audited Codex commit.

At minimum cover:

1. compact request with ordinary user/assistant messages;
2. history containing function call + output;
3. history containing custom/freeform call + output;
4. reasoning items required by current Codex compact contract;
5. alias model rewrite on native forwarding;
6. unknown benign native extension preservation;
7. native compact success;
8. native compact structured failure;
9. translated fallback success, if implemented;
10. translated fallback rejects unrepresentable required semantics;
11. request/output size bounds;
12. timeout and cancellation;
13. no tools forwarded in a summarization fallback;
14. no persisted response/history state created;
15. current v2 `compaction_trigger` native preservation or explicit rejection according to shipped capability.

If the result is streaming in the implementation-time Codex contract, also test strict terminal evidence and EOF behavior using the same streaming ownership rules as normal Responses.

---

# Workstream 8 — Live qualification

Extend `scripts/smoke_codex_compat.sh` only after deterministic fixtures are green.

Because current custom providers use local compaction, do not require an impractically huge live context just to prove basic Codex compatibility.

For remote compact qualification, prefer one of:

- a test-only Codex configuration/capability that deliberately exercises remote compaction;
- a deterministic local fixture server that accepts the compact contract;
- a documented optional manual scenario if current Codex does not expose a stable custom-provider switch.

Record the Codex version/commit or release qualified.

Do not make network/credential-dependent remote-compaction qualification mandatory for ordinary CI.

---

# Documentation changes

Update after implementation:

- `docs/stateless-responses.md` — explain that compaction is a stateless operation and does not imply stored Responses;
- `docs/agent-configuration.md` — state whether Eggpool advertises remote compaction or relies on Codex local compaction;
- `docs/api-reference.md` — document `/v1/responses/compact` only if shipped;
- `architecture/deep-dive-request-lifecycle.md` / transcoder deep dive if a new operation boundary is introduced;
- `docs/codex-compatibility-smoke.md` — qualification coverage.

Do not imply remote compaction is required for normal current Codex operation if the generated provider still advertises it unsupported.

---

# Verification

Focused checks should include the current Responses/codex suites plus any new compact target:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
```

Add the appropriate C008/C009/C011 focused targets if the compact path changes streaming handoff, retry, or terminal classification.

Then run the full serial workspace suite and locked release build.

---

# Acceptance criteria

1. Current default Eggpool Codex configuration remains valid when remote compaction is unsupported; Codex local compaction is not regressed.
2. If `/v1/responses/compact` ships, it is a bounded distinct operation, not an ordinary Responses alias.
3. Native compact-capable providers can receive source-native compact requests with only Eggpool-owned model/auth changes.
4. A translated fallback, if shipped, produces exact current-Codex replacement-history semantics and never forwards client tools during summarization.
5. If exact translated semantics are not proven, unsupported targets fail before upstream submission.
6. v2 `compaction_trigger` is either qualified or explicitly unsupported; it is never silently converted to text.
7. Eggpool does not advertise v2 remote compaction before the complete path is qualified.
8. `previous_response_id`, stored conversations, and background Responses remain outside the default stateless contract.
9. Normal routing, retry, health, usage, cancellation, and finalization ownership applies to compact operations.
10. No prompt, replacement history, compact summary, credential, or raw upstream body is persisted/logged by the new path.
11. Deterministic fixtures are pinned to a current Codex source baseline.
12. No Codex/OpenCodex runtime dependency is added.

---

## Closure evidence

- Implemented: `94b710e6d1b7fc82cb4c3ff5765441e05a008b33` — bounded
  finite-only `POST /v1/responses/compact`, native compact-capable Responses
  routes only, no translated summarization fallback, no persisted
  conversation/response state, v2 trigger capability-gated/native-only.
- Focused tests: `codex_compaction_compat` (+ `wire_*`, coordinator
  boundary/finalization/publication suites); full serial workspace suite green.
- Live qualification (Plan 206, Codex CLI `0.154.0`): custom provider path
  uses local compaction as expected; remote compaction remains an optional
  provider capability (`supports_remote_compaction_v1` + template). No live
  remote-compact upstream exercised (none configured); deterministic fixtures
  remain the authority. No state added for testing.
- Intentional deferrals: translated compaction fallback, WebSocket/continuation
  state — unchanged.
