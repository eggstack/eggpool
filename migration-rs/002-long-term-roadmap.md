# EggPool Rust Migration — Long-Term Roadmap

Status: active canonical roadmap

Planning baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

This roadmap orders the migration by correctness dependencies. It is deliberately not a calendar estimate.

## End-state dependency chain

```text
M0 Foundation + oracle
  -> M1 Config/CLI/filesystem
  -> M2 SQLite/repositories
  -> M3 HTTP read/control plane + SSR
  -> M4 Provider HTTP + Eggress outbound
  -> M5 Catalog/routing/quota/health
  -> M6 Canonical request + codecs/transcoding/SSE
  -> M7 Coordinator/retry/finalization
  -> M8 Runtime generations/rehash/background lifecycle
  -> M9 Full operational CLI/lifecycle/update/deploy
  -> M10 Differential qualification + SBC characterization
  -> M11 Rust cutover
  -> M12 Python retirement
```

No milestone may bypass correctness dependencies merely to declare closure.

## M0 — Migration foundation and behavioral oracle

Primary class: infrastructure/invariant

Establish the isolated Rust scaffold, migration rules, Python/Rust black-box oracle, contract inventory, normalization policy, deterministic fixtures, and minimal verification posture.

Exit condition: one Rust behavior can be added and measured against Python without ad hoc comparison. Satisfied after F006 corrective closure.

## M1 — Configuration, CLI parser, and filesystem contract

Primary class: capability/invariant

Port configuration/defaults/validation/env/path ownership and the CLI command-tree contract. Parser/help/validation/exit behavior must remain explicit even where underlying commands are staged.

Exit condition: config/path/CLI compatibility corpus matches the frozen contract. Included in the closed foundation sequence.

## M2 — SQLite schema, migrations, and repository layer

Primary class: infrastructure/invariant

Reuse existing numbered migrations/checksums and serialized SQLite access. Preserve Python-created DB readability and supported Rust-to-Python rollback compatibility.

Exit condition: schema/checksum/repository semantics are compatible. Included in closed F004.

## M3 — HTTP read/control plane and SSR dashboard

Primary class: capability

Use Tokio/Axum for inbound HTTP, auth/body limits, health/readiness, available read/control endpoints, SSR rendering, static assets, and dashboard routing without redesign.

Exit condition: selected read/control/dashboard surfaces pass differential tests. Foundation/F005 established the migration-stage baseline.

## M4 — Provider HTTP stack and Eggress outbound proxy integration

Primary class: infrastructure/capability

Subsystem roadmap: [Provider Transport](subsystems/provider-transport-roadmap.md).

Hyper/Hyper-util/Rustls provider/account pools plus in-process Eggress proxy connector. Exact proxy surface is corpus-qualified; unsupported forms fail closed.

Completed sequence: T001 -> T002 -> T003 -> T004 -> T005 -> T006 corrective runtime interoperability closure.

Exit condition satisfied after T006.

## M5 — Catalog, account registry, routing, quota, health, and model-router state

Primary class: capability/invariant

Subsystem roadmap: [Routing Domain and Catalog State](subsystems/routing-domain-roadmap.md).

Port deterministic catalog/account/eligibility/fairness/claim/quota/backoff/circuit/quarantine/model-router/affinity state before inference orchestration.

Completed sequence: D001 -> D002 -> D003 -> D004/D005 -> D006 -> D007 -> D008 -> D009 corrective selection-fairness/frozen-trace closure.

M5 local selection claims stop before durable inference persistence. Semantic model-router selector calls that require the coordinator remain M7. Generic external catalog/background polling remains M8.

Exit condition satisfied after D009.

## M6 — Canonical request boundary, wire codecs, transcoding, and SSE

Primary class: capability/invariant

Subsystem roadmap: [Canonical Request and Wire Codec Runtime](subsystems/canonical-wire-roadmap.md).

Own deterministic bounded request admission, canonical IR, static wire profiles/codecs, semantic adaptation/loss policy, media/documents/cache controls, finite response transformations, SSE/event conversion, normalized usage, terminal evidence, and one caller-selected-profile runtime.

