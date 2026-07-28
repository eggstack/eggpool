# Exact-Head Corrective Closure

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Depends on completed:

- `plans/032-opencode-minimax-provider-contract-correction.md`
- `plans/033-real-eggpool-runtime-test-harness.md`
- `plans/034-runtime-error-isolation-finalization-recovery-matrix.md`
- `plans/035-provider-bound-request-pipeline-completion.md`
- `plans/036-real-proxy-performance-and-writer-benchmark.md`
- `plans/037-real-runtime-soak-and-resource-plateau.md`

Corrective baseline:

- `cb7407b2114eb8aab5bc536d5b1e3b200afcaa56`

## Objective

Close the corrective roadmap with truthful, reproducible evidence tied to the exact final implementation tree.

This phase owns verification, CI partitioning, evidence generation, plan status updates, and documentation correction. It must not conceal implementation work inside an “evidence” commit. If any source or test defect is found, return it to the owning plan, establish a new implementation head, and rerun every affected gate.

The previous `artifacts/plan-030-exact-head-evidence.md` must be explicitly marked superseded because it references a commit predating later source/test changes and contains soak/performance claims not produced by the corresponding tests.

## Scope

### In scope

- Freeze final implementation commit/tree.
- Clean-worktree verification on Python 3.11 and 3.12.
- Focused Plans 032–037 suites.
- Full standard suite.
- Existing reload/control-plane/request-path suites.
- Performance benchmark artifact validation.
- Actual short/standard/extended soak artifact validation.
- Ruff format/check, Pyright, and skip/xfail audit.
- CI workflow partition and run evidence.
- Exact-head evidence artifact.
- Supersession banner/status for prior incorrect closure evidence.
- Status updates for Plans 031–038 after all gates pass.
- Final documentation-only diff proof.

### Out of scope

- New production behavior.
- New provider contracts.
- New optimization work.
- Weakening test thresholds.
- Replacing failed standard/extended runs with smoke results.
- Editing raw benchmark/soak outputs by hand.
- Declaring closure when CI or a required platform result is unknown.

## Closure model

Use two commits:

1. **Implementation commit `I`** — final source, tests, scripts, CI workflow, configuration, and runtime documentation required to execute verification.
2. **Evidence commit `E`** — artifacts and plan/document status updates only.

Verification runs against `I` in a clean worktree. `E` must be a descendant of `I` whose diff contains no source, tests, scripts, workflow, configuration schema, migrations, or executable changes.

If evidence generation requires changing executable code, tests, scripts, or CI:

- discard the previous `I` designation;
- commit the change;
- designate the new commit as `I`;
- rerun all affected verification;
- regenerate evidence.

## Workstream A — Pre-closure audit

Before freezing `I`, inspect the final tree for the corrective rejection conditions.

Required source-level checks:

1. OpenCode Go MiniMax-M3 contract matches canonical provider identity/default URL.
2. Native MiniMax contract is distinct without mandatory override.
3. Canonical correctness suites use the real Plan 033 Eggpool runtime harness.
4. `run_provider_transforms` receives the actual `ProviderBoundRequest`.
5. No empty/no-op provider request is constructed in the production pipeline.
6. Streaming usage injection occurs before final provider serialization.
7. Failure effects have one authoritative application boundary per attempt.
8. Process-owned finalization is the only cancellation-safe selected-attempt finalization path.
9. Database recovery is process-owned/single-flight and readiness is fail-closed.
10. Dispatch-writer/recorder samples are bounded.
11. Performance profiles assert writer usage when labeled writer-on.
12. Soak modes enforce actual duration and count minima.
13. No test describes direct mock-upstream traffic as Eggpool end-to-end.
14. No closure artifact contains placeholder or estimated measured values.

Add/update an architecture guard test, preferably:

- `tests/unit/test_plan_038_corrective_architecture_audit.py`

This test belongs in `I`, not `E`.

