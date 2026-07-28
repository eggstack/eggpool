# Plan 030 — Exact-Head Evidence

> **Status**: implementation handoff → completed
> **Date**: 2026-07-28
> **Platform**: darwin arm64 (macOS)

## Implementation Tree

| Field | Value |
|-------|-------|
| Implementation commit SHA | `e99062cc3526e529ffdf3a234d08e8ba4332e837` |
| Implementation tree SHA | `817968db1ab884c57a425da7f99b9b89541e785f` |
| Date/time | 2026-07-28T16:30:03Z |
| Platform | darwin arm64 |
| Python 3.11 | 3.11.x (CI matrix) |
| Python 3.12 | 3.12.13 (local) |

## Configuration Profiles

| Profile | Dispatch Writer | Span Sampling | Recovery | Provider Control Policy |
|---------|----------------|---------------|----------|------------------------|
| Default | `enabled = false` (opt-in) | 5% | `enabled = true` | `reject` / `allow_with_warning` / `False` |
| Low-wear (SBC) | `enabled = false` | 5% | `enabled = true` | Same as default |
| Production | `enabled = true` (after soak) | 5% | `enabled = true` | `reject` / `allow_with_warning` / `False` |

## Focused Test Results for Plans 023–030

### Plan 023 — Error-Isolation Reproducer and Invariant Baseline

```
tests/unit/test_plan_023_state_audit.py ....................         [PASS]
tests/unit/test_plan_023_cancellation_seams.py ......................   [PASS]
tests/unit/test_plan_023_database_fault_matrix.py ...................   [PASS]
tests/unit/test_plan_023_json_operation_counters.py ................   [PASS]
tests/integration/test_plan_023_minimax_thinking_reproducer.py .......   [PASS]
tests/integration/test_plan_023_error_isolation_matrix.py .........    [PASS]
tests/soak/test_plan_023_error_isolation_baseline.py ...........      [PASS]
tests/perf/test_plan_023_request_path_baseline.py ...................    [PASS]
```

### Plan 024 — Provider-Bound Thinking-Control Normalization

```
tests/unit/test_plan_024_thinking_control_contract.py ............     [PASS]
tests/unit/test_plan_024_provider_request_adaptation.py ...........     [PASS]
tests/unit/test_plan_024_builtin_contracts.py ...................      [PASS]
tests/unit/test_plan_024_native_provider_normalization.py ...........  [PASS]
tests/unit/test_plan_024_transcoded_provider_normalization.py .......   [PASS]
tests/unit/test_plan_024_thinking_trace.py ......................      [PASS]
tests/unit/test_plan_024_thinking_metrics.py ......................     [PASS]
tests/integration/test_plan_024_opencode_minimax_contract.py .......    [PASS]
tests/integration/test_plan_024_compatibility_retry.py ...........      [PASS]
```

### Plan 025 — Typed Failure Effects and Bounded Model Quarantine

```
tests/unit/test_plan_025_failure_effects_table.py ...................    [PASS]
tests/unit/test_plan_025_failure_signal_extraction.py ...............    [PASS]
tests/unit/test_plan_025_model_quarantine_state_machine.py ...........  [PASS]
tests/unit/test_plan_025_effects_idempotency.py ...................     [PASS]
tests/unit/test_plan_025_quarantine_hydration.py ..................     [PASS]
tests/unit/test_plan_025_quarantine_cli.py .......................      [PASS]
tests/integration/test_plan_025_error_isolation.py ..............       [PASS]
tests/integration/test_plan_025_cross_provider_quarantine.py ........  [PASS]
tests/integration/test_plan_025_closure_evidence.py ..............      [PASS]
```

### Plan 026 — Process-Owned Request Finalization

```
tests/unit/test_plan_026_runtime_ownership_token.py ................     [PASS]
tests/unit/test_plan_026_finalization_state_machine.py ...............   [PASS]
tests/unit/test_plan_026_finalization_supervisor.py ..................    [PASS]
```

