# Deployment and Packaging Milestone 002 — Installer transaction and collision corrective

Status: ready

Repository baseline: `6639d4e6028e7aeebfc46b0c6743b67dc19774c7`

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-2--installer-transaction-and-collision-corrective`

Corrects:

- `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md`
- `plans/closure/deployment-packaging/001-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md`

Primary class: invariant

## 1. Objective

Correct two transactional/ownership defects discovered after M001 closure and
reconcile the stale deployment-packaging roadmap state without reopening or
redesigning the binary-first installer.

The pass must make these invariants mechanically true:

1. `--force` is a repair operation for an attributable EggPool installation;
   it is never permission to overwrite an unrelated regular file merely because
   it exists at the fresh-install destination.
2. A fresh install that reports failure after executable publication must not
   leave a newly installed executable behind when failure is caused by
   first-time config seeding.
3. Planning/current-state documentation describes the implementation that
   actually landed in M001 rather than the superseded uv/pipx-first behavior.

No release authority, target matrix, updater design, package publication
channel, or runtime behavior changes in this corrective.

## 2. Why this milestone is ready

M001 is formally closed at `plans/closure/deployment-packaging/001-status.md`,
but post-closure review found two concrete violations of M001's own stated
acceptance/invariant boundary.

No new architectural decision is needed. ADR-0001 already states that:

- ambiguous ownership and unrelated command collisions fail closed;
- a failed fresh install leaves no executable at the destination;
- fresh current-native installation remains binary-first;
- existing owners remain authoritative.

The defects are local to `scripts/install.sh` and its deterministic
qualification. All required release/provenance interfaces are already stable.

The current hosted CI run for the M001 closure commit is operational evidence,
not a hard dependency for planning this corrective. M002 closure must record
its own CI/local evidence truthfully.

## 3. Current implementation evidence

### Finding A — fresh `--force` can replace an unowned regular file

At baseline, `install_fresh_raw_binary()` in `scripts/install.sh`:

- calls `refuse_dest_collision()`, which rejects symlinks and non-regular
  special files but permits an arbitrary regular file;
- permits an existing regular `$INSTALL_DEST` when
  `FORCE_REINSTALL=1`;
- later commits with `mv -f "$staged_samefs" "$INSTALL_DEST"`.

The authority classifier reaches the fresh path when no `eggpool` command is
discoverable on PATH. Therefore a regular file at
`${EGGPOOL_INSTALL_BIN_DIR:-$HOME/.local/bin}/eggpool` that is not currently
on PATH, or otherwise is not attributable to an EggPool owner, can be replaced
by `--force`.

That contradicts M001 plan §4 ("unrelated command collisions fail closed"),
M001 acceptance criterion 9, ADR-0001's collision rule, and the CLI help
description that `--force` repairs without changing owner.

M001 qualification had `dest-unrelated-collision`, but only without
`--force`. The closure therefore generalized evidence beyond the tested
state.

### Finding B — config seeding occurs after executable commit without rollback

At baseline, the fresh path:

1. verifies the downloaded candidate;
2. atomically moves it to `$INSTALL_DEST`;
3. verifies installed provenance/version;
4. calls `seed_config_after_commit()`;
5. `seed_config_after_commit()` fails if
   `"$active" init-config "$config_path"` fails.

There is no fresh-install rollback around step 4. A filesystem permission,
invalid parent path, injected `init-config` failure, or other first-config
creation failure can therefore cause the installer to exit non-zero while
leaving the newly installed executable committed.

ADR-0001 requires a failed fresh install to leave no executable at the
destination. M001 plan §8 says fresh commit failure leaves no config mutation,
but the stronger acceptance/ADR transaction boundary covers post-commit
validation/config work as well.

M001 tests covered:

- successful missing-config creation;
- preservation of existing config;
- pre-commit download/hash/self-check failures.

They did not inject a post-commit `init-config` failure. The closure's
requirement 12 consequently did not exercise this failure point.

### Finding C — deployment-packaging roadmap current-state prose is stale

`plans/subsystems/deployment-packaging-roadmap.md#4-current-state` still says
the quick installer selects uv -> pipx -> bootstrap uv and that the qualifier
does not model stale pipx. M001 actually replaced that flow and added the stale
pipx regression.

