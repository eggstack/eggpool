# Deployment and Packaging Milestone 008 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/deployment-packaging/008-ci-dependency-lock-discipline.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#12-milestone-status`

Repository baseline reviewed: `496ce51eb1b03fd36f8f1e8f3c064521cffe795c`

The plan was registered at baseline
`1929abf5a725cc7faac0f866622afa7af017694a`. The tree reviewed here is the
later `main` head `496ce51eb1b03fd36f8f1e8f3c064521cffe795c`; the four
`uv sync --frozen` gates, the `uv run` subprocess, the release workflow's
`--no-project` isolation, and the tooling-suite baseline recorded in M007's
closure record were all still present and unchanged at that head.

Implementation commits or pull requests:

`901d5182`

- The change set is committed on `main` as `901d5182`, "Install the CI
  environment from the committed lockfile". The baseline reviewed above is that
  commit's parent.

## 1. Executive finding

M008 is complete and **closed**.

The defect was real, and it was one line away from where the original review
pointed. `tests/tooling/test_release_docs.py:29` was flagged for invoking
`uv run` without `--frozen`, but that framing does not survive measurement:
the repository has 80 `uv run` invocations across `.github/workflows/`,
`scripts/`, and `tests/`, and none of them carries `--frozen`, `--locked`,
`--no-sync`, or `--offline`. The flagged line followed the repository's
uniform convention rather than departing from it, and the hazard it described
does not fire while the lockfile is current.

The defect was in the **gate**, not in the invocation. The four CI gates that
install the Python environment used `uv sync --frozen`, which installs the
versions the lockfile records without checking that the lockfile still
satisfies `pyproject.toml`. The first `uv run` after such a gate then
re-resolved and rewrote `uv.lock` in the working tree, exited 0, and made the
job qualify a resolution the repository never committed. No test failed and no
step reported the substitution.

All three of the milestone's work packages are implemented:

- The four gates install the environment with `uv sync --locked`, so a
  drifted `pyproject.toml` fails the job at the environment-install step with
  a message naming the lockfile and the remedy.
- The release-docs guard test runs `scripts/validate_release_docs.py` under
  the test suite's own interpreter and spawns no `uv` process. Every
  assertion in it is unchanged.
- A guard reads the workflow files as text, inspects their `run:` steps, and
  holds every workflow that installs the project environment to
  `uv sync --locked` while leaving the release workflow's `--no-project`
  isolation alone.

The committed `uv.lock` and `pyproject.toml` are byte-identical before and
after the change. The release artifact, the publication gate, the action
pins, the release version derivation, and the release documentation content
are unchanged.

