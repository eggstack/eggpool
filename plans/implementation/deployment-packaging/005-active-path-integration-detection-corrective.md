# Deployment and Packaging Milestone 005 — Active PATH integration detection corrective

Status: active

Repository baseline: `48a114db232609e515de673a6617e163e44059bc` (`main`, 2026-10-10)

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-5--active-path-integration-detection-corrective`

Corrects:

- `plans/archive/implementation/deployment-packaging/004-shell-command-discovery-and-activation-corrective.md`
- `plans/closure/deployment-packaging/004-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADR: `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` (accepted, unchanged).

Primary class: **polish** (correct, nonduplicating shell-profile recognition); preserved installation and profile safety are invariants.

## 1. Objective

Correct two confirmed false negatives in `scripts/install.sh` `profile_has_active_bin()`: a functioning **Gregg-managed guarded PATH block** and a functioning **zsh tied `path=(...)` array assignment** are not recognized. When either exposes the actual verified EggPool binary directory, the installer should report `Future-shell profile: already active` and leave the startup file **byte-for-byte unchanged**.

Keep M004's working binary-first installer, canonical off-PATH ownership discovery, safe profile append, and parent-shell immediate activation unchanged. This is a new narrow post-closure corrective, not retroactive reopening of M004.

## 2. Why ready

Deployment/Packaging M001–M004 are closed; M004 closure at `plans/closure/deployment-packaging/004-status.md` records 73 deterministic installer cases, full CI `38026958264`, and Linux Bash / macOS arm64 zsh shell run `38026958268`. No open hard dependency or external interface decision exists. Gregg Plans 130–131 already implement the guarded integration shape and serve only as a fixture reference; no Gregg code change is required.

## 3. Baseline evidence and missed coverage

`profile_has_active_bin()` in `scripts/install.sh` currently enters the normal PATH detector only for lines **anchored at the start** as `PATH=`, `export PATH=`, `path=`, or `export path=`. Gregg's canonical block instead places `*) export PATH="$HOME/.local/bin:$PATH" ;;` inside a `case ":$PATH:" in` arm. The current predicate therefore misses it.

The zsh array recognizer tests for `path=` **after** `sub(/^[^=]*=/, "", line)` has removed the assignment name. Consequently normal active `path=("$HOME/.local/bin" $path)` is also missed.

The production AWK logic was reproduced with a Gregg guarded block and a zsh array fixture: both were **not recognized**, while a simple `export PATH="$HOME/.local/bin:$PATH"` control was recognized. M004's `scripts/qualify_quick_installer.py` covers the control, EggPool block rerun, `ZDOTDIR`, and new-shell startup but does **not** cover Gregg's preexisting guarded `case` block or preexisting zsh `path` array. That omitted test matrix explains the gap.

Consequence: unnecessary append-only profile mutation, not evidence of failed executable installation. Retain the M004 closure and require targeted red/green evidence.

## 4. Invariants not to regress

- Read profiles as inert text: **never** source, eval, execute, or command-substitute user contents.
- Recognize only a *working, active, bounded* PATH-integration shape and the **actual** installed directory; comments, marker alone, truncated/disabled code, echo/printf, unrelated variables, partial subpaths, and arbitrary strings cannot suppress an append.
- Existing profile bytes are unchanged when `already active` is reported. Required new blocks retain the M004 append-only, serialized, idempotent, guarded behavior and `--no-shell-profile`.
- Preserve all owner/provenance/target/collision/`--force` checks, checksum verification, manager dispatch, config no-clobber, rollback, signal safety, and success-only profile writes.
- Preserve default and customized installation directory support; custom EggPool bin directory must **not** be treated as integrated merely because Gregg integrated `~/.local/bin`.
- No Rust/runtime, HTTP, protocol, SQLite/schema, release artifact, target-matrix, or native immediate parent-shell activation changes.

## 5. Scope

