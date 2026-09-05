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
| M5 routing domain/catalog state | [routing-domain-roadmap](subsystems/routing-domain-roadmap.md) | closed after D009 corrective pass | D009 closed |
| M6 canonical request/wire codecs | [canonical-wire-roadmap](subsystems/canonical-wire-roadmap.md) | **active** | **W008 ready** |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| W008 | [SSE, canonical stream events, usage, and terminal evidence](implementation/canonical-wire/008-sse-stream-events-usage-and-terminal-evidence.md) | capability/invariant | W007 closed | **ready for handoff** |

The M6 sequence and dependency state are also recorded under `implementation/canonical-wire/README.md` and `000-handoff-sequence.md`.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| W001 | [Canonical wire contract and deterministic fixture freeze](implementation/canonical-wire/001-contract-and-fixture-freeze.md) | invariant/infrastructure | [`52f1dfac`](https://github.com/eggstack/eggpool/commit/52f1dfac) | [closed](closure/canonical-wire/001-status.md) |
| W002 | [Canonical IR, request admission, limits, and M5 fact bridge](implementation/canonical-wire/002-canonical-ir-request-admission-and-limits.md) | capability/invariant | [`2096727b`](https://github.com/eggstack/eggpool/commit/2096727b) | [closed](closure/canonical-wire/002-status.md) |
| W003 | [Static wire-profile registry and codec contract](implementation/canonical-wire/003-wire-profile-registry-and-codec-contract.md) | capability/invariant | [`f0ab286`](https://github.com/eggstack/eggpool/commit/f0ab286) | [closed](closure/canonical-wire/003-status.md) |
| W004 | [OpenAI Chat Completions and Anthropic Messages codecs](implementation/canonical-wire/004-openai-chat-anthropic-messages-codecs.md) | capability | [`f851f62`](https://github.com/eggstack/eggpool/commit/f851f62) | [closed](closure/canonical-wire/004-status.md) |
| W005 | [OpenAI Responses and Gemini generateContent codecs](implementation/canonical-wire/005-openai-responses-gemini-codecs.md) | capability | [`42200327`](https://github.com/eggstack/eggpool/commit/42200327aadc866c2bad263ffe11a1c3a5045a6a) | [closed](closure/canonical-wire/005-status.md) |
| W006 | [Reasoning, tools, structured output, and loss policy](implementation/canonical-wire/006-reasoning-tools-structured-output-and-loss-policy.md) | capability/invariant | [`2835e8c`](https://github.com/eggstack/eggpool/commit/2835e8c) | [closed](closure/canonical-wire/006-status.md) |
| W007 | [Multimodal, documents, cache controls, and provider adaptation](implementation/canonical-wire/007-multimodal-documents-cache-and-provider-adaptation.md) | capability/invariant | [`b11bf5b`](https://github.com/eggstack/eggpool/commit/b11bf5b) | [closed](closure/canonical-wire/007-status.md) |
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
| D009 | [Selection fairness and frozen routing-trace correction](implementation/routing-domain/009-selection-fairness-and-trace-snapshot-correction.md) | invariant/corrective | [`1557d59`](https://github.com/eggstack/eggpool/commit/1557d59) | [closed](closure/routing-domain/009-status.md) |

## M5 closure state

D009 resolved the two post-D008 accepted-selection findings: configured random fairness now affects the actual accepted claim, and each accepted claim retains a frozen pre-publication selection snapshot used by routing traces. D001-D008 closure records remain append-only historical evidence. M5 is closed after D009.

## M6 planned sequence

| ID | Plan | Dependency state |
|---|---|---|
| W001 | [Contract and deterministic fixture freeze](implementation/canonical-wire/001-contract-and-fixture-freeze.md) | closed; see [closure](closure/canonical-wire/001-status.md) |
| W002 | [Canonical IR, request admission, limits, and M5 fact bridge](implementation/canonical-wire/002-canonical-ir-request-admission-and-limits.md) | closed; see [closure](closure/canonical-wire/002-status.md) |
| W003 | [Static wire-profile registry and codec contract](implementation/canonical-wire/003-wire-profile-registry-and-codec-contract.md) | closed; see [closure](closure/canonical-wire/003-status.md) |
| W004 | [OpenAI Chat Completions and Anthropic Messages codecs](implementation/canonical-wire/004-openai-chat-anthropic-messages-codecs.md) | closed; see [closure](closure/canonical-wire/004-status.md) |
| W005 | [OpenAI Responses and Gemini generateContent codecs](implementation/canonical-wire/005-openai-responses-gemini-codecs.md) | closed; see [closure](closure/canonical-wire/005-status.md) |
| W006 | [Reasoning, tools, structured output, and loss policy](implementation/canonical-wire/006-reasoning-tools-structured-output-and-loss-policy.md) | closed; see [closure](closure/canonical-wire/006-status.md) |
| W007 | [Multimodal, documents, cache controls, and provider adaptation](implementation/canonical-wire/007-multimodal-documents-cache-and-provider-adaptation.md) | closed; see [closure](closure/canonical-wire/007-status.md) |
| W008 | [SSE, canonical stream events, usage, and terminal evidence](implementation/canonical-wire/008-sse-stream-events-usage-and-terminal-evidence.md) | **dependency-ready; W007 closure accepted** |
| W009 | [Selected-profile codec runtime boundary](implementation/canonical-wire/009-selected-profile-codec-runtime-boundary.md) | planned; blocked on W008 closure |
| W010 | [Differential qualification and M6 closure](implementation/canonical-wire/010-differential-qualification-and-m6-closure.md) | planned; blocked on W009 closure |

Only the dependency-ready table authorizes implementation handoff. Successors move only after accepted closure evidence for their hard predecessors.

## M6 boundary decisions

M6 consumes the closed M5 request-independent routing/affinity DTOs through pure adapters; it does not call account selection, mutate claims, or perform semantic model-router selector inference.

M6 owns static wire-profile identity and deterministic codec/adaptation behavior. Python `wire.resolver` runtime negotiation state—learned preference, rejected wire candidates, alternate-wire retry, and negotiation handles—is M7 because it is inseparable from durable attempt/retry ownership.

M6 does not submit provider HTTP requests. The M4 transport remains a closed dependency for M7. M6 produces bounded encoded request bodies and consumes finite/stream provider bytes supplied by its caller.

SSE framing, canonical events, usage extraction, and terminal evidence are M6. Timeout/cancellation policy, downstream response handoff, retry legality, health effects, and finalization are M7.

No new database schema is planned for M6.

## Future work and block state

W008 is the sole dependency-ready M6 plan. W001-W007 are closed; W009-W010 remain planned but blocked in sequence.

M7 coordinator/retry/finalization research may proceed conceptually, but **M7 implementation handoff remains blocked until accepted W010 closure** establishes the selected-profile codec runtime as stable.

M8 runtime generations/background lifecycle, M9 operational lifecycle, M10 qualification/SBC characterization, M11 cutover, and M12 retirement remain sequenced by `002-long-term-roadmap.md`.

## Closure state

F001-F006, M4 T001-T006, and M5 D001-D009 are closed. M6 is active at W008. No M7 implementation plan is dependency-ready.

If a later review finds a material gap in a closed M6 plan, create a bounded corrective plan and update aggregate state rather than rewriting historical closure evidence.
