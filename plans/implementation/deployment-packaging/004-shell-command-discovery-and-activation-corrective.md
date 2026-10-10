# Deployment and Packaging Milestone 004 — Shell command discovery and activation corrective

Status: ready

Repository baseline: `963a1a706877728117a7abf1f0355fc017082551` (`main`, 2026-10-10)

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-4--shell-command-discovery-and-activation-corrective`

Corrective lineage:

- `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md` / `plans/closure/deployment-packaging/001-status.md`
- `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md` / `plans/closure/deployment-packaging/002-status.md`
- `plans/implementation/deployment-packaging/003-config-publication-ownership-corrective.md` / `plans/closure/deployment-packaging/003-status.md`

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADR: `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` (accepted; authority unchanged).

Primary class: **capability** (discoverable installed CLI), with invariant protection for proven ownership/collisions.

## 1. Objective

Make the supported non-root binary-first quick install leave an executable named `eggpool` discoverable in **subsequently started** supported user shells without manual PATH configuration, and make the **documented single-line invocation** activate it in the **already-running invoking shell** after success. Repair recognition of existing canonical EggPool binaries hidden by PATH without weakening provenance, overwrite, rollback, or manager-ownership safeguards.

The canonical installed name and path remain `eggpool` at `EGGPOOL_INSTALL_BIN_DIR` or default `$HOME/.local/bin/eggpool`. This plan is an installer/documentation/qualification corrective, not a Rust CLI feature.

## 2. Readiness and evidence

Hard dependencies M001/M002/M003 are closed. No new external interface/ADR is required; Gregg Plans 130/131 already demonstrate a workable bounded shell-profile policy in `eggstack/gregg/packaging/install.sh` and `scripts/tests/test-install-rerun.sh`. That implementation is a behavioral reference, not a shared-code dependency.

At baseline, `scripts/install.sh` sets `INSTALL_DEST_DIR="${EGGPOOL_INSTALL_BIN_DIR:-$HOME/.local/bin}"` and installs `$INSTALL_DEST_DIR/eggpool`, then executes `export PATH="$INSTALL_DEST_DIR:$PATH"` inside the piped installer process to pass `command -v eggpool`. Neither the caller's PATH nor its shell startup file changes. `print_binary_next_steps` prints `eggpool onboard` without explaining that the command may not be accessible. `EXISTING_BIN` is discovered only with `command -v eggpool`; an invisible but valid canonical binary is classified as absent, after which destination-collision checks block rerun (including `--force`).

The existing qualification (`scripts/qualify_quick_installer.py`; `tests/tooling/test_installer.py`) validates artifact/ownership/config transitions inside subprocesses, but does not prove future-shell startup or invoking-parent-shell activation. This is why the M001–M003 qualification missed the observable issue, not a reason to reopen their valid security closure.

## 3. Invariants not to regress

- Verified native GitHub binary-first authority; supported Linux x86_64/aarch64 and macOS arm64 target set; no Python/uv/pipx/Cargo required for native fresh installs.
- Verify SHA-256, version, and `install-provenance --shell` identity before publication; retain no-clobber config staging and guarded executable rollback.
- An unrelated binary, symlink, special destination, unknown owner, source checkout, conflicting PATH command, or ambiguous manager remains **fail-closed**; `--force` never authorizes an unowned overwrite.
- Existing standalone, uv, pipx, pip/venv ownership and native updater delegation are preserved. No conversion between ownership classes solely to repair shell discoverability.
- Shell-profile edits never source/evaluate user files, run arbitrary shell code from them, silently invoke sudo, touch system/global startup files, or delete pre-existing entries.
- Do not mutate config/database/.env/service state for PATH repair; native runtime/HTTP/provider APIs and release artifacts are unchanged.

## 4. Scope and exclusions

In scope: `scripts/install.sh` discovery/verification, post-success shell-profile integration, a non-mutating reporting/opt-out contract, documented one-line activation, deterministic qualification, and relevant installation documentation.

Out of scope: implicit privilege escalation, installing to `/usr/local/bin` on behalf of a user, a running parent-shell mutation from inside a subprocess, universal interactive shell support (fish/nu/PowerShell), sourcing downloaded installers via `eval` or `source`, general-purpose shell parsing, release target changes, package ownership rewrites, runtime changes, or porting Gregg's installer wholesale.

## 5. Required production changes

### A — Separate installed-file identity from command resolution

Inventory `scripts/install.sh` order of `EXISTING_BIN`, `INSTALL_DEST`, manager detection, and owner dispatch. Add a **canonical-destination candidate** probe when `command -v eggpool` is absent; accept it only after the same verifiable native/manager provenance checks used for visible commands. Distinguish source-checkout/manager-owned shims and ensure an existing **regular** file is not assumed to be EggPool because of its basename or output from `version` alone. Resolve and compare paths conservatively, without following untrusted symlink destinations.

Carry the proven absolute executable path through existing-native update/repair or existing-manager operations; do not prepend the canonical directory to PATH merely to decide which owner wins. Preserve the precedence and fail-closed behavior when both a PATH-visible command and a different canonical destination exist. Distinguish a canonical existing install from an unowned occupied file in the diagnostic.

Tests must demonstrate a hidden native binary is correctly identified and treated as an existing owner, not as a fresh collision; a hidden foreign binary still blocks both ordinary reinstall and `--force` without byte changes. A hidden package-manager-owned shim must not be silently reclassified as standalone.

### B — Persistent user-shell discovery, after successful install

Port/adapt the **semantics** of Gregg Plan 130/131 to an EggPool-owned guarded PATH integration helper, rather than creating a generic shared library. Detect the **user's intended shell** (e.g. `SHELL`), not the `bash` interpreter executing a pipe. Support zsh using safe `${ZDOTDIR:-$HOME}/.zshrc` (including normal macOS zsh) and Bash using `~/.bashrc` on Linux and an appropriate existing macOS login startup file (`.bash_profile`, `.bash_login`, `.profile`) or a deliberate new Bash profile. Prefer correct future-shell behavior; exercise the real startup class in tests.

Honor a validated canonical installation directory (`EGGPOOL_INSTALL_BIN_DIR`) and its path spelling. A non-default directory needs a safely quoted integration or an explicit bounded fallback; do not falsely write a `~/.local/bin` entry that does not expose the installed binary. Reject unsafe newline/control metacharacters, untrusted symlink/special targets, unwritable files, and unexpected profile locations without evaluating profile contents. Respect an explicit `--no-shell-profile` opt-out. Unsupported shells print truthful manual guidance, not a false claim of success.

Provide a managed, conditional, **append-only**, idempotent PATH block. Recognize intact EggPool/Gregg-managed entries and ordinary active user-authored PATH/zsh `path` assignments to avoid duplication. Commented-out assignments, mere mentions, aliases, `echo`, partial subpaths, and a bare marker without a functional assignment are **not** proof of activation. Do not infer persistent startup configuration solely from the already-inherited PATH. Never source a potentially untrusted startup file to inspect it. Avoid following profile symlinks or overwriting unrelated profile content. Subsequent reruns must not accumulate a second block.

Run the profile step **only after binary and config transaction success**, including package-managed fresh installs and owner-preserving update/repair where discoverability needs repair. A profile edit failure is a **non-transactional user-environment warning**: do not roll back an already verified install, change manager ownership, or claim that future sessions are configured. Avoid creating profile changes for failed download, digest mismatch, version/provenance mismatch, lock refusal, config publish failure, or rollback. Recheck the canonical binary independently of in-process PATH modifications.

### C — Immediate current-shell activation without a separate manual step

A piped `curl | bash` child **cannot** export a variable into its invoking Bash/zsh parent, cannot change parent aliases/functions, and cannot refresh its command cache. The installer must not claim otherwise or attempt ineffective tricks.

Change the primary macOS/Linux public install snippet in `README.md` and installation docs to **one copy/paste command** that runs the verified installer, then performs a **parent-shell** `export PATH=...` only on successful install. The user should be able to invoke `eggpool` immediately after that command in the **same** interactive Bash/zsh session, with no separate `export_path` or second interactive step. Gregg's README one-liner (`curl ... | bash -s -- gregg && export PATH="$HOME/.local/bin:$PATH"`) is established precedent. Prefer pipeline failure propagation (`pipefail`, or an equivalent safe wrapper) so a curl failure cannot report success and trigger activation. Keep the downloadable installer itself directly usable via the existing `curl ... | bash` form: it can persist future-shell config and print a bounded activation command but cannot guarantee immediate parent-shell availability when invoked alone.

The one-liner must support the default destination and document a safe equivalent for explicitly overridden destinations; do not use `eval "$(curl ...)"`, `source <(curl ...)`, shell injection via unquoted installer/user paths, or hidden execution of remote output in the current shell. Do not assume users must run a shell restart. Installation output must distinguish: installed executable path; current process PATH; **future-shell profile persisted/unchanged/unavailable**; and the parent-shell activation requirement for the bare pipe form.

### D — Regression harness and static guards

Extend `scripts/qualify_quick_installer.py` and `tests/tooling/test_installer.py` with temp HOME, temp XDG paths, safe fake binaries/releases, injectable `SHELL`/`ZDOTDIR`/PATH, and disposable Bash/zsh launchers. Do not write the developer's actual dotfiles or invoke real network/package managers. Use a disposable invoking-shell wrapper to prove that the documented one-liner changes the **parent** PATH and that later `command -v eggpool` and `eggpool version` work. Prove bare `curl | bash` only persists the next-session profile, not immediate parent activation; assert no misleading output.

Required scenarios: macOS zsh (including ZDOTDIR), Linux Bash and macOS login Bash, missing/active/comment-only profile entries, idempotent repeat, pre-existing Gregg integration, custom bin directory, unsafe profile symlink/special/unwritable cases, unsupported shell and opt-out, hidden existing standalone and manager owners, hidden foreign destination `--force` refusal, PATH-visible conflicting executable, interrupted/failed install without profile mutation, and successful native update with PATH discovery repair. Include hostile home/path characters without evaluating profile text. Keep prior M001–M003 checks, including 60-case baseline invariants, green.

Any OS emulation in the Python harness is **not** itself evidence that the real macOS startup selection works. Run native macOS Apple Silicon zsh smoke (or existing native macOS CI equivalent) and Linux interactive Bash smoke for release-readiness; label absent physical evidence operationally, never imply it was executed.

## 6. Failure, cancellation, restart and contention semantics

A failure before successful binary/config publication must leave startup files unchanged. Successful publication followed by profile-edit failure remains an installed, verified binary and returns truthful follow-up guidance; do not delete operator state or return a misleading "nothing installed" error. Lock ordering remains bounded and does not introduce blocking on other shell sessions. Multiple installer invocations or repeated profile edits must not corrupt the profile or append duplicate blocks; review append races explicitly and use bounded coordination where needed, avoiding arbitrary-shell execution. Startup-file symlinks/special files stay untouched. Native update failure retains previous owner/path and never writes a new profile entry as a success marker.

Immediate activation occurs only if the documented invocation returns success in the invoking shell; failed fetch/install cannot yield a success message or a newly activated path. Document that an already-running shell cannot be changed retroactively by a completed bare-pipe install.

## 7. Compatibility and migration

No storage, SQLite schema, TOML, HTTP, JSON, native CLI flags, artifacts, or service migrations. The existing standalone/uv/pipx/pip update contract survives. User shell startup files receive at most a bounded, marked user-local integration after success; preserve any existing Gregg-provided `~/.local/bin` integration. No removal of generic shared bin-path integration on uninstall. Provide a safe opt-out and avoid touching system/global profiles.

## 8. Ordered implementation packages

1. **Evidence/guard baseline:** reproduce invisible fresh install and second-run hidden-owner failure with a disposable macOS-zsh and Linux-Bash environment; record M001–M003 coverage gap.
2. **Owner-provenance correction:** resolve verified invisible canonical owners and retain all collision/force safeguards.
3. **Shell persistence:** bounded shell selection, safe profile integration, idempotency, diagnostics, opt-out, and custom-location policy.
4. **Invoking-shell activation and docs:** make the preferred copy/paste command self-activating through parent-shell syntax, with pipeline failure handling.
5. **Qualification/closure:** matrix, native shell smokes, docs validation, closure record and registry reconciliation.

## 9. Required verification

Focused, from repository root:

```bash
bash -n scripts/install.sh
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run python scripts/validate_release_docs.py
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
```

Run ShellCheck on the changed installer (if available) and all relevant `tests/tooling` installer/release/docs checks. Run deterministic parent-shell Bash and macOS zsh smoke using disposable profiles. If Rust operations code changes unexpectedly become necessary, stop for scope review first; any authorized Rust changes require focused provenance/update tests plus `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`, strict workspace Clippy, and full serial workspace/no-default-features tests per development skill. Record exact commands, outcomes, and CI provenance; do not label unrun gates green.

## 10. Documentation updates

`README.md` public quickstart; `docs/rust-release-deployment.md`; `docs/upgrading.md` where installer update/owner behavior is described; any dedicated installer documentation; `scripts/install.sh --help`. Amend `plans/subsystems/deployment-packaging-roadmap.md` and `plans/registry.md` at closure. Do not rewrite historical M001–M003 closure evidence.

## 11. Acceptance criteria

- A fresh supported macOS arm64 zsh install makes `~/.local/bin/eggpool` discoverable in new shells automatically.
- The **documented one-line command** makes `eggpool` callable immediately in the *same* invoking zsh/Bash shell without a subsequent manual export or reload.
- Repeating installation/repair does not duplicate PATH entries or treat a hidden installed EggPool as a foreign collision; hidden foreign files remain safe from `--force`.
- Existing Gregg or user PATH entries are reused without redundant profile mutation; customized installation locations are accurate; unknown/unsafe shell profile situations yield explicit nonfatal diagnostics.
- Broken download/self-check/config staging leaves startup files unchanged, all prior binary/config/owner rollback protections pass, and manager ownership never changes because of PATH repair.
- Native macOS zsh and Linux Bash evidence plus deterministic harness results are recorded, along with docs and regression guards.

## 12. Stop conditions

Stop instead of improvising if resolution requires a new root/system install authority, unverified manager ownership, shell startup execution, dynamically evaluated downloaded content, a change to accepted ADR-0001, relaxation of M002/M003 rollback protections, or a new runtime/API contract. Report a macOS native-evidence gap rather than claiming complete cross-platform functionality.

## 13. Closure evidence required

In `plans/closure/deployment-packaging/004-status.md` record implementation SHA(s); the reproduced pre-fix symptoms; before/after invoking-shell and future-shell command resolution; hidden-native/manager/foreign collision matrix; safe/idempotent profile mutation and failure matrices; default/custom bin cases; M001–M003 installer suite results; precise real macOS zsh + Linux Bash evidence (or blockers); docs/CI checks; confirmation of zero Rust/runtime/protocol/storage changes (or explicitly authorized deviation); severity-labelled residual risks; registry/roadmap disposition; and a closed/conditionally-closed/corrective-required/blocked verdict.

## 14. Handoff

Installers and tests are the owned surface. Implement relative to current `main` and preserve unrelated changes. Source reference: Gregg `packaging/install.sh` `run_path_integration_once` and Plans 130/131; Gregg already has a documented current-shell activation one-liner and needs no parallel plan unless a distinct defect is subsequently demonstrated. Do not copy its hardcoded `~/.local/bin` policy into EggPool's configurable destination without adjustment.

A one-line installer cannot mutate the parent process; immediate activation belongs to the command users paste, future-shell activation to the safe profile integration. Both must be tested as distinct contracts.