The milestone table is current, but the prose is not. This is planning drift,
not a production defect.

## 4. Invariants that must not regress

- Fresh current-native curl install remains verified GitHub raw binary first.
- Fresh native install remains independent of Python, uv, pipx, pip, Cargo, and
  source compilation.
- Supported proxy targets remain Linux x86_64, Linux aarch64, and macOS arm64.
- Candidate checksum/version/native identity verification remains before
  executable publication.
- Moving-latest resolution remains bound to the selected exact release version.
- Existing uv/pipx/pip ownership remains manager-owned.
- Existing standalone ownership remains standalone unless explicit adoption is
  requested.
- Source checkout and ambiguous provenance continue to fail closed.
- `--force` never changes ownership implicitly.
- Symlink, special-file, and unrelated regular-file collisions fail closed.
- Existing config/database/`.env` remain untouched by this corrective.
- First-time config creation is all-or-nothing with a fresh executable install.
- Historical Python-era compatibility remains explicit and bounded.
- Release workflow, raw artifact names, ABI floors, PyPI, and crates.io
  publication are unchanged.
- Root/system production deployment behavior is unchanged.

## 5. Scope

### In scope

- Tighten fresh-path destination attribution before any `--force` replacement.
- Define safe `--force` behavior for:
  - a verified existing standalone EggPool command;
  - an attributable package-managed command;
  - an unowned regular destination file.
- Make fresh executable publication + first-config seeding transactionally
  recoverable.
- Add deterministic regression cases for both defects and adjacent failure
  states.
- Correct the deployment-packaging roadmap's current-state prose and milestone
  status.
- Correct static docs/help wording only where it overstates `--force`
  semantics or fresh-install completion.

### Explicitly out of scope

- Replacing the binary-first authority.
- Changing GitHub release/checksum semantics.
- Adding artifact signing/attestations.
- Refactoring the Rust updater.
- Changing package-manager transition behavior except where needed to preserve
  the existing owner boundary.
- Changing historical release catalog policy.
- Adding targets or Windows proxy support.
- Changing database/config schema.
- Reworking source-checkout install behavior.
- Broad shell installer rewrite.
- System/root deployment changes.

## 6. Required production changes

### 6.1 Separate "fresh destination exists" from "verified repair"

The installer must not interpret `--force` + regular file as sufficient
ownership evidence.

Before any fresh-path replacement of an existing `$INSTALL_DEST`:

- if no existing EggPool command/provenance was classified, an existing
  destination is a collision, even with `--force`;
- fail with an actionable diagnostic explaining that `--force` repairs a
  verified EggPool installation and cannot adopt/overwrite an unowned file;
- do not download/commit over the file;
- preserve its bytes, mode, and type.

If a verified EggPool command exists at that same path, the operation should
not be classified as fresh in the first place. It must use the existing-owner
authority:

- standalone -> native update/verified standalone repair;
- uv/pipx/pip -> owner-aware update/repair;
- source/ambiguous -> refuse.

If there is a verified EggPool command elsewhere on PATH while
`$INSTALL_DEST` contains a different file, retain the current collision
refusal rather than selecting one implicitly.

Do not add an "adopt arbitrary file" option in M002.

### 6.2 Make fresh install completion transactional through first config seed

Define the fresh installation transaction boundary as ending only after:

1. release verification;
2. executable atomic publication;
3. installed provenance/version revalidation;
4. first-time canonical config creation, when config was absent;
5. final postcondition verification.

If config already exists, no config write occurs and executable publication
may complete normally.

If config was absent and `init-config` fails after executable commit:

- remove/rollback the executable installed by this invocation;
- remove any newly created partial config file only when this invocation can
  prove it created it;
