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
Most recently closed: Server transport M001 (EggServe 0.4.0 adoption and
requalification, `plans/closure/server-transport/001-status.md`). Legacy
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
| Provider transport | active | `plans/subsystems/provider-transport-roadmap.md` | M003 ready — Eggress 1.0.10 adoption and requalification | M001 closed; M002 remains upstream-blocked; M004 awaits M003 closure |
| Request admission and wire | active | `plans/subsystems/request-admission-wire-roadmap.md` | M001 ready — inference body resource admission hardening | Server transport M001 closed; generation-owned body limit + coordinator Bytes boundary stable. |

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| Provider transport | M003 Eggress 1.0.10 adoption and requalification | ready | `plans/implementation/provider-transport/003-eggress-1.0.10-adoption-and-requalification.md` | No hard blocker; soft coordination with M001 because both touch the provider adapter/test target. Rebase after M001 if it lands first. |
| Request admission and wire | M001 inference body resource admission hardening | ready | `plans/implementation/request-admission-wire/001-inference-body-resource-admission-hardening.md` | No hard blocker; preserve live generation limit, EggServe 1 GiB outer ceiling, and public request/wire contracts. |

## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| Provider transport | M002 stable Eggfetch transport error taxonomy | Upstream Eggfetch does not yet expose/publish a general-purpose typed classification surface sufficient to replace the remaining Hyper/Rustls source-chain inspection; requires separate upstream planning. |
| Provider transport | M004 typed transport diagnostic evidence | Hard dependencies: provider-transport M001 and M003 closure; plan at `plans/implementation/provider-transport/004-typed-transport-diagnostic-evidence.md`. |

## Recently closed

| Subsystem / plan | Disposition | Evidence |
|---|---|---|
| Provider transport M001 — Eggfetch adapter contract hardening | closed | `plans/closure/provider-transport/001-status.md`, implementation `a87790ad` |
| Plan 250 — EggServe 0.3.0 direct-Tower migration (legacy flat) | closed | `plans/250-eggserve-0.3.0-direct-tower-migration-and-requalification.md`, `7879cbf9` |
| Planning governance M001 | closing → closed on acceptance of `plans/closure/planning-governance/001-status.md` | Plan 251 authorizes; bootstrap files + verification in closure record |
| Server transport M001 — EggServe 0.4.0 adoption and requalification | closed | `plans/closure/server-transport/001-status.md`, implementation `409491ea` |

## Unblock audit

Provider-transport M001 is closed. M003 is ready and was implemented on the
same candidate after compiling unchanged 1.0.10 source in an isolated worktree.
Provider-transport M002 remains blocked on an upstream Eggfetch typed
classification interface and is not promoted to an implementation plan.
Provider-transport M004 remains blocked on M003 closure and is already planned
so it can be promoted by the closure unblock audit without rediscovery.
Request-admission-wire M001 is independently ready after the EggServe review
identified an application-side raw-body resource-admission gap; it consumes the
closed server-transport interface and does not reopen that milestone. No blocked
work is promoted by this registration.
