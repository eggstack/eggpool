# Plan 201: Codex deferred-tool compatibility and coding-agent conformance

> **Status:** complete
>
> **Closed:** 2026-09-16 (implemented `27890a19`; live qualification Plan 206;
> see Closure evidence below)
>
> **Parent:** Plan 198
>
> **Related historical work:** Plan 195 (ordinary function/freeform tool bridge and Codex conformance harness)
>
> **External audit baselines:** OpenAI Codex `4701aa4b4239c70063ab6f2fcb835324f9c109f4`; OpenCodex `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`
>
> **Priority:** P1
>
> **Scope:** add a narrow provider-neutral translation for current Codex `tool_search` deferred-tool calls across function-capable non-Responses upstreams, retain native preservation where available, and extend coding-agent conformance without turning Eggpool into a tool executor.

## Executive summary

Plan 195 deliberately avoided speculative translations for native/server tool forms. That was the correct boundary at the time. Current Codex now has a concrete, first-class `tool_search` flow used for deferred plugin/app tool discovery, and current source defines the request/call/output lifecycle explicitly enough to qualify a portable bridge.

The portable semantic is limited:

- Codex declares a deferred `tool_search` tool;
- the model chooses to call it;
- Codex executes the search locally and returns the tool-search output/history on the next request;
- Eggpool does not search tools, install plugins, execute MCP calls, or interpret the result beyond preserving the protocol lifecycle.

For a native Responses upstream, Eggpool should continue source-native preservation. For an upstream that can only call ordinary JSON function tools, Eggpool can wrap the declaration as a deterministic function and reconstruct the corresponding Responses `tool_search_call` downstream, then map the later tool-search output back into that upstream's function-result grammar.

If current Codex semantics cannot be represented exactly enough on a particular target, reject before dispatch. Do not silently downgrade `tool_search` to text or omit it.

---

# Research authority

At audit commit `4701aa4b...`, current Codex source includes:

- `codex-rs/tools/src/tool_spec.rs` — `ToolSearch` Responses tool variant;
- `codex-rs/tools/src/tool_discovery.rs` — tool-search naming/default behavior;
- `codex-rs/core/src/tools/handlers/tool_search.rs`;
- `codex-rs/core/src/tools/handlers/tool_search_spec.rs`;
- `codex-rs/core/src/context_manager/normalize.rs` — history normalization and tool-search output identity;
- current app/plugin instruction rendering that exposes deferred tool discovery.

OpenCodex provides a useful third-party translation reference but is not the protocol authority.

Eggpool authority:

- `rust/src/wire/ir.rs` — provider-neutral semantics;
- `rust/src/request/admission.rs` — Responses projection/preservation;
- `rust/src/wire/codecs.rs` and `additional_codecs.rs` — surface encoders/decoders;
- `rust/src/wire/stream.rs` / runtime — tool call streaming lifecycle;
- Plan 195 implementation for freeform/custom wrapper identity;
- `rust/tests/codex_responses_compat.rs`.

Re-audit exact current Codex JSON field names at implementation time. The plan specifies semantics, not a frozen stale schema.

---

# Workstream 1 — Capture current `tool_search` fixtures before production changes

Add source-provenanced fixtures that represent current Codex request and history behavior.

At minimum capture:

1. request tool declaration for `tool_search`;
2. model call item/event shape for `tool_search_call`;
3. streamed argument/input fragments if current Codex accepts them;
4. authoritative completed item shape;
5. next-turn `tool_search` output/history item;
6. call/output identity fields;
7. multiple/deferred discovered tool definitions on a subsequent request if relevant to the current client contract;
8. malformed/missing call identity behavior.

Do not start by designing a canonical schema from OpenCodex. First prove what current Codex sends and expects.

Keep fixture content synthetic and small; do not commit real plugin metadata or account data.

---

# Workstream 2 — Decide the minimal canonical semantic

Prefer a small reusable deferred-tool kind rather than a Codex-specific structure.

Potential approach, only if it fits the existing `CanonicalToolKind` design cleanly:

```rust
enum CanonicalToolKind {
    Function,
    Freeform,
    DeferredSearch,
}
```

The canonical representation needs enough bounded data to reconstruct the client-visible call/output lifecycle, for example:

- declared tool name/type;
- description;
- bounded JSON input schema/arguments if present;
- canonical `call_id`;
- completed result payload represented as structured JSON/text exactly as required by the current Codex contract.

Do not place Codex item IDs, output indices, namespace implementation details, or plugin installation state in the global tool type unless they are truly provider-neutral semantics.

