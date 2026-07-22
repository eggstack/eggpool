# Phase 12 Handoff Evidence

Date: 2026-07-22
Status: Implementation complete

## CI Job/Marker Matrix

| CI Job | Markers | Python | Command |
|--------|---------|--------|---------|
| lint | — | 3.12 | `ruff format --check` + `ruff check` |
| typecheck | — | 3.12 | `pyright src/ scripts/` |
| unit-integration | `not slow and not performance and not soak and not extended_soak and not live` | 3.11, 3.12 | `pytest -m "..."` |
| reload-control | `reload` | 3.11, 3.12 | `pytest tests/integration/reload/` |
| performance | `performance` | 3.12 | `pytest -m performance` |
| soak-audit | `soak` | 3.12 | `pytest -m soak` + `audit_xfail_skips.py` |

## Removed Skips/XFails

No xfails or skips were removed. The 4 existing exemptions are documented in the audit allowlist with rationale:

1. `test_d3_concurrent_reload_burst_rejects_busy` (xfail) — subprocess race; strict coverage via `test_concurrent_reload_returns_busy_immediately` and `test_concurrent_reloads_one_busy`
2. `test_d3_operator_concurrent_busy` (xfail) — same rationale as above
3. `test_d3_retirement_timeout_closes_resources` (skip) — drain timeout exceeds CI budget; covered by scenario 9 soft-drain test
4. `test_d3_phase7_xdg_state_home_isolated` (skip) — runtime_paths.state_dir() limitation; requires code change outside this phase

## Fault-Injection Coverage Matrix

| Stage | Fault Type | Test | Result |
|-------|-----------|------|--------|
| on_validation_complete | RECOVERABLE | test_validation_fault_at_on_validation_complete | Generation unchanged |
| on_candidate_started | RECOVERABLE | test_build_failure_at_candidate_started | Generation unchanged |
| on_candidate_complete | RECOVERABLE | test_build_failure_at_candidate_complete | Generation unchanged |
| on_reconcile_started | RECOVERABLE | test_reconcile_fault_at_reconcile_started | Generation unchanged |
| _apply_persistence_delta | OSError | test_persistence_apply_failure_preserves_generation | Generation unchanged |
| _publish_generation | RuntimeError | test_publish_failure_preserves_generation | Generation unchanged |
| _apply_process_transitions | RuntimeError (transient) | test_process_transition_apply_failure_compensates | Generation advanced, compensation succeeded |
| _apply_process_transitions | RuntimeError (persistent) | test_process_transition_persistent_failure_marks_compensation_failed | Generation advanced, compensation failed |
| on_candidate_started | CANCELLATION | test_cancellation_at_candidate_build | Generation unchanged |
| on_reconcile_started | CANCELLATION | test_cancellation_at_reconcile | Generation unchanged |
| on_publish_complete | CANCELLATION | test_cancellation_at_publish_complete_post_publication | Generation advanced (shielded) |
| on_retirement_started | CANCELLATION | test_cancellation_at_retirement | Generation advanced (shielded) |
| Post-publication | Task cancel | test_cancel_after_publish_shields_commit | Generation advanced |
| Concurrent | Busy rejection | test_concurrent_reloads_one_busy | Exactly 1 success, 1 busy |
| Sequential | All succeed | test_reloads_sequential_all_succeed | 3 sequential reloads succeed |
| Full snapshot | Build failure | test_full_snapshot_unchanged_after_build_failure | No state changes |
| Full snapshot | Publish failure | test_full_snapshot_unchanged_after_publish_failure | No state changes |
| Full snapshot | Success | test_generation_advances_on_successful_reload | Generation + digest advanced |

## Consistency Audit Checks (9 total)

1. `check_pending_without_attempt` — pending requests with no completed attempt
2. `check_active_reservation_for_non_pending` — active reservations on non-pending requests
3. `check_incomplete_attempt_for_terminal` — incomplete attempts on terminal requests
4. `check_duplicate_attempt_numbers` — duplicate attempt numbers per request
5. `check_no_orphan_routing_traces` — routing decisions without matching requests (warning)
6. `check_orphan_account_backoffs` — backoff rows without matching accounts
7. `check_stuck_reservations` — active reservations older than 1 hour
8. `check_attempt_ordering` — attempts not starting at 1 (warning)
9. `check_no_orphan_price_snapshots` — price snapshots without matching models (warning)

## Short Soak Summary

PR soak test (`tests/soak/test_pr_soak.py`) — 6 phases, deterministic seed=42:

| Phase | Description | Count |
|-------|-------------|-------|
| 1 | Serial requests (mixed protocols) | 20 |
| 2 | Concurrent streaming burst | 10 |
| 3 | Slow streams (cancellation) | 5 |
| 4 | Mixed load (concurrent) | 15 |
| 5 | Drain | — |
| 6 | Error handling (429) | 5 |

**Resource plateau results:**
- Active reservations after drain: 0
- Pending requests after drain: 0
- Thread count: within `initial + 20`
- RSS growth: < 50% from start
- Consistency audit: all 9 checks passed

## Resource Plateau Tolerances

| Resource | Tolerance | Enforcement |
|----------|-----------|-------------|
| Threads | `<= initial + 20` | test_resource_plateau.py |
| RSS | `< 1.5x` growth ratio | test_resource_plateau.py |
| RSS slope | `<= 1 MB/req` late window | test_extended_stability_gates.py |
| Async tasks | `<= initial + 2` after quiescence | test_resource_plateau.py |
| File descriptors | No positive slope in late window | test_resource_plateau.py |
| Reservations | Exactly 0 after quiescence | test_resource_plateau.py |
| Pending requests | Exactly 0 after drain | test_pr_soak.py |
| Writer queue | Drained after load | test_extended_stability_gates.py |

## Performance Baseline

All performance tests pass:
- `test_perf_baseline.py`: 8 passed (native OpenAI/Anthropic, transcode, segmentation, routing, retry, thinking)
- `test_hot_path_performance.py`: 11 passed (parse caching, padding, ImmutableRequestState, headers, dispatch spans, event loop lag)
- `test_dispatch_baseline.py`: 16 passed (serial, concurrent, writer-enabled, database contention, background tasks, runtime metrics)

## Updated Documentation

- `README.md` — CI partitions table, skip/xfail audit command, test markers
- `AGENTS.md` — Pre-commit checks (5 checks), CI partitions section, marker-based test commands
- `.opencode/skills/development/SKILL.md` — Markers, CI partitions, audit command
- `.opencode/skills/architecture/SKILL.md` — Phase 12 CI/test infrastructure section
- `docs/resource-plateau-tolerances.md` — New tolerances document

## Remaining Risks

1. **Full test suite timeout**: The complete test suite exceeds 10 minutes on this machine. CI partitioning mitigates this by running subsets in parallel.
2. **psutil dependency**: File descriptor plateau test skips when psutil is not installed. CI should install it for the soak job.
3. **Subprocess-based concurrency tests**: The 2 xfail tests for concurrent rehash remain non-strict because subprocess timing is inherently non-deterministic. The invariants are fully covered by single-process strict tests.
4. **XDG state isolation**: The skip for `test_d3_phase7_xdg_state_home_isolated` requires a code change to `runtime_paths.state_dir()` which is outside this phase's scope.
