# Security Hardening Milestone 005 — CI workflow pinning and least privilege

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-5--ci-workflow-pinning-and-least-privilege`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 9, tooling is never a runtime fallback; the CI graph is a supply-chain
  surface
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` — release
  publication authority is unchanged. This milestone hardens ordinary CI, not
  the release contract.

Primary class: infrastructure

## 1. Objective

Every GitHub Actions reference in `.github/workflows/` is pinned to an
immutable commit and every workflow declares top-level least-privilege
permissions, with a guard that fails on reintroduction.

## 2. Why this milestone is ready

No dependencies. The release validator already contains the SHA-pin rule and
the repository style for workflow checks; the uncovered workflows are a small,
known set; the change is text plus one CI step and one tooling test.

## 3. Current implementation evidence

- `.github/workflows/ci.yml` — `actions/checkout@v4`,
  `dtolnay/rust-toolchain@stable`, `astral-sh/setup-uv@v4`; no top-level
  `permissions:` block.
- `.github/workflows/dependency-audit.yml` — `actions/checkout@v4`,
  `EmbarkStudios/cargo-deny-action@v2.1.1`; has `permissions: contents: read`.
- `.github/workflows/installer-shell-qualification.yml` — checkout and
  setup-uv by tag.
- `.github/workflows/qualification-portability.yml` — checkout, setup-uv,
  rust-toolchain, upload-artifact by tag.
- `.github/workflows/release.yml`,
  `qualification-public-release.yml`, and
  `qualification-release-artifacts.yml` are already SHA-pinned with explicit
  permissions; `scripts/validate_release_workflow.py` enforces that subset.
- `tests/tooling/test_release_supply_chain.py` covers the release contract
  only.

## 4. Invariants that must not regress

- The release workflow set, its job graph, its artifact names, and its
  permissions remain exactly as validated today.
- The CI job still runs the same checks with the same working directory and
  toolchain selection.
- No action gains a write permission it did not have; no job loses a
  permission it needs.
- The guard accepts local reusable workflows (`./.github/...`) and rejects
  tag/branch/docker references without an immutable digest.
- No YAML runtime dependency is added; the repository's text-based validator
  style is preserved.

## 5. Scope

### In scope

- SHA pins for the four uncovered workflows, with a trailing version comment.
- A top-level `permissions: contents: read` for `ci.yml`.
- `scripts/validate_workflow_hygiene.py` and
  `tests/tooling/test_workflow_hygiene.py`.
- One CI invocation of the validator.
- Preserving the existing `dtolnay/rust-toolchain` toolchain selection when
  pinning its action SHA.

### Explicitly out of scope

- Changing runner images, container digests, or the release workflow.
- Adding Dependabot or automated pin refreshes.
- Changing branch protection or repository settings.
- Replacing `scripts/validate_release_workflow.py`.

## 6. Required production changes

- Workflows: add 40-hex `uses:` pins and version comments; add the permissions
  block to `ci.yml`.
- `scripts/validate_workflow_hygiene.py`: read every `.github/workflows/*.yml`
  as text, collect `uses:` references and the top-level `permissions:` block,
  and fail with file and line. Rule: every `uses:` must be `owner/repo@\h{40}`
  or a local `./` path; every workflow must declare top-level permissions; a
  workflow outside the release set must declare exactly `contents: read` and
  no job-level `write`.
- `.github/workflows/ci.yml`: run the validator in the `check` job after
  `uv sync --locked`.
- `tests/tooling/test_workflow_hygiene.py`: fixtures for a valid file, a tag
  pin, a missing permissions block, a write permission outside the release
  set, a local workflow reference, and a digest reference.

## 7. Ordered work packages

### Work package A — Pin the workflows

Intent: remove moving refs from code that runs with repository access.

Required changes: resolve each reference to its commit SHA; keep the existing
action inputs; for `dtolnay/rust-toolchain`, pass `toolchain: stable` (or the
workspace MSRV only under a separate decision) so pinning the action does not
change the installed toolchain.

Acceptance evidence: a repository-wide `uses:` listing shows only 40-hex or
local references; CI passes on the pinned set.

### Work package B — Permissions

Intent: every workflow states its token scope.

Required changes: add `permissions: contents: read` to `ci.yml`; verify the
other three already have it; leave release jobs alone.

Acceptance evidence: the validator reports all workflows compliant; no
workflow gains a write token.

### Work package C — Guard and test

Intent: prevent the next tag pin.

Required changes: implement the validator and its pytest module; wire it into
CI; document the rule in the development/deployment skill or
`docs/releasing.md` if that is where workflow rules live.

Acceptance evidence: the guard fails on a deliberate revert of one pin and one
permissions block; the test suite covers the accept and reject cases; CI runs
the guard.

## 8. Failure, cancellation, restart, contention semantics

The change is static configuration; there is no runtime failure mode. CI
concurrency and timeouts are unchanged. The validator reads files only and is
deterministic.

## 9. Compatibility and migration

No runtime or config change. Pinning changes when an action update reaches CI:
it now requires a reviewed commit. `dtolnay/rust-toolchain` keeps its current
toolchain behavior through the `toolchain` input.

## 10. Required tests

- `tests/tooling/test_workflow_hygiene.py` — valid file; tag pin rejected;
  missing permissions rejected; write outside the release set rejected; local
  path accepted; SHA accepted; failure message names the file and line.
- `uv run python scripts/validate_workflow_hygiene.py` against the repository.
- Existing `test_release_supply_chain.py` and
  `scripts/validate_release_workflow.py` stay green.
- Tooling lint/format/type checks as applicable.

## 11. Required verification commands

```bash
uv run python scripts/validate_workflow_hygiene.py
uv run pytest tests/tooling/test_workflow_hygiene.py -q --tb=short
uv run pytest tests/tooling/test_release_supply_chain.py -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
```

No Rust behavior changes; the full Rust suite is not required for the
implementation but is part of the repository gate if the CI workflow edit
changes the executed steps.

## 12. Documentation updates

- The workflow-hygiene rule where release/CI process is documented
  (`docs/releasing.md` or the development skill's CI section).
- `plans/registry.md` status changes.

## 13. Acceptance criteria

- Every `.github/workflows/*.yml` `uses:` reference is a 40-hex SHA or a local
  path.
- Every workflow has a top-level `permissions:` block; non-release workflows
  declare only `contents: read`.
- The guard fails on a deliberate tag pin and a missing permissions block.
- Release workflow validation output is unchanged.
- CI still runs the same checks.

## 14. Stop conditions

Stop and report if:

- a required action has no publishable commit SHA;
- pinning a toolchain action would change the resolved Rust version without a
  decision;
- the validator would need a YAML parser or a new Python dependency;
- a release job needs a permission the validator cannot distinguish from an
  ordinary workflow.

## 15. Closure evidence required

- The before/after `uses:` inventory.
- Validator output and the deliberate-revert failure.
- The pytest results and the unchanged release-workflow validation.
- A statement that no release job, artifact, or permission changed.

## 16. Handoff notes

Keep the validator text-based like the existing release validator; do not add
PyYAML. Preserve the trailing version comments so a human can audit a pin.
