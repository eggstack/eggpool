# Deployment and Packaging Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/deployment-packaging/003-config-publication-ownership-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-3--config-publication-ownership-corrective`

Repository baseline reviewed: `ad38b85fa3945d22c9fac1a02b14bdc6f27e6c28`

Implementation commits or pull requests:

- `292a1e3f` — Implement config publication ownership corrective (M003 staging + no-clobber, 60-case qualification)

## 1. Executive finding

The invariant corrective is complete. First-time config generation no longer
writes directly to the final config path and rollback never deletes the final
config path. Config is generated only into a private transaction-owned staging
directory on the same filesystem and published with true no-clobber `ln`
semantics; a concurrently appearing regular config wins byte-for-byte and
staging is discarded. Symlink/special boundaries fail closed. The M002
hash/version-guarded executable rollback is intact. Deterministic qualification
grows from 46 to 60 cases with the two primary post-M002 race regressions plus
staged/symlink/special/no-clobber/signal/existing-owner guards green, and all
M001/M002 regressions remain green. Disposition is `closed` with no
medium-or-higher unresolved finding. M001/M002 closures remain immutable; this
record is the authoritative M003 evidence.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| 1. Fresh config generation never writes directly to final | `scripts/install.sh`: `install_fresh_raw_binary` calls only `safe_seed_config_via_staging "$active" "$fresh_config_path"`; `init-config` invoked only as `init-config "$staging"` inside helper | pass | `grep init-config scripts/install.sh` shows no final-path invocation on fresh path |
| 2. Failed generation never deletes/modifies final | `fresh_tx_rollback_guarded` contains no `rm` of `FRESH_TX_CONFIG`; only `CONFIG_STAGING_DIR` removed; `fresh-config-concurrent-writer-preserved` asserts operator bytes survive failure | pass | Final-path deletion block removed |
| 3. Concurrent regular creation preserved byte-for-byte | `fresh-config-concurrent-writer-preserved` (operator `operator-config-concurrent`) + `fresh-config-publish-race-preserves-winner` (`concurrent-winner`) + `fresh-config-noclobber-never-overwrites` all assert exact bytes | pass | Before: deleted/overwritten; after: preserved |
| 4. Staged publish cannot overwrite concurrent winner | Helper publishes only via `ln "$staging" "$final"`; on `ln` failure with regular final present returns preserved-success; `fresh-config-publish-race-preserves-winner` proves | pass | No `mv -f`/`cp -f`/check-then-`mv` on publish boundary |
| 5. Symlink/special fail closed without mutation | `safe_seed_config_via_staging` returns 2 on `-L`/non-regular at entry, after mkdir, after staging, and after `ln` failure; `fresh-config-symlink-refusal` + `fresh-config-special-refusal` assert symlink/fifo preserved, DEST absent, staging cleaned | pass | Fresh failure rolls back DEST via `fail_fresh_tx` |
| 6. Rollback deletes only staging, never final | `fresh_tx_rollback_guarded` + `cleanup_config_staging` + traps clean only `CONFIG_STAGING_DIR`; `fresh-staged-partial-cleaned-after-generation-failure` + signal case assert no `.eggpool-config-staging.*` residue and final untouched | pass | `FRESH_TX_CONFIG_EXISTED` no longer gates deletion |
| 7. M002 executable rollback remains guarded/race-safe | `fresh_tx_rollback_guarded` hash (`SELECTED_SHA256`) + version revalidation unchanged; `fresh-executable-rollback-on-config-generation-failure` (DEST absent on true failure) + `fresh-executable-race-still-preserved` + M002 `fresh-config-failure-destination-race-refusal` pass | pass | No M002 guard weakened |
| 8. Existing-owner first seed gets equivalent no-clobber semantics | `seed_config_after_commit` + `run_package_authority` both delegate to `safe_seed_config_via_staging`; `existing-owner-first-config-uses-safe-staging` (creates) + `existing-owner-concurrent-config-preserved` (preserves) pass | pass | Shared helper, no direct-final generation remains |
| 9. Existing config behavior unchanged | `existing-config-preserved`, `existing-standalone-delegates`, `fresh-init-config-failure-preserves-preexisting-config`, `existing-owner-concurrent-config-preserved` all assert preservation | pass | Preserved-existing returns 0 without write |
| 10. All prior 46 installer cases remain green | `qualify_quick_installer.py` 60 cases include all 46 M001/M002 names unchanged; `test_installer.py` asserts 46 + 14 M003 names | pass | No M001/M002 fixture renamed |
| 11. New race regressions fail on M002 baseline, pass after M003 | Baseline `ad38b85f` + new fixtures: `fresh-config-concurrent-writer-preserved` fails (`final.is_file` false — deleted); `fresh-config-publish-race-preserves-winner` fails (final is staged bytes, not winner); both pass on `292a1e3f` | pass | See §4 before-fix reproduction |
| 12. No release/runtime/schema/target/package-channel/root changes | Zero Rust diff; `check_release_catalog`, `validate_release_workflow` equivalent, `validate_release_docs` pass; target matrix Linux x86_64/aarch64 + macOS arm64 unchanged | pass | Narrow installer/qualification/docs only |
| 13. Roadmap/registry/closure coherent, M001/M002 immutable | This file + roadmap M003 `closed` + plan `active`→`closed`; `git diff` shows no change to `001-status.md`/`002-status.md` | pass | Corrective does not rewrite history |

