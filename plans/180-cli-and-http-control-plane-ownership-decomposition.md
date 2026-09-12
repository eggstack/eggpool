# Plan 180 — CLI and HTTP Control-Plane Ownership Decomposition

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1 maintainability / ownership boundaries
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Reduce maintenance blast radius in the two largest application adapters—`runtime.rs` and `server.rs`—by moving already-separated responsibilities behind their existing owners and splitting the HTTP surface by durable concern.

This is a behavior-preserving decomposition. It must not redesign command semantics, daemon safety, HTTP routing, authentication, dashboard behavior, coordinator ownership, or protocol translation.

## Current-state findings

### `runtime.rs` is both CLI adapter and operational implementation

The top-level `run(cli)` dispatch is a good authority for mapping Clap commands to EggPool behavior. The file also implements substantial mechanisms for:

- daemon start/detach/log handling;
- stop/restart/process identity proof;
- watchdog and runtime status;
- update/provenance coordination;
- config mutation and onboarding;
- deployment rendering/installation;
- integration configuration;
- model/catalog/statistics/operator commands.

This overlaps with the existing `operations/*` package, whose modules already own reusable process, control, deploy, update, backup, config-mutation, integration, metrics, operator, catalog, provenance, and path services.

`operations/deploy.rs` explicitly states that its service layer owns deterministic deployment mechanisms while the CLI owns prompts/presentation. Preserve that distinction.

### `server.rs` owns several unrelated HTTP/control-plane surfaces

The file currently combines:

- process/server startup and graceful shutdown;
- listener/router construction;
- runtime generation/control-socket attachment;
- auth middleware;
- bounded request-body extraction/admission;
- health/readiness/runtime status/statistics;
- dashboard HTML/static assets/themes/API;
- Chat Completions/Responses/Messages request handlers;
- conversion from coordinator execution to Axum finite/streaming responses.

The server-to-coordinator boundary is already correct: inference execution belongs in the coordinator. This phase should make that boundary more visible, not move coordinator logic back into HTTP handlers.

## Target ownership

### CLI side

Keep `runtime` responsible for:

- initializing process-local CLI diagnostics;
- resolving the configured path;
- matching `Command` variants;
- interactive prompts and human/JSON presentation that are inherently CLI-specific;
- mapping operation/service errors to stable CLI exit codes/messages.

Move reusable mechanisms to existing `operations` owners. Where a complete workflow has no suitable owner, add **one narrowly named operations module** rather than creating a generic command framework. The strongest candidate is a local server lifecycle service built on `operations::process`, `paths`, and `control` for safe spawn/stop/restart/identity proof.

Do not move CLI printing/prompts into services simply to shrink `runtime.rs`.

### HTTP side

Convert the monolithic server implementation into a `server/` module with a small `mod.rs` and internal modules based on actual responsibility. A reasonable target is:

```text
server/
  mod.rs              # run/run_with_digest, shared state, top-level assembly
  router.rs           # route construction only, if useful
  middleware.rs       # auth and body/admission HTTP middleware
  inference.rs        # public inference handlers + Axum/coordinator adaptation
  health.rs           # liveness/readiness/status endpoints
  dashboard.rs        # dashboard routes/static assets/theme API
```

Exact filenames may differ. Avoid tiny one-function files. Keep route declarations close enough to handlers that route ownership remains discoverable.

## Governing constraints

1. Preserve every public CLI command, option, exit code, prompt-visible safety behavior, and default path.
2. Preserve daemon safety invariants: PID files are advisory, process signaling requires independent EggPool identity evidence, stale PID recovery remains bounded, detached log files remain private, and root execution policy remains unchanged.
3. Preserve `operations::process` as the primitive process-safety owner; do not duplicate PID/TERM/probe logic in a new service.
4. Preserve `operations::deploy` as deployment mechanism authority and keep its fake-runner testability.
5. Preserve all public HTTP routes, methods, status codes, auth requirements, body-size limits, and content types.
6. Dashboard-public authentication exceptions must remain exactly as documented/configured.
7. Inference handlers remain thin adapters into coordinator execution. Do not duplicate routing/retry/transcoding/finalization in `server`.
8. Do not move SSE parsing/terminal interpretation from `wire` or streaming lifecycle from `coordinator` into Axum handlers.
9. Do not introduce a generic command bus, trait-object service registry, dependency-injection container, or alternate web framework.
10. Module decomposition is successful because ownership is clearer, not because files fall below an arbitrary size.