## Workstream B — Freeze implementation commit

After all dependent plans are complete:

1. Ensure working tree is clean.
2. Commit all executable and test changes.
3. Record:
   - `I = git rev-parse HEAD`
   - implementation tree = `git rev-parse HEAD^{tree}`
4. Create a fresh detached worktree at `I`.
5. Install from the lockfile with required dev extras.
6. Verify no untracked generated source/test files are used.

Do not run final verification from a dirty developer checkout.

## Workstream C — Focused corrective suites

Run all Plan 032–038 focused suites on Python 3.11 and 3.12.

Expected command categories:

```bash
uv run pytest \
  tests/unit/test_plan_032_*.py \
  tests/integration/test_plan_032_*.py \
  tests/unit/test_plan_033_*.py \
  tests/integration/test_plan_033_*.py \
  tests/integration/test_plan_034_*.py \
  tests/unit/test_plan_035_*.py \
  tests/integration/test_plan_035_*.py \
  tests/perf/test_plan_035_*.py \
  tests/perf/test_plan_036_*.py \
  tests/unit/test_plan_038_corrective_architecture_audit.py \
  -q --tb=short
```

Use explicit file lists in `AGENTS.md` if shell glob portability is a concern.

Record test counts, skips, duration, interpreter version, and platform. A passing exit code without counts is insufficient evidence.

## Workstream D — Full repository verification

Run on both supported Python versions where configured:

### Standard non-slow/non-live suite

```bash
uv run pytest tests/ \
  -m "not slow and not performance and not soak and not extended_soak and not live" \
  -q --tb=short
```

Use the repository's current CI partitions if duplicate collection requires exclusions, but list every exclusion and the job that covers it.

### Reload/control-plane

```bash
uv run pytest tests/integration/reload/ -q --tb=short
uv run pytest tests/integration/test_rehash*.py -m "not slow" -q --tb=short
```

Run any slow rehash/closure tests required by Plans 019–021 separately and record them.

### Existing request/transcode/cache/finalization/recovery suites

Run focused established suites covering:

- proxy integration;
- coordinator lifecycle/disconnect;
- high-concurrency streaming;
- OpenAI/Anthropic transcode;
- usage/cost/cache/compression;
- failure effects/quarantine;
- finalization supervisor;
- database recovery;
- dispatch writer/observability.

Do not rely only on newly added plan tests.

## Workstream E — Static and audit gates