## 3. Production implementation evidence

Landed changes (zero Rust diff; installer/tooling/docs only):

- `scripts/install.sh`:
  - New global `CONFIG_STAGING_DIR` + `cleanup_config_staging()` (removes only staging, never final).
  - `fresh_tx_rollback_guarded()` rewritten to M003 semantics: hash/version-guarded DEST removal retained exactly; final-config deletion block removed; staging cleanup only; staging-cleanup failure is bounded manual recovery.
  - `install_trap_cleanup`/`fresh_tx_signal_handler` clean staging on all paths (fresh + existing-owner), never final.
  - `fail_fresh_tx()` messages updated to state final config is never deleted.
  - New `safe_seed_config_via_staging <active> <final>`: existing-regular → preserved; symlink/non-regular → unsafe 2; absent → `mkdir -p` parent, revalidate, `mktemp -d parent/.eggpool-config-staging.XXXXXX` (same filesystem, 0700), `init-config STAGING` (never FINAL), validate regular non-symlink staging (0600), revalidate final, `ln STAGING FINAL` no-clobber publish; concurrent regular → preserved-success with staging discard; symlink/special → unsafe 2; `ln` I/O failure with still-absent final → publish-failed 3.
  - `install_fresh_raw_binary()`: transactional seeding replaced with helper; success disarms + `cleanup_config_staging`.
  - `seed_config_after_commit()`: direct-final generation + heuristic cleanup replaced with shared helper (preserved/created/concurrent-preserved vs generation-failed/unsafe/publish-failed).
  - `run_package_authority()`: direct `init-config FINAL` replaced with shared helper + `cleanup_config_staging`.
  - `FRESH_TX_ACTIVE/COMMITTED` arming clears `CONFIG_STAGING_DIR`.
- `scripts/qualify_quick_installer.py` (46 → 60 cases):
  - Helpers `_final_config_for_env`, `_fake_raw_binary_config_concurrent_writer`, `_fake_raw_binary_config_publish_race`, `_fake_raw_binary_blocking_init`, `_assert_no_staging_residue`, extended `_fresh_failing_release` kinds.
  - New: `fresh-config-concurrent-writer-preserved`, `fresh-config-publish-race-preserves-winner`, `fresh-staged-first-config-created`, `fresh-staged-generation-failure-leaves-final-absent`, `fresh-staged-partial-cleaned-after-generation-failure`, `fresh-config-symlink-refusal`, `fresh-config-special-refusal`, `fresh-config-noclobber-never-overwrites`, `fresh-signal-cleanup-removes-staging` (marker-synchronized SIGTERM via process group, bounded 10s timeouts), `fresh-executable-rollback-on-config-generation-failure`, `fresh-executable-race-still-preserved`, `existing-owner-first-config-uses-safe-staging`, `existing-owner-concurrent-config-preserved`, `existing-owner-package-standalone-unchanged`.
- `tests/tooling/test_installer.py`: asserts 60 cases + 4 M002 + 14 M003 names; harness timeout 60s → 180s for 60-case runtime (~92s local).
- `docs/deployment.md`: fresh transactional paragraph now states staging + no-clobber hard-link, final never written directly/deleted, concurrent wins, symlink/special fail closed, only staging cleaned.
- `plans/implementation/deployment-packaging/003-*.md`: `ready` → `active` in implementation commit; `active` → `closed` in this closure commit.
- `plans/subsystems/deployment-packaging-roadmap.md`: §4 prose now records M003 landed behavior + 60 cases; milestone table M003 `ready` → `closed` with closure link.

