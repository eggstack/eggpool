# Final Test, CI, and Release Corrective Closure

Date: 2026-07-28
Status: final corrective closure pending — see Plan 043

Parent plans:

- `plans/039-test-ci-release-infrastructure-reduction.md`
- `plans/040-test-ci-release-reduction-closure.md`

Corrective baseline:

- `a8953026e5eb7aeb3a35ac1e5a915b9ddaf15815`

## Purpose

Plan 040 materially improved the repository. Ordinary CI remains reduced to two jobs, several false end-to-end matrices were deleted, the canonical suite was reduced and accelerated, Python 3.11 smoke coverage was expanded, manual release documentation was added, and the manual soak workflow was collapsed to one job.

Plan 040 was nevertheless marked complete while explicit rejection conditions remained true. This plan is the final, narrow corrective closure for those remaining defects.

The remaining scope is limited to four concrete problems:

1. The documented and workflow runtime-validation commands pass `--duration-seconds`, but `scripts/run_dispatch_stability_soak.py` does not accept that option.
2. The workflow and documentation treat `--output` as a JSON file, while the runner treats it as a directory and still writes a multi-file evidence bundle.
3. Resource metrics are not truthful: Linux `ru_maxrss` is mislabeled as bytes, the runner measures its own process rather than the Eggpool child process, and unavailable metrics can silently become zero and pass gates.
4. At least two active files under `tests/integration/` still claim end-to-end or request-pipeline coverage while exercising only pure contract-resolution and adaptation functions.

This plan must correct those defects without reopening the broader CI architecture, re-expanding the test suite, or introducing another evidence system.

## Required operating model

Eggpool is a privately operated, LAN-hosted service primarily intended for Raspberry Pi and similar SBC hardware.

The final verification model remains:

- focused local tests during development;
- one canonical Python 3.12 CI job;
- one narrow Python 3.11 compatibility smoke job;
- manual package release;
- manual runtime validation on representative hardware when request-path, database, streaming, reload, concurrency, or dependency behavior changes.

Shared GitHub runners are useful for correctness checks. They are not authoritative performance evidence for Raspberry Pi-class systems.

## Non-goals

- Adding any ordinary CI job.
- Adding a Python version matrix.
- Restoring automated publication.
- Restoring scheduled soak workflows.
- Building a new benchmark framework.
- Creating a test registry, requirement matrix, evidence manifest, checksum bundle, or exact-head validator.
- Reworking unrelated production request routing.
- Rewriting the entire soak runner if a small coherent reduction is sufficient.
- Preserving historical output formats that have no current operator use.
- Increasing the canonical test count.
- Adding broad new integration matrices.
- Treating GitHub-hosted timing as target-device proof.

## Governing decisions

The implementation must follow these decisions unless a demonstrated code constraint makes one impossible.

### Decision 1: one canonical runtime-validation CLI

The supported command is:

```bash
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 300 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json
```

The command must work exactly as written from a clean checkout with development dependencies installed.

`--duration-seconds` is the canonical duration interface. Remove `--mode` and the old duration-mode abstraction unless another active caller is found. Because this is an internal repository script rather than a published API, compatibility with unused historical invocations is not required.

### Decision 2: one output file

`--output` names one JSON file, not a directory.

The runner may use an internal temporary directory for process logs, SQLite, and configuration while running. Those temporary files must be deleted on completion. The retained operator result is one concise JSON document plus a human-readable terminal summary.

Do not retain or recreate:

- `manifest.json`;
- `summary.md`;
- `metrics.jsonl`;
- checksum generation;
- a directory-shaped artifact contract;
- multiple artifact files for routine validation.

### Decision 3: measure the Eggpool child process

RSS and process resource measurements must describe the Eggpool subprocess, not the soak-runner process.

Use a small standard-library helper such as:

```python
def read_process_rss_bytes(pid: int) -> int | None:
    ...
```

Preferred behavior:

- Linux: parse `VmRSS` from `/proc/<pid>/status`; the value is KiB and must be multiplied by 1024.
- macOS/BSD fallback: run `ps -o rss= -p <pid>`; the value is KiB and must be multiplied by 1024.
- unsupported or failed measurement: return `None`.

Do not use `resource.getrusage(resource.RUSAGE_SELF)` as Eggpool RSS.

### Decision 4: unknown is not zero

Any metric that was not successfully observed must be represented as `null`/`None`, not `0`.

This applies to:

- Eggpool RSS;
- host total memory;
- pending request count;
- active reservation count;
- database-lock metrics;
- any final snapshot needed for a pass/fail gate.

A gate that depends on unavailable data must not silently pass.

For the canonical `sbc-reference` validation profile, missing required resource or drain metrics must make the run fail with an actionable reason.

### Decision 5: pure tests belong at the unit layer

A test that invokes only pure functions such as `resolve_control_contract()` and `adapt_thinking_controls()` is a unit test, even when it covers several decision branches.

Only tests that enter an Eggpool endpoint or wire actual Eggpool components may retain `integration`, `request_path`, or `e2e` naming.

## Required end state

The corrective closure is complete only when all of the following are true:

- the documented local runtime-validation command executes successfully;
- the manual workflow uses the same supported runner arguments;
- the workflow uploads the actual single JSON output file;
- the runner accepts `--duration-seconds` and no active documentation uses an unsupported option;
- `--output` has one consistent file-path meaning;
- no manifest, checksum, Markdown summary, or JSONL artifact bundle is generated;
- RSS measures the Eggpool child process in bytes;
- Linux KiB values are converted to bytes correctly;
- unavailable values are explicit `null`, never synthetic zero;
- missing required metrics cannot produce a passing drain/resource gate;
- pure provider-adaptation tests are moved to unit scope and truthfully named;
- one representative MiniMax/OpenCode Go request-local isolation regression enters the real Eggpool request path;
- ordinary CI remains exactly `check` and `compat-311`;
- both jobs pass on the final implementation commit;
- Plan 040 is not marked complete until this plan is complete.

---

## Workstream A — Reopen the incorrectly closed status

Before changing implementation code, correct planning truthfulness.

Update `plans/040-test-ci-release-reduction-closure.md`:

```text
Status: corrective closure pending — see Plan 041
```

Do not rewrite Plan 040 or check every historical checkbox. The status line is sufficient to prevent an inaccurate completed-plan claim while corrective work is underway.

Leave Plan 041 as `Status: implementation handoff` until every acceptance criterion in this file is satisfied.

### Workstream A acceptance criteria

- [ ] Plan 040 no longer claims complete while rejection conditions remain.
- [ ] Plan 041 remains open during implementation.
- [ ] No separate status artifact or registry is created.

---

## Workstream B — Replace the broken soak CLI contract

### B1. Inventory active callers

Search before editing:

```bash
grep -RIn "run_dispatch_stability_soak.py" \
  .github docs README.md AGENTS.md .opencode scripts tests plans \
  --exclude-dir=.git

grep -RInE -- "--mode|--duration-seconds|--output" \
  .github docs README.md AGENTS.md .opencode scripts \
  --exclude-dir=.git
```

Historical examples under completed plans do not need modification unless they are presented as current execution instructions. Active workflow and operator documentation must be corrected.

### B2. Replace duration modes with direct duration

Modify `scripts/run_dispatch_stability_soak.py` so `_build_parser()` accepts:

```text
--profile PROFILE
--duration-seconds INTEGER
--seed INTEGER
--output FILE
--verbose
```

Recommended validation:

- minimum duration: 30 seconds;
- default duration: 300 seconds for direct local invocation;
- reject zero, negative, non-integer, and unreasonably low durations with parser-visible errors;
- do not silently clamp invalid input.

Remove the `--mode` argument and delete `DURATION_MODES` if no active code remains dependent on it.

### B3. Derive bounded phases from total duration

The runner currently depends on warm-up, early-window, drain, and late-window durations. Derive these from `duration_seconds` in one small pure helper.

Suggested contract:

```python
@dataclass(frozen=True, slots=True)
class DurationPlan:
    total_s: float
    warmup_s: float
    early_window_s: float
    drain_s: float
    late_window_s: float


def build_duration_plan(total_s: float) -> DurationPlan:
    ...
```

The helper must guarantee:

- all phases are positive for accepted durations;
- phase totals do not exceed the requested duration except for a small, documented process startup/shutdown allowance;
- early and late measurement windows are equal;
- warm-up and drain are bounded so short runs still spend most of their time measuring;
- the same helper is unit tested.

One acceptable proportional policy is:

```text
warm-up: min(60 seconds, max(5 seconds, total × 10%))
drain:   min(30 seconds, max(2 seconds, total × 5%))
remaining duration split equally between early and late windows
```

The exact arithmetic may differ, but it must be deterministic and documented in code.

### B4. Make output a file

Interpret `args.output` as a file path:

```python
output_path = Path(args.output)
output_path.parent.mkdir(parents=True, exist_ok=True)
```

Do not call `mkdir()` on the output path itself.

Write one JSON object atomically:

```python
tmp_output = output_path.with_suffix(output_path.suffix + ".tmp")
tmp_output.write_text(...)
tmp_output.replace(output_path)
```

The JSON should contain only useful operator data:

```json
{
  "schema_version": 1,
  "passed": true,
  "failure_reasons": [],
  "git_sha": "...",
  "profile": "sbc-reference",
  "seed": 42,
  "requested_duration_seconds": 300,
  "actual_duration_seconds": 301.2,
  "platform": {"system": "Linux", "machine": "aarch64", "python": "3.12.4"},
  "process": {
    "eggpool_pid": 1234,
    "rss_start_bytes": 123,
    "rss_end_bytes": 456,
    "rss_peak_bytes": 789
  },
  "early": {...},
  "late": {...},
  "gates": {...},
  "database_audit": {...}
}
```

Do not include secrets, request content, temporary file paths, API keys, or complete environment dumps.

### B5. Delete obsolete artifact machinery

Delete helpers and imports used only for the old bundle:

- `_write_metrics_jsonl`;
- `_write_summary_md`;
- `_compute_manifest`;
- `_write_manifest`;
- `hashlib` if no longer needed for a real security function;
- schema fields that exist only to support the removed bundle;
- obsolete command examples using `--mode nightly` or output directories.

A single JSON schema version is acceptable. A checksum manifest is not.

### B6. Correct terminal behavior

The runner must:

- print a short summary containing pass/fail, request counts, early/late throughput, relevant latency ratios, final drain state, and output path;
- exit `0` only when all required gates pass;
- exit nonzero on argument errors, startup failure, missing required metrics, failed database audit, or failed stability gates;
- always terminate the Eggpool process and mock upstream;
- remove the internal temporary working directory;
- retain the requested output JSON when a run reaches report generation, including failed gates.

### Workstream B acceptance criteria

- [ ] `--duration-seconds` is accepted and documented.
- [ ] `--mode` is removed from active code and active documentation unless a proven caller requires it.
- [ ] Invalid duration input fails clearly.
- [ ] Duration partitioning is deterministic and unit tested.
- [ ] `--output` names one JSON file.
- [ ] Parent directories are created as needed.
- [ ] Output is written atomically.
- [ ] No artifact directory, manifest, Markdown summary, or JSONL series is emitted.
- [ ] The command shown in this plan runs successfully.

---

## Workstream C — Make resource and drain metrics truthful

### C1. Measure the Eggpool process RSS

Add one narrowly scoped helper module or keep the helper inside the runner. Do not add a general monitoring package.

Implement:

```python
def read_process_rss_bytes(pid: int) -> int | None:
    """Return current RSS for the requested process in bytes."""
```

Linux implementation:

1. Open `/proc/<pid>/status`.
2. Find the line beginning with `VmRSS:`.
3. Parse the integer and unit.
4. Support `kB`/`KiB` as 1024-byte units.
5. Return `None` for missing process, malformed values, or unsupported units.

macOS/BSD fallback:

1. Execute `ps -o rss= -p <pid>` with a short timeout.
2. Parse KiB.
3. Multiply by 1024.
4. Return `None` on command or parse failure.

The helper must never return `0` merely because measurement failed.

Pass `proc.pid` into the dashboard poller. Remove the current `RUSAGE_SELF` RSS sampling.

### C2. Correct or remove `_get_rss_bytes()` in soak tests

`tests/soak/test_resource_plateau.py` currently states the Linux/macOS semantics backwards.

Correct the helper:

```python
def _get_rss_bytes() -> int | None:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return int(usage.ru_maxrss)
    if sys.platform.startswith("linux"):
        return int(usage.ru_maxrss) * 1024
    return None
```

Tests that require RSS must skip with an explicit reason when RSS is unavailable. They must not substitute zero.

This test helper measures the current pytest process and is acceptable for its own in-process resource test. The process-level runner must measure the Eggpool child process instead.

### C3. Use nullable metric types

Change metric fields that can be unavailable from `int`/`float` defaults to nullable types:

```python
rss_bytes: int | None
pending_requests: int | None
active_reservations: int | None
db_lock_wait_p95_ms: float | None
host_memory_total_bytes: int | None
```

Do not initialize `pending_requests` or `active_reservations` to zero before a network request.

Use:

```python
pending: int | None = None
active_reservations: int | None = None
```

Populate them only after a successful response with valid fields.

### C4. Track polling failures explicitly

The poller must record bounded diagnostics rather than swallowing all exceptions.

A small summary is sufficient:

```json
"polling": {
  "summary_successes": 20,
  "summary_failures": 1,
  "runtime_successes": 20,
  "runtime_failures": 1,
  "last_error": "...redacted/bounded..."
}
```

Do not record unbounded exception lists or stack traces in the result.

### C5. Fail closed for required gate inputs

Final drain evaluation must distinguish:

- observed zero pending requests;
- unavailable pending-request metric;
- no final snapshot.

Required logic:

```text
if no final snapshot:
    drain gate fails: "no final runtime snapshot"
elif pending_requests is None or active_reservations is None:
    drain gate fails: "drain metrics unavailable"
else:
    evaluate numeric limits
```

For `sbc-reference`, RSS availability is required. If the child RSS cannot be measured, the run must fail with `resource metric unavailable`.

Other profiles may declare RSS optional only if that is an explicit profile field. Do not infer optionality from a zero value.

### C6. Correct host-memory handling

`_memory_total_bytes()` must return `int | None`.

- Linux: use `os.sysconf` only when values are valid positive integers.
- macOS: use a correct supported command or return `None`.
- Do not return zero on failure.
- Do not use a Linux libc path as a macOS fallback.

No pass/fail gate should use host memory unless that gate is explicitly defined.

### C7. Add focused metric tests

Add small unit tests for:

- Linux `/proc` `VmRSS: 1234 kB` becomes `1_263_616` bytes;
- malformed or absent `VmRSS` returns `None`;
- `ps` KiB conversion becomes bytes;
- unavailable host memory returns `None`;
- missing dashboard metrics fail the drain gate;
- observed zero drain metrics pass when limits allow;
- no final snapshot fails rather than passing;
- JSON serialization preserves unavailable values as `null`.

Use dependency injection or pure parsing helpers. Do not require a real `/proc` process in unit tests.

### Workstream C acceptance criteria

- [ ] Process-level RSS describes the Eggpool subprocess.
- [ ] Linux KiB is multiplied by 1024.
- [ ] macOS/BSD `ps` KiB is multiplied by 1024.
- [ ] Unsupported measurement returns `None`.
- [ ] No unavailable resource or drain value is represented as zero.
- [ ] Missing gate inputs fail closed with actionable reasons.
- [ ] Resource unit/parser tests pass.
- [ ] `tests/soak/test_resource_plateau.py` no longer contains the inverted Linux/macOS conversion.

---

## Workstream D — Align workflow and documentation with the executable contract

### D1. Manual workflow

