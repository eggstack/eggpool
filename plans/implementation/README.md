# Milestone Implementation Plans

Bounded plans handed directly to implementation agents. Operational documents
tied to the current repository state; may be corrected, superseded, or
archived without touching canonical long-term documents.

## Layout and naming

```text
implementation/<subsystem>/NNN-short-title.md
```

Numbering local to the subsystem roadmap.

## Required implementation-plan template

```markdown
# <Subsystem> Milestone NNN — <Title>

Status: ready | active | blocked | closing | implemented | superseded

Repository baseline: `<commit SHA or branch state>`

Source roadmap:

- `plans/subsystems/<subsystem>-roadmap.md#...`

Long-term requirements:

- `plans/000-long-term-specification.md#...`
- `plans/001-terminology-and-domain-model.md#...`

Applicable ADRs:

- `plans/adrs/ADR-NNNN-...md` (or "None required")

Primary class: invariant | capability | infrastructure | polish

## 1. Objective

One bounded outcome.

## 2. Why this milestone is ready

Closed hard deps + stable interface deps (or "no dependencies").

## 3. Current implementation evidence

Relevant code, tests, storage, protocols, guards, known gaps at baseline (cite `rust/src/...`, `rust/tests/...`, `scripts/...`; no secrets).

## 4. Invariants that must not regress

- ...

## 5. Scope

### In scope

- ...

### Explicitly out of scope

- ...

## 6. Required production changes

Behavior/ownership changes. Name likely modules where useful; do not prescribe blind mechanical edits when alternatives are valid. Cover storage/migrations, protocol/DTOs, runtime/concurrency, operator surface, security/auth, docs/guards as applicable.

## 7. Ordered work packages

### Work package A — Title

Intent:

Required changes:

Acceptance evidence:

## 8. Failure, cancellation, restart, contention semantics

Partial failure, duplicate delivery, restart, cancellation races, stale generations, concurrent callers.

## 9. Compatibility and migration

Backward compat, data migration, protocol negotiation, config fallback, legacy-path removal criteria.

## 10. Required tests

Focused unit, integration, restart/recovery, contention/cancellation, security/negative, migration/compat (cite `rust/tests/...` targets; serial `--test-threads=1`).

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Plus the narrowest focused target(s) for the change. Do not claim commands not actually run in the closure record.

## 12. Documentation updates

- ...

## 13. Acceptance criteria

Externally observable or contract-level statements.

## 14. Stop conditions

Stop and report rather than improvise when: unresolved architecture decision changes ownership; hard dep absent; migration unsafe; repo evidence contradicts a canonical invariant; external evidence unavailable; scope expands into another subsystem.

## 15. Closure evidence required

Exact evidence the later closure record must contain.

## 16. Handoff notes

Hazards, resource constraints, serial-test requirement, env requirements, preserved user changes.
```

## Handoff rules

Confirm baseline current; hard deps closed; decisions have ADRs or are out
of scope; milestone completable in one coherent pass; tests/closure evidence
specific; plan registered in `plans/registry.md`. Agents may adjust
file-level mechanics from current code but MUST NOT weaken invariants or
silently enlarge scope.

## Corrective plans

New plan, same subsystem directory, new local number. MUST reference the
original plan + closure, enumerate unclosed findings, explain why
verification missed them, and add regression evidence.
