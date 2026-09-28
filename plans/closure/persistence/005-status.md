# Persistence Milestone 005 — Closure Status

Status: closed — M004's evidence interpretation and planning lifecycle corrected; M004's rejection outcome and no-retune decision preserved

Source implementation plan:

- `plans/implementation/persistence/005-m004-evidence-and-planning-reconciliation-corrective-pass.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-005--m004-evidence-and-planning-reconciliation-corrective-pass`

Corrects (immutable historical record, not edited):

- `plans/closure/persistence/004-status.md`
- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md`
- `plans/implementation/persistence/001-bounded-passive-checkpoint-scheduling-and-qualification.md`
- `plans/closure/persistence/001-status.md`

Repository baseline reviewed: `ba1423680fc279c4aff1253809d8509691477b73`

Implementation commits or pull requests:

- `1ac81a8f` — register the M005 corrective plan (`ready`).
- `be370a61` — add the M004 evidence guard and correct persistence current state; M003 `ready` → `proposed`.
- This commit — close M005 and add this record. No production Rust, schema, config, CLI, HTTP, wire, dependency, or runtime-diagnostic change exists anywhere in the pass: `git diff --name-only ba142368..HEAD -- rust/` is empty.

## 1. Executive finding

M004's **architectural conclusion stands**: on the tested Pi 5 / ext4 / MMC
target class the existing timer-driven periodic checkpoint strategy is
insufficient, no allowed periodic candidate clears the M004 §13 acceptance
gates, and the landed 60 s / 256-frame / 1000-page mechanism is retained
unchanged with no retune. M005 does not overturn that outcome and does not
claim that any event-driven design has been proven.

What M005 corrects is M004's *reading of its own evidence* and the resulting
planning state:

1. **Measured-window checkpoint activity was narrated from cumulative
   counters.** The committed artifacts carry three distinct semantic classes —
   cumulative `checkpoint_maintenance.baseline` / `.final` snapshots,
   measured-window `checkpoint_maintenance.deltas`, and
   `task_quiescence.deltas` tick counts. The M004 closeout summarized the
   cumulative `final` values as activity "in the batch". The true
   measured-window result is the opposite of what was written: **no checkpoint
   task tick occurred inside any 30 s or 60 s phase batch at all**, and the
   1s/64 stress run's three in-window ticks **all deferred** (`gate_busy` 3,
   `checkpointed` 0, `below_threshold` 0).
2. **The accepted-artifact census was 13 instead of 14.**
   `artifacts/qualification/m004/` holds 14 accepted committed artifacts:
   11 phase-diagnostic runs plus 3 ordinary benchmark runs.
3. **M003 was labelled implementation-ready while the same documents stated
   that no implementation plan was authorized.** Its hard evidence dependency
   is satisfied; its architecture review and dedicated
   `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`
   are not. M003 is `proposed`, and local number `003` is reserved for it. The
   M004 closure's `005-event-driven-checkpoint-coordination.md` suggestion is a
   superseded numbering statement retained as history only.

The correction is machine-checked: `tests/tooling/test_persistence_m004_evidence.py`
derives the census, the candidate matrix, the target attestation, the
measured-window deltas, the acceptance-gate failures, and the ordinary-run
convergence from the committed JSON, and fails if a cumulative counter is
substituted for a measured-window delta.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Machine-audit the committed M004 corpus before editing prose | `tests/tooling/test_persistence_m004_evidence.py` (17 tests, all green) reading `artifacts/qualification/m004/*.json` | pass | No network, no hardware, no new dependency |
| Exactly 14 accepted committed artifacts | `test_m004_committed_census_is_fourteen_accepted_artifacts` | pass | 14 files; every `status == "pass"` |
| 11 phase-diagnostic + 3 ordinary benchmark | `test_m004_census_splits_eleven_phase_and_three_ordinary_artifacts` | pass | Ordinary runs carry no `diagnostic_mode` and `sample_count == 10` |
| Candidate census 3×60/256, 3×60/128, 3×60/64, 1×30/64, 1×1/64 | `test_m004_phase_candidate_census_matches_the_recorded_matrix` (read from `benchmark.checkpoint_tuning`) | pass | Filenames cross-checked against artifact-recorded candidates |
| Pi 5 / aarch64 / ext4 / MMC attested in every artifact | `test_m004_artifacts_attest_the_raspberry_pi_5_aarch64_ext4_mmc_target` | pass | `Raspberry Pi 5 Model B Rev 1.0`, `aarch64`, `ext4`, `mmc`, `non-rotational`, `Linux aarch64 plus device-tree board model` |
| No automatic-checkpoint override, WAL/NORMAL/1000-page ceiling intact | `test_m004_phase_artifacts_keep_wal_normal_and_the_automatic_ceiling` | pass | `wal_autocheckpoint_override_pages` is `null` in all 11 phase runs |
| Every 30 s/60 s phase run: 0 in-batch checkpoint ticks and 0 in-batch maintenance actions | `test_long_cadence_phase_candidates_ran_no_checkpoint_tick_in_the_batch` (10 artifacts) | pass | `tick_count_delta == 0`; all five maintenance deltas `== 0` |
| 1s/64: 3 in-batch ticks, 3 `gate_busy`, 0 `checkpointed`, 0 `below_threshold` | `test_minimum_cadence_stress_defers_every_in_window_checkpoint` | pass | Also `not_due_delta == 0` and `failures_delta == 0` |
| No phase candidate completed an in-batch PASSIVE maintenance checkpoint | `test_no_phase_candidate_completed_an_in_batch_maintenance_checkpoint` | pass | `checkpointed_delta == 0` in all 11 |
| Cumulative ≠ measured window (the corrected defect) | `test_cumulative_snapshots_are_not_measured_window_deltas` | pass | `baseline + delta == final` identity for all five counters and the checkpoint tick; then asserts the 1s/64 cumulative narrative (`checkpointed` 2, `below_threshold` 1) differs from the true deltas (0, 0) |
| All three 60s/256 runs fail ≥1 M004 §13 gate | `test_current_candidate_60s_256_fails_m004_acceptance_gates` + `test_bounded_matrix_and_stress_candidates_also_fail_the_gates` | pass | Gates: p95 total < 100 ms, maximum total < 500 ms, foreground publication/finalization `COMMIT` < 50 ms. Every one of the 11 phase runs fails at least one |
| 1s/64 stress still fails the maximum-request and foreground-COMMIT gates | `test_minimum_cadence_stress_still_fails_request_and_commit_gates` | pass | 1890 ms max, 1 883 055 µs publication COMMIT; requests without a sequence change stay under the gate |
| Foreground `COMMIT` owns the WAL checkpoint tail in every phase run | `test_foreground_commit_owns_the_wal_checkpoint_tail_in_every_phase_run` | pass | `wal_checkpoint_sequence_change_count == 3`, unchanged 57; slowest request has `wal_checkpoint_sequence_changed == true`, publication `COMMIT` ≥ 50 ms, gate-wait < 50 ms, pre-provider dominated |
| Maintenance failures zero; direct-provider control ordinary | `test_maintenance_failures_are_zero_and_provider_control_stays_ordinary` | pass | `failures == 0` cumulative and in-batch; control p50 ≤ 5 ms over 30 samples |
| Durable convergence in phase runs | `test_phase_runs_converge_with_no_pending_work` | pass | `pending_requests == 0`, `active_reservations == 0`, all statuses completed, non-empty backup archive |
| Ordinary runs converge and lifecycle checks stay green | `test_ordinary_benchmarks_converge_and_keep_lifecycle_checks_green` | pass | 24 functional ids per run incl. `backup-recover-isolated`, `bounded-maintenance`, `rehash-runtime-status`, `restart-reconcile`, `graceful-shutdown`; fd 14, thread 2, no logical leaks |
| Qualification-only candidate separation preserved | `test_ordinary_candidate_differs_from_the_diagnostic_candidate` | pass | Benchmark runs share one SHA-256; the 1s/64 stress run uses the distinct `qualification-db-diagnostics` build |
| Current docs use measured-window deltas, not cumulative counters | `plans/subsystems/persistence-roadmap.md` §4, `architecture/deep-dive-database.md`, `architecture/deep-dive-background.md`, `plans/registry.md` | pass | Text audit in §4 |
| M003 no longer represented as implementation-ready without a plan | roadmap §4/§6/§7/§12, registry active-roadmap + unblock-audit rows, both architecture deep dives | pass | M003 `proposed`; no `003` plan exists; local number 003 reserved |
| Dependency-ready registry contains no M003 entry | `plans/registry.md` dependency-ready table | pass | Table emptied at closure; only M005 was listed while active |
| Zero production constant or Rust source change | `git diff --name-only ba142368..HEAD -- rust/` → empty; `git diff --check` clean | pass | `CHECKPOINT_POLL_INTERVAL_S = 60.0`, `DEFAULT_SOFT_WAL_FRAMES = 256`, `wal_autocheckpoint=1000` all untouched |
| M004's rejection outcome and no-retune decision preserved | §3, §5, §11 below | pass | No cadence/threshold/ceiling retune is authorized by this pass |
| M004 closure record unchanged | `git diff --name-only ba142368..HEAD -- plans/closure/persistence/004-status.md` → empty | pass | Corrections are additive in this record; historical text is preserved verbatim |
| Routing-selection M002 and provider-transport M002 unchanged | §11 blocker audit | pass | Neither is promoted |

## 3. Committed artifact census and per-candidate evidence

Machine-derived by `tests/tooling/test_persistence_m004_evidence.py`.

**Census: 14 accepted artifacts** (11 phase-diagnostic + 3 ordinary benchmark),
all `status == "pass"`, all attested Pi 5 / aarch64 / ext4 / MMC from
`8113d264`. Ordinary release candidate
`c3f4391ad7404dabeb0c0d219b53aaab6b9bb6568e4c29594586a1671a87893f`
(31 421 560 bytes) drives the benchmark runs;
`qualification-db-diagnostics` candidate
`b7a4fa402e1caee6048dcb527d33a4e10234a8d9e325a54ecaff8646f10c32a7`
(31 485 520 bytes) drives the 1s/64 stress run. Every phase run records
`journal_mode=wal`, `synchronous=NORMAL`, `wal_autocheckpoint_pages=1000`,
`page_size=4096`, and `wal_autocheckpoint_override_pages: null`.

| Interval / frames | Artifact | Checkpoint tick delta | `below_threshold_delta` | `checkpointed_delta` | `gate_busy_delta` | `not_due_delta` | `failures_delta` | Max request | Max publication COMMIT | Max finalization COMMIT | p95 total | Batch elapsed | Cumulative baseline `below_threshold` | Cumulative `checkpointed` base→final |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `60s/256` | `256-60s-run-1.json` | 0 | 0 | 0 | 0 | 0 | 0 | 1709 ms | 1615 ms | 578 ms | 9 ms | 4059 ms | 1 | 0 → 0 |
| `60s/256` | `256-60s-run-2.json` | 0 | 0 | 0 | 0 | 0 | 0 | 561 ms | 522 ms | 30 ms | 26 ms | 2210 ms | 1 | 0 → 0 |
| `60s/256` | `256-60s-run-3.json` | 0 | 0 | 0 | 0 | 0 | 0 | 10 943 ms | 10 928 ms | 58 ms | 15 ms | 13 136 ms | 1 | 0 → 0 |
| `60s/128` | `128-60s-run-1.json` | 0 | 0 | 0 | 0 | 0 | 0 | 14 906 ms | 14 893 ms | 68 ms | 11 ms | 26 284 ms | 1 | 0 → 0 |
| `60s/128` | `128-60s-run-2.json` | 0 | 0 | 0 | 0 | 0 | 0 | 2445 ms | 2407 ms | 28 ms | 20 ms | 4873 ms | 1 | 0 → 0 |
| `60s/128` | `128-60s-run-3.json` | 0 | 0 | 0 | 0 | 0 | 0 | 28 653 ms | 28 536 ms | 112 ms | 9 ms | 47 639 ms | 1 | 0 → 0 |
| `60s/64` | `64-60s-run-1.json` | 0 | 0 | 0 | 0 | 0 | 0 | 5986 ms | 5896 ms | 85 ms | 16 ms | 13 357 ms | 1 | 0 → 0 |
| `60s/64` | `64-60s-run-2.json` | 0 | 0 | 0 | 0 | 0 | 0 | 3734 ms | 3660 ms | 69 ms | 10 ms | 8587 ms | 1 | 0 → 0 |
| `60s/64` | `64-60s-run-3.json` | 0 | 0 | 0 | 0 | 0 | 0 | 2711 ms | 2665 ms | 60 ms | 15 ms | 5931 ms | 1 | 0 → 0 |
| `30s/64` | `64-30s-run-1.json` | 0 | 0 | 0 | 0 | 0 | 0 | 3641 ms | 3633 ms | 7 ms | 13 ms | 4840 ms | 1 | 0 → 0 |
| `1s/64` | `64-1s-run-1.json` | **3** | 0 | 0 | **3** | 0 | 0 | 1890 ms | 1883 ms | 7 ms | 8 ms | 2817 ms | 1 | **2 → 2** |

Ordinary benchmark runs (`ordinary-benchmark-run-1/2/3.json`,
`--benchmark-samples 10`): native finite min/p50/p95/max 4/4/545/545,
4/5/365/365, 3/4/314/314 ms; native streaming max 8/7/8 ms; translated
streaming max 8/7/9 ms; concurrency-4 batch 32/32 completed at 11.8 / 33.0 /
8.2 requests per second; durable `requests=85`, `attempts=85`,
`reservations=85`, `pending_requests=0`, `active_reservations=0`; peak RSS
18 239 488 / 18 227 200 / 18 255 872 bytes with fd 14 and thread 2 throughout;
24 functional ids `pass` in every run.

**Why the periodic strategy is still rejected.** No phase run clears the M004
§13 gates. In every one of the 11 runs the slowest request carries
`wal_checkpoint_sequence_changed == true` with
`maximum_total_ms_with_checkpoint_sequence_change == maximum_total_ms`, while
requests without a sequence change stay under the 500 ms gate — the tail is
foreground SQLite automatic-checkpoint work performed inside the request's own
`COMMIT`. Correcting the narration does not weaken this: it strengthens the
rejection, because the 60 s / 30 s candidates are now known to have produced
*zero* maintenance activity in the measured window (the cadence does not fire
inside the measured batches, which run 2.2–47.6 s), and the 1 s cadence is known
to have lost all three of its in-window acquisition opportunities to foreground
gate ownership.

## 4. Corrected factual statements (before → after)

Per plan §7, the M004 closure's other quantitative statements were audited
against the committed artifacts. Every row below is a mismatch found in
`plans/closure/persistence/004-status.md`; the historical record is **not**
edited, and this table is the correction.

| # | M004 closure text | Committed artifact fact | Severity of the narration error |
|---|---|---|---|
| 1 | "thirteen accepted physical runs" (§1, §2, §3, §7) | 14 accepted committed artifacts (11 phase + 3 ordinary) | low (census) |
| 2 | "the maintenance tick fires once (or zero times) during the ~3-5 s batch" (§1) | `tick_count_delta == 0` in all three 60s/256 runs; the observed "one" is the pre-batch cumulative `task_quiescence.baseline.checkpoint.tick_count` | high — inverted the actual maintenance activity |
| 3 | "Maintenance tick fired exactly once in each batch" (§4) | 0 in-batch ticks in each 60s/256 run | high |
| 4 | "observes `below_threshold` on its first watermark read" as batch activity (§1, §4) | `baseline.below_threshold == 1` already at window open, `below_threshold_delta == 0`; the observation predates the batch | high |
| 5 | §4 slowest-five row for `256-60s-run-1.json` (seq 9 / 49 / 29 at 768 / 732 / 772 ms with publication commits 651 564 / 632 986 / 701 447 µs; seq 26 / 52) | actual slowest five: seq 29 (1709 ms, publication COMMIT 1 615 805 µs), seq 49 (1388 ms, 805 362 µs), seq 9 (662 ms, 600 130 µs), seq 34 (9 ms), seq 10 (8 ms) — none of the cited µs values appear in the artifact | high — misattributed the gate-failing request |
| 6 | §3/§5 "Maintenance outcome (final) … 1 below, 0 ckpt, 0 busy, 0 fail" for the 60s/256, 60s/128, 60s/64 and 30s/64 rows | those are cumulative `final` snapshots; in-batch is 0/0/0/0 with 0 ticks | high |
| 7 | §3/§5 "1s/64: max 1 890 ms with 2 `checkpointed`, 1 `below_threshold`, 3 `gate_busy` … maintenance fires six times in the batch … fires twice as PASSIVE" | in-batch: 3 ticks, 3 `gate_busy`, 0 `checkpointed`, 0 `below_threshold`, 0 `not_due`. Cumulative baseline already held `below_threshold` 1 and `checkpointed` 2; tick counts are 5 baseline → 8 final, so "six" matches no recorded field | high — asserted in-batch PASSIVE checkpoints that never ran |
| 8 | §5 "`128-60s-run-1.json` … Batch elapsed 4.4 s" | `batch_elapsed_ms == 26284` (26.3 s) | medium |
| 9 | §7 "gate-wait … p50 1 µs, max ≤ 33 µs across all thirteen runs" | maximum recorded foreground gate-wait is 34 µs (`64-30s-run-1.json`, finalization `maximum_gate_wait_us`); the corpus is 14 artifacts / 11 phase runs | low |
| 10 | §7 "WAL bytes delta is 16-200 KB per request" | recorded nonzero deltas are 20 600, 41 200, 164 800 and 210 120 bytes (≈20–205 KiB); the other recorded deltas are 0 | low |
| 11 | §7 "`pre_provider_ms` ≤ 8 ms for the two non-checkpoint requests, vs. 635-704 ms for the three checkpoint-correlated requests" | the ≤ 8 ms bound holds, but the three correlated requests in `256-60s-run-1.json` are 1619 / 808 / 602 ms | medium |
| 12 | §8 "final-state `wal_bytes_after` ≈ 4 272 472, peak 4 252 472 in 60s/256 run-1" | `wal_bytes_after == 4272472` and the sampled peak is also 4 272 472; 4 252 472 is not a recorded value (the request's `wal_bytes_before` is 4 251 872) | low |
| 13 | §2 target attestation "cpu_frequency_policy: 2400000 kHz → 2100000 kHz" as a single target fact | `cpu_frequency_policy_end` is per-run under the `ondemand` governor and ranges 1 800 000–2 400 000 kHz across the 14 artifacts; 2 100 000 is one run's value | low |
| 14 | §15/§2/§16 M003 "promoted … to `ready`" while the same record states it "requires a new implementation plan with the architecture review gate" | M003's hard evidence dependency is satisfied; implementation readiness requires the architecture review and a registered `003` plan, neither of which exists | lifecycle — corrected in current control surfaces |
| 15 | §14 "New `plans/implementation/persistence/005-event-driven-checkpoint-coordination.md` (or successor)" | 005 is this corrective pass; M003's plan is local number 003 | superseded numbering |

**Why the cumulative counters caused the error.** Each phase artifact records
three separate views: `checkpoint_maintenance.baseline` and `.final` are
cumulative process-lifetime counters sampled at window open and window close,
`checkpoint_maintenance.deltas` is the measured-window change, and
`task_quiescence.deltas.checkpoint.tick_count_delta` is the in-window task-tick
count. The M004 closeout read `final` values and described them as activity
"during the batch". Because the runner samples the baseline after process
startup and warm-up, that snapshot already contains real maintenance history —
one `below_threshold` observation from the watermark read that follows the
warm-up writes, and in the 1s/64 run two `checkpointed` events from earlier
maintenance ticks — so the cumulative view looks like plausible in-batch work
while describing an entirely different time window. No artifact was wrong; the
prose selected the wrong fields. The added guard asserts the
`baseline + delta == final` identity for every counter in every phase artifact
and then asserts that the cumulative narrative *differs* from the measured
window, so the substitution fails loudly with the artifact and field named.

## 5. Verification executed

### Commands run

```bash
uv sync --frozen
uv run pytest tests/tooling/test_persistence_m004_evidence.py -q
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run pytest tests/tooling/ -q -rs
uv run ruff format --check tests/tooling/
uv run ruff check tests/tooling/
git diff --check
git diff --name-only -- rust/
git diff --name-only ba142368..HEAD -- rust/
```

### Results (local host, not CI)

| Command | Result |
|---|---|
| `uv sync --frozen` | 9 packages checked, environment already current |
| `pytest tests/tooling/test_persistence_m004_evidence.py -q` | **17 passed** |
| `pytest tests/tooling/test_qualification_sbc.py -q` | **32 passed** — runner semantics unchanged; the guard is read-only against artifacts |
| `pytest tests/tooling/ -q -rs` | **124 passed, 3 skipped** (host-specific preflight skips in `test_qualification_rootful_linux.py`, `test_release_packaging.py`, `test_service_transition.py`; all pre-existing) |
| `ruff format --check tests/tooling/` | 45 files already formatted |
| `ruff check tests/tooling/` | All checks passed |
| `git diff --check` | clean |
| `git diff --name-only -- rust/` | empty — acceptance met |
| `git diff --name-only ba142368..HEAD -- rust/` | empty — no Rust change since the M004 closure |

Adversarial check (ad hoc, not committed): with `checkpoint_maintenance.deltas`
replaced by the cumulative `final` snapshot and the checkpoint
`tick_count_delta` replaced by the cumulative `final` tick count, all four
delta-sensitive guard tests fail with artifact- and field-specific messages —
for example `64-1s-run-1.json: expected 3 in-batch checkpoint task ticks, found
8` and `128-60s-run-1.json: task_quiescence.deltas.checkpoint.tick_count_delta
is 1, expected 0 checkpoint task ticks in the measured batch`. This is the
substitution the M004 closeout made; the guard rejects it.

Repository text audit over `plans/` and `architecture/` confirmed that no
current (non-historical) document still claims "thirteen accepted" for M004, two
in-batch `checkpointed` events in 1s/64, an in-batch `below_threshold` event in
the 60 s/30 s runs, an M003 implementation-plan number of 005, or M003 as ready
for implementation without a `003` plan. The only remaining occurrences of
those strings are in the immutable `plans/closure/persistence/004-status.md`
and in this corrective plan, which quotes them as the defects being fixed.

**Not run, with justification.** No Rust workspace suite, no `cargo deny`, and
no release build were executed. The pass has zero production Rust diff across
both commits, so no Rust behavior, public surface, dependency graph, or feature
combination can have changed; the M001/M002 baseline suites recorded in
`plans/closure/persistence/001-status.md` and
`plans/closure/persistence/002-status.md` remain the authority for the landed
runtime. Plan §10 makes the Rust rerun conditional on Rust changing, and
plan §14 requires stopping and re-planning rather than broadening verification
if an agent changed Rust.

## 6. Invariant review

- No production Rust source change: `git diff --name-only ba142368..HEAD -- rust/` is empty.
- `CHECKPOINT_POLL_INTERVAL_S = 60.0`, `CheckpointMaintenancePolicy::DEFAULT_SOFT_WAL_FRAMES = 256`, and the ordinary `wal_autocheckpoint=1000` fallback are unchanged; the guard additionally pins `benchmark.effective.wal_autocheckpoint_pages == 1000` and `wal_autocheckpoint_override_pages is None` in every phase artifact.
- One SQLite connection / gate / worker: untouched; no new connection, worker, or task.
- WAL/NORMAL: untouched; asserted in the guard for every phase artifact.
- Publication/finalization ownership: untouched.
- Backup, restore, restart, reload, and shutdown behavior: untouched; the committed ordinary-run functional ids (`backup-recover-isolated`, `rehash-runtime-status`, `restart-reconcile`, `graceful-shutdown`) stay green in the corpus the guard re-verifies.
- No schema, Config, CLI, HTTP, wire, public Rust API, dependency, or ordinary runtime diagnostic change.
- Qualification-only diagnostics remain feature-gated and absent from ordinary builds; the guard asserts the ordinary benchmark runs used the ordinary release candidate while the 1s/64 stress run used the `qualification-db-diagnostics` build.
- M002 and routing-selection M001 stay closed; legacy Plans 239/240/242 untouched.
- Historical Plan 239 is not rerun or reinterpreted as substitute M004 evidence.

## 7. Failure, cancellation, restart, and contention semantics

No runtime path changed, so no new cancellation or restart semantics exist. The
contention distinction must survive the correction, and M005 sharpens it:

- `gate_busy_delta` belongs to the **optional maintenance attempt**: the
  foreground already owned the single database gate when the maintenance task
  tried to acquire it. It is **not** evidence that a foreground request waited
  behind a maintenance checkpoint.
- The 1s/64 batch therefore shows the timer-driven maintenance task losing
  three in-window acquisition opportunities to foreground ownership, with
  `try_acquire` deferral behaving correctly.
- Because `checkpointed_delta == 0` across the entire corpus, **no claim may be
  made** from these artifacts about the effect of an in-batch PASSIVE
  maintenance checkpoint on foreground latency. The M004 narration implied the
  opposite; that implication is withdrawn.
- Foreground gate-wait stays small everywhere (p50 1 µs; maximum 34 µs), so the
  three latency owners remain separated: foreground transaction work,
  foreground waiting behind maintenance (not observed), and SQLite
  automatic-checkpoint work inside foreground `COMMIT` (the observed tail).
- M003's future design must address this contention evidence. M005 designs
  nothing.

## 8. Migration and compatibility review

No migration. No schema, config, CLI, HTTP, wire, or public Rust compatibility
change. The committed M004 JSON artifacts remain byte-for-byte evidence — this
pass only reads them. `plans/closure/persistence/004-status.md` stays immutable
as historical evidence; this record is the current authoritative correction for
the specific evidence and lifecycle statements enumerated above, and it
supersedes only the erroneous interpretations, not the rejection outcome.

## 9. Security review

No auth, secret, privilege, or DoS surface was touched (zero Rust diff). The
guard reads only committed evidence and asserts scalar counters, statuses, and
environment attestation strings. No credentials, prompts, raw bodies, SQL,
provider/model/account names, database paths, or cache keys are introduced
into the test, the closure, or any corrected document. No cadence, threshold,
counter, or task changed, so no resource bound moved.

## 10. Documentation and operations

Corrected current-state surfaces (in `be370a61`):

- `plans/subsystems/persistence-roadmap.md`: §4 current state (14 accepted artifacts; measured-window deltas; M005 as the correction layer; M003 `proposed`), §6 dependency graph, §7 M003 status/dependencies/deliverable boundary with local number 003 reserved, §7 M004 closure note, §12 milestone status table.
- `plans/registry.md`: persistence roadmap row, dependency-ready table, M004 recently-closed row, M004 and M005 unblock-audit paragraphs, M005 registration paragraph.
- `architecture/deep-dive-database.md`: M001/M004 disposition paragraph and the physical-qualification disposition paragraph.
- `architecture/deep-dive-background.md`: checkpoint task paragraph.
- `plans/implementation/persistence/005-...md`: status line only.

Added by this closure commit: `plans/closure/persistence/005-status.md`.

Deliberately unchanged: `plans/closure/persistence/004-status.md`,
`plans/closure/persistence/001-status.md`,
`plans/closure/persistence/002-status.md`,
`plans/implementation/persistence/004-...md`, and legacy Plans 239/240/242.
Operator-facing behavior is unchanged, so no operational runbook, configuration,
or release change is required.

Environment note (not a repository change): the local `uv` 0.11.32 rewrites
`uv.lock`'s `requires-python` from `>=3.11` to `>=3.12` when
`tests/tooling/test_release_docs.py` shells out to a bare `uv run`. That is a
pre-existing local-tooling artifact unrelated to M005; `uv.lock` is not part of
this pass and was left unmodified.

## 11. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| resolved (high) | M004 narrated cumulative counters as in-batch maintenance activity | current docs overstated maintenance behavior and misattributed the gate-failing request | corrected in current control surfaces; guarded by `tests/tooling/test_persistence_m004_evidence.py`; historical record preserved |
| resolved (lifecycle) | M003 labelled `ready` while the same documents forbade implementation without a plan | risk of a handoff that skipped the architecture review | M003 `proposed`; `003` reserved; promotion requires a separate registration commit |
| resolved (census) | 13 accepted artifacts claimed; 14 committed | minor miscount in current docs | corrected and machine-pinned |
| low | M003 still has no implementation plan; its event-driven design is undesigned | periodic strategy remains rejected on the target class; the multi-second foreground `COMMIT` tail persists by design | a separate planning action: architecture review, then `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md` |
| low | `--benchmark-samples 30` still cannot complete on the ordinary release candidate because the runner's 5 s per-request HTTP timeout is exceeded by the residual tail | unchanged from M004; the `--benchmark-samples 10` corpus is complete for convergence, lifecycle, and convergence evidence | no code change; retained as target-class evidence of the rejected strategy |
| low | Per-request `commit_us` exists only for the slowest five requests per run, so batch-wide foreground-`COMMIT` distributions cannot be recomputed | limits re-audit depth for statements about non-slowest requests | if a future milestone needs that distribution, extend the qualification runner's bounded diagnostics; not required for M005 |
| low | Rust workspace suites not re-run in this pass | none — zero Rust diff across both commits; M001/M002 baseline suites remain the authority | re-run only when Rust changes |
| low | `power_thermal_mode_celsius`/frequency end-state values vary per run under the `ondemand` governor | a per-run value must not be quoted as a target constant (row 13 of §4) | keep quoting ranges from artifacts |

No medium-or-higher finding remains. No corrective pass is required beyond M005.

## 12. Roadmap disposition and blocker audit

Persistence M005 is **closed**. Persistence M004 remains closed with its
periodic-strategy rejection intact. Persistence M001 remains conditionally
closed with the mechanism retained as additive-safe; M002 and routing-selection
M001 stay closed.

- **Persistence M003** — `proposed`. Hard evidence dependency satisfied;
  architecture review outstanding; no implementation plan; no agent may
  implement it from the roadmap alone. Local implementation-plan number `003`
  is reserved for
  `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`.
  Nothing is unblocked by M005.
- **Routing-selection M002** — not started, not promoted. Its 64/512/4096-entry
  affinity workload is still unproduced and its soft/evidence gate is
  independent of persistence. Unchanged.
- **Provider-transport M002** — still `blocked` on a published, general-purpose
  upstream Eggfetch typed classification interface. Unchanged.
- **Dependency-ready implementation plans** — empty after this closure. The
  table held only M005 while M005 was active; the M003 event-driven milestone
  must be registered by a separate commit that adds its architecture review and
  `003` plan.

## 13. Registry updates

Applied in the same commit as this record:

- `plans/registry.md`: "Most recently closed" → persistence M005; persistence roadmap row → M005 closed; dependency-ready implementation-plan table emptied; `Recently closed` gains the M005 row above the M004 row; M005 unblock audit records that nothing was unblocked and why M003 stays `proposed`.
- `plans/subsystems/persistence-roadmap.md`: M005 status `active` → `closed` with its closure record; M003 row unchanged at `proposed` with the reserved plan number.
- `plans/implementation/persistence/005-m004-evidence-and-planning-reconciliation-corrective-pass.md`: status `active` → `closed`.
- No closed work is reopened.
