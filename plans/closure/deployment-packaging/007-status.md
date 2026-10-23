# Deployment and Packaging Milestone 007 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/deployment-packaging/007-release-action-pin-enforcement-and-guard-correction.md`

Original plan corrected by this pass:

- `plans/implementation/deployment-packaging/006-release-artifact-version-derivation-and-artifact-hygiene.md`

Original closure record superseded in part by this pass:

- `plans/closure/deployment-packaging/006-status.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#12-milestone-status`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The M006 implementation, the M007 corrective implementation,
  and both closure records are in the working tree on `main` at the baseline
  above; no commit was created by this milestone.

## 1. Executive finding

M007 is complete and **closed**.

**M006's two design decisions were correct and are unchanged.** The
version-derivation design — both qualification workflows take the release
version from the repository's own version authority
(`scripts/validate_release_identity.py`) and consume the derived job output in
every glob, validator argument, and assertion — is right and stays. The
artifact-retention decision — the qualification artifacts stay tracked, the
blanket ignore rule was narrowed to `/artifacts/qualification/*` plus explicit
re-inclusions, and no tracked artifact was untracked or deleted — is also right
and stays. This corrective pass changed neither. The release catalog, the
publication gate, the release artifact format, and the manifest schema are
untouched.

**What was wrong was M006's evidence and guard coverage, in three specific
ways.**

1. **The qualification workflows were never held to the immutable-SHA
   action-pin rule.** `scripts/validate_release_workflow.py` defines
   `_validate_action_pins(text)`, which rejects any `uses:` reference that is
   not pinned to a 40-hex-character SHA. It was called from
   `validate_workflow_text` — the contract used for
   `.github/workflows/release.yml` — but **not** from
   `validate_qualification_workflow_text`, the contract used for **both**
   `.github/workflows/qualification-public-release.yml` and
   `.github/workflows/qualification-release-artifacts.yml`.
   `.github/workflows/qualification-release-artifacts.yml` carried 11 `uses:`
   lines, all tag-style (`actions/checkout@v4`, `astral-sh/setup-uv@v4`,
   `dtolnay/rust-toolchain@stable`, `mlugg/setup-zig@v1`,
   `actions/upload-artifact@v4`, `actions/download-artifact@v4`), and the
   validator still passed on it. The M006 closure record's §8 statement "The
   workflows' action pins are unchanged, as the plan requires" was false — M006
   *added* `uses:` references — and its §10 statement "the two qualification
   workflows still use tag-style action pins" was false for one of the two,
   because `.github/workflows/qualification-public-release.yml` was already
   SHA-pinned at baseline `e6822d8f`.
2. **The cited regression test asserted nothing about the repository.**
   `tests/tooling/test_release_workflow_versions.py::test_stale_version_globs_do_not_match_the_current_artifacts`
   read no workflow file and touched no build artifact; it compared hardcoded
   glob patterns against hardcoded filename strings and passed
   unconditionally. It had two globs and four synthetic names, and was cited in
   M006's §2 as the evidence for a claim about 4 globs and 6 artifacts.
3. **The artifact-hygiene guard did not cover the disclosure it made.**
   `tests/tooling/test_artifact_hygiene.py` asserted only that a hardcoded list
   of **flat** generated-output paths under `artifacts/qualification/` were
   git-ignored. Because M006 replaced the blanket ignore rule with
   `/artifacts/qualification/*` plus per-path re-inclusions, generated run
   output written **inside** a re-included evidence tree is not ignored, while
   the same output at the top level still is.

