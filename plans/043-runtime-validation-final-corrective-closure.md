# Final Runtime Validation Corrective Closure

Date: 2026-07-29
Status: implementation handoff

Parent plans:

- `plans/039-test-ci-release-infrastructure-reduction.md`
- `plans/040-test-ci-release-reduction-closure.md`
- `plans/041-test-ci-release-corrective-closure.md`
- `plans/042-runtime-validation-gate-corrective-closure.md`

Corrective baseline:

- `dcb9acaaf0c1f1f4a05869ae7e37b48ed18c5daa`

Canonical-count ceiling inherited from the reductive closure:

- `8474` canonical tests

## Purpose

Plans 039 through 042 successfully reduced Eggpool's ordinary CI to two jobs, removed automated publication, established one supported runtime-validation CLI, replaced the old artifact bundle with one JSON result, corrected child-process RSS units, added truthful workload and latency gates, and introduced a real subprocess validation smoke.

The broad reduction effort is complete. Three narrow closure defects remain:

1. `collect_runtime_snapshot()` reads database contention telemetry from a top-level `contention` key, while the real `/api/stats/runtime` response exposes it under `db.contention`. The existing unit test mirrors the wrong synthetic shape and therefore does not protect the real endpoint contract.
2. The subprocess smoke does not directly prove that the recorded child PID is gone or that the actual `eggpool-soak-*` working directory was removed. Its current "child process must be gone" assertion only rechecks the return code, and its sibling-file assertion inspects `tmp_path`, not the runner's private temporary directory.
3. Plan 042 explicitly prohibited increasing canonical collection, but closure evidence reports `8504` canonical tests versus the `8474` reductive baseline. The new behavior is useful; the excess collection must be consolidated rather than accepted as permanent suite growth.

This plan is the final corrective pass for those defects only. It must not redesign the runtime runner, expand CI, add new workflow machinery, or create another evidence system.

## Required operating model

Eggpool remains a privately operated LAN/SBC service. The verification model remains:

- focused local tests during development;
- one Python 3.12 canonical CI job named `check`;
- one Python 3.11 smoke job named `compat-311`;
- one manually dispatched runtime-validation workflow;
- one retained JSON result from runtime validation;
- manual package publication;
- representative SBC validation only when making target-device performance claims.

GitHub-hosted runs establish functional correctness. They do not establish Raspberry Pi performance.

## Non-goals

- Adding an ordinary CI job.
- Adding a Python-version matrix.
- Adding scheduled runtime validation.
- Restoring automated release publication.
- Replacing `scripts/run_dispatch_stability_soak.py`.
- Changing the public runtime-validation CLI.
- Adding public `--test-mode`, `--fast`, `--keep-temp`, or diagnostics flags.
- Adding a benchmark framework.
- Adding a cleanup daemon, process supervisor, or general monitoring abstraction.
- Adding a new JSONL, Markdown, manifest, checksum, or evidence bundle.
- Adding another plan registry or verification-report file.
- Expanding runtime profiles.
- Weakening production workload, throughput, latency, quiescence, RSS, or SQLite gates.
- Lowering the production minimum duration below 30 seconds.
- Deleting useful behavioral coverage merely to reach a number.
- Increasing canonical collection above `8474`.
- Claiming target-device performance from shared-runner timing.

## Governing decisions

### Decision 1: parse the real runtime response shape

The runtime endpoint is authoritative. The runner must parse database lock telemetry from:

```python
runtime_payload["db"]["contention"]
```

not:

```python
runtime_payload["contention"]
```

The expected fields remain:

```text
lock_wait_p95_ms
lock_wait_max_ms
lock_wait_sample_count
```

Missing `db`, missing `contention`, malformed values, endpoint failure, or non-200 responses must leave those values as `None`. Do not synthesize zero.

The test fixture must mirror the actual response shape emitted by `RuntimeMetricsService.snapshot()`. A synthetic payload that cannot be produced by the real endpoint is not acceptable contract coverage.

### Decision 2: lifecycle proof must observe the actual PID and actual work directory

The process-level smoke must verify observable cleanup, not infer it from a zero return code.

After `run_validation()` returns, the test must be able to assert:

