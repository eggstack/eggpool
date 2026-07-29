# Final Runtime Validation Lifecycle Closure

Date: 2026-07-29
Status: implementation handoff

Parent plans:

- `plans/040-test-ci-release-reduction-closure.md`
- `plans/041-test-ci-release-corrective-closure.md`
- `plans/042-runtime-validation-gate-corrective-closure.md`
- `plans/043-runtime-validation-final-corrective-closure.md`

Corrective baseline:

- `6da56a45f9d69273ff14bb6e94fb46a7d76b0852`

Canonical collection ceiling:

- `8454` tests

## Purpose

Plan 043 correctly fixed the real `db.contention` response path, exposed internal cleanup diagnostics, strengthened the real-process smoke, and reduced canonical collection below the prior ceiling. Four tightly bounded closure defects remain:

1. `run_validation()` creates the work directory, starts the mock upstream, writes configuration, and starts Eggpool before entering the shared `try/finally`. Failures during those setup steps can bypass cleanup entirely.
2. Cleanup currently records only work-directory removal errors. Child termination errors and mock-upstream shutdown errors are either raised from `finally` or suppressed.
3. The malformed-runtime test labels a case `db_none` but does not actually send `{"db": None}`.
4. Process-log redaction fingerprints the entire tail when any credential marker appears, destroying unrelated diagnostic context. An exception raised by `run_validation()` also prevents the process smoke from displaying the captured tail.

This plan closes only those defects and the associated exact-head verification gap. It must not redesign the runtime-validation runner, alter production gate semantics, expand CI, add public diagnostics flags, or reopen unrelated test architecture.

## Required operating model

The repository keeps the current reduced model:

- one Python 3.12 ordinary CI job named `check`;
- one Python 3.11 smoke job named `compat-311`;
- one manually dispatched runtime-validation workflow;
- one retained JSON runtime-validation result;
- manual package publication;
- no shared-runner performance claims for Raspberry Pi or other SBC targets.

## Non-goals

- Adding an ordinary CI job or Python matrix.
- Adding scheduled validation.
- Changing `.github/workflows/extended-soak.yml` topology.
- Changing the public CLI or adding `--keep-temp`, `--fast`, `--test-mode`, or similar options.
- Changing workload, throughput, latency, quiescence, RSS, or SQLite gate thresholds.
- Changing schema version 2 solely for cleanup diagnostics.
- Writing process-log text, temporary paths, or cleanup internals into the retained JSON.
- Retaining temporary work directories for debugging.
- Adding `psutil`, a process-supervision package, a cleanup daemon, or a lifecycle framework.
- Adding a report, manifest, checksum, registry, or evidence bundle.
- Increasing canonical collection above `8454`.
- Broadly rewriting runtime metrics or test files.

## Governing decisions

### Decision 1: the lifecycle boundary starts immediately after work-directory creation

After `work_dir` and its path values are created, every fallible operation must be inside one outer `try/except/finally` lifecycle boundary.

Required initialization shape:

```python
work_dir = Path(tempfile.mkdtemp(prefix="eggpool-soak-"))
process_log_path = work_dir / "process.log"
result = ValidationResult(...)
proc: subprocess.Popen[str] | None = None
upstream_server: HTTPServer | None = None
result_payload: dict[str, Any] | None = None

try:
    # create mock state
    # start mock upstream
    # write config
    # start Eggpool
    # wait for health
    # run workload and gates
except asyncio.CancelledError:
    raise
except Exception as exc:
    # convert unexpected setup/runtime failure to a failed result
finally:
    # always collect log tail and clean every acquired resource
```

No fallible setup step may remain between `mkdtemp()` and the outer `try`.

Unknown-profile validation may continue returning before a work directory exists because no resource has been acquired.

### Decision 2: resource acquisition is nullable and cleanup is idempotent

Use nullable locals:

```python
proc = None
upstream_server = None
```

Cleanup helpers must safely accept resources that were never acquired. The same cleanup path must handle:

- mock-upstream startup failure;
- config-write failure after the upstream starts;
- Eggpool subprocess-launch failure;
- health timeout;
- workload or gate failure;
- unexpected runtime exception;
- normal success;
- task cancellation, while still re-raising cancellation after cleanup.

Cleanup must not rely on a successful `ValidationResult` payload or a started subprocess.

### Decision 3: aggregate every cleanup error

Cleanup must attempt all applicable operations even when an earlier cleanup action fails:

1. capture a bounded redacted process-log tail;
2. terminate/kill the Eggpool child when present;
3. confirm the child is stopped;
4. shut down the mock upstream when present;
5. close the mock upstream socket with `server_close()` when supported;
6. remove the work directory;
7. confirm the work directory is absent.

Do not use `contextlib.suppress(Exception)` for owned cleanup resources.

Use a small list of bounded messages, for example:

```python
cleanup_errors: list[str] = []
```

Append independent errors such as:

```text
child termination failed: ...
child process remained alive after termination
mock upstream shutdown failed: ...
mock upstream close failed: ...
work directory cleanup failed: ...
work directory remains after cleanup
```

The internal result may retain the current singular `cleanup_error` field by joining messages deterministically, or may replace it with `cleanup_errors: tuple[str, ...]`. Do not expose either in the JSON schema.

### Decision 4: cleanup failure affects the returned outcome

A cleanup failure must not coexist with `result.passed is True` or `return_code == 0`.

After cleanup:

- set `result.passed = False`;
- append bounded cleanup messages to `result.failure_reasons` without duplication;
- set a dedicated nonzero return code, recommended `12`, unless an existing nonzero code already describes the primary failure;
- preserve `process_log_tail`, `child_pid`, `work_dir`, `process_stopped`, and `work_dir_removed` for test diagnostics.

The retained JSON must not claim `passed: true` when the returned result failed because cleanup failed.

Preferred implementation:

- build the JSON payload in memory during validation;
- perform cleanup in `finally`;
- reconcile cleanup status into `result` and the payload;
- write the single JSON file once after cleanup.

A narrowly scoped rewrite of an already-written JSON file is acceptable only if it remains atomic and cannot leave the return result and JSON disagreeing.

### Decision 5: unexpected setup/runtime exceptions return diagnostic results

Except for `asyncio.CancelledError`, an unexpected exception after work-directory creation should be converted into a failed `ValidationResult` so callers and the real-process smoke can inspect cleanup diagnostics.

Required failure reason shape:

```text
runtime validation internal error: <ExceptionClass>: <bounded message>
```

Do not include secrets, full tracebacks, or unbounded exception strings in the retained JSON.

Log the traceback through the normal logger for developer diagnosis. The result and JSON receive only the bounded failure reason.

If report generation is possible, retain one minimal schema-v2 failure JSON containing the normal identity fields, `passed: false`, and the bounded failure reason. Do not create a second diagnostic file.

### Decision 6: redact log content without destroying unrelated lines

Do not pass the entire joined log tail to the current scalar `_redact()` helper.

Add one narrow log-tail redaction helper that:

- processes each line independently;
- replaces only credential-bearing values or complete credential-bearing lines;
- preserves unrelated error, traceback, lifecycle, and request-state lines;
- removes raw bearer tokens, API keys, and configured secret values;
- keeps the existing 100-line and 16-KiB bounds;
- returns an empty string for missing or unreadable files.

One acceptable result is:

```text
Authorization: Bearer <redacted>
ERROR eggpool: database connection failed
```

The second line must remain visible.

Do not create a general logging-redaction framework in this pass.

### Decision 7: exact-head closure is external state, not another evidence system

No committed CI-report artifact is required. Exact-head proof may be recorded in the final implementation/PR description or the closure section of this plan.

The status sequence is:

1. implementation commit reopens Plans 040 through 043 and leaves Plan 044 open;
2. implementation and focused checks pass;
3. status-only closure commit marks Plans 040 through 044 closed;
4. both `check` and `compat-311` must pass on that status-only commit SHA;
5. no further repository commit is needed solely to restate the already-green result.