- never remove or alter a config path that existed before the invocation;
- release lock/temp state;
- exit non-zero with bounded recovery guidance.

Fresh install has no previous executable to restore. Therefore rollback is
normally "remove exactly the executable this transaction committed", guarded by
identity/revalidation so a concurrently replaced path is never deleted.

The implementation must defend the rollback itself against a race:

- record the committed candidate identity sufficient to distinguish it from a
  later replacement (at minimum expected hash/version plus path identity);
- before deleting on config failure, revalidate that the destination is still
  the executable committed by this invocation;
- if identity changed, fail closed and report manual recovery instead of
  deleting a potentially unrelated replacement.

Avoid creating a second general transaction framework. A narrow fresh-install
rollback helper is sufficient.

### 6.3 Clarify config creation cleanup

The config-seeding helper needs an explicit result/ownership contract.

Preferred behavior:

- determine whether config existed before invoking `init-config`;
- for the fresh path, call a helper that reports:
  - preserved-existing;
  - created-successfully;
  - failed-with-no-created-file;
  - failed-with-new-partial-file.
- if a new partial file exists after failure, remove it only if:
  - it did not exist at preflight;
  - path identity is still within the expected config destination;
  - removal cannot traverse an unexpected symlink/special-file boundary.

Do not broaden this into config migration or overwrite logic.

### 6.4 Keep existing-owner repair semantics unchanged

The existing standalone `--force` repair path already has a rollback copy and
is owner-attributed. M002 should not collapse it into the fresh path.

Manager-owned `--force` remains manager-owned.

Regression tests must prove the corrective does not accidentally make
`--force` unusable for legitimate verified repairs.

### 6.5 Reconcile planning/current-state documentation

Update
`plans/subsystems/deployment-packaging-roadmap.md#4-current-state`
to reflect the landed M001 state:

- fresh current-native installer is binary-first;
- SHA256SUMS selects the raw release asset;
- fresh installs become standalone Rust;
- existing owners delegate to native owner-aware update paths;
- deterministic qualification includes stale-pipx/no-Python Linux/aarch64;
- M002 exists because post-closure review found the two transaction/collision
  gaps above.

Preserve M001's historical closure record. Do not rewrite
`plans/closure/deployment-packaging/001-status.md` to pretend these findings
were known at closure. M002 is the authoritative corrective record.

## 7. Ordered work packages

### Work package A — Add failing regressions first

Intent:

Freeze both post-closure findings before changing production shell behavior.

Required changes:

Extend `scripts/qualify_quick_installer.py` and
`tests/tooling/test_installer.py` with named cases for:

- `fresh-force-unowned-regular-refusal`:
  - destination regular file exists;
  - it is not on PATH / has no verified EggPool provenance;
  - `--force` is supplied;
  - installer fails;
  - original bytes and mode remain unchanged;
  - no config is created;
- `fresh-init-config-failure-rolls-back-binary`:
  - destination initially absent;
  - candidate passes release/provenance/version checks;
  - fake candidate fails `init-config`;
  - installer exits non-zero;
  - destination executable is absent afterward;
  - no config/partial config remains;
- `fresh-init-config-failure-preserves-preexisting-config` if a failure can be
  induced without violating the "existing config means no seed" path; otherwise
  document why this state is impossible by dispatch.
- `fresh-config-failure-destination-race-refusal`:
  - destination changes after commit but before rollback;
  - rollback must not delete the replacement;
  - installer emits manual recovery guidance.

Acceptance evidence:

Both primary new cases fail against baseline
`6639d4e6028e7aeebfc46b0c6743b67dc19774c7` for the expected reason before
production changes are applied.

### Work package B — Harden fresh `--force` attribution

Intent:

Make `--force` mean verified repair, not overwrite permission.

Required changes:

- remove the branch that permits any existing regular fresh destination under
  `FORCE_REINSTALL`;
