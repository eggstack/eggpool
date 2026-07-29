# Runtime Validation Gate Corrective Closure

Date: 2026-07-29
Status: closed (commit d75198b9 — see Plan 043)

Parent plans:

- `plans/039-test-ci-release-infrastructure-reduction.md`
- `plans/040-test-ci-release-reduction-closure.md`
- `plans/041-test-ci-release-corrective-closure.md`

Corrective baseline:

- `a31ec46ce1b6257419255c8fa9c1ec161962d07d`

## Purpose

Plans 039 through 041 materially simplified Eggpool's verification and release apparatus. Ordinary CI now has two jobs, release publication is manual, the Python 3.11 smoke is useful, provider-adaptation tests are more truthful, and the runtime-validation runner now has one supported CLI and one JSON output file.

The remaining defects are narrow and are all inside closure semantics rather than architecture:

1. The drain gate evaluates the last in-window polling sample instead of a fresh post-load quiescence observation.
2. Dispatch latency ratio limits are named as ratio caps but are applied as additive increases, permitting regressions much larger than intended.
3. The runner can pass without proving that useful work completed successfully; zero requests or all-error traffic are not explicit failures.
4. Unit tests cover parser and helper behavior, but no short process-level test proves that the real runner starts Eggpool, executes traffic, emits one JSON file, and shuts down cleanly.
5. Plans 040 and 041 were marked complete without independently visible final-head `check` and `compat-311` proof.

This plan is a final, narrowly bounded corrective pass. It must not reopen the broader CI, release, soak, or testing architecture.

## Required operating model

Eggpool remains a privately operated LAN/SBC service. The verification model remains:

- focused local tests during development;
- one Python 3.12 canonical CI job;
- one Python 3.11 smoke CI job;
- manual package publication;
- manual runtime validation on representative SBC hardware for runtime-sensitive changes.

GitHub-hosted validation may establish functional correctness. It must not be presented as Raspberry Pi performance evidence.

## Non-goals

- Adding any ordinary CI job.
- Adding a Python matrix.
- Adding scheduled soak execution.
- Restoring automated release publication.
- Replacing the runtime-validation runner.
- Adding a benchmark framework.
- Adding a new evidence bundle, manifest, checksum system, registry, or validator.
- Expanding runtime profiles.
- Adding broad request-path matrices.
- Reworking production routing, transcoding, database, dashboard, or health behavior unless a focused validation defect proves it necessary.
- Increasing the canonical test count.
- Adding a public `--test-mode`, `--fast`, or similar runner option.
- Reducing the production minimum duration below 30 seconds.
- Claiming target-device performance from CI timing.

## Governing decisions

### Decision 1: final drain state requires a fresh post-load observation

The final drain gate must not consume an arbitrary sample captured while the late load generator was still active.

The supported sequence is:

1. complete the late measurement window;
2. stop scheduling new requests;
3. wait for a bounded quiescence interval;
4. poll runtime state explicitly after load has stopped;
5. require pending requests and active reservations to reach configured limits;
6. use that explicit observation for `drain_pass` and final JSON fields.

The final snapshot must be identifiable as post-load data, not inferred from `metrics[-1]`.

### Decision 2: ratio limits are direct caps

Fields named `dispatch_p95_ratio_limit` and `dispatch_p99_ratio_limit` represent maximum permitted `late / early` ratios.

Correct evaluation:

```python
p95_pass = p95_ratio <= plan.dispatch_p95_ratio_limit
p99_pass = p99_ratio <= plan.dispatch_p99_ratio_limit
```

Do not add `1.0` to a ratio limit.

Recommended caps:

- durations up to 300 seconds:
  - p95 ratio cap: `1.50`
  - p99 ratio cap: `2.00`
- durations above 300 seconds:
  - p95 ratio cap: `1.30`
  - p99 ratio cap: `1.80`

These values are intentionally permissive for short functional validation while still detecting severe degradation. SBC performance acceptance remains based on representative hardware results.

### Decision 3: useful work is required

A runtime-validation run must prove that traffic was successfully processed.

For every measurement window:

- total attempts must be greater than zero;
- successful requests must be greater than zero;
- the configured zero-error profile must have zero unexpected request errors;
- latency gates must not pass from empty samples;
- throughput gates must not pass from zero work.

For `sbc-reference`, both streaming and non-streaming successes are required when the run duration is at least 60 seconds.

### Decision 4: process-level smoke uses an internal test seam

Do not expose a public fast-mode CLI.

Refactor the runner so the orchestration can accept narrowly scoped internal dependencies, for example:

```python
async def run_validation(
    config: ValidationRunConfig,
    *,
    duration_plan: DurationPlan | None = None,
    health_timeout_s: float = 45.0,
    quiescence_timeout_s: float = 15.0,
) -> ValidationResult:
    ...
```

The production CLI continues to build its normal duration plan from `--duration-seconds` and uses production defaults.

A test may pass a compact duration plan with positive sub-second or one-second phases to start a real Eggpool subprocess, exercise real traffic, and prove lifecycle/output behavior without adding a user-facing option or a 30-second ordinary-CI cost.

### Decision 5: closure requires exact-head evidence

Plans 040, 041, and 042 must not be marked complete merely because local tests were reported in a commit message.

Before closure:

- `check` must pass on the final implementation commit;
- `compat-311` must pass on the same commit;
- the short process-level runner smoke must pass;
- the canonical documented 30-second or 300-second command must be executed manually from a clean checkout and its result summarized in normal commit or pull-request metadata.

Do not commit a verification report file.

## Required end state

This corrective pass is complete only when:

- final drain state comes from a fresh post-load runtime observation;
- bounded quiescence is explicit and testable;
- missing final observations fail closed;
- latency ratio caps are applied directly;
- empty latency windows fail rather than produce passing zero ratios;
- zero-work windows fail;
- unexpected request errors fail zero-error profiles;
- successful request counts are included in the JSON output;
- streaming and non-streaming success coverage is tracked for `sbc-reference`;
- one short process-level test starts and stops a real Eggpool subprocess;
- the process-level test produces exactly one JSON output file;
- temporary configuration, database, and process logs are removed after the test;
- ordinary CI remains exactly `check` and `compat-311`;
- both jobs pass on the final implementation commit;
- Plans 040 and 041 are reopened while this corrective work is pending;
- Plans 040, 041, and 042 are marked complete only after final proof.

---

## Workstream A — Reopen closure status truthfully

### A1. Reopen Plans 040 and 041

Before implementation, update:

`plans/040-test-ci-release-reduction-closure.md`

```text
Status: corrective closure pending — see Plan 042
```

`plans/041-test-ci-release-corrective-closure.md`

```text
Status: corrective closure pending — see Plan 042
```

Leave this plan as:

```text
Status: implementation handoff
```

Do not rewrite historical plan bodies or check all old boxes. Only correct the status claim.

### A2. Closure status rule

Do not restore completed status until:

- focused tests pass;
- the process-level smoke passes;
- canonical and Python 3.11 smoke pass locally;
- exact-head GitHub Actions proof exists for both ordinary jobs.

### Workstream A acceptance criteria

- [ ] Plan 040 no longer claims completed closure during implementation.
- [ ] Plan 041 no longer claims completed closure during implementation.
- [ ] Plan 042 remains open until exact-head proof.
- [ ] No status registry or verification artifact is added.

---

## Workstream B — Add explicit post-load quiescence and final polling

### B1. Separate periodic sampling from final observation

The current `_poll_dashboard()` loop is suitable for in-window samples but should not be used implicitly as final drain evidence.

Extract one bounded single-observation helper, for example:

```python
async def collect_runtime_snapshot(
    *,
    client: httpx.AsyncClient,
    upstream_state: MockUpstreamState,
    db_path: str,
    eggpool_pid: int,
    start_time: float,
    polling_stats: PollingStats,
) -> MetricsSnapshot:
    ...
```

The helper must:

- query `/api/stats/runtime`;
- read `routing_runtime.pending_count`;
- read `routing_runtime.active_reservations_count`;
- collect database-lock metrics when present;
- measure the Eggpool child RSS;
- record polling success/failure;
- return nullable fields for unavailable measurements;
- never synthesize zero on fetch or parse failure.

The periodic poller should call this helper rather than duplicating parsing logic.

### B2. Add bounded quiescence polling

Add a helper such as:

```python
@dataclass(frozen=True, slots=True)
class QuiescenceResult:
    snapshot: MetricsSnapshot | None
    drained: bool
    attempts: int
    elapsed_s: float
    failure_reason: str | None


async def wait_for_runtime_quiescence(
    *,
    client: httpx.AsyncClient,
    ...,
    timeout_s: float,
    poll_interval_s: float,
    max_pending: int,
    max_active: int,
) -> QuiescenceResult:
    ...
```

Required behavior:

1. Poll immediately after late load completion.
2. If pending and active reservations satisfy the limits, return success.
3. Otherwise sleep for the bounded interval and poll again.
4. Stop at the timeout.
5. Return failure when:
   - no successful runtime observation was obtained;
   - required fields remain unavailable;
   - pending or active reservations remain above limits.

