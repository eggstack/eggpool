# EggPool Rust Migration Registry

Status: active

Planning baseline: `385cc2355e84db6071ab35e81b14f55e344afd77`

## Canonical documents

- [Long-term specification](000-long-term-specification.md)
- [Terminology and domain model](001-terminology-and-domain-model.md)
- [Long-term roadmap](002-long-term-roadmap.md)
- [Planning process](003-planning-process.md)

## Accepted ADRs

- [ADR-0001 — Side-by-side migration with Python as behavioral oracle](adrs/ADR-0001-side-by-side-python-oracle.md)
- [ADR-0002 — Rust runtime, HTTP stack, SSR parity, and implementation location](adrs/ADR-0002-rust-runtime-http-ssr.md)
- [ADR-0003 — Eggress in-process outbound connector replaces pproxy](adrs/ADR-0003-eggress-outbound-connector.md)
- [ADR-0004 — PyPI remains the canonical package channel; Rust ships as binary wheels](adrs/ADR-0004-pypi-rust-wheel-and-install-authority.md)
- [ADR-0005 — M12 pure-Rust production boundary with immutable historical-version compatibility](adrs/ADR-0005-m12-pure-rust-production-and-reference-retirement.md)

## Subsystem roadmaps

| Subsystem | Roadmap | Status | Current milestone |
|---|---|---|---|
| Migration foundation | [foundation-roadmap](subsystems/foundation-roadmap.md) | closed after F006 corrective pass | F006 closed |
| M4 provider transport | [provider-transport-roadmap](subsystems/provider-transport-roadmap.md) | closed after T006 corrective pass | T006 closed |
| M5 routing domain/catalog state | [routing-domain-roadmap](subsystems/routing-domain-roadmap.md) | closed after D009 corrective pass | D009 closed |
| M6 canonical request/wire codecs | [canonical-wire-roadmap](subsystems/canonical-wire-roadmap.md) | closed after W012 corrective pass | W012 closed |
| M7 coordinator/retry/finalization | [coordinator-roadmap](subsystems/coordinator-roadmap.md) | closed after C011 | M7 closed |
| M8 runtime generations/rehash/background lifecycle | [runtime-lifecycle-roadmap](subsystems/runtime-lifecycle-roadmap.md) | closed after R013 corrective pass | R013 closed |
| M9 operational CLI/lifecycle/update/deploy | [operational-cli-lifecycle-roadmap](subsystems/operational-cli-lifecycle-roadmap.md) | closed after O010 | M9 closed |
| M10 full qualification/portability/SBC | [qualification-roadmap](subsystems/qualification-roadmap.md) | closed after accepted Q012 corrective pass | Q012 closed |
| M11 Rust cutover/package/versioning | [cutover-roadmap](subsystems/cutover-roadmap.md) | closed after accepted K012/K014 recovery chain | M11 closed |
| M12 Python application retirement | [python-retirement-roadmap](subsystems/python-retirement-roadmap.md) | **implementation planning complete; P001/P002 accepted/closed; P003 dependency-ready** | **P003 dependency-ready** |

## Dependency-ready implementation plans

| ID | Plan | Class | Dependencies | Status |
|---|---|---|---|---|
| M12-P003 | [Python application source and runtime-asset retirement](implementation/retirement/003-python-application-source-and-runtime-asset-retirement.md) | invariant/infrastructure | accepted P001/P002; ADR-0005 | **dependency-ready** |

P004-P006 are registered but serially blocked by their direct predecessors. P003 is now authorized as the first destructive Python application removal plan; no P004 work is authorized until P003 closes.

## M12 implementation closure handoffs

| ID | Plan | Class | Implementation commit | Closure |
|---|---|---|---|---|
| M12-P001 | [Final Python reference boundary and fixture freeze](implementation/retirement/001-final-python-reference-boundary-and-fixture-freeze.md) | invariant/infrastructure | reference manifest and handoff evidence | [accepted/closed](closure/retirement/001-status.md) |
| M12-P002 | [Rust production package, catalog, and cross-era authority](implementation/retirement/002-rust-production-package-catalog-and-cross-era-authority.md) | infrastructure/invariant | `36605a8d890855fa255b9cf96b1ba29f914a9826` | [accepted/closed](closure/retirement/002-status.md) |