If the existing freeform wrapper state can represent this cleanly as an adapter-only mapping without expanding `CanonicalToolKind`, prefer the smaller change. The acceptance criterion is semantic clarity, not a specific enum variant.

---

# Workstream 3 — Native Responses preservation

For a native Responses target:

- preserve `tool_search` declarations in the source-native request envelope;
- forward the exact native request unless Eggpool owns the model rewrite;
- preserve unknown/current `tool_search` stream events/items byte-for-byte under the existing native observe-and-forward path;
- retain normal terminal/usage observation;
- preserve next-turn tool-search history natively.

Do not parse/re-encode native tool-search data merely to make the translator understand it.

Native forwarding still requires the selected upstream to accept that feature. Capability facts should prevent known-incompatible native targets where possible; otherwise the upstream's structured rejection remains a normal provider response.

---

# Workstream 4 — Function-only upstream bridge

For a cross-surface target that supports ordinary function calling, map the deferred search declaration to a deterministic wrapper function.

Conceptually:

```json
{
  "name": "tool_search",
  "description": "...",
  "parameters": { "...current bounded search input schema...": true }
}
```

Use the exact current Codex input shape or an adapter wrapper with lossless conversion. Do not make up a richer search language.

### Declaration-scoped translation state

Like the freeform/custom bridge from Plan 195, record translation identity per admitted request:

- canonical call ID;
- upstream provider call ID;
- downstream Responses item ID;
- tool name;
- original kind = deferred/tool-search;
- wrapper kind/version.

Never infer the semantic from the string `tool_search` alone. A user can legitimately define an ordinary function with that name.

### Upstream call -> downstream Responses call

When the model invokes the wrapped function:

- validate the wrapper arguments against the bounded shape;
- reconstruct the exact current Responses `tool_search_call` item/event lifecycle;
- preserve distinct item ID vs. `call_id` semantics;
- emit the authoritative completed item Codex needs, following the same discipline Plan 195 added for ordinary/freeform tools;
- reject malformed wrapper arguments rather than handing wrapper JSON to Codex as if it were a valid native tool-search call.

### Next-turn output/history -> upstream function result

When Codex returns the corresponding tool-search result on a later stateless request:

- match by explicit `call_id`/history identity;
- project to the target protocol's function/tool result item;
- do not convert it to user text;
- preserve ordering relative to other calls/results;
- support parallel ordinary/freeform/tool-search calls if current Codex can interleave them.

The next request itself carries enough history for this reconstruction; do not add a database dependency for tool-call state across requests.

---

# Workstream 5 — Namespace/deferred tool exposure boundaries

Current Codex uses deferred search to make additional tool definitions available after the local search. Eggpool's role is to transport the resulting next request, not execute those tools.

For tool definitions that appear after a search:

- ordinary function tools use existing translation;
- custom/freeform tools use Plan 195 behavior;
- namespace/native server tools remain native-only unless a separate exact semantic bridge exists;
- unsupported required tools must fail before provider dispatch when incompatibility is known.

Do not recursively implement every current Codex plugin/tool category under this plan.

Do not make Eggpool aware of plugin installation, connector credentials, Codex app IDs, or local tool registries.

---

# Workstream 6 — Capability-aware routing

Expose a reusable `deferred_tool_search`/equivalent capability fact through the wire compatibility layer from Plan 200.

A request requiring `tool_search` is eligible for a target when either:

1. native Responses forwarding preserves it and the wire profile is qualified for native tool-search semantics; or
2. the target supports the exact function-wrapper bridge implemented here.

Known-incompatible candidates should be excluded before consuming an upstream attempt.

Do not route based on a model-name allowlist.

For public aliases, Plan 200's conservative capability aggregation applies.

---

# Workstream 7 — Error and adaptation policy

Use existing structured unsupported/adaptation errors.

Examples:

- target has no function tools and no native tool-search -> pre-dispatch unsupported;
- wrapper arguments malformed -> translation error, not model text;
- next-turn output references unknown/mismatched call -> invalid semantic history;
- output payload exceeds bounds -> bounded admission/translation failure;
- upstream refuses a native tool_search feature -> ordinary classified upstream failure, not a proxy crash.

Never silently remove `tool_search` from a request where Codex declared it as available. The model's behavior can materially change if the tool disappears.

---

# Workstream 8 — Extend Codex conformance tests

Extend `rust/tests/codex_responses_compat.rs` rather than creating a parallel Codex harness unless file size/readability clearly warrants a split.

Add deterministic cases for:

1. native `tool_search` declaration preservation;
2. native call/output item preservation;
3. function-wrapper declaration encoding;
4. upstream function call -> downstream tool-search call reconstruction;
5. downstream tool-search output -> upstream function result reconstruction;
6. item ID vs. call ID separation;
7. interleaved argument accumulation if streamed;
8. parallel ordinary function + custom/freeform + tool-search calls;
9. malformed wrapper arguments fail closed;
10. ordinary user-defined function named `tool_search` is **not** reclassified without declaration metadata;
11. unsupported target rejected before provider I/O;
12. unknown future native event still preserved on same-surface Responses routing.

Pin the source commit in comments/fixture provenance.

---

# Workstream 9 — Broaden the live coding-agent smoke without making it brittle

The existing live Codex smoke proves text and a local shell tool loop. Retain that fast qualification.

If current Codex exposes a deterministic way to exercise `tool_search` without real external plugin credentials, add an optional phase. Otherwise keep `tool_search` deterministic in-process and do not make live smoke depend on a third-party app ecosystem.

For OpenCode, add an optional/manual qualification using the Plan 200 generated configuration:

- select an Eggpool model;
- text turn;
- ordinary tool call if the harness exposes one;
- multi-turn continuation;
- verify no config/parser error.

Do not add OpenCode/Node as a production dependency or mandatory Rust CI dependency.

---

# Workstream 10 — Conformance maintenance policy

Coding-agent schemas move quickly. Add a short maintenance note near the fixtures:

When a new Codex tool/item/event appears, classify it as:

1. native-preservation only;
2. provider-neutral portable semantic;
3. unsupported/non-portable.

Only category 2 expands canonical translation.

Update fixture provenance when compatibility-sensitive source behavior changes. Do not churn the pinned commit simply because upstream released unrelated UI/features.

---

# Expected source changes

Likely minimal set:

```text
rust/src/request/admission.rs
rust/src/wire/ir.rs                         # only if a reusable kind is actually needed
rust/src/wire/additional_codecs.rs
rust/src/wire/codecs.rs                     # target-specific function result mappings as needed
rust/src/wire/stream.rs / runtime helpers   # current call lifecycle reconstruction
rust/src/wire/adaptation.rs                 # compatibility fact/policy if needed
rust/tests/codex_responses_compat.rs
scripts/smoke_codex_compat.sh               # optional phase only if stable

docs/stateless-responses.md
docs/agent-configuration.md or codex compatibility docs
```

Do not add a plugin/MCP runtime, network search client, or client SDK dependency.

---

# Verification

Run the current Responses/wire/Codex focused suites before full workspace testing:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
```

Then run coordinator tests affected by routing/admission changes and the full serial workspace suite.

---

# Acceptance criteria

1. Current Codex `tool_search` request/call/output shapes are captured in source-provenanced fixtures before translation code is relied on.
2. Native Responses routes preserve tool-search semantics without canonical re-encoding.
3. A qualified function-capable non-Responses route can carry the complete tool-search declaration -> call -> local Codex execution -> output -> continuation lifecycle.
4. Translation identity is declaration-scoped; an ordinary function named `tool_search` is never misclassified by name alone.
5. `call_id` and Responses item IDs remain distinct and stable.
6. Tool-search outputs remain tool outputs, not user text.
7. Parallel/interleaved calls do not cross-contaminate wrapper state.
8. Known-incompatible targets are rejected before provider submission.
9. Eggpool does not execute searches, plugins, MCP tools, or newly discovered tools itself.
10. Existing ordinary function/freeform translations and native Responses unknown-event preservation remain green.
11. The generated capability metadata from Plan 200 advertises deferred tool support only where it is guaranteed.
12. No new production dependency on Codex, OpenCodex, OpenCode, or a plugin runtime is added.

---

## Closure evidence

- Implemented: `27890a19f9c78099027e383b73ada0680ee3efb6` — provider-neutral
  `CanonicalToolKind::DeferredSearch` (client-executed only), declaration-scoped
  function wrapper, native preservation, pre-dispatch rejection for hosted/server
  search, stream/finite reconstruction with stable IDs. Per-plan record in
  `plans/204-codex-deferred-tool-compatibility-and-conformance-closure.md`.
- Focused tests: `codex_responses_compat` (incl. deferred cases) + `wire_codecs`,
  `wire_stream`, `wire_runtime`, `wire_qualification`, `wire_adaptation`.
- Live qualification (Plan 206, Codex CLI `0.154.0`): deferred `tool_search`
  live path NOT_LIVE_EXERCISABLE (no stable way to force without private app
  state); deterministic conformance retained as authority. Ordinary function
  named `tool_search` remains non-reclassified. EggPool never executes search.
