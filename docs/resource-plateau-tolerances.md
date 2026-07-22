# Resource Plateau Tolerances

Defines the acceptance criteria for resource plateau validation in CI
soak tests and extended soak workflows. These tolerances are enforced
by `tests/soak/test_resource_plateau.py` and
`tests/soak/test_extended_stability_gates.py`.

## Design Principles

1. **Exact return to baseline** for tasks, clients, and retiring
   generations after quiescence.
2. **Bounded warm-up delta** for file descriptors — no positive slope
   in late windows.
3. **Bounded RSS growth slope** — may plateau above startup due to
   allocator behavior, but late-window growth must be flat.
4. **Writer queue must drain** after load stops.
5. **No unobserved task exception** is permitted.

## Per-Resource Tolerances

### Threads
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| Max thread count | `initial + 20` | `test_resource_plateau.py::test_thread_count_plateau` |
| Plateau across 3 cycles | No monotonic increase | `test_resource_plateau.py::test_thread_count_plateau` |

### RSS (Resident Set Size)
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| Growth ratio (first to last cycle) | `< 1.5x` | `test_resource_plateau.py::test_memory_not_growing_unboundedly` |
| RSS slope (late window) | `<= 1 MB/req` | `test_extended_stability_gates.py::evaluate_gates` |
| PR soak growth | `< 50%` from start | `test_pr_soak.py` |

### Asyncio Tasks
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| Final count vs initial | `<= initial + 2` | `test_resource_plateau.py::test_asyncio_task_count_plateau` |
| After quiescence | Exact return to baseline | `test_resource_plateau.py::test_asyncio_task_count_plateau` |

### File Descriptors
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| Late-window slope | `<= warmup_delta + 5` | `test_resource_plateau.py::test_file_descriptor_plateau` |
| Extended soak ratio | No positive slope in late window | `test_extended_stability_gates.py` |

### Reservations
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| Active after quiescence | Exactly 0 | `test_resource_plateau.py::test_reservations_cleaned_after_workload` |
| PR soak after drain | Exactly 0 | `test_pr_soak.py` |

### Pending Requests
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| After drain | Exactly 0 | `test_pr_soak.py` |

### Writer Queue
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| After load stops | Drained to 0 | `test_extended_stability_gates.py::check_absolute_invariants` |

### Routing Trace Queue
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| After load stops | Drained to 0 | `test_extended_stability_gates.py::check_absolute_invariants` |

### Finalization Retry Queue
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| After load stops | Drained to 0 | `test_extended_stability_gates.py::check_absolute_invariants` |

### Health Slots
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| After quiescence | No leaked slots | `test_extended_stability_gates.py::check_absolute_invariants` |

### Generations
| Metric | Tolerance | Enforcement |
|--------|-----------|-------------|
| Retiring count after quiescence | 0 (closed within timeout) | `test_extended_stability_gates.py::check_absolute_invariants` |

## Extended Soak Relative Gates

These gates compare early-window metrics to late-window metrics.
Ratios are bounded so regressions are caught even when absolute
numbers shift across environments.

| Metric | Early/Late Ratio Limit | Floor |
|--------|----------------------|-------|
| Dispatch overhead p95 | `<= 1.20x` | 0.01 ms |
| Dispatch overhead p99 | `<= 1.50x` | 0.01 ms |
| Local pre-upstream p95 | `<= 1.20x` | 0.01 ms |
| DB lock wait p95 | `<= 1.25x` | 0.01 ms |
| Event loop lag p95 | `<= 1.25x` | 0.01 ms |
| Throughput decline | `<= 10%` | — |

Values below `TRIVIAL_FLOOR_MS` (0.01 ms) skip the ratio gate to
avoid false positives on trivially small measurements.

## Consistency Audit (Post-Soak)

After each soak, `ConsistencyAuditor.run_full_audit()` must report
zero violations. Checks include:

- Pending requests with no active attempt
- Active reservations on non-pending requests
- Incomplete attempts on terminal requests
- Duplicate attempt numbers
- Orphan routing traces
- Orphan account backoffs
- Stuck reservations (active > 1 hour)
- Attempt ordering violations
- Orphan price snapshots