The line of work is not considered closed during the interval between steps 3 and 4.

## Required end state

The pass is complete only when all of the following are true:

- the outer lifecycle `try/finally` begins immediately after work-directory creation and result initialization;
- mock-upstream startup, config writing, and subprocess startup occur inside that boundary;
- `proc` and `upstream_server` are nullable and safely cleaned when partially acquired;
- all setup, health, workload, gate, and unexpected-exception paths use the same cleanup function;
- `asyncio.CancelledError` still propagates after cleanup;
- child termination exceptions are captured rather than escaping `finally`;
- mock-upstream `shutdown()` exceptions are captured rather than suppressed;
- `server_close()` is attempted and failures are captured;
- work-directory removal is attempted even after termination or upstream cleanup failure;
- all cleanup errors are aggregated deterministically;
- any cleanup error forces `passed=False` and a nonzero return code;
- retained JSON and returned `ValidationResult` agree on pass/fail;
- cleanup diagnostics remain Python-internal and absent from retained JSON;
- log-tail redaction preserves non-secret diagnostic lines;
- a real `{"db": None}` payload is tested;
- runtime endpoint exception and non-200 tests assert polling failure accounting;
- the real-process smoke still proves the exact PID and work directory are gone;
- failure assertions display the bounded redacted log tail;
- no public CLI option or workflow job is added;
- canonical collection remains `<= 8454`;
- canonical runtime remains within the 15-minute CI timeout;
- `check` and `compat-311` pass on the final status commit SHA.

---

## Workstream A — Reopen status before implementation

In the implementation commit, change the status lines of Plans 040 through 043 to:

```text
Status: lifecycle closure pending — see Plan 044
```

Leave this plan as:

```text
Status: implementation handoff
```

Do not rewrite historical plan bodies or delete earlier closure evidence.

### Acceptance criteria

- [ ] Plans 040 through 043 do not claim closed while Plan 044 defects remain.
- [ ] Plan 044 remains open during implementation and verification.
- [ ] No new status registry or evidence document is created.

---

## Workstream B — Move all acquisition under one lifecycle boundary

### B1. Initialize diagnostics before fallible setup

Create `ValidationResult`, nullable resource locals, and path diagnostics immediately after `mkdtemp()`.

The result must already know:

- requested output path;
- work-directory path;
- default failed state;
- no child PID yet;
- no cleanup outcome yet.

### B2. Move the following operations inside the outer `try`

- `MockUpstreamState(...)` construction if it can raise;
- `_start_mock_upstream(...)`;
- reading `server_address`;
- `_write_soak_config(...)`;
- environment preparation that can raise;
- `_start_eggpool(...)`;
- health polling;
- all workload and gate execution;
- result-payload construction.

### B3. Convert unexpected exceptions

Add an `except Exception as exc` branch that:

- logs the traceback;
- sets a bounded internal-error failure reason;
- preserves any earlier failure reasons;
- selects a nonzero return code;
- prepares a minimal failure JSON payload when possible;
- does not return before `finally` completes.

Keep `except asyncio.CancelledError: raise`, relying on `finally` for cleanup.

### B4. Focused setup-failure tests

Use a compact table or two focused tests to inject failures at these boundaries:

1. `_start_mock_upstream` raises before acquisition;
2. `_write_soak_config` raises after the upstream starts;
3. `_start_eggpool` raises after the upstream starts and config exists.

For each scenario assert:

- the work directory is removed;
- any acquired upstream is shut down and closed;
- no child is reported when none started;
- the returned result is failed and nonzero, except cancellation tests;
- cleanup diagnostics are available;
- no extra retained files are created;
- no exception escapes for ordinary `Exception` failures.

Use stubs and monkeypatching; do not start a real subprocess for every row.

### Acceptance criteria

- [ ] No fallible resource acquisition occurs before the outer lifecycle `try`.
- [ ] Partial setup failures clean every resource already acquired.
- [ ] Ordinary setup exceptions return a diagnostic failed result.
- [ ] Cancellation still propagates after cleanup.
- [ ] Coverage is compact and does not increase canonical collection.

