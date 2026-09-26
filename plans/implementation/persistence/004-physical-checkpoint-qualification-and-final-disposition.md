# Persistence Milestone 004 — Physical Checkpoint Qualification and Final Disposition

Status: ready

Repository baseline: `213e35e166a0b78030f4c53ff4003638b5e6c7d2`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-004--physical-checkpoint-qualification-and-final-disposition`

Corrects / follows:

- `plans/implementation/persistence/001-bounded-passive-checkpoint-scheduling-and-qualification.md`
- `plans/closure/persistence/001-status.md` — conditionally closed; physical Pi/MMC evidence outstanding
- `plans/239-sqlite-publication-commit-checkpoint-phase-diagnostic.md`
- `plans/240-passive-sqlite-checkpoint-production-follow-up.md`

Related completed work:

- `plans/closure/persistence/002-status.md`
- `plans/closure/routing-selection/001-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 invariant 4, §3 ownership boundaries, §5 performance posture
- `plans/002-long-term-roadmap.md` — Phase 3 persistence and publication bounds
- `plans/003-planning-process.md` — conditional closure, evidence, registry, and corrective/follow-up rules

Applicable ADRs:

- None required for this qualification/closeout pass.
- Stop for architecture review before any design that adds a second SQLite connection/writer, disables the production automatic-checkpoint safety ceiling without an equally bounded replacement, adds public checkpoint configuration, or creates a new process-lifecycle owner.

Primary class: polish

## 1. Objective

Resolve the only material open condition from persistence M001 with physical Raspberry Pi-class MMC evidence, then make one explicit final disposition:

1. **Periodic candidate accepted:** current or narrowly retuned bounded PASSIVE maintenance satisfies the target-class latency/WAL/recovery gates. Record the evidence, close the M001 condition through this M004 closure, and leave M003 unpromoted.
2. **Periodic candidate rejected:** no allowed periodic candidate satisfies the gates. Record the evidence without weakening safety, keep the landed conservative mechanism, and promote persistence M003 to the next architecture/design milestone. Do not implement M003 in this plan.

Also reconcile the planning/documentation control surfaces that became stale after M001/M002/routing-selection M001 landed. This bookkeeping is part of closure, not a separate runtime project.

The default outcome of this plan is **measurement and disposition, not code change**. Production constants may change only when physical evidence identifies a clearly superior candidate within the already-landed M001 design.

## 2. Why this milestone is ready

All hard and interface dependencies are satisfied:

- M001 production mechanism landed in `6eae94db` and is conditionally closed in `plans/closure/persistence/001-status.md`.
- M002 is closed in `plans/closure/persistence/002-status.md`; it does not alter checkpoint ownership.
- The Plan 239 physical diagnostic already established the original defect on a Raspberry Pi 5 / ext4 / MMC target and provides the baseline method.
- M001 added feature-gated threshold/cadence tuning plus `--diagnose-checkpoint-maintenance`, so this pass does not need new runtime instrumentation merely to obtain the missing evidence.
- The production safety posture is already conservative: one connection/gate/worker, WAL/NORMAL, `wal_autocheckpoint=1000` unchanged, `try_acquire` deferral, and no public tuning surface.

The remaining dependency is operational: access to a qualifying Linux/aarch64 Raspberry Pi-class target with MMC-class storage. That prevents final closure if unavailable, but does not justify changing architecture or fabricating substitute evidence.

## 3. Current implementation evidence

Authority paths:

- `rust/src/db/connection.rs`
- `rust/src/db/qualification.rs`
- `rust/src/task_supervisor.rs`
- `rust/src/runtime_lifecycle/process.rs`
- `rust/src/runtime_lifecycle/recovery.rs`
- `rust/src/operations/backup.rs`
- `rust/tests/database_compatibility.rs`
- `rust/tests/runtime_lifecycle_r006.rs`
- `rust/tests/runtime_lifecycle_r008.rs`
- `rust/tests/operations_o006.rs`
- `scripts/qualification_sbc.py`
- `tests/tooling/test_qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc-benchmark.toml`
- `artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json`
- `architecture/deep-dive-database.md`
- `architecture/deep-dive-background.md`