**The tooling suite is green on the current tree.** `uv run pytest
tests/tooling/ -q --tb=short` exits 0 with **251 passed, 1 skipped**. One
failure was observed and reported during this milestone and has since been
fixed upstream of it, in a commit that is not part of this milestone:
`tests/tooling/test_persistence_current_docs.py::test_roadmap_states_the_current_schema_census`
failed because the Persistence roadmap stated migration chain `v1–v63` while
the repository commits migration `0064_account_backoffs_null_identity.sql`.
M008 reproduced it against a pristine extract of the baseline head (§4),
confirmed that it was not caused by and not in the ownership of this
milestone, and did not paper over it. The Persistence roadmap now states
`v1–v64` and the guard passes. The pass count did not decrease: M007's
closure record recorded 244 passed and 1 skipped for this suite; it is now
251 passed and 1 skipped.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| §13.1 — No workflow installs the project environment with `uv sync --frozen` | `tests/tooling/test_release_workflow_versions.py::test_no_workflow_installs_the_project_environment_with_frozen` | pass | Reads all 7 workflow files under `.github/workflows/` and finds 0 lines carrying `uv sync --frozen`; shown failing on a deliberate revert (§4) |
| §13.2 — Every workflow that installs the project environment uses `uv sync --locked` | `tests/tooling/test_release_workflow_versions.py::test_every_project_environment_install_uses_the_locked_lockfile` | pass | The project-installing set is derived from the workflow text, not hardcoded: exactly the four corrected workflows, each with the single command `uv sync --locked`; shown failing on a deliberate revert (§4) |
| §13.3 — A drifted `pyproject.toml` fails the CI gate with a message naming the lockfile and the remedy, demonstrated in a sandbox | Sandbox run under `/tmp/m008-lock-discipline/probe` (§4) | pass | `uv sync --frozen --offline` exits 0 on the drifted tree; `uv sync --locked --offline` exits 1 with "The lockfile at `uv.lock` needs to be updated, but `--locked` was provided. To update the lockfile, run `uv lock`."; `uv lock --check --offline` exits 1 with the same message for `--check` |
| §13.4 — The release-docs guard test runs the validator under the test suite's own interpreter and spawns no `uv` process | `tests/tooling/test_release_workflow_versions.py::test_the_release_docs_guard_runs_the_validator_without_uv`; `tests/tooling/test_release_docs.py::test_guard_is_machine_readable_and_rejects_root_release_builds` | pass | The guard parses the release-docs test's source and asserts the spawned argv carries `sys.executable` and no `uv` element; the release-docs test still asserts the zero exit status, the JSON report, the `pass` status, the published release, and that the release workflow contains neither `uv build` nor `uv publish`; shown failing on a deliberate revert (§4) |
| §13.5 — A guard fails if any of the four gates regresses to `--frozen` | `tests/tooling/test_release_workflow_versions.py::test_no_workflow_installs_the_project_environment_with_frozen`, `::test_every_project_environment_install_uses_the_locked_lockfile` | pass | Reverting `.github/workflows/ci.yml:55` to `uv sync --frozen` fails both guards; restoring the file and re-running gives 21 passed (§4) |
| §13.6 — The committed `uv.lock` and `pyproject.toml` are byte-identical before and after the change | `shasum uv.lock pyproject.toml` before and after; `cmp` against the pre-change `/tmp` copies | pass | `uv.lock` = `68e1f35e268037067157b454fc049f2aacc24894`, `pyproject.toml` = `0b3af74997acb44f0dfd47876e9e541266169f6e`, unchanged before and after; `git status --short uv.lock pyproject.toml` is empty |
| §13.7 — The tooling test suite passes with no decrease in its pass count, and the linters and the release-documentation validator pass | `uv run pytest tests/tooling/ -q --tb=short` — **251 passed, 1 skipped**, exit 0 (126.96 s); `uv run ruff format --check scripts/ tests/tooling/` — exit 0; `uv run ruff check scripts/ tests/tooling/` — exit 0; `uv run pyright scripts/` — exit 0; `uv run python scripts/validate_release_docs.py` — exit 0, `status: pass` | pass | The suite is fully green on the current tree. The pass count did not decrease: M007's closure record recorded 244 passed and 1 skipped for this suite; it is now 251 passed and 1 skipped. The single failure observed earlier in this milestone was a Persistence-subsystem schema-census defect, reproduced at a pristine baseline and fixed upstream of it; §4 and §10 record that history |
| §10 (plan) — the existing release workflow guards continue to pass: the version derivation, the hardcoded-literal check, the action-pin check, and the per-workflow contract check | `::test_qualification_workflows_derive_the_version_from_the_authority`, `::test_no_covered_workflow_hardcodes_an_eggpool_release_version`, `::test_validator_rejects_a_tag_style_action_pin`, `::test_every_covered_workflow_passes_its_own_contract` | pass | All pass; the guard file is 21 passed (17 before M008, 4 added) |
| §10 (plan) — no tooling test is deleted, weakened, or skipped | `git diff --stat` over `tests/tooling/` | pass | Two files changed, +197/−6 lines overall; no existing assertion was changed, no test was removed, and no `@pytest.mark.skip` was added |
| The release workflow's `--no-project --with maturin==1.14.1` isolation is unchanged | `tests/tooling/test_release_workflow_versions.py::test_the_release_workflow_keeps_its_no_project_isolation` | pass | `.github/workflows/release.yml` carries exactly 8 sites of `uv run --no-project --with maturin==1.14.1`, has no project-environment install, and is not in the derived project-installing set |
| A workflow that installs the environment through another mechanism is not failed spuriously | `tests/tooling/test_release_workflow_versions.py::test_every_project_environment_install_uses_the_locked_lockfile` | pass | `.github/workflows/qualification-public-release.yml` installs the environment through `python -m pip install --user pipx` and `uv run python …`, has no `uv sync`, and is therefore not in the project-installing set and not flagged |
| The Rust workspace is not touched by this milestone | `git status --short` and `git diff --stat` | pass | The change set is four workflow files, two tooling test files, one closure record, and one roadmap; no path under `rust/` is modified, so no `cargo` gate is required (plan §11) |

## 3. Production implementation evidence

No production runtime change. The complete M008 change set is six files plus
this closure record and one roadmap edit:

| File | Change |
|---|---|
| `.github/workflows/ci.yml:55` | `uv sync --frozen` → `uv sync --locked`. Nothing else in the file changed: no step, no action pin, no permission, no version derivation |
| `.github/workflows/installer-shell-qualification.yml:49` | `uv sync --frozen` → `uv sync --locked`. Nothing else changed |
| `.github/workflows/qualification-portability.yml:39` | `uv sync --frozen` → `uv sync --locked`. Nothing else changed |
| `.github/workflows/qualification-release-artifacts.yml:66` | `uv sync --frozen` → `uv sync --locked`. Nothing else changed; the workflow's five `uv run --no-project --with maturin==1.14.1` sites and its three `uv run --no-project --python 3.11` sites are untouched |
| `tests/tooling/test_release_docs.py` | The `subprocess.run` argv changed from `["uv", "run", "python", "scripts/validate_release_docs.py"]` to `[sys.executable, str(ROOT / "scripts/validate_release_docs.py")]`, and `import sys` was added. Every assertion is unchanged: the zero exit status, the JSON report parse, `report["status"] == "pass"`, `report["production_release"] == "published 0.8.2"`, and the two release-workflow content checks |
| `tests/tooling/test_release_workflow_versions.py` | Four guards added (see below), plus the helpers they read the repository with: `workflow_paths()`, `workflow_run_steps()`, `project_environment_installs()`, and `project_installing_workflows()` |

### The four added guards

| Guard | Property it holds |
|---|---|
| `test_no_workflow_installs_the_project_environment_with_frozen` | No workflow file contains the line `uv sync --frozen`. It reads all 7 workflows under `.github/workflows/` as text |
| `test_every_project_environment_install_uses_the_locked_lockfile` | Every `run:` step in every workflow that invokes `uv sync` carries `--locked`. The project-installing set is **derived** from the workflow text, so a workflow that installs the environment outside the four corrected gates is covered without a manual edit. The guard also asserts that the four corrected gates are present, so it cannot go vacuous |
| `test_the_release_workflow_keeps_its_no_project_isolation` | `.github/workflows/release.yml` carries exactly 8 `uv run --no-project --with maturin==1.14.1` sites, installs no project environment, and is not in the project-installing set |
| `test_the_release_docs_guard_runs_the_validator_without_uv` | The release-docs guard test's spawned argv carries `sys.executable` and no `uv` element. The test parses the target module's source with `ast`, so it reads the real subprocess call rather than a restatement of it |

**The guard reads the repository's `run:` steps, not a restatement of them.**
`workflow_run_steps()` scans the workflow text for a `run:` key and takes the
key's inline value plus every continuation line the key owns, so the
single-line, folded (`>-`), and literal (`|`) forms the repository's
workflows use are all covered without a YAML runtime dependency. A `uv sync`
mention outside a `run:` step is not treated as an install, and a workflow
that installs the environment through some other mechanism has no `uv sync`
command at all, so it is not reported and the guard does not fail it
spuriously.

### The measurements the guards assert

