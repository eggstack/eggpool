# Deployment and Packaging Milestone 006 — Release artifact version derivation and repository artifact hygiene

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for release identity
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. The release artifact contract, the published version, and the
  installed-owner detection are unchanged.
- Stop for architecture review if the correction would change the release
  identity rules or the publication gate.

Primary class: polish

## 1. Objective

Derive the release artifact version in the qualification workflows from the
repository's version authority instead of a literal, run the existing release
workflow validator in ordinary CI, and reconcile the repository's ignored
paths with the artifact and fixture files it actually tracks.

## 2. Why this milestone is ready

No dependencies. Every defect is a literal-versus-derived mismatch or an
ignore-rule-versus-tracked-state mismatch, with no production runtime
component.

## 3. Current implementation evidence

**Two qualification workflows pin a stale version.**
`rust/Cargo.toml` declares version `0.8.2`.
`.github/workflows/qualification-release-artifacts.yml` hardcodes the string
`0.8.0` in seven places: the wheel glob, two `--version` arguments, the raw
binary glob, the chmod find expression, and the `pip install` assertion.
`.github/workflows/qualification-public-release.yml` hardcodes it in two
places: a workflow-dispatch input default and a filename assertion. Both
workflows are dispatch-only, so ordinary CI never exercises them, and running
either today fails because the build produces `0.8.2` artifacts that the
`0.8.0` globs do not match.

**The production release workflow already does this correctly.**
`.github/workflows/release.yml` derives the version from the repository
through `scripts/validate_release_identity.py`, exposes it as a workflow
output, and uses that output in every glob and validator argument. The two
qualification workflows are the outliers.

**The release workflow validator runs only inside the release workflow.**
`scripts/validate_release_workflow.py` is a repository validator for the
release workflow file. It is invoked from `.github/workflows/release.yml`,
which runs on a tag push or a manual dispatch. `.github/workflows/ci.yml`,
which runs on every push and pull request, does not invoke it. A validator
that only runs when the thing it validates is about to publish cannot catch a
drift in an ordinary review.

**The ignore rule and the tracked state disagree.**
`.gitignore` contains the root-anchored rule `/artifacts/qualification/`.
The repository nevertheless tracks 148 files under that path, across seven
milestone directories, totalling about 4.2 megabytes. A gitignore rule has
no effect on an already-tracked path, so the rule is inert while the tracked
files remain.

**A tooling default points at untracked files.**
`scripts/dashboard_parity/_shared.py` defaults its JSON and Markdown inputs to
`migration-rs/closure/qualification/012-run.json` and
`012-run.md`. Both live under the untracked `migration-rs/closure/` directory
and are not in the repository. A clean clone therefore cannot run the
dashboard parity tooling with its default inputs.

## 4. Invariants that must not regress

- The release artifact naming, manifest, and publication contract are
  unchanged.
- The release identity rules, including the tag and source-commit checks, are
  unchanged.
- The `release.yml` version derivation path is not modified.
- The installed-owner detection and the one-shot installer are unchanged.
- The qualification workflows continue to assert that a release artifact
  carries the expected version.
- The qualification artifact set that is deliberately retained as closure
  evidence remains available.
- The dashboard parity tooling continues to run against its recorded
  baselines.
- No secret, credential, or operator-identifying material is added to a
  tracked artifact.

## 5. Scope

### In scope

- The hardcoded version literals in the two qualification workflows.
- The CI invocation of the release workflow validator, and its coverage of
  the qualification workflows.
- The relationship between the `/artifacts/qualification/` ignore rule and
  the tracked files under that path.
- The dashboard parity tooling's default input paths.
- Regression tests and guards for each of the above.

### Explicitly out of scope

- Any change to the release workflow's version derivation, publication gate,
  or action pins.
- Any change to the release artifact format, the manifest schema, or the
  packaging layout.
- Any change to the installer's behavior.
- Any change to the dashboard parity harness's assertions or its recorded
  baselines.
- Any deletion of tracked qualification artifacts.
- Any change to the release version itself.

## 6. Required production changes

**Version derivation.** The two qualification workflows must derive the
version the same way `release.yml` does, from the repository's version
authority, and must use that derived value in every glob, every validator
argument, and every assertion. No version literal may remain in those
workflows.

**Validator coverage.** `scripts/validate_release_workflow.py` must be
invoked from `.github/workflows/ci.yml` so the release workflow contract is
checked on every push and pull request, not only at publication time. The
validator must also cover the two qualification workflows, or a second
validator must cover them, so that a version literal reintroduced into any
release-related workflow fails in ordinary CI.

**Ignore rule and tracked state.** The repository must state one truth about
`artifacts/qualification/`. Either the qualification artifacts are
deliberately retained as closure evidence, in which case the ignore rule is
removed or narrowed and the retention is documented, or they are not, in
which case the tracked files are untracked. The milestone must not leave an
inert rule in place. Because the tracked artifacts are referenced by closure
records as evidence, removal is the option that requires more care and more
evidence.