Keep `.github/workflows/extended-soak.yml` manual-only and single-job.

The final workflow must have:

- one `workflow_dispatch` trigger;
- one Python 3.12 job;
- no matrix;
- no schedule;
- no PR trigger;
- one runner invocation;
- one uploaded JSON result.

Use a bounded duration input. Prefer a choice to avoid values that exceed the job timeout:

```yaml
duration-seconds:
  description: "Validation duration"
  required: false
  default: "1800"
  type: choice
  options:
    - "300"
    - "1800"
    - "3600"
```

Set the job timeout high enough for the largest choice plus startup/shutdown allowance.

Invocation:

```yaml
uv run python scripts/run_dispatch_stability_soak.py \
  --profile "${{ inputs.profile || 'sbc-reference' }}" \
  --duration-seconds "${{ inputs.duration-seconds || '1800' }}" \
  --seed "${{ inputs.seed || '42' }}" \
  --output /tmp/eggpool-runtime-validation.json
```

Upload exactly `/tmp/eggpool-runtime-validation.json`.

The upload step may use `if: always()` so a failed-gate report remains available, but the runner step itself must retain its nonzero exit status.

### D2. Release documentation

Update `docs/releasing.md` so the SBC validation command is executable and matches the runner.

Document:

- the command;
- that `--output` is a JSON file;
- that RSS is the Eggpool child-process RSS;
- that `null` means unavailable and prevents release validation from passing when the metric is required;
- that GitHub workflow timing is diagnostic only;
- that representative SBC output is authoritative for target performance claims.

Do not require runtime validation for documentation-only releases.

### D3. Development documentation

Update `AGENTS.md` and `.opencode/skills/development/SKILL.md` only if their current commands or descriptions are inaccurate.

Keep the documentation short. Link to `docs/releasing.md`; do not duplicate the full procedure in multiple files.

### D4. CLI contract verification

Add fast tests that verify active commands do not drift again:

- parser accepts the documented argument set;
- parser rejects `--mode` after removal;
- parser rejects invalid duration values;
- output-path helper treats the path as a file;
- workflow text contains only supported runner options;
- release-document example contains only supported runner options.

Do not implement these as brittle full-source snapshots. Parse the runner arguments directly and use narrow text checks only for the two active command surfaces.

### Workstream D acceptance criteria

- [ ] Workflow and release guide invoke a supported CLI.
- [ ] Workflow remains one manual Python 3.12 job.
- [ ] Workflow uploads the actual JSON file.
- [ ] Largest workflow duration fits inside timeout.
- [ ] Documentation describes the output and metric semantics accurately.
- [ ] A fast regression prevents workflow/documentation CLI drift.

---

## Workstream E — Correct remaining false integration and E2E labels

### E1. Reclassify pure adaptation tests

Known targets:

- `tests/integration/test_thinking_control_contract_e2e.py`
- `tests/integration/test_provider_adaptation_actual_identity.py`
- `tests/integration/test_thinking_control_compatibility_retry.py`

Inspect each test, not only the filename.

When a file invokes only pure capability/contract/adaptation functions:

- move it under `tests/unit/`;
- remove `pytest.mark.integration`;
- remove `pytest.mark.request_path` unless the test actually traverses the request path;
- remove “end-to-end,” “full pipeline,” “selected provider request,” and shared-state claims that are not exercised;
- merge duplicate cases with existing unit files where practical;
- retain exhaustive pure decision coverage at the unit layer.

Suggested destinations:

- merge contract resolution cases into `tests/unit/test_builtin_contracts.py` or a truthful `test_thinking_control_contract_resolution.py`;
- merge adaptation identity/policy cases into `tests/unit/test_provider_request_adaptation.py`;
- move compatibility-retry deferral to `tests/unit/test_thinking_control_compatibility_retry.py`.

Do not retain two large near-duplicate pure-function files merely under new names.

### E2. Add one genuine request-local isolation regression

Use the existing shared real-runtime helper rather than constructing another full fixture hierarchy.

