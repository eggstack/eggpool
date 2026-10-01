# Deployment and Packaging Milestone 003 — Config publication ownership corrective

Status: ready

Repository baseline: `ad38b85fa3945d22c9fac1a02b14bdc6f27e6c28`

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-3--config-publication-ownership-corrective`

Corrects:

- `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md`
- `plans/closure/deployment-packaging/002-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADR:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md`

Primary class: invariant

## 1. Objective

Correct the remaining config-path ownership flaw in the fresh binary-first
installer without reopening M001 or M002.

M002 correctly made executable publication rollback ownership-aware, but its
config cleanup still infers ownership from:

- config absent before the transaction; and
- config present afterward as a regular file.

That inference is not sufficient under concurrency. A different process can
create the final config path while EggPool's `init-config` is running; if
EggPool then fails, the M002 rollback can delete that unrelated config.

M003 must remove that ambiguity by making first-time config generation
transaction-owned from the beginning:

1. generate canonical config into a private transaction-owned staging path;
2. never ask `init-config` to write the user's final config path directly on
   the fresh path;
3. publish the staged config to the final destination only if the destination
   is still absent;
4. never overwrite or delete a config that appeared concurrently;
5. clean only the transaction-owned staged file on failure.

The executable rollback added in M002 remains in force.

## 2. Why this milestone is ready

No new architectural decision or upstream dependency is required.

ADR-0001 already requires preservation of unrelated user state and failed fresh
installs to leave no unowned residue. M002 already provides:

- install locking;
- private temp state;
- guarded executable rollback;
- binary-first fresh authority;
- deterministic race fixtures.

The defect is local to how config creation uses the final destination path.
The fix should therefore remain local to `scripts/install.sh`,
`scripts/qualify_quick_installer.py`, focused tooling tests, and planning/docs.

M002 closure remains immutable historical evidence. M003 is the authoritative
record for the post-M002 finding.

## 3. Current implementation evidence

At baseline, fresh config seeding in `install_fresh_raw_binary()` does:

```bash
if [[ -f "$fresh_config_path" ]]; then
    echo "Preserved existing config: $fresh_config_path"
else
    if "$active" init-config "$fresh_config_path"; then
        ...
    else
        fail_fresh_tx ...
    fi
fi
```

On failure, `fresh_tx_rollback_guarded()` contains:

```bash
if [[ -n "$config_path" ]] && (( ! config_existed )); then
    if [[ -e "$config_path" || -L "$config_path" ]]; then
        if [[ -L "$config_path" ]]; then
            need_manual=1
        elif [[ -f "$config_path" ]]; then
            rm -f "$config_path"
        else
            need_manual=1
        fi
    fi
