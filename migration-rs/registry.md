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
| M4 provider transport | [provider-transport-roadmap](subsystems/provider-transport-roadmap.md) | corrective pass active | T006 ready |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| T006 | [Extended proxy runtime interoperability closure](implementation/provider-transport/006-extended-proxy-runtime-qualification.md) | invariant/corrective | T001-T005 historical closure evidence; post-T005 review | ready for handoff |

The provider-transport sequence and status are also recorded under `implementation/provider-transport/README.md` and `000-handoff-sequence.md`.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| F001 | [Rust workspace and build scaffold](implementation/foundation/001-rust-workspace-and-build-scaffold.md) | infrastructure | [`573e081f`](https://github.com/eggstack/eggpool/commit/573e081f) | [closed](closure/foundation/001-status.md) |
| F002 | [Contract inventory and differential oracle harness](implementation/foundation/002-contract-inventory-and-oracle-harness.md) | invariant/infrastructure | [`a8c3621`](https://github.com/eggstack/eggpool/commit/a8c3621) | [closed](closure/foundation/002-status.md) |
| F003 | [Config and CLI compatibility foundation](implementation/foundation/003-config-and-cli-compatibility.md) | capability | [`5afbbdd`](https://github.com/eggstack/eggpool/commit/5afbbdd) | [closed](closure/foundation/003-status.md) |
| F004 | [SQLite schema and repository compatibility baseline](implementation/foundation/004-sqlite-schema-and-repository-baseline.md) | invariant/infrastructure | [`9cc9fc4`](https://github.com/eggstack/eggpool/commit/9cc9fc4) | [closed](closure/foundation/004-status.md) |
| F005 | [Axum SSR shell and static-asset parity baseline](implementation/foundation/005-axum-ssr-shell-and-static-assets.md) | capability | [`9d272b8`](https://github.com/eggstack/eggpool/commit/9d272b8) | [closed](closure/foundation/005-status.md) |
| F006 | [Side-by-side safety and serve-contract closure](implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md) | invariant | [`df902b5`](https://github.com/eggstack/eggpool/commit/df902b5) | [closed](closure/foundation/006-status.md) |
| T001 | [Provider transport contract and fixture freeze](implementation/provider-transport/001-contract-and-fixture-freeze.md) | invariant/infrastructure | [`50d7ff4`](https://github.com/eggstack/eggpool/commit/50d7ff4) | [closed](closure/provider-transport/001-status.md) |
| T002 | [Direct Hyper/Rustls provider HTTP core](implementation/provider-transport/002-direct-hyper-rustls-core.md) | infrastructure | [`c9f448a`](https://github.com/eggstack/eggpool/commit/c9f448a) + [`2696e52`](https://github.com/eggstack/eggpool/commit/2696e52) | [closed](closure/provider-transport/002-status.md) |
| T003 | [Eggress connector and proxy parity](implementation/provider-transport/003-eggress-connector-and-proxy-parity.md) | infrastructure/capability | [`5b34d8b`](https://github.com/eggstack/eggpool/commit/5b34d8b) | [historical closure](closure/provider-transport/003-status.md) |
| T004 | [Provider/account client pool and lifecycle boundary](implementation/provider-transport/004-provider-account-client-pool.md) | capability/invariant | [`71ef03d`](https://github.com/eggstack/eggpool/commit/71ef03d) | [closed](closure/provider-transport/004-status.md) |
| T005 | [Differential qualification and initial M4 closure](implementation/provider-transport/005-differential-qualification-and-closure.md) | invariant | [`c89e645`](https://github.com/eggstack/eggpool/commit/c89e645) | [historical closure](closure/provider-transport/005-status.md) |

## Post-T005 review finding

Independent review of the implementation and closure evidence found no defect in the direct Hyper/Rustls core, common HTTP/SOCKS Eggress paths, provider/account pool topology, timeout/cancellation ownership, fail-closed behavior, or dependency footprint.

It did find one acceptance-evidence gap: T001/T003 require mandatory proxy corpus rows to have runtime evidence or an approved supported-difference decision, while `shadowsocks-aead`, `ssr-legacy-cipher`, `trojan`, and `ssh` were closed with construction-only evidence because deterministic protocol peers were not bundled. T005 records this limitation explicitly, but no accepted ADR converts those mandatory rows to construction-only qualification.

T006 is the bounded corrective handoff. Historical T003/T005 closure records are retained unchanged for traceability; M4's aggregate closure state is reopened until T006 resolves the gap.

## Future work and block state

M5 catalog/account-registry/routing/quota/health roadmap research may proceed, but M5 implementation handoffs remain blocked on T006 closure. M5 must not treat the provider transport as a fully closed hard dependency until the mandatory extended proxy runtime rows are qualified or explicitly approved as supported differences.

M6 transcoding/SSE, M7 coordinator/finalization, M8 runtime generations, M9 operational lifecycle, M10 qualification, M11 cutover, and M12 Python retirement remain sequenced by `002-long-term-roadmap.md` and intentionally lack dependency-ready implementation handoffs.

## Closure state

F001-F006 remain closed and the migration foundation is complete. T001-T005 retain their individual closure evidence, but M4 is reopened for one corrective qualification milestone, T006.

T006 is now the only dependency-ready implementation plan. On accepted T006 closure, the registry should move it to completed, mark M4 closed after corrective pass, and explicitly unblock the M5 implementation planning sequence.
