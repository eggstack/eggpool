# Plan 178 — Rust Maintenance Consolidation Roadmap

Date: 2026-09-12
Status: ready for handoff
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1/P2 maintenance / ownership consolidation / dependency drift control
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Consolidate EggPool's current Rust application after the completed migration and semantic-routing work without changing its product scope or rebuilding already-correct subsystems.

The repository is architecturally healthy, but several implementation files have accumulated multiple operational concerns, some current documentation and source comments still describe the retired Python/migration state, configuration change authority is spread across several modules, and the production dependency graph has no automated advisory-policy gate.

This roadmap is intentionally maintenance-oriented. It should make the existing behavior easier to reason about, review, and preserve. It must not become another migration, framework rewrite, or feature-expansion program.

Plans 168–172 remain closed. This roadmap does not reopen them; it addresses current-tree residue and maintenance pressure visible at the planning baseline.

## Baseline findings

### 1. The major runtime boundaries are conceptually sound

Keep these boundaries intact:

- `providers/transport.rs` owns HTTP/TLS/proxy transport, not credentials, routing, retries, wire semantics, or finalization.
- `wire/stream.rs` owns bounded stream framing/canonical stream semantics, not sockets, timeout policy, retry, cancellation, or durable finalization.
- `coordinator/streaming.rs` owns streaming request lifecycle, retry-window closure, downstream handoff, cancellation, timeout policy, and retained finalization.
- `rust/crates/eggpool-model-routing` owns neutral semantic model-routing policy/identity primitives, while EggPool retains selector execution and provider/account routing.

Do not merge these layers merely because adjacent files discuss the same request.

### 2. Control-plane implementation has become concentrated

At the baseline:

- `rust/src/server.rs` is about 114 KiB and combines server startup/shutdown, router construction, middleware, authentication/body admission, health/readiness, runtime status/statistics, dashboard HTTP/static handling, and inference endpoint adaptation.
- `rust/src/runtime.rs` is about 107 KiB and dispatches the CLI while also implementing substantial daemon, process, deployment, update, configuration, integration, and operator workflows already represented by `operations/*` services.
- `rust/src/runtime_lifecycle.rs` is about 89 KiB and `task_supervisor.rs` about 48 KiB.
- `coordinator/streaming.rs` is about 131 KiB.

File size alone is not a defect and is not a refactoring target. The target is clearer ownership where stable seams already exist.

### 3. Current-state documentation still contains migration/Python authority residue

Examples include:

- `rust/src/lib.rs` still says the Rust code is a side-by-side migration candidate and Python remains production authority.
- `rust/src/config.rs`, `server.rs`, and `runtime.rs` retain migration-candidate wording and phase-era commentary.
- active contributor/architecture documentation still contains Python-era `src/eggpool/*.py` runtime paths and dispatch descriptions.
- the canonical config example still describes `server.threads` in terms of Granian/asyncio even though the executable is Tokio `current_thread`.

Historical plans, migration history, changelog records, and stable test identifiers are not errors and should remain historical evidence.

### 4. Configuration change authority is fragmented but salvageable

The current separation is reasonable:

- `config.rs`: typed schema/defaults/load/validation;
- `config_reload_policy.rs`: reloadable/restart-required field policy and typed snapshots/diffs;
- `reload.rs`: candidate construction, generation publication, retirement, and transactional rehash;
- `operations/config_mutation.rs`: operator TOML edits, validation, and apply mode.

The maintenance goal is not to collapse these concerns into one file. It is to make them consume one canonical typed transition classifier so command mutation and live rehash cannot silently develop parallel restart/reload policy.

### 5. Dependency minimization is already complete enough

Plan 170 audited direct Rust dependencies/features, removed unused `tower-http` and the no-op Eggress `common` feature, and closed with the remaining production graph justified. The large transitive graph is primarily the cost of supported Eggress proxy/SSH/legacy compatibility.

Do not repeat dependency shaving. Instead add bounded automated advisory/license/source drift detection around the graph that is intentionally retained.

### 6. Duplicate config examples are unnecessary authorities

