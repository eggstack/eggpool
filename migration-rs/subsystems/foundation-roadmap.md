# Migration Foundation Subsystem Roadmap

Status: closed after corrective pass

Canonical source:

- `../000-long-term-specification.md`
- `../001-terminology-and-domain-model.md`
- `../002-long-term-roadmap.md#M0`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

## 1. Purpose and ownership

The foundation subsystem creates the side-by-side Rust build and the compatibility machinery needed to port EggPool safely. It owns build layout, parity harness conventions, contract inventory, initial config/CLI translation, SQLite compatibility baseline, the first Axum/SSR read-plane slice, and corrective safety work required to make that side-by-side boundary trustworthy.

It does not own provider dispatch, routing, transcoding, retries, runtime-generation semantics, production daemon lifecycle, or cutover.

## 2. Invariants

- Python remains runnable and unchanged by default.
- Rust lives under `rust/`; migration planning/evidence lives under `migration-rs/`.
- tests invoke implementations by explicit path and cannot accidentally compare Python to itself.
- normalization rules are narrow and reviewable.
- existing database/schema and config files remain the source compatibility boundaries.
- dashboard work ports SSR/static behavior; it does not redesign the frontend.
- dependencies are introduced only for an immediate foundation need.
- a rejected Rust startup must not mutate durable state before listener ownership is established.
- migration dual-run tests use distinct writable DB paths or equivalent copied fixtures rather than racing Python and Rust on one writable database.
- once a Rust command becomes executable, accepted options may not silently do nothing; unsupported migration-stage behavior must fail explicitly.

## 3. Current-state evidence

At the original planning baseline, EggPool was Python 3.11+ with FastAPI, Granian, HTTPX, aiosqlite, Pydantic, Click, optional orjson, and optional pproxy. It exposes a large Click command surface, a mature TOML/Pydantic config model, 54 numbered SQLite migrations, JSON/stats APIs, and server-rendered dashboard pages with static/theme resources.

F001 through F005 have now landed. The Rust candidate builds independently, has a differential oracle harness, parses the config and CLI surface, opens/upgrades the canonical SQLite schema, and runs a development-only Axum/SSR read plane.

Post-F005 review found one material startup-order defect and several smaller explicitness/robustness gaps. `rust/src/server.rs` currently opens/migrates/synchronizes SQLite before attempting `TcpListener::bind`, so a port collision can alter durable state before startup rejection. `serve` is now executable while its parsed `ServeArgs` are discarded. The binary intentionally remains current-thread Tokio even when `server.threads` differs, and `rust/build.rs` parses `checksums.json` with layout-sensitive line splitting. These findings are owned by corrective milestone F006.

The existing Python test suite remains authoritative regression coverage for the Python implementation. Migration tests supplement it with cross-implementation and side-effect assertions rather than duplicate all unit tests mechanically in Rust.

## 4. Dependency graph

```text
F001 Rust scaffold
   |
   +----> F002 oracle harness
              |
              +----> F003 config + CLI
              +----> F004 SQLite baseline
              +----> F005 Axum + SSR shell
                         |
                         +----> F006 side-by-side safety + serve-contract closure
                                      |
                                      +----> M4 provider HTTP + Eggress handoff may become ready
```

F003 and F004 proceeded in parallel after F002. F005 closed against those
stable interfaces. F006 was the corrective integration pass over
F003/F004/F005 and is now closed.

M4 subsystem-roadmap drafting and implementation handoff registration may now
proceed because F006 is closed.

## 5. Milestones

### F001 — Rust workspace and build scaffold (closed)

Class: infrastructure

Create the isolated Cargo package, minimal dependency policy, build/test commands, logging/error skeleton, and explicit executable-path invocation without changing Python packaging or install behavior.

Exit: `rust/target/.../eggpool` builds and runs a minimal version/help probe without replacing Python.

### F002 — Contract inventory and differential oracle harness (closed)

