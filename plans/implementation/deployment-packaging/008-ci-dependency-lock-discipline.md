# Deployment and Packaging Milestone 008 — CI dependency-lock discipline

Status: implemented

Repository baseline: `1929abf5a725cc7faac0f866622afa7af017694a`

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for release artifacts and qualification
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None. This milestone changes how the CI environment is constructed, not which
  dependencies are selected, not the runtime, and not a public contract. The
  lockfile's contents are unchanged, so there is no dependency-selection
  decision to record. §2 states the reasoning.

Primary class: invariant

## 1. Objective

Make the CI environment be built from the committed lockfile, or fail — and
never silently re-resolve.

Concretely: the four CI gates that install the Python environment with
`uv sync --frozen` must use `uv sync --locked` instead, so a `pyproject.toml`
that has drifted from `uv.lock` fails the job at the gate instead of being
silently re-resolved by the first `uv run` that follows it.

## 2. Why this milestone is ready

**Ready.** This milestone changes no runtime behaviour, no dependency
selection, no published contract, and no configuration key. The lockfile's
contents do not change; only the CI command that installs it changes, from a
flag that skips the consistency check to the flag that enforces it. That is a
tooling-hygiene correction inside the deployment and packaging subsystem's
ownership of the release and qualification workflows, and it does not reach the
repository's ADR threshold.

The milestone has no hard dependency on another milestone, and every defect it
addresses is reproducible from the repository at the stated baseline.

**A note on the original finding.** A prior review flagged
`tests/tooling/test_release_docs.py:29` for invoking `uv run` without
`--frozen`. That framing does not survive measurement: the repository has 80
`uv run` invocations across `.github/workflows/`, `scripts/`, and `tests/`,
and **none** of them carries `--frozen`, `--locked`, `--no-sync`, or
`--offline`. The flagged line follows the repository's uniform convention rather
than departing from it, and the hazard it described does not fire while the
lockfile is current — `uv lock --check --offline` exits 0 on this tree.

The defect is real, but it is one line away from where the finding pointed. It
is in the **gate**, not in the invocation. This milestone fixes the gate, and
it additionally removes the flagged subprocess in favour of the repository's
own established precedent, because that is a strict improvement regardless of
the flag question.

## 3. Current implementation evidence

**The gate installs a stale lock and exits 0 when the lock has drifted.**
`uv sync --frozen` installs the versions recorded in `uv.lock` without
checking that `uv.lock` still satisfies `pyproject.toml`. This was measured
directly against uv 0.11.16 in an isolated project under `/tmp`, with a
`pyproject.toml` deliberately drifted from its `uv.lock`:

```
$ uv sync --frozen --offline
Checked in 0.12ms
exit=0
```

The drift is not detected. The job proceeds to install the old resolution.

**The next `uv run` then re-resolves silently and rewrites the lockfile.**
The same isolated project, with a local path dependency whose version had been
changed:

```
$ uv run --offline python -c 'pass'
   Building pkg @ file:///tmp/eggpool-lockprobe2/vendor
      Built pkg @ file:///tmp/eggpool-lockprobe2/vendor
Installed 1 package in 2ms
exit=0

$ diff uv.lock uv.lock.before
18c18
< version = "0.1.0"
---
> version = "0.2.0"
```

`uv.lock` was rewritten in the working tree and the command still exited 0. So
the CI sequence is: install the stale lock, then re-resolve and rewrite the
lock, then build against a resolution that is not the committed one. **No test
fails and no step reports the substitution.** The committed lockfile is not
what was qualified.

**The flag that detects the drift exists and behaves as required.** The same
drifted tree:

```
$ uv sync --locked --offline
Resolved 2 packages in 47ms
The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.
To update the lockfile, run `uv lock`.
exit=1
```

and the non-mutating check:

```
$ uv lock --check --offline
Resolved 2 packages in 38ms
The lockfile at `uv.lock` needs to be updated, but `--check` was provided.
To update the lockfile, run `uv lock`.
exit=1
```

`--locked` therefore converts a silent substitution into a loud, correctly
located failure, before any `uv run` executes.

**Four CI gates use the flag that skips the check.**