**Parity tooling defaults.** The dashboard parity tooling's default inputs
must exist in a clean clone. Either the fixture files become tracked, or the
tooling's defaults point at tracked files.

## 7. Ordered work packages

### Work package A — Derive the qualification workflow versions

Intent:

Remove the stale version literals from the two qualification workflows.

Required changes:

- Derive the version from the repository authority in both workflows.
- Replace every literal use, including the dispatch input default and the
  filename assertion.

Acceptance evidence:

- A test asserts no release-related workflow contains a hardcoded EggPool
  version literal.
- A local dry run of the derivation produces the version declared in
  `rust/Cargo.toml`.

### Work package B — Run the release workflow validator in CI

Intent:

Check the release workflow contract on every change.

Required changes:

- Invoke `scripts/validate_release_workflow.py` from `.github/workflows/ci.yml`.
- Extend the validator's coverage to the qualification workflows, or add the
  equivalent check.

Acceptance evidence:

- The validator fails when a version literal is reintroduced into any covered
  workflow.
- The CI job passes on the current workflows.

### Work package C — Reconcile the qualification artifact ignore rule

Intent:

Make the ignore rule and the tracked state agree.

Required changes:

- Choose one truth for `artifacts/qualification/` and make the repository
  reflect it.
- If artifacts are retained, document the retention and narrow or remove the
  ignore rule.
- If artifacts are not retained, untrack them and confirm the closure records
  that reference them still have a valid source.

Acceptance evidence:

- No path in the repository is simultaneously ignored and tracked.
- The closure records that reference the retained artifacts still resolve.

### Work package D — Track the parity tooling defaults

Intent:

Make the dashboard parity tooling runnable from a clean clone.

Required changes:

- Ensure the default input files exist in the repository, or change the
  defaults to tracked files.

Acceptance evidence:

- A clean-clone check confirms the default inputs resolve.
- The parity tooling still produces the same result as the recorded baseline.

## 8. Failure, cancellation, restart, contention semantics

- The version derivation must fail closed: a mismatch between the derived
  version and the built artifact must fail the workflow, not be warned about.
- The validator must not accept a workflow that omits the version gate.
- No runtime, request-path, or process-lifecycle behavior is affected.

## 9. Compatibility and migration

- No migration. No storage change.
- No configuration key is added, removed, or reclassified.
- No public HTTP or CLI contract change.
- The published release artifact format is unchanged. Only the way the
  workflows learn the version changes.
- The dispatch input default for the public-release workflow changes from a
  stale literal to a derived default, which is a workflow-interface
  improvement rather than a break.

## 10. Required tests

- `tests/tooling/` — a validator test asserting that a version literal
  reintroduced into a covered workflow fails validation.
- `tests/tooling/` — a repository-state test asserting that no path is both
  ignored and tracked.
- `tests/tooling/` — a test asserting the parity tooling's default inputs
  exist in the repository.
- The existing `tests/tooling/test_installer.py` and release tooling tests
  continue to pass.
- Python tooling tests run under `uv run pytest tests/tooling/`.

## 11. Required verification commands

```bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
```

This milestone changes `.github/workflows/`, which is not a documentation-only
path, so the ordinary CI job runs for it. The Rust gates below must pass
unchanged even though this milestone adds no Rust code.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `docs/releasing.md` — record the derived-version rule and that the
  qualification workflows use it.
- `architecture/deep-dive-deployment.md` — record the release workflow
  validation coverage.
- `plans/subsystems/deployment-packaging-roadmap.md` — updated by this
  milestone's closure, not by this plan.

## 13. Acceptance criteria

- No release-related workflow contains a hardcoded EggPool version literal.
- The release workflow validator runs in ordinary CI and covers the
  qualification workflows.
- A reintroduced version literal fails validation.
- No path in the repository is simultaneously ignored and tracked.
- The dashboard parity tooling's default inputs exist in a clean clone.
- The release artifact format, the publication gate, and the installed-owner
  detection are unchanged.
- The Python tooling suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- Deriving the version would require changing the release identity rules or
  the publication gate.
- Untracking the qualification artifacts would orphan a closure record that
  has no other source; escalate instead of deleting evidence.
- The validator extension would require a new dependency.
- Evidence contradicts a deployment invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the qualification workflows failed against the current
  version before the correction, so the new coverage is a genuine regression
  guard.
- A statement of which truth was chosen for the qualification artifact path
  and why.

## 16. Handoff notes

Work package C carries the most judgment. The qualification artifacts are
referenced by closure records as evidence, so untracking them is not
mechanical. Read the closure records before deciding, and prefer retention
with a corrected ignore rule if the records genuinely need the files.

Work packages A and B are mechanical and safe. Do them first.

Python tooling work uses `uv sync --dev` locally and `uv sync --frozen` for
CI parity. Strict Clippy with `-D warnings` remains an invariant of this
repository even though this milestone does not touch Rust code. Preserve
unrelated user changes in the working tree.