Historical sequence: W001 -> W002 -> W003 -> W004 -> W005 -> W006 -> W007 -> W008 -> W009 -> W010. Post-W010 review added W011 SSE EOF UTF-8 correction and W012 full cross-surface differential requalification/re-closure.

Dynamic learned wire preference/rejection/negotiation/retry, provider send, response handoff, timeout/cancellation, effects, and durable finalization remained M7.

Exit condition satisfied after W011/W012.

## M7 — Coordinator, retry/failover, and durable finalization

Primary class: invariant/capability

Subsystem roadmap: [Coordinator, Retry, Failover, and Durable Finalization](subsystems/coordinator-roadmap.md).

M7 composed closed M4 transport, M5 routing/claim state, and M6 selected-profile runtime into the Rust request/attempt lifecycle. It owns durable dispatch publication, runtime wire negotiation, provider-bound attempt submission, canonical failure effects, bounded account/wire retry, response handoff, finite/streaming completion, timeout/cancellation, retained terminal ownership, public inference endpoints, semantic-router internal dispatch, and deterministic restart reconciliation.

Accepted sequence:

```text
C001 -> C002 -> C003-C006 historical core
  -> C012 coordinator core correction
  -> C013 coordinator core requalification
  -> C014 finalization/Retry-After closure
  -> C007 finite handoff
  -> C008 streaming lifecycle
  -> C009 public endpoints/semantic dispatch
  -> C010 crash/restart reconciliation
  -> C011 aggregate qualification/M7 closure
```

C012-C014 corrected the post-C006/post-C013 findings without rewriting historical closure evidence. C011 then qualified the integrated endpoint/coordinator/recovery surface and closed M7 with no unresolved high/medium correctness or security finding.

M7 exposes stable bounded interfaces for M8: `InferenceState`, finite/stream execution, finalization supervisor drain/reconcile, crash reconciler, routing/publication ownership, wire resolver, and failure/effect state. M7 deliberately owns no active-generation publication, live rehash, process signal/shutdown orchestration, or recurring background scheduler.

Exit condition satisfied after accepted C011 closure.

## M8 — Runtime generations, rehash, background tasks, and process lifecycle

Primary class: infrastructure/capability/invariant

Subsystem roadmap: [Runtime Generations, Rehash, Background Tasks, and Process Lifecycle](subsystems/runtime-lifecycle-roadmap.md).

M8 replaced static Rust server state and Python/Granian generation/process machinery with a Rust-native process runtime. It owns immutable generation snapshots, shared startup/reload generation factory, explicit candidate abort, `ArcSwap` active publication, linearizable request/stream leases, old-generation retirement after retained-finalization drain, exhaustive fail-closed reload policy, serialized transactional rehash, one bounded process task supervisor, generation-leased maintenance, startup crash reconciliation, process signals/graceful/forced shutdown, process-owned wire policy, and runtime/reload diagnostics.

Accepted sequence and corrective history:

```text
R001 runtime/reload oracle freeze
 -> R002 process runtime + generation factory + candidate ownership
 -> R003 active manager + atomic publication + request/stream leases
 -> R004 retirement + finalization drain + resource close
 -> R005 config diff/reload policy/redaction
 -> R006 task supervisor + staged task specs
 -> R007 transactional live rehash
 -> R008 generation-leased maintenance/recovery/background integration
 -> R009 server startup/signals/shutdown
 -> R010 active-generation authority/diagnostics
 -> R011 differential qualification/initial M8 closure
 -> R012 wire-negotiation runtime authority + reload-diagnostics historical corrective closure
 -> R013 wire-policy validation/acceptance + boundary requalification
```

R011/R012 remain append-only historical closure evidence. Accepted R013 fixed the remaining process wire-policy validation/acceptance/rollback and real-inference qualification defects and re-closed M8. R013 closure recorded no unresolved high/medium M8 finding.

