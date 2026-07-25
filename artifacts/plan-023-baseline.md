# Plan 023 — Baseline Artifact

## Implementation Info

- **Implementation SHA**: `303db104552cd14f283d4c616f04959f71b5c5da`
- **Tree SHA**: `3991c42d63a5b5aa856be0d6464831a057ae2b42`
- **Python**: 3.14.2 (implementation: CPython)
- **Platform**: Darwin (macOS)

## Focused Command Results

### Plan 023 tests: 64 passed, 0 failed

```
tests/unit/test_plan_023_state_audit.py         — 14 passed
tests/unit/test_plan_023_cancellation_seams.py  — 12 passed
tests/unit/test_plan_023_database_fault_matrix.py — 8 passed
tests/unit/test_plan_023_json_operation_counters.py — 11 passed
tests/integration/test_plan_023_minimax_thinking_reproducer.py — 11 passed
tests/integration/test_plan_023_error_isolation_matrix.py — 6 passed
tests/soak/test_plan_023_error_isolation_baseline.py — 3 passed
tests/perf/test_plan_023_request_path_baseline.py — 3 passed (performance marker)
```

### Lint checks

- `ruff format --check src/ tests/ scripts/` — PASS (680 files formatted)
- `ruff check src/ tests/ scripts/` — PASS (0 issues)
- `pyright src/ scripts/` — PASS (0 errors, 0 warnings)
- `audit_xfail_skips.py` — PASS

## Failure-State Mutation Table

| Scenario | Status Code | Health Change | DB Rows Created | Reservation Leak |
|----------|-------------|---------------|-----------------|------------------|
| No thinking success | 200 | none | request + attempt + reservation | none |
| Accepted thinking success | 200 | none | request + attempt + reservation | none |
| Unsupported thinking 400 | 400 | none | request + attempt + reservation | none |
| Unsupported thinking 422 | 422 | none | request + attempt + reservation | none |
| Misleading 404 | 404 | none | request + attempt + reservation | none |
| Error then unrelated success | 400→200 | none | 2x request + 2x attempt + 2x reservation | none |
| Error then minimax success | 400→200 | none | 2x request + 2x attempt + 2x reservation | none |
| Streaming rejected | 400 | none | request + attempt + reservation | none |
| Connection drop | 200 (incomplete) | none | request + attempt + reservation | none |

## Parse/Encode Counts

JSON operation counters instrument `eggpool.jsonx.loads` and `dumps_bytes`.
Baseline measurements captured in test_plan_023_json_operation_counters.py:

- `request_decode`: counted per `jsonx.loads` call in request context
- `request_encode`: counted per `jsonx.dumps_bytes` call in request context
- `response_decode`: counted per `jsonx.loads` call in response context
- `response_encode`: counted per `jsonx.dumps_bytes` call in response context
- `stream_event_decode`: counted per `jsonx.loads` call in stream context
- `stream_event_encode`: counted per `jsonx.dumps_bytes` call in stream context

## Latency and Resource Baselines

Captured in `test_plan_023_request_path_baseline.py`:

- Serial native non-stream: p50 < 5s (respx-mocked upstream)
- Serial native stream: p50 < 5s
- 50 concurrent native streams: wall time < 30s
- Process metrics: asyncio task count > 0

## Known Nondeterminism

- `connection_drop_after_headers` scenario: httpx may raise or return an
  incomplete response depending on timing. Both outcomes are accepted.
- Concurrent test ordering: sequence-based rules may not match all concurrent
  requests perfectly; assertions check aggregate counts rather than ordering.
- Resource plateau test: thread count may vary by a small margin (≤5).
