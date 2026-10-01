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
Most recently closed: Deployment and packaging M001 (binary-first quick
installer and ownership cleanup; `plans/closure/deployment-packaging/001-status.md`).
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
| Provider transport | active | `plans/subsystems/provider-transport-roadmap.md` | M005 closed — Eggfetch 0.2.1 / Eggress 1.0.11 refresh; M002 blocked | M002 remains independently blocked on a published upstream typed classification interface. |
| Routing selection | active | `plans/subsystems/routing-selection-roadmap.md` | M001 closed — ordered quota-scoring and candidate-allocation cleanup | M002 stays evidence-gated (no affinity workload measured yet). |
| Persistence | active | `plans/subsystems/persistence-roadmap.md` | M003 closed — event-driven candidate rejected; runtime changes reverted | No eligible successor; any further checkpoint redesign requires a new bounded plan. |
| Deployment and packaging | active | `plans/subsystems/deployment-packaging-roadmap.md` | M002 active — installer transaction and collision corrective | M001 closed; M002 has no hard blocker and corrects two post-closure installer invariants. |

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| Deployment and packaging | M002 installer transaction and collision corrective | active | `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md` | Corrective to closed M001; ADR-0001 already defines ownership/failure invariants; no hard blocker. |

## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| Provider transport | M002 stable Eggfetch transport error taxonomy | Upstream Eggfetch does not yet expose/publish a general-purpose typed classification surface sufficient to replace the remaining Hyper/Rustls source-chain inspection; requires separate upstream planning. |

## Recently closed

| Subsystem / plan | Disposition | Evidence |
|---|---|---|
| Deployment and packaging M001 — binary-first quick installer and ownership cleanup | closed — verified raw-binary fresh path, owner delegation, 42-case deterministic qualification, docs/validators green, zero Rust diff | `plans/closure/deployment-packaging/001-status.md`, implementation `05600504` |
| Provider transport M005 — Eggfetch 0.2.1 / Eggress 1.0.11 refresh | closed — provider, consumer, workspace, dependency, tooling, hosted CI, and audit qualification passed | `plans/closure/provider-transport/005-status.md`, implementation `e08c0e25`, test portability correction `7715a448` |
| Persistence M006 — SQLite NOOP and WAL-reset safety baseline | closed — bundled SQLite 3.53.2; NOOP regression and full default/no-default qualification passed | `plans/closure/persistence/006-status.md`, implementation `c3a72720` |
| Persistence M003 — event-driven checkpoint coordination | closed — rejected by paired Pi/MMC gates; candidate moved multi-second tails into finalization gate wait and was reverted | `plans/closure/persistence/003-status.md`, attempted implementation `b5d145fb`, revert `29bcbb4e` |
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

Post-closure review of deployment-packaging M001 found two concrete invariant
gaps not exercised by its 42-case qualification: fresh `--force` can replace
an unowned regular destination that was never classified as an EggPool owner,
and first-time `init-config` failure after executable commit can return
failure while leaving that executable installed. M001 remains immutable and
closed as historical evidence; corrective M002 is registered `ready` at
baseline `6639d4e6028e7aeebfc46b0c6743b67dc19774c7` to add the missing
regressions, tighten repair attribution, make fresh config seeding rollback
transactional, and reconcile stale roadmap current-state prose. No new ADR or
external dependency is required. Provider-transport M002 remains independently
blocked and is unaffected.


Explicit user direction opens deployment-packaging M001 at baseline `5dc8aeccf06c0f012d6fae81410a805586c9d9d3`. `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` establishes the fresh native curl-install ownership contract; the existing qualified raw release assets/checksum sidecar and standalone provenance/update path satisfy M001's interface dependencies, so `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md` is `ready`. Provider-transport M002 remains independently blocked and is unaffected.

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

Provider transport M005 is closed (`e08c0e25`, test-only error-source
assertion correction `7715a448`; `plans/closure/provider-transport/005-status.md`).
Eggfetch 0.2.1 preserves its API and does not supply the typed classification
interface M002 requires. M002 remains blocked and no future plan became
dependency-ready in this closure pass.


Explicit user direction opens the persistence performance workstream at the current Rust baseline. Persistence M001 is dependency-ready against the completed Plan 239 diagnostic and Plan 240 design constraints; its physical SBC requirement is operational closure evidence, not a reason to invent a different storage architecture. Provider-transport M002 remains blocked and is unaffected.

Persistence M001 is conditionally closed (`6eae94db` implementation, `plans/closure/persistence/001-status.md`): the bounded passive-checkpoint mechanism, tuning hooks, report plumbing, and host verification are landed. The M004 closure now resolves the physical-evidence condition with a rejection verdict; the M001 mechanism is retained as additive-safe and its performance claim is unfulfilled.

