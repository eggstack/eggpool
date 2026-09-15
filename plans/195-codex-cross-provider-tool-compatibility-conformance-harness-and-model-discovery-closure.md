# Plan 195: Codex cross-provider tool compatibility, conformance harness, and model-discovery closure

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` at `51d770e16598cdea76ed56670f68b885f7f68ed5`
>
> **Parent:** Plans 192–194
>
> **External references:** OpenAI Codex `508a006d7aaa485ac0367c9e45c69ebb948af518`; OpenCodex `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`
>
> **Scope:** finish practical Codex use through Eggpool after native request/stream fidelity is repaired: translate the Codex tool forms that are meaningfully portable, build an exact protocol conformance harness, document custom-provider setup, and close the model-discovery question without breaking Eggpool's standard `/v1/models` contract.

## Executive summary

Plans 193–194 make Responses itself reliable. This plan verifies that the resulting surface is useful as an aggregator rather than only as a transparent OpenAI-compatible tunnel.

Current Codex can declare more than ordinary JSON function tools. Its current Responses tool schema includes function, custom/freeform, namespace, tool-search, and web-search forms. Its history also distinguishes ordinary `function_call` from `custom_tool_call` and pairs each with its corresponding output item.

OpenCodex gives a useful portability pattern: when bridging a Responses custom/freeform tool to a Chat-Completions-style provider, it represents the tool upstream as a function with a single string payload and then reconstructs the authoritative `custom_tool_call` Responses item downstream. That pattern is appropriate for Eggpool only where semantics are genuinely equivalent and deterministic.

The goal is not to emulate every Codex/OpenAI server tool. The goal is:

- ordinary function tools work across all existing Eggpool translators;
- custom/freeform client-executed tools can be bridged when the target provider can call a normal function;
- provider/server tools are forwarded natively when possible and fail/adapt explicitly when not;
- current Codex request/SSE behavior is captured as external conformance fixtures so future internal refactors cannot regress it;
- explicit Eggpool model selection is a documented supported path;
- automatic Codex model picker discovery remains optional and does not corrupt the OpenAI-standard `/v1/models` endpoint.

---

# Workstream 1 — Add a provider-neutral tool kind only where portability is real

Primary files:

- `rust/src/wire/ir.rs`;
- `rust/src/request/admission.rs`;
- `rust/src/wire/additional_codecs.rs`;
- other finite/stream encoders that consume canonical tools.

Current `CanonicalTool` assumes JSON-object parameters. That is correct for ordinary functions but cannot faithfully describe a Responses `custom` tool whose input is freeform text.

Introduce a small provider-neutral distinction, for example conceptually:

```rust
enum CanonicalToolKind {
    Function,
    Freeform,
}
```

and enough data for a freeform tool to retain:

- name;
- description;
- optional input-format grammar/metadata only if it is portable and bounded.

Do not add Responses-specific `namespace`, `output_index`, item IDs, or server-tool event names to this type.

## 1.1 Function tools

Preserve existing behavior exactly.

## 1.2 Freeform/custom tools

For Responses admission, project `type: "custom"` into the freeform canonical tool kind while retaining the original native object through Plan 193's preservation envelope.

For a target Responses upstream, use the preserved native definition rather than reconstructing it where possible.

For a target that supports only function-style JSON tool calling, bridge a freeform tool using a deterministic wrapper such as:

```json
{
  "type": "function",
  "function": {
    "name": "apply_patch",
    "description": "...",
    "parameters": {
      "type": "object",
      "properties": {
        "input": {"type": "string"}
      },
      "required": ["input"],
      "additionalProperties": false
    }
  }
}
```

The exact wrapper can follow each target surface's existing function-tool grammar.

Record in per-request translation state that this tool was wrapped from freeform semantics. When the upstream later calls it, unwrap the single `input` string and synthesize a downstream `custom_tool_call`, not a `function_call`.

This mapping must be name/call-ID scoped and per request/stream. Do not infer freeform semantics from a tool name alone.

If the upstream returns malformed wrapper arguments, fail the tool-call translation rather than passing JSON wrapper text to Codex as if it were the original freeform body.

## 1.3 Custom tool outputs

Admit and project `custom_tool_call_output` so a later Codex turn can be translated back to a wrapped function result for non-Responses providers.

Maintain the original `call_id` pairing. Do not convert custom outputs to ordinary user text.

---

# Workstream 2 — Delimit non-portable server/native tools

Codex may expose tool definitions/items such as:

- `web_search`;
- `tool_search`;
- namespaces;
- local shell forms;
- image generation/server-owned tool outputs.

Do not implement speculative universal translations.

Use this policy:

## Native Responses target

Preserve and forward the source-native tool definitions/history exactly under Plan 193. Native upstream capability remains responsible for accepting/rejecting the feature.

## Target with an existing Eggpool semantic equivalent

Use the existing canonical feature only when semantics align. For example, Eggpool already has web-search/server-tool concepts in parts of the wire layer; reuse them rather than creating Codex-specific duplicates.

## No equivalent

Return an explicit pre-dispatch unsupported/adaptation error when the item is required for the requested turn. Do not silently strip a declared tool and let the model answer as if the tool were unavailable unless existing adaptation policy explicitly permits that degradation and the user/client requested a best-effort policy.

The default Codex compatibility path should prefer correctness over surprising tool removal.

---

# Workstream 3 — Preserve tool identity through translated turns

A Codex agent loop crosses two request directions:

```text
model -> tool call -> Codex executes -> tool output -> model
```

For translated freeform/function tools, keep a deterministic per-turn mapping among:

- downstream Responses item ID;
- downstream Responses `call_id`;
- canonical call ID;
- upstream provider tool-call ID;
- tool name;
- original tool kind;
- wrapper kind, if any.

Do not persist this mapping in Eggpool's database across independent requests. Codex includes the call/result history in later stateless Requests. Each request must carry enough information to re-project prior calls/results through the Plan 193 input preservation/canonical projection path.

Where an upstream protocol requires its own opaque signature for a previous tool call, preserve it only through an existing provider-specific mechanism or the native request envelope. Do not make global provider state a prerequisite for Codex.

---

# Workstream 4 — Build a Codex protocol conformance test target

Add a dedicated deterministic test target, suggested name:

```text
rust/tests/codex_responses_compat.rs
```

or the closest repository convention.

The fixtures should be derived from current Codex source behavior, not from Eggpool's interpretation of the OpenAI docs alone.

Record the audited Codex commit in the fixture/module comments:

```text
508a006d7aaa485ac0367c9e45c69ebb948af518
```

Likewise note the OpenCodex bridge commit used as a cross-provider reference:

```text
e4a8539b957b7ae7cd278666f0364eb0f82d4ac3
```

These are provenance markers, not runtime dependencies.

## 4.1 Request fixtures

At minimum cover:

1. first-turn user message with instructions;
2. second-turn replay containing prior assistant output;
3. prior `reasoning` item with encrypted content;
4. function call + function output replay;
5. custom tool call + custom tool output replay;
6. parallel tool calls;
7. `include: ["reasoning.encrypted_content"]`;
8. reasoning effort/summary controls;
9. prompt cache key/service tier/text controls when present in current Codex builder;
10. unknown benign Responses extension preserved natively;
11. unsupported stateful field rejected explicitly.

For each fixture, test both:

- native Responses target;
- translated target where the feature is supposed to be portable.

## 4.2 SSE fixtures

At minimum cover:

1. response created;
2. assistant text delta + complete assistant `output_item.done`;
3. function argument deltas + complete function-call `output_item.done`;
4. custom tool input delta/done + complete custom-tool-call item if supported;
5. parallel calls with independent IDs/buffers;
6. reasoning summary delta with `summary_index`;
7. reasoning complete item;
8. completed response with usage;
9. failed response;
10. incomplete response;
11. premature EOF;
12. unknown future event preserved on native path.

The downstream assertion should mimic current Codex's parser contract. In particular, a function-call test must **not** pass merely because argument delta/done frames exist; it passes only when the complete function call is present in `response.output_item.done`.

## 4.3 Round-trip agent fixture

Add one deterministic in-process fixture that executes the protocol sequence:

```text
Codex-style request
  -> Eggpool translated upstream request
  -> synthetic provider tool-call stream
  -> Eggpool Responses stream
  -> captured call_id/tool payload
  -> Codex-style function/custom output request
  -> Eggpool translated continuation request
  -> synthetic provider assistant response
  -> Eggpool completed Responses output