### 3.1 Original physical failure evidence

Plan 239's Pi 5/MMC artifact established:

- WAL mode, `synchronous=NORMAL`, 4096-byte pages;
- default `wal_autocheckpoint=1000`;
- 60 sequential native Responses finite requests per run;
- default H0 maximum request latencies of 4009 ms, 12012 ms, and 14903 ms;
- worst H0 samples dominated by foreground publication `COMMIT`;
- qualification-only `wal_autocheckpoint=0` reduced maximums to 14 ms, 5 ms, and 4 ms;
- a 256-page automatic threshold merely traded rare large publication pauses for more frequent pauses, including finalization;
- database-only tmpfs removed the tail, confirming storage/checkpoint ownership rather than provider latency.

That evidence is historical baseline only. It does not prove the new M001 maintenance candidate.

### 3.2 Current production candidate

Persistence M001 currently ships:

- `CheckpointMaintenancePolicy::DEFAULT_SOFT_WAL_FRAMES = 256`;
- a 60-second process-owned checkpoint polling interval;
- transaction-watermark idle suppression;
- non-queueing `try_acquire_owned` on the existing database gate;
- `PRAGMA wal_checkpoint(NOOP)` observation on the existing worker;
- `PRAGMA wal_checkpoint(PASSIVE)` only when the soft threshold is due;
- unchanged SQLite automatic checkpoint threshold as the hard fallback;
- feature-only:
  - `EGGPOOL_QUALIFICATION_CHECKPOINT_INTERVAL_S` in 1..=3600;
  - `EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES` in 1..=1000;
  - `EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES` for historical Plan 239 diagnostics;
- additive scalar checkpoint-maintenance qualification counters.

Host verification is already strong, but the physical performance claim remains unproven.

### 3.3 Current planning/documentation defects to reconcile

The runtime is ahead of some planning prose:

- `plans/registry.md` still points "Most recently closed" at request-admission-wire M005 instead of the later routing-selection M001 closure.
- The registry's "Dependency-ready implementation plans" table still lists closed/conditionally-closed milestones; it should contain only work that is actually ready for handoff.
- The persistence roadmap's "Current state" still describes the pre-M001 14,400-second unconditional checkpoint and pre-M002 publication/metrics inefficiencies.
- `architecture/deep-dive-database.md` describes an "accepted M001 candidate" without immediately stating that physical target performance qualification remains conditional.
- The M001 closure contains historical statements that M002/routing M001 were still ready; do **not** rewrite that closure record. Correct current control surfaces instead.

This plan registration may repair roadmap/registry control-surface state. Architecture wording that describes production qualification status should be corrected during M004 execution/closure when the final disposition is known.

## 4. Invariants that must not regress

- Exactly one SQLite connection, one serialized database gate, and one tokio-rusqlite worker.
- WAL mode and `synchronous=NORMAL`.
- Production `wal_autocheckpoint=1000` remains unchanged throughout this milestone.
- Publication/finalization transaction grouping, durable request state, reservation ownership, compensation, and startup reconciliation remain unchanged.
- Checkpoint maintenance remains process-owned through the existing `checkpoint` task.
- Optional checkpoint work does not queue behind an already-owned foreground gate.
- No per-request checkpoint spawning, additional worker, or separate checkpoint connection.
- Qualification-only overrides remain absent from ordinary release builds and public Config/CLI/example configuration.
- No credentials, prompts, bodies, provider/model/account identities, SQL text, database paths, raw WAL bytes, or cache keys enter the qualification artifact.
- No public HTTP, wire, CLI, config, or Rust compatibility change.
- M002 publication/metrics cleanup and routing-selection M001 remain closed and are not reopened.

## 5. Scope

### In scope

- Build ordinary and `qualification-db-diagnostics` release candidates from the same reviewed commit.
- Verify candidate SHA-256 and record exact source commit.
- Run the current 60s/256-frame policy on the physical target.
- Run a bounded threshold/cadence matrix using only the already-landed feature-gated tuning knobs.
- Run the existing physical SBC benchmark path including its concurrency-4 finite observation to check burst behavior and process/resource convergence.
- Record publication/finalization phase timings, foreground gate wait, checkpoint-maintenance deltas, WAL-frame progress, fallback behavior, latency distribution, and ownership convergence.
- Exercise backup/restart/recovery/shutdown around recent checkpoint activity.
- If evidence supports a different internal periodic candidate, change only:
  - the production checkpoint polling constant; and/or
  - `CheckpointMaintenancePolicy::DEFAULT_SOFT_WAL_FRAMES`;
  plus their exact fixtures/docs/tests.