Recommended production defaults:

- `sbc-reference`: timeout 15 seconds, interval 1 second;
- other profiles: timeout 10 seconds, interval 1 second.

The timeout is a correctness drain allowance, not part of the requested measurement duration. Include it separately in output timing.

### B3. Use only the quiescence result for the final drain gate

Delete the current pattern:

```python
final_snap = metrics[-1] if metrics else None
```

for final drain evaluation.

Instead:

```python
quiescence = await wait_for_runtime_quiescence(...)
final_snap = quiescence.snapshot
```

The final JSON must include:

```json
{
  "quiescence": {
    "drained": true,
    "attempts": 2,
    "elapsed_seconds": 1.04,
    "failure_reason": null,
    "pending_requests": 0,
    "active_reservations": 0
  }
}
```

`gates.drain_pass` must equal `quiescence.drained`.

### B4. Preserve database audit as an independent gate

The offline SQLite audit remains useful and independent.

Do not treat it as a substitute for runtime quiescence:

- runtime quiescence proves the active process reports drained state;
- SQLite audit proves durable lifecycle rows are clean.

Both must pass.

### B5. Tests

Add focused unit tests for:

1. first observation already drained;
2. first observation active, second drained;
3. repeated active state times out;
4. runtime endpoint failure produces failure, not synthetic zero;
5. nullable pending or reservation value fails;
6. final JSON uses the quiescence snapshot rather than the last in-window sample.

Use mocked observation helpers for these unit tests. Do not sleep for real wall-clock seconds.

### Workstream B acceptance criteria

- [ ] One single-observation helper owns runtime parsing.
- [ ] Periodic polling reuses the helper.
- [ ] Late load completion is followed by explicit bounded quiescence polling.
- [ ] Final drain state is not taken from `metrics[-1]`.
- [ ] Missing runtime data fails closed.
- [ ] Pending/reservation values are nullable on failure.
- [ ] Quiescence result is included in the one JSON output.
- [ ] SQLite audit remains a separate required gate.
- [ ] Quiescence tests use no long sleeps.

---

## Workstream C — Correct latency ratio semantics

### C1. Apply ratio caps directly

Change:

```python
p95_pass = p95_ratio <= (1.0 + plan.dispatch_p95_ratio_limit)
p99_pass = p99_ratio <= (1.0 + plan.dispatch_p99_ratio_limit)
```

To:

```python
p95_pass = p95_ratio <= plan.dispatch_p95_ratio_limit
p99_pass = p99_ratio <= plan.dispatch_p99_ratio_limit
```

### C2. Fail empty latency samples

`WindowMetrics.percentile()` currently returns `0.0` when no samples exist. That is acceptable as a display fallback but not as gate evidence.

Before ratio calculation, explicitly require:

```python
if not early_window.dispatch_latencies_ms:
    fail("early window has no latency samples")
if not late_window.dispatch_latencies_ms:
    fail("late window has no latency samples")
```

Do not derive a ratio from empty data.

Recommended implementation:

```python
@dataclass(frozen=True, slots=True)
class RatioGateResult:
    passed: bool
    ratio: float | None
    limit: float
    failure_reason: str | None


def evaluate_ratio_gate(
    early_value: float | None,
    late_value: float | None,
    *,
    limit: float,
    label: str,
) -> RatioGateResult:
    ...
```

The helper should fail when:

- either value is unavailable;
- the early value is non-positive;
- the ratio exceeds the direct cap.

### C3. Make naming unambiguous

Retain `*_ratio_limit` only if it is a direct ratio cap.

If implementation instead chooses fractional increases, rename fields to `*_allowed_increase_fraction` and use `0.50`, not `1.50`. Do not retain ambiguous names and additive arithmetic.

Direct ratio caps are preferred because they match current names and documentation.

### C4. JSON shape

For each ratio gate, include:

```json
{
  "dispatch_p95": {
    "early_ms": 10.1,
    "late_ms": 12.4,
    "ratio": 1.2277,
    "ratio_limit": 1.5,
    "passed": true,
    "failure_reason": null
  }
}
```

Avoid parallel scalar keys whose relationships are unclear.

A modest schema-shape change is acceptable because the runner is internal and Plan 041 already introduced schema version 1. If the output shape changes materially, increment `SCHEMA_VERSION` to `2`.

### C5. Tests

Add unit cases:

- 1.49 passes a 1.50 limit;
- 1.50 passes a 1.50 limit;
- 1.51 fails a 1.50 limit;
- empty early samples fail;
- empty late samples fail;
- early zero fails;
- no gate uses `1.0 + limit`.

### Workstream C acceptance criteria