The repository-root config examples and `rust/assets/config/` copies are currently byte-identical committed files. `rust/build.rs` already has a deterministic generation pattern for embedded migration data, so configuration packaging can use the same one-authority principle rather than relying on two manually synchronized copies.

## Governing constraints

1. Preserve the public CLI, route paths, status codes, JSON/SSE shapes, authentication semantics, update/rollback behavior, database schema, configuration keys, and supported provider/proxy behavior unless an individual phase explicitly documents a compatibility correction.
2. Preserve the current single-thread Tokio runtime behavior. Do not silently reinterpret `server.threads` as a multithread scheduler setting.
3. Preserve transactional rehash: invalid candidates do not replace the current generation, restart-required changes do not partially apply, and old generations remain usable by in-flight leases until retirement.
4. Preserve coordinator retry/finalization invariants, especially the rule that no transparent replay occurs after downstream streaming handoff.
5. Do not merge `wire/stream.rs` into coordinator streaming or provider transport into routing.
6. Do not add a dependency-injection framework, command framework, actor system, generic service locator, plugin layer, or new family of micro-crates.
7. Do not split files to satisfy a line-count threshold. Split only along durable ownership seams.
8. Do not rename historical plans, changelog entries, migration-history material, or phase-coded regression tests solely to remove old terminology.
9. Keep Python limited to repository/release tooling. Do not restore a Python application runtime.
10. Keep CI proportionate to a local/LAN/SBC project. New dependency auditing should run on dependency changes, manually, and/or on a schedule rather than making every source-only commit materially slower.
11. No feature expansion is part of this line. Stateful Responses, embeddings/images/audio endpoints, Prometheus/OpenTelemetry, persistent semantic affinity, HA, distributed state, RBAC, and additional inbound security layers require separate product decisions.

## Execution sequence

Execute in this order unless a phase explicitly proves independence:

1. **Plan 179 — Current-State Truth and Canonical Config Assets**
   Correct active Rust/contributor/architecture descriptions, clarify the fixed single-thread compatibility semantics, and remove duplicated config-example authority.
2. **Plan 180 — CLI and HTTP Control-Plane Ownership Decomposition**
   Make `runtime.rs` a thin CLI adapter over existing operations services and split `server.rs` by stable HTTP/control-plane responsibility without altering routes or inference semantics.
3. **Plan 181 — Canonical Configuration Transition Authority**
   Make config mutation and live rehash consume one typed load/validate/classify contract while retaining transactional publication in `reload.rs`.
4. **Plan 182 — Runtime Generation and Task-Lifecycle Decomposition**
   Split the large lifecycle implementation around generation construction, publication/retirement, recovery, leases, and background-task ownership while keeping the same state machine.
5. **Plan 183 — Streaming Coordinator Internal Decomposition**
   Split the large streaming coordinator around existing lifecycle phases while preserving the wire/coordinator boundary and all handoff/finalization semantics.
6. **Plan 184 — Dependency Security and Drift Automation**
   Add lightweight Rust advisory/license/source policy and scheduled/dependency-change checks without reopening feature minimization.
7. **Plan 185 — Maintenance Consolidation Closure Pass**
   Re-audit ownership, current-state truth, duplicate authorities, dependency policy, focused invariants, and the full repository qualification surface.

Plans 180 and 181 may be implemented in separate commits, but Plan 181 should be rebased on the post-180 module layout if both touch CLI/config application paths. Plan 184 is behaviorally independent and may be implemented earlier if desired; its policy must still be re-run during Plan 185.

## Shared verification baseline

Each implementation phase should run its focused tests plus, before handoff/merge, the repository baseline:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Run packaging/release/update validators only when a phase touches their inputs or contracts; do not expand every maintenance commit into a public-release rehearsal.

## Definition of done

This roadmap is complete when active documentation/source describe the actual Rust runtime, canonical config examples have one authority, CLI/HTTP/lifecycle/streaming code is divided along existing ownership seams, configuration changes use one typed transition policy, the dependency graph is continuously checked for relevant security/license/source drift, all existing behavior remains qualified, and no unnecessary framework or product feature has been introduced.

The final repository should be easier to maintain because ownership is clearer, not because functionality has been redistributed into more abstractions.