Add one focused integration test that enters the Eggpool ASGI endpoint and models the original operational risk:

1. Configure/select an `opencode-go` provider identity for `MiniMax-M3`.
2. Configure a deterministic unsupported-control policy, preferably strict local rejection.
3. Capture relevant shared state before the request:
   - account health/eligibility;
   - quarantine state;
   - active reservations;
   - pending request/attempt rows;
   - circuit/backoff state where directly observable.
4. POST an OpenAI-compatible request containing unsupported thinking control through `/v1/chat/completions`.
5. Assert the request receives the configured bounded client outcome.
6. Assert no unintended shared account/model/circuit/quarantine penalty was applied.
7. Send a subsequent plain request through Eggpool.
8. Mock the upstream response and assert the subsequent request succeeds.
9. Assert final durable state contains no active reservation or pending request leak.

The test need not recreate every historical matrix row. It exists to prove cross-component wiring and request-local isolation for the representative MiniMax/OpenCode Go defect.

If the current fixture cannot configure provider identity, make the smallest extension:

- introduce one small runtime-app factory used by the existing fixture and this specialized test; or
- parameterize the helper with a narrow immutable spec.

Do not build a general plugin or test application framework.

### E3. Repository-wide truthfulness scan

Run:

```bash
find tests/integration -type f -name '*e2e*.py' -print

grep -RInE 'end-to-end|full pipeline|full request path|shared-state' \
  tests/integration

grep -RIn 'pytest.mark.integration' tests/integration
```

Review matches manually.

An integration test may call the coordinator directly if it is genuinely testing multiple Eggpool components. A file must not claim externally exposed end-to-end behavior unless it enters the application endpoint.

### Workstream E acceptance criteria

- [ ] Pure adaptation/contract tests live under unit scope.
- [ ] No pure-function file retains integration or request-path markers.
- [ ] No active file claims E2E/full-pipeline behavior it does not exercise.
- [ ] One representative MiniMax/OpenCode Go isolation test enters the ASGI endpoint.
- [ ] The representative failure does not poison subsequent requests or shared state.
- [ ] No broad replacement integration matrix is added.
- [ ] Canonical test count does not increase overall.

---

## Workstream F — Verify without rebuilding infrastructure

### F1. Focused checks during implementation

Run focused checks after each workstream:

```bash
uv run ruff format --check \
  scripts/run_dispatch_stability_soak.py \
  tests/soak/test_resource_plateau.py \
  tests/unit \
  tests/integration

uv run ruff check \
  scripts/run_dispatch_stability_soak.py \
  tests/soak/test_resource_plateau.py \
  tests/unit \
  tests/integration

uv run pyright src/ scripts/
```

Run focused tests for:

- duration planning;
- process RSS parsing;
- unavailable metric handling;
- parser contract;
- moved provider-adaptation unit tests;
- the new real-runtime MiniMax isolation regression;
- Python 3.11 smoke.

### F2. Execute the real local command

From a clean checkout or clean working tree:

```bash
uv sync --frozen --extra dev

uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 60 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json
```

Use 60 seconds for implementation verification. The release guide may recommend 300 seconds or longer on target hardware.

Verify:

```bash
test -f /tmp/eggpool-runtime-validation.json
python -m json.tool /tmp/eggpool-runtime-validation.json >/dev/null
test ! -d /tmp/eggpool-runtime-validation.json
```

Inspect the result and confirm:

- one JSON object exists;
- no secret values are present;
- RSS fields are bytes or null;
- process PID identifies Eggpool;
- required metric availability is explicit;
- pass/fail reasons are coherent;
- no manifest, Markdown, JSONL, or sibling artifact directory was created.

### F3. Canonical local gates

Run the exact ordinary CI commands:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Record in the implementation commit or pull-request description:

- canonical collected count before and after;
- canonical duration;
- smoke count and duration;
- focused runtime-validation command result;
- paths moved from integration to unit;
- final JSON output shape;
- final CI links.

Do not commit a verification report file.

### F4. Final GitHub Actions proof

Push the final implementation commit and verify both ordinary jobs on that exact commit:

- `check` — success;
- `compat-311` — success.

Do not mark the plan complete based only on an earlier commit or a local claim.

The manual soak workflow does not need to be a required status check. Run it manually if practical after fixing the command, but the authoritative closure requirement is that the exact local command works and the normal two CI jobs pass.

### F5. Close plan statuses only after proof

After all gates pass:

- set Plan 041 to `Status: complete`;
- restore Plan 040 to `Status: complete — corrective closure completed by Plan 041`.

Do not mark either complete before final-head CI is green.

### Workstream F acceptance criteria

- [ ] Focused tests pass.
- [ ] The exact local runtime-validation command executes.
- [ ] One valid JSON file is produced.
- [ ] Canonical Python 3.12 suite passes.
- [ ] Python 3.11 smoke passes.
- [ ] Final-head `check` passes in GitHub Actions.
- [ ] Final-head `compat-311` passes in GitHub Actions.
- [ ] No verification artifact or new framework is committed.
- [ ] Plan statuses are updated only after proof.

---

## Implementation order

Use two reviewable commits, with a third only for verification-discovered corrections.

### Commit 1 — Correct runtime validation and metrics

- reopen Plan 040 status;
- replace `--mode` with `--duration-seconds`;
- make `--output` a single JSON file;
- remove manifest/Markdown/JSONL bundle generation;
- measure Eggpool child RSS correctly;
- make unavailable values nullable;
- fail closed for missing drain/resource inputs;
- align workflow and release documentation;
- add focused CLI, duration, output, and metric tests.

Suggested message:

```text
Fix runtime validation CLI and resource metrics
```

### Commit 2 — Correct test taxonomy and request-local regression

- move/merge pure provider-adaptation tests into unit scope;
- remove false E2E/full-pipeline claims;
- add one real-runtime MiniMax/OpenCode Go request-local isolation test;
- remove duplicate cases so total collection does not increase;
- run canonical and smoke gates.

Suggested message:

```text
Make provider adaptation tests truthful
```

### Commit 3 — Verification corrections only, if required

Use only for defects exposed by the real command or final CI. Do not add scope.

Suggested message:

```text
Close final verification gaps
```

After final-head CI succeeds, update plan statuses in the final implementation commit or one tiny documentation-only closure commit.

---

## Small-model execution rules

1. Do not modify `.github/workflows/ci.yml` unless a real correctness defect is discovered.
2. Do not add a CI job.
3. Do not add a matrix.
4. Do not add scheduled validation.
5. Do not restore automated release publication.
6. Do not retain both `--mode` and `--duration-seconds` without a proven active caller.
7. Treat `--output` as a file everywhere.
8. Do not write a manifest, checksum bundle, Markdown report, or JSONL series.
9. Measure the Eggpool child process, not the test driver.
10. Convert KiB to bytes explicitly.
11. Return `None` for unavailable metrics.
12. Never let missing metrics satisfy a zero threshold.
13. Do not catch broad exceptions and silently continue without recording bounded failure state.
14. Do not move pure-function tests into a new integration wrapper.
15. Keep exhaustive contract/adaptation cases at the unit layer.
16. Add only one representative real-runtime MiniMax isolation regression.
17. Reuse the shared real-runtime helper.
18. Do not create a new fixture framework.
19. Do not increase the canonical collected test count.
20. Do not use arbitrary long sleeps in ordinary tests.
21. Do not claim target-device performance from GitHub runners.
22. Do not mark Plan 040 or Plan 041 complete until final-head CI passes.
23. Record execution evidence in normal commit or pull-request metadata, not a committed artifact.
24. Stop if work expands beyond the defects listed in this plan.

---

## Global acceptance criteria

### Runtime CLI

- [ ] `--duration-seconds` is supported.
- [ ] `--mode` is absent from active CLI and documentation.
- [ ] Invalid durations fail clearly.
- [ ] Duration phase derivation is deterministic.
- [ ] The canonical command executes from a clean checkout.

### Output

