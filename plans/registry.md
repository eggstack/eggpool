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
Most recently closed: Request admission and wire M001 (inference body resource
admission hardening, `plans/closure/request-admission-wire/001-status.md`). Legacy
archive latest: Plan 250 (EggServe 0.3.0 direct-Tower migration,
`7879cbf9`). Plans 244–245, 215–220, 241 remain historical per their own
closure passes; the `146-*` duplicate pair is a known numbering accident.

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
| Request admission and wire | active | `plans/subsystems/request-admission-wire-roadmap.md` | M003 ready — sans-I/O wire-kernel extraction and EggPool cutover | M002 closed; no external hard dependency. |

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| Request admission and wire | M003 sans-I/O wire-kernel extraction and EggPool cutover | ready | `plans/implementation/request-admission-wire/003-sans-io-wire-kernel-extraction-and-eggpool-cutover.md` | Move the M002-qualified kernel into one internal crate and cut over. |

## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| Provider transport | M002 stable Eggfetch transport error taxonomy | Upstream Eggfetch does not yet expose/publish a general-purpose typed classification surface sufficient to replace the remaining Hyper/Rustls source-chain inspection; requires separate upstream planning. |
| Request admission and wire | M004 fidelity, provenance, and conformance hardening | Hard dependency: M003 closure. |

## Recently closed

| Subsystem / plan | Disposition | Evidence |
|---|---|---|
| Request admission and wire M001 — inference body resource admission hardening | closed | `plans/closure/request-admission-wire/001-status.md`, implementation `a87790ad` |
| Provider transport M004 — typed transport diagnostic evidence | closed | `plans/closure/provider-transport/004-status.md`, implementation `a87790ad` |
| Provider transport M003 — Eggress 1.0.10 adoption and requalification | closed | `plans/closure/provider-transport/003-status.md`, implementation `a87790ad` |
| Provider transport M001 — Eggfetch adapter contract hardening | closed | `plans/closure/provider-transport/001-status.md`, implementation `a87790ad` |
| Plan 250 — EggServe 0.3.0 direct-Tower migration (legacy flat) | closed | `plans/250-eggserve-0.3.0-direct-tower-migration-and-requalification.md`, `7879cbf9` |
| Planning governance M001 | closing → closed on acceptance of `plans/closure/planning-governance/001-status.md` | Plan 251 authorizes; bootstrap files + verification in closure record |
| Server transport M001 — EggServe 0.4.0 adoption and requalification | closed | `plans/closure/server-transport/001-status.md`, implementation `409491ea` |
| Request admission and wire M002 — wire-kernel extraction seam and contract freeze | closed | `plans/closure/request-admission-wire/002-status.md`, implementation `ca3d16b3` |

## Unblock audit

Provider-transport M001, M003, and M004 are closed. M004 did not depend on
M002 and did not change its upstream API blocker.
Provider-transport M002 remains blocked on an upstream Eggfetch typed
classification interface and is not promoted to an implementation plan.
Request-admission-wire M001 is closed after consuming the stable
server-transport interface. Explicit user direction reopened that subsystem for
the wire-kernel extraction sequence: M002 is closed (`ca3d16b3` seam +
`wire_extraction_contract`/`wire_kernel_boundary` corpus); M003 is promoted
to dependency-ready; M004 remains blocked on M003 closure. No provider-
transport blocked work is promoted; Provider M002 remains blocked on upstream
Eggfetch API work.
