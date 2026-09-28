# Persistence Milestone 005 — M004 Evidence and Planning Reconciliation Corrective Pass

Status: ready

Repository baseline: `ba1423680fc279c4aff1253809d8509691477b73`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-005--m004-evidence-and-planning-reconciliation-corrective-pass`

Corrects / follows:

- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md`
- `plans/closure/persistence/004-status.md`
- `plans/implementation/persistence/001-bounded-passive-checkpoint-scheduling-and-qualification.md`
- `plans/closure/persistence/001-status.md`

Related evidence:

- `artifacts/qualification/m004/`
- `artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 invariant 4, §3 ownership boundaries, §5 performance posture
- `plans/002-long-term-roadmap.md` — Phase 3 persistence and publication bounds
- `plans/003-planning-process.md` — corrective-pass, closure, registry, and evidence rules

Applicable ADRs:

- None required. This pass changes no runtime/storage/public contract.
- M003 remains subject to its existing architecture-review gate before any event-driven implementation plan is registered.

Primary class: polish

## 1. Objective

Correct the evidence interpretation and planning-lifecycle defects discovered after persistence M004 closed, without modifying production Rust behavior or rewriting the immutable M004 closure record.

M005 must leave one machine-checked, current source of truth for the committed M004 artifacts and a planning state that clearly distinguishes:

1. **M004's valid architectural conclusion:** timer-driven periodic checkpoint maintenance is insufficient on the Pi 5/MMC target class.
2. **M004's incorrect evidence narration:** cumulative checkpoint counters were described as if they were activity inside the measured request batch.
3. **M003's actual lifecycle:** the evidence dependency is satisfied, but the architecture review and dedicated `003-...` implementation plan do not yet exist, so M003 is not ready for implementation handoff.

The pass must preserve the M004 decision to keep the current production 60 s / 256-frame / 1000-page fallback unchanged.

## 2. Why this corrective pass is ready

The defects are reproducible from committed repository evidence and need no new hardware run:

- `artifacts/qualification/m004/` contains **14 accepted committed artifacts**:
  - 11 phase-diagnostic runs:
    - 3 × 60s/256,
    - 3 × 60s/128,
    - 3 × 60s/64,
    - 1 × 30s/64,
    - 1 × 1s/64;
  - 3 ordinary benchmark runs.
- In all 60-second and 30-second phase runs, `benchmark.task_quiescence.deltas.checkpoint.tick_count_delta == 0`.
- In those same runs, the in-batch maintenance deltas are zero for `below_threshold`, `checkpointed`, and `gate_busy`.
- In the 1s/64 run:
  - checkpoint task tick delta = 3;
  - `gate_busy_delta = 3`;
  - `checkpointed_delta = 0`;
  - `below_threshold_delta = 0`.
- The 1s/64 artifact's cumulative baseline already contained `below_threshold = 1` and `checkpointed = 2` before the measured batch. M004's closure mistakenly narrated those cumulative values as in-batch maintenance work.
- The request/COMMIT tail evidence remains valid: every tested periodic candidate fails the M004 target gates, including the 1s/64 stress case.
- The registry/roadmap currently label M003 `ready` while simultaneously stating that no implementation plan is authorized until architecture review. That conflates "hard evidence dependency satisfied" with "implementation handoff ready."
- The M004 closure suggests a future `persistence/005-event-driven-checkpoint-coordination.md`; that number is now reserved by this corrective pass. The event-driven implementation milestone remains **M003** and must eventually use `plans/implementation/persistence/003-...`.

No production experiment is needed to correct these facts.

## 3. Why M004 verification missed the defects

The M004 closeout manually summarized fields from two different semantic classes:

- cumulative counters under `benchmark.checkpoint_maintenance.baseline` / `.final`;
- measured-window changes under `benchmark.checkpoint_maintenance.deltas` and `benchmark.task_quiescence.deltas`.

The closeout used cumulative `final` values to describe activity "during the batch" instead of using the delta fields. It also manually counted accepted artifacts and manually reconciled roadmap readiness, creating three avoidable errors:

1. 13 accepted runs stated instead of 14 committed accepted artifacts;
2. cumulative `below_threshold` / `checkpointed` values described as in-batch activity;
3. M003 labeled `ready` despite the explicit "no implementation plan authorized yet" architecture gate.

The corrective pass therefore requires a test-only evidence guard rather than another prose-only reconciliation.

## 4. Non-regressing invariants

- No production Rust source changes.
- No change to:
  - `CHECKPOINT_POLL_INTERVAL_S = 60.0`;
  - `CheckpointMaintenancePolicy::DEFAULT_SOFT_WAL_FRAMES = 256`;
  - ordinary `wal_autocheckpoint=1000`;
  - one SQLite connection/gate/worker;
  - WAL/NORMAL;
  - publication/finalization ownership;
  - backup/recovery/restart/shutdown behavior.
- No schema, Config, CLI, HTTP, wire, public Rust API, dependency, or runtime diagnostic change.
- Do not rerun or reinterpret historical Plan 239 as substitute M004 evidence.
- Do not edit `plans/closure/persistence/004-status.md`; closed records remain immutable.
- Do not edit legacy Plans 239/240/242.
- M002 and routing-selection M001 stay closed.
- M003 event-driven runtime design is out of scope.

## 5. Scope

### In scope

- Add one test-only M004 evidence consistency guard under `tests/tooling/`.
- Derive the accepted artifact census and measured-window checkpoint activity from committed JSON fields.
- Record the corrected authoritative interpretation in `plans/closure/persistence/005-status.md`.
- Correct current-state statements in:
  - `plans/subsystems/persistence-roadmap.md`;
  - `plans/registry.md`;
  - `architecture/deep-dive-database.md`;
  - `architecture/deep-dive-background.md`.
- Reconcile M003 lifecycle:
  - hard evidence dependency is satisfied;
  - architecture review remains outstanding;
  - no implementation plan exists;
  - M003 becomes **proposed**, not implementation-ready, until a separate registration commit creates `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`.
- Explicitly reserve implementation-plan number `003` for M003 event-driven checkpoint coordination.
- Audit the M004 closure's other quantitative statements against committed artifacts and list any additional factual mismatches in the M005 closure.

### Explicitly out of scope

- Editing the immutable M004 closure record.
- New Pi/MMC runs.
- Any checkpoint scheduling or threshold retune.
- Event-driven trigger design or implementation.
- Disabling or changing the automatic checkpoint ceiling.
- Adding a second SQLite connection/worker.
- Reworking the qualification runner except for a test-only evidence guard if needed.
- Routing affinity M002.
- Provider-transport M002.
- Broad documentation cleanup unrelated to M004/M003 persistence state.

## 6. Required changes

### 6.1 Test-only evidence guard

Add a narrow test, preferred path:

`tests/tooling/test_persistence_m004_evidence.py`

The test may use small local helpers, but must not add runtime dependencies or invoke the network.

It must load the committed `artifacts/qualification/m004/*.json` files and assert at minimum:

- exactly 14 accepted committed artifacts;
- exactly 11 phase-diagnostic artifacts and 3 ordinary benchmark artifacts;
- phase candidate census:
  - 3 × 60/256,
  - 3 × 60/128,
  - 3 × 60/64,
  - 1 × 30/64,
  - 1 × 1/64;
- every committed artifact has `status == "pass"`;
- every phase artifact is Pi 5 / aarch64 / ext4 / MMC-attested;
- every 60s and 30s phase artifact has:
  - checkpoint `tick_count_delta == 0`;
  - `below_threshold_delta == 0`;
  - `checkpointed_delta == 0`;
  - `gate_busy_delta == 0`;
- the 1s/64 phase artifact has:
  - checkpoint `tick_count_delta == 3`;
  - `gate_busy_delta == 3`;
  - `checkpointed_delta == 0`;
  - `below_threshold_delta == 0`;
- the 1s/64 baseline/final cumulative values are not equal to the in-batch deltas, proving why cumulative counters must not be narrated as measured-window activity;
- all three 60s/256 runs fail at least one M004 acceptance gate through the committed request/COMMIT evidence;
- the 1s/64 stress run still fails the maximum-request / foreground-COMMIT gate;
- ordinary benchmark artifacts converge to zero pending requests and zero active reservations.

The guard should expose assertion messages that identify the artifact and semantic field when a future change breaks the evidence contract.

Do not encode prose wording into the test. Guard the data interpretation, not exact documentation strings.

### 6.2 Corrected evidence interpretation

Current docs must say:

- 14 accepted committed M004 artifacts exist.
- For 60s/256, 60s/128, 60s/64, and 30s/64, **no checkpoint task tick occurred during the measured phase batch**.
- Their cumulative baseline had already observed a prior below-threshold maintenance event; that is startup/pre-batch history, not in-batch checkpoint work.
- For 1s/64, three checkpoint task ticks occurred during the measured batch and all three deferred because the foreground database gate was busy:
  - 3 `gate_busy`;
  - 0 `checkpointed`;
  - 0 `below_threshold` in-batch.
- The two cumulative `checkpointed` values and one `below_threshold` value visible in the 1s/64 `final` snapshot predate the measured window and must not be described as batch activity.
- The periodic-strategy rejection still stands:
  - long cadence variants simply do not run inside these finite bursts;
  - the minimum tested one-second cadence does run, but every measured opportunity collides with foreground gate ownership;
  - foreground COMMIT still owns the automatic checkpoint tail.

Do not overclaim that M004 proved an event-driven design will succeed. It proved the current timer-driven strategy insufficient and gives concrete contention evidence that the next design must address.

### 6.3 Planning lifecycle correction

After corrected evidence is established:

- Persistence M004 remains closed historically.
- Persistence M005 is the active corrective milestone until its closure.
- M003 should be represented as **proposed**:
  - its evidence dependency is satisfied;
  - architecture review is still required;
  - no implementation plan exists;
  - no agent may implement it from the roadmap alone.
- The registry's dependency-ready implementation plan table must contain only M005 while M005 is active/ready.
- After M005 closes, that table should be empty unless a separate later commit registers the M003 implementation plan.
- A future M003 registration must use:
  - `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`;
  - not `005-...`.
- The future M003 registration is a separate planning action and separate status transition from M005 closure.

## 7. Ordered work packages

### Work package A — Machine-audit committed M004 artifacts

Intent:

Establish data-derived truth before editing current docs.

Required changes:

1. Add the test-only evidence guard.
2. Enumerate the artifact set by filename and diagnostic mode.
3. Assert candidate identity, target class, status, phase/ordinary counts.
4. Assert measured-window checkpoint task and maintenance deltas.
5. Assert the acceptance-gate failures that support periodic rejection.
6. Assert ordinary durable convergence.

Acceptance evidence:

- focused test passes;
- the test fails if `final` cumulative counters are substituted for `deltas`;
- no network/hardware dependency.

### Work package B — Reconcile current persistence evidence docs

Intent:

Correct current authority without mutating historical closed records.

Required changes:

- Update the persistence roadmap current-state M004 paragraph.
- Update `architecture/deep-dive-database.md`.
- Update `architecture/deep-dive-background.md`.
- Remove all current statements that:
  - say 13 accepted artifacts;
  - say a maintenance tick ran inside 60s/30s phase batches;
  - say 1s/64 performed two in-batch PASSIVE checkpoints;
  - use cumulative `final` counts as measured-window deltas.
- Preserve the valid target, request-latency, COMMIT, WAL-sequence, convergence, backup/recovery/restart/shutdown conclusions.

Acceptance evidence:

- every current statement about M004 maintenance activity is traceable to the test-guarded `deltas` fields;
- M004 closure remains untouched.

### Work package C — Correct M003 lifecycle and numbering

Intent:

Prevent the next handoff from skipping architecture review.

Required changes:

- Change current roadmap/registry wording so M003 is `proposed`, not `ready`, until its dedicated implementation plan exists.
- State that the hard evidence dependency is satisfied but the architecture-review/implementation-plan dependency is not.
- Remove or supersede current statements that call M003 dependency-ready for implementation.
- Reserve local number 003 for the future event-driven plan.
- Explicitly identify the M004 closure's `005-event-driven...` suggestion as a superseded numbering statement; do not edit the historical closure.
- Do not write the M003 implementation plan in this corrective pass.

Planning-commit discipline:

Because `plans/003-planning-process.md` requires one registry status change per commit, execute closeout in separate commits when necessary:

1. one reconciliation commit may move M003 `ready -> proposed` after the evidence guard is green;
2. a later closure commit moves M005 `ready/closing -> closed` and adds `plans/closure/persistence/005-status.md`.

Do not combine both status transitions into one registry commit.

### Work package D — Write corrective closure and blocker audit

Intent:

Make M005 the authoritative correction layer.

Create:

`plans/closure/persistence/005-status.md`

It must:

- state that M004's periodic-strategy rejection remains valid;
- enumerate every corrected factual statement;
- link each correction to committed artifact fields/test assertions;
- explain the cumulative-vs-delta error;
- record 14 accepted committed artifacts;
- record the correct measured-window activity:
  - 60/30s candidates: zero in-batch checkpoint ticks;
  - 1s/64: 3 ticks, 3 gate-busy, zero PASSIVE checkpoints;
- preserve the no-retune decision;
- state M003 lifecycle as proposed pending its own plan;
- audit routing-selection M002 and provider-transport M002 without promotion;
- classify residual findings by severity.

## 8. Failure, cancellation, restart, contention semantics

No runtime path changes, so no new cancellation/restart semantics are introduced.

The corrected evidence must, however, preserve the important contention distinction:

- `gate_busy_delta` belongs to the optional maintenance attempt, meaning foreground work already owned the gate.
- It is not evidence that foreground requests waited behind a maintenance checkpoint.
- The 1s/64 batch therefore shows timer-driven maintenance lost three in-window acquisition opportunities to foreground ownership.
- Since `checkpointed_delta == 0`, no claim may be made about the effect of an in-batch PASSIVE checkpoint on foreground latency from this artifact.

The eventual M003 design must handle this evidence, but M005 must not design the mechanism.

## 9. Compatibility and migration

No migration.

No production compatibility change.

No artifact rewrite.

The committed M004 JSON artifacts remain byte-for-byte evidence. The corrective guard and docs consume them as-is.

M004's immutable closure remains part of history; M005 closure becomes the current authoritative correction for the specific evidence/lifecycle statements enumerated here.

## 10. Required tests

Required:

- new M004 evidence consistency test;
- existing qualification tooling tests to ensure the new guard does not alter runner semantics.

No Rust workspace rerun is required because production Rust is explicitly out of scope and must remain unchanged.

If an implementation agent changes any Rust source despite this plan, stop and re-plan rather than broadening verification.

## 11. Required verification commands

~~~bash
uv sync --frozen
uv run pytest tests/tooling/test_persistence_m004_evidence.py -q
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run ruff format --check tests/tooling/
uv run ruff check tests/tooling/
git diff --check
git diff --name-only -- rust/
~~~

Acceptance for the last command: no output.

Also run a repository text audit for stale current-state claims. Use any repository-native search tool, but verify at minimum that current (non-historical) roadmap/architecture/registry docs no longer claim:

- "thirteen accepted" for M004;
- two in-batch `checkpointed` events in 1s/64;
- one in-batch `below_threshold` event in the 60s/30s runs;
- M003 implementation plan number 005;
- M003 ready for implementation without a `003` plan.

Historical M004 closure text may still contain those strings and must not be edited.

## 12. Documentation updates

Required current-state updates:

- `plans/subsystems/persistence-roadmap.md`
- `plans/registry.md`
- `architecture/deep-dive-database.md`
- `architecture/deep-dive-background.md`
- `plans/implementation/persistence/005-m004-evidence-and-planning-reconciliation-corrective-pass.md` — lifecycle only
- new `plans/closure/persistence/005-status.md`

Historical records explicitly unchanged:

- `plans/closure/persistence/004-status.md`
- `plans/closure/persistence/001-status.md`
- `plans/closure/persistence/002-status.md`
- legacy Plans 239/240/242.

## 13. Acceptance criteria

M005 may close only when:

- committed M004 artifact census is machine-verified as 14 accepted artifacts;
- candidate/mode counts match 11 phase + 3 ordinary;
- every long-cadence phase artifact is verified to have zero in-batch checkpoint ticks and zero maintenance-action deltas;
- 1s/64 is verified to have exactly three in-batch checkpoint ticks, all three `gate_busy`, zero `checkpointed`, zero `below_threshold`;
- current docs distinguish cumulative baseline/final counters from measured-window deltas;
- M004's periodic rejection remains supported by request/COMMIT acceptance-gate failures;
- no production constant or Rust source changes;
- M003 is no longer represented as implementation-ready without a plan;
- future event-driven implementation numbering is unambiguously `003`;
- dependency-ready registry contains no M003 entry until a separate `003` plan is registered;
- M005 closure explicitly supersedes only the erroneous M004 interpretations, not the valid rejection outcome;
- routing-selection M002 and provider-transport M002 remain unchanged.

## 14. Stop conditions

Stop and report rather than improvise if:

- any committed artifact does not match the census above;
- the artifact fields needed to distinguish cumulative vs delta semantics are missing or inconsistent;
- correcting the evidence would overturn the periodic-strategy rejection;
- a current document requires changing canonical long-term direction;
- runtime code would need to change;
- an agent attempts to design or implement M003 inside M005;
- a second hardware run becomes necessary to resolve a newly discovered factual conflict.

If corrected evidence materially changes the M004 architectural disposition, do not close M005 as a documentation corrective; open a new evidence/architecture plan.

## 15. Closure evidence required

`plans/closure/persistence/005-status.md` must include:

- baseline and corrective commits;
- exact committed artifact census;
- table of each phase candidate with:
  - interval/threshold;
  - phase artifact count;
  - checkpoint task tick delta;
  - `below_threshold_delta`;
  - `checkpointed_delta`;
  - `gate_busy_delta`;
  - maximum request latency;
  - maximum publication/finalization COMMIT as relevant;
- explicit before/after correction list for M004 narration;
- explanation of why cumulative counters caused the error;
- test-only guard path and command/results;
- no-Rust-diff proof;
- current roadmap/registry/architecture corrections;
- M003 status and numbering disposition;
- blocker audit for routing-selection M002/provider-transport M002;
- severity-tagged residual findings;
- final disposition `closed` only if all current control surfaces are internally consistent.

## 16. Handoff notes

Treat repository artifacts as the data source and prose as the thing being corrected.

Do not "fix" M004 by editing its closure record. Closed records are historical evidence. The corrective chain is:

~~~text
M004 closure (immutable, contains interpretation mistakes)
        |
        v
M005 machine-checks committed artifacts
        |
        +--> current docs corrected
        +--> M003 lifecycle corrected to proposed
        +--> M005 closure becomes authoritative correction layer
        |
        v
separate future planning action
        |
        +--> plans/implementation/persistence/003-event-driven-checkpoint-coordination.md
        +--> architecture review
        +--> M003 proposed -> ready
~~~

The target architectural conclusion to preserve is narrow: the existing timer-driven periodic strategy is insufficient on the tested Pi/MMC workload. M005 must not claim that a particular event-driven design is already proven.