```

No real provider credentials are needed for this test.

Test both an ordinary function tool and, after implementation, a freeform/custom tool.

---

# Workstream 5 — Add a small external Codex smoke harness

Unit/golden fixtures prove wire shape, but the final closure needs one smoke path against an actual current Codex executable.

Prefer a script or documented test procedure under the repository's existing integration-test conventions rather than adding Codex as a Cargo/npm dependency.

The harness should:

1. launch Eggpool locally with a deterministic test/upstream adapter or a configured real provider;
2. set a temporary Codex config containing the Eggpool custom provider;
3. disable WebSocket use for that provider;
4. select an explicit Eggpool model/alias;
5. execute one text turn;
6. execute one tool-use turn;
7. execute a continuation after tool output;
8. record exit status plus bounded/redacted Eggpool diagnostics.

Never capture API keys or full private prompts in committed fixtures.

If automating Codex itself proves brittle across release packaging, keep the smoke procedure manual but make the protocol fixtures mandatory in CI.

---

# Workstream 6 — Document the supported custom-provider configuration

Add/update the appropriate Eggpool integration documentation after the protocol tests pass.

Document a current configuration analogous to:

```toml
model = "<eggpool model or alias>"
model_provider = "eggpool"

[model_providers.eggpool]
name = "Eggpool"
base_url = "http://127.0.0.1:8000/v1"
env_key = "EGGPOOL_API_KEY"
wire_api = "responses"
supports_websockets = false
```

Do not hard-code port `8000` if the repository's current examples use another default; use the actual current server default/environment conventions during implementation.

Explain:

- Eggpool's key belongs in the configured environment variable;
- model aliases are allowed;
- Codex uses the Responses wire API;
- HTTP/SSE is the qualified path;
- automatic model-picker discovery may not enumerate Eggpool models yet;
- explicit model selection is the supported initial behavior.

If the current Codex syntax has changed, update the example to the implementation-time source rather than preserving this plan's illustrative spelling.

---

# Workstream 7 — Keep `/v1/models` standards-compatible

Eggpool currently exposes an OpenAI-style model list for general clients. Current Codex's richer remote model catalog is a different contract containing model metadata such as slug/display name, reasoning levels, context/truncation behavior, visibility, priority, and tool/shell capability information.

Do **not** replace Eggpool's standard `/v1/models` response with the Codex catalog shape.

That would fix one client by regressing the general OpenAI-compatible API.

Initial closure therefore uses explicit model configuration.

---

# Workstream 8 — Optional Codex model discovery, only after inference closure

After Plans 193–194 and Workstreams 1–6 above are green, investigate current Codex support for discovery on custom providers.

Preferred options, in order:

## Option A — Codex learns standard `/v1/models` for custom providers

If current Codex has or accepts a clean fallback path for OpenAI-standard discovery, contribute/support that mechanism there. Codex can derive conservative defaults for metadata that a standard model list does not provide.

This keeps Eggpool generic.

## Option B — Separate opt-in Eggpool catalog endpoint

If Codex requires its rich schema and permits a configurable catalog URL, add a distinct opt-in endpoint rather than changing `/v1/models`.

The endpoint should derive metadata only from facts Eggpool actually knows. Unknown reasoning/context/tool capabilities must use conservative values rather than fabricated precision.

Do not expose this endpoint as though it were part of OpenAI's standard API.

## Option C — Generated static configuration

If Codex cannot query a custom catalog cleanly, a small Eggpool command/documented generator can emit provider/model TOML snippets from Eggpool's current catalog. This is preferable to maintaining a Codex fork.

Only add such a generator if manual explicit models are a real usability burden after the core integration works.

Model discovery is not a prerequisite for marking `/v1/responses` Codex-compatible.

---

# Workstream 9 — Capability advertisement and routing

Do not route a Codex request to a provider merely because that provider can answer text.

Where the request requires a tool/reasoning feature, feed the translated/native feature facts into the existing routing/capability policy where feasible.

At minimum:

- a mandatory freeform/custom tool should not be sent to a profile that cannot represent the wrapper/function translation;
- a native-only server tool should not be silently routed to an incompatible surface;
- reasoning controls should follow existing adaptation policy;
- unsupported feature classification should occur before consuming a provider attempt whenever the incompatibility is known from profile/capability metadata.

Do not build a Codex-only capability matrix. Extend the general wire-profile/capability model only with reusable facts.

---

# Workstream 10 — Regression and maintenance guardrails

Add comments/tests around the compatibility-sensitive facts most likely to regress:

- native Responses model rewrite must not trigger canonical request reconstruction;
- native Responses SSE must not be re-encoded;
- translated function calls require `output_item.done`;
- item ID and `call_id` are different identities;
- freeform/custom tools must retain their tool kind;
- Codex success requires `response.completed` before EOF;
- standard `/v1/models` must not become a Codex-private schema.

Prefer tests over large compatibility branches in production code.

When future Codex versions add a ResponseItem/event type, first decide whether it is:

1. source-native and pass-through-only;
2. a portable canonical semantic;
3. a non-portable feature that should fail/adapt explicitly.

Do not reflexively expand the canonical IR for category 1.

---

# Expected source/document changes

Likely primary files after Plans 193–194:

- `rust/src/wire/ir.rs` — small function/freeform tool-kind distinction if needed;
- `rust/src/request/admission.rs` — custom tool/call/output canonical projection;
- `rust/src/wire/additional_codecs.rs` — freeform wrapper encoding for target surfaces and Responses reconstruction;
- `rust/src/wire/codecs.rs` and/or target-specific encoder helpers for wrapped freeform functions;
- `rust/src/wire/stream.rs` — downstream custom-tool reconstruction state;
- `rust/tests/codex_responses_compat.rs` — protocol contract;
- integration documentation/config examples.

Potential routing/capability files should change only if a reusable tool-kind capability fact is needed.

No production dependency on OpenCodex or Codex should be added.

---

# Acceptance criteria

1. Current unmodified Codex can use Eggpool as a custom `/v1/responses` provider with an explicit model alias.
2. Text-only first and second turns succeed.
3. Ordinary function tools complete a full call/result/continuation loop through at least one translated non-Responses provider fixture.
4. Freeform/custom tool calls either complete a deterministic wrapper/unwrapper loop or are rejected before provider dispatch; they are never silently converted into incompatible semantics.
5. Native Responses providers preserve all current Codex tool forms without Eggpool needing to understand every one.
6. Reasoning replay survives native Responses routing, including encrypted content.
7. Current-Codex-derived conformance fixtures enforce the authoritative `output_item.done` contract.
8. Parallel calls retain distinct item IDs/call IDs and outputs pair correctly.
9. Premature EOF remains a failure.
10. A real current Codex HTTP/SSE smoke run succeeds for text and one tool loop.
11. Integration docs contain a working current custom-provider configuration.
12. Eggpool's standard `/v1/models` response remains unchanged for general OpenAI-compatible clients.
13. Automatic Codex model discovery is either separately implemented with a non-breaking mechanism or explicitly documented as deferred/optional.
14. No Codex/OpenCodex runtime dependency or Eggpool-specific Codex fork is introduced.
15. Existing provider/wire/routing/coordinator tests remain green.

---

# Recommended completion evidence

At closure, record in the final implementation commit/plan note:

- Eggpool commit;
- Codex commit/version used for live smoke qualification;
- whether OpenCodex reference behavior changed materially from `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`;
- test commands and results;
- exact providers/wire surfaces covered by deterministic translation fixtures;
- whether optional model discovery was implemented or intentionally deferred.

Do not claim general "OpenAI Responses compatibility" solely from Codex passing. The tests should separately retain Eggpool's provider-neutral Responses surface and native-forwarding guarantees.