# Plan 172 — Rust Production Cleanup Closure Pass

Date: 2026-09-11
Status: complete (verified 2026-09-11)
Parent roadmap: `plans/168-rust-production-cleanup-roadmap.md`
Priority: P2 closure / documentation consistency
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Close the post-migration Rust-production cleanup line cleanly after implementation of Plans 169–171. This is a bookkeeping and verification pass, not a new engineering phase.

The substantive work has landed:

- strict Clippy is green and enforced in the existing bounded CI job;
- the dependency/feature audit removed only demonstrably unused surface while preserving supported proxy/TLS/SQLite behavior;
- durable release/compatibility tooling was moved to neutral names and paths;
- the active `migration-rs/` scaffold was retired;
- final head `c96ab4a512de1f622edf9a909ac8418a36570127` passed hosted CI in run `34652706163` after the release-workflow indentation correction.

The remaining issue is that the planning metadata does not fully reflect that completed state: Plan 169 and parent Plan 168 still read `ready for handoff`, while Plans 170 and 171 are already marked complete/completed. This pass reconciles that record, verifies no stale current-state wording remains, and explicitly closes the cleanup roadmap.

## Governing constraints

1. Do not change runtime behavior, provider routing, retry/finalization, wire contracts, database semantics, updater behavior, package formats, or release destinations.
2. Do not reopen the Python-to-Rust migration or create a new migration milestone/registry.
3. Do not perform further dependency removal unless a concrete regression from Plan 170 is discovered during verification.
4. Do not introduce new CI workflows, coverage/fuzz/benchmark infrastructure, or additional release gates.
5. Do not rewrite retained Python release tooling in Rust.
6. Git history remains the migration archive; do not restore or recreate `migration-rs/`.
7. Keep this pass small and reviewable. Any newly discovered functional defect must be documented separately rather than folded into closure bookkeeping.

## Workstream A — Reconcile plan status metadata

Update `plans/169-rust-clippy-baseline-and-ci-gate.md`:

- change status from `ready for handoff` to a completed/verified state consistent with Plan 170;
- append concise closure evidence identifying the implementation commit `ec7e19968a07e0064a76a302db756a9acdd890ad`;
- record that strict Clippy now runs in `.github/workflows/ci.yml` with `-D warnings` across all targets;
- record the final hosted-CI confirmation from head `c96ab4a512de1f622edf9a909ac8418a36570127` / run `34652706163`;
- do not duplicate large diffs or exhaustive lint inventories.

Update `plans/168-rust-production-cleanup-roadmap.md`:

- mark the roadmap complete/closed;
- replace statements that describe the pre-cleanup Clippy baseline, dependency graph, and migration scaffold as current problems with a short completion section or clearly historical wording;
- identify the implementation sequence:
  - Plan 169 / `ec7e19968a07e0064a76a302db756a9acdd890ad`;
  - Plan 170 / `34c635e28c40fa2c9f9597f2cc59902606594961`;
  - Plan 171 / `acbb495dcde4723cf04ed7ddb21c0d4f1411f4ba`;
  - release-workflow syntax correction / `c96ab4a512de1f622edf9a909ac8418a36570127`;
- state explicitly that this line of work is ordinary maintenance and is closed, with no M13 or replacement cleanup framework required.

Do not rewrite Plans 168/169 wholesale. Preserve their original implementation rationale as historical planning context and add only the minimum completion metadata/evidence needed to make their current status truthful.

## Workstream B — Verify completed-plan consistency

Review Plans 168–171 together and normalize only material inconsistencies:

- status terminology should make it unambiguous that all child plans are complete;
- the parent roadmap should not imply any child remains dependency-ready;
- references to `migration-rs/` in completed planning documents may remain as historical context, but no text should instruct current contributors to use it;
- Plan 170's closure evidence and Plan 171's completed status should remain intact;
- do not mass-edit unrelated historical plans.

The desired outcome is a coherent historical chain, not stylistic uniformity across the entire `plans/` directory.

## Workstream C — Current-tree closure audit

Perform a bounded audit to ensure the implementation state still matches the completion record.

At minimum verify:

```bash
rg -n "migration-tooling-only|validate_m12|check_cutover_catalog|build_cutover_artifacts|qualification_cutover_rehearsal" \
  --glob '!CHANGELOG.md' \
  --glob '!plans/**' \
  .
```

Expected result: no active runtime, workflow, release, packaging, installer, test, contributor-guide, or current documentation references to retired migration tooling names.

Also verify the root tree does not contain `migration-rs/` and that current neutral authorities remain present, including representative paths:

- `scripts/check_release_catalog.py`;
- `scripts/build_release_artifacts.py`;
- `scripts/qualification_release_rehearsal.py`;
- current neutral release/package validators;
- `tests/tooling/fixtures/qualification/` where retained fixtures now live;
- root `pyproject.toml` with `project_role = "repository-tooling-only"`.

Historical references in completed plans or changelog are acceptable and should not be scrubbed solely for keyword cleanliness.

## Workstream D — Reconfirm bounded production verification

Run the established production/tooling checks without adding new gates:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Run the retained neutral release/package validators that replaced the former cutover/M12 names. Validate `.github/workflows/release.yml` structurally because it was touched during Plan 171 and required the follow-up indentation fix.

Do not run public publication, create a release, or mutate package indexes as part of this closure pass.

## Workstream E — Record final closure evidence

After verification passes, append a concise closure section to this plan containing:

- final commit SHA for the closure-pass implementation;
- local verification summary;
- hosted CI run URL/ID for the resulting head when available;
- confirmation that Plans 168–171 all read as complete;
- confirmation that no functional change was required;
- any residual low-severity maintenance note that remains outside this line of work.

Do not create another follow-up plan unless verification discovers a real defect. Documentation phrasing preferences, historical-plan keyword hits, or negligible dependency-size opportunities are not sufficient reasons to continue this roadmap.

## Acceptance criteria

- Plan 169 is marked complete and contains concise implementation/verification evidence.
- Plan 168 is marked complete/closed and accurately summarizes Plans 169–171 as finished.
- Plans 168–171 no longer disagree about whether the Rust-production cleanup is still pending.
- No active repository surface depends on retired migration-specific tooling/path names.
- `migration-rs/` remains absent from the active tree.
- Root Python metadata remains neutral tooling-only metadata.
- Strict Clippy, Rust tests, release build, Ruff, Pyright, tooling tests, and retained release validators pass.
- `.github/workflows/release.yml` remains structurally valid after the prior indentation correction.
- Hosted CI is green on the closure head when the changed paths trigger CI.
- No runtime, packaging, provider, database, updater, or release behavior changes are introduced.
- The roadmap is explicitly closed with no M13/new migration framework/new cleanup program created.

## Handoff note

Treat this as the final administrative closure of Plans 168–171. Keep the patch focused on status/evidence consistency and verification. If all checks are green, close the line of work rather than searching for additional cleanup merely to justify another pass.

## Closure evidence

The closure pass required no functional change. Plans 168–171 now read as
complete, `migration-rs/` remains absent, retired migration-specific tooling
names have no active-tree references, and the neutral release/package
authorities remain present.

Local verification passed:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_identity.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

The closure implementation was committed as
`341c31b726dcac019a2fcac0a571d430f2b32d21` and pushed to `main`. Hosted CI
passed for that head in [run 34657132426](https://github.com/eggstack/eggpool/actions/runs/34657132426).
No residual maintenance item remains within this cleanup line.
