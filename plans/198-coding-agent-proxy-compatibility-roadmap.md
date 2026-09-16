# Plan 198: Coding-agent proxy compatibility roadmap

> **Status:** complete
>
> **Baseline:** Eggpool `main` at `f6e5bdbe098356b22a8c5c7705a0b7c1d35fc5ba` (2026-09-16)
>
> **Closed:** 2026-09-16 via Plans 206–208 (implementation Plans 199–202 +
> live qualification; see Closure evidence below)
>
>
> **Parent context:** Plans 192–197
>
> **External audit baselines:** OpenAI Codex `4701aa4b4239c70063ab6f2fcb835324f9c109f4`; OpenCodex `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`; OpenCode provider documentation reviewed 2026-09-16
>
> **Scope:** finish the remaining work that makes Eggpool a low-friction model proxy for coding agents such as Codex and OpenCode without turning Eggpool into a Codex backend or client-specific product. Add an operator-facing `eggpool status` command as part of the same usability pass.

## Executive summary

Plans 192–197 established the difficult protocol foundation: a native Rust `POST /v1/responses` surface, source-native Responses preservation, bounded cross-surface translation, authoritative Responses streaming lifecycle synthesis, ordinary function tools, freeform/custom tool wrapping, Codex conformance fixtures, a live Codex smoke path, and a safe Codex custom-provider renderer.

The current repository is therefore not missing a general Responses implementation. The remaining work is concentrated at the client-integration and lifecycle edges:

1. project Eggpool's existing catalog/model-info facts into a conservative coding-agent model capability view;
2. generate a current Codex `model_catalog_json` and richer OpenCode model definitions from that view;
3. add idempotent client-side config apply/sync/check/remove operations rather than requiring copy/paste-only setup;
4. translate current Codex `tool_search` semantics on cross-surface routes where the client remains the tool executor;
5. add remote-compaction compatibility as an optional/forward-compatible surface, while preserving the existing stateless HTTP/SSE path;
6. expand conformance tests around these client-sensitive contracts;
7. add a concise `eggpool status` command backed by authoritative live health/catalog/runtime evidence.

The implementation should preserve Eggpool's current architectural identity: a small LAN-hosted provider router/transcoder with strong protocol compatibility. Do **not** copy OpenCodex's broader Codex-private backend, account-affinity, image/voice/history, or dashboard surfaces.

---

# Research findings

## 1. Current Codex custom providers do not require remote compaction

Current Codex's provider abstraction defines:

```rust
enum RemoteCompactionSupport {
    Unsupported,
    V2,
}
```

and the default `ProviderCapabilities` uses `RemoteCompactionSupport::Unsupported`. The v2 remote form is described as `compaction_trigger` items carried over the Responses endpoint. See current Codex:

- `codex-rs/model-provider/src/provider.rs` at audit commit `4701aa4b...`;
- `codex-rs/core/src/tasks/compact.rs`;
- `codex-rs/core/src/compact_remote_v2.rs`.

Eggpool's generated Codex provider is an ordinary custom Responses provider with WebSockets disabled, so Codex's normal qualified path can continue using local compaction. This is important: lack of `/v1/responses/compact` is **not** a current blocker for the Eggpool custom-provider configuration.

OpenCodex supports additional compatibility behavior because it aims at a much deeper Codex replacement/integration surface. Its current implementation handles both historical `POST /v1/responses/compact` behavior and newer v2 compaction triggers. Eggpool should add compatible support only after the generic coding-agent integration work is stable; it should not make stateful server-side compaction a prerequisite for basic Codex use.

## 2. Stateless HTTP/SSE is still a sound Eggpool boundary

Current Codex has extensive `previous_response_id` machinery, especially for WebSocket incremental request reuse. However, its HTTP/SSE custom-provider tests also explicitly exercise stateless replay: later requests carry retained history in `input` rather than requiring the server to persist response state. See current Codex:

- `codex-rs/core/src/client.rs`;
- `codex-rs/core/tests/suite/client.rs`, including the stateless Responses/custom-provider coverage;
- `codex-rs/rollout-trace/src/reducer/conversation.rs`.

Therefore this roadmap does **not** reopen Eggpool's current admission policy from `rust/src/request/admission.rs` / `docs/stateless-responses.md`. `previous_response_id`, stored conversations, and background jobs remain out of scope for the qualified HTTP/SSE coding-agent path.

WebSocket Responses and bounded response-ID continuation can be reconsidered together later. Adding persistence now would increase memory/state ownership and recovery complexity without solving a current blocker.

## 3. Rich model metadata is now the largest Codex usability gap

Current Codex supports `model_catalog_json` as a startup model-catalog override. The field is present in current configuration/schema code, including:

- `codex-rs/config/src/profile_toml.rs`;
- `codex-rs/config/src/config_toml.rs`;
- `codex-rs/core/src/config/mod.rs`;
- `codex-rs/core/config.schema.json`.

Current model metadata includes context limits and reasoning presets. Codex derives its default automatic compaction threshold from the resolved context window (normally 90% when no explicit override exists) in `codex-rs/protocol/src/openai_models.rs`.

Eggpool already has much of the necessary raw information:

- `rust/src/catalog/` owns provider model discovery and capability/limit metadata;
- `rust/src/operations/integrations.rs::IntegrationModel` already carries display name, capabilities, source metadata, and context/input/output limits;
- model-info enrichment and semantic model routing already exist;
- aliases/selectors can represent multiple underlying provider/model candidates.

The missing layer is a conservative, provider-neutral **agent model projection** and client-specific renderers. Do not mutate the standard OpenAI `/v1/models` schema to satisfy Codex.

## 4. OpenCode is primarily a configuration/metadata problem

Current OpenCode documentation distinguishes OpenAI-compatible Chat Completions from Responses-backed providers. A Responses endpoint should use the OpenAI AI SDK runtime (`@ai-sdk/openai`), while generic Chat-Completions-compatible endpoints use the compatibility provider. Per-model context/output limits are used by OpenCode to reason about available context.

Eggpool already renders OpenCode configuration. The needed work is to make that renderer use the same canonical agent-model facts as Codex and to expose accurate limits/capabilities without maintaining two independent compatibility databases.

## 5. `tool_search` is now a real Codex tool form

Current Codex defines `tool_search` as a first-class Responses tool and uses it for deferred tool/plugin discovery. Relevant current source includes:

- `codex-rs/tools/src/tool_spec.rs`;
- `codex-rs/tools/src/tool_discovery.rs`;
- `codex-rs/core/src/tools/handlers/tool_search.rs`;
- `codex-rs/core/src/context_manager/normalize.rs`.

Plan 195 correctly left native/server tool forms native-only unless a real portable semantic existed. There is now enough current client behavior to add a narrowly scoped bridge: Codex remains the tool executor; Eggpool only needs to preserve the declaration/call/output lifecycle across an upstream protocol that understands ordinary function tools.

Do not generalize this into server-side plugin execution or MCP hosting.

## 6. Eggpool already owns the evidence needed for `status`

The new `eggpool status` command should aggregate existing authoritative state rather than add active provider probes:

- `rust/src/health/health_manager.rs` — account health, cooldowns, circuit state, model quarantine;
- `rust/src/accounts/registry.rs` — immutable provider/account identity, enabled state, credential usability, supported request surfaces;
- `rust/src/catalog/` plus `db::PingRepository` — latest provider `/models` probe status, latency, model count, refresh state;
- `rust/src/runtime_lifecycle/diagnostics.rs` — active generation, tasks, reload/publication/shutdown/finalization state;
- `rust/src/server/health.rs::readyz` — current minimum readiness semantics.

`GET /api/stats/runtime` is useful deep diagnostics but currently contains intentionally placeholder/null fields for some memory/routing/provider-client metrics. The new status surface must not relabel those placeholders as authoritative health.

---

# Implementation sequence