**In:** `profile_has_active_bin()` or closely bounded read-only helpers in `scripts/install.sh`; disposable regression fixtures in `scripts/qualify_quick_installer.py` (and existing `tests/tooling/test_installer.py` wrapper as needed); minimal code/docs comments and planning/closure.

**Out:** general-purpose Bash/zsh AST/parser, following sourced dotfiles, arbitrary conditional-execution analysis, dynamic evaluation, new shells/platforms, profile selection rewrites, generic installer cleanup, new CI workflows or dependencies, changing Gregg, and modifications to native executable/manager ownership.

## 6. Required production changes

### A. Gregg guarded PATH block

Recognize the exact supported functioning Gregg integration shape, such as:

```sh
# added by gregg installer: ensure user-local binaries are on PATH
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
```

It is an active block despite the interior indented `export`, but comments/marker alone, a disabled export, an incomplete `case`, or an arbitrary textual occurrence must **not** count. A conservative bounded recognizer of a complete expected shape is preferable to a generic substring match. Verify the block targets the requested `want`: default Gregg integration does not integrate a different `EGGPOOL_INSTALL_BIN_DIR`.

Maintain ordinary user-authored `PATH=` / `export PATH=` recognition and intact EggPool-owned managed-block recognition. Do not create false positives from arbitrary dormant shell code.

### B. Zsh tied-array assignment

Detect zsh `path=(...)` assignments **before discarding the assignment prefix**, or retain original syntax separately. Cover supported simple `path=("$HOME/.local/bin" $path)`, `path=(${HOME}/.local/bin $path)`, unquoted `~/.local/bin`, and an absolute custom directory as a **discrete** array entry. Do not mistake a quoted literal `"~/.local/bin"` (tilde expansion does not occur there), `$HOME/.local/bin/tool`, `echo path=...`, or commented-out assignments for effective PATH modification. Avoid a general zsh parser; fail conservative on complex metaprogramming. Confirm compatibility with stock macOS AWK and zsh.

### C. Truthful classification

If a supported preexisting block is recognized, report `already active` and do not append any EggPool marker. If not recognized, append one conditional EggPool-managed block by M004's existing success-only locked mechanism and remain idempotent on rerun. Neither inherited installer PATH nor one unrelated startup file is proof of persistence.

## 7. Ordered work packages

**A — Red/green reproductions:** Add disposable `HOME` and fake release fixtures for Gregg's exact guarded case and zsh array, run against baseline `48a114db` and demonstrate the new assertions fail before the detector is changed. Keep simple-export control green.

**B — Minimal recognizer correction:** Update only read-only static detector. Confirm the existing EggPool block, Bash PATH cases, functional Gregg case, and zsh tied array all classify correctly. Test with macOS stock AWK; zsh runtime startup smoke uses `zsh -ic` and an already integrated profile, where available.

**C — Negative matrix:** Exercise Gregg marker/comment-only, incomplete/disabled guard, unrelated `echo`/`printf`/function text, zsh commented/quoted-tilde/path-superstring, wrong directory and custom target, multiple zsh array elements, and previously EggPool-managed profile. Verify byte-identical profiles when recognized and exactly one append when absent after rerun.

**D — Qualification and closure:** Run installer and tooling gates, the existing shell qualification workflow, document real CI evidence and residual risks in `plans/closure/deployment-packaging/005-status.md`, then reconcile roadmap/registry and blocked dependencies. Do not mark closed merely because the code was committed.

## 8. Failure, cancellation, restart, contention

No new write mechanism. Existing M004 checks reject symlink/special/unwritable profile targets and serialize safe appends. Failed release fetch, digest/provenance, binary transaction/config publish, update/rollback, or interruption must not modify user profiles. `--no-shell-profile` skips integration. An already working profile is left intact, even during repeated installs; uncertain/unrecognized syntax may produce one safe guarded append rather than false `already active`. Installer stdout must reflect the actual result.

## 9. Compatibility and migration

No storage, config, service, schema, protocol, package-manager, CLI, or release migration. Existing dotfiles are not rewritten. Improved recognition only prevents redundant additions on later successful installs. Gregg needs no update. Current-shell one-liner remains exactly as documented in M004.