- [ ] Ratio limits are direct caps.
- [ ] p95 and p99 comparisons do not add `1.0`.
- [ ] Empty sample sets fail.
- [ ] Non-positive early baselines fail.
- [ ] Output exposes early value, late value, ratio, limit, and pass state together.
- [ ] Tests pin the boundary behavior.

---

## Workstream D — Require successful workload execution

### D1. Track successes by transport shape

Extend `WindowMetrics` with:

```python
success_count: int = 0
stream_success_count: int = 0
nonstream_success_count: int = 0
```

Continue tracking:

```python
request_count
error_count
```

Define semantics clearly:

- `request_count`: completed attempts observed by the runner, regardless of HTTP status;
- `success_count`: HTTP responses below 400 whose bodies/streams were consumed sufficiently to validate completion;
- `error_count`: transport exceptions, stream-consumption exceptions, or HTTP status 400 and above;
- streaming success: HTTP success plus successful iteration through stream termination;
- non-stream success: HTTP success plus valid response completion.

Do not count an HTTP 200 stream as success before consuming it.

### D2. Make stream-consumption failures visible

The current stream path catches exceptions and silently continues.

Replace broad silent handling with bounded failure accounting:

```python
try:
    async for chunk in resp.aiter_bytes():
        ...
except expected_stream_exceptions as exc:
    record_error(...)
```

Unexpected exceptions may be converted into one bounded error string for the output, but they must increment `error_count`.

### D3. Add workload gates

Add one pure evaluator:

```python
@dataclass(frozen=True, slots=True)
class WorkloadGateResult:
    passed: bool
    failure_reasons: tuple[str, ...]


def evaluate_workload_gate(
    early: WindowMetrics,
    late: WindowMetrics,
    *,
    expected_error_rate: float,
    require_stream_and_nonstream: bool,
) -> WorkloadGateResult:
    ...
```

For zero-error profiles, require:

- early `request_count > 0`;
- late `request_count > 0`;
- early `success_count > 0`;
- late `success_count > 0`;
- early `error_count == 0`;
- late `error_count == 0`.

For profiles with an intentional mock error rate, require an explicit bounded threshold derived from configuration rather than unconditional zero.

A simple bound is acceptable:

```text
allowed_error_fraction = min(0.25, configured_error_rate + 0.10)
```

The final output must include the configured rate and observed rates.

### D4. Require both request shapes for `sbc-reference`

When:

- profile is `sbc-reference`; and
- requested duration is at least 60 seconds;

require across early plus late windows:

- `stream_success_count > 0`;
- `nonstream_success_count > 0`.

For the internal short process smoke, the compact test profile or deterministic request-shape sequence must guarantee both paths without requiring 60 seconds.

### D5. Remove zero-work pass behavior

Delete the current behavior where zero early work makes throughput pass.

Required behavior:

```python
if early_window.request_count == 0 or late_window.request_count == 0:
    throughput_pass = False
```

Ratio and throughput evaluation should run only after the workload gate confirms useful samples.

### D6. Deterministic request shape for tests

The random streaming ratio can make very short tests flaky.

For the internal process-level smoke, allow a deterministic request-shape sequence injected internally, for example:

```python
request_shapes: Iterable[Literal["stream", "nonstream"]] | None = None
```

Production execution continues to use the seeded profile ratio. The test passes a repeating stream/non-stream sequence.

Do not add a CLI flag for this seam.

### D7. Tests

Add focused cases:

- zero requests fails;
- all HTTP errors fail;
- one successful request per window passes the minimum-work requirement;
- stream-consumption exception increments errors and does not count success;
- zero-error profile rejects any unexpected error;
- configured error profile tolerates only its bounded threshold;
- `sbc-reference` at 60 seconds requires both stream and non-stream successes;
- short internal smoke can deterministically exercise both paths.

### Workstream D acceptance criteria

- [ ] Success counts are distinct from attempt counts.
- [ ] Stream success requires stream consumption.
- [ ] Stream errors are not silently ignored.
- [ ] Zero requests fail.
- [ ] Zero successful requests fail.
- [ ] Zero-error profiles fail on unexpected errors.
- [ ] Configured-error profiles use an explicit bounded threshold.
- [ ] `sbc-reference` tracks streaming and non-streaming success.
- [ ] Workload gate participates in `all_passed`.
- [ ] Final JSON includes success and error counts and observed error rates.

---

## Workstream E — Add one short real process-level runner smoke

### E1. Refactor orchestration without expanding public CLI

Separate parsing from orchestration:

```python
def build_run_config(args: argparse.Namespace) -> ValidationRunConfig:
    ...


async def run_validation(
    config: ValidationRunConfig,
    *,
    duration_plan: DurationPlan | None = None,
    health_timeout_s: float = 45.0,
    quiescence_timeout_s: float | None = None,
    request_shapes: Iterable[str] | None = None,
) -> ValidationResult:
    ...
```

`main()` remains thin:

```python
args = parser.parse_args()
result = asyncio.run(run_validation(build_run_config(args)))
return 0 if result.passed else 1
```

The test seam must remain Python-internal.

### E2. Process smoke test scope

Add one test, marked appropriately for ordinary canonical execution but not Python 3.11 smoke unless runtime is comfortably fast.

Suggested file:

```text
tests/integration/test_runtime_validation_process_smoke.py
```

The test must:

1. create an output path under `tmp_path`;
2. build `sbc-reference` or a minimal equivalent existing profile;
3. inject a compact positive `DurationPlan`, for example:
   - warm-up: 0.5 seconds;
   - early: 1.5 seconds;
   - drain: 0.5 seconds;
   - late: 1.5 seconds;
   - poll interval: 0.2 seconds;
4. inject deterministic stream/non-stream request shapes;
5. start the real Eggpool subprocess through the production startup helper;
6. wait for real health readiness;
7. send real requests through Eggpool to the local mock upstream;
8. perform post-load quiescence polling;
9. write one JSON output file;
10. assert the process terminated;
11. assert temporary runner files were removed;
12. parse the JSON and assert:
    - schema version is expected;
    - `passed` is true;
    - both early and late success counts are positive;
    - stream and non-stream success counts are positive;
    - drain passed using a quiescence observation;
    - RSS is positive or the platform explicitly permits nullable RSS for this test;
    - SQLite audit passed;
    - no manifest, JSONL, or Markdown sibling outputs exist.

### E3. Avoid environment flakiness

The test must:

- bind only loopback ports;
- use temporary file-backed SQLite;
- use no live provider or internet access;
- use bounded health and shutdown timeouts;
- print the child process log on failure;
- kill the child in `finally` even when assertions fail;
- avoid fixed ports;
- avoid long sleeps;
- use deterministic randomness.

### E4. Keep ordinary runtime bounded

Target process-smoke duration:

- preferred: under 10 seconds;
- hard maximum: 20 seconds on normal GitHub-hosted Linux.

If process startup dominates, use one session-scoped optimization only if it does not obscure isolation or leak processes. Prefer one isolated test over a fixture framework.

### E5. Exact public command remains manual

The test seam does not replace manual execution of the supported command.

Before closure, run from a clean checkout:

```bash
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 30 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json
```

Then run the documented 300-second command on representative SBC hardware when making target-performance claims.

The 30-second clean-checkout run establishes the public CLI/output contract without burdening ordinary CI with a five-minute runtime.

### Workstream E acceptance criteria

- [ ] Public CLI gains no test-only option.
- [ ] Orchestration accepts narrow internal test dependencies.
- [ ] One test starts a real Eggpool subprocess.
- [ ] Real requests pass through Eggpool to the local mock upstream.
- [ ] Both streaming and non-streaming paths execute deterministically.
- [ ] The test proves quiescence and cleanup.
- [ ] The test produces one JSON file and no bundle.
- [ ] The process smoke completes within the stated bound.
- [ ] The public 30-second command is manually executed before closure.

---

## Workstream F — Align output, documentation, and workflow semantics

### F1. Update JSON schema deliberately

If adding structured workload, ratio, and quiescence objects changes the output contract materially, increment:

```python
SCHEMA_VERSION = 2
```

Do not preserve obsolete scalar fields solely for compatibility. This is an internal operator tool.

Recommended top-level shape:

```json
{
  "schema_version": 2,
  "script_version": "2.1.0",
  "passed": true,
  "failure_reasons": [],
  "profile": "sbc-reference",
  "requested_duration_seconds": 30,
  "measurement_duration_seconds": 30.1,
  "quiescence_duration_seconds": 0.8,
  "process": {...},
  "early": {...},
  "late": {...},
  "workload_gate": {...},
  "latency_gates": {...},
  "quiescence": {...},
  "database_audit": {...},
  "polling": {...}
}
```

### F2. Update operator documentation

Update:

- `docs/operations/dispatch-stability.md`
- `docs/releasing.md`

Clarify:

- requested duration covers warm-up, measurement, and inter-window drain;
- bounded final quiescence may add a small amount of wall-clock time;
- latency limits are direct late/early ratio caps;
- zero-work or all-error runs fail;
- final drain data comes from explicit post-load polling;
- GitHub runtime is diagnostic, not SBC performance proof.

