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
| M8 runtime generations/rehash/background lifecycle | [runtime-lifecycle-roadmap](subsystems/runtime-lifecycle-roadmap.md) | closed after R013 corrective pass | R013 closed |
| M9 operational CLI/lifecycle/update/deploy | [operational-cli-lifecycle-roadmap](subsystems/operational-cli-lifecycle-roadmap.md) | **closed after O010** | **M9 closed; M10 eligible for review** |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| — | None | — | — | — |

O010 is accepted and removed from the dependency-ready queue. M10 is eligible
for a separate planning/implementation review, but no M10 implementation plan
is promoted automatically.

## Completed implementation plans

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| R001 | [Runtime/reload contract and deterministic oracle freeze](implementation/runtime-lifecycle/001-runtime-reload-contract-and-oracle-freeze.md) | invariant/infrastructure | `56492759e40d4bbc8febef36dce22ee0a07e6760` | [closed](closure/runtime-lifecycle/001-status.md) |
| R002 | [Process runtime, generation factory, and candidate ownership](implementation/runtime-lifecycle/002-process-runtime-generation-factory-and-candidate-ownership.md) | infrastructure/invariant | `ded541f4a576928015ecf5f1be1b1a96b0b1539c` | [closed](closure/runtime-lifecycle/002-status.md) |
| R003 | [Active generation manager, atomic publication, and request leases](implementation/runtime-lifecycle/003-active-generation-manager-publication-and-leases.md) | invariant/infrastructure | `af794eaa92eb767c837b10c9f01dc3f962181c3e` | [closed](closure/runtime-lifecycle/003-status.md) |
| R004 | [Retirement, retained finalization drain, and resource close](implementation/runtime-lifecycle/004-generation-retirement-finalization-drain-and-close.md) | invariant/infrastructure | `fa3ab9d` | [closed](closure/runtime-lifecycle/004-status.md) |
| R005 | [Config diff, reload policy, and redacted change model](implementation/runtime-lifecycle/005-config-diff-reload-policy-and-redaction.md) | invariant/capability | `c9ee3656a097addaec0982ec0d0128b1d3d2ad7d` | [closed](closure/runtime-lifecycle/005-status.md) |
| R006 | [Process task supervisor and authoritative task-spec staging](implementation/runtime-lifecycle/006-process-task-supervisor-and-task-spec-staging.md) | infrastructure/invariant | `bc1220f` | [closed](closure/runtime-lifecycle/006-status.md) |
| R007 | [Transactional live rehash and coherent acceptance](implementation/runtime-lifecycle/007-transactional-live-rehash-and-coherent-acceptance.md) | invariant/capability | `1b03ade393e7caa532d71ee3b9ce9b3989c49612` | [closed](closure/runtime-lifecycle/007-status.md) |
| R008 | [Generation-leased maintenance, recovery, and background integration](implementation/runtime-lifecycle/008-generation-leased-maintenance-recovery-and-background.md) | capability/invariant | `a814ea8` | [closed](closure/runtime-lifecycle/008-status.md) |
| R009 | [Server startup, signals, graceful drain, and forced shutdown](implementation/runtime-lifecycle/009-server-startup-signals-and-shutdown.md) | capability/invariant | `5f34e90` + `d04967d` | [closed](closure/runtime-lifecycle/009-status.md) |
| R010 | [Active-generation authority audit and runtime/reload diagnostics](implementation/runtime-lifecycle/010-active-generation-authority-and-diagnostics.md) | invariant | `1e784d03` | [closed](closure/runtime-lifecycle/010-status.md) |
| R011 | [Differential qualification and initial M8 closure](implementation/runtime-lifecycle/011-differential-qualification-and-m8-closure.md) | invariant | `31b32c4` | [historical aggregate closure](closure/runtime-lifecycle/011-status.md) |
| R012 | [Wire-negotiation runtime authority and reload-diagnostics re-closure](implementation/runtime-lifecycle/012-wire-negotiation-runtime-authority-and-reload-diagnostics-reclosure.md) | invariant/corrective | `37ec54b` | [historical corrective closure](closure/runtime-lifecycle/012-status.md) |
| R013 | [Wire-policy acceptance and boundary requalification](implementation/runtime-lifecycle/013-wire-policy-acceptance-and-boundary-requalification.md) | invariant/corrective | `55a01b3` | [closed](closure/runtime-lifecycle/013-status.md) |
| O001 | [Operational CLI contract and deterministic oracle freeze](implementation/operations/001-operational-cli-contract-and-oracle-freeze.md) | invariant/infrastructure | `db3a11085689f608a64c95079c5481b45b1d9911` | [closed](closure/operations/001-status.md) |
| O002 | [Local control, runtime paths, and process-state boundary](implementation/operations/002-local-control-runtime-paths-and-process-state.md) | infrastructure/invariant | `32f7e8c220f3797223bc534afa3b620063dc6a83` | [closed](closure/operations/002-status.md) |
| O003 | [Process lifecycle control and watchdog commands](implementation/operations/003-process-lifecycle-control-and-watchdog-commands.md) | capability/invariant | `35f089f2cc8a30e44fafada1816b734e279cf28d` | [closed](closure/operations/003-status.md) |
| O004 | [Config, key, provider onboarding, and live-apply mutations](implementation/operations/004-config-key-provider-onboarding-and-live-apply.md) | capability/invariant | `3d1b63c` | [closed](closure/operations/004-status.md) |
| O005 | [Agent integration and `configsetup` generation](implementation/operations/005-agent-integration-config-generation.md) | capability | `59591bca254fc5d71a55fbf750e9f0eb5189aba8` | [closed](closure/operations/005-status.md) |
| O006 | [Database, backup, recovery, and automatic backup](implementation/operations/006-database-backup-recovery-and-automatic-backup.md) | capability/invariant | `6af52fb` | [closed](closure/operations/006-status.md) |
| O007 | [Operator inspection, maintenance, and metrics flush](implementation/operations/007-operator-inspection-maintenance-and-metrics-flush.md) | capability/invariant | `94fddcc` | [closed](closure/operations/007-status.md) |
| O008 | [Update, version resolution, and update-checker task](implementation/operations/008-update-version-and-update-checker.md) | capability/invariant | `c9b63d5` | [closed](closure/operations/008-status.md) |
| O009 | [Deployment, install artifacts, and uninstall](implementation/operations/009-deployment-install-artifacts-and-uninstall.md) | capability/invariant | `27310ff` | [closed](closure/operations/009-status.md) |
| O010 | [Differential qualification and M9 closure](implementation/operations/010-differential-qualification-and-m9-closure.md) | invariant/polish | `f41489c` | [closed](closure/operations/010-status.md) |
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
| F005 | [Axum SSR shell and static assets](implementation/foundation/005-axum-ssr-shell-and-static-assets.md) | capability | `9d272b8` | [closed](closure/foundation/005-status.md) |
| F006 | [Side-by-side safety and serve-contract closure](implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md) | invariant | `df902b5` | [closed](closure/foundation/006-status.md) |
| T001 | [Provider transport contract and fixture freeze](implementation/provider-transport/001-contract-and-fixture-freeze.md) | invariant/infrastructure | `50d7ff4` | [closed](closure/provider-transport/001-status.md) |
| T002 | [Direct Hyper/Rustls provider HTTP core](implementation/provider-transport/002-direct-hyper-rustls-core.md) | infrastructure | `c9f448a` + `2696e52` | [closed](closure/provider-transport/002-status.md) |
| T003 | [Eggress connector and proxy parity](implementation/provider-transport/003-egress-connector-and-proxy-parity.md) | infrastructure/capability | `5b34d8b` | [historical closure](closure/provider-transport/003-status.md) |
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