- the exact Eggpool child PID recorded by the runner is no longer alive;
- the exact temporary `eggpool-soak-*` directory created by the runner no longer exists;
- the requested JSON output still exists;
- no extra retained report files exist;
- useful process-log context is available in the assertion message when the run fails.

This information is test diagnostics, not operator output. Do not place internal temporary paths or process-log contents in the retained JSON result.

Preferred implementation: extend the Python-internal `ValidationResult` with non-serialized diagnostics, for example:

```python
@dataclass
class ValidationResult:
    passed: bool
    failure_reasons: list[str]
    output_path: Path
    duration_s: float
    return_code: int
    child_pid: int | None = None
    work_dir: Path | None = None
    process_log_tail: str = ""
    process_stopped: bool | None = None
    work_dir_removed: bool | None = None
```

Equivalent narrowly scoped internal designs are acceptable. Do not expose these fields through CLI arguments or the retained JSON schema.

### Decision 3: cleanup failures must not be silently hidden

The current unconditional:

```python
shutil.rmtree(work_dir, ignore_errors=True)
```

prevents the subprocess smoke from distinguishing successful cleanup from a silent failure.

Required behavior:

1. Capture a bounded tail of `process.log` before removing the working directory.
2. Terminate the child process using the existing bounded terminate/kill path.
3. Confirm the child is no longer alive.
4. Remove the working directory without silently discarding removal errors.
5. Record cleanup outcomes in the internal `ValidationResult`.
6. Preserve the requested output JSON.

A cleanup failure must make the process-level smoke fail with the captured log tail and cleanup exception. The production runner may also return nonzero on cleanup failure, but this plan does not require adding a public cleanup gate to schema version 2.

Do not retain the temporary work directory by default. Do not add a public keep-temp option.

### Decision 4: reduce collected nodes by consolidation, not capability loss

The canonical ceiling is `8474` collected tests. The current reported closure count is `8504`, so the implementation must remove or consolidate at least 30 canonical nodes, plus any new nodes introduced by this corrective pass.

Preserve behavioral invariants while reducing node count. Prefer:

- table-driven loops inside one test function where all rows exercise one pure contract;
- merging static text assertions into one contract test;
- deleting legacy helpers and tests no longer used by production code;
- moving repeated parser/helper assertions into existing tests;
- deleting exact duplicates;
- retaining one clear failure message per scenario.

Do not preserve one pytest node per scalar boundary when a compact table inside one function communicates the same contract.

Do not reduce count by:

- removing the real subprocess smoke;
- removing endpoint-shape coverage;
- removing direct ratio boundary coverage;
- removing zero-work/all-error failure coverage;
- removing quiescence fail-closed coverage;
- marking tests `slow`, `soak`, `network`, or otherwise excluding them from canonical CI solely to reduce count;
- hiding tests through collection tricks;
- deleting unrelated correctness coverage without demonstrating duplication.

### Decision 5: no further CI architecture work

`.github/workflows/ci.yml` must continue to contain only:

- `check`;
- `compat-311`.

`.github/workflows/extended-soak.yml` remains:

- `workflow_dispatch` only;
- one Python 3.12 job;
- no matrix;
- no schedule;
- one JSON artifact.

No workflow change is expected unless a static assertion currently misstates the final contract.

## Required end state

This corrective pass is complete only when all of the following are true:

- database contention metrics are read from `db.contention`;
- the real endpoint-shape test uses `{"db": {"contention": ...}}`;
- absent or malformed contention data remains `None`;
- pending and active-reservation parsing remains unchanged and fail-closed;
- the process smoke receives the actual child PID and work-directory path through a Python-internal diagnostic seam;
- the process smoke proves the exact PID is gone after return;
- the process smoke proves the exact work directory is gone after return;
- process-log tail text is available when an assertion fails;
- cleanup errors are not swallowed by `ignore_errors=True`;
- the output JSON remains after cleanup;
- no public CLI flag is added;
- no temporary path or log content is written to the retained JSON;
- canonical collection is `<= 8474`;
- the real subprocess smoke remains in canonical execution;
- canonical runtime remains within the existing 15-minute timeout;
- ordinary CI remains exactly `check` and `compat-311`;
- both jobs pass on the final closure commit;
- Plans 040 through 042 are reopened while correction is pending;
- Plans 040 through 043 are marked complete only after exact-head green checks.