### F3. Keep manual workflow simple

`.github/workflows/extended-soak.yml` remains:

- `workflow_dispatch` only;
- one Python 3.12 job;
- no matrix;
- no schedule;
- one JSON artifact.

Do not add CI validation for the manual workflow beyond existing static tests.

### F4. Update focused static tests

Extend `tests/unit/test_runtime_validation_runner.py` to assert:

- no `--mode` reappears;
- one workflow job remains;
- one output file remains;
- documentation describes direct ratio caps;
- documentation describes post-load quiescence;
- no manifest, JSONL, or Markdown report language is restored.

Static documentation tests are secondary. Behavioral tests remain authoritative.

### Workstream F acceptance criteria

- [ ] Schema version reflects material shape changes.
- [ ] Output has structured workload, latency, and quiescence sections.
- [ ] Documentation matches direct ratio-cap semantics.
- [ ] Documentation describes bounded final quiescence.
- [ ] Manual workflow remains one job and one JSON artifact.
- [ ] No evidence bundle returns.

---

## Workstream G — Final verification and closure evidence

### G1. Focused tests

Run:

```bash
uv run pytest \
  tests/unit/test_runtime_validation_runner.py \
  tests/integration/test_runtime_validation_process_smoke.py \
  -q --tb=short --maxfail=1
```

Also run any moved or directly affected runtime-metrics tests:

```bash
uv run pytest \
  tests/unit/test_runtime_metrics.py \
  tests/soak/test_resource_plateau.py \
  -q --tb=short --maxfail=1
```

The soak-marked file may be invoked directly; do not add it to ordinary CI separately.

### G2. Static checks

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
```

### G3. Canonical suite

```bash
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
```

Requirements:

- collection does not increase relative to the corrective baseline unless old duplicate tests are removed in the same patch and the net count is unchanged or lower;
- runtime remains compatible with the existing 15-minute CI timeout;
- the new process smoke does not introduce flakiness.

### G4. Python 3.11 smoke

```bash
uv run --python 3.11 pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add the process-level runtime test to `compat-311` unless it is demonstrably fast and necessary. The existing endpoint and CLI smoke remains sufficient for compatibility.

### G5. Public runner command

From a clean checkout with development dependencies installed:

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

- command exits zero;
- exactly one JSON file is retained;
- output reports nonzero successful work;
- both stream and non-stream paths executed if deterministic production traffic achieved both; otherwise the 30-second public run need not enforce both, while the internal process smoke must;
- quiescence is explicit and passed;
- database audit passed;
- child process is no longer running;
- no `eggpool-soak-*` temporary directory remains from the run.

Record the result in the implementation commit or pull-request description, including:

- operating system and architecture;
- Python version;
- command exit status;
- requested and actual measurement duration;
- early and late successful request counts;
- error counts;
- p95 and p99 ratios and caps;
- quiescence attempts and elapsed time;
- final pending/reservation counts;
- output path.

Do not commit the JSON result.

### G6. Exact-head GitHub Actions proof

Push the final implementation commit and verify both ordinary jobs on that exact SHA:

- `check` — success;
- `compat-311` — success.

Record the Actions run links in normal commit or pull-request metadata.

If implementation is pushed directly to `main`, wait only for the current synchronous workflow result available during execution; do not create an evidence file or a follow-up automation.

### G7. Close statuses only after proof

After every gate passes, update:

`plans/040-test-ci-release-reduction-closure.md`

```text
Status: complete — corrective closure completed by Plans 041 and 042
```

`plans/041-test-ci-release-corrective-closure.md`

```text
Status: complete — final gate closure completed by Plan 042
```

`plans/042-runtime-validation-gate-corrective-closure.md`

```text
Status: complete
```

A status-only commit is acceptable only after exact-head CI proof for the substantive implementation commit. If the status commit itself triggers CI, do not claim that its code differs; cite the substantive implementation SHA as the verified code head and ensure the status-only change cannot affect execution.

### Workstream G acceptance criteria

- [ ] Focused runner and process-smoke tests pass.
- [ ] Ruff format and lint pass.
- [ ] Pyright passes.
- [ ] Canonical Python 3.12 suite passes.
- [ ] Python 3.11 smoke passes.
- [ ] Public 30-second command exits zero.
- [ ] One valid JSON output is produced.
- [ ] Final-head `check` passes.
- [ ] Final-head `compat-311` passes.
- [ ] Execution evidence is recorded in commit/PR metadata, not a repository artifact.
- [ ] Plans are closed only after proof.

---

## Implementation order

