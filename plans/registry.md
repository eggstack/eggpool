# EggPool Active Planning Registry

This file is the compact control surface for active interim planning.
Detailed requirements and completed history remain in source roadmaps,
implementation plans, `plans/closure/`, flat legacy plans, and Git history.
Links only — do not duplicate milestone requirements here.

Canonical direction:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Legacy archive (pre-251, immutable, top level): `plans/001-*` through
`plans/250-*` plus `python_hotpath_dispatch_compression_optimization.md`.
Most recently closed: Persistence M005 (M004 evidence and planning
reconciliation corrective pass, `plans/closure/persistence/005-status.md`).
Legacy archive latest: Plan 250
(EggServe 0.3.0 direct-Tower migration, `7879cbf9`). Plans 244–245, 215–220,
241 remain historical per their own closure passes; the `146-*` duplicate
pair is a known numbering accident.

## Status vocabulary

- **proposed** — roadmap or plan exists but is not approved for execution.
- **ready** — dependencies and interfaces satisfied; may be handed off.
- **active** — implementation or closure work in progress.
- **blocked** — a named dependency or evidence requirement prevents progress.
- **closing** — implementation landed; closure evidence being gathered.
- **closed** — closure record accepted.
- **conditionally closed** — substantial work landed, named correctness or
  operational evidence remains (condition + risk + exact future evidence in
  the closure record).
- **superseded** — replaced by another document.
- **archived** — no longer active; retained for traceability.

## Active subsystem roadmaps

| Subsystem | Status | Roadmap | Current milestone | Dependencies or blockers |
|---|---|---|---|---|
| Provider transport | active | `plans/subsystems/provider-transport-roadmap.md` | M002 blocked — stable Eggfetch transport error taxonomy | Requires a published upstream typed classification interface. |
| Routing selection | active | `plans/subsystems/routing-selection-roadmap.md` | M001 closed — ordered quota-scoring and candidate-allocation cleanup | M002 stays evidence-gated (no affinity workload measured yet). |
| Persistence | active | `plans/subsystems/persistence-roadmap.md` | M005 closed — M004 evidence/planning reconciliation corrective pass | M004's rejection outcome stands with its narration corrected by `plans/closure/persistence/005-status.md`. M003 is `proposed`, not `ready`: its evidence dependency is satisfied but no architecture review or `003` implementation plan exists. |

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| — | none | — | — | No dependency-ready implementation plan remains. Persistence M005 closed; persistence M003 is `proposed` and needs its architecture review plus a registered `003` plan first. |

## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| Provider transport | M002 stable Eggfetch transport error taxonomy | Upstream Eggfetch does not yet expose/publish a general-purpose typed classification surface sufficient to replace the remaining Hyper/Rustls source-chain inspection; requires separate upstream planning. |

## Recently closed

| Subsystem / plan | Disposition | Evidence |
|---|---|---|
| Persistence M005 — M004 evidence and planning reconciliation corrective pass | closed — M004 evidence interpretation and M003 lifecycle corrected | `plans/closure/persistence/005-status.md`, guard `tests/tooling/test_persistence_m004_evidence.py` (17 tests; 14 accepted M004 artifacts machine-checked; zero production Rust diff) |
| Persistence M004 — physical checkpoint qualification and final disposition (periodic strategy rejected on target) | closed — M003's evidence dependency satisfied; narration corrected by M005 | `plans/closure/persistence/004-status.md`, implementation `8113d264` (zero production Rust diff; 14 accepted Pi 5 / ext4 / MMC physical artifacts at `artifacts/qualification/m004/`) |
| Routing selection M001 — ordered quota-scoring and candidate-allocation cleanup | closed | `plans/closure/routing-selection/001-status.md`, implementation `4db8000d` |
| Persistence M001 — bounded passive checkpoint scheduling and target qualification | conditionally closed (periodic claim now disproven by M004; mechanism retained as additive-safe) | `plans/closure/persistence/001-status.md`, implementation `6eae94db` |
| Persistence M002 — single-gate tenure and metrics-flush allocation cleanup | closed | `plans/closure/persistence/002-status.md`, implementation `52494140` |
| Request admission and wire M001 — inference body resource admission hardening | closed | `plans/closure/request-admission-wire/001-status.md`, implementation `a87790ad` |
| Provider transport M004 — typed transport diagnostic evidence | closed | `plans/closure/provider-transport/004-status.md`, implementation `a87790ad` |
| Provider transport M003 — Eggress 1.0.10 adoption and requalification | closed | `plans/closure/provider-transport/003-status.md`, implementation `a87790ad` |
| Provider transport M001 — Eggfetch adapter contract hardening | closed | `plans/closure/provider-transport/001-status.md`, implementation `a87790ad` |
| Plan 250 — EggServe 0.3.0 direct-Tower migration (legacy flat) | closed | `plans/250-eggserve-0.3.0-direct-tower-migration-and-requalification.md`, `7879cbf9` |
| Planning governance M001 | closing → closed on acceptance of `plans/closure/planning-governance/001-status.md` | Plan 251 authorizes; bootstrap files + verification in closure record |
| Server transport M001 — EggServe 0.4.0 adoption and requalification | closed | `plans/closure/server-transport/001-status.md`, implementation `409491ea` |
| Request admission and wire M002 — wire-kernel extraction seam and contract freeze | closed | `plans/closure/request-admission-wire/002-status.md`, implementation `ca3d16b3` |
| Request admission and wire M003 — sans-I/O wire-kernel extraction and EggPool cutover | closed | `plans/closure/request-admission-wire/003-status.md`, implementation `5f373c98` |
| Request admission and wire M004 — fidelity, provenance, and conformance hardening | closed | `plans/closure/request-admission-wire/004-status.md`, implementation `72d6d442` |
| Request admission and wire M005 — planning reconciliation and minor wire cleanup | closed | `plans/closure/request-admission-wire/005-status.md`, implementation `4a1315a1` |