M5 is closed after D009. D001-D008 remain append-only historical evidence.

## M6 closure state

M6 is closed after W011/W012. W008/W010 remain historical evidence.

## M7 closure state

M7 is closed after C011. C001-C002, C007-C011 and C012-C014 are closed; C003-C006 remain append-only historical evidence for findings corrected by C012-C014.

## M8 closure state

M8 is closed after accepted R013. R001-R010 remain closed; R011/R012 remain historical closure evidence after post-close audits. R013 closed the final wire-policy acceptance/validation/rollback qualification boundary.

M8 owns one process task supervisor. R008 intentionally left exactly `metrics_flush`, `update_checker`, and `automatic_backup` as explicit deferred M9 business capabilities; O006 registered automatic backup, O007 registered metrics flush, and O008 registered update checking.

## M9 sequence and handoff state

M9 owns user-facing operational CLI/control/lifecycle/update/deploy behavior and now owns the completed R008 update-checker callback. It composes closed M4-M8 services and does not own M10 broad qualification or M11 cutover.

| ID | Plan | Dependency state |
|---|---|---|
| O001 | [Operational CLI contract and deterministic oracle freeze](implementation/operations/001-operational-cli-contract-and-oracle-freeze.md) | **closed** |
| O002 | [Local control, runtime paths, and process-state boundary](implementation/operations/002-local-control-runtime-paths-and-process-state.md) | **closed** |
| O003 | [Process lifecycle control and watchdog commands](implementation/operations/003-process-lifecycle-control-and-watchdog-commands.md) | **closed** |
| O004 | [Config, key, provider onboarding, and live-apply mutations](implementation/operations/004-config-key-provider-onboarding-and-live-apply.md) | **closed** |
| O005 | [Agent integration and configsetup generation](implementation/operations/005-agent-integration-config-generation.md) | **closed** |
| O006 | [Database, backup, recovery, and automatic backup](implementation/operations/006-database-backup-recovery-and-automatic-backup.md) | **closed; 6af52fb** |
| O007 | [Operator inspection, maintenance, and metrics flush](implementation/operations/007-operator-inspection-maintenance-and-metrics-flush.md) | **closed** |
| O008 | [Update, version resolution, and update-checker task](implementation/operations/008-update-version-and-update-checker.md) | **closed** |
| O009 | [Deployment, install artifacts, and uninstall](implementation/operations/009-deployment-install-artifacts-and-uninstall.md) | **closed; 27310ff** |
| O010 | [Differential qualification and M9 closure](implementation/operations/010-differential-qualification-and-m9-closure.md) | **closed; f41489c** |