R008 left exactly three business capabilities intentionally deferred for M9: `metrics_flush`, `update_checker`, and `automatic_backup`. M8 owns their scheduler/task-spec machinery; M9 owns the real callbacks. O006 registered `automatic_backup`, O007 registered `metrics_flush`, and O008 registered `update_checker`; O010 accepted the complete singleton/background-task boundary.

Exit condition satisfied after accepted R013 closure.

## M9 — Operational CLI and lifecycle completeness

Primary class: capability/invariant

Subsystem roadmap: [Operational CLI, Lifecycle, Update, and Deployment](subsystems/operational-cli-lifecycle-roadmap.md).

M9 converted the complete F003 Rust parser surface into real operational behavior and wired it to the closed M4-M8 services. It owns local control/runtime paths/process state, daemon lifecycle commands, live rehash/status/watchdogs, config/key/provider onboarding mutations, agent integration rendering, migrations/DB/backup/recover, operator inspection/model/stats maintenance, the three R008 deferred background callbacks, update/version behavior, local deployment artifacts, and uninstall.

Accepted sequence:

```text
O001 operational CLI contract + deterministic oracle freeze
 -> O002 local control/runtime paths/process state
 -> O003 lifecycle/daemon/rehash/status/watchdog commands
 -> O004 config/key/provider onboarding + live apply
 -> O005 agent integration/configsetup generation
 -> O006 migrations/DB/backup/recover + automatic backup
 -> O007 operator inspection/maintenance + metrics flush
 -> O008 update/version + update checker
 -> O009 deploy/install artifacts + uninstall
 -> O010 differential qualification + M9 closure
```

O010 is accepted and M9 is closed with the complete command surface, background callbacks, and operational fault matrices qualified. M9 still deliberately leaves broad platform/SBC/live/visual qualification and public Rust-default distribution to M10/M11.

Exit condition satisfied after accepted O010 closure.

## M10 — Full differential qualification and SBC characterization

Primary class: invariant/polish

Subsystem roadmap: [Full Qualification, Portability, and SBC Characterization](subsystems/qualification-roadmap.md).

M10 is the final evidence milestone before public Rust cutover planning. It verifies the accumulated M4-M9 compatibility claims across deterministic migration-wide behavior, DB rollback/backup/recovery, dashboard rendering, supported targets, real Linux deployment, bounded live-provider interoperability, representative ARM64 SBC operation, and sustained failure/resource stability.

Sequence and corrective history:

```text
Q001 qualification contract + target/evidence freeze
 -> Q002 migration-wide deterministic differential runner
 -> Q003 DB upgrade/rollback/backup/recovery compatibility
 -> Q004 dashboard DOM/static/visual parity (historical accepted closure)
 -> Q005 supported-target build/non-root runtime portability
 -> Q006 disposable rootful Linux operational acceptance
 -> Q007 bounded live-provider attempt (historical blocked)
 -> Q008 ARM64 SBC functional/resource characterization
 -> Q009 sustained failure/reload/stream/resource stability
 -> Q010 aggregate closure/re-acceptance (historical current closure before audit)
 -> Q011 live-provider corrective closure and dependency-order re-acceptance
 -> Q012 dashboard state/semantic content/actual visual requalification
```

Accepted Q011 successfully closed the live-provider blocker with a bounded real-provider matrix, and Q008-Q010 were re-accepted in dependency order. A later audit found that Q004 did not actually satisfy the frozen mandatory dashboard-state contract: populated/multi-provider states were only reserved in metadata, semantic data rows/card values were not compared, and the screenshot matrix primarily contained planned filenames rather than actual captures. Q010 inherited that gap.

M10 is closed after the accepted Q012 corrective pass. Q004 and Q010 remain historical accepted closure evidence for what they actually proved; Q012 is the current M10 closure authority.

M10 keeps normal CI intentionally lean. Expensive/rootful/live/physical/browser qualification remains explicit/manual evidence rather than an always-on matrix. Performance/resource facts remain characterization unless they expose correctness problems.

Exit condition satisfied by accepted Q012 closure.

## M11 — Rust cutover

Primary class: capability/invariant

Subsystem roadmap: [Rust Cutover, Packaging, Release, and Cross-Era Versioning](subsystems/cutover-roadmap.md).