The detailed handoff is split into Plans 199–202.

## Phase A — Agent model projection, catalogs, and config lifecycle

Implement Plan 200 first unless a protocol regression makes another phase urgent.

This phase provides the largest usability improvement:

```text
Eggpool catalog + model-info + routing facts
                |
                v
       AgentModelProjection
          /             \
         v               v
 Codex catalog       OpenCode models
         \               /
          v             v
       configsetup apply/sync/check
```

The projection is reusable and conservative. Client-specific schema details remain in renderers.

## Phase B — Deferred tool compatibility and conformance

Implement Plan 201 after the projection/renderers are in place. Add `tool_search` only where its semantics can be reproduced exactly enough for Codex to remain the executor.

Native Responses routes continue forwarding native forms unchanged.

## Phase C — Operator `status`

Plan 202 is independent enough to run in parallel with A/B. It should reuse live generation, health, catalog/ping, and runtime diagnostics without making outbound provider calls.

## Phase D — Remote compaction compatibility

Implement Plan 199 after A/B unless current Codex changes make it necessary earlier.

The ordering is deliberate: current custom providers default to local compaction. Remote compaction is valuable for future compatibility and for deeper Codex/OpenCodex-style integration, but it should not delay model discovery/config ergonomics or tool compatibility.

---

# Cross-cutting architecture rules

## Keep standard `/v1/models` standard

`GET /v1/models` remains the ordinary OpenAI-compatible model-list surface. Do not add Codex-private fields or replace its shape.

If remote client-side synchronization needs richer Eggpool facts, expose a separately named, authenticated, non-standard Eggpool endpoint with an explicit schema/version. Plan 200 defines this boundary.

## Keep coding-agent facts provider-neutral

Do not introduce a `CodexModel` into routing or catalog internals. The shared projection should express facts Eggpool actually knows, such as:

- canonical/public model ID;
- display name;
- provider/route provenance where safe;
- context and output limits;
- input modalities;
- reasoning support/presets;
- ordinary function-tool support;
- freeform/custom-tool support;
- deferred tool-search support;
- Responses support;
- WebSocket support.

Codex/OpenCode renderers translate those facts into their own schema.

## Advertise route aliases conservatively

For an alias/selector that may route to heterogeneous targets, advertise only capabilities guaranteed across all candidates unless the routing policy can prove it will select a compatible candidate for the requested feature.

Examples:

- context limit: minimum guaranteed context among eligible targets;
- max output: minimum guaranteed output limit;
- input image support: true only if all eligible targets can satisfy it, unless capability-aware routing makes that guarantee;
- freeform/deferred tools: true only if every eligible target can carry or translate the semantic;
- reasoning levels: intersection of supported levels.

Unknown metadata stays unknown/conservative. Do not infer capabilities from model names.

## Preserve the stateless Responses contract

Plans 199–201 must not silently enable `store`, conversations, background jobs, or `previous_response_id` persistence.

Native Responses same-surface preservation and translated canonical semantics remain the two explicit paths documented in `architecture/` and `docs/stateless-responses.md`.

## No new client runtime dependency

Do not add Codex, OpenCodex, OpenCode, Node, or an AI SDK as an Eggpool production dependency. Use pinned source-derived fixtures and optional live smoke scripts.

## No active upstream traffic for `status`

`eggpool status` is a read-only snapshot. It must not consume rate limits, alter circuit breakers, acquire routing claims, or refresh catalogs. A future explicit `probe` command can perform live checks if desired.

---

# Deferred/non-goals

The following are explicitly outside this roadmap unless a current client requirement proves otherwise:

- Responses WebSocket proxying;
- server-persisted `previous_response_id` replay;
- ChatGPT/Codex private history, notes, memories, voice, image-generation, or account-affinity APIs;
- an OpenCodex-compatible management dashboard;
- server-side execution of Codex plugins/MCP tools;
- replacing Codex local compaction with Eggpool-managed compaction by default;
- broad changes to provider routing solely for one coding client;
- automatic modification of unrelated user configuration without a narrow ownership marker and reversible merge.