- `.github/workflows/ci.yml:55`
- `.github/workflows/installer-shell-qualification.yml:49`
- `.github/workflows/qualification-portability.yml:39`
- `.github/workflows/qualification-release-artifacts.yml:66`

Each is `- run: uv sync --frozen`.

**The release workflow is already isolated.** `.github/workflows/release.yml`
uses `uv run --no-project --with maturin==1.14.1` at eight sites (`:98`,
`:103`, `:138`, `:143`, `:175`, `:180`, `:355`, `:362`). `--no-project` means
the repository's project and lockfile are not consulted at all, so the release
workflow is not exposed to this defect and is not changed by this milestone.

**The flagged subprocess is unnecessary.** `tests/tooling/test_release_docs.py:28-34`
runs `["uv", "run", "python", "scripts/validate_release_docs.py"]`.
`scripts/validate_release_docs.py` has no third-party imports — it is pure
standard library — so the `uv run` wrapper buys nothing functionally, and it
is a network-capable subprocess in a test that runs on every CI job.

The repository already has the better precedent one file away:
`tests/tooling/test_installer.py:14` runs
`[sys.executable, str(ROOT / "scripts/qualify_quick_installer.py")]`.

**The tree is currently consistent.** `uv lock --check --offline` exits 0
against this repository, so the change does not introduce a failing gate on
today's tree; it makes the gate meaningful for tomorrow's tree.

**A workflow-content guard already exists as a precedent.**
`tests/tooling/test_release_workflow_versions.py` reads
`CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"` (`:35`) and asserts
properties of the workflow files. The lock-discipline guard belongs with it.

## 4. Invariants that must not regress

- The committed `uv.lock` is unchanged. This milestone changes the command
  that installs it, never its contents.
- The resolved dependency set is unchanged. The gate must install exactly the
  versions the committed lockfile records.
- No workflow may re-resolve the project during a CI job. A `uv run` that
  re-locks must not be able to do so unnoticed.
- The release workflow's `--no-project --with maturin==1.14.1` isolation is
  unchanged.
- The release artifact, the publication gate, the installed-owner detection,
  and the release documentation contract are unchanged.
- The action pins in every covered workflow are unchanged.
- The pinned action SHAs and the release version derivation are unchanged.
- The release documentation validator's checks and its machine-readable report
  shape are unchanged.
- The tooling test suite's pass count does not decrease, and no tooling test
  is deleted or skipped to accommodate this milestone.

## 5. Scope

### In scope

- The four `uv sync --frozen` gates in the CI and qualification workflows.
- The `uv run` subprocess in `tests/tooling/test_release_docs.py`.
- A guard that keeps the CI workflows on the flag that detects lock drift.

### Explicitly out of scope

- The contents of `uv.lock` and `pyproject.toml`. No dependency is added,
  removed, or re-versioned.
- The 80 `uv run` invocations across the repository. They follow the
  repository's uniform convention, and after this milestone the gate in front
  of them guarantees the lockfile is current, so each of them installs the
  committed resolution. Adding a lock flag to each is noise, not safety.
- The release workflow's `--no-project --with maturin==1.14.1` invocations,
  which do not consult the project or its lockfile.
- The release artifact, the publication gate, the installer, and the release
  documentation content.
- The action SHA pins and the release version derivation, which are the
  deployment and packaging subsystem's M006 and M007 subjects and are closed.
- The runtime, the Rust workspace, and every `cargo` gate.

## 6. Required production changes

### 6.1 The four CI gates detect lock drift

Each of the four sites changes from `uv sync --frozen` to `uv sync --locked`:

- `.github/workflows/ci.yml:55`
- `.github/workflows/installer-shell-qualification.yml:49`
- `.github/workflows/qualification-portability.yml:39`
- `.github/workflows/qualification-release-artifacts.yml:66`

`--locked` installs exactly what `uv.lock` records and fails when
`uv.lock` does not satisfy `pyproject.toml`. It does not rewrite the lockfile,
because a lockfile that needs updating is an error under `--locked`.

`--frozen` and `--locked` are not combined: `--locked` already refuses to
update the lockfile, and adding `--frozen` on top would only suppress the
diagnostic that names the file and the remedy.

### 6.2 The release-docs guard test stops shelling out through uv

