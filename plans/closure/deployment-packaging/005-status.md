# Deployment and Packaging Milestone 005 — Closure Status

Status: closed

Source implementation plan:

- `plans/archive/implementation/deployment-packaging/005-active-path-integration-detection-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-5--active-path-integration-detection-corrective`

Repository baseline reviewed: `48a114db232609e515de673a6617e163e44059bc`

Implementation commits or pull requests:

- `576b462d` — implement deployment packaging M005 PATH detector corrective (`profile_has_active_bin()` Gregg guard + zsh array, 5 new qualification cases, wrapper 73 → 78).

## 1. Executive finding

The detector corrective is complete and verified. A functioning Gregg-managed guarded `case` block and a functioning zsh tied `path=(...)` array exposing the verified installed directory now report `Future-shell profile: already active` with the startup file left byte-for-byte unchanged. Comment-only, incomplete/disabled, quoted-tilde, subpath, wrong-directory, and custom-target fixtures cannot suppress a needed append; the existing EggPool-managed block, ordinary Bash PATH forms, ownership/provenance, checksum, rollback, opt-out, unsafe-profile, and parent-shell activation invariants remain green. The 73-case M004 harness passes unchanged alongside 5 new M005 cases (78 total). Disposition: `closed`, with no unresolved correctness or security findings.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Reproduce Gregg + zsh false negatives on baseline `48a114db` | Direct `profile_has_active_bin()` driver on baseline-equivalent code (no prod diff `48a114db..HEAD -- scripts/install.sh` before fix): simple `export PATH="$HOME/.local/bin:$PATH"` → ACTIVE; Gregg exact guarded block → INACTIVE; `path=("$HOME/.local/bin" $path)` → INACTIVE | pass | Both misses confirmed before fix; control stayed green. Explains M004 omitted matrix (no Gregg `case` or `path` array coverage). |
| Gregg guarded block recognized when targeting `want` | `gregg-guarded-active-path-reused`: exact Gregg block, default path, profile bytes/hash unchanged, no EggPool marker, `already active`; rerun still unchanged + `already active` | pass | Bounded `case` → guard → export → `esac` recognizer; `$HOME` / `${HOME}` literals + expanded `want`; custom `want` does not match default literals. |
| Zsh tied-array recognized with real startup proof | `zsh-active-path-array-reused` with `ZDOTDIR`: `path=("$HOME/.local/bin" $path)` unchanged + `already active`; disposable `zsh -ic "command -v eggpool && eggpool version"` resolves `0.8.1` where `zsh` exists | pass | Array matched before discarding `path=` prefix as discrete entries; local Darwin x86_64 `zsh 5.9` smoke `true`; hosted macOS arm64 job supplies target-class evidence. |
| Inactive guard does not suppress append | `inactive-guard-does-not-suppress-append`: marker/comment-only and disabled-export fixtures each → `persisted` exactly once, operator prefix preserved, rerun `already active` with still one block | pass | Comment-only, missing `esac`, commented export all stay inactive; safe append is conservative. |
| Custom bin not covered by Gregg default | `custom-bin-not-covered-by-gregg-default`: Gregg `~/.local/bin` block + `EGGPOOL_INSTALL_BIN_DIR=<custom>` → `persisted` for custom, Gregg preserved, one EggPool block with custom literal; rerun `already active` + still one | pass | `gregg_targets()` requires discrete `want`; default literals never satisfy custom `want`. |
| Valid array syntaxes + negative matrix, no false positive | `zsh-array-syntax-and-negative-matrix`: direct-detector positives (`$HOME`, `${HOME}`, unquoted `~/`, unquoted `$HOME`, `export path=`, multi-element, custom absolute) all active; negatives (quoted `~/`, commented, `echo`, `printf`, function single-line, unrelated var, subpath, incomplete paren, wrong dir, orphan `case`/`export`/`esac`, marker-only) all inactive; plus installer proof that quoted-tilde `path=("~/.local/bin" $path)` → `persisted` once | pass | Discrete boundary checks reject `/tool` suffix and `bin2`; quoted tilde rejected via immediate-quote check; `echo`/`printf`/unrelated rejected by anchoring; incomplete paren requires `)`. |
| Existing EggPool-managed + ordinary PATH stay idempotent | `active-user-path-reused-comment-only-ignored` (still green), `profile-zdotdir-custom-path-idempotent` rerun count==1, `eggpool-managed` direct-detector active, `export PATH=...` control active | pass | `EGGPOOL_INSTALL_BIN_DIR` + single-line `case` check untouched; colon splitter untouched except dead array branch removal. |
| All M001–M004 ownership/transaction/profile safety green | Full 78-case harness green; `hidden-canonical-native-owner`, `hidden-canonical-manager-owner`, `hidden-foreign-force-refusal`, `path-visible-command-canonical-conflict`, `profile-symlink-refusal-and-optout`, `unsupported-shell-and-unsafe-bin-fallback`, `failed-config-transaction-does-not-edit-profile`, `documented-parent-shell-activation`, `package-manager-profile-uses-verified-owner-bin` all pass | pass | No owner/provenance/target/collision/`--force`/checksum/manager/config/rollback/signal/activation change. |
| Zero Rust/runtime/protocol/storage/artifact change | `git diff --name-only 48a114db..576b462d -- rust/` empty; full diff is `scripts/install.sh`, `scripts/qualify_quick_installer.py`, `tests/tooling/test_installer.py`, planning/registry/roadmap only | pass | Shell-only plan; no `--no-default-features` Rust gates required beyond zero-diff statement. |
| Docs/comments only where overstated | `scripts/install.sh` detector header documents Gregg/array bounded behavior + conservative fallback; no public quickstart/opt-out/activation doc change (none overstated `already active`); M004 closure immutable | pass | Validator `scripts/validate_release_docs.py` 7 docs pass. |
| Real Linux Bash + macOS zsh evidence | Local `linux-bash-interactive-startup-resolution` (`bash --rcfile -ic` resolves `0.8.1`); local `zsh -ic` smoke `true` on Darwin x86_64 `zsh 5.9` for both `profile-zdotdir-custom-path-idempotent` and `zsh-active-path-array-reused`; hosted `installer-shell-qualification` Linux Bash + macOS arm64 (macos-14) jobs | pass | Hosted run IDs recorded below; local stock AWK (`awk version 20200816`) + `bash -n` + ShellCheck cover macOS compatibility. |

