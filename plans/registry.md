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
Most recently closed: Dashboard M012 (lifecycle closure and documentation
polish; `plans/closure/dashboard/012-status.md`).
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
| Provider transport | closed | `plans/subsystems/provider-transport-roadmap.md` | M002 closed — Eggfetch 0.2.2 transport failure classification adoption | M001–M005 closed; no successor registered. |
| Provider profile metadata corrective | active | `plans/subsystems/provider-profile-metadata-corrective-roadmap.md` | M001 closed — provider template endpoint/source reconciliation | No successor registered; future re-reviews (including two low deferred discovery-probing items) require new bounded plans. |
| Provider profile metadata planning/documentation reconciliation | active | `plans/subsystems/provider-profile-metadata-planning-reconciliation-corrective-roadmap.md` | C001 ready — closed-roadmap/source-truth reconciliation | Docs-only correction: predecessor M001 is already closed; reconcile stale active lifecycle and refuted Together `.xyz` premise. No template/runtime change. |
| Routing selection | active | `plans/subsystems/routing-selection-roadmap.md` | M001 closed — ordered quota-scoring and candidate-allocation cleanup | M002 stays evidence-gated (no affinity workload measured yet). |
| Runtime efficiency | active | `plans/subsystems/runtime-efficiency-roadmap.md` | M001/M002/M003 ready — independent residual optimization/qualification passes | No hard blockers; may run in parallel. Persistence M007 remains a separate operationally blocked higher-priority storage-tail line. |
| Persistence | active | `plans/subsystems/persistence-roadmap.md` | M007 blocked — dedicated checkpointer qualification experiment | Implementation/local gates passed; paired physical Pi/MMC performance disposition is unavailable from this host. |
| Deployment and packaging | active | `plans/subsystems/deployment-packaging-roadmap.md` | M003 closed — config publication ownership corrective | M001/M002/M003 closed; no ready successor; future hardening requires new bounded plans. |
| Dashboard | closed | `plans/subsystems/dashboard-roadmap.md` | M012 closed — lifecycle closure and documentation polish | M001–M012 closed; no registered successor; future dashboard work requires a new bounded plan. |

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| Provider profile metadata planning/documentation reconciliation | C001 closed-roadmap + source-truth reconciliation | ready | `plans/implementation/provider-profile-metadata-planning-reconciliation/001-closed-roadmap-and-source-truth-reconciliation.md` | No hard dependency. Markdown/planning only; M001 closure and provider templates remain immutable. |
| Runtime efficiency | M001 provider/wire-resolver hot-path ownership and cache cleanup | ready | `plans/implementation/runtime-efficiency/001-provider-wire-resolver-hotpath-cleanup.md` | No hard dependency. Preserve exact resolver fingerprint/ordering and public coordinator/wire behavior; no runtime/persistence topology change. |
| Runtime efficiency | M002 catalog refresh projection and lock-tenure cleanup | ready | `plans/implementation/runtime-efficiency/002-catalog-refresh-projection-lock-tenure-cleanup.md` | No hard dependency. Projection/ownership cleanup only; schema 54, refresh semantics, failure retention, and routing-visible cache behavior stay unchanged. |
| Runtime efficiency | M003 dashboard TTFT percentile query qualification | ready | `plans/implementation/runtime-efficiency/003-dashboard-ttft-percentile-query-qualification.md` | No hard dependency. Measurement first; query-only rewrite conditional on evidence. No migration, second connection, or persistence-topology change. |

## Active implementation plans

| Subsystem | Milestone | Status | Implementation plan | Handoff note |
|---|---|---|---|---|

## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| Persistence | M007 dedicated checkpointer qualification experiment | Physical Linux/aarch64 Raspberry Pi-class MMC target required for three paired 60-request control/candidate runs and the candidate 300-request convergence corpus; implementation and local qualification are complete. |

Historical M007 blocker assessment at baseline `7e241ad` (`plans/closure/persistence/007-status.md`) predates the scope revision that allowed implementation and local qualification. The current disposition is in `plans/closure/persistence/007-implementation-status.md`: implementation passed locally, while paired Pi/MMC performance evidence remains an operational blocker.

## Recently closed

