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

## Subsystem roadmaps

| Subsystem | Roadmap | Status | Current milestone |
|---|---|---|---|
| Migration foundation | [foundation-roadmap](subsystems/foundation-roadmap.md) | closed after F006 corrective pass | F006 closed |
| M4 provider transport | [provider-transport-roadmap](subsystems/provider-transport-roadmap.md) | active | T001 ready |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| T001 | [Provider transport contract and fixture freeze](implementation/provider-transport/001-contract-and-fixture-freeze.md) | invariant/infrastructure | F001-F006 closed | ready for handoff |

## Registered queued plans

These plans are part of the reviewed M4 workstream but are not dependency-ready until their hard predecessors close.

| ID | Plan | Class | Hard dependency | Status |
|---|---|---|---|---|
| T002 | [Direct Hyper/Rustls provider HTTP core](implementation/provider-transport/002-direct-hyper-rustls-core.md) | infrastructure | T001 | queued |
| T003 | [Eggress connector and proxy parity](implementation/provider-transport/003-eggress-connector-and-proxy-parity.md) | infrastructure/capability | T002 plus T001 Eggress feature decision | queued |
| T004 | [Provider/account client pool and lifecycle boundary](implementation/provider-transport/004-provider-account-client-pool.md) | capability/invariant | T003 | queued |
| T005 | [Differential qualification and M4 closure](implementation/provider-transport/005-differential-qualification-and-closure.md) | invariant | T004 | queued |

The sequence and handoff index are also recorded under `implementation/provider-transport/README.md` and `000-handoff-sequence.md`.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| F001 | [Rust workspace and build scaffold](implementation/foundation/001-rust-workspace-and-build-scaffold.md) | infrastructure | [`573e081f`](https://github.com/eggstack/eggpool/commit/573e081f) | [closed](closure/foundation/001-status.md) |
| F002 | [Contract inventory and differential oracle harness](implementation/foundation/002-contract-inventory-and-oracle-harness.md) | invariant/infrastructure | [`a8c3621`](https://github.com/eggstack/eggpool/commit/a8c3621) | [closed](closure/foundation/002-status.md) |
| F003 | [Config and CLI compatibility foundation](implementation/foundation/003-config-and-cli-compatibility.md) | capability | [`5afbbdd`](https://github.com/eggstack/eggpool/commit/5afbbdd) | [closed](closure/foundation/003-status.md) |
| F004 | [SQLite schema and repository compatibility baseline](implementation/foundation/004-sqlite-schema-and-repository-baseline.md) | invariant/infrastructure | [`9cc9fc4`](https://github.com/eggstack/eggpool/commit/9cc9fc4) | [closed](closure/foundation/004-status.md) |
| F005 | [Axum SSR shell and static-asset parity baseline](implementation/foundation/005-axum-ssr-shell-and-static-assets.md) | capability | [`9d272b8`](https://github.com/eggstack/eggpool/commit/9d272b8) | [closed](closure/foundation/005-status.md) |
| F006 | [Side-by-side safety and serve-contract closure](implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md) | invariant | [`df902b5`](https://github.com/eggstack/eggpool/commit/df902b5) | [closed](closure/foundation/006-status.md) |

## Future work and block state

M5 catalog/account registry/routing/quota/health implementation handoffs remain blocked on M4/T005 closure. M5 roadmap research may proceed against the canonical long-term roadmap, but it should not assume provider HTTP/proxy details until T005 records the stable handoff interface and error taxonomy.

M6 transcoding/SSE, M7 coordinator/finalization, M8 runtime generations, M9 operational lifecycle, M10 qualification, M11 cutover, and M12 Python retirement remain sequenced by `002-long-term-roadmap.md` and intentionally lack dependency-ready implementation handoffs.

## Closure state

F001 through F006 are closed and the migration foundation is complete. F006's implementation and state-based regressions establish a trustworthy dual-run startup boundary. M4 Provider HTTP + Eggress is now the active line of work.

T001 is the only dependency-ready M4 plan. It must freeze the real Python provider/proxy contract and Eggress feature subset before transport dependencies are added. T002-T005 are registered in dependency order so the complete line of work is visible without allowing later implementation to bypass closure evidence.