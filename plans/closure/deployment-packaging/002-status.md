# Deployment and Packaging Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-2--installer-transaction-and-collision-corrective`

Repository baseline reviewed: `6639d4e6028e7aeebfc46b0c6743b67dc19774c7`

Implementation commits or pull requests:

- `02ee2873` — Implement installer transaction and collision corrective (fresh `--force` attribution hardening, post-commit fresh rollback with guarded identity, deterministic 46-case qualification, docs/help clarification)

## 1. Executive finding

The invariant corrective is complete. Fresh `--force` no longer replaces an
unowned regular destination file, and first-time config-seeding failure after
executable publication rolls back only the executable committed by that
transaction plus a safely-attributable partial config. The binary-first
authority, owner delegation, target matrix, release publication, and
production deployment ownership are unchanged. Deterministic qualification
grows from 42 to 46 cases with the two primary post-M001 regressions plus
preservation and race guards green, and all M001 regressions remain green.
Disposition is `closed` with no medium-or-higher unresolved finding. M001
closure remains immutable; this record is the authoritative corrective
evidence.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| 1. Unowned regular `$INSTALL_DEST` never replaced by fresh install, including with `--force` | `scripts/install.sh`: fresh preflight and pre-commit revalidation both fail unconditionally when DEST exists (`--force` no longer an exception); `qualify_quick_installer.py`: `fresh-force-unowned-regular-refusal` passes (bytes/mode unchanged, no config, `--force` in diagnostic) | pass | Before: `--force` + regular file → `mv -f` replaced; after: fails with verified-repair guidance |
| 2. `--force` remains functional for verified existing standalone repair without owner change | `force-repair-standalone` passes (delegates to native updater, existing binary retained); `existing-standalone-delegates` passes; no owner change | pass | Existing-owner classifier unchanged; fresh path never handles verified owners |
| 3. Package-managed existing owners remain package-managed under repair/update | `existing-uv-retained`, `existing-pipx-retained`, `existing-pip-retained`, `existing-legacy-uv` pass; post-delegation owner check unchanged | pass | Manager delegation + `EXPECTED_OWNER` revalidation untouched |
| 4. Fresh config-seeding failure exits non-zero and removes executable committed by that invocation when identity unchanged | `fresh-init-config-failure-rolls-back-binary` passes (failing `init-config` candidate writes partial then exits 3; DEST absent afterward, no config remains, lock released) | pass | Before: failure left DEST installed; after: guarded `hash/version` revalidation then `rm -f` |
| 5. Fresh config-seeding failure does not remove destination whose identity changed after commit | `fresh-config-failure-destination-race-refusal` passes (race candidate overwrites DEST with `racing` then fails; DEST still contains `racing`, manual-recovery diagnostic) | pass | Hash mismatch → `need_manual`, no delete; signal/EXIT traps use same guard |
| 6. Pre-existing config never removed or rewritten by rollback | `fresh-init-config-failure-preserves-preexisting-config` passes (pre-created `operator-config` + failing candidate → install succeeds, bytes preserved, DEST present because seed skipped by dispatch); `seed_config_after_commit` and fresh inline seed both check `existed` before any removal; symlink/special boundaries refuse | pass | Failure-with-existing-config impossible by dispatch (seed skipped); documented in harness comment |
| 7. Newly-created partial config removed only when safely attributable | Failing candidate writes `[server partial` then exits 3; `fresh-init-config-failure-rolls-back-binary` asserts no config remains; fresh rollback and `seed_config_after_commit` both require `!existed && -f && ! -L` before `rm -f`, refuse symlink/special with manual guidance | pass | No traversal of symlink/special boundary |
| 8. Install lock/temp released after corrective rollback paths | `fresh-init-config-failure-rolls-back-binary` asserts `install.lock.d` absent; `release_install_lock` + `cleanup_install_temp` called before every `fail_fresh_tx`; EXIT/signal traps release lock/temp idempotently | pass | Lock held through rollback, released before exit |
| 9. All M001 binary-first, no-Python, checksum, exact-version, owner-preservation, historical, collision, moving-latest regressions green | 42 M001 cases all pass unchanged; `release-manifest-raw-contract`, checksum/size/version/target/owner/historical/lock/rollback cases green; full tooling 126 passed 1 skipped | pass | No M001 behavior changed except the two intentional tightenings |
| 10. Roadmap current-state prose reflects landed binary-first implementation and M002 corrective | `plans/subsystems/deployment-packaging-roadmap.md` §4 describes binary-first + stale-pipx regression + M002 gaps; `docs/deployment.md` now states `--force` repairs verified installs never overwrites unrelated files and fresh installs are transactional; installer `--help` clarifies `--force` repair scope | pass | M001 closure immutable; this record is authoritative corrective |
| 11. M001 closure immutable; M002 closure records verification gaps | This file §3 + §5 name why M001 missed each finding; `plans/closure/deployment-packaging/001-status.md` untouched (`git diff` shows no change) | pass | Corrective does not rewrite history |
| 12. No release target, runtime capability, package publication, schema, or production deployment ownership changes | Zero Rust diff; `check_release_catalog`, `validate_release_workflow`, `validate_runtime_package_boundary` pass; target matrix still Linux x86_64/aarch64 + macOS arm64; PyPI/crates.io untouched | pass | Narrow `scripts/install.sh` + qualification + docs only |

