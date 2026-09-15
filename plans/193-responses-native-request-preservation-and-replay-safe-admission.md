# Plan 193: Responses native request preservation and replay-safe admission

> **Status:** IMPLEMENTED
>
> **Baseline:** Eggpool `main` at `51d770e16598cdea76ed56670f68b885f7f68ed5`
>
> **Parent:** Plan 192
>
> **Scope:** preserve current/future Responses request semantics across Eggpool's admission and native routing path, including Codex multi-turn history and model-alias rewriting, without turning the canonical IR into an OpenAI schema mirror. Make cross-surface loss explicit and bounded.

## Problem statement

Eggpool currently has a native request passthrough optimization, but a current Codex request can fail or lose semantics before that optimization is useful.

`rust/src/request/admission.rs` constructs `CanonicalRequest` from the source JSON and currently rejects typed Responses input items other than:

```text
message
function_call
function_call_output
```

Current Codex history can contain additional API-visible Responses items. At the audited Codex baseline these include, depending on tools/features used:

```text
reasoning
custom_tool_call
custom_tool_call_output
local_shell_call
tool_search_call
tool_search_output
web_search_call
image_generation_call
compaction/context-compaction forms
```

The exact list is expected to evolve.

Separately, `decode_tools()` currently interprets any tool object as a generic `CanonicalTool`. This loses the distinction between Responses function tools and Responses-native `custom`, `namespace`, `tool_search`, or `web_search` definitions. Some objects do not even satisfy the generic function assumptions.

Finally, `WireRuntime::prepare_admitted_request()` only forwards raw request bytes when the upstream model ID is identical to the client model ID. Eggpool's normal aliasing behavior therefore disables native passthrough and causes `OpenAiResponsesCodec::encode_request()` to rebuild the request from canonical semantics. `encode_responses_request()` cannot reproduce current Codex's entire input/tool/control envelope.

The correct fix is not to add every current Responses item to `CanonicalMessage`. Eggpool needs a source-native preservation channel at the request boundary.

---

# Desired architecture

Admission should produce two conceptually separate products:

```text
source JSON
  |-- canonical semantic projection --> routing/adaptation IR
  `-- native-preservation envelope ---> native same-surface forwarding
```

The canonical projection remains the representation used for:

- routing facts;
- model/capability selection;
- affinity inputs;
- token/resource accounting;
- genuine cross-surface translation.

The preservation envelope exists so a same-surface destination can receive valid source-native fields/items Eggpool does not need to understand.

Do not place raw arbitrary Responses JSON in `CanonicalMessage` or `CanonicalContentBlock`.

A suitable implementation could be named `SourceRequestEnvelope`, `NativeRequestPreservation`, or similar. The exact type name is not important; ownership is.

## Suggested shape

Prefer a bounded request-level structure associated with `AdmittedRequest`, for example conceptually:

```rust
struct NativeRequestPreservation {
    source_surface: ClientSurface,
    parsed: serde_json::Value,
    native_only_features: NativeFeatureSummary,
}
```

`NativeFeatureSummary` should be small metadata, not a second copy of the request. It can track facts such as:

- unknown/unprojected input item present;
- native-only tool definition present;
- native-only top-level control present;
- stateful feature requested;
- exact reason a cross-surface conversion must fail rather than silently omit semantics.

Retaining the parsed source `Value` for Responses requests is acceptable if it remains within the existing bounded request lifetime and avoids reparsing. If memory measurements show that retaining it for all protocols is wasteful, retain it only for surfaces/profiles that need native preservation. Do not introduce a second JSON parse in `prepare_admitted_request()` merely to avoid keeping the already parsed object.

The current one-parse admission contract is worth preserving.

---

# Workstream 1 — Separate bounded admission from canonical completeness

Primary file: `rust/src/request/admission.rs`.

## 1.1 Preserve unknown native Responses input items

Change Responses admission so a syntactically valid, bounded typed input item is not rejected solely because Eggpool lacks a canonical projection for its `type`.

Known items should continue to project into canonical semantics:

- `message`;
- `function_call`;
- `function_call_output`.

Add canonical projections only where the semantics are genuinely needed for cross-provider translation. In particular, do not create dummy `CanonicalMessage` entries for encrypted reasoning or protocol bookkeeping just to make admission succeed.

For source-native items that have no current canonical equivalent:

- validate overall JSON depth/body/collection bounds;
- retain them in source order in the preservation envelope;
- mark the request as containing native-only semantics;
- continue canonical projection of the rest of the request.

Do not silently discard those items and then permit an apparently lossless cross-surface conversion.

## 1.2 Preserve item order

Current Codex history pairing depends on call/result ordering and can include reasoning between visible messages and tool traffic. Native forwarding must preserve the original `input` array order exactly at the JSON-value level.

The canonical projection may remain grouped as current routing logic requires, but it must not be used to reconstruct a native request when the source envelope is available.

## 1.3 Tool definitions

Introduce enough admission classification to distinguish at least:

```text
function
custom
namespace
tool_search
web_search
other/native
```

Do not require every native tool kind to become `CanonicalTool` immediately.

For ordinary `function`, retain current canonical tool projection.

For `custom`/freeform tools, Plan 195 may add a provider-neutral canonical freeform-tool kind because cross-surface Codex translation benefits from it. Until that work is present, preserve the original definition natively and mark it native-only for adaptation decisions.

For `web_search`, `tool_search`, namespace, and future built-in/native tools, preserve rather than reinterpret them as JSON functions.

## 1.4 Top-level extension fields

Do not whitelist away current/future Responses controls on the native path. The source envelope should retain fields including, when present:

- `include`;
- `stream_options`;
- `prompt_cache_key`;
- `service_tier`;
- `client_metadata`;
- `text` subfields not represented canonically;
- provider-supported extension fields.

This does not mean Eggpool must translate them cross-protocol.

---

# Workstream 2 — Reassert stateless/safety policy before native forwarding

A preservation path must not accidentally re-enable server-side state features that earlier compatibility work intentionally excluded.

Before a native Responses request can be forwarded, explicitly validate the stateful fields Eggpool owns as product policy.

At minimum verify behavior for:

- `store` — `false` or omitted is accepted; `true` fails closed unless Eggpool later adopts a deliberate stateful design;
- `previous_response_id` — reject under the stateless contract;
- conversation/server-owned continuation fields — reject under the stateless contract;
- `background` — reject when it requests asynchronous/background server execution;
- any analogous current Responses field that requires Eggpool to persist provider-side response identity across independently routed calls.

Do this in a named validation function with focused tests. Do not rely on `encode_responses_request()` forcing `store: false`, because native preservation bypasses that encoder by design.

Stateful-policy errors should be distinguishable from malformed JSON and from unsupported cross-surface adaptation.

---

# Workstream 3 — Native model rewrite without canonical rebuild

Primary file: `rust/src/wire/runtime.rs`.

The current condition:

```text
native path && upstream_model_id == canonical model
    -> raw bytes
