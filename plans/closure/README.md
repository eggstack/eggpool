# Closure and Verification Records

Evidence-based completion records. A closure record is the gate determining
whether a milestone is complete — not a retrospective summary alone.

## Layout and naming

```text
closure/<subsystem>/NNN-status.md
```

Same milestone number as the source implementation plan.

## Required closure-record template

```markdown
# <Subsystem> Milestone NNN — Closure Status

Status: closed | conditionally closed | corrective pass required | blocked

Source implementation plan:

- `plans/implementation/<subsystem>/NNN-...md`

Source subsystem roadmap:

- `plans/subsystems/<subsystem>-roadmap.md#...`

Repository baseline reviewed: `<SHA>`

Implementation commits or pull requests:

- `<SHA>` — summary

## 1. Executive finding

Whether the actual capability/infrastructure boundary is complete and why.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| ... | test, code, migration, docs, runtime output | pass/fail/partial/not run | ... |

## 3. Production implementation evidence

Landed ownership/storage/protocol/runtime/operator changes. Distinguish implemented from planned-but-absent behavior.

## 4. Verification executed

### Commands run

```bash
# exact commands
```

### Results

Pass/fail/timeout/env-block/skipped scope with counts. No concealed partial execution.

## 5. Invariant review

Per source-plan invariant, evidence it remains true.

## 6. Failure and recovery review

Duplicate delivery/idempotency; cancellation races; restart; partial persistence failure; stale generation/lease; contention/resource release; malformed/unauthorized input — as applicable.

## 7. Migration and compatibility review

Schema migration, backward compat, protocol negotiation, config behavior, rollback limits, legacy-path status.

## 8. Security review

Auth enforcement, secret handling, redaction, privilege bounds, DoS bounds, audit behavior — as applicable. Secret-free.

## 9. Documentation and operations

Updated `architecture/` docs, commands, diagnostics, guards, recovery instructions.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| ... | ... | ... | ... |

critical = unsafe to operate; high = core correctness/security incomplete; medium = bounded important gap; low = polish/optional evidence gap.

## 11. Roadmap disposition

One of: milestone closed, next dependency may proceed; conditionally closed with named outstanding evidence; corrective plan required; blocked + reason; roadmap must be revised.

## 12. Registry updates

Changes required in `plans/registry.md` + source roadmap (applied in the same commit).
```

## Closure rules

MUST NOT mark `closed` when: only compilation/formatting verified; required
tests unrun without justified substitute; user-visible capability has only
internal infrastructure; security/migration requirement unimplemented;
`--no-default-features` parity broken; known high-severity defect remains;
closure depends on unrecorded assumptions. MAY mark `conditionally closed`
when production is complete but named external/operational evidence is
unobtainable here — condition, risk, and exact future evidence MUST be
explicit.

## Corrective follow-up

Keep the closure record immutable except factual corrections; create a new
implementation plan under the same subsystem; reference every unclosed
finding; add regression tests/guards; do not reopen unrelated closed scope.