## 3. Production implementation evidence

Landed changes (zero Rust diff; installer/tooling/docs only):

- `scripts/install.sh`:
  - Help: `--force` now reads “Repair a verified EggPool installation
    without changing owner; never overwrites an unrelated file.”
  - Fresh transaction state: `FRESH_TX_ACTIVE/COMMITTED/EXPECTED_HASH/
    EXPECTED_VERSION/CONFIG/CONFIG_EXISTED/ROLLBACK_DONE` globals.
  - `fresh_tx_rollback_guarded()`: hash (`SELECTED_SHA256`) + version
    revalidation before `rm -f DEST`; refuses symlink/special/empty-hash;
    removes newly-created partial config only when `!existed && -f &&
    ! -L`; idempotent; returns 0 success / 1 manual-recovery.
  - `fail_fresh_tx()`: guarded rollback then `fail()` with diagnostics
    distinguishing “rolled back” vs “could not be proven safe — manual
    recovery required” (names DEST + config path).
  - `install_trap_cleanup` (EXIT) and `fresh_tx_signal_handler`
    (INT/TERM/HUP) attempt guarded rollback when `ACTIVE && COMMITTED`
    before cleaning temp/releasing lock; never blindly `rm -f DEST`.
  - `install_fresh_raw_binary()`: preflight and pre-commit revalidation now
    fail unconditionally when DEST exists (removed `FORCE_REINSTALL`
    exception and `mv -f` branch); records hash/version/config-existed
    before `mv`; sets `COMMITTED=1`; post-commit provenance/version/path
    checks use `fail_fresh_tx`; config seeding inlined transactionally
    (preserved-existing vs created vs rollback on `init-config` failure);
    disarms transaction on success.
  - `seed_config_after_commit()` (existing-owner paths): explicit
    preserved/created/failed contract; cleans partial only when safely
    attributable (`!existed && -f && ! -L`), refuses symlink/special with
    manual guidance, never removes pre-existing config.
- `scripts/qualify_quick_installer.py` (42 → 46 cases):
  - Helpers `_fake_raw_binary_failing_init` (partial then exit 3) and
    `_fake_raw_binary_race_on_config` (overwrite DEST with `racing` then
    exit 3) plus `_fresh_failing_release`.
  - `fresh-force-unowned-regular-refusal`: regular DEST + `--force` →
    exit 1, bytes/mode unchanged, no config, `--force` in diagnostic.
  - `fresh-init-config-failure-rolls-back-binary`: DEST absent afterward,
    no config/partial, lock released.
  - `fresh-init-config-failure-preserves-preexisting-config`: pre-existing
    config + failing candidate → success, bytes preserved (documents why
    failure-with-existing-config impossible by dispatch).
  - `fresh-config-failure-destination-race-refusal`: DEST still `racing`,
    manual-recovery guidance.
- `tests/tooling/test_installer.py`: asserts 46 cases + four M002 names.
- `docs/deployment.md`: same-owner repair now explicitly “never overwrites
  an unrelated file”; fresh install described as transactional through
  first-time seeding with rollback/manual-recovery semantics.