else
    -> encode canonical request
```

must change.

For native Responses->Responses routing:

- if the model ID is unchanged, forwarding original validated bytes remains ideal;
- if Eggpool resolved an alias to another upstream model ID, take the preserved parsed source object and rewrite only the top-level `model` field;
- serialize the rewritten source object using the existing bounded compact encoder;
- preserve every other admitted field/item unchanged;
- do not route the body through `encode_responses_request()` merely because the model changed.

This pattern can later be generalized to other native wire surfaces if useful, but this plan does not require broad refactoring.

Conceptually:

```text
Responses source + Responses target
    preserved source object
      -> enforce native policy
      -> rewrite model iff required
      -> bounded serialize
      -> upstream
```

Authentication, endpoint selection, headers, and provider-specific path construction remain outside this object rewrite.

## 3.1 Keep exact raw bytes when possible

If no body mutation is required, keep the current exact raw-byte fast path. It is cheaper and preserves formatting/unknown fields automatically.

If only `model` changes, semantic JSON preservation is sufficient; byte-for-byte formatting is not a compatibility requirement.

## 3.2 No arbitrary provider mutation

Do not build an open-ended JSON patch system here. Eggpool should mutate only fields it owns. Initially that is the selected upstream `model` plus any already-established provider boundary behavior that is explicitly required by the wire profile.

---

# Workstream 4 — Cross-surface adaptation must know when projection is incomplete

A tolerant native admission path creates a new obligation: translated destinations must not unknowingly receive a canonical request from which important source semantics were omitted.

When the selected upstream surface is not Responses, inspect the preservation summary before encoding.

Classify preserved native features into three categories:

### Safe to omit with an explicit adaptation notice

Examples may include provider-specific cache hints or encrypted reasoning continuity that has no meaning on the target provider, provided omission cannot turn a tool or user instruction into different semantics.

The implementation should be conservative. If uncertain, fail rather than silently omit.

### Canonically translatable

Known `message`, function-call/result items and tool definitions continue through existing canonical encoders. Plan 195 adds freeform/custom-tool conversion where justified.

### Native-only semantic blocker

Examples include an unsupported tool kind or replay item whose removal would alter the agent loop. Return `UnsupportedSemanticFeature` before provider dispatch.

Do not mark the upstream account unhealthy and do not consume a provider retry merely because the chosen wire profile cannot represent the source request. This is a compatibility/adaptation decision, not an upstream outage.

Use the existing adaptation notice/policy mechanisms from Plans 149–156 rather than creating Codex-specific error handling.

---

# Workstream 5 — Reasoning replay policy

Current Codex commonly requests `include: ["reasoning.encrypted_content"]` and can replay returned `reasoning` items on subsequent stateless turns.

For native Responses->Responses routing:

- preserve `include` exactly;
- preserve reasoning input items exactly, including `encrypted_content` and IDs;
- preserve provider-specific extra content attached to a Responses item;
- never inspect/log encrypted content by default.

For a cross-provider route:

- do not pretend an OpenAI encrypted reasoning blob is portable to Anthropic/Gemini/Chat Completions;
- omit only under an explicit, tested adaptation policy when the remainder of the turn is still semantically usable, or fail closed if the selected model/provider contract requires that continuation state;
- visible reasoning summaries may be projected only if existing reasoning policy permits it;
- do not leak hidden reasoning into ordinary assistant/user text as a translation technique.

Eggpool should not reproduce OpenCodex's provider-specific reasoning cache unless a real provider contract requires it. Eggpool already receives the complete request each turn; native Responses replay should remain client-carried/stateless.

---

# Workstream 6 — Finite Responses parity

Although Codex normally streams, apply the same source-preservation rule to finite Responses requests and responses where applicable.

`OpenAiResponsesCodec::encode_request()` should remain the cross-surface/native-synthesis codec. Do not remove it. Change the runtime selection so it is bypassed for source-preserving native Requests even when the model is rewritten.

Finite same-surface provider responses already have a body passthrough path in `WireRuntime::process_finite_response()`. Preserve that behavior and add regression coverage showing native response fields survive.

---

# Tests

Add focused unit tests before live Codex qualification.

## Admission tests

Fixtures should include:

1. ordinary message/function call/function output;
2. reasoning item with `encrypted_content`;
3. custom tool call/output history;
4. local shell/tool-search/web-search history items;
5. unknown future typed input item with bounded JSON;
6. custom tool definition;
7. web-search tool definition;
8. unknown future tool definition;
9. `store: false`;
10. explicit rejection of `store: true`;
11. explicit rejection of `previous_response_id`/background state as defined by current policy.

Assert both the canonical projection and preservation metadata.

## Native request tests

For a Responses alias such as:

```text
client model: coding-fast
upstream model: provider/model-v2
```

assert that the upstream encoded JSON differs only where Eggpool intentionally rewrites it. Specifically retain:

- complete ordered `input` array;
- reasoning encrypted content;
- custom/native tool objects;
- `include`;
- `prompt_cache_key`;
- text controls;
- an unknown benign extension fixture.

## Cross-surface tests

Prove that a native-only semantic blocker does not silently disappear when routing to Chat/Anthropic/Gemini.

Use exact error reason codes/notices expected by the existing adaptation policy.

---

# Source files expected to change

Primary:

- `rust/src/request/admission.rs`;
- `rust/src/request/mod.rs` if new preservation types are exported;
- `rust/src/wire/runtime.rs`;
- `rust/src/wire/additional_codecs.rs` only where request encoding/adaptation policy must recognize the new boundary;
- relevant request/wire unit tests.

Potential secondary:

- `rust/src/wire/ir.rs` only for genuinely provider-neutral tool-kind semantics required by Plan 195;
- `rust/src/wire/adaptation.rs` for native-only/cross-surface notices;
- coordinator error mapping tests if a new adaptation classification reaches the endpoint.

Avoid changing routing, retry, provider account, or transport modules unless tests expose a concrete boundary issue.

---

# Acceptance criteria

1. A current Codex Responses request containing replayed reasoning/custom items passes bounded admission.
2. Same-surface Responses routing preserves those items without adding them to generic canonical message types.
3. A model alias rewrite changes only the top-level `model` while preserving the remaining native request JSON.
4. Same-surface native forwarding still rejects Eggpool-disallowed stateful Responses features before provider dispatch.
5. Unknown future bounded Responses items can survive a native path without an Eggpool release.
6. The same unknown/native-only semantic item cannot silently disappear on a translated route.
7. Ordinary existing function-tool translation remains unchanged.
8. Request limits, media validation, tool identity checks for canonically understood tools, routing facts, affinity behavior, and token reservation remain bounded.
9. No second JSON parse is added to the hot request path.
10. Logs/debug representations do not emit preserved prompts, encrypted reasoning, tool arguments, or secrets by default.

---

# Handoff implementation order

1. Add preservation metadata/type at admission boundary.
2. Make Responses input/tool admission tolerant-but-bounded and classify unprojected semantics.
3. Add explicit stateless Responses policy validation.
4. Teach native request preparation to use the preserved source object and rewrite only `model` when needed.
5. Add cross-surface blocker/notice handling.
6. Add request-level regression fixtures.
7. Re-run existing codec/admission/coordinator tests before starting Plan 194.

Do not start by adding every Codex `ResponseItem` variant to `CanonicalMessage`; that is the failure mode this plan is designed to avoid.

## Completion evidence

- Implemented in `0285cffd1179623cfcffd8a3b22018f609cc0964` (`Preserve native Responses requests across aliases`).
- Admission retains a bounded native preservation envelope alongside the
  canonical projection; same-surface alias routing rewrites only `model`.
- Unknown/native input items and tools survive native forwarding, while
  cross-surface blockers and adaptation notices remain explicit.
- Stateless Responses policy is enforced before provider dispatch.