### M9 boundary decisions

- F003 remains the command/option shape authority; O001 refreshes current behavior/effects without rewriting F003 history.
- Rust supported commands execute Rust behavior; no Python fallback.
- O002 creates one bounded local Unix control listener for proven local-control needs; no public management port or general RPC framework.
- O003 consumes M8 startup/reload/shutdown/drain APIs rather than creating a second process runtime.
- O004/O005 keep config/provider/integration mutation/output narrow and secret-safe.
- O006 reuses canonical migrations and makes `automatic_backup` a real M8 task.
- O007 reuses domain repositories/services and makes `metrics_flush` a real M8 task.
- O008 uses a Rust artifact/update backend with staged integrity-checked replacement and makes `update_checker` a real M8 task; M11 owns making public Rust assets canonical.
- O009 ports local systemd/cron/logrotate/uninstall behavior but does not perform M11 public-install cutover; its implementation and closure are accepted in `27310ff` and `closure/operations/009-status.md`.
- O010 alone may close M9; its accepted closure is recorded in `closure/operations/010-status.md`.

## Future work and block state

M10 is eligible after accepted O010 M9 closure, subject to its own
planning/implementation review. M11-M12 remain sequenced by
`002-long-term-roadmap.md`. No M10 plan is promoted automatically.

## Closure state

F001-F006, M4 T001-T006, M5 D001-D009, M6 W001-W012, M7 C001-C011 with C012-C014 corrective passes, M8 R001-R013, and O001-O010 are closed as described above. M9 is closed after accepted O010 closure; M10 is eligible for its own planning/implementation review, and no later implementation plan is promoted automatically.