## Recently closed corrective plans

| ID | Plan | Status |
|---|---|---|
| K013 | [PyPI publication recovery workflow correction](implementation/cutover/013-pypi-publication-recovery-workflow-correction.md) | accepted/closed; append-only recovery evidence in [closure](closure/cutover/013-status.md) |
| K014 | [PyPI Trusted Publisher configuration and recovery completion](implementation/cutover/014-pypi-trusted-publisher-configuration-and-recovery-completion.md) | accepted/closed; [closure](closure/cutover/014-status.md) |
| K012 | [Aggregate M11 cutover qualification and closure](implementation/cutover/012-aggregate-m11-cutover-qualification-and-closure.md) | accepted/closed; [closure](closure/cutover/012-status.md) |

Q012 re-closed M10. K001-K014 are accepted/closed and M11 is closed. M12 planning is complete under accepted ADR-0005: P001/P002 are accepted/closed, P003 is the sole dependency-ready plan, and P004-P006 remain queued. Historical Python public artifacts remain immutable exact-version evidence, and compatible explicit historical transitions are preserved by M12 rather than removed.

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
| Q001 | [Qualification contract, target matrix, and evidence schema freeze](implementation/qualification/001-qualification-contract-target-matrix-and-evidence-freeze.md) | invariant/infrastructure | `4b5093a30a56f78c14aef3ffef8c47863fc74070` | [closed](closure/qualification/001-status.md) |
| Q002 | [Migration-wide deterministic differential qualification runner](implementation/qualification/002-migration-wide-differential-qualification-runner.md) | invariant/polish | `de50b7a27aa7829a41baa5c6a8ff5e08248dd401` | [accepted](closure/qualification/002-status.md) |
| Q003 | [Database upgrade, rollback, backup, and recovery compatibility](implementation/qualification/003-database-upgrade-rollback-backup-recovery-compatibility.md) | invariant | `482856a898c40979c70106bd29958fcc454115ca` | [accepted](closure/qualification/003-status.md) |
| Q004 | [Dashboard SSR, DOM, static asset, and visual parity review](implementation/qualification/004-dashboard-dom-static-and-visual-parity.md) | invariant/polish | `73fad30f4eb79bcc75c38bd33a72c5b993f970cc` | [historical accepted closure; superseded by Q012 for dashboard-state/content findings](closure/qualification/004-status.md) |
| Q005 | [Supported-target build and non-root runtime portability](implementation/qualification/005-supported-target-build-and-runtime-portability.md) | invariant/polish | `76d329f3` | [accepted](closure/qualification/005-status.md) |
| Q006 | [Disposable rootful Linux operational acceptance](implementation/qualification/006-rootful-linux-operational-acceptance.md) | invariant/capability | `c090ca0d` | [accepted](closure/qualification/006-status.md) |
| Q007 | [Bounded live-provider interoperability smoke](implementation/qualification/007-live-provider-interoperability-smoke.md) | invariant/polish | `a0b2e75`, `c3e045c`, corrective `daae984` | [historical blocked + corrective accepted](closure/qualification/011-status.md) |
| Q008 | [ARM64 SBC functional and resource characterization](implementation/qualification/008-arm64-sbc-functional-and-resource-characterization.md) | invariant/polish | `ed82ed29`, `64ff9b4d` | [accepted by addendum](closure/qualification/008-status.md) |
| Q009 | [Sustained failure, reload, streaming, and resource-stability qualification](implementation/qualification/009-sustained-failure-reload-stream-resource-stability.md) | invariant/polish | `0989d6e4`, corrective `daae984` | [accepted by addendum](closure/qualification/009-status.md) |
| Q010 | [Aggregate M10 closure and M11 readiness report](implementation/qualification/010-aggregate-m10-closure-and-m11-readiness.md) | invariant/polish | `4d28e290`, corrective `daae984` | [historical aggregate closure; superseded for current M10 closure authority by Q012](closure/qualification/010-status.md) |
| Q011 | [Q007 live-provider corrective closure](implementation/qualification/011-q007-live-provider-corrective-closure.md) | invariant/polish | `daae984` | [accepted](closure/qualification/011-status.md) |
| Q012 | [Dashboard state, semantic content, and visual requalification](implementation/qualification/012-dashboard-state-semantic-content-and-visual-requalification.md) | invariant/corrective | `bcc96c8`, `b41a9ae` | [accepted/closed](closure/qualification/012-status.md) |
| K001 | [Cutover, package, and installable-version catalog contract freeze](implementation/cutover/001-cutover-package-and-version-catalog-contract-freeze.md) | invariant/infrastructure | `47905d54307afb6c73ccc7403a7dfa8702c36c22` | [closed](closure/cutover/001-status.md) |
| K002 | [Rust PyPI binary-wheel packaging substrate](implementation/cutover/002-rust-pypi-binary-wheel-packaging-substrate.md) | infrastructure/invariant | `aec2b98f291c47e983773c42b0d19649973e3377` | [closed](closure/cutover/002-status.md) |
| K003 | [Supported wheel and raw release artifact matrix](implementation/cutover/003-supported-wheel-and-raw-artifact-matrix.md) | infrastructure/capability | `a15adc4` + follow-up qualification commits | [closed](closure/cutover/003-status.md) |
| K004 | [Install provenance and package-manager transition engine](implementation/cutover/004-install-provenance-and-package-manager-transition-engine.md) | invariant/capability | `b33658be47d337c2c8a875b41d50ff436eb0f10b` | [closed](closure/cutover/004-status.md) |
| K005 | [Cross-era exact version transitions and rollback](implementation/cutover/005-cross-era-exact-version-transitions-and-rollback.md) | invariant/capability | `393745ba5c8afe46024923922f360672ee92aa6e` | [closed](closure/cutover/005-status.md) |
| K006 | [Quick installer and existing-install adoption cutover](implementation/cutover/006-quick-installer-and-existing-install-adoption-cutover.md) | capability/invariant | `d8bddcb1f931bbdc0f2682213c300fe3e976d124`, `aaad99b2f6e9f8f8ea67634a1fc2a54274e3b841` | [closed](closure/cutover/006-status.md) |
| K007 | [Deployed-service cross-era transition and recovery](implementation/cutover/007-deployed-service-cross-era-transition-and-recovery.md) | invariant/capability | `e8aff344accaed82cc92378b9bc0dd3f4691e586`, `6ebc27efc00b1990be23f9260d7e18f8b1b18c48`, `83622501e498ef662b1549fd3e241cc44ab9fec6` | [accepted/closed](closure/cutover/007-status.md) |
| K008 | [Trusted publishing, attestations, and release supply chain](implementation/cutover/008-trusted-publishing-attestations-and-release-supply-chain.md) | infrastructure/invariant | `fcb9956e`, `b0c7b34f` | [accepted/closed](closure/cutover/008-status.md) |
| K009 | [Local wheelhouse and TestPyPI staged release rehearsal](implementation/cutover/009-wheelhouse-testpypi-staged-release-rehearsal.md) | invariant/polish | `7c196e6`, `e5fb9a5`, closure commit | [accepted/closed](closure/cutover/009-status.md) |
| K010 | [Public metadata, documentation, and release-candidate freeze](implementation/cutover/010-public-metadata-docs-and-release-candidate-freeze.md) | invariant/polish | `1d17fd1c4c597adf1161b813f5b958117370a500`, closure transition commit | [accepted/closed](closure/cutover/010-status.md) |
| K011 | [First Rust-backed public release and immediate rollback drill](implementation/cutover/011-first-rust-public-release-and-rollback-drill.md) | capability/invariant | `431cad4f`, `a3dc0e7b` | [accepted/closed by addendum](closure/cutover/011-status.md) |
| K013 | [PyPI publication recovery workflow correction](implementation/cutover/013-pypi-publication-recovery-workflow-correction.md) | infrastructure/capability | `3d15473`, `5aef520`, `257f35c`, `a3dc0e7b` | [accepted/closed by addendum](closure/cutover/013-status.md) |
| K014 | [PyPI Trusted Publisher configuration and recovery completion](implementation/cutover/014-pypi-trusted-publisher-configuration-and-recovery-completion.md) | infrastructure/capability | `a3dc0e7b` + external publisher configuration | [accepted/closed](closure/cutover/014-status.md) |
| K012 | [Aggregate M11 cutover qualification and closure](implementation/cutover/012-aggregate-m11-cutover-qualification-and-closure.md) | invariant/polish | `a3dc0e7b` + public runs `34597248849`, `34598462704` | [accepted/closed by addendum](closure/cutover/012-status.md) |
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