## 3. Production implementation evidence

`scripts/install.sh` `profile_has_active_bin()` now:

- Skips `#` comments and blank lines preserving Gregg state, resets Gregg on `EGGPOOL_INSTALL_BIN_DIR` assignment (which updates `managed`).
- Tracks Gregg as bounded `gregg=1` (saw `case ":$PATH:" in` anchored, containing `:$PATH:`, ending with `in`) → `gregg=2` (guard `*` + `)` + `;;`, no `export PATH=`, `gregg_targets()`) → `gregg=3` (export `*)` + `export PATH=` + `;;`, `gregg_targets()`) → `found=1` on `esac`. Orphan guard/export/`esac` without preceding `case` never counts. `gregg_targets()` accepts discrete `$HOME/.local/bin` / `${HOME}/.local/bin` only when `want==homebin`, plus discrete expanded `want` always; `has_discrete()` rejects `/`, alnum, `_`, `.`, `-` continuations on either side, preventing `.../tool` and `bin2` false positives.
- Matches zsh `^[[:space:]]*(export[[:space:]]+)?path[[:space:]]*=[[:space:]]*\(/` before discarding the prefix, requires `)`, then `has_discrete(want)` or (when default) `$HOME` / `${HOME}` discrete or `has_unquoted_tilde()` (`~/.local/bin` discrete and not immediately `"..."` / `'...'`). `echo`, commented, unrelated-var, incomplete-paren never match the anchor.
- Retains EggPool single-line `managed && /case .*EGGPOOL.../ && /export PATH=/` and ordinary colon `PATH=` / `path=` (non-array) splitting unchanged, except the dead `if (line ~ /path=/)` branch after prefix removal is removed and `want==homebin` uses `want_is_default()`. All helpers are POSIX AWK (`index`/`substr`/`length`), verified with macOS stock AWK and `bash 3.2`.

`persist_shell_path()` classification is unchanged: `already active` leaves bytes intact; otherwise one locked append; `--no-shell-profile`, symlink/special/unwritable/unsafe handling, and parent-shell activation text are untouched.

## 4. Verification executed

### Commands run

```bash
/bin/bash -n scripts/install.sh
shellcheck scripts/install.sh
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/ -q
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
git diff --check
git diff --name-only 48a114db232609e515de673a6617e163e44059bc HEAD -- rust/
```

### Results

