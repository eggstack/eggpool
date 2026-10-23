# Planning Governance Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md` §2 (invariants are documented, not improvised)
- `plans/001-terminology-and-domain-model.md` (status/classification language)
- `plans/002-long-term-roadmap.md` Phase 0
- `plans/003-planning-process.md` (all sections; this roadmap is its first consumer)

Related ADRs: None required. No runtime ownership, protocol, storage, or
auth boundary changes.

Authorizing plan: `plans/251-planning-convention-adoption-codegg-hierarchy.md`

## 1. Purpose and ownership boundary

Owns the planning convention itself: hierarchy layout, templates, registry
discipline, and skill wording. Consumes `architecture/overview.md` direction
as grounding. MUST NOT own runtime behavior, dependencies, CI, or deployment
policy.

## 2. Work classification

### Invariants

- Flat legacy plans (`001-*`…`250-*`) remain untouched and immutable.
- Canonical `000–003` change only by explicit architecture decision or user direction.

### Capabilities

- An agent can find executable work from `plans/registry.md` alone.
- A new milestone can be handed off from `implementation/README.md` without
  copying requirements across files.

### Infrastructure

- Hierarchy directories + README templates + registry skeleton.
- Expanded `plan` skill describing the new lifecycle.

### Polish

- Wording alignment between `plans/README.md`, `003`, templates, and the skill.

## 3. Non-goals

- No runtime/config/dependency change of any kind.
- No bulk generation of future subsystem roadmaps (created on demand).
- No CodeGG product content imported as EggPool direction.
- No AGENTS.md restructuring beyond the pointer the skill update requires
  (kept minimal; follow-up polish may propose more).

## 4. Current state

Baseline `7879cbf9`: flat `plans/` (~254 files), 39-line `plan` skill,
AGENTS.md “Skills” entry pointing at the `plan` skill. No registry, no ADRs,
no subsystem/implementation/closure split, no work classification. Plan 251
authorizes the bootstrap; this roadmap is its first hierarchy consumer.


The roadmap reopened on repository evidence gathered at baseline `e6822d8f78`.
The architecture review index states a CLI command census and a migration chain
census that no longer match the code, omits the entire managed-user runtime
subsystem from the index, the subsystem ownership table, and the agent
workflow index, and attributes wire kernel implementation ownership to root
facade files that only re-export the kernel crate. No guard relates any of
these documented claims to the code they describe. The historical M001 record
remains closed and immutable.

## 5. Target architecture

CodeGG-style hierarchy (Plan 251 delta table) layered over the immutable
flat history: canonical `000–003`, `adrs/`, `subsystems/`,
`implementation/<subsystem>/`, `closure/<subsystem>/`, `archive/` (post-251
only), `registry.md`, `README.md`, expanded skill. Post-251 milestones use
subsystem-local `NNN` + status vocabulary
(`proposed/ready/active/blocked/closing/closed/conditionally closed/superseded/archived`).

## 6. Dependency graph

```text
M001 bootstrap (no dependencies; process-only)
  +--> future subsystem roadmaps (soft; each registers when ready to be reasoned about)
  +--> M002 review index / census reconciliation (no dependencies; closed)
```

No hard/interface dependencies. No operational evidence gate. M002 closed with
its census guards; no further milestone is registered in this subsystem.

## 7. Milestones

### Milestone 1 — Planning-convention bootstrap

Class: infrastructure

Objective: land the hierarchy skeleton + canonical docs + registry +
templates + skill update + this roadmap, verified.

Dependencies: none.

Deliverable boundary: the 15 files listed in Plan 251 § Bootstrap file
list, with EggPool-grounded content.

User or operator value: contributors/agents find active work and handoff
rules without reading 254 flat files.

Exit conditions: acceptance criteria in
`plans/implementation/planning-governance/001-planning-convention-bootstrap.md`
§13 met; closure record
`plans/closure/planning-governance/001-status.md` accepted; registry shows
M001 closed.

Deferred work: future subsystem roadmaps; AGENTS.md deep pointer cleanup
(optional polish, separate plan if wanted).

### Milestone 2 — Review index, subsystem ownership, and census reconciliation

Class: polish

Objective: make the architecture review index and the agent workflow index
describe the shipped runtime accurately, and add the guards that stop the
documented censuses from drifting away from the code again.

Dependencies: none. Every correction is a documentation and guard change with
no production runtime component.

Deliverable boundary: the CLI command census and the migration chain census
are corrected and guarded; the managed-user subsystem gains a review index
entry, an ownership row, and a deep dive; the agent workflow index names every
top-level runtime module; the wire ownership paragraph credits the kernel
crate rather than the root facades; the route topology covers every
registered route or states its scope. The canonical long-term documents are
not touched.

Exit conditions: each census guard fails on a deliberately perturbed input;
the managed-user subsystem is reachable from the review index alone; no
document attributes wire kernel implementation ownership to a root facade.

Deferred work: the durable health documentation corrections, which belong to
the routing selection subsystem that owns those claims.

## 8. Cross-cutting requirements

Storage/migration: none (no runtime state). Protocol/compat: plan-header
vocabulary changes apply to new hierarchy docs only; legacy headers
untouched. Security: secret-free (no credentials/prompts/bodies/keys in any
new doc). Concurrency: n/a. Observability: registry is the observable
surface. Docs/ops: skill + READMEs are the docs.

## 9. Verification strategy

File-existence audit + `git status --short` (new files + skill edit only) +
`git diff --check` clean + registry/roadmap/closure status consistency
check. No Rust/Python suite required (no code change); the closure record
states this justification explicitly rather than claiming test runs.

## 10. Risks and decision points

- Risk: contributors keep writing flat `NNN-*.md` out of habit → mitigated
  by skill rewrite + registry + README core rule.
- Risk: over-generation of empty roadmaps → mitigated by on-demand rule.
- No ADR needed (no durable runtime decision).

## 11. Completion definition

M001 closed with accepted closure record; registry truthful; skill
describes the hierarchy; no legacy file modified; and the architecture review
index and the agent workflow index describe the shipped runtime accurately,
with guards that keep their documented censuses and module map aligned with
the code.


## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 1 bootstrap | closed | `plans/implementation/planning-governance/001-planning-convention-bootstrap.md` | `plans/closure/planning-governance/001-status.md` | — |
| 2 review index, ownership, and census reconciliation | closed | `plans/implementation/planning-governance/002-architecture-index-ownership-and-census-reconciliation.md` | `plans/closure/planning-governance/002-status.md` | none |