| Fact | Measured value |
|---|---|
| Workflow files under `.github/workflows/` | 7 (`ci.yml`, `dependency-audit.yml`, `installer-shell-qualification.yml`, `qualification-portability.yml`, `qualification-public-release.yml`, `qualification-release-artifacts.yml`, `release.yml`) |
| Workflows that install the project environment | 4 — the four corrected gates |
| The install command each of them runs | `uv sync --locked` |
| Workflows carrying `uv sync --frozen` | 0 |
| `.github/workflows/release.yml` `--no-project --with maturin==1.14.1` sites | 8 |
| `.github/workflows/release.yml` project-environment installs | 0 |
| `.github/workflows/qualification-public-release.yml` project-environment installs | 0 — it installs the environment through `python -m pip install --user pipx` and `uv run python …` |
| uv version used for every measurement | 0.11.16 (`135a36367 2026-05-21`) |
| Tooling suite before M008 (M007's closure record) | 244 passed, 1 skipped |
| Tooling suite after M008 | 251 passed, 1 skipped, 0 failed |

## 4. Verification executed

### Commands run

The plan's §11 command set, from the repository root:

```bash
uv sync --locked
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
```

The targeted guards and the mutation checks:

```bash
uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=short
uv run pytest tests/tooling/test_release_workflow_versions.py tests/tooling/test_release_docs.py -q --tb=short
```

The lockfile-integrity evidence:

```bash
shasum uv.lock pyproject.toml
cmp uv.lock /tmp/m008-lock-discipline/uv.lock.backup
cmp pyproject.toml /tmp/m008-lock-discipline/pyproject.toml.backup
git status --short uv.lock pyproject.toml
```

The sandbox drift demonstration (§13.3), in an isolated project at
`/tmp/m008-lock-discipline/probe` whose `pyproject.toml` was deliberately
drifted from its `uv.lock`:

```bash
uv lock --offline            # establish the lock
# drift the project metadata: version 0.1.0 -> 0.2.0
uv sync --frozen --offline
uv sync --locked --offline
uv lock --check --offline
uv run --offline python -c 'pass'
diff uv.lock uv.lock.before
```

### Results

| Command | Exit status | Observed output |
|---|---|---|
| `uv sync --locked` | **0** | `warning: No `requires-python` value found in the workspace. Defaulting to `>=3.11`.` / `Resolved 10 packages in 16ms` / `Checked 9 packages in 6ms` |
| `uv run pytest tests/tooling/ -q --tb=short` | **0** | **251 passed, 1 skipped in 126.96 s (0:02:06)** — no failures — and re-run with this record exactly as written: **251 passed, 1 skipped in 146.85 s (0:02:26)**, exit 0. Two earlier runs, before this record was written, collected the same 251 tests but reported one failure, at 113.95 s (0:01:53) and 119.81 s (0:01:59); that failure was a Persistence-subsystem schema-census defect, reproduced at a pristine baseline and since fixed upstream of this milestone — see the resolved observation below. The record is inside the suite's own coverage: the artifact-hygiene resolver guard scans every `*.md` under `plans/closure/` |
| `uv run ruff format --check scripts/ tests/tooling/` | **0** | `71 files already formatted` |
| `uv run ruff check scripts/ tests/tooling/` | **0** | `All checks passed!` |
| `uv run pyright scripts/` | **0** | `0 errors, 0 warnings, 0 informations` |
| `uv run python scripts/validate_release_docs.py` | **0** | `{"docs_checked": 7, "production_release": "published 0.8.2", "python_reference": "historical external artifacts", "status": "pass", "targets": ["linux-aarch64", "linux-x86_64", "macos-arm64"], "version": "0.8.2"}` |
| `uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=short` | **0** | **21 passed in 0.12 s** — 17 before M008, 4 added by it |
| `uv run pytest tests/tooling/test_release_workflow_versions.py tests/tooling/test_release_docs.py -q --tb=short` | **0** | **23 passed in 0.21 s** |

### The lockfile-integrity evidence

`uv.lock` and `pyproject.toml` were copied to `/tmp/m008-lock-discipline/`
before the first `uv` command was run and compared byte-for-byte afterwards.

| File | SHA-1 before | SHA-1 after |
|---|---|---|
| `uv.lock` | `68e1f35e268037067157b454fc049f2aacc24894` | `68e1f35e268037067157b454fc049f2aacc24894` |
| `pyproject.toml` | `0b3af74997acb44f0dfd47876e9e541266169f6e` | `0b3af74997acb44f0dfd47876e9e541266169f6e` |

`cmp` against the pre-change copies reported no difference for either file,
and `git status --short uv.lock pyproject.toml` is empty. The gate itself
cannot rewrite the lockfile: under `--locked` a lockfile that needs updating
is an error, not an update.

### The sandbox demonstration of the drift behaviour

The sandbox is an isolated project under `/tmp` with a single version-only
dependency and no network access. Its `pyproject.toml` was drifted from its
`uv.lock` by moving the project version from `0.1.0` to `0.2.0` after the
lock was established with `uv lock --offline` (exit 0).

| # | Command | Exit status | Observed output |
|---|---|---|---|
| 1 | `uv sync --frozen --offline` | **0** | `Using CPython 3.11.9 interpreter at: …` / `Creating virtual environment at: .venv` / `Checked in 0.33ms` — **the drift is not detected and the stale resolution is installed** |
| 2 | `uv sync --locked --offline` | **1** | `Resolved 1 package in 29ms` / `The lockfile at `uv.lock` needs to be updated, but `--locked` was provided. To update the lockfile, run `uv lock`.` — **the drift is detected at the gate, and the message names both the lockfile and the remedy** |
| 3 | `uv lock --check --offline` | **1** | `Resolved 1 package in 27ms` / `The lockfile at `uv.lock` needs to be updated, but `--check` was provided. To update the lockfile, run `uv lock`.` |
| 4 | `uv run --offline python -c 'pass'` | **0** | no output, exit 0 — **and the lockfile was silently rewritten**: `diff uv.lock uv.lock.before` reported `7c7` / `< version = "0.2.0"` / `---` / `> version = "0.1.0"` |

Command 4 is the defect in its clearest form: the `uv run` that follows the
`--frozen` gate re-resolves, rewrites `uv.lock` in the working tree, and
still exits 0. No test fails and no step reports the substitution. The
`--locked` gate turns that silent substitution into a loud failure located at
the environment-install step, before any `uv run` executes.

### Mutation checks

Both mutations were applied by hand-editing the file and undone by
hand-editing it back. No `git checkout`, `git restore`, `git stash`,
`git reset`, or `git clean` was used. Every restore was verified with `cmp`
against a copy of the intended final content taken before the first mutation,
and the restored `git diff` shows only the intended change.

| # | Perturbation | Guard / command | Observed failure output | Restored |
|---|---|---|---|---|
| 1 | `.github/workflows/ci.yml:55` reverted from `uv sync --locked` to `uv sync --frozen` | `uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=short` | **2 failed, 19 passed** — `test_no_workflow_installs_the_project_environment_with_frozen` (line 413): `AssertionError: assert {'.github/wor...nc --frozen']} == {}` with `Left contains 1 more item: {'.github/workflows/ci.yml': ['- run: uv sync --frozen']}`; `test_every_project_environment_install_uses_the_locked_lockfile` (line 431): `AssertionError: assert {'.github/wor...nc --frozen']} == {}` with `Left contains 1 more item: {'.github/workflows/ci.yml': ['uv sync --frozen']}` | `cmp` against the pre-mutation copy: **IDENTICAL**. `git diff` shows only `-      - run: uv sync --frozen` / `+      - run: uv sync --locked`. Re-run: **21 passed**, exit 0 |
| 2 | `tests/tooling/test_release_docs.py` restored to `["uv", "run", "python", "scripts/validate_release_docs.py"]` | `uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=short` | **1 failed, 20 passed** — `test_the_release_docs_guard_runs_the_validator_without_uv` (line 469): `AssertionError: the release-docs guard must not spawn uv` / `assert not True` / `+ where True = any(<generator object test_the_release_docs_guard_runs_the_validator_without_uv.<locals>.<genexpr> at 0x10b698ba0>)` | `cmp` against the pre-mutation copy: **IDENTICAL**. `git diff` shows only the `import sys` line and the argv line. Re-run of both guard files: **23 passed**, exit 0 |

Mutation 1 proves the lock-discipline guard is load-bearing in both of its
directions: the whole-text check and the per-install check each fail
independently. Mutation 2 proves the release-docs subprocess discipline is
guarded rather than merely asserted in prose.

### The one tooling failure seen during this milestone, reproduced and resolved upstream

The tooling suite reported a single failure while this milestone was being
implemented:
`tests/tooling/test_persistence_current_docs.py::test_roadmap_states_the_current_schema_census`.
It was shown not to be caused by this milestone. The baseline head was
extracted read-only into `/tmp/m008-lock-discipline/baseline` with
`git archive HEAD | tar -x`, and the failing test was run there against the
pristine files:

```
$ cd /tmp/m008-lock-discipline/baseline
$ grep -n 'v1–v' plans/subsystems/persistence-roadmap.md
485:Storage and migration: no schema change. WAL/NORMAL and the current schema (migration chain v1–v63) remain authoritative.
$ ls rust/assets/db/migrations/ | tail -3
0063_default_member_profile.sql
0064_account_backoffs_null_identity.sql
checksums.json
$ PYTHONPATH=. :scripts …/python -m pytest tests/tooling/test_persistence_current_docs.py::test_roadmap_states_the_current_schema_census -q --tb=line
1 failed in 0.01s
AssertionError: assert 'v1–v64' in '# Persistence Roadmap\n\nStatus: active\n…'
```

The failure reproduced with none of M008's six files present. The cause was
the `v64` migration, which had landed in the repository before this milestone
started: the guard computes the current version from the migration directory
and asserts that the Persistence roadmap states it, and the roadmap still
stated `v1–v63`.

**The fix is upstream of this milestone and is not part of it.** The
Persistence roadmap's §8 "Cross-cutting requirements" now states migration
chain `v1–v64`, and the guard passes. M008 neither edited that roadmap nor
its guard, and it did not paper over the failure while it was outstanding:
the failure was reported with its reproduction rather than suppressed, and the
tooling suite's pass count was recorded as it stood. The suite is now fully
green on the current tree — **251 passed, 1 skipped, 0 failed**. This is
recorded here as a resolved, upstream-of-this-milestone observation, not as
an outstanding finding of M008.

## 5. Invariant review

| Plan §4 invariant | Evidence |
|---|---|
| The committed `uv.lock` is unchanged; the milestone changes the command that installs it, never its contents | `shasum` before and after are identical (`68e1f35e…`), `cmp` against the pre-change copy reports no difference, `git status --short uv.lock` is empty (§4) |
| The resolved dependency set is unchanged; the gate must install exactly the versions the committed lockfile records | `--locked` installs exactly what the lockfile records and only fails when the lockfile no longer satisfies the project metadata. No dependency was added, removed, or re-versioned, and `pyproject.toml` is byte-identical (`0b3af749…`) |
| No workflow may re-resolve the project during a CI job | The gate now fails closed on a stale lockfile, and a lockfile that needs updating is an error under `--locked`, so the gate cannot rewrite it. The sandbox shows `uv sync --frozen --offline` exiting 0 on a drifted tree while `uv sync --locked --offline` exits 1 (§4) |
| The release workflow's `--no-project --with maturin==1.14.1` isolation is unchanged | `test_the_release_workflow_keeps_its_no_project_isolation` — pass; exactly 8 sites, unchanged by `git diff` (the release workflow is not in this milestone's change set at all) |
| The release artifact, the publication gate, the installed-owner detection, and the release documentation contract are unchanged | None of `.github/workflows/release.yml`, `scripts/build_release_artifacts.py`, `scripts/qualify_release_wheel.py`, `scripts/create_release_manifest.py`, `scripts/validate_release_artifacts.py`, or `scripts/install.sh` is in the change set |
| The action pins in every covered workflow are unchanged | `git diff` over the four workflows shows one line changed in each, and only the flag on that line. `test_every_covered_workflow_passes_its_own_contract` and `test_validator_rejects_a_tag_style_action_pin` still pass |
| The pinned action SHAs and the release version derivation are unchanged | `test_qualification_workflows_derive_the_version_from_the_authority` and `test_derived_version_matches_the_cargo_version` pass; `test_no_covered_workflow_hardcodes_an_eggpool_release_version` passes |
| The release documentation validator's checks and its machine-readable report shape are unchanged | `uv run python scripts/validate_release_docs.py` — exit 0, `status: pass`, 7 docs checked, `production_release: "published 0.8.2"`, same four keys and the same values as before. Only the interpreter that runs it changed |
| The tooling test suite's pass count does not decrease, and no tooling test is deleted or skipped to accommodate this milestone | The suite is green on the current tree: 251 passed and 1 skipped, 0 failed, against 244 passed and 1 skipped in M007's closure record. `git diff --stat` over `tests/tooling/` shows two files, no test removed, no assertion weakened, and no skip marker added |