---

## Workstream A — Reopen closure status truthfully

### A1. Reopen Plans 040, 041, and 042

In the first implementation commit, change only the status lines:

`plans/040-test-ci-release-reduction-closure.md`

```text
Status: final corrective closure pending — see Plan 043
```

`plans/041-test-ci-release-corrective-closure.md`

```text
Status: final corrective closure pending — see Plan 043
```

`plans/042-runtime-validation-gate-corrective-closure.md`

```text
Status: final corrective closure pending — see Plan 043
```

Leave this file as:

```text
Status: implementation handoff
```

Do not rewrite historical plan bodies or delete prior closure evidence. The status correction is sufficient.

### A2. Closure status rule

Do not restore completed status until:

- focused runtime-runner tests pass;
- the real subprocess smoke passes;
- canonical collection is at or below `8474`;
- the canonical suite passes;
- Python 3.11 smoke passes;
- Ruff and Pyright pass;
- `check` and `compat-311` are green on the final closure commit.

### Workstream A acceptance criteria

- [ ] Plans 040, 041, and 042 no longer claim complete during implementation.
- [ ] Plan 043 remains open while any rejection condition is true.
- [ ] No new status registry, evidence artifact, or validator is added.

---

## Workstream B — Correct the runtime contention response path

### B1. Fix `collect_runtime_snapshot()`

Current incorrect behavior:

```python
contention = rt.get("contention", {})
```

Required behavior:

```python
db_runtime = rt.get("db")
contention = db_runtime.get("contention") if isinstance(db_runtime, dict) else None
```

Then parse only from a dictionary:

```python
if isinstance(contention, dict):
    db_lock_p95 = _optional_number(contention.get("lock_wait_p95_ms"))
    db_lock_max = _optional_number(contention.get("lock_wait_max_ms"))
    db_lock_count = _optional_int(contention.get("lock_wait_sample_count"))
```

Use existing small conversion helpers if available. Do not create a generic schema framework.

Required semantics:

- valid numeric values are preserved;
- integer or float latency values are accepted when the endpoint legitimately returns either;
- booleans are not treated as numeric metrics;
- absent values stay `None`;
- malformed values stay `None` and may update bounded polling diagnostics;
- endpoint failure leaves all runtime-derived values `None`;
- routing-runtime parsing continues to read:
  - `routing_runtime.pending_count`;
  - `routing_runtime.active_reservations_count`.

### B2. Replace the synthetic unit payload

Update the endpoint-shape unit test from:

```python
{
    "contention": {...},
    "routing_runtime": {...},
}
```

to:

```python
{
    "db": {
        "contention": {
            "lock_wait_p95_ms": 0.5,
            "lock_wait_max_ms": 1.5,
            "lock_wait_sample_count": 10,
        }
    },
    "routing_runtime": {
        "pending_count": 0,
        "active_reservations_count": 0,
    },
}
```

The test must assert all three contention fields and both routing fields.

### B3. Add compact failure-shape coverage

Use one table-driven test function with an internal loop covering:

1. missing `db`;
2. `db=None`;
3. missing `contention`;
4. `contention=None`;
5. malformed contention scalar;
6. malformed individual values;
7. runtime endpoint exception;
8. non-200 runtime response.

For every row:

- contention metrics remain `None`;
- unavailable pending/reservation values remain `None` when the runtime call failed;
- no synthetic zero appears;
- polling failure counters are updated when appropriate.

Keep this as one or two pytest nodes, not eight separate tests.

### B4. Optional real-service contract assertion

If `tests/unit/test_runtime_metrics.py` already exercises `RuntimeMetricsService.snapshot()`, add or reuse one assertion that verifies:

```python
snapshot["db"]["contention"]
```

exists when the database service supplies a contention snapshot.

Do not add a new integration file solely for this assertion. Prefer extending an existing runtime-metrics test.

### Workstream B acceptance criteria

- [ ] Runner reads contention data from `db.contention`.
- [ ] No top-level contention parsing remains.
- [ ] Test payload matches the real runtime endpoint shape.
- [ ] Valid p95, max, and sample-count values are preserved.
- [ ] Missing or malformed metrics remain `None`.
- [ ] Routing-runtime drain fields remain correct.
- [ ] Coverage is compact and does not increase canonical collection.