| Subsystem / plan | Disposition | Evidence |
|---|---|---|
| Dashboard M012 — lifecycle closure and documentation polish | closed — terminal planning/docs reconciliation; M001–M012 closed, no registered successor; three stale dashboard ownership paths corrected; zero Rust/Cargo/asset/oracle diff; merge-head CI and dependency audit green | `plans/closure/dashboard/012-status.md`, documentation `bdd22aef`, hosted CI `37090882257`, dependency audit `37090882237` |
| Provider Transport M002 — Eggfetch 0.2.2 transport failure classification adoption | closed — typed upstream classifications adopted without changing EggPool policy; default/test-support/no-default provider fixtures and hosted gates pass | `plans/closure/provider-transport/002-status.md`, implementation `67d6ceb3`, hosted CI `37070421301`, dependency audit `37070423689` |
| Provider profile metadata corrective M001 — provider template endpoint/source reconciliation | closed — all 23 bundled templates dispositioned against first-party docs; Together retained on canonical `.ai` (plan's `.xyz` correction refuted), OpenCode Go retained on `/zen/go/v1`; 5 template regression tests + authority docs; zero template diff; 815 workspace tests green | `plans/closure/provider-profile-metadata/001-status.md`, implementation `805d6f70` |
| Request admission and wire M006 — external semantic-producer consumer contract | closed — source-neutral canonical requests, surface encoding/options, bounded tool-call accumulation, isolated consumer compile, and EggPool regression gates pass; downstream pin available | `plans/closure/request-admission-wire/006-status.md`, implementation `f05b18b7358d9a4125d1e20c491151eec265e403` |
| Deployment and packaging M003 — config publication ownership corrective | closed — transaction-owned staging + no-clobber publish, concurrent config preserved, final never deleted, 60-case qualification green, zero Rust diff | `plans/closure/deployment-packaging/003-status.md`, implementation `292a1e3f` |
| Dashboard M006 — Full parity qualification and closure | closed — strict report retains nine explicitly accepted source-backed differences; 144 matched captures, eight interaction runs, lifecycle shutdown/restart, and full local Rust/tooling gates passed | `plans/closure/dashboard/006-status.md`, browser manifest `plans/closure/dashboard/006-browser-manifest.json`; implementation `6bfa1781`, `5bb3aba1` |
| Dashboard corrective pass 007 — deterministic recovery-summary qualification | closed | `plans/closure/dashboard/007-status.md`; two consecutive strict empty/populated Reliability runs pass with the startup event inside the summary window. |
| Dashboard M011 — hosted oracle history qualification | closed — checkout now includes the pinned oracle source commit; all hosted CI gates pass | `plans/closure/dashboard/011-status.md`, run `37049147155`, implementation `30b8282a` |
| Dashboard M008 — post-merge strict-CI and planning reconciliation | closed — strict Clippy, no-default, serial Rust, tooling, strict oracle baseline, and hosted CI all pass; M009 ready | `plans/closure/dashboard/008-status.md`, run `37049147155`, implementation `3c40e34f` |
| Dashboard M009 — production module decomposition and ownership cleanup | closed — strict parity, full local/hosted gates pass; M010 is ready | `plans/closure/dashboard/009-status.md`, implementation `caa0b042`, hosted run `37053752173` |
| Dashboard M010 — parity qualification harness decomposition | closed — stable command facade, unchanged oracle/report semantics, browser/lifecycle and full local/hosted gates pass; no successor unblocked | `plans/closure/dashboard/010-status.md`, implementation `06c950f`, hosted run `37057371175` |
| Dashboard M004 — telemetry, routing, reliability, and trace parity | closed | `plans/closure/dashboard/004-status.md` plus additive resolution `plans/closure/dashboard/004-follow-up-007.md`; all M004 routes pass strict qualification. |
| Dashboard M005 — Runtime and Cache Observability Parity | closed | `plans/closure/dashboard/005-status.md` plus additive resolution `plans/closure/dashboard/005-follow-up-006.md`; M006 accepted the four source-truth dispositions, 815 serial Rust tests, 152 tooling tests (one skipped), and 144 matched browser captures. |
| Dashboard M003 — Overview, Accounts, Models, and Model Detail parity | closed — restored bounded current-owner projections; exact populated Model Detail oracle comparison; 32 paired captures; four accepted source-truth differences | `plans/closure/dashboard/003-status.md`, implementation/qualification `03b4988`, `8261e5a` |
| Dashboard M002 — shared shell, interaction, and dashboard API restoration | closed — exact shared-shell projection passed all 28 route/state cells; timeseries APIs matched empty/populated/private cases; eight paired desktop/mobile interaction runs and clean browser checks; 804 serial Rust tests passed | `plans/closure/dashboard/002-status.md`, implementation `da8183d`, paired viewport gate `00c69f5` |
| Dashboard M001 — Python oracle freeze and strict parity substrate | closed — fixed 14-page/8-API oracle, 50 static/theme hashes, reproducible sanitized captures, strict negative tests, 50-cell current-gap report; no production diff | `plans/closure/dashboard/001-status.md`, implementation `3f3d5de`, theme inventory `286b70a` |
| Deployment and packaging M002 — installer transaction and collision corrective | closed — fresh `--force` refuses unowned files, config-seed failure rolls back executable, 46-case qualification green, zero Rust diff | `plans/closure/deployment-packaging/002-status.md`, implementation `02ee2873` |
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

Provider-transport M002 is unblocked by the coordinated Eggfetch 0.2.2
release published on 2026-10-02. Upstream now exposes the general native
`TransportFailureKind` / `Error::transport_failure_kind()` seam required by
the roadmap, while admission/timeouts/custom-dialer facts remain on their
existing typed APIs. M002 is registered `ready` at baseline `8db2d16c73cb7bb832c23a2e037d5fe0823021f7`;
Eggress stays 1.0.11 and no separate upstream work remains before downstream
implementation.

Provider-transport M002 is now closed. Hosted CI `37070421301` and hosted
dependency audit `37070423689` passed at implementation head
`6f0cfd532e753a43c454b370453850c55679aab5`. Its completion unblocks no
registered plan; the ready provider-profile metadata C001 plan is independent.
The Provider Transport roadmap is closed because all registered milestones
M001–M005 are closed and no successor is ready.

Historical unblock-audit snapshot: Dashboard M008 was registered `ready` at
baseline `299a0b3657667af509742a184e658c14df22d406` after hosted run
`37040025250` reported two strict-Clippy findings. This snapshot is superseded
by M008's closure record and the current tables above. M008 and M011 are now
closed, M009 is closed and M010 is ready, Deployment/Packaging M003 remains closed,
Provider-transport M002 is ready, and Routing-selection M002 remains
evidence-gated.

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

Unblock audit (deployment-packaging M002 closed, implementation `02ee2873`,
`plans/closure/deployment-packaging/002-status.md`): M002 is a corrective to
closed M001 with no hard dependencies; it consumed only the stable ADR-0001
ownership/failure invariants and the M001 binary-first baseline. Closure
promotes no blocked work and unblocks no future plan. Provider-transport M002
remains independently blocked on the upstream Eggfetch typed classification
interface. Routing-selection M002 stays evidence-gated (no affinity workload
measured). Persistence has no eligible successor. No deployment-packaging
successor is registered; future hardening requires new bounded plans.

Unblock audit (deployment-packaging M003 closed, implementation `292a1e3f`,
`plans/closure/deployment-packaging/003-status.md`): M003 is a corrective to
closed M002 with no hard dependencies; it consumed only the stable ADR-0001
ownership/failure invariants and the M002 binary-first + guarded-rollback
baseline. Closure promotes no blocked work and unblocks no future plan.
Provider-transport M002 remains independently blocked on the upstream Eggfetch
typed classification interface. Routing-selection M002 stays evidence-gated (no
affinity workload measured). Persistence has no eligible successor.
Dashboard M001-M006 are now closed with no successor registered.
No deployment-packaging successor is registered; future hardening
(attestations, system/root distribution, additional targets) requires new
bounded plans.


Explicit user direction reopened request-admission-wire after M005 closure for M006 at baseline `8067ad3d1eef5a40ae6e300923d8be3b75437d26`; M006 is now closed at `f05b18b7358d9a4125d1e20c491151eec265e403`. The work remains bounded to the extracted `eggpool-wire` API and does not reopen provider transport, routing, accounts, persistence, or public HTTP behavior. `eggpool-wire 0.1.0` is already on crates.io; this milestone neither released a new version nor changed versioning policy. The downstream CodeGG adoption dependency is satisfied and may proceed using the immutable pin. No future in-repository plan was registered as blocked on M006, so no other blocked row was promoted; provider transport retains its independent upstream blocker and dashboard is closed.


Explicit user direction opens the provider-profile metadata corrective at baseline `c17a55218b2810791fcf6f3136b8805becfa27c1`. Current first-party documentation reviewed on 2026-10-02 confirms that EggPool's Together template is stale (`api.together.ai` vs current `api.together.xyz`) while its OpenCode Go `/zen/go/v1` prefix is correct. The corrective therefore treats first-party provider documentation—not CodeGG or EggPool sibling state—as authority. M001 is dependency-ready and does not reopen provider transport, routing, request-admission/wire, persistence, or publication work.

Unblock audit (provider-profile-metadata M001 closed, implementation
`805d6f70`, `plans/closure/provider-profile-metadata/001-status.md`):
execution-time review of canonical `docs.together.ai` refuted the Together
drift premise — the bundled `api.together.ai/v1` is the current first-party
value and the `.xyz` host is a legacy alias — so no template correction was
applied and the regression surface locks the confirmed compositions
instead. The roadmap's completion definition is met with no unresolved
medium-or-higher finding and no registered successor. No blocked work in any
subsystem depended on M001 (provider-transport M002 stays upstream-blocked,
 dashboard is closed with no successor, routing-selection M002 stays
evidence-gated), so this closure promotes nothing and unblocks no future
plan; the two low deferred discovery-probing items (Fireworks/Alibaba
models paths) require new bounded plans with live verification if pursued.
oracle is commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`. M001-M006 and
corrective pass 007 are closed; the complete sanitized oracle, strict
comparator, matched browser evidence, and closure/unblock audit are recorded in
the dashboard closure records. M006 retains nine explicitly accepted
source-backed differences and the strict report remains `gaps`, with zero M004
differences. The 144-capture matched browser manifest, eight interaction runs,
bounded shutdown/restart, and full local Rust/tooling verification are recorded
in `plans/closure/dashboard/006-status.md`. No dashboard work is blocked on
M006, no successor remains registered, and no plan is newly eligible from this
closure. Provider Transport M002 and Routing Selection M002 retain their
unrelated independent blockers/evidence gates.


Explicit user direction opens a documentation-only provider-profile metadata reconciliation at baseline `43c987ea458bd563d5108fd8051ad31185704bb0`. The accepted technical outcome remains M001 closure `805d6f70`: canonical Together `api.together.ai/v1` retained, OpenCode Go `/zen/go/v1` retained, zero template diff, two low discovery uncertainties deferred. C001 exists only to reconcile the predecessor roadmap's stale top-level `active` state, the registry's active/no-successor contradiction, and planning-time Together `.xyz` wording that was superseded by execution-time first-party evidence. No provider/template/runtime work is reopened.

Unblock audit (dashboard M012 closed, documentation `bdd22aef`, `plans/closure/dashboard/012-status.md`): M012 was a terminal planning/documentation pass with no hard dependency beyond the already-closed M001–M011. It promoted nothing and unblocks nothing. Dashboard M001–M012 are closed, the dashboard roadmap is closed, and no dashboard plan remains in the dependency-ready, active, or blocked tables. Persistence M007 stays blocked for the same independent reason: paired physical aarch64 Pi-class MMC control/candidate evidence is an operational requirement no planning change can satisfy. Routing-selection M002 stays evidence-gated because no affinity workload has been measured. Provider Transport M002 stays closed and its upstream blocker is historical. The only remaining ready row is the independent provider-profile-metadata planning/documentation reconciliation C001, which M012 does not affect. M012's closure record carries three out-of-scope documentation findings, none of them a dashboard defect: one medium (`docs/thinking.md` still describes Python-era thinking counters, `GET /api/stats/thinking`, and coordinator/health Python members that have no Rust owner) and two low (`docs/rust-dashboard-qualification.md` does not mention the frozen Python oracle runner; `tests/tooling/test_release_docs.py` invokes `uv run` without `--frozen` and so rewrites the committed `uv.lock` `requires-python`). Each requires a new bounded plan outside this subsystem.
