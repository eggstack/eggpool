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
  distinct function-call/item identities. Interleaved translated calls must
  remain keyed by source index/call identity, and later tool outputs must be
  paired by `call_id` rather than output-item order.
- Routing is deterministic and load-based, never cost-based. Selector (virtual
  model) decisions happen before provider/account routing and cannot pin an
  account, bypass health/quota, or reselect after submission.
- Treat `rust/Cargo.toml` and its locked resolved graph as the native dependency
  authority. Keep direct crates and non-default features tied to a live source,
  build, test, packaging, or documented compatibility owner.
- Treat `configsetup` renderers as format-specific delivery boundaries: a
  target is secret-bearing only when its rendered artifact embeds the resolved
  key. Codex emits an HTTP/SSE Responses TOML block with
  `env_key = "EGGPOOL_API_KEY"` and OpenCode uses `{env:EGGPOOL_API_KEY}`
  with the Responses-capable `@ai-sdk/openai` runtime, so both are printable
  by default. Codex picker discovery uses an EggPool-owned generated
  `model_catalog_json` built from the conservative provider-neutral
  projection (minimum limits, intersected capabilities, unknown stays
  unknown, no name inference, no WebSocket advertisement); `/v1/models`
  remains the standard OpenAI schema.

## Verification pointers

- CLI/config/errors: `rust/src/cli.rs`, `rust/src/runtime.rs`, `rust/src/config.rs`, `rust/src/error.rs`, `rust/src/lib.rs` (module map), `rust/src/version.rs`
- Request path: `rust/src/request/` (`admission.rs`, `body.rs`, `limits.rs`), `rust/src/coordinator/` (`finite.rs`, `attempt.rs`, `publication.rs`, `finalization.rs`, `failure.rs`, `wire_resolver.rs`, `endpoints.rs`, `semantic.rs`, `reconciliation.rs`); streaming internals
  are decomposed under `rust/src/coordinator/streaming/` as `coordinator.rs` (pre-handoff),
  `execution.rs` (post-handoff), `terminal.rs` (terminal classification), `timeout.rs` (policy),
  `types.rs` (contracts), `diagnostics.rs` (bounded observation) behind the `mod.rs` facade.
- Routing/accounting: `rust/src/routing/` (`router.rs`, `eligibility.rs`, `fairness.rs`, `claim.rs`), `rust/src/accounts/registry.rs`, `rust/src/catalog/` (`cache.rs`, `refresh.rs`), `rust/src/quota/` (`state.rs`, `estimator.rs`, `scorer.rs`), `rust/src/health/` (`health_manager.rs`, `backoff.rs`, `circuit_breaker.rs`, `effects.rs`, `quarantine.rs`, `repository.rs`)
- Semantic model routing: `rust/crates/eggpool-model-routing/` (neutral policy
  and identity), `rust/src/model_router.rs` (EggPool async affinity)
- Providers/wire: `rust/src/providers/` (`transport.rs`, `client_pool.rs`), `rust/src/wire/` (`ir.rs`, `codec.rs`, `codecs.rs`, `additional_codecs.rs`, `registry.rs`, `runtime.rs`, `stream.rs`, `adaptation.rs`)
- Runtime/reload: `rust/src/runtime_lifecycle/` (process, generation, lease, manager, recovery, diagnostics), `rust/src/reload.rs`, `rust/src/config_reload_policy.rs`, `rust/src/operations/lifecycle.rs`, `rust/src/task_supervisor.rs`
- HTTP adapters: `rust/src/server/mod.rs`, `rust/src/server/middleware.rs`, `rust/src/server/health.rs`, `rust/src/server/inference.rs`, `rust/src/server/dashboard.rs`
- Operations: `rust/src/operations/` (`lifecycle.rs`, `process.rs`, `paths.rs`, `control.rs`, `config_mutation.rs`, `deploy.rs`, `backup.rs`, `update.rs`, `catalog.rs`, `provenance.rs`, `operator.rs`, `metrics.rs`, `integrations.rs`)
- Database/assets: `rust/src/db/` (`connection.rs`, `migrations.rs`, `repositories.rs`), `rust/assets/db/migrations/` (v1–v54, immutable), `rust/crates/eggpool-model-routing/src/` (`policy.rs`, `identity.rs`, `lib.rs`)

Start routing changes at `architecture/deep-dive-routing.md`, provider/transport
changes at `architecture/deep-dive-providers.md`, and reload changes at
`architecture/deep-dive-control.md` + `deep-dive-runtime.md`. The review index in
`architecture/overview.md` is the module-to-deep-dive map; do not duplicate it here.

For streaming changes, preserve the handoff boundary: retries belong only to
`streaming/coordinator.rs` before `StreamingExecution` is returned;
`streaming/execution.rs` owns the single downstream body and cancellation path;
`streaming/terminal.rs` consumes wire terminal summaries and must not duplicate
wire event parsing. Streams remain incremental, translated Responses state is
bounded, and SSE transport EOF is not success without terminal evidence.
