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

M8 replaces static Rust server state and Python/Granian generation/process machinery with a Rust-native process runtime. It owns immutable generation snapshots, one shared startup/reload generation factory, explicit candidate abort, `ArcSwap` active publication, linearizable request/stream leases, old-generation retirement after M7 retained-finalization drain, exhaustive fail-closed reload policy, serialized transactional rehash, one bounded process task supervisor, generation-leased recurring maintenance, startup crash-recovery scheduling, process signals/graceful/forced shutdown, and active-generation runtime diagnostics.

Planned sequence:

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
 -> R011 differential qualification/M8 closure
```

Only `registry.md` authorizes handoff. R011 is the current dependency-ready M8
plan after accepted R010 closure.

M8 keeps M9 operational surfaces out of scope. It exposes the server-side typed reload/runtime/task/shutdown APIs that M9 will use, but does not implement `eggpool rehash`, daemon/control socket, stop/restart/install/systemd/croncheck, backup/recover CLI, update CLI, or packaging.

A major M8 closure condition is elimination of stale startup-generation authority: finite/streaming requests, live request-body limits, readiness, model/routing reads, and generation-dependent diagnostics must use one acquired generation for the relevant async operation. Constructor-owned fields explicitly classified restart-required may remain startup-owned.

Exit condition: live rehash does not interrupt/mix in-flight work; invalid/restart/mixed/failed reloads leave old runtime/DB/task state coherent; retirement waits for request leases and retained finalization; background generation-dependent ticks cannot stay stale across publication; startup recovery and graceful/forced shutdown converge without provider replay or DB reset; diagnostics remain bounded/secret-free; no unresolved high/medium M8 finding remains. Satisfied only by accepted R011 closure.

## M9 — Operational CLI and lifecycle completeness

Primary class: capability

Complete serve/daemon/stop/restart/deploy/croncheck, backup/recover, migrations, update/version, onboarding/connect/logout, config/key management, diagnostics, uninstall, and documented operational commands. Wire the M8 server-side reload/status/shutdown/task interfaces into the user-facing control/CLI workflow. Packaging follows only when binary behavior exists.

Exit condition: documented CLI workflow parity on supported targets.

## M10 — Full differential qualification and SBC characterization

Primary class: invariant/polish

Run the complete contract matrix, targeted live-provider smoke tests, dashboard visual review, DB rollback/upgrade, failure/restart, and representative ARM64 SBC resource characterization. Do not invent unsupported performance gates.

Exit condition: mandatory compatibility gaps are closed or approved by ADR.

## M11 — Rust cutover

Primary class: capability

Make Rust the canonical install/release/runtime implementation while preserving filesystem/config/database locations and documented rollback to the final Python reference where schema compatibility permits.

Exit condition: new installs/upgrades use Rust by default without Python runtime dependency.

## M12 — Python retirement

Primary class: polish/invariant

After stabilization, remove Python production/runtime packaging and migration-only dual-run machinery while preserving reference history and useful differential fixtures.

Exit condition: production repository/release path is pure Rust with traceable parity evidence.

## Cross-cutting constraints

At every milestone:

- Python remains usable until cutover;
- no dashboard redesign is folded into migration work;
- no database reset or Rust-only schema fork for convenience;
- no broad CI matrix without demonstrated need;
- secrets/proxy credentials remain redacted;
- unsupported behavior fails closed;
- implementation plans remain bounded with accepted closure evidence;
- local/SBC scope does not justify cloud/distributed orchestration frameworks.