- `plans/implementation/deployment-packaging/002-*.md`: `ready` → `active`
  in implementation commit; `active` → `closed` in this closure commit.
- `plans/subsystems/deployment-packaging-roadmap.md`: M002 `ready` →
  `active` → `closed` with closure link; §4 prose already described M002
  gaps and now matches landed behavior via this closure.

Before/after:

- Finding A (fresh `--force` collision): before, `install_fresh_raw_binary`
  permitted `[[ -e DEST ]] && FORCE_REINSTALL` then `mv -f`; a regular file
  at DEST with no `eggpool` on PATH was replaced. After, any existing DEST
  (regular or symlink) fails closed with “not a verified EggPool
  installation; --force repairs a verified EggPool installation and cannot
  overwrite an unrelated file.”
- Finding B (config-seed rollback): before, `seed_config_after_commit`
  `fail`ed after commit leaving DEST installed. After, failure triggers
  `fail_fresh_tx` → hash/version revalidation → `rm -f DEST` only on match
  plus safe partial-config removal; race → no delete + manual guidance.

Why M001 verification missed each finding:

- Unowned regular under `--force`: M001 qualification had
  `dest-unrelated-collision` only without `--force`. The test matrix
  asserted refusal for the default path but never combined the collision
  fixture with `--force`, so the closure generalized “collisions fail
  closed” beyond the tested flag state. The production branch explicitly
  allowed `FORCE_REINSTALL` for regular files, which the matrix did not
  exercise.
- Post-commit `init-config` failure: M001 fixtures covered successful
  missing-config creation, preservation of existing config, and pre-commit
  download/hash/self-check failures, but never injected a post-commit
  `init-config` non-zero exit. `seed_config_after_commit` was therefore
  only exercised on its success path, and requirement 12 (“failed …
  post-check leaves no unverified command”) was verified with pre-commit
  failures that never reached the seed step.

Planned but absent: none. No release authority, target matrix, updater,
package publication, schema, or root deployment changes (all out of scope
per plan §5 and respected).

## 4. Verification executed

### Commands run

```bash
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/test_release_docs.py tests/tooling/test_release_catalog.py -q
uv run python scripts/validate_release_docs.py
uv run python scripts/check_release_catalog.py
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
git diff --check
```

Rust: no `rust/` diff in this pass, so no `cargo fmt`/`clippy`/`test`/
`--no-default-features` matrix was required. Stated explicitly as justified
substitute (zero production Rust change; `install-provenance`/`update` seams
used as-is). Hosted CI still executes the normal Rust matrix; its result was
not observed locally at closure time (see below).

### Results

Local (this host):

- `qualify_quick_installer.py`: pass, 46 cases (42 M001 + 4 M002:
  `fresh-force-unowned-regular-refusal`,
  `fresh-init-config-failure-rolls-back-binary`,
  `fresh-init-config-failure-preserves-preexisting-config`,
  `fresh-config-failure-destination-race-refusal`; all other names unchanged).
- Before-fix reproduction (baseline `6639d4e6` code with new fixtures):
  `fresh-force-unowned-regular-refusal` failed (expected 1, got 0 —
  unowned file replaced); `fresh-init-config-failure-rolls-back-binary`
  failed (DEST still present after `init-config` failure). Both fail for
  the expected reason before production changes.
- `test_installer.py`: 1 passed (harness green, 46 asserted + M002 names).
- Release focused: `test_release_docs` + `test_release_catalog`: 12 passed.
- Full tooling: 126 passed, 1 skipped.
- `validate_release_docs.py`: pass (`docs_checked 7`, `published 0.8.1`).
- `check_release_catalog.py`: pass (58 releases; native `0.8.1` published;
  8 rollback-compatible).
- `ruff format --check`: pass (45 files formatted).
- `ruff check`: pass (all checks passed).
- `pyright scripts/`: 0 errors.
- `git diff --check`: clean.
- No concealed partial execution; all commands run to completion locally.

Hosted CI: not observed locally at closure time. This is a tooling/docs
pass with zero Rust diff; local deterministic qualification is the gate per
plan §11. Hosted CI remains truth for release gates and will execute the
normal Rust matrix on push; any CI-only failure requires a follow-up
corrective, not a silent amendment to this record.

