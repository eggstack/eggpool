# EggPool Rust Migration Registry

Status: active

Planning baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

## Canonical documents

- [Long-term specification](000-long-term-specification.md)
- [Terminology and domain model](001-terminology-and-domain-model.md)
- [Long-term roadmap](002-long-term-roadmap.md)
- [Planning process](003-planning-process.md)

## Accepted ADRs

- [ADR-0001 — Side-by-side migration with Python as behavioral oracle](adrs/ADR-0001-side-by-side-python-oracle.md)
- [ADR-0002 — Rust runtime, HTTP stack, SSR parity, and implementation location](adrs/ADR-0002-rust-runtime-http-ssr.md)
- [ADR-0003 — Eggress in-process outbound connector replaces pproxy](adrs/ADR-0003-eggress-outbound-connector.md)

## Active subsystem roadmaps

| Subsystem | Roadmap | Status | Current milestone |
|---|---|---|---|
| Migration foundation | [foundation-roadmap](subsystems/foundation-roadmap.md) | corrective pass active | F006 |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| F006 | [Side-by-side safety and serve-contract closure](implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md) | invariant | F001-F005 closed | ready for handoff |

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| F001 | [Rust workspace and build scaffold](implementation/foundation/001-rust-workspace-and-build-scaffold.md) | infrastructure | [`573e081f`](https://github.com/eggstack/eggpool/commit/573e081f) | [closed](closure/foundation/001-status.md) |
| F002 | [Contract inventory and differential oracle harness](implementation/foundation/002-contract-inventory-and-oracle-harness.md) | invariant/infrastructure | [`a8c3621`](https://github.com/eggstack/eggpool/commit/a8c3621) | [closed](closure/foundation/002-status.md) |
| F003 | [Config and CLI compatibility foundation](implementation/foundation/003-config-and-cli-compatibility.md) | capability | [`5afbbdd`](https://github.com/eggstack/eggpool/commit/5afbbdd) | [closed](closure/foundation/003-status.md) |
| F004 | [SQLite schema and repository compatibility baseline](implementation/foundation/004-sqlite-schema-and-repository-baseline.md) | invariant/infrastructure | [`9cc9fc4`](https://github.com/eggstack/eggpool/commit/9cc9fc4) | [closed](closure/foundation/004-status.md) |
| F005 | [Axum SSR shell and static-asset parity baseline](implementation/foundation/005-axum-ssr-shell-and-static-assets.md) | capability | [`9d272b8`](https://github.com/eggstack/eggpool/commit/9d272b862cf8e7f55a8583ff51a020305922c431) | [closed](closure/foundation/005-status.md) |

## Blocked future work

M4 Provider HTTP + Eggress subsystem-roadmap drafting may proceed in parallel, but its implementation handoff must not be registered dependency-ready until F006 closes. The dual-run provider qualification work will depend on F006's guarantee that listener rejection cannot mutate durable state and that the supported Rust foreground server invocation is explicit.

Routing/quota/health, transcoding/SSE, coordinator/finalization, runtime generations, operational lifecycle, qualification, and cutover remain intentionally unrepresented by implementation handoff plans. They remain sequenced by `002-long-term-roadmap.md` and should receive repository-specific subsystem roadmaps/plans only as their correctness dependencies stabilize.

## Closure state

F001 through F005 remain closed on their recorded evidence. Post-closure review identified an integration defect not covered by F005's bind test: Rust currently opens/migrates/synchronizes SQLite before listener bind, so a bind failure can have durable side effects. It also identified silent migration-stage semantics for executable `serve` options, the currently deferred `server.threads` runtime meaning, and a layout-sensitive checksum parser.

F006 is the bounded corrective plan for those findings and is ready for handoff. Foundation must not be declared fully closed again until F006 has its own implementation and closure evidence.
