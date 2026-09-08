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
| M7 coordinator/retry/finalization | [coordinator-roadmap](subsystems/coordinator-roadmap.md) | closed after C011 | M7 closed |
| M8 runtime generations/rehash/background lifecycle | [runtime-lifecycle-roadmap](subsystems/runtime-lifecycle-roadmap.md) | **active implementation** | **R003 ready** |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| R003 | [Active generation manager, atomic publication, and request leases](implementation/runtime-lifecycle/003-active-generation-manager-publication-and-leases.md) | invariant/infrastructure | accepted R002 closure | **dependency-ready** |

R004-R011 remain serially blocked behind the accepted closure of their immediate predecessor. M9 is blocked on accepted R011 M8 closure and its own planning/implementation review.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| R001 | [Runtime/reload contract and deterministic oracle freeze](implementation/runtime-lifecycle/001-runtime-reload-contract-and-oracle-freeze.md) | invariant/infrastructure | `56492759e40d4bbc8febef36dce22ee0a07e6760` | [closed](closure/runtime-lifecycle/001-status.md) |
| R002 | [Process runtime, generation factory, and candidate ownership](implementation/runtime-lifecycle/002-process-runtime-generation-factory-and-candidate-ownership.md) | infrastructure/invariant | `ded541f4a576928015ecf5f1be1b1a96b0b1539c` | [closed](closure/runtime-lifecycle/002-status.md) |
| C001 | [Coordinator contract and deterministic failure corpus](implementation/coordinator/001-contract-and-failure-corpus-freeze.md) | invariant/infrastructure | `59eda5ab` | [closed](closure/coordinator/001-status.md) |
| C002 | [Durable dispatch publication and lifecycle identity](implementation/coordinator/002-durable-dispatch-publication-and-lifecycle-identity.md) | invariant/capability | `8caae259` | [closed](closure/coordinator/002-status.md) |
| C003 | [Runtime wire resolution and negotiation ownership](implementation/coordinator/003-runtime-wire-resolution-and-negotiation.md) | capability/invariant | `97a4846` | [historical closure](closure/coordinator/003-status.md) |
| C004 | [Provider-bound attempt construction and upstream submission](implementation/coordinator/004-provider-attempt-construction-and-submission.md) | capability/invariant | `97a4846` | [historical closure](closure/coordinator/004-status.md) |
| C005 | [Failure effects, retry budget, and failover](implementation/coordinator/005-failure-effects-retry-and-failover.md) | invariant/capability | `97a4846` | [historical closure](closure/coordinator/005-status.md) |
| C006 | [Durable finalization and retained terminal ownership](implementation/coordinator/006-durable-finalization-and-retained-ownership.md) | invariant | `97a4846` | [historical closure](closure/coordinator/006-status.md) |
| C012 | [Coordinator core contract correction](implementation/coordinator/012-coordinator-core-contract-correction.md) | invariant/corrective | `5495f72` + `2f37f7b` | [closed](closure/coordinator/012-status.md) |
| C013 | [Coordinator core differential requalification](implementation/coordinator/013-coordinator-core-differential-requalification.md) | invariant/corrective | `85ad837b` | [closed](closure/coordinator/013-status.md) |
| C014 | [Finalization idempotency and Retry-After closure](implementation/coordinator/014-finalization-idempotency-and-retry-after-closure.md) | invariant/corrective | `7607237d533e5e3f6ae33d2ecd504acff5732959` | [closed](closure/coordinator/014-status.md) |
| C007 | [Finite response handoff and completion](implementation/coordinator/007-finite-response-handoff-and-completion.md) | capability/invariant | `a7a119ed` | [closed](closure/coordinator/007-status.md) |
| C008 | [Streaming handoff, timeouts, cancellation](implementation/coordinator/008-streaming-handoff-timeouts-and-cancellation.md) | capability/invariant | `ecce4212` | [closed](closure/coordinator/008-status.md) |
| C009 | [Public inference endpoints and semantic-router dispatch](implementation/coordinator/009-inference-endpoints-and-semantic-router-dispatch.md) | capability/invariant | `0813ba62` | [closed](closure/coordinator/009-status.md) |
| C010 | [Crash/restart reconciliation and fault injection](implementation/coordinator/010-crash-restart-reconciliation-and-fault-injection.md) | invariant | `d1d7f5a2` | [closed](closure/coordinator/010-status.md) |
| C011 | [Differential qualification and M7 closure](implementation/coordinator/011-differential-qualification-and-m7-closure.md) | invariant | `0216410f` | [closed](closure/coordinator/011-status.md) |
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

