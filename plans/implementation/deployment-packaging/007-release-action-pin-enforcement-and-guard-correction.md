# Deployment and Packaging Milestone 007 — Release action-pin enforcement and guard correction

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10` (branch `main`;
the M006 implementation and closure are uncommitted in the working tree, and the
corrective implementation of this plan is likewise uncommitted in the working
tree on top of them)

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#7-milestones`
- `plans/subsystems/deployment-packaging-roadmap.md#12-milestone-status`

Original plan and closure corrected by this pass:

- `plans/implementation/deployment-packaging/006-release-artifact-version-derivation-and-artifact-hygiene.md`
- `plans/closure/deployment-packaging/006-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for release identity
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. The release artifact contract, the published version, the
  release identity rules, the publication gate, and the installed-owner
  detection are unchanged.
- Stop for architecture review if the correction would change the release
  identity rules or the publication gate.

Primary class: invariant

## 1. Objective

Make the release workflows' action pins immutable **and** enforced by the
validator, and make the milestone's cited guards actually assert the properties
the M006 closure claimed — one bounded outcome covering the three findings the
M006 review left open: the unqualified action-pin rule, the tautological
regression test, and the artifact-hygiene guard that did not cover the
disclosure it made.

## 2. Why this milestone is ready

This is a corrective pass over a closed milestone. The M006 implementation and
its closure record are complete
(`plans/closure/deployment-packaging/006-status.md`, `Status: closed`), the
release version-derivation design is unchanged and correct, and the artifact
retention decision is unchanged and correct. There are no hard dependencies, no
interface dependencies, and no new dependencies of any kind: the corrective work
is a workflow-file edit, a validator call, and two guard rewrites, all inside
files the repository already owns.

## 3. Current implementation evidence

The state this corrective pass starts from is the M006-closed state: the M006
implementation is uncommitted in the working tree, so the only committed
reference point is baseline `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`. The
three findings below are stated against that state, with the repository
anchors that corroborate each one.

### 3.1 Finding 1 — the qualification workflows were never held to the immutable-SHA action-pin rule

`scripts/validate_release_workflow.py` defines `_validate_action_pins(text)`,
which rejects any `uses:` reference whose revision is not a 40-hex-character
SHA and raises
`WorkflowValidationError("action is not pinned to an immutable SHA: <reference>")`.

The helper was called from `validate_workflow_text` — the contract used for
`.github/workflows/release.yml` — but **not** from
`validate_qualification_workflow_text`, the contract used for **both**
`.github/workflows/qualification-public-release.yml` and
`.github/workflows/qualification-release-artifacts.yml`. The gap is stated in
the M006 closure record's own §10 row, which asks for a follow-up that would
"pin the qualification workflows to SHAs **and extend the SHA-pin rule in
`validate_qualification_workflow_text`**" — i.e. at M006 closure the rule did
not reach the qualification contract.

`.github/workflows/qualification-release-artifacts.yml` carried 11 `uses:`
lines, **all** tag-style. At baseline `e6822d8f` the file carried 9, all
tag-style:

- `actions/checkout@v4` (twice)
- `astral-sh/setup-uv@v4` (twice)
- `dtolnay/rust-toolchain@stable`
- `mlugg/setup-zig@v1`
- `actions/upload-artifact@v4` (twice)
- `actions/download-artifact@v4`

M006's new `resolve-version` job contributed the remaining 2 `uses:` lines
(`actions/checkout` and `astral-sh/setup-uv`). The validator still passed on
the file, because nothing in the qualification contract asked about pins.

`.github/workflows/qualification-public-release.yml` was **already** SHA-pinned
at baseline `e6822d8f` (`actions/checkout@34e1…`, `astral-sh/setup-uv@3259…`,
`actions/upload-artifact@ea16…`). Only **one** of the two qualification
workflows used tag pins.

Two statements in the M006 closure record are therefore inaccurate as written,
and this corrective pass supersedes them:

- §8, "The workflows' action pins are unchanged, as the plan requires" — false,
  because M006 *added* `uses:` references (the `resolve-version` job's
  `actions/checkout` and `astral-sh/setup-uv` steps) and left all 11 references
  tag-style.
- §10, "The two qualification workflows still use tag-style action pins
  (`actions/checkout@v4`, `astral-sh/setup-uv@v4`)" — false for one of the
  two, because the public-release workflow was already SHA-pinned.

### 3.2 Finding 2 — the cited regression test asserted nothing about the repository

`tests/tooling/test_release_workflow_versions.py::test_stale_version_globs_do_not_match_the_current_artifacts`
read no workflow file and touched no build artifact; it compared hardcoded glob
patterns against hardcoded filename strings and passed unconditionally. The
test had two globs and four synthetic names.

The M006 closure record cited that very test in §2 as the evidence that "The
four pre-correction globs match **0 of the 6** artifacts the build produces at
Cargo version `0.8.2`; the derived globs match all of them" — a claim about the
repository that the test did not make.

The pre-corrective body is not recoverable from git: `tests/tooling/test_release_workflow_versions.py`
is untracked (`git ls-files tests/tooling/test_release_workflow_versions.py`
returns nothing), and the bytecode in `tests/tooling/__pycache__/` postdates
the correction. The M006 closure record's own citation is the repository
evidence of the gap.

### 3.3 Finding 3 — the artifact-hygiene guard did not cover the disclosure it made

`tests/tooling/test_artifact_hygiene.py` asserted only that a hardcoded list of
**flat** generated-output paths under `artifacts/qualification/` were
git-ignored (`test_generated_qualification_output_stays_untracked`).

M006 replaced the blanket `.gitignore` rule `/artifacts/qualification/` with
`/artifacts/qualification/*` (`.gitignore:77`) plus 18 explicit re-inclusions:
7 retained evidence trees (`.gitignore:78`–`84`) and 11 retained loose
evidence files (`.gitignore:85`–`95`). A generated-output shape written
**inside** a re-included tree is therefore not ignored, while the same shape at
the top level is. Measured on the current tree with
`git check-ignore --no-index`, against a generated-output shape named
`007-run.json` placed in three locations (none of the three probe paths is a
repository file; the table names the containing directory):

| Probe: a generated-output shape named `007-run.json` | `git check-ignore --no-index` |
|---|---|
| at the top level of `artifacts/qualification/` | IGNORED |
| inside the retained tree `artifacts/qualification/m004/` | NOT IGNORED |
| inside the retained corpus `artifacts/qualification/on-prem-concurrency-performance/ordinary-server/` | NOT IGNORED |

`docs/releasing.md:117`–`123` already discloses the `ordinary-server` case as
the documented exception, so the disclosure is accurate; the *guard* did not
cover it.

### 3.4 Why the M006 verification missed all three

- **Finding 1** was missed because the milestone's own guard ran the
  qualification contract against the qualification workflows and compared only
  the derived-version rule. The pin rule lived in a *different* contract
  (`validate_workflow_text`) that was never applied to the qualification
  workflows, so no test exercised pin enforcement on them. The M006 closure
  then recorded the pin state by assertion rather than by measurement, and the
  assertion was wrong in both directions (§8 and §10 above).
- **Finding 2** was missed because the test was written against synthetic
  strings rather than against the repository, so it could not fail. A guard
  that cannot fail is not evidence, however green it reports.
- **Finding 3** was missed because the guard enumerated a hardcoded flat list
  of generated-output paths. The hardcoded list was consistent with the
  pre-M006 ignore rule and was never re-derived from the post-M006 rule, so
  the guard silently tested the old contract against the new state.

## 4. Invariants that must not regress

- No covered workflow hardcodes a catalogued EggPool release version.
- Every covered workflow derives the release version from the repository
  version authority (`scripts/validate_release_identity.py`), publishes it as a
  job output, and consumes it in every artifact glob and version assertion.
- Every `uses:` reference in a covered workflow is pinned to an immutable
  40-hex-character SHA.
- The tracked artifact corpus stays tracked, and the tracked-and-ignored
  overlap stays zero.
- The release catalog
  (`rust/assets/catalog/installable-releases.json`) stays the version
  authority; no covered workflow or guard derives the version from anywhere
  else.
- No credential, prompt, raw body, or cache key enters a tracked artifact, and
  no operator-identifying path enters a tracked artifact.
- The release artifact naming, manifest schema, packaging layout, publication
  gate, and installed-owner detection are unchanged.

## 5. Scope

### In scope

- Finding 1: the immutable-SHA action-pin rule as applied to the two
  qualification workflows — both the pins themselves and the validator
  coverage that enforces them.
- Finding 2: `test_stale_version_globs_do_not_match_the_current_artifacts`
  rewritten so that it reads the covered workflows, the release catalog, and
  the Cargo version, and so that it can fail; plus a guard that the validator
  rejects a tag-style action pin.
- Finding 3: the artifact-hygiene guard's coverage of the disclosure it makes
  — the retained-evidence list derived from `.gitignore` rather than
  hardcoded, the generated-output shapes inside the retained evidence trees
  asserted to stay untracked, and the `ordinary-server` corpus asserted to be
  deliberately un-ignored and tracked.

### Explicitly out of scope

- Changing the release catalog, its `version_authority` block, or the set of
  catalogued versions.
- Changing the version-derivation design: the `resolve-version` job, the
  derived-output contract, and the fail-closed version gate are unchanged.
- Changing the retained-evidence decision. The qualification artifacts stay
  tracked, the `.gitignore` re-inclusions stay, and no artifact is untracked or
  deleted.
- Making generated output **inside** a retained evidence tree git-ignored. That
  needs a `.gitignore` rule change per tree (`!<tree>/` followed by
  `/<tree>/*`), which would change the retention contract; it is recorded as a
  residual finding in the closure record, not fixed here.
- Any change to `.github/workflows/release.yml`, `.github/workflows/ci.yml`, or
  `.github/workflows/qualification-public-release.yml`.
- Any Rust, `Cargo.toml`, manifest-schema, or runtime change.
- Any change to `docs/releasing.md` unless it is inaccurate (it is accurate;
  see §12).

## 6. Required production changes

- **`.github/workflows/qualification-release-artifacts.yml`** — every `uses:`
  reference must be pinned to the same immutable SHA the repository already
  uses in `.github/workflows/release.yml`. The `with:` blocks, the `if:`
  condition, and the step ordering are otherwise byte-identical. Pinning
  `dtolnay/rust-toolchain` by SHA requires adding `toolchain: stable` to its
  `with:` block, because the `@stable` tag used to carry that selection.
- **`scripts/validate_release_workflow.py`** —
  `validate_qualification_workflow_text` must call `_validate_action_pins(text)`
  so both qualification workflows are held to the immutable-SHA rule. The
  `release` contract, the CLI interface (single-path and no-argument), and
  every other function stay unchanged.
- **`tests/tooling/test_release_workflow_versions.py`** —
  `test_stale_version_globs_do_not_match_the_current_artifacts` is rewritten to
  read the three covered workflows, the release catalog, and the Cargo version,
  to build the real artifact set from the catalog's published wheels and
  `scripts/inspect_release_raw.raw_filename`, and to assert that no covered
  workflow carries a glob built from any superseded catalogued version, that
  every build job selects its wheel with the derived version, and that the
  derived glob selects all artifacts while every stale glob selects none. A new
  `test_validator_rejects_a_tag_style_action_pin` reintroduces a tag-style pin
  into each covered qualification workflow and asserts the validator rejects it.
- **`tests/tooling/test_artifact_hygiene.py`** — the retained-evidence list is
  derived from `.gitignore` instead of hardcoded; the guard asserts that
  generated-output shapes inside the retained evidence trees stay untracked;
  and the `ordinary-server` corpus is asserted to be deliberately un-ignored
  and tracked. The guard asserts the intended state: retention inside an
  evidence tree is an explicit operator decision, while flat generated output
  stays ignored.

## 7. Ordered work packages

### Work package A — Pin the qualification workflows and extend the validator's pin rule

Intent:

Make every `uses:` reference in every covered workflow an immutable SHA, and
make the validator enforce that for the qualification workflows rather than
only for the release workflow.

Required changes:

- Convert all 11 `uses:` references in
  `.github/workflows/qualification-release-artifacts.yml` to the canonical
  SHAs already used by `.github/workflows/release.yml`:
  `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5`,
  `astral-sh/setup-uv@3259c6206f993105e3a61b142c2d97bf4b9ef83d`,
  `dtolnay/rust-toolchain@d1031067263f94b142dd6c0ce24c5eb9d02d52a0`,
  `mlugg/setup-zig@53fc45b17fe98b52f92ee5ea08ff48a85a3e7eb7`,
  `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02`,
  `actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093`.
  Add `toolchain: stable` to the `dtolnay/rust-toolchain` step so the pinned
  action resolves the toolchain the `@stable` tag did. Leave every `with:`
  block, every `if:` condition, and the step ordering otherwise byte-identical.
- Call `_validate_action_pins(text)` from
  `validate_qualification_workflow_text` in
  `scripts/validate_release_workflow.py`, so both qualification workflows are
  held to the rule. Change nothing else in the validator.

Acceptance evidence:

- `tests/tooling/test_release_workflow_versions.py::test_validator_rejects_a_tag_style_action_pin`
  — the guard that fails when the validator stops enforcing the rule.
- `tests/tooling/test_release_workflow_versions.py::test_every_covered_workflow_passes_its_own_contract`
  — the current workflows satisfy the extended contract.
- `uv run python scripts/validate_release_workflow.py` — all three covered
  workflows validate, and the CLI exits 0.

### Work package B — Make the glob guard read the repository

Intent:

Replace the tautological glob test with one that reads the covered workflows,
the release catalog, and the Cargo version, so that it can fail when the
property it claims is broken.

Required changes:

- Rewrite `test_stale_version_globs_do_not_match_the_current_artifacts` in
  `tests/tooling/test_release_workflow_versions.py` to build the artifact set
  from the catalog's `supported_wheels` and
  `scripts.inspect_release_raw.raw_filename`, to assert that no covered
  workflow carries `eggpool-<superseded>-` in its text, that every build job
  selects its wheel with the derived version, that the derived glob selects
  every artifact, and that a glob built from every superseded catalogued
  version selects none.
- Add `test_validator_rejects_a_tag_style_action_pin`, parametrized over both
  covered qualification workflows, which reintroduces a tag-style pin and
  asserts the validator rejects it.

Acceptance evidence:

- `tests/tooling/test_release_workflow_versions.py::test_stale_version_globs_do_not_match_the_current_artifacts`
  — passes, and fails when a stale version literal is reintroduced into a real
  workflow glob.
- `tests/tooling/test_release_workflow_versions.py::test_validator_rejects_a_tag_style_action_pin`
  — passes, and fails with `DID NOT RAISE` when the validator's pin
  enforcement is removed.

### Work package C — Make the artifact-hygiene guard cover its own disclosure

Intent:

Assert the intended repository state instead of a hardcoded subset of it: the
retained evidence is derived from the ignore rule, retention inside an
evidence tree is an explicit operator decision, and flat generated output stays
ignored.

Required changes:

- Derive the retained-evidence directories and files from the `!` re-inclusion
  patterns in `.gitignore` in `tests/tooling/test_artifact_hygiene.py`.
- Assert that no generated-output shape inside a retained evidence tree is
  tracked, and keep the flat generated-output shapes asserted ignored.
- Assert that the `ordinary-server` corpus is deliberately un-ignored and
  tracked, so the documented exception is a tested property rather than a
  sentence in prose.

Acceptance evidence:

- `tests/tooling/test_artifact_hygiene.py::test_no_tracked_path_is_simultaneously_ignored_and_tracked`
  — the tracked-and-ignored overlap stays zero.
- `tests/tooling/test_artifact_hygiene.py::test_generated_output_inside_evidence_directories_stays_untracked`
  — the generated-output shapes inside the retained trees stay untracked.
- `tests/tooling/test_artifact_hygiene.py::test_the_retained_corpus_is_deliberately_unignored_and_tracked`
  — the `ordinary-server` corpus is un-ignored and tracked.

## 8. Failure, cancellation, restart, contention semantics

`scripts/validate_release_workflow.py` is a pure function over workflow text.
It performs no I/O beyond reading the workflow file it is given and the
release catalog it consults for the catalogued version set; it opens no
network connection, no database, and no subprocess. It has no concurrency, no
cancellation, no restart, no lease, and no partial-persistence surface, so
there is nothing to recover and no race to arbitrate.

The failure direction is a **false ACCEPT**, not a false reject. If the
validator's coverage of a rule is removed, or a `uses:` reference is left
unpinned, the validator returns success and the defect reaches a publish or
qualification run. That is why work package A's regression guard is written to
assert the *absence* of an exception: a validator that stops raising must make
`test_validator_rejects_a_tag_style_action_pin` fail, not pass silently.

The same false-ACCEPT direction applies to work package B's guard: a glob test
that reads no repository state cannot fail, so the M006 test passed
unconditionally. The rewritten guard is required to fail when a superseded
catalogued version is reintroduced into a covered workflow's glob.

There is no runtime, request-path, or process-lifecycle behavior affected by
this milestone.

## 9. Compatibility and migration

- No schema change. No storage change. No database migration.
- No configuration key is added, removed, or reclassified.
- No public HTTP, CLI, or Rust API contract change.
- The validator's CLI interface is preserved in both forms: the single-path
  invocation `scripts/validate_release_workflow.py
  .github/workflows/release.yml` and the no-argument invocation, which
  validates every covered workflow.
- The release catalog is untouched: it remains the version authority, with 59
  catalogued versions at the current baseline.
- The tracked artifact corpus is unchanged in count: 153 tracked paths under
  `artifacts/`, of which 148 are under `artifacts/qualification/`. The
  tracked-and-ignored overlap is 0.
- The published release artifact format, the manifest schema, the packaging
  layout, and the release version itself (`0.8.2`) are unchanged.
- The dispatch surface of the qualification workflows is unchanged: the
  workflows remain dispatch-only and read-only.

## 10. Required tests

- `tests/tooling/test_release_workflow_versions.py::test_validator_rejects_a_tag_style_action_pin`
  — the immutable-SHA rule is enforced for both covered qualification
  workflows.
- `tests/tooling/test_release_workflow_versions.py::test_stale_version_globs_do_not_match_the_current_artifacts`
  — the derived glob selects every artifact the build produces at the Cargo
  version, and a glob built from every superseded catalogued version selects
  none.
- `tests/tooling/test_release_workflow_versions.py::test_every_covered_workflow_passes_its_own_contract`
  — every covered workflow satisfies the extended contract.
- `tests/tooling/test_release_workflow_versions.py::test_no_covered_workflow_hardcodes_an_eggpool_release_version`
  — the derived-version rule still holds for all three covered workflows.
- `tests/tooling/test_artifact_hygiene.py::test_no_tracked_path_is_simultaneously_ignored_and_tracked`
  — the tracked-and-ignored overlap stays zero.
- `tests/tooling/test_artifact_hygiene.py::test_generated_output_inside_evidence_directories_stays_untracked`
  — the generated-output shapes inside the retained evidence trees stay
  untracked.
- `tests/tooling/test_artifact_hygiene.py::test_the_retained_corpus_is_deliberately_unignored_and_tracked`
  — the `ordinary-server` corpus is deliberately un-ignored and tracked.
- `tests/tooling/test_artifact_hygiene.py::test_retained_qualification_evidence_is_tracked`
  — the derived retained-evidence list agrees with the tracked state in both
  directions.
- The full Python tooling suite under `uv run pytest tests/tooling/` must stay
  green.
- Each rewritten or added guard must be shown **failing** on a deliberate
  perturbation and restored afterwards; a guard that was not shown failing is
  not accepted as evidence.

## 11. Required verification commands

```bash
uv run python scripts/validate_release_workflow.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_workflow.py .github/workflows/qualification-release-artifacts.yml
uv run python scripts/validate_release_workflow.py .github/workflows/qualification-public-release.yml
uv run pytest tests/tooling/test_release_workflow_versions.py tests/tooling/test_artifact_hygiene.py -q --tb=short
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
git ls-files artifacts/ | wc -l
git ls-files -z | git check-ignore --no-index -z --stdin | tr '\0' '\n' | grep -c .
```

Plus the narrowest focused targets for the change, and the deliberate-failure
demonstrations for each rewritten or added guard. The perturbations are made
with `cp` backups under `/tmp` and restored with `cp`, verified with `diff`;
`git checkout`, `git restore`, `git stash`, `git reset`, and `git clean` are
forbidden in this repository and must not be used.

## 12. Documentation updates

- `docs/releasing.md` — **no change required; it is accurate as written and
  must not be edited by this milestone.** Its "Qualification artifact
  retention" section (`docs/releasing.md:105`–`123`) already states the
  intended state accurately: the ignore rule is narrowed to generated run
  output, the retained evidence directories and files are re-included, no path
  is simultaneously ignored and tracked, and
  `scripts/qualification_ordinary_server.py` is named as the documented
  exception because it writes into the retained `ordinary-server` corpus. The
  "Release checks" block already lists all three covered workflows for the
  validator. The file owner may optionally add a sentence recording that every
  covered workflow is pinned to immutable SHAs; that is a disclosure
  improvement, not a correction, and this milestone does not make it.
- `architecture/deep-dive-deployment.md` — **owned outside this milestone; the
  owner must update it.** Its "Release workflow validation" section
  (`architecture/deep-dive-deployment.md:124`–`148`) still describes pinned
  actions as part of the release workflow's contract only, and still says "the
  action pins … are unchanged", which this corrective pass makes stale for the
  qualification workflows.
- `plans/registry.md` and `plans/subsystems/deployment-packaging-roadmap.md` —
  updated by this milestone's closure, not by this plan.

## 13. Acceptance criteria

- Every `uses:` reference in
  `.github/workflows/qualification-release-artifacts.yml` is pinned to a
  40-hex-character SHA, and every one of the 11 references matches the
  corresponding reference in `.github/workflows/release.yml`.
- The `dtolnay/rust-toolchain` step carries `toolchain: stable`, so the
  pinned action resolves the toolchain the `@stable` tag resolved.
- Every `with:` block, `if:` condition, and the step ordering of
  `.github/workflows/qualification-release-artifacts.yml` are otherwise
  unchanged from the M006 state.
- `validate_qualification_workflow_text` rejects a tag-style action
  reference, and the validator's CLI still accepts both the single-path and
  the no-argument invocation and exits 0 on the current tree.
- A tag-style action reference reintroduced into either covered qualification
  workflow is rejected by the validator.
- The rewritten glob test reads the three covered workflows, the release
  catalog, and the Cargo version, and it fails when a superseded catalogued
  version is reintroduced into a covered workflow's glob.
- The artifact-hygiene guard derives the retained-evidence list from
  `.gitignore`, asserts that the generated-output shapes inside the retained
  evidence trees stay untracked, and asserts that the `ordinary-server` corpus
  is un-ignored and tracked.
- The tracked-and-ignored overlap is 0, and the tracked artifact corpus is
  unchanged in count.
- The release catalog, the version-derivation design, the publication gate,
  the release artifact format, and the manifest schema are unchanged.
- The Python tooling suite passes, and `ruff format --check`, `ruff check`,
  and `pyright scripts/` are clean.

## 14. Stop conditions

Stop and report rather than improvise when:

- Converting a pin would change what a step does — for example if a pinned
  action cannot reproduce the behaviour the tag resolved. Adding
  `toolchain: stable` is the sanctioned way to keep the `dtolnay/rust-toolchain`
  step equivalent; any further behavioural difference is a stop.
- Extending the validator's pin rule would require a new dependency, a YAML
  runtime, or a change to the `release` contract.
- The retained-evidence decision would have to change, or a tracked artifact
  would have to be untracked, to satisfy a guard.
- Making generated output inside a retained evidence tree ignored would require
  a `.gitignore` rule change; that belongs to a separate change and is a stop
  for this milestone.
- Repository evidence contradicts an invariant stated in `AGENTS.md`.
- A perturbation cannot be restored exactly; the restore must be verified with
  `diff` before any further work continues.

## 15. Closure evidence required

- The implementation commit or the working-tree state, stated honestly.
- A requirement-to-evidence matrix with one row per corrected finding, citing
  the exact test name and its result.
- The exact verification commands run and their real outcomes, with counts,
  labeled honestly between local and CI runs.
- The deliberate-failure demonstration for each rewritten or added guard: the
  perturbation applied, the observed failure output, and the `diff`-verified
  restore.
- A statement that the tracked-and-ignored overlap is 0 and that the tracked
  artifact corpus count is unchanged.
- An explicit statement of which M006 closure statements this corrective pass
  supersedes, given the append-only discipline on closed records.
- The residual findings that remain open after this pass, with severities.

## 16. Handoff notes

- The Python tooling suite is the verification surface for this milestone: the
  change touches no Rust file, so the Rust gates are unaffected. Strict Clippy
  with `-D warnings` remains an invariant of this repository even though this
  milestone adds no Rust code, and the Rust workspace gates must stay green on
  the surrounding branch.
- No new Python dependency. The validator and both guards are stdlib-only;
  `uv.lock` is unchanged and `uv sync --frozen` remains satisfied. Use
  `uv sync --dev` for local tooling work and `uv sync --frozen` for CI parity.
- The two guard files are untracked in the working tree
  (`git ls-files tests/tooling/test_release_workflow_versions.py` and
  `git ls-files tests/tooling/test_artifact_hygiene.py` both return nothing),
  so their pre-corrective bodies cannot be recovered from git. The
  baseline evidence for the findings is the baseline commit's workflow
  contents and the M006 closure record's own citations.
- `git checkout`, `git restore`, `git stash`, `git reset`, and `git clean` are
  forbidden in this repository. Use `cp` to `/tmp` and verify the restore with
  `diff`.
- Preserve unrelated user changes in the working tree. The working tree
  carries a large unrelated change set; touch only the four files this plan
  names.
- Python tooling tests must run serially where they touch shared state, and
  the known timing sensitivity of
  `tests/tooling/test_installer.py::test_quick_installer_harness_passes`
  (recorded in the M006 closure record's §10) can be triggered by running the
  lint gates concurrently with the suite. Run them sequentially.