## Unblock audit

Provider-transport M001, M003, and M004 are closed. M004 did not depend on
M002 and did not change its upstream API blocker.
Provider-transport M002 remains blocked on an upstream Eggfetch typed
classification interface and is not promoted to an implementation plan.
Request-admission-wire M001 is closed after consuming the stable
server-transport interface. Explicit user direction reopened that subsystem for
the wire-kernel extraction sequence: M002 is closed (`ca3d16b3` seam +
`wire_extraction_contract`/`wire_kernel_boundary` corpus); M003 is closed
(`5f373c98` crate + cutover); M004 is closed (`72d6d442` fidelity/provenance/
conformance, additive only). Explicit user direction opens M005 as a bounded
corrective/polish pass for planning reconciliation plus the two low-severity
cleanup findings recorded by M004. M005 is closed (`4a1315a1` shared classifier
+ `Approximated` reservation + planning reconciliation, 781 default / 782
no-default tests green, no medium-or-higher finding); the
request-admission-wire subsystem is closed with no ready successor. No
provider-transport blocked work is promoted; Provider
M002 remains blocked on upstream Eggfetch API work.


Explicit user direction opens the persistence performance workstream at the current Rust baseline. Persistence M001 is dependency-ready against the completed Plan 239 diagnostic and Plan 240 design constraints; its physical SBC requirement is operational closure evidence, not a reason to invent a different storage architecture. Provider-transport M002 remains blocked and is unaffected.

Persistence M001 is conditionally closed (`6eae94db` implementation, `plans/closure/persistence/001-status.md`): the bounded passive-checkpoint mechanism, tuning hooks, report plumbing, and host verification are landed. The M004 closure now resolves the physical-evidence condition with a rejection verdict; the M001 mechanism is retained as additive-safe and its performance claim is unfulfilled.

Persistence M002 is closed (`52494140` implementation, `plans/closure/persistence/002-status.md`): gate-tenure and metrics-flush ownership cleanup with row/rebuffer equivalence.

Routing-selection M001 is closed (`4db8000d` implementation, `plans/closure/routing-selection/001-status.md`): ordered quota-scoring and candidate-allocation cleanup with seam parity and twin-router determinism.

Persistence M004 (`8113d264` zero-Rust-diff closure, `plans/closure/persistence/004-status.md`) collected 14 accepted Pi 5 / ext4 / MMC physical artifacts at `artifacts/qualification/m004/` (11 phase-diagnostic runs plus 3 ordinary benchmark runs) against the production M001 mechanism. The 60s/256 candidate maxima were 1709 / 561 / 10 943 ms; bounded matrix candidates (60s/128, 60s/64, 30s/64) and the minimum-cadence 1s/64 stress run all left the foreground tail. Three ordinary `--benchmark-samples 10` runs converged (`pending_requests=0`, `active_reservations=0`, peak RSS ≈ 18.2 MB) with backup / recovery / restart / shutdown / rehash / bounded-maintenance / graceful-shutdown all green. Periodic strategy is rejected on the target class; landed 60s/256 constants untouched per plan §6.2.

Unblock audit (M004 closed): the M004 evidence satisfies M003's hard dependency ("M001 closure evidence must explicitly show the periodic strategy is insufficient"). M005 corrects the M004 record's reading of that evidence: measured-window checkpoint activity is zero in every 30 s/60 s phase run, and the 1s/64 stress run deferred all three in-window checkpoint ticks on the busy foreground gate, so M003 inherits concrete gate-contention evidence rather than in-batch PASSIVE checkpoint evidence. M003 is `proposed`, not `ready` — see the M005 audit below.

Unblock audit (M005 closed): persistence M003 moves from `ready` back to
`proposed`. Its hard evidence dependency is satisfied, but the persistence
roadmap §7 architecture review and its dedicated
`plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`
do not exist, so it is not an implementation-handoff candidate. Local number
003 is reserved for that future plan; the M004 closure's `005-event-driven-...`
suggestion is a superseded numbering statement kept only as history. M003 may
become `ready` only through a separate registration commit. Routing-selection
M002 stays not started — its 64/512/4096-entry affinity workload is still not
produced. Provider-transport M002 remains blocked on the upstream Eggfetch
typed classification interface. No blocked work is promoted and the
dependency-ready implementation-plan table is now empty: M005 closed without
unblocking any future plan, and M003's registration is the only remaining
persistence planning action.


Persistence M005 is **closed** (`plans/closure/persistence/005-status.md`,
guard `tests/tooling/test_persistence_m004_evidence.py`, 17 tests green). It
preserved the immutable M004 closure, machine-checked the committed artifact
deltas — 14 accepted artifacts, zero in-batch checkpoint ticks in every 30 s and
60 s phase run, three in-window ticks all `gate_busy` in the 1s/64 stress run —
corrected the current roadmap/architecture statements that had carried
cumulative counters into the measured window, and reserved local
implementation-plan number 003 for the future event-driven checkpoint
milestone. The M004 closure's own accepted-artifact count and
cumulative-vs-delta narration remain historical text; M005's closure record is
the authoritative correction. Zero production Rust change in the pass.