## 6. Failure and recovery review

| Plan §8 concern | Evidence |
|---|---|
| Failure — a drifted `pyproject.toml` now fails the CI job at the environment-install step, with a message naming the lockfile and the remedy | Sandbox step 2: `The lockfile at `uv.lock` needs to be updated, but `--locked` was provided. To update the lockfile, run `uv lock`.`, exit 1 (§4). The failure is located at the gate rather than surfacing later as an unexplained resolution difference |
| Failure — the release-docs guard test no longer depends on `uv` being on `PATH` and no longer depends on the resolver reaching a network | The test runs `[sys.executable, str(ROOT / "scripts/validate_release_docs.py")]`, and the script is pure standard library. `test_the_release_docs_guard_runs_the_validator_without_uv` holds the property; mutation 2 shows it failing when the `uv` subprocess is restored |
| Cancellation — a cancelled CI job leaves the workspace as it found it | The gate does not modify the lockfile: under `--locked` a lockfile that needs updating is an error, not an update. The previous sequence could leave a rewritten `uv.lock` behind (sandbox step 4 rewrote it); the new sequence cannot |
| Restart — a re-run of a failed job starts from a clean checkout and repeats the same gate | The gate is deterministic and read-only with respect to the lockfile, so the same drifted tree fails identically on every re-run |
| Contention | Not applicable. The change is a workflow-command change and a test-argv change; it introduces no lock, gate, or shared state |
| Concurrency | Not applicable |
| Duplicate delivery / idempotency | The four gates and the two test edits are idempotent by construction: re-applying them yields the same bytes, and the guard's derived project-installing set is recomputed from the files on every run rather than cached |