- route verified owners through the existing-owner classifier before fresh
  authority can be selected;
- retain collision refusal for an unowned regular destination regardless of
  `--force`;
- retain current symlink/special-file refusal.

Acceptance evidence:

- unowned regular destination + `--force` fails byte-for-byte unchanged;
- legitimate existing standalone `--force` repair still passes;
- legitimate manager-owned repair still passes;
- no owner changes.

### Work package C — Add post-commit fresh rollback

Intent:

Make first-time config seeding part of the fresh-install success transaction.

Required changes:

- record whether config existed before executable commit;
- after executable publication and provenance/version verification, attempt
  first-time config seed;
- on seed failure, safely remove only the executable committed by this
  invocation;
- clean only a newly-created partial config owned by this invocation;
- keep lock held until success or rollback completes;
- make rollback diagnostics distinguish:
  - rollback succeeded;
  - executable identity changed, manual recovery required;
  - config cleanup could not be proven safe.

Acceptance evidence:

Injected config failure leaves the system in the pre-invocation state when no
race occurs. A simulated destination race is never deleted by rollback.

### Work package D — Reconcile documentation and closure bookkeeping

Intent:

Remove stale planning state and prevent M001's closure overclaim from being
repeated.

Required changes:

- update deployment-packaging roadmap current-state prose;
- add M002 milestone/status linkage;
- update installer help/docs only if needed to make `--force` repair semantics
  explicit;
- ensure closure for M002 explicitly names why M001 verification missed each
  finding;
- keep M001 closure immutable.

Acceptance evidence:

Registry, roadmap, implementation plan, and later closure agree. Static doc
validators remain green.

## 8. Failure, cancellation, restart, contention semantics

### Unowned destination collision

Failure occurs before executable replacement. No config/data mutation.

### Fresh config-seed failure

The installer remains under its install lock while rolling back.

If the destination still matches the committed candidate identity, remove it.
If it no longer matches, do not delete it; report that rollback could not be
proven safe.

If a config file did not exist at invocation start but a new file appears and
can be proven to be the partial output of this invocation, remove it. Never
remove a pre-existing config.

### Signal during config seeding

Signal handling must not leave trap logic blindly deleting a destination after
ownership/identity has changed. Either:

- keep sufficient transaction state for a guarded rollback in the trap; or
- arrange the config creation step so the normal failure handler owns rollback
  and signal interruption is converted into that path.

Do not introduce unsafe unguarded `rm -f "$INSTALL_DEST"` cleanup.

### Existing-owner repair/update

Continue to delegate/reuse existing native updater and package-manager
semantics. M002 must not weaken their service restart or rollback behavior.

### Concurrency

Existing install lock remains held through first config seed and rollback.
Race revalidation before executable commit remains.

## 9. Compatibility and migration

No runtime/API/config/database migration.

Public quick-install command and binary-first authority are unchanged.

Behavioral tightening:

- previously, a fresh invocation with `--force` could replace an unowned
  regular destination file;
- after M002, that state is always a collision and requires explicit operator
  cleanup before install.

This is an intentional safety correction consistent with existing help text and
ADR-0001, not a compatibility promise to preserve unsafe overwrite behavior.

Legitimate verified repair paths remain supported.

## 10. Required tests

At minimum, preserve all M001 installer cases and add:

- fresh unowned regular destination + `--force` refuses;
- original unowned file bytes/mode preserved;
- no config created on that refusal;
- fresh candidate commit + `init-config` failure rolls executable back;
- partial newly-created config is cleaned when safely attributable;
- pre-existing config is never removed;
- destination replacement race during rollback is not deleted;
- rollback diagnostic is bounded and actionable;
- install lock is released after rollback success/failure;
- subsequent clean install can proceed after rollback;
- verified standalone `--force` repair still succeeds;
- verified uv/pipx/pip repair/update ownership remains unchanged;
- symlink/special-file/collision tests from M001 remain green;
- stale pipx/no-Python aarch64 regression remains green;
- checksum/version/moving-latest tests remain green.