- [ ] `--output` names one JSON file.
- [ ] Output is atomic.
- [ ] Output parent directory is created.
- [ ] No output directory is created at the file path.
- [ ] No manifest is generated.
- [ ] No checksum bundle is generated.
- [ ] No Markdown summary is generated.
- [ ] No JSONL series is generated.
- [ ] Terminal output remains concise and useful.

### Metrics

- [ ] RSS measures the Eggpool child PID.
- [ ] Linux KiB is converted to bytes.
- [ ] macOS/BSD KiB is converted to bytes.
- [ ] Unsupported measurement becomes null.
- [ ] Host memory failure becomes null.
- [ ] Pending/reservation query failure becomes null.
- [ ] Missing final snapshot fails the drain gate.
- [ ] Missing required resource metrics fail the `sbc-reference` run.
- [ ] Polling failures are bounded and visible.

### Workflow and documentation

- [ ] Manual workflow has one Python 3.12 job.
- [ ] Manual workflow has no matrix or schedule.
- [ ] Workflow command uses supported options.
- [ ] Workflow uploads the actual JSON file.
- [ ] Workflow timeout covers the largest duration choice.
- [ ] Release guide command works exactly as documented.
- [ ] SBC results, not GitHub timing, are authoritative for target claims.

### Test truthfulness

- [ ] Pure contract/adaptation tests live in `tests/unit/`.
- [ ] Pure tests do not carry integration/request-path markers.
- [ ] No active test falsely claims E2E/full-pipeline coverage.
- [ ] One genuine ASGI-path MiniMax/OpenCode Go isolation regression exists.
- [ ] A rejected thinking-control request does not poison account/model/circuit/quarantine state.
- [ ] A subsequent plain request succeeds.
- [ ] No durable reservation or pending-request leak remains.
- [ ] Overall canonical collection does not increase.

### CI and closure

- [ ] Ordinary CI remains exactly two jobs.
- [ ] Canonical Python 3.12 checks pass.
- [ ] Python 3.11 smoke passes.
- [ ] Final-head `check` is green.
- [ ] Final-head `compat-311` is green.
- [ ] Plan 040 status reflects corrective closure during work.
- [ ] Plan 041 is marked complete only after final proof.
- [ ] No new evidence framework or committed verification report exists.

## Explicit rejection conditions

Do not close this plan if any of the following remain:

- the workflow or release guide passes an argument the runner does not accept;
- `--output` is treated as a directory anywhere in active runtime-validation code;
- the runner emits `manifest.json`, `summary.md`, or `metrics.jsonl`;
- RSS is collected from `RUSAGE_SELF` while claiming to measure Eggpool;
- Linux `ru_maxrss` is stored as bytes without multiplying by 1024;
- unavailable RSS, host memory, pending requests, or active reservations are represented as zero;
- missing summary/runtime polling data can produce a passing drain gate;
- `test_thinking_control_contract_e2e.py` remains under integration scope while invoking only pure functions;
- `test_provider_adaptation_actual_identity.py` remains under integration scope while invoking only pure functions;
- compatibility-retry pure tests retain integration markers;
- no real ASGI-path MiniMax/OpenCode Go request-local isolation regression exists;
- canonical test collection increases;
- ordinary CI contains more than `check` and `compat-311`;
- either final-head CI job is absent, failing, cancelled, or unverified;
- Plan 040 or Plan 041 is marked complete before the final-head proof exists;
- a new report bundle, checksum system, task runner, test registry, or verification framework is introduced.

## Definition of done

This line of work is closed when Eggpool retains its reduced two-job CI, its manual release procedure, and its smaller fast canonical suite, while also having one runtime-validation command that actually runs, produces one truthful JSON result, measures the Eggpool process correctly, fails closed when required metrics are unavailable, and is consistent across code, workflow, and documentation.

The remaining provider-adaptation tests must be classified by what they execute rather than by historical plan language, and the original MiniMax/OpenCode Go isolation risk must retain one real request-path regression. Both ordinary CI jobs must pass on the final implementation commit before Plans 040 and 041 are declared complete.