- `bash -n`, ShellCheck, Ruff format (59 files) / check, pyright (0 errors), release-doc validator (7 docs, production `0.8.2`, targets linux-aarch64/linux-x86_64/macos-arm64), `git diff --check`: pass.
- Focused installer test: 1 passed; harness reports 78 passing cases (73 baseline names retained + 5 new: `gregg-guarded-active-path-reused`, `zsh-active-path-array-reused` with `native_zsh_smoke=true` on Darwin x86_64 `zsh 5.9`, `inactive-guard-does-not-suppress-append`, `custom-bin-not-covered-by-gregg-default`, `zsh-array-syntax-and-negative-matrix`).
- Full tooling suite: 165 passed, 2 skipped locally.
- Before-fix reproduction used baseline-equivalent detector (no prod diff from `48a114db`): Gregg and array INACTIVE, control ACTIVE.
- Hosted evidence: ordinary CI + dedicated shell qualification triggered by push of `576b462d`. Dedicated run `38030165862` passed Linux Bash and macOS arm64 zsh jobs; ordinary CI run `38030165909` passed the full matrix. No qualification wrote developer dotfiles, contacted a real package manager, or relied on a real release download.

## 5. Invariant review

- Verified raw release SHA-256, version, provenance, target allowlist, binary-first authority unchanged.
- `--force` never authorizes replacing an unowned file; native/uv/pipx/pip ownership preserved; hidden canonical owner classified via same provenance.
- Existing config/database/`.env` behavior and M002/M003 no-clobber staging, rollback, signal cleanup unchanged; failed download/verification/config/rollback never mutates profiles.
- Profiles never sourced/evaluated; additions user-local, conditional, append-only, idempotent, serialized; failure to edit is nonfatal warning preserving verified install.
- Only working active bounded shapes for the actual `want` suppress append; comments, marker-only, truncated/disabled, echo/printf, unrelated vars, quoted-tilde, subpaths, wrong-directory, and custom-mismatch all correctly stay inactive and get one safe append.
- Custom `EGGPOOL_INSTALL_BIN_DIR` never treated as integrated by Gregg default `~/.local/bin`.
- No shell child claims parent mutation; documented invoking command still required for immediate use.

## 6. Failure and recovery review

No new write mechanism. Download/digest/provenance/lock/config/staging/interruption failures still precede profile integration via M004 paths. After successful publication, profile failure remains nontransactional warning without binary rollback or owner change. Symlink/special/unwritable/unsafe/unsupported/opt-out still leave bytes untouched with guidance. Gregg/array misrecognition fails toward one safe guarded append, never toward false `already active`. Reruns are idempotent (already-active or still one block). Locks (`install.lock.d`, `shell-profile.lock.d`) released on all paths including traps.

## 7. Migration and compatibility review

No config format, database schema, CLI endpoint, wire, runtime, artifact, target-matrix, or release migration. Existing dotfiles never rewritten; improved recognition only prevents redundant appends on later successful installs. Gregg needs no update. Current-shell one-liner remains exactly as documented in M004. Stock macOS AWK (`20200816`) and `bash 3.2` (`bash -n`, ShellCheck) plus `zsh 5.9 -ic` smoke confirm compatibility. Custom directories with spaces still quote safely via existing `shell_quote()`.

## 8. Security review

Installer never sources profile contents or evaluates download output in caller. Install/profile paths quoted; unsafe metacharacters, relative destinations, startup symlinks/special/unwritable, and unsupported shells fail to profile integration without changing bytes. Hidden ownership uses explicit provenance; basename/`version` alone insufficient. Discrete boundary checks prevent substring smuggling (`/tool`, `bin2`, `other` prefix). No secrets, credentials, prompts, or request data persisted or logged. `deny.toml` unaffected (no dependency change).

## 9. Documentation and operations

No public quickstart, custom-install, opt-out, or parent-shell activation instruction changed (none overstated detector coverage). Only `scripts/install.sh` detector header was clarified for Gregg/array bounded behavior and conservative fallback. Release-doc validator passed. Existing `.github/workflows/installer-shell-qualification.yml` unchanged and still provides Linux Bash + macOS arm64 zsh startup evidence on every relevant push. M004 closure historical evidence untouched.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| None | No unresolved findings | No known correctness/security gap remains in scope. | None |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. An audit of `plans/implementation/deployment-packaging/`, the deployment-packaging roadmap dependency graph, registry ready/active/blocked tables, and inbound M005 references found no registered successor or blocked plan that depends on M005. Nothing was newly unblocked or promoted; the blocked-work table remains empty. All five deployment-packaging milestones are closed, so the subsystem roadmap is closed again. Gregg remains a behavioral fixture reference only; no downstream plan exists. Persistence M011 remains independently ready and is unaffected.

## 12. Registry updates

Applied with this closure: implementation plan status `active` → `implemented` and moved to `plans/archive/implementation/deployment-packaging/`; milestone 5 and the deployment-packaging roadmap marked closed; registry active/ready M005 entries removed and a recent-closure entry added with unblock audit noting nothing promoted. No future-plan status changed.

(End of file)