---

## Workstream C — Make subprocess and temp-directory cleanup observable

### C1. Extend the internal result contract

Extend `ValidationResult` or an equivalent internal diagnostics object with enough information for the process smoke to observe cleanup directly.

Preferred fields:

```python
child_pid: int | None
work_dir: Path | None
process_log_tail: str
process_stopped: bool | None
work_dir_removed: bool | None
cleanup_error: str | None
```

Constraints:

- fields are Python-internal;
- fields are not written into the retained JSON;
- no public CLI option is added;
- no full process log is retained;
- log tail is bounded, for example 100 lines or 16 KiB;
- log tail is redacted using existing redaction helpers before exposure.

### C2. Use one cleanup path

Refactor `run_validation()` so startup failure, gate failure, successful validation, and unexpected exceptions all use one cleanup block.

Recommended sequence:

```text
capture bounded process-log tail
terminate/kill child
confirm child exited
shut down mock upstream
remove work directory
record cleanup outcome
return ValidationResult
```

Avoid multiple early-return paths after the child process has started unless they still pass through the same cleanup code.

A small mutable result builder or local outcome variables are acceptable. Do not create an orchestration class hierarchy.

### C3. Stop swallowing work-directory removal failures

Replace silent removal:

```python
shutil.rmtree(work_dir, ignore_errors=True)
```

with explicit handling:

```python
try:
    shutil.rmtree(work_dir)
except FileNotFoundError:
    pass
except OSError as exc:
    cleanup_error = f"work directory cleanup failed: {exc}"
```

After removal:

```python
work_dir_removed = not work_dir.exists()
```

The process smoke must fail if `work_dir_removed` is not true.

Do not leave the directory intentionally for debugging. Use the bounded captured log tail instead.

### C4. Confirm process exit

After `_terminate_eggpool(proc)`:

- require `proc.poll() is not None`;
- record `process_stopped=True` only after confirmation;
- optionally use a small standard-library helper in the test to confirm the PID is not alive after return.

Suggested test helper:

```python
def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
```

On Windows, use the existing process object or a platform-appropriate bounded check. The canonical GitHub runner is Linux; the implementation must remain importable on macOS and Windows.

Do not depend on `psutil`.

### C5. Preserve useful log context

Before deleting `work_dir`, read a bounded tail from `process.log`.

Requirements:

- missing log file yields an empty string;
- read errors do not hide the original validation failure;
- the tail is redacted;
- assertion messages include the tail only on failure;
- no log file or log text is written beside the retained JSON.

### C6. Strengthen the real subprocess smoke

Update `tests/integration/test_runtime_validation_process_smoke.py` to assert:

```python
assert result.child_pid is not None
assert not pid_is_alive(result.child_pid)
assert result.process_stopped is True
assert result.work_dir is not None
assert not result.work_dir.exists()
assert result.work_dir_removed is True
assert result.cleanup_error is None
```

Failure messages should include:

```python
result.process_log_tail
```

Continue asserting:

- one retained JSON file;
- successful early and late traffic;
- streaming and non-streaming success;
- explicit quiescence success;
- RSS availability;
- SQLite audit success;
- no JSONL, Markdown, or manifest siblings;
- wall-clock duration under 20 seconds.

### C7. Add one focused cleanup-failure unit test

Do not create another process test. Use one unit test with mocks to prove that a simulated `shutil.rmtree()` failure:

- is recorded as `cleanup_error`;
- produces `work_dir_removed=False`;
- does not erase the captured process-log tail;
- does not delete or corrupt the requested output JSON.

Keep this as one pytest node.

### Workstream C acceptance criteria

- [ ] Process smoke observes the actual child PID.
- [ ] Process smoke proves the PID is gone.
- [ ] Process smoke observes the actual work-directory path.
- [ ] Process smoke proves the work directory is gone.
- [ ] `ignore_errors=True` is removed from runner cleanup.
- [ ] A bounded redacted process-log tail is available on failure.
- [ ] Temporary paths and logs are not added to retained JSON.
- [ ] Public CLI remains unchanged.
- [ ] Cleanup failure is tested without adding another real process test.

---

## Workstream D — Restore the canonical suite to the reductive ceiling

