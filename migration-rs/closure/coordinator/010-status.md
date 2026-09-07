# C010 Closure — Crash/Restart Reconciliation and Fault Injection

Status: closed

Implementation commit: [`d1d7f5a2`](https://github.com/eggstack/eggpool/commit/d1d7f5a2)

Plan: [C010 — crash/restart reconciliation and fault injection](../../implementation/coordinator/010-crash-restart-reconciliation-and-fault-injection.md)

Repository baseline: `6adcf606`

## Outcome

C010 proves M7 durable ownership survives process interruption without a
database reset or manual repair. The new `coordinator::reconciliation`
module owns an explicit bounded `CrashReconciler::reconcile_once`
primitive plus the single test-only `CoordinatorFaultInjector` covering
every plan crash boundary. Deterministic fault hooks were added at the
durable finalizer writes, runtime component releases, failed-attempt
terminalization, retained supervisor registration/completion, provider-send
start/header receipt, and reconciler write boundaries; publication
write/commit/conversion hooks already existed via
`PublicationFaultInjector`. `rust/tests/coordinator_c010.rs` passes 20/20
and the full Rust run passes 288 tests with no failures.

Reconciliation freezes the exact Python `_crash_recovery` policy from
`src/eggpool/app.py`: every `pending` request becomes `interrupted`,
every `active` reservation is released with `crash_recovery`, and every
open attempt completes with `process_interrupted`. No time gate, no new
attempts or reservations, no cost/token/byte mutation, no body access, and
no M5 count/quota/probe hydration. M8 can later schedule the primitive
without changing its semantics; no scheduler, WAL parser, or recovery
framework was introduced.

## Requirement-to-evidence matrix

| C010 requirement | Evidence | Result |
|---|---|---|
| Bounded `reconcile_once` over nonterminal rows only | `bounded_scans_never_exceed_the_configured_limit_per_pass` (batch 2 fixes exactly 2+2+2, drain totals 18, `ReconciliationConfig` clamping) | Pass |
| Indexed/bounded queries, no raw body access | Classification/fix use `requests(status)`, `reservations(status)`, ordered-`id` `LIMIT` scans selecting only ids/status labels; `reconciliation_report_carries_no_bodies_or_secrets` proves the report never contains diagnostic text | Pass |
| Request nonterminal with no attempt | `request_without_attempt_converges_to_interrupted` (bare pending request → `interrupted`) | Pass |
| Attempt nonterminal with active reservation | `open_attempt_with_active_reservation_terminalizes_both` (1+1+1 converged with explicit reasons) | Pass |
| Attempt terminal with active reservation | `terminal_attempt_with_active_reservation_releases_only_the_reservation` (reservation released, terminal attempt untouched) | Pass |
| Request terminal with nonterminal attempt/reservation | `terminal_request_with_open_attempt_converges_without_touching_the_request` (request stays `completed`, leftovers converge) | Pass |
| Interrupted post-commit publication | `post_commit_interruption_converges_through_reconciliation` (retained `PublicationStage::AfterCommit` identity converges; second pass is a noop) | Pass |
| Failed-attempt cleanup pending, request retryable | `failed_attempt_cleanup_pending_fails_closed_to_interrupted` (fail closed to `interrupted`, never replayed) | Pass |
| Terminal request with released/expired reservation | `converged_terminal_state_is_a_noop` (zero fixes, row counts stable) | Pass |
| Stale duplicate terminal command evidence | `stale_duplicate_terminal_evidence_needs_no_durable_write` (supervisor shares the job; reconciler is a noop) | Pass |
| Freeze Python behavior; fail closed; explicit release reason | Exact `_crash_recovery` SQL frozen (`interrupted` / `crash_recovery` / `process_interrupted`); no ADR needed because no user-visible history changes | Pass |
| Never replay unknown in-flight; never double-charge | `reconciliation_never_double_charges_usage_or_replays_attempts` (tokens/cost preserved, counts stable at 1/1/1) | Pass |
| Fault hooks before/after every listed boundary | `coordinator_fault_injector_covers_every_named_crash_point` (all 36 points fail-once + barrier rendezvous); `PublicationFaultInjector` covers publication writes/commit/conversion (existing C002 matrix) | Pass |
| Finalizer write/release faults converge | `finalizer_faults_leave_valid_durable_state_for_reconciliation` (6 points: write before/after, release before/after, failed-attempt terminalization before/after) | Pass |
| Supervisor registration/completion faults stay bounded | `supervisor_registration_and_completion_faults_stay_bounded` (4 points; zero leaked jobs) | Pass |
| Reconciler fault fails closed before any write | `reconciler_fault_hook_fails_closed_before_any_write` (nonterminal counts unchanged, clean pass converges) | Pass |
| Publication barrier crash converges after restart | `publication_barrier_crash_still_converges_after_restart` (abort-while-parked → zero nonterminal rows) | Pass |
| Fresh restart over the same DB | `restart_over_the_same_db_converges_and_stays_python_readable` (close, reopen, migrate idempotently, `quick_check`, reconcile 6 rows, second pass converged) | Pass |
| Python rollback readability | Same restart test shells to `python3 + sqlite3` and asserts `interrupted` / `crash_recovery` / `process_interrupted` group-bys | Pass |
| Repeated reconciliation idempotent, no fanout | `repeated_reconciliation_is_idempotent_without_row_fanout` (first pass fixes 9, three repeats fix 0, counts stable 3/3/3) | Pass |
| Concurrent reconciliation converges, queues bounded | `concurrent_reconciliation_converges_without_new_rows` (8 workers fix exactly 12 total; supervisor jobs stay 0) | Pass |
| No stale count/quota/probe reconstruction | `fresh_process_state_is_not_hydrated_from_durable_rows` (fresh router/estimator stay at zero after reconciliation) | Pass |
| No scheduler/WAL parser/framework | No new dependency (`Cargo.lock` untouched); only `tokio`/`rusqlite` primitives; reconciler exposes no background task | Pass |

## Failing-before / passing-after evidence

Before the implementation there was no restart reconciler: the only
`reconcile_once` was the supervisor job-count snapshot, publication
post-commit interruptions required explicit compensation by the crashing
caller, and no test restarted fresh Rust state over the same database
file. A crashed publication left `pending`/`active`/open rows with no
converging primitive; `rust/tests/coordinator_c010.rs` did not exist.

After implementation, `rust/tests/coordinator_c010.rs` passes 20/20 and
the full Rust run passes 288 tests across all targets with no failures
(268 pre-existing + 20 new), `tests/migration_rs` passes 83 with 3
skipped, the targeted Python retry/finalization/stream suites pass 238,
`tests/smoke/` passes 14, `pyright src/ scripts/` reports 0 errors, and
ruff format/check pass on all 728 files.

No implementation-review defect required a regression fix beyond the one
caught by the new suite itself: the initial finalizer-fault test armed
failed-attempt terminalization points against the terminal-request path,
which correctly did not fire; the test now routes those two points
through `finalize_failed_attempt`, matching the hook contract.

## Verification commands actually run

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets                         # 288 passed, 0 failed
cargo test --test coordinator_c010               # 20 passed, 0 failed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
uv run pytest tests/unit/test_retry_classification.py tests/unit/test_backoff.py tests/unit/test_failure_effects_table.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_accepted_finalization_state_machine.py tests/unit/test_stream_completion.py tests/unit/test_stream_diagnostics.py -q --tb=short --maxfail=1  # 238 passed
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
uv run pyright src/ scripts/                     # 0 errors, 0 warnings
uv run ruff format --check src/ tests/ scripts/  # 728 files formatted
uv run ruff check src/ tests/ scripts/
git diff --check
```

No live/paid provider, external network, schema migration, Eggress
feature change, or Python fixture change was required. `Cargo.lock` is
untouched; no dependency was added.

## Security, contention, restart, and resource review

No credential, request body, provider body, prompt text, selector
response, session header, or diagnostic prose enters the reconciler
report, the fault injector, or any coordinator `Debug` beyond lengths
and counts. Classification selects only ids and status labels; the
secret-bearing `error_detail` test row never appears in report output.
Attempt error classes are overwritten with the frozen
`process_interrupted` marker exactly as Python does; cost, token, and
byte columns are never written by reconciliation.

Every fixing update re-guards on its nonterminal predicate
(`status = 'pending'`, `status = 'active'`, `completed_at IS NULL`) with
an ordered-`id` `LIMIT` subselect, so concurrent reconcilers converge
without double-fixing and bounded passes drain without growth. The
bounded test proves per-pass limits; the concurrency test proves exactly
one pass worth of work across 8 workers; the idempotency test proves
repeats fix zero rows with stable 3/3/3 counts. Supervisor job tables
stay at zero active jobs; wire-resolver flights, effect ledgers, and
retained jobs are untouched process-local state that correctly restarts
empty. Cancellation of a parked publication worker compensates or
commits atomically and reconciles to zero nonterminal rows. No schema
fork, no second HTTP stack, no scheduler, and no M8 lifecycle change
were introduced.

## Supported differences from the Python oracle

- Python `_crash_recovery` also records per-account `crash_recovery`
  events plus one operational summary event in the same transaction. Rust
  reconciliation writes no audit rows: durable lifecycle convergence is
  the frozen contract, and event observability stays owned by the Python
  runtime until M8 wires generation lifecycle. Reconciliation reports
  carry the same counts an operator would read from those events.
- Python runs recovery implicitly at startup; Rust exposes it as the
  explicit `CrashReconciler::reconcile_once` primitive with no
  scheduling. M8 owns when it runs.
- The bounded batch (default 500, ceiling 5000) has no Python
  equivalent: Python sweeps all rows in one transaction. Bounded passes
  drain to the identical terminal state, as the bounded test proves by
  looping to convergence.

## Registry transition and future-plan audit

C010 moves from the dependency-ready table to completed implementation
plans with commit `d1d7f5a2` and this accepted closure record. C011 has
C010 as its sole hard dependency, so C011 is promoted to the
dependency-ready table as the sole ready plan. No other future plan is
unblocked: C011 aggregates the full M7 qualification and requires C010's
reconciliation primitives, and M8 remains blocked on accepted C011 M7
closure plus its separate planning review.

Unresolved mandatory findings within C010 scope: none. C010 does not
replace the aggregate C011 M7 closure and does not claim M8 lifecycle
parity.

Recommendation: **closed**; C011 may proceed, with M8 retaining its
existing serial gate.