---

# Expected plan set

- `199-responses-remote-compaction-compatibility.md`
- `200-agent-model-catalog-and-client-config-lifecycle.md`
- `201-codex-deferred-tool-compatibility-and-conformance.md`
- `202-status-command-and-provider-health-summary.md`

Plans 192–197 remain historical authority for the work they closed. Do not rewrite them.

---

# Verification strategy

Each child plan has focused checks. At roadmap closure, run at minimum:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release

git diff --check
```

For coding-agent compatibility, retain and extend:

```bash
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
```

Run the opt-in live Codex smoke when credentials/provider access are available. Add an OpenCode live/manual smoke only if it can remain optional and dependency-free.

---

# Roadmap acceptance criteria

This roadmap is complete when:

1. a user can generate and safely install/sync an Eggpool Codex provider plus model catalog without manually writing model entries;
2. the Codex catalog uses current schema and conservative Eggpool-derived model metadata;
3. OpenCode receives the same model limits/capabilities through its own current configuration format;
4. standard `/v1/models` remains OpenAI-compatible;
5. a current Codex `tool_search` loop either survives a supported translated upstream or fails before provider dispatch with an explicit unsupported-semantic error;
6. native Responses routes continue preserving current/future unknown Responses tool/events without re-encoding;
7. remote compaction has an exact, bounded compatibility path if enabled, while default HTTP/SSE Codex remains valid without it;
8. no server-side conversation persistence is added merely for current Codex HTTP/SSE use;
9. `eggpool status` reports proxy health and one line per configured upstream provider using authoritative cached/live state and no outbound probes;
10. human and JSON status output contain no credentials, raw provider bodies, prompts, cache keys, or unbounded error text;
11. all new client-sensitive behavior is protected by source-provenanced deterministic fixtures;
12. no Codex/OpenCodex/OpenCode production dependency is added.

---

# Handoff note

Before implementing any client-specific schema, re-check the current Codex/OpenCode source at implementation time. Their config/catalog schemas evolve faster than Eggpool's internal routing contracts. Keep the Eggpool projection stable and make version-sensitive behavior a renderer/test concern rather than a routing concern.

---

## Closure evidence (2026-09-16, Plans 206–208)

- Implemented: Plan 199 in `94b710e6`, Plan 200 in `7499c15e`, Plan 201 in
  `27890a19`, Plan 202 in `3dc9ece9`; per-plan closure records in
  `plans/203-agent-model-catalog-and-client-config-lifecycle-closure.md`,
  `plans/204-codex-deferred-tool-compatibility-and-conformance-closure.md`,
  `plans/205-status-command-and-provider-health-summary-closure.md`.
- Focused tests: `codex_compaction_compat`, `codex_responses_compat`,
  `operations_o005` + `operations::integrations`, `status_command` +
  `operations::status`, `cli_contract`, wire/coordinator suites; full serial
  workspace suite green at closure.
- Live qualification (Plan 206, Eggpool `0.8.0`, Codex CLI `0.154.0`,
  OpenCode `1.18.30`): managed Codex/OpenCode `--apply/--check/--sync/--remove`
  PASS (isolated); `codex debug models` + `codex doctor` PASS after narrow
  catalog fix (`supported_reasoning_levels` always present,
  `base_instructions = ""`; regression test
  `codex_catalog_emits_current_required_fields_for_unknown_models`);
  `opencode models` lists Eggpool models; `eggpool status` healthy/degraded/
  offline PASS with exit 0/0/3 and secret-free JSON; live inference SKIP (no
  provider creds, smoke exits 77); deferred `tool_search` live
  NOT_LIVE_EXERCISABLE (deterministic conformance retained); compaction uses
  local client path (remote remains optional, no translated fallback/state).
- Intentional deferrals: Responses WebSocket, persisted
  `previous_response_id`/conversations/background, server-side tool
  execution, OpenCodex-private parity, active probing from `status`.