`tests/tooling/test_release_docs.py:28-34` runs
`["uv", "run", "python", "scripts/validate_release_docs.py"]`. It runs
`[sys.executable, str(ROOT / "scripts/validate_release_docs.py")]` instead,
matching `tests/tooling/test_installer.py:14`.

`sys` is added to the imports. The assertions are unchanged: the test still
asserts a zero exit status, a machine-readable JSON report on stdout, the
`pass` status, the published release, and that the release workflow contains
neither `uv build` nor `uv publish`.

### 6.3 A guard keeps the workflows on the flag

`tests/tooling/test_release_workflow_versions.py` gains a guard that every
workflow which installs the project environment uses `uv sync --locked`, and
that no workflow uses `uv sync --frozen` for that purpose.

The guard reads the workflow files as text and asserts on the `run:` steps,
so a workflow that installs the environment through some other mechanism does
not fail the guard spuriously. The guard covers the four sites this milestone
changes, and it fails if one of them regresses to `--frozen`.

The guard is written against the repository's own convention for
workflow-content assertions, which this file already establishes.

## 7. Ordered work packages

### Work package A — Replace the four lock gates

Files: `.github/workflows/ci.yml`,
`.github/workflows/installer-shell-qualification.yml`,
`.github/workflows/qualification-portability.yml`,
`.github/workflows/qualification-release-artifacts.yml`.

- Change `uv sync --frozen` to `uv sync --locked` at the four sites.
- Change nothing else in the workflows: no step, no action pin, no version
  derivation, no permission.

Acceptance:

- No workflow contains `uv sync --frozen`.
- Every workflow that installs the project environment uses `uv sync --locked`.

### Work package B — Remove the uv subprocess from the release-docs guard test

Files: `tests/tooling/test_release_docs.py`.

- Replace the `uv run` argv with `[sys.executable, str(ROOT / "scripts/validate_release_docs.py")]`.
- Add the `sys` import.
- Leave every assertion unchanged.

Acceptance:

- The test still asserts the machine-readable report and the workflow content
  checks.
- The test no longer spawns `uv`.

### Work package C — Guard the lock discipline

Files: `tests/tooling/test_release_workflow_versions.py`.

- Assert that no workflow in the repository installs the project environment
  with `uv sync --frozen`.
- Assert that each workflow that installs the project environment uses
  `uv sync --locked`.
- Assert that the release workflow's `--no-project --with maturin==1.14.1`
  isolation is unchanged, so the guard does not push that workflow into the
  project-installing set.

Acceptance:

- The guard passes on the corrected tree.
- The guard fails when one of the four gates is reverted to `--frozen`, shown
  as a mutation check in the closure record.

## 8. Failure, cancellation, restart, contention semantics

**Failure.** A drifted `pyproject.toml` now fails the CI job at the
environment-install step, with a message naming the lockfile and the remedy
(`run uv lock`). The failure is located at the gate rather than surfacing later
as an unexplained resolution difference.

**Failure.** The release-docs guard test no longer depends on `uv` being on
`PATH` and no longer depends on the resolver being able to reach a network.
The script it runs is pure standard library, so the test runs under the
interpreter that is already running the test suite.

**Cancellation.** A cancelled CI job leaves the workspace as it found it. The
gate does not modify the lockfile, so a cancelled job cannot leave a rewritten
`uv.lock` behind — which the previous sequence could.

**Restart.** A re-run of a failed job starts from a clean checkout and repeats
the same gate, so the failure is reproducible.

**Contention.** Not applicable. The change is a workflow-command change and a
test-argv change; it introduces no lock, gate, or shared state.

**Concurrency.** Not applicable.

## 9. Compatibility and migration

No migration. `uv.lock` and `pyproject.toml` are unchanged.

No published API, endpoint, response shape, configuration key, release
artifact, action pin, or release documentation content changes.

The release documentation validator's behaviour and its machine-readable report
shape are unchanged; only the interpreter that runs it changes.

The only observable difference is in CI: a job whose `pyproject.toml` has
drifted from its `uv.lock` now fails at the environment-install step with a
message naming the remedy, instead of proceeding against a silently re-resolved
dependency set. On a consistent tree, nothing changes.

