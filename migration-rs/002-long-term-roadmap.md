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

Dynamic learned wire preference/rejection/negotiation/retry, provider send, response handoff, timeout/cancellation, effects, and durable finalization remain M7.

Exit condition satisfied after W011/W012.

## M7 — Coordinator, retry/failover, and durable finalization

Primary class: invariant/capability

Subsystem roadmap: [Coordinator, Retry, Failover, and Durable Finalization](subsystems/coordinator-roadmap.md).

This is the highest-risk migration milestone. Compose closed M4 transport, M5 routing/claim state, and M6 selected-profile runtime into an explicit request/attempt state machine. Port durable dispatch publication, runtime wire negotiation, provider-bound attempt submission, canonical failure effects, bounded account/wire retry, response handoff, finite/streaming completion, timeout/cancellation, retained terminal ownership, public inference endpoints, semantic-router internal dispatch, and restart reconciliation.

Original sequence:

C001 contract/failure corpus -> C002 durable dispatch publication/lifecycle identity -> C003 runtime wire resolution/negotiation -> C004 provider-bound attempt/submission -> C005 failure effects/retry/failover -> C006 durable finalization/retained ownership -> C007 finite handoff/completion -> C008 streaming/timeouts/cancellation -> C009 public inference endpoints/semantic-router dispatch -> C010 crash/restart reconciliation/fault injection -> C011 differential qualification/M7 closure.

Post-C006 audit found material contract/qualification gaps in C003-C006. Historical closure records remain append-only. The active corrective insertion is:

```text
C003-C006 historical implementation
  -> C012 coordinator core contract correction
  -> C013 coordinator core differential requalification
  -> C007 finite handoff/completion
  -> C008 -> C009 -> C010 -> C011
```

C012 repairs missing fixed/hinted/rate-limited wire semantics and state bounds, preserves provider-native `upstream_model_id` through C004, completes header/request evidence, restores the full C001 failure/effect distinctions including ambiguous-auth behavior, bounds/retires effect ownership, and makes durable finalization re-read zero-row transitions and reject incompatible retained commands. C013 independently proves those fixes against the C001 Python oracle plus deterministic M4, concurrency, boundedness, and finalization fault fixtures.

Only `registry.md` authorizes handoff. C012 is currently the sole dependency-ready plan; C013 is queued; C007 has been re-blocked until accepted C013 closure.

M7 implements a bounded retained-finalization supervisor and explicit reconciliation interface because terminal cleanup cannot depend on the client task. M8, not M7, owns immutable runtime-generation publication, rehash, signal/shutdown orchestration, and recurring/background scheduling around those interfaces.

Response-start is a monotonic point of no return: transparent retries are pre-handoff only. Failed attempts become independently durable-terminal or retained-cleanup-owned before replacement attempt ownership is accepted. Unknown in-flight provider work is never replayed merely because the Rust process restarted.

Exit condition: the C001 failure corpus plus C013/C011 integrated qualification prove parity for success, retry, alternate-wire/account failover, rejection, cancellation, partial/malformed stream, terminal evidence, DB/runtime cleanup faults, public endpoint semantics, retained finalization, and restart reconciliation, with no unresolved high/medium M7 correctness/security issue. Satisfied only by accepted C011 closure after the corrective core passes C013.

## M8 — Runtime generations, rehash, background tasks, and process lifecycle

Primary class: infrastructure/capability

After M7 closure, replace Python/Granian generation/process machinery with Rust-native immutable generation snapshots, reference-counted leases, atomic publication, ownership of the M7 finalization supervisor, live rehash, process-level state, signal/shutdown handling, and bounded recurring background tasks.

Exit condition: live rehash does not interrupt in-flight work; shutdown/restart semantics converge; runtime diagnostics remain compatible.

## M9 — Operational CLI and lifecycle completeness

Primary class: capability

Complete serve/daemon/stop/restart/deploy/croncheck, backup/recover, migrations, update/version, onboarding/connect/logout, config/key management, diagnostics, uninstall, and documented operational commands. Packaging follows only when binary behavior exists.

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