Run exactly:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run python scripts/audit_xfail_skips.py
```

Also validate:

- migrations/checksums;
- `eggpool check-config` on old/default/new corrective configurations;
- documentation command paths where automated checks exist;
- no secret/request content in committed artifacts.

No non-strict xfail or unconditional skip may be added to avoid a corrective requirement.

## Workstream F — Performance evidence validation

Validate, do not rewrite, Plan 036 artifacts.

Required files:

- `artifacts/plan-036-baseline.json`
- `artifacts/plan-036-final.json`
- `artifacts/plan-036-comparison.md`

Checks:

- JSON schema version recognized;
- baseline/final commits and trees exist;
- profile parameters match;
- all requests used real Eggpool runtime;
- writer-on counters prove usage;
- raw sample counts match summary counts;
- exact JSON operation gates pass;
- latency/throughput calculations reproduce from raw data;
- comparison contains no placeholder values;
- benchmark host/interpreter/config are recorded;
- artifacts contain no secrets/content.

Add an artifact validator script/test in `I`, for example:

- `scripts/validate_plan_036_artifacts.py`
- `tests/unit/test_plan_038_performance_artifact_validation.py`

## Workstream G — Soak evidence validation

Validate Plan 037 artifacts for all required modes/profiles.

Required closure evidence:

- short writer-off and writer-on;
- standard writer-off and writer-on;
- extended writer-off and writer-on;
- raw checkpoint JSONL or immutable CI artifact references plus checksums;
- summary JSON;
- `artifacts/plan-037-soak-analysis.md`.

Checks:

- actual monotonic elapsed time meets each mode minimum;
- actual completed requests meet each mode minimum;
- start/end timestamps are coherent;
- mode/profile/seed/commit/tree are recorded;
- required recovery/rehash/cancellation cycle counts are met;
- quiescent checkpoints exist at required cadence;
- plateau calculations reproduce from raw checkpoints;
- no unavailable metric is represented as zero;
- interrupted/incomplete runs are not included as passing;
- all artifacts correspond to `I`, or any documentation-only evidence commit relationship is explained precisely;
- no “equivalent,” extrapolated, or estimated duration appears.

Add:

- `scripts/validate_plan_037_artifacts.py`
- `tests/unit/test_plan_038_soak_artifact_validation.py`

These validators are executable and therefore must be committed before freezing `I`.

## Workstream H — CI closure

Ensure CI contains distinct jobs or clearly documented partitions for:

- lint/format/type/audit;
- standard unit/integration on Python 3.11 and 3.12;
- reload/control-plane;
- Plans 019–021 retained finalization/reload closure;
- Plans 032–035 corrective correctness;
- bounded Plan 036 performance contracts;
- Plan 037 smoke only;
- scheduled/manual standard/extended soak workflow.

For `I`, record actual workflow run IDs/URLs, conclusions, and job names. If the repository's connector cannot retrieve non-PR runs, use GitHub Actions UI/API evidence during implementation; do not write “PASS” without a run identifier.

If CI is unavailable due an external outage, closure remains blocked unless repository policy explicitly permits signed local evidence. Documenting unavailability is honest but not equivalent to passing CI.

## Workstream I — New exact-head evidence artifact

Create only after verification:

- `artifacts/plan-038-exact-head-evidence.md`

Required sections:

### Identity

- corrective roadmap baseline;
- implementation commit `I` and tree;
- evidence commit `E` once created;
- exact verification timestamps;
- OS/architecture;
- Python 3.11/3.12 patch versions;
- uv version;
- relevant configuration profiles.

### Corrective defect proof

- actual OpenCode Go provider identity/default URL tested;
- strict rejection upstream request count = 0;
- warn-drop captured payload evidence;
- unrelated and corrected requests success;
- native MiniMax distinct behavior;
- zero state-diff fields for compatibility rejection.

### Finalization/recovery proof

- cancellation seams and iteration counts;
- zero leak counts;
- database fault classes and repetitions;
- recovery epochs and readiness transitions;
- reconciliation exactness;
- rehash/shutdown interaction results.

### Payload lifecycle proof

- architecture guard results;
- exact JSON operation counts by profile;
- one final serialization evidence;
- provider retry reset evidence.

### Performance proof

- baseline/final table derived from Plan 036 raw artifacts;
- writer transactions/intents/batch distribution;
- sampling overhead;
- resource deltas for benchmark duration.

### Soak proof

- actual duration/count table for every mode/profile;
- checkpoint counts;
- resource plateau/slope table;
- early/late latency/throughput table;
- recovery/rehash/cancellation totals;
- quiescent invariant summary;
- raw artifact checksums/CI artifact references.

### Repository gates

- focused suites;
- full standard suite;
- reload/control-plane;
- established request-path suites;
- Ruff/Pyright/audit;
- CI run table.

### Post-verification diff proof

- list files changed from `I` to `E`;
- assert every file is documentation/evidence/plan status;
- state no executable/source/test/workflow/config/migration change occurred;
- include compare command and result.

No result may be represented only by `[PASS]` without count/duration/source artifact.

## Workstream J — Supersede prior evidence accurately

Update `artifacts/plan-030-exact-head-evidence.md` with a prominent banner:

```markdown
> Superseded by Plan 031 corrective roadmap and
> artifacts/plan-038-exact-head-evidence.md.
> This artifact is retained for history and must not be used as current
> closure evidence.
```

Update:

- Plan 022 status to `superseded by corrective roadmap 031`;
- Plan 030 status to `superseded; implementation retained, closure evidence invalidated`;
- Plans 031–038 status to completed only after all requirements pass.

Do not delete the historical artifact or rewrite its old measured tables as though they were valid. Preserve history and clearly correct the record.

## Workstream K — Evidence commit and final diff

After `I` verification:

1. Add Plan 038 evidence and documentation/status changes only.
2. Commit as `E`.
3. Compare `I...E`.
4. Reject `E` if changed paths include:
   - `src/`;
   - `tests/`;
   - `scripts/`;
   - `.github/workflows/`;
   - migrations/schema/checksums;
   - executable config behavior.
5. Fetch `E` and confirm the evidence names the correct `I` and tree.
6. Confirm `main` is exactly `E` after push.

If any forbidden path changed, establish a new `I` and repeat verification.

## Required evidence validators

The following must run before closure:

```bash
uv run python scripts/validate_plan_036_artifacts.py
uv run python scripts/validate_plan_037_artifacts.py
uv run pytest \
  tests/unit/test_plan_038_corrective_architecture_audit.py \
  tests/unit/test_plan_038_performance_artifact_validation.py \
  tests/unit/test_plan_038_soak_artifact_validation.py \
  -q --tb=short