- Re-run the entire accepted target matrix after any constant change.
- Write `plans/closure/persistence/004-status.md` with the final disposition.
- Reconcile `plans/registry.md`, the persistence roadmap, and current architecture wording.

### Explicitly out of scope

- Disabling or raising the production automatic checkpoint threshold.
- Introducing event-driven checkpoint coordination.
- Implementing persistence M003.
- A second SQLite connection, reader/writer pool, dedicated checkpoint worker, or another runtime.
- `FULL`, `RESTART`, or `TRUNCATE` checkpoint policy changes.
- Schema migrations.
- New public checkpoint configuration or environment variables.
- New benchmark framework or hardware CI.
- Routing affinity M002 qualification.
- The optional `fairness_order` clone cleanup.
- Reopening M002 publication/metrics behavior without new correctness evidence.

## 6. Required production changes

No production change is required merely to execute M004.

### 6.1 Allowed constant-only retune

A production code change is allowed only if the physical matrix demonstrates that the current 60s/256-frame candidate is inferior to another candidate already expressible through the M001 tuning hooks.

Permitted production edits:

- `CHECKPOINT_POLL_INTERVAL_S` in `rust/src/task_supervisor.rs`;
- `CheckpointMaintenancePolicy::DEFAULT_SOFT_WAL_FRAMES` in `rust/src/db/connection.rs`;
- directly associated fixture/test/documentation expectations.

No other behavior change is authorized.

The selected candidate must be rerun in the complete target acceptance corpus after the constants change. Do not select constants from a single lucky run.

### 6.2 No speculative retune

If several candidates all pass and differences are within ordinary run variance, prefer the simpler/current 60s/256 configuration. This milestone is not an exercise in optimizing the smallest observed p50.

If the current candidate already clears the target gate with margin and bounded WAL behavior, no production Rust edit is preferable.

## 7. Ordered work packages

### Work package A — Freeze candidate identity and preflight the physical target

Intent:

Ensure evidence is attributable to one exact build and one valid storage class.

Required work:

1. Record `git rev-parse HEAD`.
2. Build the ordinary locked release candidate.
3. Build the `qualification-db-diagnostics` locked release candidate from the same commit.
4. Record SHA-256 and byte size for both.
5. Verify the target:
   - Linux/aarch64;
   - Raspberry Pi 5 or materially equivalent Pi-class SBC;
   - ext4 or the same filesystem class used by Plan 239;
   - MMC/non-rotational storage for the database;
   - not tmpfs for the acceptance run;
   - page size/effective SQLite pragmas captured by the feature build.
6. Use `--expected-sha256` when invoking the qualification runner.
7. Record candidate origin truthfully (`on-device-release-build` or `q005-qualified-aarch64-copy`).

Acceptance evidence:

- target metadata and candidate hashes in the M004 artifact/closure;
- runner physical-target gate passes;
- no cloud VM or desktop filesystem is labeled Pi/MMC evidence.

### Work package B — Reproduce the current 60s/256 candidate

Intent:

Answer the primary unresolved question before tuning anything.

Run at least three accepted phase-diagnostic runs with:

- `--diagnose-publication-phases`;
- `--diagnose-checkpoint-maintenance`;
- `--qualification-checkpoint-interval-s 60`;
- `--qualification-checkpoint-soft-frames 256`;
- the standard SBC benchmark fixture;
- a unique output artifact for each run or a combined M004 evidence artifact preserving all run summaries.

Each run must retain exactly the existing Plan-239-style 60 sequential successful requests and one publication/finalization transaction record per request.

Collect:

- p50/p95/maximum total latency;
- five slowest request phase correlations;
- publication/finalization gate-wait/begin/body/commit phases;
- checkpoint `not_due`/`gate_busy`/`below_threshold`/`checkpointed`/failure deltas;
- last/peak observed WAL-frame facts available from the runner;
- effective pragmas;
- task tick deltas;
- pending-request and active-reservation convergence;
- direct-provider control.

Acceptance evidence:

- three accepted, internally consistent runs;
- no background DB contamination other than the intentionally measured checkpoint task;
- no missing/duplicate foreground transaction records.

### Work package C — Bounded policy matrix

Intent:

Determine whether the current constants are safe/effective or whether a narrow retune is justified.

Required minimum matrix:

1. current production candidate: 60s / 256 frames;
2. 60s / 128 frames;
3. 60s / 64 frames.

If the current candidate or both lower thresholds still allow automatic fallback / unacceptable foreground tails before the checkpoint task can run, add cadence variants using the existing knob, starting with:

4. 30s / the best safe threshold from the first matrix;
5. 15s / that threshold only if 30s remains insufficient.

Do not expand beyond a small matrix merely to optimize p50. The question is whether a periodic opportunistic owner can intercept routine WAL growth before the 1000-page automatic fallback without producing excessive checkpoint I/O or foreground gate waits.

For each candidate:

- run enough accepted 60-request phase batches to distinguish repeatable behavior from a one-run outlier;
- record checkpoint frequency, `gate_busy` deferrals, foreground gate wait, fallback evidence, WAL growth, and request latency;
- reject a candidate that merely trades a rare multi-second `COMMIT` for frequent material gate-wait stalls;
- prefer the least aggressive candidate that meets all acceptance gates with margin.

### Work package D — Burst/concurrency and operational lifecycle evidence

Intent:

Ensure the periodic policy is not only good for the sequential diagnostic.

Use the existing ordinary physical SBC benchmark mode on the ordinary release candidate, with a representative `--benchmark-samples` value such as 30, so the runner executes its native finite and concurrency-4 observations.

Additionally exercise:

- backup creation after recent checkpoint activity;
- process restart/reopen with non-empty or recently-active WAL state;
- startup recovery/convergence;
- task supervisor shutdown while checkpoint maintenance can be due;
- database close;
- no-default compile/test parity from the implementation host or target as appropriate.

Acceptance evidence:

- no pending requests or active reservations after stabilization;
- no checkpoint-maintenance failure accumulation;
- bounded WAL under the burst case;
- backup/restart/recovery/shutdown succeed;
- no claim that task cancellation interrupts already-running SQLite worker I/O.

### Work package E — Decide keep / retune / reject

Intent:

Make one explicit engineering decision from the evidence.

#### E1 — Keep current 60s/256

Choose this when it meets the full acceptance gate and no tested alternative demonstrates a material/repeatable safety or tail-latency advantage.

Production code change: none.

#### E2 — Retune periodic constants

Allowed only when another tested candidate clearly meets the gates more reliably or with materially lower tail/fallback frequency without increasing contention.

Production edits remain limited to the two constants and their exact fixtures/docs. After changing them:

- rerun focused tests;
- rerun default/no-default full suites;
- rebuild ordinary + feature release candidates;
- rerun **three fresh accepted physical target runs** for the selected final constants;
- rerun ordinary concurrency/burst and operational lifecycle evidence.

Do not close on pre-change evidence.

#### E3 — Reject periodic strategy for full tail closure

Choose this when no allowed periodic candidate meets the target gate without excessive contention or automatic-fallback tails.

Production code change in M004: none beyond reverting any experimental constant edit.

Disposition:

- M001 remains historically conditionally closed with its conservative safe mechanism;
- M004 closes with "periodic strategy insufficient on target";
- persistence M003 becomes the next dependency-ready architecture/design milestone;
- do not implement or silently design M003 inside M004.

### Work package F — Planning and documentation reconciliation

Intent:

Make current control surfaces truthful after the disposition.

Required changes:

- create `plans/closure/persistence/004-status.md`;
- update the persistence roadmap milestone table and current-state description;
- update `plans/registry.md`:
  - "Most recently closed" must point at the actual latest accepted closure;
  - remove closed/conditionally-closed milestones from the dependency-ready table;
  - list only genuinely ready work;
  - audit Provider-transport M002 and routing-selection M002 blockers without promoting either absent evidence;
  - if periodic strategy failed, promote persistence M003 according to its roadmap dependency;
  - if periodic strategy passed, leave M003 unpromoted;
