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
| M6 canonical request/wire codecs | [canonical-wire-roadmap](subsystems/canonical-wire-roadmap.md) | closed after W012 corrective pass | W012 closed |
| M7 coordinator/retry/finalization | [coordinator-roadmap](subsystems/coordinator-roadmap.md) | **active** | **C001 ready** |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| C001 | [Coordinator contract and deterministic failure corpus](implementation/coordinator/001-contract-and-failure-corpus-freeze.md) | invariant/infrastructure | M4, M5, M6 closed | **ready for handoff** |

C002-C011 are registered but remain queued behind their explicit hard dependencies. The sequence is recorded in `implementation/coordinator/README.md` and `000-handoff-sequence.md`. M8 implementation planning remains blocked on accepted C011 M7 closure.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| F001 | [Rust workspace and build scaffold](implementation/foundation/001-rust-workspace-and-build-scaffold.md) | infrastructure | `573e081f` | [closed](closure/foundation/001-status.md) |
| F002 | [Contract inventory and differential oracle harness](implementation/foundation/002-contract-inventory-and-oracle-harness.md) | invariant/infrastructure | `a8c3621` | [closed](closure/foundation/002-status.md) |
| F003 | [Config and CLI compatibility foundation](implementation/foundation/003-config-and-cli-compatibility.md) | capability | `5afbbdd` | [closed](closure/foundation/003-status.md) |
| F004 | [SQLite schema and repository compatibility baseline](implementation/foundation/004-sqlite-schema-and-repository-baseline.md) | invariant/infrastructure | `9cc9fc4` | [closed](closure/foundation/004-status.md) |
| F005 | [Axum SSR shell and static-asset parity baseline](implementation/foundation/005-axum-ssr-shell-and-static-assets.md) | capability | `9d272b8` | [closed](closure/foundation/005-status.md) |
| F006 | [Side-by-side safety and serve-contract closure](implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md) | invariant | `df902b5` | [closed](closure/foundation/006-status.md) |
| T001 | [Provider transport contract and fixture freeze](implementation/provider-transport/001-contract-and-fixture-freeze.md) | invariant/infrastructure | `50d7ff4` | [closed](closure/provider-transport/001-status.md) |
| T002 | [Direct Hyper/Rustls provider HTTP core](implementation/provider-transport/002-direct-hyper-rustls-core.md) | infrastructure | `c9f448a` + `2696e52` | [closed](closure/provider-transport/002-status.md) |
| T003 | [Eggress connector and proxy parity](implementation/provider-transport/003-eggress-connector-and-proxy-parity.md) | infrastructure/capability | `5b34d8b` | [historical closure](closure/provider-transport/003-status.md) |
| T004 | [Provider/account client pool and lifecycle boundary](implementation/provider-transport/004-provider-account-client-pool.md) | capability/invariant | `71ef03d` | [closed](closure/provider-transport/004-status.md) |
| T005 | [Differential qualification and initial M4 closure](implementation/provider-transport/005-differential-qualification-and-closure.md) | invariant | `c89e645` | [historical closure](closure/provider-transport/005-status.md) |
| T006 | [Extended proxy runtime interoperability closure](implementation/provider-transport/006-extended-proxy-runtime-qualification.md) | invariant/corrective | `4b3a95a` | [closed](closure/provider-transport/006-status.md) |
| D001 | [Routing-domain contract and deterministic fixture freeze](implementation/routing-domain/001-contract-and-fixture-freeze.md) | invariant/infrastructure | `40be1bf` | [closed](closure/routing-domain/001-status.md) |
| D002 | [Account registry and catalog cache/hydration](implementation/routing-domain/002-account-registry-and-catalog-cache.md) | capability/invariant | `966ca1b` + `4110d23` + `3916c84` + `b661705` | [closed](closure/routing-domain/002-status.md) |
| D003 | [Catalog refresh, normalization, and persistence](implementation/routing-domain/003-catalog-refresh-normalization-and-persistence.md) | capability/invariant | `c956e89` | [closed](closure/routing-domain/003-status.md) |
| D004 | [Quota, claims, and fair-share scoring](implementation/routing-domain/004-quota-claims-and-fair-scoring.md) | capability/invariant | `d649e8a` | [closed](closure/routing-domain/004-status.md) |
| D005 | [Health, backoff, circuit, and quarantine](implementation/routing-domain/005-health-backoff-circuit-and-quarantine.md) | invariant/capability | `d5dd16d` | [closed](closure/routing-domain/005-status.md) |
| D006 | [Routing eligibility, fairness, and local claims](implementation/routing-domain/006-routing-eligibility-fairness-and-claims.md) | capability/invariant | `b009023` | [historical closure](closure/routing-domain/006-status.md) |
| D007 | [Model-router registry and affinity](implementation/routing-domain/007-model-router-registry-and-affinity.md) | capability/invariant | `43ce484` | [closed](closure/routing-domain/007-status.md) |
| D008 | [Differential qualification and initial M5 closure](implementation/routing-domain/008-differential-qualification-and-closure.md) | invariant | `477aade` | [historical aggregate closure](closure/routing-domain/008-status.md) |
| D009 | [Selection fairness and frozen routing-trace correction](implementation/routing-domain/009-selection-fairness-and-trace-snapshot-correction.md) | invariant/corrective | `1557d59` | [closed](closure/routing-domain/009-status.md) |
| W001 | [Canonical wire contract and deterministic fixture freeze](implementation/canonical-wire/001-contract-and-fixture-freeze.md) | invariant/infrastructure | `52f1dfac` | [closed](closure/canonical-wire/001-status.md) |
| W002 | [Canonical IR, request admission, limits, and M5 fact bridge](implementation/canonical-wire/002-canonical-ir-request-admission-and-limits.md) | capability/invariant | `2096727b` | [closed](closure/canonical-wire/002-status.md) |
| W003 | [Static wire-profile registry and codec contract](implementation/canonical-wire/003-wire-profile-registry-and-codec-contract.md) | capability/invariant | `f0ab286` | [closed](closure/canonical-wire/003-status.md) |
| W004 | [OpenAI Chat Completions and Anthropic Messages codecs](implementation/canonical-wire/004-openai-chat-anthropic-messages-codecs.md) | capability | `f851f62` | [closed](closure/canonical-wire/004-status.md) |
| W005 | [OpenAI Responses and Gemini generateContent codecs](implementation/canonical-wire/005-openai-responses-gemini-codecs.md) | capability | `42200327` | [closed](closure/canonical-wire/005-status.md) |
| W006 | [Reasoning, tools, structured output, and loss policy](implementation/canonical-wire/006-reasoning-tools-structured-output-and-loss-policy.md) | capability/invariant | `2835e8c` | [closed](closure/canonical-wire/006-status.md) |
| W007 | [Multimodal, documents, cache controls, and provider adaptation](implementation/canonical-wire/007-multimodal-documents-cache-and-provider-adaptation.md) | capability/invariant | `b11bf5b` | [closed](closure/canonical-wire/007-status.md) |
| W008 | [SSE, canonical stream events, usage, and terminal evidence](implementation/canonical-wire/008-sse-stream-events-usage-and-terminal-evidence.md) | capability/invariant | `6cf01595` | [historical closure](closure/canonical-wire/008-status.md) |
| W009 | [Selected-profile codec runtime boundary](implementation/canonical-wire/009-selected-profile-codec-runtime-boundary.md) | capability/invariant | `0acbccb` | [closed](closure/canonical-wire/009-status.md) |
| W010 | [Differential qualification and initial M6 closure](implementation/canonical-wire/010-differential-qualification-and-m6-closure.md) | invariant | `77e4dde` | [historical aggregate closure](closure/canonical-wire/010-status.md) |
| W011 | [SSE EOF UTF-8 finalization correction](implementation/canonical-wire/011-sse-eof-utf8-correction.md) | invariant/corrective | `35cdd04` | [closed](closure/canonical-wire/011-status.md) |
| W012 | [Cross-surface differential requalification and M6 re-closure](implementation/canonical-wire/012-cross-surface-differential-requalification-and-m6-reclosure.md) | invariant/corrective | `1e0bb712` | [closed](closure/canonical-wire/012-status.md) |