## Workstream A — Freeze public adapter contracts

Before moving code, inventory:

- all `Command` variants in `cli.rs` and their runtime destinations;
- exit-code constants and where each is emitted;
- daemon/process lifecycle functions in `runtime.rs`;
- all Axum routes and their auth/public semantics;
- `ServerState`, health state, request-body helpers, runtime-status data, and dashboard static assets;
- tests covering CLI contracts, operations O002–O010, health, coordinator HTTP publication, and runtime lifecycle.

Add focused golden/contract assertions only if a public route/command is not already protected. Do not build a second route manifest merely for this refactor.

## Workstream B — Extract local server lifecycle mechanisms from CLI dispatch

Create or extend an operations service around the existing primitives for:

- start-safety checks;
- detached child spawning and private log setup;
- independent process identity proof;
- safe stop/restart;
- restart-after-config-mutation;
- watchdog/ensure-running lifecycle mechanics where currently duplicated.

The service should return typed outcomes suitable for both CLI presentation and config-mutation callers. `runtime.rs` should render the human messages and convert failures to existing exit codes.

Do not weaken the current requirement that TERM is sent only after PID-file match plus health/control evidence.

## Workstream C — Route CLI commands to existing operations services

For each command family, remove mechanism logic from `runtime.rs` when an existing service already owns it:

- config mutation/onboarding -> `operations::config_mutation`;
- update/install provenance -> `operations::update` / `provenance`;
- deployment/uninstall -> `operations::deploy`;
- backup/recover -> `operations::backup`;
- model/catalog -> `operations::catalog`;
- stats/repair -> `operations::metrics`;
- integrations -> `operations::integrations`;
- account/operator helpers -> `operations::operator` and relevant account services;
- process/control -> `operations::process` / `control` / the narrowly extracted lifecycle workflow.

Do not force functions into an unrelated module merely to reduce imports. If a CLI-only formatting helper is not reusable, leave it in the runtime adapter.

The desired end-state is a readable `run(cli)` plus small presentation/adaptation helpers, not a zero-logic façade.

## Workstream D — Split HTTP server by surface

Move code mechanically in small commits:

1. extract dashboard static/theme/API code;
2. extract health/readiness/runtime-status/statistics handlers;
3. extract auth/body middleware;
4. extract inference handlers and finite/streaming Axum adaptation;
5. leave startup/router/shared state in `server/mod.rs`, extracting router assembly only if it remains large enough to obscure startup ownership.

Prefer `pub(super)`/private module visibility. Do not broaden internal server APIs to `pub` simply because files moved.

## Workstream E — Preserve server/coordinator publication semantics

Pay special attention to the exact point where coordinator ownership transfers to Axum streaming bodies:

- finite responses keep their current header/body/status behavior;
- streaming responses must retain their downstream cancellation notification path;
- dropping a downstream body must still communicate the correct `DownstreamResult` to coordinator finalization;
- no post-handoff server error may re-enter the coordinator retry loop;
- request-size/auth errors remain local and occur before expensive request execution.

No changes to wire codecs or provider transport are required for this plan.

## Workstream F — Update architecture/navigation

After the moves, update:

- `AGENTS.md` current implementation pointers;
- relevant architecture current-state pages;
- module comments affected by the path changes.

Do not create a new architectural layer in documentation that does not exist in code.

## Focused verification

Run the tests most likely to detect adapter regressions during implementation:

```bash
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o010 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
```

Then run the roadmap-wide Rust/Tooling verification baseline, including strict Clippy and the complete workspace suite.

## Acceptance criteria

- `runtime` is primarily command dispatch, CLI-specific interaction/presentation, and error/exit-code adaptation.
- Reusable start/stop/restart/watchdog mechanisms are owned once by operations services using the existing safe process primitives.
- `server` is a directory/module with clearly separated dashboard, health/status, middleware, inference-adapter, and startup/router concerns.
- Public CLI and HTTP contracts are unchanged.
- Coordinator remains the sole inference lifecycle/retry/finalization owner and wire remains the stream codec/terminal-evidence owner.
- No new generic framework or unnecessary dependency is introduced.
- Existing focused and full regression suites remain green.

## Handoff note

Perform this as mechanical extraction with behavior tests between moves. If a move reveals duplicated logic, consolidate it under the already-established owner; do not opportunistically redesign semantics in the same commit.