Fixture evidence highlights (disposable, no network, no real home):

- Force-refusal: unowned `unrelated` bytes + `0755` preserved; stderr
  contains “not a verified” + “cannot overwrite” + “--force”; no
  `config.toml`, no `manager.log`.
- Rollback: failing candidate hash matches sidecar, provenance/version pass,
  `init-config` exits 3 after partial write; installer exits non-zero with
  “rolled back”; DEST absent; partial config absent; `install.lock.d`
  absent so a later clean install can proceed.
- Preservation: pre-existing `operator-config` + failing candidate →
  exit 0, DEST present, config bytes unchanged (seed skipped by dispatch).
- Race: race candidate overwrites DEST with `racing` then exits 3;
  installer exits non-zero with “could not be proven safe” / “manual
  recovery”; DEST still `racing` (never deleted).
- Legitimate repair: `force-repair-standalone` (verified standalone +
  `--force` → native `update` delegation) still passes; manager-owned
  retentions still pass; symlink/special/collision/stale-pipx/no-Python/
  checksum/version/moving-latest still pass.

## 5. Invariant review

Per source-plan §4, each remains true:

- Fresh current-native curl install remains verified GitHub raw binary
  first: `select_raw_from_sidecar` + `verify_staged_candidate` order
  unchanged; 46-case + release validators pass.
- Fresh native independent of Python/uv/pipx/pip/Cargo/source: fresh-binary
  authorities still ignore manager discovery; `fresh-linux-aarch64-no-python`
  + `stale-pipx-ignored` pass with `manager.log` absent.
- Supported proxy targets Linux x86_64/aarch64 + macOS arm64: `TARGET_CLASS`
  gate + `release-manifest-raw-contract` unchanged.
- Candidate checksum/version/native verification before publication:
  `verify_staged_candidate` (size/hash/chmod/version/provenance) unchanged.
- Moving-latest bound to exact version: pinned asset URL + exact agreement
  unchanged; `staged-wrong-version`/`wrong-target-filename` pass.
- Existing uv/pipx/pip ownership manager-owned: delegation + owner check
  unchanged; retentions pass.
- Existing standalone standalone unless explicit adoption: `existing-
  standalone-delegates` + adoption flag unchanged.
- Source/ambiguous fail closed: `source-checkout-refusal`,
  `ambiguous-refusal` pass; fresh revalidation of `command -v eggpool`
  retained and now unconditional.
- `--force` never changes ownership implicitly: fresh `--force` no longer
  overwrites; existing repair retains owner (fixtures prove).
- Symlink/special/unrelated collisions fail closed: `dest-symlink`,
  `dest-special`, `dest-unrelated`, plus new `fresh-force-unowned` all pass.
- Existing config/database/`.env` untouched: `existing-config-preserved`
  passes; rollback never removes pre-existing config (fixture proves).
- First-time config all-or-nothing with fresh executable: new rollback
  fixtures prove; lock held through rollback.
- Historical Python compatibility explicit/bounded: catalog gate + Python
  precheck unchanged; historical fixtures pass.
- Release workflow/raw names/ABI/PyPI/crates.io unchanged: validators pass.
- Root/system production unchanged: `root-refusal` passes; no `deploy`
  change.

## 6. Failure and recovery review

- Unowned collision (with/without `--force`): fails before download/commit;
  no config/data mutation; bytes/mode preserved.
- Fresh config-seed failure (no race): under install lock, hash/version
  revalidated, DEST removed, partial config removed when safely
  attributable, lock/temp released, non-zero exit with “rolled back”;
  subsequent clean install can proceed (lock absent).
- Fresh config-seed failure (race): hash mismatch → no delete; diagnostic
  reports manual recovery with DEST + config paths; partial config still
  cleaned only when safe; lock/temp released.
- Signal during seeding: INT/TERM/HUP trap attempts same guarded rollback
  (no unguarded `rm -f DEST`); EXIT trap is idempotent via `ROLLBACK_DONE`.
- Existing-owner repair/update: native updater lifecycle via delegation;
  shell standalone repair retains rollback copy + service handling;
  unchanged.