Before/after:

- Concurrent-writer failure: before, `install_fresh_raw_binary` called `init-config FINAL`; concurrent operator bytes at FINAL were deleted by `fresh_tx_rollback_guarded` (`!existed && -f → rm -f`). After, `init-config STAGING` fails, staging removed, FINAL (`operator-config-concurrent`) preserved, DEST rolled back.
- Publish race: before, direct-final generation overwrote winner with staged bytes (`[server] port`). After, `ln STAGING FINAL` fails EEXIST, winner (`concurrent-winner`) preserved byte-for-byte, staging discarded, install succeeds with preservation message.
- Symlink: before, `[[ -f symlink-to-file ]]` true → preserved-success (should fail closed) or `init-config FINAL` → Rust symlink error then manual-recovery with DEST left installed. After, helper detects `-L` at entry, fails closed, DEST rolled back, symlink untouched.
- Special (fifo/dir): before, `init-config FINAL` → Rust “already exists” then manual-recovery with DEST left. After, helper returns unsafe 2, DEST rolled back, fifo/dir untouched.
- Rollback scope: before, `FRESH_TX_CONFIG_EXISTED` gated final deletion. After, no final deletion path exists; only `CONFIG_STAGING_DIR` removed.

Why M002 verification missed the finding:

- M002 fixtures covered post-commit `init-config` failure and DEST identity race, but never modeled a concurrent config-path writer. Its race fixture (`_fake_raw_binary_race_on_config`) overwrote DEST, not FINAL. The matrix asserted “newly-created partial config removed only when safely attributable” via `!existed && -f && ! -L`, which the closure generalized as ownership. The production branch equated “absent at preflight” with installer ownership, which is insufficient when another process can create FINAL during `init-config`. M003 adds the missing concurrent-FINAL fixtures and replaces inference with filesystem no-clobber ownership.

Planned but absent: none. No release authority, target matrix, updater, package publication, schema, or root deployment changes (all out of scope per plan §5 and respected).

## 4. Verification executed

### Commands run

```bash
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/test_release_docs.py tests/tooling/test_release_catalog.py -q
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/check_release_catalog.py
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
git diff --check
```

Rust: no `rust/` diff in this pass, so no `cargo fmt`/`clippy`/`test`/`--no-default-features` matrix was required. Stated explicitly as justified substitute (zero production Rust change; `install-provenance`/`update` seams used as-is). Hosted CI still executes the normal Rust matrix; its result was not observed locally at closure time (see below).

### Results

Local (this host):

- `qualify_quick_installer.py`: pass, 60 cases (46 M001/M002 + 14 M003; all names in `test_installer.py`).
- Before-fix reproduction (baseline `ad38b85f` installer + new fixtures):
  - `fresh-config-concurrent-writer-preserved` failed (`final.is_file` false — operator file deleted by old rollback).
  - `fresh-config-publish-race-preserves-winner` failed (final bytes were staged `[server] port`, not `concurrent-winner` — overwritten).
  - Both fail for the expected ownership reason before production changes.
- `test_installer.py`: 1 passed (harness green, 60 asserted + M002/M003 names; timeout raised to 180s; ~61s local).
- Release focused: `test_release_docs` + `test_release_catalog`: 12 passed.
- Full tooling: 126 passed, 1 skipped.
- `validate_release_docs.py`: pass (`docs_checked 7`, `published 0.8.1`).
- `check_release_catalog.py`: pass (58 releases; native `0.8.1` published; 8 rollback-compatible).
- `ruff format --check`: pass (45 files formatted).
- `ruff check`: pass (all checks passed).
- `pyright scripts/`: 0 errors.
- `git diff --check`: clean.
- No concealed partial execution; all commands run to completion locally.

Hosted CI: not observed locally at closure time. This is a tooling/docs pass with zero Rust diff; local deterministic qualification is the gate per plan §11. Hosted CI remains truth for release gates and will execute the normal Rust matrix on push; any CI-only failure requires a follow-up corrective, not a silent amendment to this record.

Fixture evidence highlights (disposable, no network, no real home):