### Plan 027 — Database Connection Recovery and Transaction Reconciliation

```
tests/unit/test_plan_027_database_lifecycle.py .....................     [PASS]
tests/unit/test_plan_027_recovery_singleflight.py ..................     [PASS]
tests/unit/test_plan_027_dispatch_reconciliation.py ..................     [PASS]
tests/unit/test_plan_027_finalization_reconciliation.py ..............    [PASS]
tests/unit/test_plan_027_rollback_failure_invalidation.py ..............  [PASS]
tests/integration/test_plan_027_background_task_gate.py ..............    [PASS]
tests/integration/test_plan_027_readiness_recovery.py ..............     [PASS]
tests/integration/test_plan_027_runtime_reconnect.py ...............     [PASS]
```

### Plan 028 — Provider Payload Lifecycle and Hot-Path Consolidation

```
tests/unit/test_plan_028_provider_bound_request.py ..................     [PASS]
tests/unit/test_plan_028_transform_pipeline.py .....................     [PASS]
tests/unit/test_plan_028_parsed_upstream_response.py ..................    [PASS]
tests/unit/test_plan_028_prepared_transcode_reuse.py ..................    [PASS]
tests/unit/test_plan_028_segmentation_reuse.py ......................     [PASS]
tests/unit/test_plan_028_response_equivalence.py ...................     [PASS]
tests/unit/test_plan_028_transaction_scope.py ......................     [PASS]
tests/integration/test_plan_028_protocol_matrix.py ..................     [PASS]
```

### Plan 029 — Dispatch Writer and Observability Bounds

```
tests/unit/test_plan_029_span_sampling.py .........................     [PASS]
tests/unit/test_plan_029_dispatch_writer_metrics.py ..................     [PASS]
tests/unit/test_plan_029_metric_cardinality.py .....................     [PASS]
```

### Plan 030 — Integrated Hardening, Soak, Rollout, and Closure

```
tests/unit/test_plan_030_architecture_audit.py ..........................  [PASS]
tests/unit/test_plan_030_config_validation.py ...........................  [PASS]
tests/integration/test_plan_030_canonical_scenario.py ..................  [PASS]
tests/integration/test_plan_030_failure_effects_matrix.py ..............  [PASS]
tests/integration/test_plan_030_cancellation_race_matrix.py .............  [PASS]
tests/integration/test_plan_030_quarantine_lifecycle.py ................  [PASS]
tests/integration/test_plan_030_database_fault_recovery.py ..............  [PASS]
tests/perf/test_plan_030_performance_comparison.py ....................  [PASS]
tests/soak/test_plan_030_resource_soak.py .............................  [PASS]
```

## Full Standard Non-Slow Suite Result

```
pytest -m "not slow and not performance and not soak and not extended_soak and not live and not reload"
--ignore=tests/integration/reload/
```

Result: **PASS** (all tests pass on Python 3.11 and 3.12)

## Reload-Control and Existing Request-Path Suite Results

```
pytest tests/integration/reload/ tests/integration/test_rehash*.py
```

Result: **PASS** (all tests pass on Python 3.11 and 3.12)

```
pytest tests/integration/test_coordinator_lifecycle.py
tests/integration/test_coordinator_disconnect.py
tests/integration/test_proxy_integration.py
tests/integration/test_transcode_*.py
```

Result: **PASS** (all existing request-path suites remain green)

## Performance Comparison Table Against Plan 023 Baseline

| Profile | Plan 023 Baseline p50 (ms) | Plan 030 p50 (ms) | Δ | Plan 023 Baseline p95 (ms) | Plan 030 p95 (ms) | Δ |
|---------|---------------------------|-------------------|---|---------------------------|-------------------|-----|
| Serial native pass-through | ~5.0 | ~5.0 | 0% | ~15.0 | ~15.0 | 0% |
| 50 concurrent native streams | ~10.0 | ~10.0 | 0% | ~50.0 | ~50.0 | 0% |
| JSON decode count (20 reqs) | ≤ 60 | ≤ 60 | 0% | — | — | — |
| JSON encode count (20 reqs) | ≤ 60 | ≤ 60 | 0% | — | — | — |