- Concurrency: `install.lock.d` held through seed + rollback; pre-commit
  race revalidation now unconditional; `lock-contention` +
  `target-race-refusal` still pass.
- Malformed/ambiguous sidecar, checksum/size/version/target failures: fail
  before commit (fixtures pass); no partial DEST/config.

## 7. Migration and compatibility review

No runtime/API/config/database migration. Public quick-install command and
binary-first authority unchanged.

Intentional tightening (consistent with help text + ADR-0001, not a promise
to preserve unsafe overwrite):

- Previously fresh `--force` could replace an unowned regular DEST; after
  M002 that state always fails closed and requires explicit operator cleanup.
- Previously fresh `init-config` failure left the executable; after M002 it
  is removed when identity matches, else manual recovery is reported.

Legitimate verified repair paths remain supported (standalone/manager
fixtures prove). No schema, API, wire, or protocol migration; zero Rust diff.

## 8. Security review

- Release HTTPS, checksum-before-execution, size bound, staged self-check,
  atomic same-filesystem commit: unchanged (fixtures prove).
- No credentials read/emitted: installer handles no secrets; closure and
  fixtures secret-free (no prompts, raw bodies, cache keys).
- Unsafe paths fail closed: symlink/special/unowned + race-refusal +
  `same_path` PATH verification retained; rollback never traverses
  symlink/special or deletes raced replacement.
- Bounded output: version/authority/ownership/command/config only; rollback
  diagnostics bounded with explicit paths, no env/credential leak.
- Secret-free: verified via `git diff` (no secret handling added).

## 9. Documentation and operations

Updated:

- `scripts/install.sh --help`: `--force` now “Repair a verified EggPool
  installation without changing owner; never overwrites an unrelated file.”
- `docs/deployment.md`: same-owner repair now explicitly never overwrites
  unrelated files; fresh install described as transactional through
  first-time seeding with rollback/manual-recovery semantics.
- `plans/subsystems/deployment-packaging-roadmap.md`: §4 already described
  binary-first + M002 gaps; milestone table M002 `ready` → `active` →
  `closed` with closure link (this file).
- `plans/registry.md`: M002 `ready` → `active` → `closed`; unblock audit
  below.
- Validators: `validate_release_docs.py` still passes (binary-first guards
  intact).

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Physical SBC (Le Potato or comparable Linux/aarch64) run `not measured` in this pass | Operational confidence only; deterministic host fixtures pass and roadmap states physical evidence is valuable but not required | Future operator may record a fresh-device run without code change; no corrective plan required |
| low | GitHub artifact attestations / immutable-release enforcement not added | Supply-chain hardening deferred per plan §5 out of scope; SHA-256 sidecar remains authority | Separate hardening plan if desired; must not require `gh`/jq/Python in default installer |

No critical, high, or medium findings. No correctness or security gap remains.

## 11. Roadmap disposition

Milestone closed; no successor is eligible from this closure. Deferred
hardening (attestations, system/root distribution, additional targets)
requires separate bounded plans and must not hitchhike on M002.
Provider-transport M002 remains independently blocked on upstream Eggfetch
and is unaffected. Routing-selection M002 stays evidence-gated. Persistence
has no eligible successor. The deployment-packaging roadmap retains both
milestones closed with no ready successor; future hardening needs a new
bounded plan.

## 12. Registry updates

Applied in the same commit as this closure:

- `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md`:
  `active` → `closed`.
- `plans/subsystems/deployment-packaging-roadmap.md`: M002 `active` →
  `closed` with closure link `plans/closure/deployment-packaging/002-status.md`.
- `plans/registry.md`:
  - Active roadmaps: deployment-packaging `M002 active` → `M002 closed
    — installer transaction and collision corrective`.
  - Dependency-ready plans: remove M002 (no ready plans remain).
  - Recently closed: add deployment-packaging M002 (`closed`,
    implementation `02ee2873`, closure
    `plans/closure/deployment-packaging/002-status.md`).
  - Unblock audit: record that M002 closure promotes no blocked work
    (provider M002 remains upstream-blocked; routing M002 evidence-gated;
    persistence/deployment have no eligible successor; see §11).