```

Validators must fail on missing files, placeholder values, mismatched SHAs, insufficient durations/counts, unavailable-as-zero resource metrics, or inconsistent totals.

## Acceptance criteria

### Implementation freeze

- [ ] One exact implementation commit `I` contains every executable change.
- [ ] Verification runs from a clean worktree at `I`.
- [ ] Implementation tree SHA is recorded.
- [ ] No generated/untracked code is required for passing results.

### Corrective functionality

- [ ] Plan 032 actual provider-identity behavior is proved.
- [ ] Plan 034 real-runtime error isolation/finalization/recovery matrix passes.
- [ ] Plan 035 real provider-bound payload ownership guards pass.
- [ ] No original corrective rejection condition remains.

### Performance and soak

- [ ] Plan 036 raw artifacts validate and reproduce.
- [ ] Writer-on profiles prove writer use and transaction reduction.
- [ ] Actual short/standard/extended Plan 037 runs pass for required profiles.
- [ ] Duration/count/recovery/rehash/cancellation minima are met.
- [ ] Resource and latency plateau gates pass.
- [ ] Raw artifacts are retained and checksummed.

### Repository verification

- [ ] Focused Plans 032–038 suites pass on Python 3.11 and 3.12.
- [ ] Full standard suite passes.
- [ ] Reload/control-plane and established request-path suites pass.
- [ ] Ruff format/check pass.
- [ ] Pyright reports zero errors.
- [ ] Skip/xfail audit passes.
- [ ] Required CI jobs have recorded successful run IDs.

### Evidence integrity

- [ ] `artifacts/plan-038-exact-head-evidence.md` contains measured counts/durations and exact SHAs.
- [ ] Prior Plan 030 evidence is visibly superseded.
- [ ] Plans 022/030 status accurately reflects supersession.
- [ ] Plans 031–038 are marked complete only after all gates.
- [ ] Evidence commit `E` changes documentation/evidence/status files only.
- [ ] Final `main` equals `E`.

## Explicit rejection conditions

Do not mark this plan or roadmap complete if:

- exact-head evidence names a commit predating later source/test changes;
- a source/test/script/workflow change occurs after verification without a new `I` and rerun;
- CI is claimed passing without run identifiers;
- standard/extended soak evidence is absent, incomplete, extrapolated, or from direct mock-upstream traffic;
- performance artifacts contain placeholder or hand-entered values not derived from raw output;
- Plan 030 evidence remains presented as current;
- any focused suite is skipped/xfail-marked to achieve closure;
- evidence commit contains executable changes;
- `main` advances after `E` before closure is declared.