## 10. Required tests

- A guard asserts that no workflow installs the project environment with
  `uv sync --frozen`.
- A guard asserts that every workflow that installs the project environment
  uses `uv sync --locked`.
- A guard asserts that the release workflow's `--no-project --with
  maturin==1.14.1` isolation is unchanged.
- The existing release-docs guard test continues to assert the
  machine-readable report and the workflow content checks, without a `uv`
  subprocess.
- The existing release workflow guards — the version derivation, the
  hardcoded-literal check, the action-pin check, and the per-workflow contract
  check — continue to pass.

## 11. Required verification commands

```bash
uv sync --locked
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
```

The Rust workspace is not touched by this milestone, so no `cargo` gate is
required for it. The repository's full Rust gate remains the pre-push
requirement for any commit that touches the Rust workspace; this milestone does
not.

## 12. Documentation updates

- `plans/subsystems/deployment-packaging-roadmap.md` — the milestone entry
  and the milestone status table gain the row.
- `.github/workflows/ci.yml` and the three qualification workflows — the
  command changes. No comment change is required; the flag is
  self-describing.

No change to `AGENTS.md`, to `docs/`, to `architecture/`, or to any release
documentation.

## 13. Acceptance criteria

- No workflow installs the project environment with `uv sync --frozen`.
- Every workflow that installs the project environment uses `uv sync --locked`.
- A drifted `pyproject.toml` fails the CI gate with a message naming the
  lockfile and the remedy, demonstrated in a sandbox and not merely asserted.
- The release-docs guard test runs the validator under the test suite's own
  interpreter and spawns no `uv` process.
- A guard fails if any of the four gates regresses to `--frozen`.
- The committed `uv.lock` and `pyproject.toml` are byte-identical before and
  after the change.
- The tooling test suite passes with no decrease in its pass count, and the
  linters and the release-documentation validator pass.

## 14. Stop conditions

Stop and report if any of the following is observed:

- `uv.lock` or `pyproject.toml` would change as a side effect of the
  milestone.
- `--locked` would reject the repository's current, consistent lockfile. That
  would mean the committed lockfile is already out of date, which is a
  different defect and is reported rather than papered over.
- The release workflow's `--no-project` isolation would have to change.
- An action pin, a release version derivation, or the release artifact would
  have to change.
- The release documentation validator's report shape would have to change for
  the test to pass.
- A guard would need to be weakened, skipped, or given a baseline allowlist to
  pass.
- The tooling suite's pass count would decrease.

## 15. Closure evidence required

The closure record must contain:

- The requirement-to-evidence matrix mapping every §13 acceptance criterion to
  the test or command that proves it, with the observed result.
- The verification table for every command in §11, with its exit status and
  the pass counts.
- The sandbox demonstration of the drift behaviour: the `--frozen` result, the
  `--locked` result, and the `uv run` rewrite, with the commands and their
  outputs.
- A mutation-check table: at least one deliberate perturbation per corrected
  behaviour — reverting one gate to `--frozen`, and restoring the `uv run`
  subprocess — each shown to fail the named guard.
- The evidence that `uv.lock` and `pyproject.toml` are byte-identical before
  and after the change.
- The invariant, security, compatibility, and migration reviews, each stating
  what was checked and what was deliberately left unchanged.
- The unresolved-findings table, which may be empty, listing anything the
  milestone did not address.

## 16. Handoff notes

- The registry's deployment and packaging row and the "Dependency-ready
  implementation plans" table carry this milestone's status. One commit changes
  one status.
- The closed deployment and packaging records
  `plans/closure/deployment-packaging/006-status.md` and
  `plans/closure/deployment-packaging/007-status.md` are immutable and are
  not edited by this milestone.
- The 80 `uv run` invocations across the repository are deliberately left
  unchanged. The gate in front of them guarantees the lockfile is current, so
  each of them installs the committed resolution. If a future milestone finds a
  workflow that installs the project environment outside the four gated sites,
  that workflow must be added to the guard's set rather than left ungated.
- The original finding about `tests/tooling/test_release_docs.py:29` is
  discharged by work package B, with the measurement that showed the line was
  conventional rather than an outlier recorded in §3 and in the closure
  record.