fi
```

`seed_config_after_commit()` has the same ownership assumption for
existing-owner paths: if the config did not exist before the helper called
`init-config` and a regular file exists after failure, it removes the file.

This proves path shape, not transaction ownership.

### Concrete race

A valid failing sequence is:

1. installer observes final config absent;
2. installer starts `eggpool init-config FINAL_PATH`;
3. another process creates/replaces `FINAL_PATH` with operator state;
4. `init-config` exits non-zero;
5. installer sees a regular file at `FINAL_PATH`;
6. rollback removes it because it was absent at preflight.

The install lock does not prevent this because it coordinates EggPool installer
instances, not arbitrary processes writing the config path.

M002 qualification does not model a concurrent config-path writer. Its race
fixture changes the executable destination only.

## 4. Required invariants

- Fresh current-native install remains binary-first.
- No Python/uv/pipx/pip/Cargo/source dependency is added to the default path.
- Release checksum/version/native verification remains unchanged.
- Fresh executable publication remains atomic and M002 guarded rollback remains.
- Existing config is never overwritten.
- Config that appears concurrently is never removed or overwritten.
- Failed config generation never creates the final config path.
- Only transaction-owned temp/staging files may be unconditionally removed.
- Config publication must be no-clobber.
- Symlink/special-file boundaries at the final config path fail closed.
- Existing-owner update/repair must preserve config and ownership.
- Historical Python-era compatibility remains unchanged.
- No schema/config-format change.
- No release-target, publication-channel, runtime, or production deployment
  change.

## 5. Scope

### In scope

- Fresh first-config generation into transaction-owned staging.
- No-clobber publish from staging to final config path.
- Config-path concurrency/race tests.
- Removing unsafe "absent before + regular after => ours" cleanup.
- Reusing the safer staging helper from `seed_config_after_commit()` where
  practical so existing-owner config creation gets the same ownership
  semantics.
- Updating deployment docs/help only if current wording materially overstates
  rollback ownership.
- Roadmap/registry/closure bookkeeping.

### Out of scope

- Config schema changes.
- General config migration.
- Overwriting existing config.
- Config merge behavior.
- Cross-filesystem config replacement.
- Broad installer transaction framework extraction.
- Changes to Rust `init-config` semantics unless shell-safe staging is
  impossible.
- Release signing/attestation.
- Root/system deployment changes.
- Additional platforms.
- Runtime/provider/database behavior.

## 6. Target design

### 6.1 Transaction-owned staging

For a missing final config path, create a private staging file under a
transaction-owned directory.

Preferred location:

- same parent filesystem as final config when practical, so publication can be
  atomic; or
- an already-private installer temp directory if publication uses a safe
  no-clobber copy/link primitive and cleanup remains attributable.

The staging path must:

- be created by this invocation;
- not be discoverable as the active EggPool config;
- be mode-restricted while incomplete;
- be removed on failure/signal;
- never alias the final config path.

Run:

```bash
eggpool init-config STAGING_PATH
```

not:

```bash
eggpool init-config FINAL_PATH
```

Validate staging output as a regular non-symlink file before publication.

### 6.2 No-clobber final publication

After staging succeeds, revalidate final config state.

If final config:

- already exists as a regular file: preserve it; discard staged config; report
  that another actor supplied config;
- is a symlink: fail closed and discard staging;
- is a special file/directory: fail closed and discard staging;
- remains absent: publish staged config without overwriting a path that could
  appear between check and commit.

A check-then-`mv` sequence is insufficient because it has a TOCTOU race.

Use a primitive with true no-clobber creation semantics available on supported
hosts. Acceptable implementation families include:

- hard-link staging -> final followed by unlink staging, where same-filesystem
  constraints and mode semantics are satisfied;
- `set -C`/noclobber redirection into an exclusively created final file
  followed by bounded content copy, if implemented portably and verified;
- a repository-supported helper already providing O_EXCL/no-replace semantics.

Do not use plain `mv -f`, `cp -f`, or `test ! -e; mv` as the publication
boundary.

If shell portability prevents a dependable no-clobber primitive across Linux
and macOS, stop and report rather than weakening the ownership guarantee.
A tiny existing EggPool/Rust helper may be considered only if it does not add a
new runtime dependency to fresh install.

### 6.3 Rollback simplification

Once final config is never written directly during generation:

- remove final-config deletion from fresh rollback;
- rollback cleans transaction staging only;
- executable rollback remains guarded exactly as M002 defined;
- if final config appeared concurrently, it is preserved unconditionally.

The final config path should never need deletion as part of fresh rollback.

### 6.4 Existing-owner config seeding

`seed_config_after_commit()` currently repeats the unsafe direct-final
generation and cleanup pattern.

M003 should either:

A. reuse the same transaction-owned config staging + no-clobber publication
helper for all first-time config creation; preferred; or

B. prove that existing-owner call sites cannot race and document why. This is
unlikely to be defensible, so shared staging is the expected design.

Do not alter behavior when config already exists.

## 7. Ordered work packages

### Work package A — Add the missing race regression first

Add a deterministic fixture named at minimum:

- `fresh-config-concurrent-writer-preserved`

Scenario:

1. final config absent at installer preflight;
2. candidate begins config generation;
3. fixture creates operator-owned bytes at final config path before generation
   reports failure or before publish;
4. installer completes failure/preservation handling;
5. final config bytes remain exactly operator-owned;
6. installer never deletes or overwrites them.

The fixture must fail against baseline
`ad38b85fa3945d22c9fac1a02b14bdc6f27e6c28` for the expected reason.

Also add a successful publish race case:

- `fresh-config-publish-race-preserves-winner`

The candidate successfully generates staging output, but another process wins
the final path immediately before publication. The installer must preserve the
winner and discard staging.

### Work package B — Introduce shared config staging helper

Create a narrow shell helper responsible for:

- detect existing final config;
- create transaction-owned staging;
- call `init-config STAGING_PATH`;
- validate staging type;
- publish no-clobber;
- clean staging;
- return structured/bounded status such as:
  - preserved-existing;
  - created;
  - concurrent-existing-preserved;
  - generation-failed;
  - unsafe-final-boundary;
  - publish-failed.

Avoid a general transaction abstraction.

### Work package C — Remove final-path rollback deletion

Update `fresh_tx_rollback_guarded()`:

- executable rollback remains hash/version guarded;
- it must not remove `FRESH_TX_CONFIG`;
- it may remove only an explicitly recorded transaction-owned staging path;
- EXIT/signal traps retain idempotence.

Delete comments/closure assumptions that equate "absent at start" with config
ownership.

### Work package D — Apply shared helper to existing-owner first seed

Replace direct `seed_config_after_commit()` final-path generation with the
shared safe config staging/publish helper.

Regression requirements:

- existing config preserved;
- first-time config creation still succeeds;
- concurrent writer preserved;
- package-managed owner unchanged;
- standalone repair/update unchanged.

### Work package E — Reconcile planning/docs and close

Update:

- deployment-packaging roadmap current-state prose;
- registry;
- `docs/deployment.md` only if needed;
- M003 closure evidence.

M001/M002 closure records remain immutable.

## 8. Failure and recovery semantics

### Generation failure

If `init-config STAGING_PATH` fails:

- remove staging only;
- fresh path invokes existing executable rollback;
- final config path is untouched.

### Concurrent final config appears before publish

Preserve final config. Remove staging.

For fresh install, a concurrently supplied valid config is not itself an
installation failure unless existing documented behavior requires one. The
implementation may complete successfully and report preservation, provided the
final path is a safe regular file.

If final path is symlink/special, fail closed and roll back the fresh
executable; never remove the unsafe final path.

### Publish failure

If no-clobber publication fails for an unclassified I/O reason:

- final config is untouched;
- staging removed when safe;
- fresh executable rollback proceeds;
- bounded manual-recovery diagnostics if staging cleanup fails.

### Signals

Signal traps may delete transaction-owned staging, but never final config.
Executable rollback remains guarded as in M002.

## 9. Compatibility and migration

No migration.

The observable successful result remains:

- fresh binary installed as `standalone-rust`;
- canonical config created only if absent.

Behavior tightening:

- config creation is now generated off-path and published no-clobber;
- a concurrently appearing config wins and is preserved.

No user-facing flag changes are required.

## 10. Required deterministic tests

Preserve all 46 M001/M002 cases and add at minimum:

- `fresh-config-concurrent-writer-preserved`;
- `fresh-config-publish-race-preserves-winner`;
- successful staged first-config creation;
- staged generation failure leaves final config absent;
- staged partial file cleaned after generation failure;
- final symlink refusal;
- final special-file refusal;
- no-clobber publication never overwrites concurrent regular file;
- signal cleanup removes staging but never final config;
- executable rollback still occurs when config generation truly fails;
- executable raced replacement still preserved by M002 guard;
- existing-owner first-config creation uses safe staging;
- existing-owner concurrent config preserved;
- package-managed/standalone ownership unchanged.

Do not rely only on total case count. Assert the M003 case names explicitly.

## 11. Verification commands

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

If no Rust changes, state that explicitly. Hosted CI may run Rust anyway; record
actual status in closure.

If Rust changes are required for no-clobber publication, run the full repository
Rust/default/no-default matrix and justify why shell-only implementation was
not sufficiently portable.

## 12. Acceptance criteria

1. Fresh config generation never writes directly to the final config path.
2. Failed config generation never deletes or modifies the final config path.
3. Concurrent regular config creation is preserved byte-for-byte.
4. Successful staged config publication cannot overwrite a concurrent winner.
5. Final symlink/special-file boundaries fail closed without mutation.
6. Rollback deletes only transaction-owned staging, never final config.
7. M002 executable rollback remains hash/version guarded and race-safe.
8. Existing-owner first-time config seeding gets equivalent no-clobber
   semantics.
9. Existing config behavior is unchanged.
10. All prior 46 installer cases remain green.
11. New config-race regressions fail on the M002 baseline and pass after M003.
12. No release/runtime/schema/target/package-channel/root-deployment changes.
13. Roadmap/registry/closure state is coherent and M001/M002 closures remain
    immutable.

## 13. Stop conditions

Stop and report if:

- no portable no-clobber publication primitive can be implemented for the
  supported Linux/macOS shell environments;
- fixing this requires changing config format/schema;
- fixing this requires making the default installer depend on Python, jq, gh,
  Cargo, or another external runtime;
- a Rust helper becomes necessary but would materially expand installer/runtime
  API surface;
- repository state has already implemented equivalent config staging semantics.

Do not replace the race with a wider check-then-act window and call it fixed.

## 14. Closure evidence required

Closure must include:

- implementation commit(s);
- before-fix reproduction of concurrent config deletion/overwrite;
- named M003 race cases;
- evidence final operator bytes survive concurrency;
- evidence staged config cleanup is transaction-owned;
- evidence final config is never deleted by rollback;
- evidence M002 executable rollback still works;
- full tooling results;
- actual hosted CI status if available;
- Rust diff statement;
- roadmap/registry reconciliation;
- residual findings by severity;
- disposition.

## 15. Handoff notes

The safest direction is to stop treating the final config path as scratch
space.

Generate configuration privately, then publish with a true no-clobber
operation. That converts ownership from an inference into a filesystem
property: the installer owns staging because it created staging, and it never
deletes the user's final config.

Keep M003 narrow. Do not use this pass to redesign config APIs, extract a
general installer transaction engine, or add release hardening.