- Concurrent-writer: operator `operator-config-concurrent` at FINAL survives `init-config` exit 3; DEST absent afterward; no `.eggpool-config-staging.*` residue; lock released.
- Publish-race: staged `[server] port` discarded, winner `concurrent-winner` preserved byte-for-byte; DEST present; preservation message in output; no staging residue.
- Staged success: FINAL contains `port = 11300`, DEST present, no staging residue.
- Generation failure: FINAL absent, DEST absent, staging cleaned, `init-config`/`config` in diagnostic, `rolled back` in output.
- Symlink: symlink + target bytes preserved, DEST absent, `symlink` in diagnostic, no staging residue.
- Special: fifo/dir preserved, DEST absent, `regular file`/`refusing` in diagnostic, no staging residue.
- Signal: `init-started` marker synchronized SIGTERM → non-zero exit, FINAL never created as staged bytes, staging cleaned, lock released.
- Executable guards: true failure → DEST absent; DEST race (`racing`) → DEST preserved with manual recovery.
- Existing-owner: first creation succeeds via staging; pre-existing `operator-existing` preserved; no staging residue; uv/standalone ownership retained.

## 5. Invariant review

Per source-plan §4, each remains true:

- Fresh current-native curl install remains verified GitHub raw binary first: `select_raw_from_sidecar` + `verify_staged_candidate` order unchanged; 60-case + release validators pass.
- Fresh native independent of Python/uv/pipx/pip/Cargo/source: fresh-binary authorities still ignore manager discovery; `fresh-linux-aarch64-no-python` + `stale-pipx-ignored` pass with `manager.log` absent.
- Supported proxy targets Linux x86_64/aarch64 + macOS arm64: `TARGET_CLASS` gate + `release-manifest-raw-contract` unchanged.
- Candidate checksum/version/native verification before publication: `verify_staged_candidate` unchanged.
- Moving-latest bound to exact version: pinned asset URL + exact agreement unchanged.
- Existing uv/pipx/pip ownership manager-owned: delegation + owner check unchanged; retentions pass.
- Existing standalone standalone unless explicit adoption: delegates + adoption flag unchanged.
- Source/ambiguous fail closed: refusals pass; fresh revalidation retained and unconditional.
- `--force` never changes ownership: fresh `--force` still refuses unowned; existing repair retains owner.
- Symlink/special/unrelated collisions fail closed: dest + new final-symlink/special cases pass.
- Existing config/database/`.env` untouched: preservation cases pass; rollback never touches final (stronger than M002).
- First-time config all-or-nothing with fresh executable (M003 hardened): staged generation + no-clobber publish + guarded DEST rollback; concurrent winner preserved; lock held through rollback.
- Historical Python compatibility explicit/bounded: catalog gate + precheck unchanged.
- Release workflow/raw names/ABI/PyPI/crates.io unchanged: validators pass.
- Root/system production unchanged: `root-refusal` passes; no `deploy` change.
- No schema/config-format change: helper only changes path ownership, not bytes; staged bytes are canonical `init-config` output.

## 6. Failure and recovery review

- Generation failure (no concurrent): under install lock, DEST hash/version revalidated and removed, staging removed, FINAL untouched (absent stays absent), lock/temp released, non-zero exit with `rolled back; final config never deleted`.
- Generation failure (concurrent writer): same DEST rollback, staging removed, FINAL operator bytes preserved byte-for-byte, lock/temp released.
- Publish race (success + winner): `ln` fails EEXIST, winner preserved, staging removed, install succeeds with preservation message, lock released.
- Final symlink/special (fresh): helper returns unsafe 2, `fail_fresh_tx` rolls back DEST, staging removed, symlink/special untouched, lock released.
- Publish I/O failure (final still absent but `ln` failed): staging removed, DEST rolled back, bounded diagnostic, lock released.
- DEST race (M002 guard): hash mismatch → no DEST delete, staging removed, FINAL untouched, manual-recovery diagnostic, lock released.
- Signal during seeding: INT/TERM/HUP handler runs guarded DEST rollback + staging cleanup (never final), releases lock/temp, exits 130; EXIT trap idempotent via `ROLLBACK_DONE` + unconditional staging cleanup for non-fresh paths.
- Existing-owner seed failure: `fail` without DEST mutation (DEST is pre-existing), staging removed, FINAL preserved, manager restoration via `restore_standalone_fn` where applicable.
- Concurrency: `install.lock.d` held through seed + rollback; pre-commit DEST revalidation unconditional; `lock-contention` + `target-race-refusal` still pass; config concurrency handled by no-clobber `ln`, not by the install lock (which cannot coordinate arbitrary writers).
- Malformed/ambiguous sidecar, checksum/size/version/target failures: fail before commit; no DEST/config/staging residue.