### D1. Measure exact current collection before editing

From a clean checkout at the implementation baseline, run:

```bash
uv sync --frozen --extra dev
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  --collect-only -q > /tmp/eggpool-plan043-collect-before.txt

tail -n 5 /tmp/eggpool-plan043-collect-before.txt
```

Record the exact count only in the implementation commit or pull-request description. Do not commit the collection output.

Expected reported starting point from Plan 042 closure evidence:

```text
8504 canonical tests
```

If the actual count differs because `main` advanced, use the actual count but retain the final ceiling of `8474`.

### D2. Inventory the Plan 042 additions

Inspect:

- `tests/unit/test_runtime_validation_runner.py`;
- `tests/integration/test_runtime_validation_process_smoke.py`;
- any tests added or split by commits:
  - `a2dd66db`;
  - `727f3d06`;
  - `c4d717d3`;
  - `9775dbf6`;
  - `b5959ed8`.

List locally:

- pure boundary tests that can be table-driven inside one node;
- static documentation/workflow tests that can be merged;
- legacy tests for helpers no longer used by production code;
- duplicate response-shape tests;
- duplicate parser or output-shape assertions;
- tests whose assertions are already fully subsumed by the real process smoke.

Do not commit this inventory.

### D3. Required consolidation targets

Consolidate enough nodes to reach `<= 8474`, including any nodes added by Workstreams B and C.

Preferred targets:

#### Ratio-gate tests

Merge separate methods for:

- below limit;
- exactly at limit;
- above limit;
- missing early value;
- missing late value;
- zero early value;
- negative early value;

into at most two table-driven test functions with internal scenario loops and per-case assertion labels.

#### Workload-gate tests

Merge separate methods for:

- zero attempts;
- zero successes/all errors;
- minimum success;
- configured error allowance;
- configured error excess;
- missing stream/non-stream coverage;

into at most two test functions.

#### Quiescence tests

Keep behavior for:

- first observation drained;
- active then drained;
- active until timeout;
- endpoint failure;
- nullable values.

Consolidate compatible cases into a small number of async tests using internal scenario tables. Preserve clear per-scenario failure messages.

#### Static workflow/documentation tests

Merge parser, workflow-shape, documentation-alignment, and removed-artifact-language checks where they are all static text-contract assertions.

#### Legacy drain evaluator

If `evaluate_drain_gate()` is no longer called by production runner code and only exists for tests, delete the helper and its tests. The quiescence evaluator is now authoritative.

Verify before deletion:

```bash
grep -RIn "evaluate_drain_gate" scripts src tests --exclude-dir=.git
```

#### Process-smoke helper tests

Move `build_run_config` and unknown-profile checks into existing unit coverage if they do not need the integration module. Avoid collecting trivial non-process tests from the process-smoke file.

### D4. Preserve diagnostic quality inside loops

For table-driven loops, use scenario IDs in assertion messages:

```python
for case in cases:
    result = ...
    assert result.passed is case.expected_pass, case.name
```

Do not use one opaque mega-test with unrelated behaviors. Consolidate only rows of the same pure contract.

### D5. Do not manipulate markers to hide count

The real subprocess smoke remains canonical.

Do not add or change markers solely to exclude tests from:

```text
not slow and not performance and not soak and not extended_soak and not live and not network
```

The final reduction must come from deleting duplication or consolidating collected nodes.

### D6. Verify final count

Run:

```bash
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  --collect-only -q > /tmp/eggpool-plan043-collect-after.txt

tail -n 5 /tmp/eggpool-plan043-collect-after.txt
```

Required result:

```text
canonical collection <= 8474
```

Record before/after counts in commit or pull-request metadata only.

### Workstream D acceptance criteria

- [ ] Exact starting collection is measured.
- [ ] Final canonical collection is `<= 8474`.
- [ ] No test is hidden through marker manipulation.
- [ ] Real subprocess coverage remains canonical.
- [ ] Contention parsing coverage remains.
- [ ] Ratio, workload, and quiescence boundary behavior remains covered.
- [ ] Duplicate or obsolete nodes are removed rather than replaced with new files.
- [ ] No test-count report artifact is committed.

---

## Workstream E — Focused verification

### E1. Runtime-runner unit tests

Run:

```bash
uv run pytest tests/unit/test_runtime_validation_runner.py \
  -q --tb=short --maxfail=1
```

Required coverage includes:

- real `db.contention` parsing;
- malformed/missing contention values remain `None`;
- direct ratio boundaries;
- workload minimums and error bounds;
- quiescence success and fail-closed behavior;
- cleanup failure diagnostics;
- one-file output contract;
- public parser unchanged.

### E2. Runtime metrics contract

Run the affected runtime metrics tests:

```bash
uv run pytest tests/unit/test_runtime_metrics.py \
  -q --tb=short --maxfail=1
```

If a narrower existing file owns the runtime endpoint response shape, run that file instead and record the exact command.

### E3. Real process smoke

Run:

```bash
uv run pytest tests/integration/test_runtime_validation_process_smoke.py \
  -q --tb=short --maxfail=1
```

Then repeat the real process test enough to expose lifecycle flakiness without creating permanent tooling:

```bash
for i in 1 2 3 4 5; do
  uv run pytest \
    tests/integration/test_runtime_validation_process_smoke.py::test_run_validation_produces_one_json_and_cleans_up \
    -q --tb=short --maxfail=1 || exit 1
done
```

Required on every run:

- child PID gone;
- work directory gone;
- one output JSON retained;
- no cleanup error;
- runtime below 20 seconds.

Five repetitions are implementation evidence only. Do not commit logs or add a repeat workflow.

### E4. Static checks

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
```

Do not exclude the runner or test files from Pyright.

### E5. Canonical suite

```bash
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
```

Requirements:

- `<= 8474` collected/passing canonical tests;
- no failure;
- no leaked child process;
- no leaked `eggpool-soak-*` directory;
- total runtime remains within the existing 15-minute CI timeout.

### E6. Python 3.11 smoke

```bash
uv run --python 3.11 pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add the real process smoke to `compat-311`.

### E7. Public command regression

From a clean checkout:

```bash
rm -f /tmp/eggpool-runtime-validation.json
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 30 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json
python -m json.tool /tmp/eggpool-runtime-validation.json >/dev/null
```

Verify:

- exit status zero;
- one JSON file retained;
- workload gate passes;
- direct p95/p99 ratio gates pass;
- quiescence passes;
- RSS gate passes;
- SQLite audit passes;
- no child process remains;
- no `eggpool-soak-*` directory from the run remains.

Record only a concise summary in commit or pull-request metadata. Do not add an evidence file.

### E8. Workflow-shape verification

Confirm ordinary CI still has two jobs:

```bash
python - <<'PY'
from pathlib import Path
text = Path('.github/workflows/ci.yml').read_text()
assert text.count('runs-on:') == 2
assert 'check:' in text
assert 'compat-311:' in text
PY
```

Confirm manual runtime validation remains one job and one artifact:

```bash
python - <<'PY'
from pathlib import Path
text = Path('.github/workflows/extended-soak.yml').read_text()
assert 'workflow_dispatch:' in text
assert text.count('runs-on:') == 1
assert 'matrix:' not in text
assert 'schedule:' not in text
assert text.count('upload-artifact') == 1
assert '/tmp/eggpool-runtime-validation.json' in text
PY
```

Do not add these snippets as scripts.

### Workstream E acceptance criteria

- [ ] Focused unit tests pass.
- [ ] Runtime-metrics contract tests pass.
- [ ] Real process smoke passes five consecutive local runs.
- [ ] Ruff format and lint pass.
- [ ] Pyright passes.
- [ ] Canonical suite passes at `<= 8474` tests.
- [ ] Python 3.11 smoke passes.
- [ ] Public 30-second command passes from a clean checkout.
- [ ] Ordinary CI remains two jobs.
- [ ] Manual runtime workflow remains one job and one JSON artifact.

---

## Workstream F — Commit sequence and exact-head closure

### F1. Implementation commit

Recommended first commit:

```text
fix: close final runtime validation gaps
```

It should contain:

- reopened Plan 040–042 status lines;
- corrected `db.contention` parsing;
- real response-shape tests;
- cleanup diagnostics and explicit cleanup handling;
- strengthened process smoke;
- test consolidation to `<= 8474`;
- any minimal documentation correction required by actual behavior.

