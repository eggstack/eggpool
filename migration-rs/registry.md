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
| Migration foundation | [foundation-roadmap](subsystems/foundation-roadmap.md) | closed after F006 corrective pass | F006 closed |

## Dependency-ready implementation plans

No dependency-ready implementation plans are currently registered. M4
Provider HTTP + Eggress handoff may be registered after its own roadmap and
implementation plan are reviewed; F006 no longer blocks that registration.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| F001 | [Rust workspace and build scaffold](implementation/foundation/001-rust-workspace-and-build-scaffold.md) | infrastructure | [`573e081f`](https://github.com/eggstack/eggpool/commit/573e081f) | [closed](closure/foundation/001-status.md) |
| F002 | [Contract inventory and differential oracle harness](implementation/foundation/002-contract-inventory-and-oracle-harness.md) | invariant/infrastructure | [`a8c3621`](https://github.com/eggstack/eggpool/commit/a8c3621) | [closed](closure/foundation/002-status.md) |
| F003 | [Config and CLI compatibility foundation](implementation/foundation/003-config-and-cli-compatibility.md) | capability | [`5afbbdd`](https://github.com/eggstack/eggpool/commit/5afbbdd) | [closed](closure/foundation/003-status.md) |
| F004 | [SQLite schema and repository compatibility baseline](implementation/foundation/004-sqlite-schema-and-repository-baseline.md) | invariant/infrastructure | [`9cc9fc4`](https://github.com/eggstack/eggpool/commit/9cc9fc4) | [closed](closure/foundation/004-status.md) |
| F005 | [Axum SSR shell and static-asset parity baseline](implementation/foundation/005-axum-ssr-shell-and-static-assets.md) | capability | [`9d272b8`](https://github.com/eggstack/eggpool/commit/9d272b862cf8e7f55a8583ff51a020305922c431) | [closed](closure/foundation/005-status.md) |
| F006 | [Side-by-side safety and serve-contract closure](implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md) | invariant | [`df902b5`](https://github.com/eggstack/eggpool/commit/df902b5) | [closed](closure/foundation/006-status.md) |

## Future work and unblock state

M4 Provider HTTP + Eggress subsystem-roadmap drafting and implementation
handoff registration are now unblocked by foundation. No M4 roadmap or
implementation plan is currently represented in this registry, so there is no
future plan status to update. Its dual-run provider qualification work can use
F006's guarantee that listener rejection cannot mutate durable state and that
the supported Rust foreground server invocation is explicit.

Routing/quota/health, transcoding/SSE, coordinator/finalization, runtime generations, operational lifecycle, qualification, and cutover remain intentionally unrepresented by implementation handoff plans. They remain sequenced by `002-long-term-roadmap.md` and should receive repository-specific subsystem roadmaps/plans only as their correctness dependencies stabilize.

## Closure state

F001 through F006 are closed on their recorded evidence. F006 corrected the
post-F005 startup-order defect, made migration-stage serve behavior explicit,
made the deferred `server.threads` meaning visible, and replaced the
layout-sensitive checksum parser. Foundation is closed; later subsystem work
remains unrepresented until its own roadmap and handoff are reviewed.