Persistence M002 is closed (`52494140` implementation, `plans/closure/persistence/002-status.md`): gate-tenure and metrics-flush ownership cleanup with row/rebuffer equivalence.

Routing-selection M001 is closed (`4db8000d` implementation, `plans/closure/routing-selection/001-status.md`): ordered quota-scoring and candidate-allocation cleanup with seam parity and twin-router determinism.

Persistence M004 (`8113d264` zero-Rust-diff closure, `plans/closure/persistence/004-status.md`) collected 14 accepted Pi 5 / ext4 / MMC physical artifacts at `artifacts/qualification/m004/` (11 phase-diagnostic runs plus 3 ordinary benchmark runs) against the production M001 mechanism. The 60s/256 candidate maxima were 1709 / 561 / 10 943 ms; bounded matrix candidates (60s/128, 60s/64, 30s/64) and the minimum-cadence 1s/64 stress run all left the foreground tail. Three ordinary `--benchmark-samples 10` runs converged (`pending_requests=0`, `active_reservations=0`, peak RSS ≈ 18.2 MB) with backup / recovery / restart / shutdown / rehash / bounded-maintenance / graceful-shutdown all green. Periodic strategy is rejected on the target class; landed 60s/256 constants untouched per plan §6.2.

Unblock audit (M004 closed): the M004 evidence satisfies M003's hard evidence dependency ("M001 closure evidence must explicitly show the periodic strategy is insufficient"). M005 corrects the M004 record's reading of that evidence: measured-window checkpoint activity is zero in every 30 s/60 s phase run, and the 1s/64 stress run deferred all three in-window checkpoint ticks on the busy foreground gate, so M003 inherits concrete gate-contention evidence rather than in-batch PASSIVE checkpoint evidence. At M005 closure this returned M003 to `proposed`; the current M006/M003 planning state is recorded below.

Unblock audit (M005 closed, historical disposition): persistence M003 moved
from `ready` back to `proposed` because its architecture review and dedicated
`003` implementation plan did not yet exist. That M005 condition has now been
addressed by the registered M003 plan, but new M006 research exposed a prior
hard dependency: the locked SQLite 3.50.2 baseline predates true
`wal_checkpoint(NOOP)` and the WAL-reset fix. M003 is therefore currently
`blocked` on M006 rather than `proposed` or `ready`. Routing-selection M002
stays evidence-gated and Provider-transport M002 remains blocked on the
upstream Eggfetch typed classification interface.


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


Persistence M006 is registered as the dependency-ready engine-safety prerequisite
for M003. Repository and upstream research found that the current
tokio-rusqlite 0.7 / rusqlite 0.37 / libsqlite3-sys 0.35 graph bundles SQLite
3.50.2, which predates the 3.51.0 NOOP checkpoint mode and the 3.51.3 WAL-reset
fix. M006 changes no checkpoint scheduling policy; it must establish a
true-observational NOOP and fixed bundled engine before event-driven checkpoint
coordination is handed off.


Persistence M003 was unblocked after M006 closed and received paired physical
qualification on a Raspberry Pi 5 / ext4 / MMC target. The event-assisted
candidate reduced foreground publication COMMIT maxima to 349–376 µs but
introduced 1.88–3.30 s finalization gate waits in all three runs; this fails
M003's explicit no-tail-transfer gate. Its runtime implementation was reverted
(`29bcbb4e`), and M003 is closed with a rejected outcome in
`plans/closure/persistence/003-status.md`. No successor is eligible from this
closure. Any future checkpoint redesign must be separately planned and must
address storage stalls without transferring them to foreground gate wait.

Explicit user direction opens Provider transport M005 as a dependency-ready
patch refresh at baseline `869964c236cfd985a771beeb0ee815df3e1cf601`. It upgrades
`eggfetch-core 0.2.0 -> 0.2.1`, resolves `eggfetch-http-connect` to 0.2.1,
and upgrades the live Eggress family `1.0.10 -> 1.0.11` under the existing
listener-free outbound/typed-error/default-no-default contracts. Upstream
Eggfetch 0.2.1 is documented as runtime/API-equivalent to 0.2.0; Eggress
1.0.11 contains substantive parser/timeout/redaction/SSH/protocol fixes and
therefore requires full provider-transport requalification. EggServe needs no
parallel plan: `eggserve-server 0.4.0` is already the latest published line.
Provider M002 remains blocked and is not promoted by M005.

Unblock audit (deployment-packaging M001 closed, implementation `05600504`,
`plans/closure/deployment-packaging/001-status.md`): M001 had no hard
dependencies and consumed only already-stable raw/checksum and provenance/update
interfaces. Closure promotes no blocked work. Provider-transport M002 remains
independently blocked on the upstream Eggfetch typed classification interface.
Routing-selection M002 stays evidence-gated. No deployment-packaging successor
is registered; future hardening (attestations, system/root distribution,
additional targets) requires new bounded plans.