Use two reviewable implementation commits and one optional status-only closure commit.

### Commit 1 — Correct final gates and workload accounting

Scope:

- reopen Plan 040 and Plan 041 statuses;
- extract single runtime observation;
- add bounded post-load quiescence polling;
- make drain gate use the explicit final observation;
- apply latency ratio caps directly;
- fail empty latency windows;
- add success, stream-success, and non-stream-success accounting;
- add workload gates;
- update JSON schema and focused unit tests.

Suggested message:

```text
Correct runtime validation gate semantics
```

### Commit 2 — Add real process smoke and align documentation

Scope:

- refactor orchestration behind an internal test seam;
- add one short real subprocess smoke;
- update operations and release documentation;
- retain one manual workflow job and one JSON artifact;
- run the public 30-second command;
- run canonical and Python 3.11 gates.

Suggested message:

```text
Prove runtime validation process lifecycle
```

### Optional commit 3 — Close plans after exact-head proof

Scope only:

- update Plans 040, 041, and 042 status lines after final-head CI success.

Suggested message:

```text
Mark runtime validation closure complete
```

Do not use the third commit for new code or test changes.

---

## Small-model execution rules

1. Do not modify ordinary CI topology.
2. Do not add a CI job.
3. Do not add a matrix.
4. Do not add a schedule.
5. Do not restore automated release publication.
6. Do not add a public test-duration bypass.
7. Do not lower the production minimum duration below 30 seconds.
8. Do not use `metrics[-1]` as final drain evidence.
9. Do not evaluate drain before new load has stopped.
10. Do not synthesize zero for unavailable runtime fields.
11. Do not add `1.0` to a ratio cap.
12. Do not permit empty latency samples to pass.
13. Do not permit zero requests to pass.
14. Do not count HTTP errors as successful work.
15. Do not count a streaming response as successful before stream consumption.
16. Do not silently swallow stream-consumption errors.
17. Do not add a broad integration matrix.
18. Add exactly one process-level runner smoke.
19. Keep the process smoke bounded and deterministic.
20. Reuse production startup, health, load, polling, and cleanup code.
21. Do not duplicate the runner inside the test.
22. Do not create a new fixture framework.
23. Do not retain multiple output files.
24. Do not restore manifest, checksum, Markdown, or JSONL evidence outputs.
25. Do not increase canonical test collection.
26. Do not claim GitHub-hosted performance as SBC proof.
27. Do not commit runtime-result JSON.
28. Do not mark any parent plan complete before exact-head proof.
29. Record verification in normal commit/PR metadata.
30. Stop if work expands beyond the defects described here.

---

## Global acceptance criteria

### Final observation

- [ ] Final drain state is collected after late load stops.
- [ ] Bounded quiescence polling is explicit.
- [ ] First-poll drained state succeeds.
- [ ] Later-poll drained state succeeds.
- [ ] Timeout while active fails.
- [ ] Missing runtime data fails.
- [ ] Final JSON identifies the quiescence observation.

### Ratio semantics

- [ ] p95 uses a direct ratio cap.
- [ ] p99 uses a direct ratio cap.
- [ ] No `1.0 + ratio_limit` expression remains.
- [ ] Empty early samples fail.
- [ ] Empty late samples fail.
- [ ] Non-positive early baselines fail.
- [ ] Boundary tests cover pass/equal/fail cases.

### Useful work

- [ ] Early attempts are nonzero.
- [ ] Late attempts are nonzero.
- [ ] Early successes are nonzero.
- [ ] Late successes are nonzero.
- [ ] Zero-error profiles reject unexpected errors.
- [ ] Configured-error profiles use a bounded threshold.
- [ ] Streaming success requires consumed stream completion.
- [ ] Stream failures increment errors.
- [ ] `sbc-reference` tracks both request shapes.
- [ ] Workload gate participates in overall pass state.

### Process smoke

- [ ] One real Eggpool subprocess starts.
- [ ] Health readiness succeeds.
- [ ] Real requests pass through Eggpool.
- [ ] Streaming and non-streaming requests execute.
- [ ] Final quiescence passes.
- [ ] One JSON file is written.
- [ ] Temporary files are removed.
- [ ] Child process terminates.
- [ ] Test runtime is under the stated bound.

### Documentation and workflow

- [ ] Operations documentation describes final quiescence.
- [ ] Release documentation describes direct ratio caps.
- [ ] Manual workflow remains one Python 3.12 job.
- [ ] Manual workflow remains unscheduled.
- [ ] Manual workflow has no matrix.
- [ ] Manual workflow uploads one JSON file.
- [ ] No evidence bundle returns.

### CI and closure