## 7. Migration and compatibility review

- No migration. `uv.lock` and `pyproject.toml` are unchanged, byte-for-byte.
- No published API, endpoint, response shape, configuration key, release
  artifact, action pin, or release documentation content changes.
- The release documentation validator's behaviour and its machine-readable
  report shape are unchanged; only the interpreter that runs it changes, from
  the `uv`-managed one to the interpreter already running the test suite.
- No runtime, Rust workspace, or database change. No `cargo` gate is required
  (plan §11), and the repository's full Rust gate remains the pre-push
  requirement for any commit that touches the Rust workspace; this milestone
  does not.
- The only observable difference is in CI: a job whose `pyproject.toml` has
  drifted from its `uv.lock` now fails at the environment-install step with a
  message naming the remedy, instead of proceeding against a silently
  re-resolved dependency set. On a consistent tree — this one — nothing
  changes.
- Backward compatibility for operators: the commands the workflows run
  (`uv sync --locked`) are supported by the uv version the repository already
  pins through `astral-sh/setup-uv`, and no flag is combined with
  `--frozen`, which would suppress the diagnostic naming the file and the
  remedy.

## 8. Security review

- No credential, prompt, raw body, or cache key appears in the closure
  record, in the guard, or in any diagnostic this milestone introduces. The
  four added guards read workflow text and one Python module's source; they
  print only workflow-relative paths, run-step command text, and the
  release-docs test's file path.
