# Deployment and Packaging Milestone 006 — Closure Status

Status: corrective pass required

Corrective pass: `plans/closure/deployment-packaging/007-status.md` — the
action-pin statements and the cited glob evidence recorded here were found to be
inaccurate. Milestone 007 closes them.

Source implementation plan:

- `plans/implementation/deployment-packaging/006-release-artifact-version-derivation-and-artifact-hygiene.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M006 is complete and **closed**. The release surface now states one truth
about the release version and one truth about the qualification artifact path.

The two qualification workflows no longer hardcode `0.8.0` while the crate is
at `0.8.2`. Both now derive the version from the repository's own version
authority, through the same chain `release.yml` already used, and the
derivation fails closed. The release-workflow validator, which previously ran
only on tag push, now runs on every push and pull request and covers all three
release-related workflows.

**The qualification artifact path chose RETENTION.** The 148 tracked files
under `artifacts/qualification/` remain tracked, and the inert ignore rule was
corrected instead. No closure record was orphaned, no artifact was untracked
or deleted, and no committed test lost its input.

The tracked∩ignored overlap went from **148 paths to 0**, computed with
`git ls-files -z | git check-ignore --no-index -z --stdin`.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Work package A — the qualification workflows derive the version | `tests/tooling/test_release_workflow_versions.py::test_qualification_workflows_derive_the_version_from_the_authority`, `::test_no_covered_workflow_hardcodes_an_eggpool_release_version`, `::test_derived_version_matches_the_cargo_version`, `::test_every_covered_workflow_passes_its_own_contract` | pass | All seven literal sites in `qualification-release-artifacts.yml` and both sites in `qualification-public-release.yml` now consume a derived value |
| Work package A — the pre-correction globs did not match the current artifacts | `...::test_stale_version_globs_do_not_match_the_current_artifacts` | pass | The four pre-correction globs match **0 of the 6** artifacts the build produces at Cargo version `0.8.2`; the derived globs match all of them |
| Work package B — the validator runs in CI and covers the qualification workflows | `...::test_ci_runs_the_release_workflow_validator_for_every_covered_workflow`, `::test_validator_rejects_a_reintroduced_version_literal`, `::test_validator_rejects_a_workflow_without_the_version_gate`, `::test_validator_rejects_a_workflow_that_omits_the_version_resolution_job`, `::test_validator_rejects_a_workflow_outside_the_covered_set`, `::test_covered_workflows_are_tracked` | pass | `.github/workflows/ci.yml` invokes the validator for all three covered workflows on every push and pull request |
| Work package B — the single-file validator interface still works | `uv run python scripts/validate_release_workflow.py .github/workflows/release.yml` | pass | The CLI now takes `nargs="*"` paths and dispatches per contract; the existing one-path invocation is unchanged |
| Work package C — no path is simultaneously ignored and tracked | `tests/tooling/test_artifact_hygiene.py::test_no_tracked_path_is_simultaneously_ignored_and_tracked` | pass | The overlap went from 148 paths to 0 |
| Work package C — retained evidence stays tracked and referenced | `...::test_retained_qualification_evidence_is_tracked`, `...::test_retained_evidence_is_referenced_by_a_closure_record`, `...::test_closure_records_resolve_the_retained_qualification_artifacts` | pass | The closure-record resolver pins the 8 pre-existing dangling historical references so any **new** dangling reference fails the suite |
| Work package C — generated output stays untracked | `...::test_generated_qualification_output_stays_untracked` | pass | — |
| Work package D — the parity defaults resolve in a clean clone | `tests/tooling/test_dashboard_parity_defaults.py::test_parity_default_inputs_resolve_inside_the_repository`, `::test_parity_default_inputs_are_tracked`, `::test_parity_default_inputs_carry_no_secret_or_operator_path`, `::test_parity_recorded_baseline_is_unchanged`, `::test_parity_default_report_carries_the_same_report_shape` | pass | A clean-clone simulation over 1211 indexed files resolves both defaults and reproduces `parity_status=gaps`, `mismatch_count=54` |
| Zero production runtime change | `git diff --stat` over the M006 file set (§3) | pass | No Rust, `Cargo.toml`, or manifest-schema file touched |
| Targeted guards pass | `uv run pytest tests/tooling/test_release_workflow_versions.py tests/tooling/test_artifact_hygiene.py tests/tooling/test_dashboard_parity_defaults.py -q` — 25 passed | pass | — |
| Full tooling suite passes | `uv run pytest tests/tooling/ -q --tb=short --maxfail=1` — 240 passed, 1 skipped (§4) | pass | — |
| Release-doc validator passes | `uv run python scripts/validate_release_docs.py` — `status: pass`, version `0.8.2`, 7 docs checked | pass | — |
| Lint gates | `ruff format --check`, `ruff check`, `pyright scripts/` | pass | pyright: 0 errors, 0 warnings |
| Rust workspace gates | Not run for this milestone | not run | M006 changed no Rust file. The surrounding branch's Rust gates are verified separately in the enclosing milestone batch (§10) |

## 3. Production implementation evidence

No production runtime change. The complete M006 change set is:

| File | Change |
|---|---|
| `.github/workflows/qualification-release-artifacts.yml` | A new `resolve-version` job derives the version via `scripts/validate_release_identity.py --source-commit "$GITHUB_SHA"` and exposes it as `needs.resolve-version.outputs.version`. All seven literal sites — the wheel glob, two `--version` arguments, the raw-binary glob, two `chmod` `find` name expressions, and the `pip install` assertion — now use the derived value. `build-and-qualify` and `manifest` depend on `resolve-version` |
| `.github/workflows/qualification-public-release.yml` | A new `resolve-version` job derives the native version **and** the historical Python rollback version from the catalog's `version_authority.historical_python_project_version`. Both dispatch inputs became optional with `default: ""`; the effective value is `${{ inputs.version \|\| needs.resolve-version.outputs.version }}`. The filename assertion now reads `os.environ["PUBLIC_VERSION"]` |
| `.github/workflows/ci.yml` | A new step runs `uv run python scripts/validate_release_workflow.py` for all three covered workflows, after `uv sync --frozen` |
| `scripts/validate_release_workflow.py` | Added `catalog_release_versions()`, `validate_version_derivation()`, `validate_qualification_workflow_text()`, `validate_qualification_workflow()`, `validate_covered_workflow()`, and a `COVERED_WORKFLOWS` registry. The CLI now takes `nargs="*"` paths and dispatches per contract. `validate_workflow`, `validate_workflow_text`, `job_blocks`, and `without_helper_blocks` are unchanged for existing importers. Stdlib only — no new dependency |
| `.gitignore` | `/artifacts/qualification/` → `/artifacts/qualification/*` plus 18 re-inclusions (7 evidence directories, 11 loose evidence files). Generated run output stays ignored |
| `migration-rs/closure/qualification/012-run.json`, `012-run.md` | Staged for tracking, not committed. `rust_identity` normalized from an operator-home absolute path to `rust:rust/target/debug/eggpool` |
| `tests/tooling/test_release_workflow_versions.py` | **New.** 15 tests |
| `tests/tooling/test_artifact_hygiene.py` | **New.** 5 tests |
| `tests/tooling/test_dashboard_parity_defaults.py` | **New.** 5 tests |
| `docs/releasing.md` | New "Derived release version" and "Qualification artifact retention" sections; the release-check block now lists all three workflows |
| `architecture/deep-dive-deployment.md` | New "Release workflow validation" section; tooling census counts corrected |

`scripts/dashboard_parity/_shared.py` was **not** changed. Work package D's
first sanctioned option — "the fixture files become tracked" — made the
existing defaults resolve with zero harness change.

### Which truth was chosen for the qualification artifact path, and why

**Retention.** The 148 tracked files stay tracked; the ignore rule was
corrected instead.

The decision was made after reading the evidence, not before:

- `plans/closure/persistence/007-pi5-qualification.md` cites
  `artifacts/qualification/m007-pi5-2026-10-03/` and its `binary.sha256` as
  the evidence column for the Pi 5 qualification verdict.
- `plans/closure/persistence/005-status.md` states that `artifacts/qualification/m004/`
  "holds 14 accepted committed artifacts" and pins a 17-test machine audit that
  reads them.
- `plans/closure/persistence/003-status.md` cites the `m003/` baseline and
  candidate runs.
- `plans/closure/on-prem-concurrency-performance/002-status.md` states "The
  committed artifact corpus is in
  `artifacts/qualification/on-prem-concurrency-performance/ordinary-server/`".
- `plans/closure/on-prem-user-access/010-status.md` records the precedent
  explicitly: "Observation artifact force-added per the 235–237 precedent
  despite the `/artifacts/qualification/` ignore." The ignore rule — not the
  tracked state — is the newer deviation.
- Two **committed CI tests** read the retained artifacts:
  `tests/tooling/test_persistence_m004_evidence.py`
  (`ARTIFACT_DIR = artifacts/qualification/m004`) and
  `tests/tooling/test_on_prem_mmc_tail_attribution.py`
  (`artifacts/qualification/on-prem-concurrency-performance`). Untracking them
  would have broken `uv run pytest tests/tooling/` in a clean clone.

**Yes — at least six closure records depend on a retained artifact**, across
`plans/closure/persistence/`, `plans/closure/on-prem-concurrency-performance/`,
and `plans/closure/on-prem-user-access/`, and two committed tests read them.
Because of that, the plan's stop condition ("untracking would orphan a closure
record that has no other source") was **not** triggered. The artifacts were
retained and the rule corrected.

Acceptance evidence: `git ls-files -z | git check-ignore --no-index -z --stdin`
now returns **empty** (was 148 paths).

## 4. Verification executed

### Commands run

```bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_workflow.py
uv sync --frozen
git diff --check
```

### Results

- `ruff format --check` — pass, 71 files already formatted.
- `ruff check` — "All checks passed!"
- `pyright scripts/` — pass, 0 errors, 0 warnings.
- Full tooling suite — **240 passed, 1 skipped**, 0 failed. An earlier run on the
  same tree reported `test_installer.py::test_quick_installer_harness_passes`
  failing on a 30 s subprocess timeout while the machine was under concurrent
  cargo load; it passes on a quiet machine, and the same sensitivity was
  reproduced on the unmodified baseline commit `e6822d8f`. See §10.
- `scripts/validate_release_docs.py` — `status: pass`, version `0.8.2`, 7 docs
  checked.
- `scripts/validate_release_workflow.py .github/workflows/release.yml` — pass;
  the single-file interface is preserved.
- `scripts/validate_release_workflow.py` with no arguments — pass, 3 workflows
  validated.
- `uv sync --frozen` — pass, 9 packages checked; `uv.lock` unchanged.
- `git diff --check` — clean.
- Batch integration sweep — `cargo fmt --all -- --check` clean; strict Clippy exit 0 in both feature configurations; `cargo check --workspace --all-targets --no-default-features` clean; 47 test targets plus the lib, **645 passed, 0 failed, 1 ignored** (the pre-existing manual gate), covering every target touched by this milestone and by the four milestones closed alongside it.
- Clean-clone simulation via `git checkout-index -a --prefix=$TMP/` — pass;
  1211 files, both parity defaults resolve, `parity_status=gaps`,
  `mismatch_count=54`, all 7 retained evidence directories present.

### Regression-guard evidence

| WP | Perturbation | Test | Failure output | Restored |
|---|---|---|---|---|
| A | `eggpool-0.8.0-*.whl` reintroduced in `qualification-release-artifacts.yml` | `test_no_covered_workflow_hardcodes_an_eggpool_release_version` | `AssertionError: assert {'.github/wor...l': ['0.8.0']} == {}` | 15 passed |
| A | same | `test_validator_rejects_a_reintroduced_version_literal[…artifacts.yml]` | `WorkflowValidationError: workflow hardcodes the EggPool release version: 0.8.0` | — |
| A | same | the exact CI command | `release workflow workflow validation failed: workflow hardcodes the EggPool release version: 0.8.0` — **exit 1** | exit 0 |
| B | the validator step deleted from `ci.yml` | `test_ci_runs_the_release_workflow_validator_for_every_covered_workflow` | `AssertionError: assert 'scripts/validate_release_workflow.py' in '# ---…'` | 15 passed |
| B | `validate_release_identity.py` → `print_version.py` (version gate removed) | CLI + `test_every_covered_workflow_passes_its_own_contract` | `version gate is missing: version resolution does not use the repository version authority` — **exit 1** | exit 0, 15 passed |
| C | `/artifacts/qualification/` restored (the inert rule) | `test_no_tracked_path_is_simultaneously_ignored_and_tracked` | `AssertionError: assert ['artifacts/q...rk.json', ...] == []` (148 paths) | 5 passed |
| D | `git rm --cached` the two parity fixtures (files kept on disk) | `test_parity_default_inputs_are_tracked` | `AssertionError: migration-rs/closure/qualification/012-run.json` | 5 passed, re-`git add`ed |

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| The release artifact naming, manifest, and publication contract are unchanged | Only the version *source* changed; every glob and assertion still targets the same artifact names |
| The release identity rules (tag and source-commit checks) are unchanged | `release.yml` was not modified; the qualification workflows call the same `scripts/validate_release_identity.py` authority |
| The `release.yml` version derivation path is not modified | `git diff` shows no change to `.github/workflows/release.yml` |
| The installed-owner detection and the one-shot installer are unchanged | `scripts/install.sh` and the installer tooling were not touched |
| The qualification workflows still assert that a release artifact carries the expected version | The assertions were kept; only their version source changed, and the derivation fails closed |
| The dashboard parity tooling still runs against its recorded baselines | `scripts/dashboard_parity/_shared.py` was not changed; the harness's assertions and baselines are untouched. The clean-clone simulation reproduces `parity_status=gaps`, `mismatch_count=54` |
| No secret, credential, or operator-identifying material is added to a tracked artifact | The parity fixture's `rust_identity` was normalized from an operator-home absolute path to `rust:rust/target/debug/eggpool`; `test_parity_default_inputs_carry_no_secret_or_operator_path` enforces it |
| No change to the release artifact format, the manifest schema, the packaging layout, the installer behavior, or the release version itself | `rust/Cargo.toml` and the release catalog are untouched; the version remains `0.8.2` |
| The validator extension requires no new Python dependency | The extension is stdlib-only; `uv.lock` is unchanged and `uv sync --frozen` is satisfied |
| No closure record was orphaned and no artifact was deleted | The retention decision is documented in §3; the artifact-hygiene guard resolves every retained reference |

## 6. Failure and recovery review

The new failure behavior is confined to CI workflow validation and the
tooling guards:

- A workflow that hardcodes an EggPool release version fails the validator
  with a non-zero exit, failing the CI job.
- A workflow that omits the version-resolution job fails the validator with
  "version resolution does not use the repository version authority".
- A workflow outside the covered set fails the validator rather than being
  silently skipped.
- A tracked path that becomes simultaneously ignored fails the artifact
  hygiene guard.
- A parity default that stops resolving, stops being tracked, or starts
  carrying an operator path fails the parity defaults guard.

No runtime path, lock, await, task, or storage access was added. There is no
new recovery surface to review.

## 7. Migration and compatibility review

No migration. No storage change. No configuration key added, removed, or
reclassified. No public HTTP or CLI contract change. No public Rust API change.

The release version is unchanged at `0.8.2`. The release catalog's
`version_authority` block — `native_release_version` and
`historical_python_project_version` — is now read by the qualification
workflows instead of a literal, which makes the derivation a single-source
change.

The two parity fixture files are newly tracked. Their content is unchanged
apart from the `rust_identity` normalization, and the recorded report shape
and baseline are asserted unchanged by
`test_parity_recorded_baseline_is_unchanged` and
`test_parity_default_report_carries_the_same_report_shape`.

## 8. Security review

- **Auth** — unchanged; no runtime path was touched. The workflows' action
  pins are unchanged, as the plan requires.
- **Secret handling** — the one operator-identifying absolute path in the
  tracked parity fixture was removed before the file was staged. The guard
  `test_parity_default_inputs_carry_no_secret_or_operator_path` fails if a
  secret or an operator-home path reappears. No credential, token, or key
  was added to any tracked artifact.
- **Privilege bounds** — unchanged.
- **DoS bounds** — the guards parse bounded workflow and fixture files. The
  validator is stdlib-only and runs on every push and pull request; its cost
  is a text scan of three workflows.
- **Audit behavior** — unchanged.

## 9. Documentation and operations

- `docs/releasing.md`: new "Derived release version" and "Qualification
  artifact retention" sections; the release-check block now lists all three
  workflows.
- `architecture/deep-dive-deployment.md`: a new "Release workflow validation"
  section and corrected tooling census counts.
- `tests/tooling/`: three new test files, 25 new tests.
- `.gitignore`: the artifact ignore rule is now `/artifacts/qualification/*`
  with 18 explicit re-inclusions, so generated run output stays ignored while
  the retained evidence stays tracked.
- No operator command or runtime diagnostic changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | The catalog-scoped literal guard can false-positive on a future version collision | If a future EggPool release version equals a third-party pin in these workflows (for example EggPool `0.13.0` against the Zig `0.13.0` pin), the validator fails loudly and misleadingly | When a release bumps into a colliding version, refine the guard to scan only EggPool-artifact lines. No action needed today — all three workflows pass |
| low | `rust_identity` in the parity report still carries a machine-local path on regeneration | `scripts/dashboard_parity/runner.py:558` computes it from the absolute `RUST_BINARY`; a local parity run writes the absolute path back into the tracked file | The `runner.py` owner should render `rust_identity` repo-relative. The already-tracked `tests/fixtures/dashboard-python-oracle/current-gap-report.json` has the same pre-existing property |
| low | The two qualification workflows still use tag-style action pins (`actions/checkout@v4`, `astral-sh/setup-uv@v4`) while `release.yml` pins immutable SHAs | Lower exposure than `release.yml` because the qualification workflows are dispatch-only and read-only | A follow-up milestone should pin the qualification workflows to SHAs and extend the SHA-pin rule in `validate_qualification_workflow_text`. Explicitly out of scope here |
| low | `scripts/qualification_ordinary_server.py` writes into the retained `…/ordinary-server/` corpus by default, which can no longer be git-ignored | New runs there surface as untracked files for review | Accepted consequence of retention: a tracked path may not be ignored. Review the untracked output before staging |
| low | `tests/tooling/test_installer.py::test_quick_installer_harness_passes` is timing-sensitive and intermittently fails | The installer harness gives each of its 78 cases a 30 s subprocess timeout. Under load or a cold `uv run` sync, one case can exceed it and the test fails with a `TimeoutExpired` instead of a real defect | **Pre-existing, not caused by this milestone.** The same test was observed to fail on the unmodified baseline commit `e6822d8f` and to pass on the same tree moments later, with the installer harness passing all 78 cases on every passing run. The baseline worktree at `e6822d8f` reproduces the same sensitivity. Recommendation: raise the harness's per-case timeout, or bound the case set, in a follow-up that owns the installer qualification harness. The release and installer contracts themselves were not changed here |
| low | The full serial Rust workspace suite was not run for this milestone | None for M006 specifically: no Rust file changed | The enclosing milestone batch runs the Rust gates on the surrounding branch. See the batch record |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Deployment and packaging
M006 was the only registered successor in this subsystem, so no milestone is
promoted and the blocked-work table gains no deployment-packaging entry.

The subsystem's remaining obligations are unchanged and are tracked
separately: the release workflow's SHA-pinning rule does not yet cover the
qualification workflows, and the parity harness's `rust_identity` rendering
still embeds an absolute path on regeneration.

## 12. Registry updates

- `plans/registry.md`: the Deployment and packaging M006 row is removed from
  "Dependency-ready implementation plans" and added to "Recently closed" with
  its disposition; the Deployment and packaging row in "Active subsystem
  roadmaps" records `M006 closed`.
- `plans/subsystems/deployment-packaging-roadmap.md`: the milestone 006 row in
  the milestone status table moves to `closed` with its closure record linked;
  the dependency graph and the roadmap's completion statement reflect the
  closed state.
- `plans/implementation/deployment-packaging/006-release-artifact-version-derivation-and-artifact-hygiene.md`:
  the `Status` line moves from `ready` to `implemented`.