Avoid a brittle total-case-count-only assertion. Require named case presence
for the corrective regressions.

## 11. Required verification commands

Focused:

```bash
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/test_release_docs.py tests/tooling/test_release_catalog.py -q
uv run python scripts/validate_release_docs.py
uv run python scripts/check_release_catalog.py
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
git diff --check
```

Full tooling because installer helpers share release tooling:

```bash
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

If no Rust code changes, record that explicitly and do not claim unrun Rust
commands as local evidence. Hosted CI may still execute the normal Rust matrix;
record its actual result in closure.

If any Rust file changes, run the repository's full Rust/default/no-default
matrix per the development skill.

## 12. Documentation updates

Required:

- `plans/subsystems/deployment-packaging-roadmap.md`
- `plans/registry.md`
- installer help text if `--force` wording needs clarification
- operator docs only if they currently imply `--force` can overwrite an
  unowned path

Do not rewrite M001 closure.

## 13. Acceptance criteria

1. An unowned regular `$INSTALL_DEST` is never replaced by a fresh install,
   including with `--force`.
2. `--force` remains functional for a verified existing standalone EggPool
   repair without changing owner.
3. Package-managed existing owners remain package-managed under repair/update.
4. Fresh config-seeding failure exits non-zero and removes the executable
   committed by that invocation when its identity is unchanged.
5. Fresh config-seeding failure does not remove a destination whose identity
   changed after commit.
6. A pre-existing config is never removed or rewritten by rollback.
7. A newly-created partial config from the failed transaction is removed only
   when safely attributable to that invocation.
8. Install lock/temp state is released after corrective rollback paths.
9. All M001 binary-first, no-Python, checksum, exact-version, owner-preservation,
   historical, collision, and moving-latest regressions remain green.
10. Deployment-packaging roadmap current-state prose reflects the landed
    binary-first implementation and the M002 corrective.
11. M001 closure remains immutable; M002 closure explicitly records the
    verification gaps that allowed these defects through.
12. No release target, runtime capability, package publication channel, schema,
    or production deployment ownership changes.

## 14. Stop conditions

Stop and report rather than improvise if:

- proving fresh executable identity for rollback requires a new persistent
  installer database or broad state subsystem;
- config seeding cannot be made rollback-safe without changing the runtime's
  config ownership semantics;
- legitimate verified `--force` repair cannot be distinguished from an
  unowned destination without changing public provenance contracts;
- the fix would require changing ADR-0001's binary-first authority;
- the pass expands into system/root deployment or release signing;
- repository evidence shows a concurrent implementation already changed these
  semantics and this plan's baseline is no longer accurate.

A small guarded hash/version/path revalidation is acceptable; a new general
transaction engine is not.

## 15. Closure evidence required

The M002 closure record must include:

- implementation commit(s);
- exact before/after behavior for both post-M001 findings;
- explanation of why M001 verification missed:
  - unowned regular destination specifically under `--force`;
  - post-commit `init-config` failure;
- named regression cases and outputs;
- evidence that legitimate verified `--force` repair still works;
- evidence that config failure rollback does not delete a raced replacement;
- full tooling result;
- docs/roadmap/registry reconciliation;
- actual hosted CI status/result if available;
- statement of Rust diff and which Rust commands were/weren't run;
- severity-tagged residual findings;
- unblock audit;
- disposition.

## 16. Handoff notes

Keep this corrective small. The binary-first architecture itself is sound.

The main implementation mistake to avoid is treating `--force` as a generic
filesystem overwrite switch. It is an EggPool ownership-aware repair switch.

The second mistake to avoid is an unguarded cleanup trap that removes
`$INSTALL_DEST` after any failure. Rollback must prove it is removing the
candidate committed by this transaction, especially under race injection.

Add the failing fixtures first, then make the smallest production-shell change
that satisfies them. Preserve the rest of M001's 42-case behavior unchanged.