This corrective pass closes all three: every `uses:` reference in every covered
workflow is now an immutable SHA, the qualification contract now enforces that
rule, the glob guard now reads the repository and can fail, and the
artifact-hygiene guard now derives the retained-evidence list from `.gitignore`
and asserts the intended state on both sides of the tree boundary.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Finding 1 — every `uses:` in the artifacts workflow is pinned to an immutable SHA | `tests/tooling/test_release_workflow_versions.py::test_every_covered_workflow_passes_its_own_contract` | pass | All 11 references now match the corresponding references in `.github/workflows/release.yml` |
| Finding 1 — the qualification contract enforces the immutable-SHA rule | `tests/tooling/test_release_workflow_versions.py::test_validator_rejects_a_tag_style_action_pin[.github/workflows/qualification-public-release.yml]`, `::test_validator_rejects_a_tag_style_action_pin[.github/workflows/qualification-release-artifacts.yml]` | pass | `validate_qualification_workflow_text` calls `_validate_action_pins(text)`; the guard fails with `DID NOT RAISE` when that call is removed (§4) |
| Finding 1 — the validator CLI is unchanged in both forms | `uv run python scripts/validate_release_workflow.py` — exit 0; `uv run python scripts/validate_release_workflow.py .github/workflows/release.yml` — exit 0; the same for each qualification workflow — exit 0 | pass | No-argument and single-path invocations both preserved |
| Finding 2 — the glob guard reads the repository and can fail | `tests/tooling/test_release_workflow_versions.py::test_stale_version_globs_do_not_match_the_current_artifacts` | pass | Reads the 3 covered workflows, the release catalog, and `rust/Cargo.toml`; builds the 6-artifact set from the catalog's `supported_wheels` and `scripts.inspect_release_raw.raw_filename`; the derived glob selects all 6, every one of the 58 superseded catalogued versions selects 0; it fails when a stale glob is reintroduced (§4) |
| Finding 3 — the artifact-hygiene guard covers the disclosure it makes | `tests/tooling/test_artifact_hygiene.py::test_generated_output_inside_evidence_directories_stays_untracked`, `::test_the_retained_corpus_is_deliberately_unignored_and_tracked` | pass | The retained-evidence list is derived from `.gitignore`; the `ordinary-server` corpus is asserted to be deliberately un-ignored and tracked |
| Finding 3 — the tracked-and-ignored overlap stays zero | `tests/tooling/test_artifact_hygiene.py::test_no_tracked_path_is_simultaneously_ignored_and_tracked` | pass | `git ls-files -z \| git check-ignore --no-index -z --stdin` returns 0 paths |
| The derived-version rule still holds for all three covered workflows | `tests/tooling/test_release_workflow_versions.py::test_no_covered_workflow_hardcodes_an_eggpool_release_version`, `::test_qualification_workflows_derive_the_version_from_the_authority`, `::test_derived_version_matches_the_cargo_version` | pass | Cargo version `0.8.2`; 59 catalogued versions; 58 superseded |
| The tracked artifact corpus is unchanged in count | `git ls-files artifacts/ \| wc -l` — 153; `git ls-files artifacts/qualification/ \| wc -l` — 148 | pass | 5 further tracked paths sit directly under `artifacts/` |
| Targeted guards pass | `uv run pytest tests/tooling/test_release_workflow_versions.py tests/tooling/test_artifact_hygiene.py -q --tb=short` — 24 passed (17 + 7) | pass | — |
| Full tooling suite passes | `uv run pytest tests/tooling/ -q --tb=short` — 244 passed, 1 skipped, 0 failed | pass | — |
| Lint gates | `uv run ruff format --check scripts/ tests/tooling/` — 71 files already formatted; `uv run ruff check scripts/ tests/tooling/` — All checks passed!; `uv run pyright scripts/` — 0 errors, 0 warnings | pass | — |
| Release-doc validator passes | `uv run python scripts/validate_release_docs.py` — `status: pass`, version `0.8.2`, 7 docs checked | pass | — |
| Zero production runtime change | The complete M007 change set is four files (§3); no Rust, `Cargo.toml`, or manifest-schema file touched | pass | — |
| Rust workspace gates | Not run for this milestone | not run | M007 changes no Rust file. The surrounding branch's Rust gates are verified separately in the enclosing milestone batch (§10) |

## 3. Production implementation evidence

No production runtime change. The complete M007 change set is four files — one
workflow, one validator, and two guard test files:

| File | Change |
|---|---|
| `.github/workflows/qualification-release-artifacts.yml` | All 11 `uses:` lines converted to the repository's canonical immutable SHAs — the same revisions `.github/workflows/release.yml` already uses: `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5`, `astral-sh/setup-uv@3259c6206f993105e3a61b142c2d97bf4b9ef83d`, `dtolnay/rust-toolchain@d1031067263f94b142dd6c0ce24c5eb9d02d52a0`, `mlugg/setup-zig@53fc45b17fe98b52f92ee5ea08ff48a85a3e7eb7`, `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02`, `actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093`. Every `with:` block, every `if:` condition, and the step ordering is otherwise byte-identical; `toolchain: stable` was added to the `dtolnay/rust-toolchain` step so the pinned action resolves the toolchain the `@stable` tag did |
| `scripts/validate_release_workflow.py` | `validate_qualification_workflow_text` now calls `_validate_action_pins(text)`, so both qualification workflows are held to the immutable-SHA rule. The `release` contract, the CLI interface (single-path and no-argument), and every other function are unchanged. Stdlib only — no new dependency |
| `tests/tooling/test_release_workflow_versions.py` | `test_stale_version_globs_do_not_match_the_current_artifacts` rewritten to read the three covered workflows, the release catalog, and the Cargo version, to build the real artifact set from the catalog's published wheels and `scripts.inspect_release_raw.raw_filename`, and to assert that no covered workflow carries a glob built from any superseded catalogued version, that every build job selects its wheel with the derived version, and that the derived glob selects all six artifacts while every stale glob selects none. New `test_validator_rejects_a_tag_style_action_pin` reintroduces a tag-style pin into each covered qualification workflow and asserts the validator rejects it |
| `tests/tooling/test_artifact_hygiene.py` | The retained-evidence list is now derived from `.gitignore` rather than hardcoded; the guard asserts that generated-output shapes inside the retained evidence trees stay untracked; and the `ordinary-server` corpus is asserted to be deliberately un-ignored and tracked. The guard now asserts the intended state: retention inside an evidence tree is an explicit operator decision, while flat generated output stays ignored |

**No other production file changed by this milestone.** In particular,
`.github/workflows/release.yml`, `.github/workflows/ci.yml`,
`.github/workflows/qualification-public-release.yml`, `.gitignore`,
`rust/assets/catalog/installable-releases.json`, `rust/Cargo.toml`, and
`docs/releasing.md` are unchanged by this milestone.

### The measurements the guards now assert

| Fact | Measured value |
|---|---|
| Cargo version | `0.8.2` (`rust/Cargo.toml`) |
| Catalogued release versions | 59 (`rust/assets/catalog/installable-releases.json`) |
| Superseded catalogued versions | 58 (`0.1.0` … `0.8.1`) |
| Artifact set the build produces | 6 — 3 published wheels from the catalog's `supported_wheels` plus 3 raw executable names from `scripts.inspect_release_raw.raw_filename` |
| Derived glob `eggpool-0.8.2-*.whl` | selects 3 of 6 |
| Derived glob `eggpool-0.8.2-linux-*` | selects 2 of 6 |
| Derived glob `eggpool-0.8.2-macos-*` | selects 1 of 6 |
| Every one of the 58 superseded globs, per selector | selects 0 of 6 |
| Tracked paths under `artifacts/` | 153, of which 148 under `artifacts/qualification/` |
| Tracked-and-ignored overlap | 0 |
| Retained evidence trees re-included by `.gitignore` | 7 (`m003`, `m004`, `m007-pi5-2026-10-03`, `m008-pi5-2026-10-03`, `on-prem-concurrency-performance`, `on-prem-user-access`, `routing-affinity-m003-pi5-2026-10-03`), plus 11 retained loose evidence files |
| Tracked files in the `ordinary-server` corpus | 38 |

## 4. Verification executed