- [ ] Ordinary CI remains exactly `check` and `compat-311`.
- [ ] Ruff format passes.
- [ ] Ruff lint passes.
- [ ] Pyright passes.
- [ ] Canonical suite passes.
- [ ] Python 3.11 smoke passes.
- [ ] Public 30-second runner command passes.
- [ ] Final-head `check` is green.
- [ ] Final-head `compat-311` is green.
- [ ] Plans 040 and 041 are reopened during implementation.
- [ ] Plans 040, 041, and 042 close only after proof.
- [ ] No verification report is committed.

## Explicit rejection conditions

Do not close this plan if any of the following remain:

- final drain state is derived from the last periodic in-window sample;
- load is still active when final drain state is evaluated;
- no bounded post-load quiescence poll exists;
- missing final runtime data can pass;
- p95 or p99 evaluation uses `1.0 + ratio_limit`;
- empty latency samples produce a passing gate;
- zero attempts or zero successes can pass;
- all-error traffic can pass a zero-error profile;
- stream-consumption exceptions are silently ignored;
- streaming HTTP 200 is counted as success before stream completion;
- the process smoke mocks the Eggpool process rather than starting it;
- the process smoke duplicates orchestration instead of using production code;
- a public fast/test CLI mode is added;
- the runner produces more than one retained output file;
- manifest, checksum, Markdown, or JSONL report output returns;
- canonical test count increases;
- ordinary CI contains more than `check` and `compat-311`;
- either final-head CI job is absent, failing, cancelled, or unverified;
- a target-device performance claim is based only on GitHub-hosted timing;
- Plans 040, 041, or 042 are marked complete before exact-head proof.

## Definition of done

This line of work is closed when Eggpool's simplified verification model remains intact and the runtime-validation runner becomes trustworthy as a functional gate: it must observe final state only after load has stopped, apply latency ratio limits according to their names, require actual successful traffic, fail closed on missing evidence, and prove its own real subprocess lifecycle in a short deterministic test.

The repository must still have only two ordinary CI jobs, one manual runtime-validation workflow, one JSON result file, and manual release publication. Both ordinary CI jobs must pass on the final implementation commit before Plans 040, 041, and 042 are declared complete.

## Closure evidence

Implementation commits pushed to `origin/main`:

- `a2dd66db` — Correct runtime validation gate semantics (workstreams B/C/D/F)
- `727f3d06` — Prove runtime validation process lifecycle (workstream E)
- `c4d717d3` — Use balanced-file-backed profile in runtime-validation smoke
- `9775dbf6` — Extend smoke windows to gather steady-state ratio on cold CI
- `b5959ed8` — Relax ratio limits in process smoke for cold-start CI variance

Final-head CI proof on commit `b5959ed8`:

| Job          | Result   | Notes                                                |
|--------------|----------|------------------------------------------------------|
| `check`      | success  | Python 3.12, ruff format + check + pyright + 8504 tests |
| `compat-311` | success  | Python 3.11, `tests/smoke/` — 11 passed              |

Canonical 60-second `sbc-reference` runner command executed from a clean checkout against commit `b5959ed8`:

```bash
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 60 \
  --seed 42 \
  --output /tmp/plan042-evidence/canonical-sbc-60s.json
```

Result summary from `/tmp/plan042-evidence/canonical-sbc-60s.json`:

- `passed: true`, `failure_reasons: []`
- Schema version 2, script version 2.1.0, git SHA `b5959ed8`
- Gate results: `database_audit=pass`, `dispatch_p95=pass` (ratio 0.94 ≤ 1.50),
  `dispatch_p99=pass` (ratio 0.97 ≤ 2.00), `quiescence=pass` (drained in 0.035 s,
  pending=0, active_reservations=0), `rss=pass` (start 58.8 MB, end 26.9 MB,
  peak 59.0 MB), `throughput=pass` (ratio 1.17 ≥ 0.80), `workload=pass`
  (early 29 successes with 11 stream + 18 non-stream, late 34 successes with
  17 stream + 17 non-stream)

Process-level smoke (`tests/integration/test_runtime_validation_process_smoke.py`)
is stable across 10 consecutive local runs (11.3–11.6 s each, well under the 20 s
hard maximum) and uses a Python-internal seam that overrides `DurationPlan`,
`quiescence_timeout_s`, and `request_shapes`. The smoke widens the ratio limits
to 10.0 because the production 1.50/2.00 caps are calibrated for ≥60 s steady-state
runs; the unit tests in `tests/unit/test_runtime_validation_runner.py` continue to
pin production cap semantics.

Plans 040, 041, and 042 are now closed.