- No secret is read, written, or emitted. The change removes a
  network-capable subprocess (`uv run`) from a test that runs on every CI
  job, which strictly reduces the subprocess surface of the tooling suite.
- The change does not alter the release workflow's secret handling, its
  `contents: read` default, its job-scoped permissions, or the action-pin
  rule. `scripts/validate_release_workflow.py`'s `FORBIDDEN_SECRET_MARKERS`
  check is untouched.
- No privilege bound, DoS bound, or audit behaviour changes.
- The sandbox work was performed entirely under `/tmp` against an isolated
  project; no repository file was read into or written out of it, and no
  network access was used or required.

## 9. Documentation and operations

- `plans/subsystems/deployment-packaging-roadmap.md` — the milestone status
  table row for milestone 8 now reads `closed` and points at
  `plans/closure/deployment-packaging/008-status.md`; the §6 dependency note
  for M008 now follows the file's existing convention for a closed
  milestone.
- `plans/closure/deployment-packaging/008-status.md` — this record.
- The four workflows — the command changes. No comment change was needed;
  the flag is self-describing.
- No change to `AGENTS.md`, to `docs/`, to `architecture/`, or to any release
  documentation.
- Operator-facing behaviour: a CI job whose project metadata has drifted
  from the committed lockfile now fails at the environment-install step with
  the message `The lockfile at `uv.lock` needs to be updated, but `--locked`
  was provided. To update the lockfile, run `uv lock`.` The remedy is to run
  `uv lock` and commit the resulting `uv.lock`. No operator state, install,
  or runtime configuration is affected.

## 10. Unresolved findings

**This milestone has no unresolved findings.** The table below is empty: no
critical, high, medium, or low finding is outstanding against M008.

**One finding that this milestone raised during its implementation has been
resolved upstream of it and is therefore not listed here.** While the
milestone was being implemented the tooling suite reported one failure,
`tests/tooling/test_persistence_current_docs.py::test_roadmap_states_the_current_schema_census`,
because the `v64` migration had landed in the repository before this milestone
started and the Persistence roadmap still stated migration chain `v1–v63`.
M008 reproduced the failure against a pristine extract of the baseline with
none of its own files present (§4), established that it was not caused by
this milestone, and reported it rather than suppressing it. The Persistence
roadmap's §8 "Cross-cutting requirements" now states `v1–v64`; the guard
passes and the tooling suite is fully green — **251 passed, 1 skipped, 0
failed**. The fix is upstream of M008, in a commit that is not part of this
milestone, and M008 neither edited that roadmap nor its guard.

The one item the plan itself defers is recorded here for completeness rather
than as a finding against this milestone, because plan §5 puts it explicitly
out of scope:

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low (deferred by plan §5, not an M008 defect) | `.github/workflows/qualification-public-release.yml` installs the project environment implicitly through `uv run python …` rather than through an explicit `uv sync --locked` gate, so it has no gate of its own | The workflow inherits the committed lockfile through `uv run`'s implicit sync, so it qualifies the committed resolution on a consistent tree; it would re-resolve silently on a drifted tree, exactly as the corrected gates used to. The plan §16 note applies: such a workflow must be added to the guard's set rather than left ungated | Deferred by the plan's §5 (the repository's `uv run` invocations are explicitly out of scope, and the gate in front of them is the intended control). A future milestone should give this workflow an explicit `uv sync --locked` gate; the guard already derives the project-installing set from the workflow text, so that gate is covered automatically once added |