---

## Workstream C — Aggregate cleanup errors and reconcile final status

### C1. Make child termination fail-safe

Wrap `_terminate_eggpool(proc)` and exit confirmation independently.

Even when termination raises:

- record the error;
- continue upstream shutdown;
- continue work-directory removal;
- set `process_stopped` truthfully from `proc.poll()` and, where useful, `_pid_is_alive()`.

Do not claim `process_stopped=True` merely because `terminate()` was called.

### C2. Make upstream cleanup explicit

Replace suppressed shutdown with explicit handling:

```python
try:
    upstream_server.shutdown()
except Exception as exc:
    cleanup_errors.append(...)

try:
    upstream_server.server_close()
except Exception as exc:
    cleanup_errors.append(...)
```

Continue to directory removal regardless of either failure.

### C3. Reconcile result and JSON

After cleanup:

- aggregate cleanup messages;
- update the returned result;
- update the in-memory JSON payload to `passed: false` and append the same bounded reasons;
- atomically write the one output JSON;
- preserve the original nonzero primary failure code when one exists;
- use the cleanup-specific code only when validation otherwise passed.

No cleanup diagnostic fields or temporary paths may be serialized.

### C4. Focused cleanup-failure table

Use one table-driven test covering:

- termination raises;
- process remains alive after termination attempt;
- upstream `shutdown()` raises;
- upstream `server_close()` raises;
- `shutil.rmtree()` raises;
- more than one cleanup action raises in the same run.

For each case assert:

- later cleanup actions were still attempted;
- all expected errors are present once and in stable order;
- the result is failed and nonzero;
- successful cleanup outcomes remain truthful;
- internal paths/log text are absent from JSON;
- JSON and returned result agree.

### Acceptance criteria

- [ ] No owned-resource cleanup exception is suppressed or allowed to replace the primary result.
- [ ] Cleanup attempts continue after earlier cleanup failures.
- [ ] Multiple cleanup failures are aggregated deterministically.
- [ ] Cleanup failure cannot return success.
- [ ] JSON and `ValidationResult` pass/fail values cannot diverge.
- [ ] No schema expansion or second output file is introduced.

---

## Workstream D — Preserve useful, secret-safe log context

### D1. Add line-preserving redaction

Implement one narrowly scoped helper for process-log lines. It must redact secret-bearing portions without hashing or replacing the entire tail.

At minimum cover:

- `Authorization: Bearer ...`;
- `api_key=...` and `api-key=...` forms already recognized by the script;
- configured soak API key values;
- raw tokens matching existing credential patterns.

### D2. Add a mixed-content test

Construct a log containing:

```text
Authorization: Bearer sk-supersecret123
ERROR eggpool.db: database connection failed
INFO eggpool: shutting down
```

Assert:

- `sk-supersecret123` is absent;
- a redaction marker is present;
- `database connection failed` remains present;
- `shutting down` remains present;
- line and byte bounds still apply.

### D3. Ensure exception paths retain the tail

Because ordinary internal exceptions now return a failed `ValidationResult`, the process smoke and setup-failure tests must include `process_log_tail` in assertion messages.

Do not catch an escaping exception and replace it with a generic message that discards diagnostics.

### Acceptance criteria

- [ ] One secret-bearing line does not erase unrelated log lines.
- [ ] Raw credentials are absent from the returned tail.
- [ ] Bounds remain 100 lines and 16 KiB or stricter.
- [ ] Missing/read-failure behavior remains empty and non-throwing.
- [ ] Failure assertions expose the bounded redacted tail.

---

## Workstream E — Correct the remaining runtime-shape test gap

Update the malformed-contention table so the named row actually supplies:

```python
{"db": None, "routing_runtime": {...}}
```

Do not normalize `None` payloads into `{}` in a way that changes the intended case before the response object returns it.

Also assert in existing endpoint-failure and non-200 tests:

- `runtime_failures` increments;
- `runtime_successes` does not increment;
- `last_error` is populated appropriately;
- pending, reservations, and contention fields remain `None`.

