# EggPool Performance Final Verification Handoff Plan

## Purpose

This is a narrow final handoff plan for the performance optimization line. The implementation now appears largely closed: prepared transcode dispatch data is frozen, mutable diagnostics are separated, segmentation `not_collected` is persisted and surfaced, trace-mode docs were corrected, and routine proxy logging was moved to DEBUG.

The remaining handoff work is not another feature pass. It is verification and small documentation/code alignment for the last unconfirmed items:

1. Prove the documented local verification commands pass, or record exact existing failures.
2. Confirm CI/status visibility, or explicitly document that verification is local-only.
3. Verify the Phase 5 middleware claim, or de-scope/correct the documentation if `BaseHTTPMiddleware` replacement has not landed.
4. Confirm no stale docs still overstate the performance closure.

## Current state to preserve

Preserve the following behavior unless a failing test proves otherwise:

- `PreparedTranscode` is frozen and recursively freezes translated payload/warnings.
- Only `PreparedTranscodeDiagnostics` mutates after construction.
- Coordinator reuse path converts frozen warning mappings back to plain dictionaries before appending to `loss_warnings`.
- Transcode preflight uses one compact encoded body for dispatch and a separate padded body only for context-limit estimation.
- Segmentation gating reads the effective resolved compression policy.
- `segmentation_not_collected=True` finalizes as `segmentation_status = 'not_collected'`.
- Missing segmentation without the not-collected flag finalizes as `empty_request`.
- Canonical segmentation stats count `not_collected` separately in total, provider/protocol, and model buckets.
- API JSON serializes tuple provider/protocol keys as string labels.
- Routing trace modes are only `all`, `sampled`, and `off`.
- Routine proxying log is DEBUG, not INFO.
- Routing remains load-based and never consumes cache/compression/synthetic-cache metrics.

## Non-goals

- Do not add new performance optimizations.
- Do not add or reintroduce `routing.trace.mode = "errors"`.
- Do not change request routing behavior.
- Do not alter compression or synthetic-cache behavior.
- Do not change cost/pricing/finalization semantics.
- Do not redesign the dashboard.
- Do not introduce wall-clock performance thresholds into default CI.

## Item 1: Run and record the documented verification commands

### Tasks

Run the exact commands currently documented for contributors:

```bash
uv sync --extra dev
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
uv run pytest -m request_path -v
uv run pytest -m dashboard -v
uv run pytest -m performance -v
```

Also run a targeted subset around this performance line:

```bash
uv run pytest tests/unit/test_prepared_transcode.py -v
uv run pytest tests/unit/test_request_finalizer.py -k segmentation -v
uv run pytest tests/unit/test_stats.py -k canonical_request_segmentation -v
uv run pytest tests/unit/test_api_phase7.py -k canonical_request_segmentation -v
uv run pytest tests/unit/test_dashboard_cache_page.py -k segmentation -v
```

### Validation points

- No `PytestUnknownMarkWarning` appears.
- `pytest -m performance` actually selects the perf tests.
- `pytest -m request_path` includes prepared transcode, segmentation guard, request finalizer, and routing tests expected by `pyproject.toml` markers.
- `pytest -m dashboard` includes dashboard/cache-page rendering tests.
- Failures, if any, are classified as:
  - introduced by the performance line,
  - pre-existing unrelated failure,
  - environment/config issue,
  - flaky/timing-only perf diagnostic.

### Acceptance criteria

- All commands pass, or a short `plans/verification-results-YYYY-MM-DD.md` file records exact failures, suspected ownership, and next action.
- No undocumented marker names are required to validate this line.

## Item 2: Confirm CI/status visibility

### Tasks

1. Inspect `.github/workflows/`.
2. Confirm whether workflow jobs run on push/PR for:
   - ruff format check,
   - ruff lint,
   - pyright,
   - pytest.
3. Confirm whether GitHub exposes check/status results for the current head of `main`.
4. If CI is absent or intentionally disabled, update `AGENTS.md` and/or `README.md` to say verification is local-only and list the required commands.
5. If CI exists but the connector/status API shows no statuses, document where maintainers should check workflow results.
6. Keep performance tests diagnostic unless a stable no-regression assertion exists. Do not make timing thresholds default required checks without a stable baseline.