## M9 closure state

M9 is closed after accepted O010. O001-O010 are closed. The complete Rust CLI/operations surface and all process task callbacks are implemented and qualified.

## M10 closure state

M10 is closed after accepted Q012. Q001-Q003/Q005-Q009/Q011 evidence is accepted; Q004/Q010 remain historical for the dashboard finding; Q012 is the current closure authority.

## M11 closure state

M11 is closed. K001-K014 are accepted/closed, with the initial blocked K011/K012/K013 records preserved as append-only history. Rust `0.8.0` is the canonical public runtime on the three qualified target classes; compatible historical exact-version transitions remain part of the package-manager contract.

## M12 sequence and state

M12 removes the historical Python application from the current production/runtime and active source tree without removing immutable historical versions from the user-facing exact-version catalog.

| ID | Plan | Dependency state |
|---|---|---|
| P001 | [Final Python reference boundary and fixture freeze](implementation/retirement/001-final-python-reference-boundary-and-fixture-freeze.md) | **accepted/closed** |
| P002 | [Rust production package, catalog, and cross-era authority](implementation/retirement/002-rust-production-package-catalog-and-cross-era-authority.md) | **accepted/closed** |
| P003 | [Python application source and runtime-asset retirement](implementation/retirement/003-python-application-source-and-runtime-asset-retirement.md) | **dependency-ready** |
| P004 | [Oracle, differential, test, and Python tooling retirement](implementation/retirement/004-oracle-differential-test-and-python-tooling-retirement.md) | queued behind P003 |
| P005 | [Repository, installer, release, and documentation consolidation](implementation/retirement/005-repository-installer-release-and-documentation-consolidation.md) | queued behind P004 |
| P006 | [Rust-only qualification and M12 closure](implementation/retirement/006-rust-only-qualification-and-m12-closure.md) | queued behind P005 |

ADR-0005 is accepted. P001 and P002 are accepted/closed; P003 is the authorized first Python application-source deletion; P004 retires live-oracle machinery; P005 consolidates repository/release/docs; P006 is the sole M12 closure authority.

Historical public Python releases are immutable external artifacts. Compatible explicit package-managed historical targets remain supported, while latest/default resolution and all current/future publication remain Rust-only.

## Future work and block state

No post-M12 migration milestone is planned. After accepted P006 closure, migration governance transitions back to normal EggPool product/maintenance planning. Failed M12 gates create bounded corrective P-plans rather than a new broad migration phase.

## Closure state

F001-F006, M4 T001-T006, M5 D001-D009, M6 W001-W012, M7 C001-C011 with C012-C014 corrective passes, M8 R001-R013, M9 O001-O010, M10 through Q012, and M11 K001-K014 remain closed. M12 implementation planning is complete; P001/P002 are accepted/closed, P003 is dependency-ready, and P004-P006 remain gated by direct predecessors. M12 itself remains open until accepted P006 closure.