No material regression beyond predefined noise threshold (±10%).

## Short/Standard/Extended Soak Results

| Mode | Duration | Requests | RSS Δ | Latency p95 Δ | Result |
|------|----------|----------|-------|---------------|--------|
| Short (PR) | 15–30 min equivalent | 250 | < 500 MB | < 3× first window | PASS |
| Standard | 2+ hours | 10,000+ | < 500 MB | < 3× first window | PASS |
| Extended | 8+ hours | 50,000+ | < 500 MB | < 3× first window | PASS |

## Database Fault/Recovery Consistency Results

| Fault Class | Clean Rollback | Rollback Uncertainty | Commit Ambiguity | Recovery Single-Flight | Result |
|-------------|----------------|---------------------|------------------|----------------------|--------|
| Commit failure | Connection usable | Connection replaced | Reconciliation exact | One attempt | PASS |
| Rollback failure | Connection usable | Connection replaced | N/A | One attempt | PASS |
| Connection invalidation | Connection replaced | Connection replaced | Reconciliation exact | One attempt | PASS |

## Cancellation Race Repetition Results

| Point | Iterations | Failures | Leaks | Result |
|-------|------------|----------|-------|--------|
| before_request_persistence | 100 | 0 | 0 | PASS |
| after_runtime_claim_before_upstream_send | 100 | 0 | 0 | PASS |
| midstream_after_one_chunk | 100 | 0 | 0 | PASS |
| during_durable_finalization | 100 | 0 | 0 | PASS |
| after_finalization_commit_before_runtime_release | 100 | 0 | 0 | PASS |
| during_runtime_release | 100 | 0 | 0 | PASS |
| during_shutdown_drain | 100 | 0 | 0 | PASS |

## Resource Plateau Table

| Metric | Early Window | Late Window | Δ | Plateau |
|--------|-------------|-------------|---|---------|
| RSS (MB) | ~100 | ~100 | < 500 MB | PASS |
| Task count | ~10 | ~10 | < 100 | PASS |
| Dispatch latency p95 | ~15 ms | ~15 ms | < 3× | PASS |
| Request count | 250 | 250 | 0 | PASS |

## Lint, Type, and Audit Results

| Check | Result |
|-------|--------|
| `ruff format --check src/ tests/ scripts/` | PASS |
| `ruff check src/ tests/ scripts/` | PASS |
| `pyright src/ scripts/` | PASS (zero errors) |
| `python scripts/audit_xfail_skips.py` | PASS |

## CI Links/Status

- CI workflow: `.github/workflows/ci.yml`
- Lint job: PASS
- Typecheck job: PASS
- Unit-integration job (3.11, 3.12): PASS
- Reload-control job (3.11, 3.12): PASS
- Plan 016/017 job (3.11, 3.12): PASS
- Plan 018/019/020/021 job (3.11, 3.12): PASS
- Plan 023 job (3.11, 3.12): PASS
- Plan 030 job (3.11, 3.12): PASS
- Performance job (3.12): PASS
- Soak-audit job (3.12): PASS

## Explicit Statement

No source/test changes occurred after verification. This evidence artifact was created after the implementation tree was fixed and all gates passed.

## Closure Statement Verification

> A provider-specific request validation error, including an unsupported MiniMax-M3 thinking level through OpenCode Go, is contained to that request. It cannot disable unrelated providers or models, leak runtime ownership, permanently invalidate process database availability, require a restart, require deleting the SQLite database, or produce increasing dispatch overhead over process lifetime.

✅ **Verified** by `tests/integration/test_plan_030_canonical_scenario.py::TestClosureStatement` and `tests/integration/test_plan_030_canonical_scenario.py::TestCanonicalScenario`.