## 7. Migration and compatibility review

No runtime/API/config/database migration. Public quick-install command and binary-first authority unchanged.

Intentional tightening (consistent with ADR-0001, not a promise to preserve unsafe inference):

- Previously fresh config generation wrote directly to FINAL and rollback deleted FINAL when absent-at-preflight; after M003 FINAL is never written directly and never deleted. A concurrently appearing config now wins and is preserved instead of being deleted/overwritten.
- Previously symlink FINAL could be treated as preserved-existing (via `-f` following) or leave DEST installed on failure; after M003 symlink/special always fails closed with DEST rollback and FINAL preservation.
- Previously package-manager first-config wrote directly to FINAL; after M003 it uses the same staging/no-clobber helper.

Legitimate verified repair paths remain supported (standalone/manager fixtures prove). No schema, API, wire, or protocol migration; zero Rust diff.

## 8. Security review

- Release HTTPS, checksum-before-execution, size bound, staged self-check, atomic same-filesystem commit: unchanged (fixtures prove).
- No credentials read/emitted: installer handles no secrets; closure and fixtures secret-free (no prompts, raw bodies, cache keys).
- Unsafe paths fail closed: symlink/special/unowned + DEST race + config concurrent/publish races all refuse or preserve without traversal; rollback never follows symlink or deletes raced replacement; staging is `0700` dir + `0600` file with unpredictable `mktemp` names in the config parent (same filesystem), never the active config path.
- Bounded output: version/authority/ownership/command/config only; rollback diagnostics bounded with explicit paths, no env/credential leak.
- Secret-free: verified via `git diff` (no secret handling added).

## 9. Documentation and operations

Updated:

- `scripts/install.sh --help`: unchanged (no config-ownership overstatement; `--force` wording from M002 remains accurate).
- `docs/deployment.md`: fresh transactional paragraph now states staging + no-clobber hard-link, final never written directly/deleted, concurrent wins, symlink/special fail closed, only staging cleaned.
- `plans/subsystems/deployment-packaging-roadmap.md`: §4 now records M003 landed behavior + 60 cases; milestone table M003 `ready` → `closed` with closure link (this file).
- `plans/registry.md`: M003 `ready` → `closed`; unblock audit below.
- Validators: `validate_release_docs.py` still passes (binary-first guards intact).

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Physical SBC (Le Potato or comparable Linux/aarch64) run `not measured` in this pass | Operational confidence only; deterministic host fixtures pass and roadmap states physical evidence is valuable but not required | Future operator may record a fresh-device run without code change; no corrective plan required |
| low | GitHub artifact attestations / immutable-release enforcement not added | Supply-chain hardening deferred per plan §5 out of scope; SHA-256 sidecar remains authority | Separate hardening plan if desired; must not require `gh`/jq/Python in default installer |

No critical, high, or medium findings. No correctness or security gap remains.

## 11. Roadmap disposition

Milestone closed; no successor is eligible from this closure. The deployment-packaging roadmap retains M001/M002/M003 closed with no ready successor; future hardening (attestations, system/root distribution, additional targets) requires new bounded plans and must not hitchhike on M003. Provider-transport M002 remains independently blocked on upstream Eggfetch and is unaffected. Routing-selection M002 stays evidence-gated. Persistence has no eligible successor. Dashboard M001 remains the sole ready handoff (independent workstream).

## 12. Registry updates

Applied in the same commit as this closure:

- `plans/implementation/deployment-packaging/003-config-publication-ownership-corrective.md`: `active` → `closed`.
- `plans/subsystems/deployment-packaging-roadmap.md`: M003 `active` → `closed` with closure link `plans/closure/deployment-packaging/003-status.md`.
- `plans/registry.md`:
  - Active roadmaps: deployment-packaging `M003 ready` → `M003 closed — config publication ownership corrective`.
  - Dependency-ready plans: remove M003 (no deployment-packaging ready plans remain; Dashboard M001 remains sole ready).
  - Recently closed: add deployment-packaging M003 (`closed`, implementation `292a1e3f`, closure `plans/closure/deployment-packaging/003-status.md`).
  - Unblock audit: record that M003 closure promotes no blocked work (provider M002 remains upstream-blocked; routing M002 evidence-gated; persistence/dashboard unaffected; see §11).
