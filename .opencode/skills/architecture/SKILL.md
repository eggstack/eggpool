---
name: architecture
description: Architecture principles and design decisions for the native Rust EggPool runtime.
---

# Architecture Skill

Read `architecture/README.md` and the relevant Rust deep dive before changing
runtime behavior. The repository-root `pyproject.toml`, `scripts/`, and
`tests/tooling/` are release/development tooling only.

## Core principles

- `rust/src/` owns current production behavior, `rust/crates/` owns explicitly
  reusable Rust boundaries, and `rust/assets/` owns embedded
  runtime data.
- Keep endpoint handling, coordinator, routing, persistence, provider
  transport, wire adaptation, and operations as explicit boundaries.
- Build complete immutable runtime-generation candidates before publication.
- Keep credentials, raw bodies, prompts, cache keys, and provider bodies out of
  persistence and diagnostics.
- Use the shared SQLite transaction/recovery contract; fail closed on commit or
  ownership ambiguity.
- Preserve the canonical wire intent and never chain translated payloads.
- For Responses admission, keep source-native preservation separate from the
  canonical IR. Same-surface forwarding may rewrite only EggPool-owned fields
  such as `model`; cross-surface codecs must reject native-only semantics or
  emit explicit bounded adaptation notices.
- Keep the provider-neutral tool distinction narrow: ordinary function tools
  remain unchanged, while Responses `custom` tools use
  `CanonicalToolKind::Freeform` and a deterministic function wrapper only on
  function-style targets. The per-request declaration, not a tool name alone,
  must drive unwrapping and downstream `custom_tool_call` reconstruction.
- For Responses streaming, select the explicit native-observed path only for
  Responses-to-Responses compatibility. Observe raw SSE incrementally and
  forward valid source bytes unchanged; use a separate bounded stateful encoder
  for cross-surface output, including completed output items, indexes, and
  distinct function-call/item identities.
- Treat `rust/Cargo.toml` and its locked resolved graph as the native dependency
  authority. Keep direct crates and non-default features tied to a live source,
  build, test, packaging, or documented compatibility owner.

## Verification pointers

- CLI/config/errors: `rust/src/cli.rs`, `rust/src/runtime.rs`, `rust/src/config.rs`, `rust/src/error.rs`
- Request path: `rust/src/request/`, `rust/src/coordinator/`; streaming internals
  are decomposed under `rust/src/coordinator/streaming/` with pre-handoff
  coordination, post-handoff execution, terminal classification, timeout,
  contract, and diagnostics modules behind the `mod.rs` facade.
- Semantic model routing: `rust/crates/eggpool-model-routing/` (neutral policy
  and identity), `rust/src/model_router.rs` (EggPool async affinity)
- Providers/wire: `rust/src/providers/`, `rust/src/wire/`
- Runtime/reload: `rust/src/runtime_lifecycle/` (process, generation, lease, manager, recovery, diagnostics), `rust/src/reload.rs`, `rust/src/config_reload_policy.rs`, `rust/src/operations/lifecycle.rs`
- HTTP adapters: `rust/src/server/mod.rs`, `rust/src/server/middleware.rs`, `rust/src/server/health.rs`, `rust/src/server/inference.rs`, `rust/src/server/dashboard.rs`
- Operations: `rust/src/operations/`
- Database/assets: `rust/src/db/`, `rust/assets/`

For streaming changes, preserve the handoff boundary: retries belong only to
`streaming/coordinator.rs` before `StreamingExecution` is returned;
`streaming/execution.rs` owns the single downstream body and cancellation path;
`streaming/terminal.rs` consumes wire terminal summaries and must not duplicate
wire event parsing. Streams remain incremental, translated Responses state is
bounded, and SSE transport EOF is not success without terminal evidence.