### Acceptance criteria

- A handoff reviewer can tell whether checks passed.
- If status checks are not visible, the repo explicitly says how verification is expected to happen.

## Item 3: Verify or de-scope Phase 5 middleware claim

### Problem

Documentation now describes Phase 5 as replacing `BaseHTTPMiddleware` with direct ASGI classes and moving routine logs to DEBUG. The DEBUG logging portion appears landed. The middleware replacement claim needs direct confirmation.

### Tasks

1. Search for `BaseHTTPMiddleware` usage in `src/eggpool/`.
2. If `BaseHTTPMiddleware` remains on the request path:
   - update `AGENTS.md`, architecture docs, and any performance plan closure notes to say only the logging part of Phase 5 has landed;
   - add a follow-up TODO or plan note for ASGI middleware replacement.
3. If direct ASGI middleware is already in place:
   - add or confirm tests for body-limit middleware behavior;
   - add or confirm tests for header redaction behavior;
   - add or confirm streaming responses are not buffered by middleware;
   - add or confirm response status/content-type/error body compatibility.
4. Keep middleware changes separate from routing/transcoding changes if code changes are required.

### Acceptance criteria

- Docs accurately reflect actual middleware implementation.
- If middleware replacement landed, tests pin 413 behavior, header redaction, and streaming non-buffering.
- If it did not land, there is no misleading claim that Phase 5 is complete.

## Item 4: Final docs/config consistency sweep

### Tasks

Search and correct stale language across:

- `README.md`
- `AGENTS.md`
- `architecture/README.md`
- `.opencode/skills/**/SKILL.md`
- `config.example.toml`
- `src/eggpool/_share/config.example.toml`
- `plans/performance_optimization_safety_plan.md`
- `plans/2026-07-05-performance-corrective-pass.md`
- `plans/2026-07-05-performance-closure-pass.md`

Required consistency checks:

- Trace modes are listed only as `all`, `sampled`, `off`.
- `sampled` is described as deterministic request-id sampling at selection/write time, not as "successful plus all errors" unless that behavior is actually implemented.
- Segmentation gating is described as driven by resolved effective compression policy.
- `not_collected` is described distinctly from `empty_request` and parse/segmentation failure.
- Prepared transcode reuse is described as safe-case reuse with thinking-bearing requests recomputed.
- `PreparedTranscode` dispatch data is described as frozen; diagnostics are mutable.
- Routing remains load-based and does not consume cache/compression/synthetic-cache metrics.
- Phase 5 middleware/logging statements match the actual code.

### Acceptance criteria

- No stale `errors` trace mode references remain outside historical plan context.
- No docs claim middleware replacement if it did not land.
- Config examples match parser behavior.

## Item 5: Final targeted tests to keep or add

If any of these are not already present, add the smallest targeted tests:

1. `PreparedTranscode` dispatch data cannot be mutated after construction.
2. Mutating original preflight payload/warnings after construction cannot mutate prepared data.
3. Coordinator reuse path appends warning dicts exactly once.
4. `segmentation_not_collected=True` finalizes as `not_collected`.
5. Missing segmentation without that flag finalizes as `empty_request`.
6. Stats count `not_collected` separately.
7. API serializes provider/protocol segmentation buckets to string keys.
8. Dashboard/cache page renders not-collected segmentation as unavailable/not-collected, not as zero/empty.
9. `routing.trace.mode = "errors"` is rejected by config parsing.
10. Routine proxy log is DEBUG-level only.

## Suggested execution order

1. Run verification commands and capture results.
2. Inspect CI/workflow status visibility.
3. Confirm or de-scope middleware replacement claim.
4. Run docs/config consistency sweep.
5. Add any missing targeted tests only after the sweep identifies real gaps.
6. Update handoff notes with exact command output or failure classification.

## Definition of done

This final handoff is complete when:

- Documented local verification commands are proven accurate.
- CI/status visibility is available or local-only verification is explicitly documented.
- Middleware documentation matches actual implementation.
- Trace-mode, segmentation, prepared-transcode, and routing docs/config examples match runtime behavior.
- Any remaining failures are documented with ownership and next action.
- No further performance-line code changes are needed before normal feature development resumes.
