# Deployment and Packaging Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/archive/implementation/deployment-packaging/004-shell-command-discovery-and-activation-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-4--shell-command-discovery-and-activation-corrective`

Repository baseline reviewed: `963a1a706877728117a7abf1f0355fc017082551`

Implementation commits or pull requests:

- `57af5bc` — verified canonical owner discovery, safe shell-profile persistence, parent-shell activation docs and workflow.
- `45149d2` — explicit special/unwritable profile, failed-download, and interruption regression coverage.
- `6857ba6` — moved M004 to closing while hosted shell evidence ran.

## 1. Executive finding

The installer capability is complete and verified. Fresh installs, owner-preserving updates, and explicit package-manager installs now persist the actual executable directory to a supported user shell only after the executable and config transaction succeeds. A documented Bash/zsh one-liner activates PATH in its invoking shell after success; the bare pipe is described accurately. Hidden canonical owners are classified through the same provenance checks as PATH-visible commands, while unowned and conflicting files remain fail-closed. The 60-case M001–M003 regression baseline remains green; the harness now runs 73 cases. Disposition: `closed`, with no unresolved correctness or security findings.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Reproduce the old hidden-owner and immediate-discovery symptoms | Baseline `963a1a7` installer was run with the disposable qualification harness. Fresh install left the binary at `~/.local/bin/eggpool`, but the invoking shell's `command -v eggpool` failed and no `.bashrc` was created. A hidden native owner failed as “already exists and is not a verified EggPool installation.” | pass | Both observations reproduced before the fix. |
| Recognize a PATH-hidden native owner and preserve its owner | `hidden-canonical-native-owner`; existing native updater exercised at the canonical file. | pass | The subsequent profile repair uses the installed executable's directory. |
| Keep a hidden package-manager shim manager-owned | `hidden-canonical-manager-owner`. | pass | Verified provenance selects `uv-tool`; it is never relabelled standalone. |
| Refuse hidden foreign files, including with `--force`, without changing bytes | `hidden-foreign-force-refusal-preserves-bytes` covers ordinary reinstall and `--force`; `path-visible-command-canonical-conflict-refusal` covers a different PATH command and canonical file. | pass | Exact pre/post bytes are asserted. |
| Persist future zsh and Bash PATH safely and idempotently | `profile-zdotdir-custom-path-idempotent`, `linux-bash-interactive-startup-resolution`, `macos-login-bash-profile-selection`, `active-user-path-reused-comment-only-ignored`, `package-manager-profile-uses-verified-owner-bin`. | pass | Includes ZDOTDIR, a custom path with spaces, active Gregg-style/user assignments, comment-only entries, and manager-specific bin locations. |
| Preserve profile safety and opt-out behavior | `profile-symlink-refusal-and-optout`; FIFO and read-only file cases in the same harness case; `unsupported-shell-and-unsafe-bin-fallback`. | pass | Existing startup files remain byte-identical; no profile is sourced. |
| Leave profiles unchanged on install failure or interruption | `truncated-download`, `fresh-signal-cleanup-removes-staging`, `fresh-init-config-failure-rolls-back-binary`, and `failed-config-transaction-does-not-edit-profile`. | pass | Download, config publication, and synchronized signal failure all precede profile mutation. |
| Activate the invoking shell only after successful installation | `documented-parent-shell-activation` runs the documented pipeline in a disposable Bash parent and then verifies `command -v eggpool` and `eggpool version`. | pass | The child installer prints a bounded export command but does not attempt to mutate its parent. |
| Retain all M001–M003 transaction and ownership guarantees | `uv run pytest tests/tooling/test_installer.py`; 73-case harness retains all prior 60 names and M001–M003 assertions. | pass | Includes checksum, provenance, manager ownership, no-clobber config, rollback, collision, and signal regressions. |
| Update public install and upgrade guidance | `README.md`, `docs/deployment.md`, `docs/raspberry-pi.md`, `docs/rust-release-deployment.md`, `docs/upgrading.md`, and `scripts/install.sh --help`. | pass | Default and custom-directory commands use `pipefail` and parent-shell export syntax. |
| Run real Linux Bash and native Apple Silicon macOS zsh qualification | Hosted [installer shell qualification run 38026958268](https://github.com/eggstack/eggpool/actions/runs/38026958268) passed Linux Bash and macOS zsh jobs at `45149d2`. The macOS job uses `macos-14`, which GitHub's [runner image matrix](https://github.com/actions/runner-images/blob/main/README.md) identifies as arm64. | pass | Local disposable zsh smoke also passed on macOS x86_64; hosted arm64 run supplies target-class evidence. |
| Keep runtime, protocol, storage, release artifacts, and supported targets unchanged | Reviewed commit diff and `git diff --name-only`; changes are installer, tooling tests, docs, workflow, and planning only. | pass | Zero Rust, artifact, protocol, schema, or runtime changes. |

## 3. Production implementation evidence

`scripts/install.sh` probes a canonical regular-file candidate only when
`command -v eggpool` is absent, then demands verifiable native or package
provenance. A conflicting PATH-visible command and canonical destination fail
closed. Fresh raw installation verifies and executes the committed absolute
path directly; it no longer mutates the installer's private PATH to simulate a
parent-shell change.

After successful binary/config publication, the installer selects the
intended shell from `SHELL`: zsh uses `${ZDOTDIR:-$HOME}/.zshrc`; Linux Bash
uses `.bashrc`; macOS Bash reuses `.bash_profile`, `.bash_login`, or `.profile`
and otherwise creates `.bash_profile`. It inspects startup text without
sourcing it, recognizes functional active assignments and intact EggPool
blocks, serializes appends with a bounded private lock, and refuses symlink,
special, unwritable, unsafe, or unsupported targets with nonfatal guidance.
`--no-shell-profile` is supported. The command users paste wraps the pipe with
`bash -o pipefail -c` and exports the correct directory in the invoking shell
only after success. Package-manager paths are taken from the verified owner
directory, not assumed to be `~/.local/bin`.

Added `.github/workflows/installer-shell-qualification.yml` to run disposable
installer and real shell-startup tests on Linux and macOS arm64. Rust runtime,
storage, and protocol code were not changed.

## 4. Verification executed

### Commands run

```bash
/bin/bash -n scripts/install.sh
shellcheck scripts/install.sh
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q --tb=short --maxfail=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/installer-shell-qualification.yml")'
git diff --check
```

### Results

- `bash -n`, ShellCheck, Ruff format/check, pyright, release-doc validation,
  release-catalog validation, release-workflow validation, new workflow YAML
  parse, and `git diff --check`: pass.
- Focused installer test: 1 passed; its qualification harness reports 73
  passing cases.
- Full tooling suite: 165 passed, 2 skipped locally before the last profile
  guard additions; the final-hash hosted CI run `38026958264` then passed the
  complete Rust/tooling matrix. The dedicated final-hash native shell run
  `38026958268` passed both Linux Bash and macOS arm64 zsh jobs.
- Before-fix reproduction used the baseline installer from `963a1a7` with the
  disposable harness. It reported the canonical native binary as an unverified
  collision; a fresh binary existed after install, while the parent shell
  could not resolve it and no future-shell profile was configured.
- Hosted evidence is separate from local emulation. No qualification wrote
  developer dotfiles, contacted a real package manager, or relied on a real
  release download.

## 5. Invariant review

- Verified raw release SHA-256, version, provenance, target allowlist, and
  binary-first authority are unchanged.
- `--force` never authorizes replacing an unowned file; native, uv, pipx, and
  pip ownership is preserved.
- Existing config/database/`.env` behavior and M002/M003 no-clobber staging,
  rollback, and signal cleanup are unchanged.
- Startup profiles are never evaluated; additions are user-local,
  conditional, append-only, and idempotent. Failure to edit a profile leaves
  the already verified install committed and reports the actual state.
- No shell child claims to mutate its parent. Only the documented invoking
  command applies the post-success export.

## 6. Failure and recovery review

Download, digest, provenance, lock, config generation/publication, and
interruption failures occur before shell-profile integration. Existing
transaction rollback remains authoritative. After successful publication, a
profile failure is a nontransactional warning and never rolls back the binary,
changes manager ownership, or reports a false configured state. Symlinks,
special files, and unwritable profiles remain untouched. Repeated appends are
serialized; an occupied profile-edit lock reports a retryable warning.

## 7. Migration and compatibility review

No config format, database schema, CLI endpoint, wire, runtime, or artifact
migration. Existing standalone and package-manager owners are retained.
Supported shell profiles gain at most one marked conditional user-local PATH
block. Existing working user or Gregg PATH assignments are reused. The
documented default command and custom-directory equivalent activate only
after a successful installer exit.

## 8. Security review

The installer never sources profile contents or evaluates downloaded output in
the caller. Install and profile paths are quoted; unsafe path metacharacters,
relative destinations, startup symlinks, special files, and unwritable targets
fail to profile integration without changing profile bytes. Hidden ownership
uses explicit provenance; basename or `version` output alone is insufficient.
No secrets, credentials, prompts, or request data are persisted or logged.

## 9. Documentation and operations

README quick start, deployment, Raspberry Pi, Rust release deployment,
upgrading, and installer help now distinguish installed path, current
installer PATH, future-shell status, and parent-shell activation. The release
documentation validator checked seven documents and passed. The new workflow
keeps real Linux Bash and macOS arm64 zsh startup behavior in release-readiness
CI.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| None | No unresolved findings | No known correctness/security gap remains in scope. | None |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. An audit of
`plans/implementation/deployment-packaging/`, the deployment-packaging
roadmap, registry ready/active/blocked tables, and inbound M004 references
found no registered successor or blocked plan that depends on M004. Nothing
was newly unblocked or promoted; the blocked-work table remains empty. All
four deployment-packaging milestones are closed, so the subsystem roadmap is
closed. Gregg remains a behavioral reference only; no downstream plan exists.

## 12. Registry updates

Applied with this closure: implementation plan status `implemented` and moved
to `plans/archive/implementation/deployment-packaging/`; milestone 4 and the
deployment-packaging roadmap marked closed; registry active/ready entries
removed and a recent-closure entry added. No future-plan status changed.