M6 is closed after W011/W012. W011 corrected SSE EOF UTF-8 finalization; W012 replaced the under-asserted W010 cross-surface qualification with full Python-derived request/finite/stream comparisons. W008/W010 remain historical evidence.

## M7 closure state

M7 is closed after C011. C011 aggregated the full coordinator qualification (integrated finite/streaming matrix, failure corpus, concurrency/leak pass, restart reconciliation, security review) with no unresolved high/medium finding. C001-C002, C007-C011, and C012-C014 are closed; C003-C006 remain append-only historical evidence for the findings corrected by C012-C014.

## M8 planned sequence

M8 starts from the stable interfaces documented by C011. It owns immutable generation snapshots, linearizable leases, atomic publication, live reload, retirement/finalization drain, background task ownership, process signals/shutdown, and active-generation diagnostics. It does not own the M9 control/daemon CLI.

| ID | Plan | Dependency state |
|---|---|---|
| R001 | [Runtime/reload contract and deterministic oracle freeze](implementation/runtime-lifecycle/001-runtime-reload-contract-and-oracle-freeze.md) | closed; see [closure](closure/runtime-lifecycle/001-status.md) |
| R002 | [Process runtime, generation factory, and candidate ownership](implementation/runtime-lifecycle/002-process-runtime-generation-factory-and-candidate-ownership.md) | closed; see [closure](closure/runtime-lifecycle/002-status.md) |
| R003 | [Active generation manager, atomic publication, and request leases](implementation/runtime-lifecycle/003-active-generation-manager-publication-and-leases.md) | **dependency-ready**; R002 |
| R004 | [Retirement, retained finalization drain, and resource close](implementation/runtime-lifecycle/004-generation-retirement-finalization-drain-and-close.md) | queued; R003 |
| R005 | [Config diff, reload policy, and redacted change model](implementation/runtime-lifecycle/005-config-diff-reload-policy-and-redaction.md) | queued; R004 |
| R006 | [Process task supervisor and authoritative task-spec staging](implementation/runtime-lifecycle/006-process-task-supervisor-and-task-spec-staging.md) | queued; R005 |
| R007 | [Transactional live rehash and coherent acceptance](implementation/runtime-lifecycle/007-transactional-live-rehash-and-coherent-acceptance.md) | queued; R006 |
| R008 | [Generation-leased maintenance, recovery, and background integration](implementation/runtime-lifecycle/008-generation-leased-maintenance-recovery-and-background.md) | queued; R007 |
| R009 | [Server startup, signals, graceful drain, and forced shutdown](implementation/runtime-lifecycle/009-server-startup-signals-and-shutdown.md) | queued; R008 |
| R010 | [Active-generation authority audit and runtime/reload diagnostics](implementation/runtime-lifecycle/010-active-generation-authority-and-diagnostics.md) | queued; R009 |
| R011 | [Differential qualification and M8 closure](implementation/runtime-lifecycle/011-differential-qualification-and-m8-closure.md) | queued; R010 |

Only the dependency-ready table authorizes implementation. R003 is the sole current M8 handoff.

## M8 boundary decisions

M8 composes the closed M7 `InferenceState`, finalization supervisor/drain interface, crash reconciler, routing claims, and finite/stream lifecycle. M8 may refactor construction/ownership to inject process-owned affinity/wire state, but it must not redesign M7 retry/handoff/finalization semantics.

M8 uses one process-owned recurring-task supervisor. Generation-dependent ticks acquire the active generation for each tick instead of capturing a generation across reload. Deferred backup/update business callbacks owned by M9 must be explicit and unregistered, never silent placeholders.

`arc-swap` is the expected small dependency for active `Arc` publication. A tiny synchronization section around lease claim/publication is acceptable; a lifecycle/actor/workflow framework is not.

No new database schema is planned. Any discovered need for a Rust-only schema fork is a stop condition.

## Future work and block state

M9 operational CLI/control/lifecycle work remains blocked on accepted R011 M8 closure. M10-M12 remain sequenced by `002-long-term-roadmap.md`.

## Closure state

F001-F006, M4 T001-T006, M5 D001-D009, M6 W001-W012, and M7 C001-C011 with C012-C014 corrective passes remain closed. M8 is active with R001/R002 closed, R003 ready, and R004-R011 serially blocked.
