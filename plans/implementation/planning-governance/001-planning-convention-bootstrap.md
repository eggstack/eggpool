# Planning Governance Milestone 001 — Planning-convention bootstrap

Status: implemented (closure accepted in
`plans/closure/planning-governance/001-status.md`)

Repository baseline: `7879cbf9` (main, post-Plan 250)

Source roadmap:

- `plans/subsystems/planning-governance-roadmap.md` Milestone 1

Long-term requirements:

- `plans/000-long-term-specification.md` §2 (invariants documented)
- `plans/001-terminology-and-domain-model.md` (status/classification terms)
- `plans/002-long-term-roadmap.md` Phase 0

Applicable ADRs: None required (no runtime/ownership/protocol decision).

Primary class: infrastructure

## 1. Objective

Land the CodeGG-style planning hierarchy skeleton from Plan 251 (15 files)
with EggPool-grounded content and zero runtime effect.

## 2. Why this milestone is ready

No dependencies. Process-only milestone; authorizing Plan 251
(`plans/251-planning-convention-adoption-codegg-hierarchy.md`,
`implementation handoff`) is the handoff authority.

## 3. Current implementation evidence

Baseline: flat `plans/` (~254 files, `001-*`…`250-*` + one unnumbered),
39-line `.opencode/skills/plan/SKILL.md`, AGENTS.md Skills entry. No
`plans/README.md`, `000–003`, `registry.md`, `adrs/`, `subsystems/`
(roadmap), `implementation/`, `closure/`, `archive/` README layer.

## 4. Invariants that must not regress

- `rust/src/config_reload_policy.rs::classify_transition` remains the only
  reload-vs-restart authority (untouched).
- `rust/src/error.rs` remains the HTTP/status mapping owner (untouched).
- `rust/src/server/*` stays thin (untouched).
- Secret-free diagnostics (no credentials/prompts/bodies/keys in new docs).

## 5. Scope

### In scope

- Create: `plans/README.md`, `000`, `001`, `002`, `003`, `registry.md`,
  `adrs/README.md`, `subsystems/README.md`, `implementation/README.md`,
  `closure/README.md`, `archive/README.md`,
  `subsystems/planning-governance-roadmap.md`, this plan,
  `closure/planning-governance/001-status.md`.
- Expand `.opencode/skills/plan/SKILL.md` to the new lifecycle (same skill
  name per AGENTS.md).
- Minimal AGENTS.md pointer update if required for truthfulness.

### Explicitly out of scope

- Editing, moving, or renumbering any flat legacy plan.
- Any `rust/`, `scripts/`, CI, dependency, config, or deployment change.
- Bulk creation of future subsystem roadmaps.
- Importing CodeGG product content as EggPool direction.

## 6. Required production changes

None (no production code). Documentation/governance files only, as listed.

## 7. Ordered work packages

### Work package A — Hierarchy + canonical docs + registry

Intent: establish the durable layer.

Required changes: `plans/README.md`, `000`, `001`, `002`, `003`,
`registry.md`, five `README.md` templates, `subsystems/planning-governance-roadmap.md`.

Acceptance evidence: files exist; content cites EggPool authority paths
(`rust/src/...`, `architecture/...`); no CodeGG domain content; legacy
files unmodified per `git status --short`.

### Work package B — Skill + pointer

Intent: make the new convention discoverable where agents actually look.

Required changes: expanded `.opencode/skills/plan/SKILL.md`; AGENTS.md only
if its current wording becomes false.

Acceptance evidence: skill describes hierarchy, status vocabulary,
classification, dependency types, closure gate, corrective-pass rule;
`git diff --check` clean.

### Work package C — Closure record + registry consistency

Intent: prove completion under the new rules themselves.

Required changes: `plans/closure/planning-governance/001-status.md` with
requirement→evidence matrix; registry + roadmap status lines updated.

Acceptance evidence: closure disposition `closed`; registry shows M001
closed with evidence link; no blocked-work invention.

## 8. Failure, cancellation, restart, contention semantics

N/A (no runtime, no concurrency, no persistence). Partial landing is
tolerated only as untracked new files completed in the same pass; nothing
is published half-registered: registry updates land with the files.

## 9. Compatibility and migration

New-hierarchy vocabulary applies to new docs only. Legacy flat headers keep
their original status text. Consumers reading old plans see no change.

## 10. Required tests

No Rust/Python suite required (no code change). Verification is documentary:
file audit + git checks + status-consistency review (see §11).

## 11. Required verification commands

```bash
ls plans/ | tail -n 5
git status --short
git diff --check
```

Plus manual consistency check: `plans/registry.md` rows match
roadmap/plan/closure status lines.

## 12. Documentation updates

This milestone IS the documentation update. `architecture/` untouched (no
runtime change to document).

## 13. Acceptance criteria

- All 15 files exist with EggPool-grounded, secret-free content.
- `git status --short` shows only new `plans/` files + skill edit (+ Plan
  251 file + minimal AGENTS.md edit if made).
- `git diff --check` clean.
- Registry + roadmap + closure statuses mutually consistent.

## 14. Stop conditions

Stop and report rather than improvise when: any step needs a legacy-plan
edit; runtime requirements would have to be invented; CodeGG domain text
would have to be copied as EggPool direction; subsystem decomposition
dispute arises (land templates + registry, defer roadmaps as `proposed`).

## 15. Closure evidence required

File list, `git status --short` output, `git diff --check` result,
registry/roadmap/closure consistency statement, follow-ups registered as
`proposed` (or explicit “none”).

## 16. Handoff notes

Serial-test requirement n/a. No environment beyond git + shell. Preserve
unrelated user changes (none expected; working tree was clean apart from
this bootstrap).