## 10. Required tests

Extend `scripts/qualify_quick_installer.py` and the existing installer wrapper. At minimum:

- `gregg-guarded-active-path-reused`: exact active Gregg block, default installed path, profile bytes and hash unchanged, no EggPool-managed marker, output `already active`.
- `zsh-active-path-array-reused`: functioning tied-array assignment with `ZDOTDIR`; unchanged profile, successful disposable `zsh -ic` resolution, actual `eggpool version` output.
- `inactive-guard-does-not-suppress-append`: disabled/missing guard or comment/marker-only; required append occurs exactly once with operator content unchanged.
- `custom-bin-not-covered-by-gregg-default`: active Gregg `~/.local/bin` entry with a different verified EggPool custom directory does not suppress needed integration.
- Valid array syntaxes and negative quoted tilde, commented array, echo, unrelated variable, and subpath forms; no false positive.
- Intact EggPool-managed block, ordinary PATH export, hidden native/manager ownership and profile owner selection, unowned collision/`--force` refusal, failed transaction/no-profile-mutation, opt-out, unsafe profile and parent-shell activation: existing M004 coverage remains green.
- Before-fix reproduction failing under `48a114db`, followed by passing patched behavior. Tests must not modify real dotfiles, call a live package manager, or download actual release assets.

## 11. Required verification commands

```bash
bash -n scripts/install.sh
shellcheck scripts/install.sh
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/ -q
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
git diff --check
```

Use existing ordinary CI and `.github/workflows/installer-shell-qualification.yml` for real Linux Bash and macOS arm64 zsh evidence; do not add workflow/matrix. Record what ran rather than inferring green from documentation. This shell-only plan must have **zero Rust diff**; unexpected Rust/owner changes require scope review and complete default/no-default serial Rust gates.

## 12. Documentation updates

Only correct relevant source comments/installer docs that overstate active-profile recognition. Preserve public quickstart, custom-install invocation, opt-out, and parent-shell activation instructions. Add the M005 roadmap and registry links, but do **not** rewrite M004's immutable closure evidence. If appropriate, append an accurately dated and scoped post-closure note referencing M005, never altering M004 implementation SHA or historical test counts.

## 13. Acceptance criteria

1. An intact functioning Gregg guard and an active zsh tied-array assignment exposing the installed directory both cause `already active` with byte-identical startup file.
2. Commented, inactive, malformed, unrelated, quoted-tilde, substring, and wrong-directory fixtures cannot claim active integration.
3. Existing recognized user/EggPool integration stays idempotent; unsafe cases still fail safely.
4. The original 73-case installer harness passes unchanged alongside the added fixtures, with real native shell CI evidence.
5. Parent-shell activation, binary and manager ownership, checksum checks, rollback, config staging, API and release contracts stay unchanged.

## 14. Stop conditions

Do not broaden to shell interpretation/eval, generic parser, changes to Gregg, root/system profile edits, runtime modifications, new CI machinery, or weakening owner/security guards. Report structurally complex unrecognized profile syntax as a bounded conservative limitation.

## 15. Closure evidence required

The `plans/closure/deployment-packaging/005-status.md` record must name implementation SHA(s), baseline failures, fixture classification/byte-preservation matrix, existing 73-case qualification plus new cases, CI/Linux/macOS results, security/compatibility review, zero-out-of-scope-diff statement, residual finding severity, and final disposition. Audit ready and blocked plans on closure; only then mark M005 and the deployment roadmap closed again.

## 16. Handoff notes

Primary owner is `scripts/install.sh` `profile_has_active_bin()` plus `scripts/qualify_quick_installer.py`. The two known source errors are **anchored top-level assignment matching** (Gregg guard export is indented inside a case arm) and **looking for path= after removing the path= prefix** (zsh array). Treat Gregg as a fixture, not as work to execute in another repo. The M004 install, update, parent-shell activation, and safe append work is qualified and must remain untouched.
