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
| M4 provider transport | [provider-transport-roadmap](subsystems/provider-transport-roadmap.md) | closed after T006 corrective pass | T006 closed |
| M5 routing domain/catalog state | [routing-domain-roadmap](subsystems/routing-domain-roadmap.md) | **active corrective pass after D008** | **D009 ready** |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| D009 | [Selection fairness and frozen routing-trace correction](implementation/routing-domain/009-selection-fairness-and-trace-snapshot-correction.md) | invariant/corrective | D001-D008 implemented/closed; independent post-closure review | **ready for handoff** |

The M5 sequence and status are also recorded under `implementation/routing-domain/README.md` and `000-handoff-sequence.md`.

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
| T006 | [Extended proxy runtime interoperability closure](implementation/provider-transport/006-extended-proxy-runtime-qualification.md) | invariant/corrective | [`4b3a95a`](https://github.com/eggstack/eggpool/commit/4b3a95a) | [closed](closure/provider-transport/006-status.md) |
| D001 | [Routing-domain contract and deterministic fixture freeze](implementation/routing-domain/001-contract-and-fixture-freeze.md) | invariant/infrastructure | [`40be1bf`](https://github.com/eggstack/eggpool/commit/40be1bf) | [closed](closure/routing-domain/001-status.md) |
| D002 | [Account registry and catalog cache/hydration](implementation/routing-domain/002-account-registry-and-catalog-cache.md) | capability/invariant | [`966ca1b`](https://github.com/eggstack/eggpool/commit/966ca1b) + [`4110d23`](https://github.com/eggstack/eggpool/commit/4110d23) + [`3916c84`](https://github.com/eggstack/eggpool/commit/3916c84) + [`b661705`](https://github.com/eggstack/eggpool/commit/b661705) | [closed](closure/routing-domain/002-status.md) |
| D003 | [Catalog refresh, normalization, and persistence](implementation/routing-domain/003-catalog-refresh-normalization-and-persistence.md) | capability/invariant | [`c956e89`](https://github.com/eggstack/eggpool/commit/c956e89) | [closed](closure/routing-domain/003-status.md) |
| D004 | [Quota, claims, and fair-share scoring](implementation/routing-domain/004-quota-claims-and-fair-scoring.md) | capability/invariant | [`d649e8a`](https://github.com/eggstack/eggpool/commit/d649e8a) | [closed](closure/routing-domain/004-status.md) |
| D005 | [Health, backoff, circuit, and quarantine](implementation/routing-domain/005-health-backoff-circuit-and-quarantine.md) | invariant/capability | [`d5dd16d`](https://github.com/eggstack/eggpool/commit/d5dd16d) | [closed](closure/routing-domain/005-status.md) |
| D006 | [Routing eligibility, fairness, and local claims](implementation/routing-domain/006-routing-eligibility-fairness-and-claims.md) | capability/invariant | [`b009023`](https://github.com/eggstack/eggpool/commit/b009023) | [historical closure](closure/routing-domain/006-status.md) |
| D007 | [Model-router registry and affinity](implementation/routing-domain/007-model-router-registry-and-affinity.md) | capability/invariant | [`43ce484`](https://github.com/eggstack/eggpool/commit/43ce484) | [closed](closure/routing-domain/007-status.md) |
| D008 | [Differential qualification and initial M5 closure](implementation/routing-domain/008-differential-qualification-and-closure.md) | invariant | [`477aade`](https://github.com/eggstack/eggpool/commit/477aade) | [historical aggregate closure](closure/routing-domain/008-status.md) |

D009 is not listed as completed until a new `closure/routing-domain/009-status.md` is accepted.

## M5 planned sequence

| ID | Plan | Dependency state |
|---|---|---|
| D001 | [Contract and deterministic fixture freeze](implementation/routing-domain/001-contract-and-fixture-freeze.md) | closed |
| D002 | [Account registry and catalog cache/hydration](implementation/routing-domain/002-account-registry-and-catalog-cache.md) | closed |
| D003 | [Catalog refresh, normalization, and persistence](implementation/routing-domain/003-catalog-refresh-normalization-and-persistence.md) | closed; see [closure](closure/routing-domain/003-status.md) |
| D004 | [Quota, claims, and fair-share scoring](implementation/routing-domain/004-quota-claims-and-fair-scoring.md) | closed; see [closure](closure/routing-domain/004-status.md) |
| D005 | [Health, backoff, circuit, and quarantine](implementation/routing-domain/005-health-backoff-circuit-and-quarantine.md) | closed |
| D006 | [Routing eligibility, fairness, and local claims](implementation/routing-domain/006-routing-eligibility-fairness-and-claims.md) | historical closure; D009 corrects uncovered selection evidence |
| D007 | [Model-router registry and affinity](implementation/routing-domain/007-model-router-registry-and-affinity.md) | closed; see [closure](closure/routing-domain/007-status.md) |
| D008 | [Differential qualification and initial M5 closure](implementation/routing-domain/008-differential-qualification-and-closure.md) | historical aggregate closure; superseded for D009 findings only |
| D009 | [Selection fairness and frozen routing-trace correction](implementation/routing-domain/009-selection-fairness-and-trace-snapshot-correction.md) | **ready for handoff** |

Only the dependency-ready table authorizes implementation handoff. The planned-sequence table documents historical and future work without weakening closure gates.

## D009 independent-review findings

Two mandatory M5 selection-contract defects were found after D008:

1. Rust `RoutingRouter::select_and_claim` invokes fairness ordering in non-committing mode. For `FairnessMode::Random`, that mode returns identity order rather than invoking `FairnessRandom::choose_index`, so configured random fairness does not currently affect actual accepted selection. Existing D006 routing-claim tests are round-robin focused and did not exercise this branch.
2. Rust `SelectionClaim` does not retain the accepted score/fairness candidate snapshot. `RoutingRouter::trace_for` rebuilds a plan after claim publication; active/pending load can already have changed scores/order, allowing trace evidence to describe a state that did not produce the selected account.

D009 fixes these together because both belong to the same accepted-selection boundary. No broad M5 redesign is authorized.

## M5 boundary decisions

M5 consumes the closed M4 `ProviderClientPool`/`ProviderHttpClient` only for routing-essential provider catalog discovery. It does not reopen HTTP/TLS/proxy design.

The deprecated Python in-memory `ReservationManager` is not a Rust production target. M5 preserves the SQLite reservation representation plus bounded quota estimator mirrors and provisional local selection claims.

D006/D009 own the local atomic selection-claim transition (selection/revalidation, fairness decision, circuit probe, active ownership, pending quota load, frozen accepted-decision snapshot). Durable inference request/reservation/attempt publication, retry/failover, compensation, and finalization remain M7.

Python `ModelRouterSelector` calls `RequestCoordinator`; therefore D007 ports compiled virtual-router policy and bounded affinity/single-flight state only. Real semantic selector inference is M7. M6 will later adapt canonical requests into the M5 request-independent routing/affinity DTOs.

Optional generic external catalog/model-info polling is not allowed to introduce a second HTTP stack in M5; deterministic resolver logic may use fixtures/persisted metadata, while periodic generic outbound lifecycle remains M8.

## Future work and block state

D009 is the sole dependency-ready implementation plan.

M6 canonical request/codec/transcoding/SSE research and planning may continue, but **M6 implementation handoff is blocked until accepted D009 closure** re-establishes a stable M5 routing-domain interface.

M7 coordinator/finalization remains additionally blocked on M6. M8 runtime generations/background lifecycle, M9 operational lifecycle, M10 qualification, M11 cutover, and M12 retirement remain sequenced by `002-long-term-roadmap.md`.

## Closure state

F001-F006 and M4 T001-T006 are closed. M5 D001-D008 retain their historical closure records, but M5 is reopened for D009 and is not currently considered fully closed.

On accepted D009 closure, mark M5 `closed after D009 corrective pass`, move D009 to the completed table, and re-unblock M6 implementation handoff. If D009 discovers a separate material issue outside its defined selection boundary, create another bounded corrective plan rather than expanding D009 opportunistically.
