# Tool Integration M001 — Eggsearch backend configuration and readiness

Status: implemented

Repository baseline: `0e021c1ffe55803aff5f325033e65313dca9f1dc` (main at planning, 2026-11-03)

Source roadmap: plans/subsystems/tool-integration-roadmap.md#7-milestones (M001)

Long-term requirements: plans/000-long-term-specification.md §§1–5; plans/001-terminology-and-domain-model.md; plans/002-long-term-roadmap.md `4; plans/003-planning-process.md

Applicable ADR: plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (proposed; **M001 excludes every public tool route, grant and model execution feature**, so ADR approval is not a hard dependency for this operator-only milestone)

Primary class: capability

## 1. Objective

Create one opt-in, operator-owned connection/readiness contract for a local Eggsearch Streamable HTTP service, without embedding Eggsearch or exposing it to downstream clients through EggPool. A user can configure the backend, run an explicit diagnostic, and see deterministic outcomes for disabled, not installed/not reachable, wrong service/version, incompatible MCP contract and ready.

## 2. Why ready

No hard dependencies. Uses existing config, CLI, local operations, provider-neutral HTTP transport, and Eggsearch's published `eggsearch mcp serve --bind 127.0.0.1:11320 --path /mcp` contract. No ADR-level API/auth decision is made or published.

## 3. Implementation evidence

- rust/src/config.rs is operator TOML authority; rust/src/config_reload_policy.rs owns rehash-vs-restart classification. rust/config.example.toml and rust/config.sbc.example.toml document supported deployment profiles.
- rust/src/cli.rs and rust/src/operations/integrations.rs own existing client setup/status commands; rust/src/runtime.rs is their CLI adapter.
- rust/src/server/mod.rs owns existing inference routes, but M001 must not touch routing or HTTP ingress.
- Eggsearch docs/deployment.md, docs/service.md and docs/tool-matrix.md describe a loopback HTTP MCP service, `/healthz`, tool schemas, structured results, versioned protocol and bounded output. Do not assume metadata-only health implies a working `tools/list`.

## 4. Non-regression invariants

Tooling is disabled/missing by default; no implicit subprocess startup, listener, recurring probe, socket, or blocking request on ordinary inference. No new default/`--no-default-features` graph dependency. Configuration provider secrets remain operator-owned; identity/usage DB remains untouched. Existing endpoint behavior, agent setup, singleton SQLite gate, generation leases and reload/restart classification do not change.

## 5. Scope

**In scope:** an explicitly disabled-by-default one-backend config with typed backend ID, loopback URL and separate bounded connect/read/operation limits; explicit `eggpool tools doctor` and read-only backend status commands or equivalent existing CLI integration; checked service identity and MCP contract; typed secret-free diagnostics; documentation.

**Out:** MCP gateway, tool list available to users, identity grants, hosted execution, Rust SDK linking, arbitrary remote backends, auto-installing Eggsearch, CLI edits to Eggsearch-managed config, and feature flags enabling outbound search.

## 6. Required production changes

Add a small typed tool-backend config in rust/src/config.rs with strict validation of canonical `127.0.0.1`/`::1` HTTP loopback address, scheme, port and exact path; forbid URL userinfo, fragments, redirects, arbitrary DNS aliases, bind-all addresses, private-LAN and public destinations in this initial version. Config section omission must require zero work. Do not add a second HTTP server, and do not overtake any existing transport clients. Use an explicitly invoked diagnostic connection through the supported Eggfetch/Eggress transport boundary (inspect its current interface rather than inventing a transport). Probe `/healthz` with short fixed timeout plus a correctly framed MCP discovery/list negotiation when available; validate `service=eggsearch` and a compatible protocol/tool fingerprint. Never treat a successful TCP connect or `/healthz` alone as a ready tool backend. Add operator-friendly errors and a safe JSON status shape without leaking configured headers or responses. Add config examples only to opt-in documents or commented blocks.

Choose whether changes to backend identity/address can rehash safely using classify_transition, and document its actual decision; do not install parallel key lists. If runtime state is unnecessary for operator-only CLI, do not add it.

## 7. Ordered work packages

A. **Configuration and validation.** Add typed optional config and no-backend compatibility tests; test rejection of nonloopback, URL credentials, malformed URL, unsupported scheme and unexpected paths.

B. **Bounded probe and diagnostics.** Use a separate explicitly invoked operation with service identity and MCP version/catalog checks; distinguish unreachable, wrong service, incompatible catalog, malformed response and provider failure. Do not emit fetched content, tool arguments or secrets.

C. **Operator UX.** Integrate in rust/src/cli.rs and rust/src/operations/integrations.rs or the repo's then-current operation owner; document Eggsearch's own service lifecycle as the canonical setup path rather than reproducing its installer.

D. **Qualification.** Run with no Eggsearch process and one local Eggsearch service; inspect baseline RSS/startup/binary-size deltas and test forced rehash/restart behavior for the new config.

## 8. Failure, cancellation, restart and contention

Probe must timeout and terminate without creating a retained task, connection pool or PID ownership. An unavailable backend must not change EggPool health/readiness or provider routing. Concurrency across two CLI probes is read-only. Reload either atomically accepts validated backend configuration or rejects candidate; active inference never depends on an MCP service. Restart does not start Eggsearch.

## 9. Compatibility and migration

No DB migration, no new public HTTP route, no change in current shared inference or managed access flows. Single-user SBC installation still runs without Eggsearch. Version mismatch returns a truthful disabled/incompatible diagnostic; no silent fallback to an unverified SDK path.

## 10. Required tests

Add a focused backend-config/diagnostic integration target (e.g. rust/tests/tool_backend_config.rs); augment rust/tests/cli_contract.rs and rust/tests/server_transport.rs where surfaces are touched. Positive local mock contract plus missing executable/service, identity mismatch, wrong port, redirect, invalid URI, oversized discovery, timeout and no process spawning. Assert old config parses unchanged, and default request-serving behavior does not invoke any tool client.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Also run new focused target with `cargo test --manifest-path rust/Cargo.toml --test tool_backend_config -- --test-threads=1` and existing `--test cli_contract`; run a real local `eggsearch mcp serve` qualification separately and record its exact version and outcome.

## 12. Documentation

Update docs/cli-reference.md, docs/deployment.md, docs/configuration.md (or the current canonical operator config reference), rust/config.example.toml, rust/config.sbc.example.toml, and architecture/overview.md. Link Eggsearch's own installation/service docs. Do not claim a built-in MCP gateway.

## 13. Acceptance criteria

A clean default EggPool works byte-identically at public inference endpoints without Eggsearch. One explicit command distinguishes ready from all specified negative states and emits no secrets; configuration validation cannot target an arbitrary address. No default binary dependency or background task is added. No public tool operation exists yet.

## 14. Stop conditions

If current Eggfetch/Eggress cannot safely perform the bounded handshake without an upstream change, record the dependency and propose a separate transport plan. If an Eggsearch version cannot support negotiated catalog discovery, add an explicitly versioned compatibility fixture; do not broaden to blind JSON-RPC forwarding. Stop on any mandatory runtime or key-list drift.

## 15. Closure evidence

Commit IDs, exact diff, old/new config validation, connected and disconnected diagnostic fixtures, version/protocol proof, binary/RSS/startup comparison, default+no-default checks, security review, and documentation. Record all unrun live checks as operational limitations.

## 16. Handoff notes

Keep only M001 files within scope and preserve unrelated operator work. The registered next milestone M002 remains blocked on both this closure and ADR-0007 acceptance. Plan is authorization to implement configuration/readiness only; it does not claim a usable hosted tool service.