### Commands run

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
git ls-files artifacts/qualification/ | wc -l
git ls-files -z | git check-ignore --no-index -z --stdin | tr '\0' '\n' | grep -c .
```

Perturbation and restore procedure (no `git checkout`, `git restore`,
`git stash`, `git reset`, or `git clean` was used — all restores were `cp` from
a `/tmp` backup, verified with `diff -u`):

```bash
cp -p <file> /tmp/m007-<name>-backup
# perturb the file
cp -p /tmp/m007-<name>-backup <file>
diff -u /tmp/m007-<name>-backup <file>   # must print nothing
```

### Results

- `validate_release_workflow.py` with no arguments — **exit 0**, 3 workflows
  validated. `release.yml` reports `contract: release`, 52 actions, 13 jobs;
  each qualification workflow reports `contract: qualification`,
  `derived_versions: 59`.
- `validate_release_workflow.py .github/workflows/release.yml` — **exit 0**.
- `validate_release_workflow.py .github/workflows/qualification-release-artifacts.yml`
  — **exit 0**.
- `validate_release_workflow.py .github/workflows/qualification-public-release.yml`
  — **exit 0**.
- `pytest tests/tooling/test_release_workflow_versions.py tests/tooling/test_artifact_hygiene.py -q --tb=short`
  — **24 passed** (17 in the release-workflow guard file, 7 in the artifact-hygiene
  guard file), 0 failed.
- `pytest tests/tooling/ -q --tb=short` — **244 passed, 1 skipped**, 0 failed.
  Run twice: **101.40 s** before this closure record was written, and
  **100.42 s** after it was written, so the new record is inside the suite's
  own coverage (the artifact-hygiene resolver guard scans every `*.md` under
  `plans/closure/`).
- `ruff format --check scripts/ tests/tooling/` — **exit 0**,
  "71 files already formatted".
- `ruff check scripts/ tests/tooling/` — **exit 0**, "All checks passed!".
- `pyright scripts/` — **exit 0**, "0 errors, 0 warnings, 0 informations".
- `scripts/validate_release_docs.py` — **exit 0**,
  `{"docs_checked": 7, "production_release": "published 0.8.2", "status": "pass", "targets": ["linux-aarch64", "linux-x86_64", "macos-arm64"], "version": "0.8.2"}`.
- `git ls-files artifacts/ | wc -l` — **153**.
- `git ls-files artifacts/qualification/ | wc -l` — **148**.
- `git ls-files -z | git check-ignore --no-index -z --stdin | tr '\0' '\n' | grep -c .`
  — **0** (the tracked-and-ignored overlap is empty; `grep` exits 1 because it
  matched nothing, which is the desired result).

### Deliberate-failure demonstrations

Each rewritten or added guard was shown **failing** on a deliberate
perturbation and then restored, with the restore verified by `diff -u` printing
nothing.

| # | Perturbation | Guard / command | Observed failure output | Restored |
|---|---|---|---|---|
| 1 | First `actions/checkout` pin in `.github/workflows/qualification-release-artifacts.yml` reverted to `actions/checkout@v4` | `uv run python scripts/validate_release_workflow.py .github/workflows/qualification-release-artifacts.yml` | `release workflow workflow validation failed: action is not pinned to an immutable SHA: actions/checkout@v4` — **exit 1** | `diff -u` clean |
| 2 | Same perturbation | `uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=short` | **2 failed, 15 passed** — `test_every_covered_workflow_passes_its_own_contract`: `WorkflowValidationError: action is not pinned to an immutable SHA: actions/checkout@v4`; `test_validator_rejects_a_workflow_without_the_version_gate[…qualification-release-artifacts.yml]`: `AssertionError: Regex pattern did not match. Regex: 'version gate is missing' Input: 'action is not pinned to an immutable SHA: actions/checkout@v4'` | `diff -u` clean |
| 3 | `_validate_action_pins(text)` removed from `validate_qualification_workflow_text` in `scripts/validate_release_workflow.py` | `uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=line -k "tag_style_action_pin"` | **2 failed, 15 deselected** — both `test_validator_rejects_a_tag_style_action_pin[…]` fail at `tests/tooling/test_release_workflow_versions.py:249` with `Failed: DID NOT RAISE <class 'scripts.validate_release_workflow.WorkflowValidationError'>` | `diff -u` clean; re-run **17 passed** |
| 4 | Wheel glob reverted to the stale literal `dist/release-artifacts/${{ matrix.target_class }}/eggpool-0.8.0-*.whl` | `uv run pytest tests/tooling/test_release_workflow_versions.py -q --tb=short -k "stale_version_globs"` | **1 failed, 16 deselected** — `test_stale_version_globs_do_not_match_the_current_artifacts` fails at `tests/tooling/test_release_workflow_versions.py:186` with `AssertionError: .github/workflows/qualification-release-artifacts.yml selects artifacts with the stale version 0.8.0` | `diff -u` clean; re-run **17 passed** |
| 5 | Blanket `.gitignore` rule `/artifacts/qualification/` restored in place of `/artifacts/qualification/*` | `uv run pytest tests/tooling/test_artifact_hygiene.py -q --tb=line` | **2 failed, 5 passed** — `test_no_tracked_path_is_simultaneously_ignored_and_tracked`: `AssertionError: assert ['artifacts/q...rk.json', ...] == []`; `test_the_retained_corpus_is_deliberately_unignored_and_tracked`: `AssertionError: assert 'artifacts/qualification/on-prem-concurrency-performance/ordinary-server/disk-contention.json' not in ['artifacts/qualification/on-prem-concurrency-performance/ordinary-server/disk-contention.json']` | `diff -u` clean; re-run **7 passed** |

Demonstrations 3 and 5 are the ones that prove the new guards are load-bearing:
removing the validator's pin enforcement makes the tag-pin guard fail with
`DID NOT RAISE`, and restoring the blanket ignore rule makes the retained-evidence
guards fail. Neither guard can pass vacuously.

### The residual ignore asymmetry, measured directly

`git check-ignore --no-index` was run against a generated-output shape
(`007-run.json`, one of the shape names the guard derives) placed in three
locations. None of the three probe paths is a repository file; the table names
the containing directory, which is a real tracked location:

| Probe: a generated-output shape named `007-run.json` | `git check-ignore --no-index` |
|---|---|
| at the top level of `artifacts/qualification/` | IGNORED |
| inside the retained tree `artifacts/qualification/m004/` | NOT IGNORED |
| inside the retained corpus `artifacts/qualification/on-prem-concurrency-performance/ordinary-server/` | NOT IGNORED |

This is the property behind the medium finding in §10: the same generated-output
shape is ignored at the top level and not ignored inside a re-included tree.

The closure-record resolver guard
(`tests/tooling/test_artifact_hygiene.py::test_closure_records_resolve_the_retained_qualification_artifacts`)
is live and repository-wide: it scans every `*.md` under `plans/closure/`, and
it rejected this record's first draft because it named three non-existent
probe files as artifact references. The citations above were reworded to name
the containing directory instead. The guard is therefore confirmed to cover
records written after M006, not only the M006 corpus.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| No covered workflow hardcodes a catalogued EggPool release version | `test_no_covered_workflow_hardcodes_an_eggpool_release_version` — pass; the validator reports `derived_versions: 59` against a 59-version catalog |
| Every covered workflow derives the version from the repository authority | `test_qualification_workflows_derive_the_version_from_the_authority`, `test_derived_version_matches_the_cargo_version` — pass; `validate_release_identity()` returns `0.8.2`, matching `rust/Cargo.toml` |
| Every `uses:` in a covered workflow is pinned to an immutable SHA | `test_validator_rejects_a_tag_style_action_pin` — pass for both qualification workflows; `validate_workflow_text` already covered `release.yml`; the three no-argument validator runs report `status: pass` for all three workflows |
| The tracked artifact corpus stays tracked and the tracked-and-ignored overlap stays zero | `test_no_tracked_path_is_simultaneously_ignored_and_tracked` — pass; measured 153 tracked paths under `artifacts/`, 148 under `artifacts/qualification/`, overlap 0 |
| The release catalog stays the version authority | `rust/assets/catalog/installable-releases.json` is unchanged by this milestone; `catalog_release_versions()` reads it, and the derived version is `0.8.2` from `rust/Cargo.toml` |
| No credential, prompt, raw body, or cache key enters a tracked artifact | The M007 change set touches no artifact; the two staged parity fixtures (`migration-rs/closure/qualification/012-run.json`, `012-run.md`) contain 0 occurrences of an operator-home path; `test_parity_default_inputs_carry_no_secret_or_operator_path` — pass (§8) |
| The release artifact naming, manifest schema, packaging layout, publication gate, and installed-owner detection are unchanged | None of `rust/Cargo.toml`, `scripts/create_release_manifest.py`, `scripts/validate_release_artifacts.py`, `scripts/install.sh`, or the release catalog was modified |
| The qualification workflows stay dispatch-only and read-only | `validate_qualification_workflow_text` still requires `workflow_dispatch`, `contents: read`, and rejects `contents: write` and repository secrets; the files' trigger and permission blocks were not modified |
| No new Python dependency | The validator and both guards are stdlib-only; `uv.lock` is unchanged and `uv sync --frozen` remains satisfied |

## 6. Failure and recovery review

- **A tag-style action reference in a covered workflow** fails the validator
  with `action is not pinned to an immutable SHA: <reference>` and a non-zero
  exit, failing the CI job. Demonstrated in §4 rows 1 and 2.
- **A tag-style action reference in a covered workflow, with the validator's
  pin enforcement removed**, is accepted. This is the false-ACCEPT direction
  the regression guard exists to catch; demonstrated in §4 row 3.
- **A superseded catalogued version reintroduced into a covered workflow's
  glob** fails `test_stale_version_globs_do_not_match_the_current_artifacts`
  with `selects artifacts with the stale version 0.8.0`; demonstrated in §4
  row 4.
- **A tracked path that becomes simultaneously ignored** fails
  `test_no_tracked_path_is_simultaneously_ignored_and_tracked`; **the retained
  corpus becoming ignored** fails
  `test_the_retained_corpus_is_deliberately_unignored_and_tracked`; both
  demonstrated in §4 row 5.
- The validator is a pure function over workflow text. It reads the workflow
  file it is given and the release catalog it consults for the catalogued
  version set, and does nothing else: no network, no database, no subprocess,
  no cache, no partial write. There is no concurrency, cancellation, restart,
  lease, or stale-generation surface, so there is nothing to recover and no
  race to arbitrate.
- No runtime path, lock, await, task, or storage access was added. There is no
  new recovery surface to review.

## 7. Migration and compatibility review

No migration. No storage change. No database migration. No configuration key
added, removed, or reclassified. No public HTTP, CLI, or Rust API contract
change.

The validator's CLI is preserved in both forms: the single-path invocation
`scripts/validate_release_workflow.py .github/workflows/release.yml` and the
no-argument invocation, which validates every covered workflow. Both were run
and both exit 0.

The release catalog is untouched. It holds 59 catalogued versions, of which 58
are superseded relative to the Cargo version `0.8.2`, and it remains the single
version authority the covered workflows derive from.

**The tracked artifact corpus is unchanged in count.** Measured on the
corrected tree:

| Measurement | Value |
|---|---|
| `git ls-files artifacts/ \| wc -l` | 153 |
| `git ls-files artifacts/qualification/ \| wc -l` | 148 |
| Tracked paths outside `artifacts/qualification/` | 5 (`artifacts/plan-019-evidence.md`, `plan-020-evidence.md`, `plan-021-evidence.md`, `plan-023-baseline.md`, `plan-030-exact-head-evidence.md`) |
| Tracked-and-ignored overlap | 0 |
| Retained evidence trees re-included by `.gitignore` | 7 |
| Retained loose evidence files re-included by `.gitignore` | 11 |
| Tracked files in the `ordinary-server` corpus | 38 |

Per retained tree, the tracked file counts are `m003` 8, `m004` 14,
`m007-pi5-2026-10-03` 9, `m008-pi5-2026-10-03` 11,
`on-prem-concurrency-performance` 48, `on-prem-user-access` 45,
`routing-affinity-m003-pi5-2026-10-03` 2 — 137 in the trees, plus 11 loose
evidence files, plus the 5 files directly under `artifacts/`, for the 153
measured above.

The 148 count under `artifacts/qualification/` matches the M006 closure
record's figure exactly, so the retention decision is preserved byte-for-byte
in count and in membership: nothing was untracked, nothing was deleted, and
nothing was added.

## 8. Security review

- **The pin rule is a supply-chain control.** An immutable-SHA `uses:`
  reference is what makes a third-party action's code auditable and
  non-movable; a tag reference (`actions/checkout@v4`) can be re-pointed at
  different code by the action's publisher at any time. Before this milestone
  the release workflow was held to the immutable-SHA rule while the two
  qualification workflows were not, so the exposure was asymmetric exactly
  where the rule was weakest. The rule is now uniform across all three
  covered workflows, and it is enforced by the validator rather than asserted
  in prose.
- **Secret handling.** No credential, token, key, or password is added to any
  tracked artifact by this milestone. The `validate_qualification_workflow_text`
  contract still rejects any `secrets.<NAME>` reference and any
  `FORBIDDEN_SECRET_MARKERS` literal, and the M007 change set adds no
  `with:` value that could carry a secret.
- **No operator-identifying path in a tracked artifact.** The parity artifact
  `migration-rs/closure/qualification/012-run.json` carries
  `rust_identity: 'rust:rust/target/debug/eggpool'` — a normalized
  container-relative identity, not an operator-home absolute path. Both staged
  parity fixtures (`012-run.json`, `012-run.md`) contain **0** occurrences of
  `/Users/`. This milestone does not touch either file.
- **No credential, prompt, raw body, or cache key** appears in either
  planning document this milestone produced.
- **Privilege bounds.** Unchanged. The qualification workflows remain
  dispatch-only with `contents: read`; no job gained a permission, and
  `contents: write` remains rejected.
- **DoS bounds.** The validator is stdlib-only and scans three bounded
  workflow files plus one bounded catalog; the guards shell out to `git
  ls-files` / `git check-ignore` over the tracked set. The cost is unchanged
  from M006.
- **Audit behavior.** Unchanged. The validator runs on every push and pull
  request through `.github/workflows/ci.yml`, so the extended pin rule is
  checked in ordinary review.

## 9. Documentation and operations

- **`docs/releasing.md` — no change required; the file is accurate as
  written and was not edited.** Its "Qualification artifact retention" section
  (`docs/releasing.md:105`–`123`) already states the corrected state: the
  ignore rule is narrowed to generated run output, the retained evidence
  directories and files are re-included, no path is simultaneously ignored and
  tracked, and `scripts/qualification_ordinary_server.py` is named as the
  documented exception because it writes into the retained `ordinary-server`
  corpus. The "Release checks" block (`docs/releasing.md:8`–`22`) already lists
  all three covered workflows for the validator. The file's owner may
  optionally add a sentence recording that every covered workflow is pinned to
  immutable SHAs; that is a disclosure improvement, not a correction.
- **`architecture/deep-dive-deployment.md` — owned outside this milestone and
  now stale in one respect.** Its "Release workflow validation" section
  (`architecture/deep-dive-deployment.md:124`–`148`) describes pinned actions
  as part of the release workflow's contract only ("the release workflow keeps
  the full publication contract (tag trigger, pinned actions, …)") and still
  says "the action pins … are unchanged". Both statements predate this
  milestone and no longer describe the qualification workflows. The owner must
  update it; this milestone does not.
- **Operator surface.** Unchanged. No CLI command, diagnostic, exit code, or
  configuration key changed. The validator's failure message for a tag-style
  pin is `action is not pinned to an immutable SHA: <reference>`.
- **Recovery instructions.** None needed. Every perturbation in §4 was
  restored with `cp` from a `/tmp` backup and verified with `diff -u`, and the
  tree is byte-identical to its pre-perturbation state.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Generated run output written **inside** a re-included evidence tree is not git-ignored, while the same output at the top level is. Measured with `git check-ignore --no-index` against a generated-output shape named `007-run.json`: IGNORED at the top level of `artifacts/qualification/`, NOT IGNORED inside the retained tree `artifacts/qualification/m004/`, and NOT IGNORED inside the retained corpus `artifacts/qualification/on-prem-concurrency-performance/ordinary-server/` | A qualification run that writes into a retained tree leaves untracked files in the tree, which an operator may stage by accident. Retention inside a tree is currently an explicit operator decision, and `docs/releasing.md` discloses the `ordinary-server` case; `test_generated_output_inside_evidence_directories_stays_untracked` and `test_the_retained_corpus_is_deliberately_unignored_and_tracked` assert the intended state rather than the ignore behaviour | Making the shapes ignored inside the trees needs a `.gitignore` rule per tree (`!<tree>/` followed by `/<tree>/*`) and belongs to a separate change. It is deliberately out of scope here (§5) because it changes the retention contract |
| medium | The original M006 closure record's §8 and §10 action-pin statements were inaccurate as written: §8 said "The workflows' action pins are unchanged, as the plan requires" although the milestone ADDED `uses:` references, and §10 said "the two qualification workflows still use tag-style action pins" although only one of the two used tag pins | The closed record's security review and its unresolved-findings table both misstate the repository state | The original record is left unchanged under the append-only discipline. This corrective closure supersedes those two statements; the parent must add a supersession note to `plans/closure/deployment-packaging/006-status.md` pointing at this record |
| medium | The original M006 closure record cited a tautological test as its glob evidence: `test_stale_version_globs_do_not_match_the_current_artifacts` read no workflow file, touched no build artifact, compared hardcoded globs against hardcoded filenames, and passed unconditionally | The closed record's §2 matrix row claims "the four pre-correction globs match 0 of the 6 artifacts the build produces" on the strength of a test that could not fail | The rewritten test now proves the property against the repository, and the original record's claim is superseded by §2 of this record. The parent must add the supersession note to the M006 closure record |
| low | A stale `tests/__init__.pyc` bytecode file in `tests/` breaks `import tests.tooling.*` for any future tooling test that imports the package rather than the module. The file is a Python 2.7 byte-compiled artifact (`file` reports "python 2.7 byte-compiled"; magic `03 f3 0d 0a`); importing `tests.tooling` raises `ImportError: bad magic number in 'tests': b'\x03\xf3\r\n'`. The file is untracked and git-ignored, so it is a local-worktree artifact only | A future tooling test that writes `from tests.tooling.<module> import …` fails at import time with a confusing error rather than a test failure. No committed tooling test imports the package today — they import modules directly — so the current suite is unaffected | Remove the local bytecode file (`tests/__init__.pyc`) and keep the package importable; the tooling test modules should import helper modules directly, as they do today. A repository-level ignore for `__pycache__`/`*.pyc` would prevent a recurrence |
| low | The serial Rust workspace suite (`cargo fmt --all -- --check`, strict Clippy `-D warnings`, and `cargo test --workspace --all-targets -- --test-threads=1`) was not run for this milestone | None for M007 specifically: no Rust file changed, and the milestone's verification surface is the Python tooling suite | The enclosing milestone batch runs the Rust gates on the surrounding branch. Strict Clippy with `-D warnings` remains an invariant of this repository |
| info | The catalog-scoped version-literal guard can false-positive on a future version collision (for example an EggPool `0.13.0` against the Zig `0.13.0` pin) | When a future release bumps into a version that collides with a third-party pin in a covered workflow, the validator fails loudly and misleadingly | Refine the guard to scan only EggPool-artifact lines when a release bumps into a colliding version. No action needed today — all three workflows pass |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed.

M006's version-derivation design and its artifact-retention decision are
unchanged and correct. The three findings the M006 review left open — the
unqualified action-pin rule, the tautological regression test, and the
artifact-hygiene guard that did not cover its own disclosure — are closed by
this corrective pass, each with a regression guard shown failing on a
deliberate perturbation.

Deployment and packaging M007 was a corrective pass over a closed milestone and
introduces no new interface, so no successor milestone is registered in this
subsystem and no other subsystem's dependency is affected. The subsystem's
remaining obligations are unchanged and tracked separately: the residual
`.gitignore` asymmetry inside retained evidence trees (§10, medium), the
parity harness's `rust_identity` rendering on regeneration (recorded in the
M006 closure record's §10), and the `tests/__init__.pyc` local bytecode
artifact (§10, low).

## 12. Registry updates

The parent owns `plans/registry.md` and
`plans/subsystems/deployment-packaging-roadmap.md` and must apply the
following; this milestone does not edit them.

- `plans/registry.md`, "Active subsystem roadmaps" table — the
  **Deployment and packaging** row: keep the subsystem status `active`, and
  update its status cell to record `M007 closed — release action-pin
  enforcement and guard correction`, retaining the existing baseline note that
  M001–M005 remain closed and that M006 is closed.
- `plans/registry.md`, "Recently closed" table — add a row:
  **Deployment and packaging M007 — release action-pin enforcement and guard
  correction**, status `closed —` with the summary that all 11 `uses:`
  references in `qualification-release-artifacts.yml` are now the canonical
  immutable SHAs, that `validate_qualification_workflow_text` enforces the
  immutable-SHA rule for both qualification workflows, that the glob guard now
  reads the three covered workflows, the release catalog, and the Cargo
  version (6 artifacts, 58 superseded catalogued versions), that the
  artifact-hygiene guard now derives the retained-evidence list from
  `.gitignore` and asserts the intended state on both sides of the tree
  boundary, that each rewritten or added guard was shown failing on a
  deliberate perturbation, and that there was zero production Rust, `Cargo.toml`,
  or manifest-schema change; linked to
  `plans/closure/deployment-packaging/007-status.md`.
- `plans/registry.md`, "Recently closed" table — the existing
  **Deployment and packaging M006** row stays as written (append-only). The
  parent may append a short note that M007 supersedes that record's §8 and §10
  action-pin statements and its §2 glob-evidence row.
- `plans/registry.md`, "Dependency-ready implementation plans" table — no row
  for M007 is required: it is a corrective pass over a closed milestone with
  no successor. The parent should confirm that no milestone elsewhere in the
  registry is unblocked by it.
- `plans/subsystems/deployment-packaging-roadmap.md`, §12 milestone status
  table — add the row
  `| 7 | closed | plans/implementation/deployment-packaging/007-release-action-pin-enforcement-and-guard-correction.md | plans/closure/deployment-packaging/007-status.md | none |`.
- `plans/subsystems/deployment-packaging-roadmap.md` — update the dependency
  graph note that currently reads "M006 closed. No further milestone is
  registered in this subsystem." to record that M007 is also closed, and
  update the roadmap's completion statement so it reflects that the
  release-workflow contract now includes the immutable-SHA action-pin rule for
  all three covered workflows.
- `plans/implementation/deployment-packaging/007-release-action-pin-enforcement-and-guard-correction.md`
  — the `Status` line is already `implemented`; no edit is required.
- `plans/closure/deployment-packaging/006-status.md` — the parent owns this
  edit. Add a supersession note recording that M007 corrected the §8 and §10
  action-pin statements and the §2 glob-evidence row, and linking
  `plans/closure/deployment-packaging/007-status.md`. The original text stays
  unchanged.