M11 makes the qualified Rust implementation the canonical public EggPool runtime without abandoning the existing PyPI package identity or the Python-era rollback window. ADR-0004 freezes the packaging authority: `eggpool` remains the one PyPI project; Rust-backed releases are platform-specific Maturin `bin` wheels that install the native executable; package-manager-owned installs update/downgrade through uv/pipx/pip rather than direct binary overwrite; standalone Rust installs retain O008's verified GitHub raw updater; Python source/oracle remains until M12.

Planned sequence:

```text
K001 cutover/package/version-catalog contract freeze
 -> K002 Rust PyPI binary-wheel packaging substrate
 -> K003 supported wheel + raw artifact matrix/release manifest
 -> K004 install provenance + package-manager transition engine
 -> K005 cross-era exact upgrade/downgrade + rollback
 -> K006 quick installer + existing-install adoption cutover
 -> K007 deployed-service cross-era transition/recovery
 -> K008 Trusted Publishing/attestations/release supply chain
 -> K009 local wheelhouse/TestPyPI staged release rehearsal
 -> K010 public metadata/docs/release-candidate freeze
 -> K011 first Rust-backed public release + immediate rollback drill
 -> K012 aggregate M11 qualification/closure
```

Only `registry.md` authorizes handoff. K001-K009 are closed and K010 is the sole dependency-ready M11 plan.

M11 preserves the PyPI user experience. Existing `pip install eggpool`, `pipx install eggpool`, and `uv tool install eggpool` workflows remain viable; the installed payload becomes the native Rust binary on qualified targets. The Rust wheel retains `Requires-Python >=3.11` during M11 as a package-manager rollback compatibility floor even though normal EggPool runtime does not invoke Python. M12 may reconsider that metadata after Python retirement.

M11 must resolve the historical distribution gap explicitly: at planning time public PyPI ends at 0.5.6 while GitHub/repository versions reach 0.7.4. K001 freezes an installable-release catalog and decides which missing official Python versions can be reproducibly backfilled as immutable PyPI wheels, which need a pinned immutable fallback, and which cannot truthfully be advertised as exact switch targets. M11's guarantee is every version in that frozen installable catalog, not every tag irrespective of artifact availability.

Release targets inherit M10: Linux x86_64, Linux aarch64, and macOS arm64 development/runtime. Rust-backed M11 PyPI releases are wheel-only; no Rust sdist, Windows wheel, or other-unqualified fallback is published. Release builds use pinned Maturin/tooling, exact Cargo lock state, explicit manylinux policy, one source revision, hashes/manifests, and PyPI Trusted Publishing/OIDC. Production publication occurs only in K011 after staged rehearsal.

Exit condition: the first Rust-backed stable PyPI wheel set and matching GitHub raw assets are public on every required supported target; fresh package installs execute Rust; existing Python installs upgrade in place without config/DB relocation; exact package-managed Python -> Rust -> Python -> Rust transitions are demonstrated over the documented rollback window; deployed service state survives those transitions; unsupported targets fail cleanly with no source fallback; public installer/docs are Rust-default; release integrity/provenance is accepted; no unresolved high/medium cutover, package-ownership, update, rollback, security or data-loss finding remains. Satisfied only by accepted K012 closure.

## M12 — Python retirement

Primary class: polish/invariant

After stabilization, remove Python production/runtime packaging and migration-only dual-run machinery while preserving reference history and useful differential fixtures.

M12 is blocked on accepted K012 M11 closure and requires its own separate planning review. M11 does not auto-promote Python removal.

Exit condition: production repository/release path is pure Rust with traceable parity evidence.

## Cross-cutting constraints

At every milestone:

- Python remains usable until cutover and remains as reference through M11;
- no dashboard redesign is folded into migration work;
- no database reset or Rust-only schema fork for convenience;
- no broad CI matrix without demonstrated need;
- secrets/proxy credentials remain redacted;
- unsupported behavior fails closed;
- implementation plans remain bounded with accepted closure evidence;
- local/SBC scope does not justify cloud/distributed orchestration frameworks.