The commit message or pull-request description should record:

- canonical count before and after;
- focused-test results;
- canonical-suite result and duration;
- Python 3.11 smoke result;
- five-run process-smoke result;
- public 30-second command result.

Do not add a verification file.

### F2. Push and verify implementation CI

Push the implementation commit and require:

- `check`: success;
- `compat-311`: success.

If either fails, correct the implementation and repeat. Do not close plans on a failing commit.

### F3. Closure commit

After implementation correctness is established, update Plans 040–043 to completed status in one minimal documentation commit.

Recommended message:

```text
docs: close runtime validation reduction plans
```

Do not add tables of copied workflow logs. A concise reference to the implementation commit and final canonical count is sufficient.

### F4. Exact-head proof

Because the closure commit becomes `main`, both ordinary CI jobs must also pass on that closure commit.

Do not make another commit after exact-head checks pass.

If the closure commit fails CI:

1. reopen Plan 043 status in the corrective commit;
2. fix the failure;
3. repeat exact-head verification.

### Workstream F acceptance criteria

- [ ] Implementation commit contains no unrelated production changes.
- [ ] Implementation commit CI passes.
- [ ] Closure commit changes only plan status/evidence wording.
- [ ] `check` passes on the exact closure commit.
- [ ] `compat-311` passes on the exact closure commit.
- [ ] No commit follows the green closure commit.

---

## Suggested execution order for a smaller implementation model

Follow this order exactly:

1. Read Plans 040–043 and the current runner/tests.
2. Reopen Plans 040–042 status lines.
3. Measure current canonical collection.
4. Correct `db.contention` parsing.
5. Replace the synthetic response-shape test.
6. Add compact malformed-shape coverage without increasing node count.
7. Extend internal `ValidationResult` diagnostics.
8. Capture bounded redacted process-log tail.
9. Make process and work-directory cleanup explicit and observable.
10. Strengthen the existing real process smoke; do not add another process test.
11. Consolidate runtime-runner tests until canonical collection is `<= 8474`.
12. Run focused tests.
13. Run the process smoke five times.
14. Run Ruff and Pyright.
15. Run canonical collection and canonical execution.
16. Run Python 3.11 smoke.
17. Run the public 30-second command.
18. Push the implementation commit.
19. Verify both ordinary CI jobs.
20. Update plan statuses in a minimal closure commit.
21. Verify both ordinary CI jobs on the closure commit.
22. Stop. Do not add further polish or evidence machinery.

## Explicit rejection conditions

Do not close this plan if any of the following remain:

- runner still reads top-level `runtime["contention"]`;
- the unit payload still uses a top-level contention key;
- real contention values are silently absent despite being present under `db.contention`;
- unavailable contention values become zero;
- process smoke infers cleanup only from `return_code`;
- process smoke does not observe the actual PID;
- process smoke does not observe the actual work directory;
- child PID remains alive after return;
- work directory remains after return;
- `shutil.rmtree(..., ignore_errors=True)` remains in the runner cleanup path;
- process log context is unavailable on smoke failure;
- temporary path or process log is added to retained JSON;
- a public test/debug/keep-temp CLI flag is added;
- canonical collection exceeds `8474`;
- tests are hidden with markers solely to reduce canonical count;
- the real process smoke is removed or excluded from canonical CI;
- production ratio, workload, throughput, quiescence, RSS, or database-audit gates are weakened;
- ordinary CI contains more than `check` and `compat-311`;
- manual runtime workflow gains a matrix, schedule, or extra job;
- manifest, JSONL, Markdown, checksum, or report-bundle output returns;
- either exact-head CI job is absent, failing, cancelled, or unverified;
- Plans 040–043 are marked complete before exact-head closure checks pass.

## Definition of done

This line of work is closed when Eggpool's reduced verification model remains intact and the final runtime-validation gaps are corrected without adding new machinery: the runner must parse the real runtime endpoint schema, the real-process smoke must directly prove child and temporary-directory cleanup, and canonical collection must return to the `8474` reductive ceiling while preserving the essential behavioral contracts.

The final repository must still have exactly two ordinary CI jobs, one manually dispatched runtime-validation job, one retained JSON result, and manual release publication. Both ordinary CI jobs must pass on the exact final closure commit.