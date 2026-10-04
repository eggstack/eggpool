# Persistence Milestone 009 — M008 Documentation Reconciliation Corrective Pass

Status: ready

Repository baseline: `f02b5b8484a0d25ad29d7bdf0dec5c178b703d26`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-009--m008-documentation-reconciliation-corrective-pass`

Corrective lineage:

- original plan: `plans/implementation/persistence/008-persist-journal-mode-qualification-and-write-amplification.md`
- authoritative closure: `plans/closure/persistence/008-status.md`
- implementation: `fac930b9e7be1dd5d05519e4bc14e922df335869`
- closure transition: `f02b5b8484a0d25ad29d7bdf0dec5c178b703d26`

Long-term requirements:

- `plans/000-long-term-specification.md` — persistence integrity and evidence-gated performance claims
- `plans/003-planning-process.md` — corrective passes must be new plans, current authority must not contradict accepted closure, historical records remain immutable

Applicable ADRs:

- None. M009 is documentation/planning reconciliation only and changes no architecture.

Primary class: polish

## 1. Objective

Reconcile the remaining current-authority persistence documentation after M008 closed with a rejected PERSIST/EXTRA disposition.

M009 exists because M008 implementation and closure were correct, but two current documentation surfaces still describe superseded execution state:

1. the M008 milestone body in `plans/subsystems/persistence-roadmap.md` still says `Status: ready` even though its milestone table, registry, implementation plan, and closure record say closed/rejected;
2. `architecture/deep-dive-database.md` still says M007 “is registered as a qualification-only experiment,” even though M007 and its M008 successor have both completed physical Pi 5/MMC qualification and were rejected.

The pass must also sweep the current documentation control surface for equivalent stale M007/M008 lifecycle wording so the next persistence architecture investigation starts from one coherent source of truth.

## 2. Why verification missed this

M008 closure correctly updated its implementation plan, roadmap completion section/table, registry, and closure record, but the status transition was not applied to the earlier milestone-body heading and the M004 context paragraph in the database deep dive was not revisited.

The existing planning checks validate structure and links but do not currently assert cross-document lifecycle agreement for the active persistence roadmap.

M009 adds a narrow regression guard for those current-authority lifecycle statements rather than relying on manual review.

## 3. Scope

### Required current-authority corrections

Audit and reconcile:

- `plans/subsystems/persistence-roadmap.md`;
- `architecture/deep-dive-database.md`;
- `architecture/deep-dive-background.md` if it contains current persistence lifecycle claims;
- `plans/registry.md`;
- current persistence references in other `plans/subsystems/*.md` only when they make a present-tense M007/M008 status claim.

Required source truth:

- M007: closed/rejected on Pi 5/MMC WAL progress/convergence; no production second connection.
- M008: closed/rejected on Pi 5/MMC request-p95 gates; PERSIST/EXTRA not adopted.
- production: one SQLite connection/gate/worker, WAL/NORMAL, existing checkpoint safety behavior.
- persistence roadmap: still active because the storage/checkpoint latency tail is unresolved.
- no production storage-split successor is implicitly authorized by M008 closure.

### Immutable history

Do not rewrite:

- M003–M008 closure records;
- committed physical qualification artifacts;
- historical implementation-plan rationale that is clearly written as planning-time intent;
- legacy plans 237–242.

When historical prose says a milestone was “ready” or “registered” at that historical point, leave it intact unless the text is on a current-authority page and reads as a present-tense current state.

## 4. Non-regressing invariants

- Zero production Rust/Cargo/migration/asset behavior change.
- Zero schema or configuration change.
- No qualification artifact rewrite.
- No retrospective alteration of accepted closure evidence.
- M008 rejection metrics remain exactly those in `plans/closure/persistence/008-status.md`.
- M009 does not decide the next persistence architecture.
- M010, if registered independently, remains a separate architecture-investigation milestone.

## 5. Ordered work packages

### Work package A — Current-authority lifecycle sweep

Search current authority for stale forms including, at minimum:

- `M007 ... blocked`;
- `M007 is registered`;
- `M007 ... qualification experiment` when written as current/future state;
- `M008 ... ready`;
- `M008 ... active`;
- claims that PERSIST/EXTRA remains under consideration for production without referencing its rejection.

Classify each match as:

- current stale statement — correct it;
- immutable/historical statement — retain it;
- ambiguous narrative — rewrite only enough to make historical timing explicit.

### Work package B — Roadmap reconciliation

In `plans/subsystems/persistence-roadmap.md`:

- make the M008 milestone-body status agree with the closed/rejected milestone table and completion definition;
- keep M008 evidence and non-goals intact;
- keep M009/M010 statuses exactly aligned with the registry if those plans are registered;
- do not alter M008 closure evidence.

### Work package C — Database architecture deep-dive reconciliation

In `architecture/deep-dive-database.md`:

- replace the stale present-tense M007 sentence with the accepted M007 + M008 disposition;
- state that production remains one connection/gate/worker on WAL/NORMAL;
- point readers to M007/M008 closure records for physical evidence;
- avoid duplicating detailed benchmark tables already owned by closure records.

### Work package D — Regression guard

Add or extend a narrow tooling test, preferred path:

`tests/tooling/test_persistence_current_docs.py`

The guard should inspect only the explicitly enumerated current-authority files and assert:

- M008 roadmap status is closed/rejected, not ready/active;
- database deep dive no longer presents M007 as a future/registered experiment;
- production WAL/NORMAL invariant remains present;
- closure links for M007 and M008 remain discoverable.

Do not grep the entire repository and fail on immutable historical text.

### Work package E — Closure and unblock audit

Write:

`plans/closure/persistence/009-status.md`

Record:

- changed documentation files;
- zero production/source-schema diff;
- tooling guard results;
- explicit confirmation that M008 evidence/disposition was not rewritten;
- unblock audit.

M009 itself should not promote or block M010; the architecture investigation may proceed against the already-authoritative M008 closure.

## 6. Required verification

~~~bash
uv run --frozen ruff format --check tests/tooling/
uv run --frozen ruff check tests/tooling/
uv run --frozen pytest tests/tooling/test_persistence_current_docs.py -q
uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
~~~

If no Python guard is required because an existing planning/docs validator already provides the exact assertions, extend that validator instead and record the substitution.

No Rust build is required when the final diff is strictly Markdown + tooling-only Python and the tooling test proves no production file changed. If Rust/Cargo/source files change unexpectedly, stop and re-plan rather than expanding M009.

## 7. Acceptance criteria

M009 closes only when:

- the M008 milestone body says closed/rejected consistently with its closure/table/registry;
- no current architecture document presents M007 as still registered/pending;
- production WAL/NORMAL and one-connection/gate/worker ownership are stated consistently;
- M007 and M008 closure links are current;
- the targeted stale-lifecycle regression guard passes;
- no production Rust, Cargo, migration, dashboard asset, qualification artifact, or public API file changed;
- no historical closure record was edited;
- registry unblock audit is recorded truthfully.

## 8. Stop conditions

Stop and report instead of widening scope if:

- correcting the docs reveals a real implementation/closure contradiction rather than stale wording;
- M008 evidence values differ between closure and committed artifacts;
- a production code change appears necessary;
- resolving wording requires choosing the future storage architecture;
- historical records would need rewriting to make current docs coherent.

Those cases require a separate plan.

## 9. Closure evidence required

`plans/closure/persistence/009-status.md` must include:

- implementation commit;
- exact files changed;
- stale statements corrected;
- current-authority sweep result;
- tooling guard command/result;
- zero-production-diff proof;
- immutable-history confirmation;
- severity-tagged residual findings;
- recommendation;
- unblock audit.

## 10. Handoff note

This is a small corrective pass. Do not use it to redesign persistence, remove M007/M008 qualification code, change SQLite configuration, or draft the storage-split ADR. Its only purpose is to make current documentation agree with already-accepted M007/M008 evidence.