- update `architecture/deep-dive-database.md` so the M001 section states the final physical qualification outcome instead of an ambiguous "accepted candidate";
- update `architecture/deep-dive-background.md` if the final cadence changes;
- do not rewrite the historical M001 closure record merely because later evidence changed the current disposition.

## 8. Failure, cancellation, restart, contention semantics

This milestone must distinguish three different latency owners:

1. foreground transaction work;
2. foreground waiting behind a PASSIVE checkpoint that already acquired the gate;
3. SQLite automatic-checkpoint work occurring inside foreground `COMMIT`.

A successful result must not hide one by reporting only total request time.

`GateBusy` maintenance deferral remains correct behavior. A high deferral rate that allows repeated automatic fallback is a performance failure of the periodic candidate, not a correctness failure.

Once PASSIVE work starts on the SQLite worker, ordinary task cancellation does not imply underlying I/O cancellation. Shutdown/restart evidence must describe the observed lifecycle truthfully.

A failed qualification run must retain enough bounded scalar evidence to diagnose which gate failed, but it must not weaken the acceptance threshold to obtain closure.

## 9. Compatibility and migration

No schema migration.

No public configuration migration.

No API or protocol change.

If constants are retuned, the task name remains `checkpoint`, feature-only override names/ranges remain unchanged, and ordinary runtime JSON remains unchanged.

The M001 mechanism remains additive-safe even if M004 rejects it as sufficient to eliminate the original target tail; do not remove it unless separate evidence shows a correctness/performance regression.

## 10. Required tests

If no production constants change, do not rerun the entire Rust suite solely for ceremony if current baseline evidence remains valid; run the physical qualification, documentation validators, and focused sanity checks necessary to prove candidate identity and artifact parsing.

If production constants change, run at minimum:

- `rust/tests/database_compatibility.rs`;
- `rust/tests/runtime_lifecycle_r006.rs`;
- `rust/tests/runtime_lifecycle_r008.rs`;
- `rust/tests/coordinator_publication.rs`;
- `rust/tests/coordinator_finalization.rs`;
- `rust/tests/operations_o006.rs`;
- feature-gated `db::` and `task_supervisor` tests;
- `tests/tooling/test_qualification_sbc.py`;
- full default and no-default workspace suites.

Do not add timing assertions to unit tests. Physical timing gates belong only in the target qualification artifact/closure.

## 11. Required verification commands

### Candidate/build sanity

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-db-diagnostics
uv sync --frozen
uv run pytest tests/tooling/test_qualification_sbc.py -q
git diff --check
~~~

### Focused runtime gates when constants change

~~~bash
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib --features qualification-db-diagnostics db:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib --features qualification-db-diagnostics task_supervisor -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
~~~

### Physical phase diagnostic — current candidate example

~~~bash
python scripts/qualification_sbc.py \
  --binary <qualification-feature-binary> \
  --expected-sha256 <sha256> \
  --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --diagnose-publication-phases \
  --diagnose-checkpoint-maintenance \
  --qualification-checkpoint-interval-s 60 \
  --qualification-checkpoint-soft-frames 256 \
  --output artifacts/qualification/persistence-m004-60s-256-run1.json
~~~

Repeat for the required runs and threshold/cadence matrix.

### Ordinary physical benchmark / concurrency example

~~~bash
python scripts/qualification_sbc.py \
  --binary <ordinary-release-binary> \
  --expected-sha256 <sha256> \
  --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --benchmark-samples 30 \
  --output artifacts/qualification/persistence-m004-ordinary-benchmark.json
~~~

Use `python` vs `python3`/environment wrapper according to the target's repository tooling setup; record the exact executed commands in closure.

## 12. Documentation updates

At closure:

- `architecture/deep-dive-database.md` — physical qualification result, final cadence/threshold, fallback behavior, and whether M003 is needed.
- `architecture/deep-dive-background.md` — final checkpoint cadence if changed.
- `plans/subsystems/persistence-roadmap.md` — M004 closure and M001/M003 disposition.
- `plans/registry.md` — truthful active/ready/blocked/recent-closure state.
- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md` — lifecycle status only.
- `plans/closure/persistence/004-status.md` — authoritative evidence/result.

Do not rewrite legacy Plans 239/240/242 or closed M002/routing records.

## 13. Acceptance criteria

The periodic checkpoint strategy is accepted only if the final candidate demonstrates, on the qualifying Pi/MMC target:

- three accepted 60-request sequential runs;
- no multi-second foreground publication/finalization `COMMIT` tail;
- p95 total request latency < 100 ms in every accepted run;
- maximum total request latency < 500 ms in every accepted run;
- foreground publication/finalization `COMMIT` phase < 50 ms for all accepted requests;
- no unexplained large foreground gate-wait caused by maintenance;
- routine checkpoint progress before the 1000-page automatic fallback in the accepted corpus;
- bounded WAL-frame growth during ordinary and concurrency/burst evidence;
- no checkpoint-maintenance failures;
- pending requests = 0 and active reservations = 0 after stabilization;
- direct-provider control remains within the existing fixture's ordinary range;
- backup/restart/recovery/shutdown evidence is green;
- one connection/gate/worker and WAL/NORMAL/1000-page fallback remain unchanged.

If no periodic candidate satisfies all gates, that is a valid M004 outcome. The plan closes by recording rejection and promoting M003; it must not weaken the criteria or implement M003 opportunistically.

## 14. Stop conditions

Stop and report rather than improvise if:

- a qualifying Pi/MMC target is unavailable;
- the candidate binary cannot be tied to a reviewed commit/SHA;
- the runner's physical-target or contamination checks fail;
- qualification would require disabling the production automatic checkpoint ceiling;
- a passing result depends on a second SQLite connection/worker;
- the only apparent solution is event-driven coordination or another lifecycle owner;
- target evidence is inconsistent across repeated runs and no bounded matrix resolves it;
- a requested constant change cannot be expressed through the existing M001 policy/cadence constants;
- backup/recovery/ownership convergence fails;
- the work begins to involve routing, provider transport, request admission, or unrelated performance cleanup.

## 15. Closure evidence required

`plans/closure/persistence/004-status.md` must contain:

- exact implementation/qualification commit(s);
- ordinary and feature binary SHA-256 + byte size;
- target board, architecture, OS, kernel, filesystem, storage class, SQLite page size;
- effective `journal_mode`, `synchronous`, and `wal_autocheckpoint`;
- every threshold/cadence candidate tested;
- per-run p50/p95/max and slowest-request phase facts;
- foreground gate-wait/begin/body/commit summaries;
- maintenance outcome counters and WAL-frame progress;
- evidence whether/when the 1000-page fallback fired;
- ordinary concurrency-4/burst result and convergence;
- backup/restart/recovery/shutdown result;
- final keep / retune / reject decision with rationale;
- if retuned, post-change fresh physical evidence and full test matrix;
- current docs/registry reconciliation;
- blocker audit for persistence M003, routing-selection M002, and provider-transport M002;
- severity-tagged residual findings;
- explicit final disposition:
  - **closed — periodic strategy accepted; M003 remains unpromoted**, or
  - **closed — periodic strategy insufficient; M003 promoted for separate architecture/design planning**, or
  - **blocked/conditionally closed — required physical evidence could not be obtained**.

## 16. Handoff notes

Do not optimize the benchmark. Resolve the architectural question.

The decision tree is:

~~~text
current 60s / 256-frame candidate
        |
        +-- passes target gate with margin --> keep constants --> close condition
        |
        +-- misses --> bounded 128/64 + cadence matrix
                          |
                          +-- one periodic candidate passes cleanly
                          |      --> change only constants
                          |      --> rebuild + rerun full final evidence
                          |      --> close condition
                          |
                          +-- no periodic candidate passes
                                 --> keep conservative landed mechanism
                                 --> record failure evidence
                                 --> promote M003
                                 --> stop
~~~

M004 must leave the repository with a truthful answer to "is the periodic M001 design sufficient on the target class?" It is acceptable for that answer to be no.