## M5 closure state

M5 is closed after D009. D009 corrected accepted random-fairness execution and froze the pre-publication selection snapshot used by routing traces. D001-D008 remain append-only historical evidence.

## M6 closure state

M6 is closed after W011/W012. W011 corrected SSE EOF UTF-8 finalization; W012 replaced the under-asserted W010 cross-surface qualification with full Python-derived 15 request + 15 finite response + 15 stream comparisons. W008/W010 remain historical evidence.

## M7 planned sequence

| ID | Plan | Dependency state |
|---|---|---|
| C001 | [Contract and deterministic failure corpus freeze](implementation/coordinator/001-contract-and-failure-corpus-freeze.md) | **dependency-ready** |
| C002 | [Durable dispatch publication and lifecycle identity](implementation/coordinator/002-durable-dispatch-publication-and-lifecycle-identity.md) | queued; C001 |
| C003 | [Runtime wire resolution and negotiation](implementation/coordinator/003-runtime-wire-resolution-and-negotiation.md) | queued; C002 |
| C004 | [Provider attempt construction and upstream submission](implementation/coordinator/004-provider-attempt-construction-and-submission.md) | queued; C003 |
| C005 | [Failure effects, retry budget, and failover](implementation/coordinator/005-failure-effects-retry-and-failover.md) | queued; C004 |
| C006 | [Durable finalization and retained terminal ownership](implementation/coordinator/006-durable-finalization-and-retained-ownership.md) | queued; C005 |
| C007 | [Finite response handoff and completion](implementation/coordinator/007-finite-response-handoff-and-completion.md) | queued; C006 |
| C008 | [Streaming handoff, timeouts, cancellation, terminal policy](implementation/coordinator/008-streaming-handoff-timeouts-and-cancellation.md) | queued; C007 |
| C009 | [Public inference endpoints and semantic-router dispatch](implementation/coordinator/009-inference-endpoints-and-semantic-router-dispatch.md) | queued; C008 |
| C010 | [Crash/restart reconciliation and fault injection](implementation/coordinator/010-crash-restart-reconciliation-and-fault-injection.md) | queued; C009 |
| C011 | [Differential qualification and M7 closure](implementation/coordinator/011-differential-qualification-and-m7-closure.md) | queued; C010 |

Only the dependency-ready table authorizes implementation. Successors move only after accepted closure evidence for their hard predecessor.

## M7 boundary decisions

M7 consumes M4 transport, M5 local selection/claim state, and M6 selected-profile transformation. It owns durable attempt publication, dynamic wire negotiation, provider submission, retry/failover, downstream handoff, cancellation/timeout policy, retained terminal cleanup, public inference endpoints, semantic-selector internal dispatch, and deterministic restart reconciliation.

M7 does not own runtime generation publication, live rehash, process signal/shutdown orchestration, or recurring/background scheduling. Its retained-finalization supervisor and reconciliation routines must expose stable interfaces for M8 to own/schedule later.

No new database schema is planned. Any discovered requirement for a Rust-only schema fork is a stop condition.

## Future work and block state

M8 runtime generations/background lifecycle remains blocked on accepted C011 M7 closure and its own planning review. M9-M12 remain sequenced by `002-long-term-roadmap.md`.

## Closure state

F001-F006, M4 T001-T006, M5 D001-D009, and M6 W001-W012 are closed. M7 is active with C001 as the sole dependency-ready implementation plan.