Class: invariant/infrastructure

Create the migration test runner, observation schema, normalization policy, deterministic fixtures, and inventory of compatibility surfaces.

Exit: at least one CLI, one config, one HTTP, one database, and one SSR/static observation can be collected from Python; Rust placeholders are wired so later cases can become true differential tests.

### F003 — Config and CLI compatibility foundation (closed)

Class: capability

Port config parsing/defaults/validation/path resolution and the complete CLI parser tree sufficiently to compare help/arguments/exit classes while underlying command implementations remain explicitly staged.

Exit: the supported config corpus and CLI parser contract pass differential gates.

### F004 — SQLite schema and repository compatibility baseline (closed)

Class: invariant/infrastructure

Open existing DBs, reuse migrations/checksums, establish serialized transactions, and port the first repositories needed by health/stats/SSR.

Exit: Python-created fixtures are readable by Rust and Rust fixture writes remain compatible with Python within the declared rollback boundary.

### F005 — Axum SSR shell and static asset parity baseline (closed)

Class: capability

Establish inbound server/auth/body-limit middleware, health/readiness, static assets, dashboard route skeleton, renderer utilities/escaping, and enough read-only endpoints/pages to verify the dashboard can be mirrored without redesign.

Exit: selected dashboard pages/static resources and health/readiness work from Rust and pass DOM/static/API differential checks.

Closure: [F005 closure record](../closure/foundation/005-status.md). The selected read-plane slice, static-resource guard, and operational evidence are complete; provider dispatch and the remaining dashboard are explicitly deferred to later subsystem roadmaps.

### F006 — Side-by-side safety and serve-contract closure (closed)

Class: invariant

Correct the post-F005 startup-order and command-contract gaps without expanding into later lifecycle/provider work.

Required outcomes:

- reserve the configured listener before any writable SQLite open/migration/account-sync side effect;
- prove occupied-port rejection leaves both nonexistent and existing DB fixtures unchanged;
- make `serve --verbose` the explicit supported migration-stage foreground path while daemon-oriented behavior is explicitly deferred rather than silently reinterpreted;
- ensure parsed `serve` options are implemented or explicitly rejected before side effects;
- keep `server.threads` config-compatible while visibly staged until the runtime milestone;
- replace the format-sensitive checksum-manifest parser with structural JSON parsing;
- make distinct writable DB paths/copies the documented and tested dual-run default.

Exit: startup rejection is side-effect-free, resource cleanup is bounded on post-bind failures, command/config staging is explicit, checksum parsing is formatting-robust, and F003/F004/F005 gates remain green.

Implementation plan: [F006 corrective closure](../implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md).

Closure record: [F006 closure](../closure/foundation/006-status.md).

## 6. Risks

The main foundation risks are over-normalizing behavioral differences, prematurely porting internal Python abstractions instead of contracts, duplicating static assets without drift control, making the initial Rust crate hierarchy more complex than the product needs, allowing side-by-side runs to share mutable state unintentionally, and presenting migration-stage CLI/config compatibility fields as implemented runtime behavior when they are still deferred.

## 7. Deferred work

Provider HTTP/Eggress transport, catalog/routing/quota/health, canonical request/transcoding/SSE, coordinator/finalization, runtime generations/rehash, full operational CLI/daemon lifecycle, packaging/cutover, and performance characterization are deferred to later subsystem roadmaps.

F006 must not absorb those systems. In particular, it does not implement Python daemon detach/PID files, provider clients, Eggress, multi-thread runtime generations, or stop/restart behavior.

## 8. Foundation exit condition

Foundation is closed after F006: the Rust implementation has a reproducible
build plus trustworthy differential machinery, independently satisfies the
config/CLI, SQLite baseline, and initial HTTP/SSR compatibility slices, and can
be started/rejected side-by-side without mutating durable state before listener
ownership or silently accepting unsupported command/runtime semantics.