## 11. Roadmap disposition

**Milestone closed; no further dependency may proceed.** M008 is closed on the
evidence in §2 and §4: the four gates install the project environment with
`uv sync --locked`, the release-docs guard test spawns no `uv` process, the
new guard holds the workflows to the flag that detects lock drift and was
shown failing on a deliberate revert, the committed `uv.lock` and
`pyproject.toml` are byte-identical before and after, and the tooling suite's
pass count did not decrease.

`plans/subsystems/deployment-packaging-roadmap.md` is updated in this
milestone: milestone 8's status is `closed` and its closure-record column
points at `plans/closure/deployment-packaging/008-status.md`. The §6
dependency note for M008 now follows the roadmap's existing convention for a
closed milestone.

M008 had no dependency and none is unblocked by it. The milestone has no
unresolved findings (§10): the Persistence schema-census failure observed
during implementation was fixed upstream of this milestone and is now
resolved, and the ungated `uv run` in `qualification-public-release.yml` is
explicitly deferred by the plan rather than an M008 defect. The deployment
and packaging roadmap's own exit conditions are now met — every
release-related workflow derives the release version from the repository
authority, the release workflow contract is checked in ordinary CI, the
repository's ignore rules and tracked state agree, and the CI environment is
built from the committed lockfile or the job fails — so the registry's
subsystem disposition is the milestone owner's remaining decision.

## 12. Registry updates

The milestone owner applies the following in `plans/registry.md`, in the same
commit as this closure record. **This milestone does not edit
`plans/registry.md`; the updates below are described, not applied.**

1. **Move the Deployment and Packaging M008 row out of the "Dependency-ready
   implementation plans" table.** Delete the row

   ```
   | Deployment and packaging | M008 | ready | `plans/implementation/deployment-packaging/008-ci-dependency-lock-discipline.md` | none |
   ```

   from the table under the `## Dependency-ready implementation plans`
   heading. The Routing selection M008 row in the same table is unrelated
   and stays.

2. **Add a Deployment and Packaging M008 row to the "Recently closed"
   table.** The table has three columns — `Subsystem / plan`,
   `Disposition`, `Evidence` — and the new row follows the shape of the
   existing Deployment and packaging M007 row. The plan title is
   `Deployment and packaging M008 — CI dependency-lock discipline`; the
   disposition records that the four CI gates that install the Python
   environment now use `uv sync --locked`, so a `pyproject.toml` that has
   drifted from the committed lockfile fails the job at the environment-install
   step with a message naming the lockfile and the remedy instead of the
   first `uv run` silently re-resolving and rewriting the lockfile; that the
   release-docs guard test now runs the validator under the test suite's own
   interpreter and spawns no `uv` process; that a guard derived from the
   workflows' own `run:` steps holds every project-environment install to
   `--locked` while leaving the release workflow's `--no-project --with
   maturin==1.14.1` isolation alone, and was shown failing on a deliberate
   revert; that the committed `uv.lock` and `pyproject.toml` are
   byte-identical before and after; and that the release artifact, the
   publication gate, the action pins, the release version derivation, and the
   release documentation contract are unchanged. The evidence column cites
   `plans/closure/deployment-packaging/008-status.md` and the implementing
   commit.

3. **Update the Deployment and packaging subsystem row.** The row's
   milestone cell currently reads `M008 ready — CI dependency-lock
   discipline` and must read `M008 closed — CI dependency-lock discipline`.
   Its note currently reads "Reopened on evidence at baseline `e6822d8f78`:
   M001–M005 remain closed. M006 is closed through M007. M008 is registered
   and has no dependency." and must record that M008 is closed, that its
   closure record is `plans/closure/deployment-packaging/008-status.md`,
   and that the Deployment and Packaging M008 row has moved from the
   "Dependency-ready implementation plans" table to the "Recently closed"
   table. The subsystem's own status field is left as the owner decides it;
   this milestone does not change it.

The implementing commit is cited as:

`901d5182`

The source roadmap update in `plans/subsystems/deployment-packaging-roadmap.md`
is applied by this milestone and is not part of the registry change.