Keep these assertions inside existing nodes or consolidate an equivalent node elsewhere. Do not increase collection.

### Acceptance criteria

- [ ] `db=None` is exercised literally.
- [ ] Missing, `None`, scalar, and malformed contention structures remain fail-closed.
- [ ] Failure accounting is asserted for exception and non-200 paths.
- [ ] No synthetic zero is introduced.
- [ ] Canonical collection does not increase.

---

## Workstream F — Verification and exact-head closure

### F1. Focused tests

Run:

```bash
uv run pytest \
  tests/unit/test_runtime_validation_runner.py \
  tests/integration/test_runtime_validation_process_smoke.py \
  -q --tb=short --maxfail=1
```

Run the process smoke five consecutive times:

```bash
for i in 1 2 3 4 5; do
  uv run pytest \
    tests/integration/test_runtime_validation_process_smoke.py \
    -q --tb=short --maxfail=1 || exit 1
done
```

### F2. Static and compatibility checks

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run --python 3.11 pytest tests/smoke/ -q --tb=short --maxfail=1
```

### F3. Canonical collection and suite

```bash
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  --collect-only -q

uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
```

Requirements:

- collected nodes are `<= 8454`;
- the real-process smoke remains collected;
- no marker is added merely to exclude new tests;
- the suite remains within the existing 15-minute job timeout.

### F4. Public command

Run from a clean checkout:

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

- exit code is zero;
- exactly one JSON file is retained;
- JSON reports passed gates;
- the child PID is gone;
- no `eggpool-soak-*` directory from the run remains.

Do not present this shared/development-host run as SBC performance evidence.

### F5. Commit and status sequence

Recommended commits:

1. `fix: close runtime validation lifecycle cleanup gaps`
   - reopen Plans 040 through 043;
   - implement Workstreams B through E;
   - keep Plan 044 open.
2. `docs: close runtime validation lifecycle plan`
   - only after all local/focused checks pass;
   - mark Plans 040 through 044 closed and name commit 1 as the implementation commit.

After commit 2 reaches `main`, wait for both exact-head jobs:

```text
check       success
compat-311  success
```

Record the exact commit SHA and job results in the handoff response or PR description. Do not add an evidence file.

### Acceptance criteria

- [ ] All focused tests pass.
- [ ] The real-process smoke passes five consecutive runs.
- [ ] Ruff formatting and lint pass.
- [ ] Pyright passes.
- [ ] Python 3.11 smoke passes.
- [ ] Canonical collection is `<= 8454`.
- [ ] Canonical suite passes within the existing timeout.
- [ ] The documented 30-second command passes from a clean checkout.
- [ ] Ordinary CI still contains only `check` and `compat-311`.
- [ ] Both jobs pass on the final status commit SHA.

## Rejection conditions

Do not close Plan 044 if any of the following is true:

- any fallible setup operation remains outside the lifecycle `try/finally`;
- a config-write or subprocess-launch failure can leave the upstream or work directory alive;
- cleanup suppresses termination, shutdown, close, or removal errors;
- one cleanup failure prevents later cleanup attempts;
- cleanup failure can return `passed=True` or exit zero;
- retained JSON says passed while the returned result failed;
- raw credentials appear in `process_log_tail`;
- redaction removes unrelated diagnostic lines;
- the literal `db=None` response shape remains untested;
- endpoint failure accounting is not asserted;
- a public diagnostic CLI option is added;
- a workflow job, matrix, schedule, or evidence system is added;
- canonical collection exceeds `8454`;
- either exact-head CI job is absent, failing, cancelled, or unverified;
- Plans 040 through 044 are treated as closed before exact-head CI succeeds.

## Definition of done

This line of work is closed when every resource acquired by the runtime-validation runner is owned by one outer lifecycle boundary; partial setup failures and cleanup failures produce truthful, bounded diagnostics without leaking processes, sockets, directories, secrets, or extra files; the existing functional gates remain unchanged; canonical collection remains reduced; and both ordinary CI jobs pass on the exact final status commit.