# O009 — Deployment, Install Artifacts, and Uninstall

Status: closed; implementation and closure recorded in `migration-rs/closure/operations/009-status.md`

Implementation commit: `27310ffdd96e1d9aa94887edd312a932be0ad091a`

Closure: [O009 status](../../closure/operations/009-status.md)

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted O008.

## Objective

Implement the Rust operational deployment/removal surface after command behavior is complete: `deploy systemd`, `deploy cron`, `deploy backup-cron`, `deploy logrotate`, `deploy all`, and `uninstall`. Prepare Rust-capable local install/deployment artifacts without performing M11's public Rust-default cutover.

## Boundary

M9 owns service snippets, local installation/removal behavior, path/user/permission safety, and deterministic command execution. M10 owns broad platform/SBC characterization. M11 owns making Rust artifacts the canonical public install/release path and updating the primary quick-start installer accordingly.

Do not turn M9 into a packaging/release-matrix project.

## Part A — deployment model

Port the current personal and production deployment layouts from Python constants/helpers into small Rust renderers.

Freeze/implement:

- personal systemd unit using resolved binary/config/data/env/user/group paths;
- production systemd layout with dedicated `eggpool` system user and `/etc/eggpool`, `/var/lib/eggpool`, `/var/log/eggpool`, `/var/backups/eggpool` paths where current contract says so;
- watchdog cron command and interval semantics;
- automatic backup cron wrapper when explicitly requested;
- logrotate content/path;
- `deploy all` ordering and exclusion/note for separately-managed backup cron.

Rendered snippets must be deterministic for fixed inputs and safely quote paths. Do not render secrets into systemd/cron command lines. Use `EnvironmentFile`/existing env ownership when required.

## Part B — direct install operations

For `--install`/`--uninstall` actions build a reusable command runner abstraction so tests use fake executables/root trees.

Requirements:

- root/sudo/personal/production checks follow O001;
- explicit confirmation unless current flag semantics bypass it;
- stop a directly-running unmanaged server before service takeover when required;
- prepare directories with exact owner/group/modes;
- validate config before enabling service;
- write files atomically where practical;
- invoke external tools with argv arrays, never shell strings;
- mandatory `systemctl` steps fail the command; optional status presentation may warn;
- partial failure leaves files/service state documented and retryable rather than pretending success;
- repeated install is idempotent where the Python contract is idempotent.

Do not hide privilege escalation inside Rust. The operator invokes via sudo/root according to the documented command.

## Part C — systemd

Personal install should:

- resolve the actual Rust executable path;
- preserve invoking user under sudo where current deploy-user logic does;
- create correct config/data/state/env directories;
- write the unit;
- `daemon-reload`, `enable`, `start` in required order;
- report status and failure actionably.

Production install additionally owns dedicated user/directory provisioning and hardened unit content already present in the project. Preserve `--production` and `--as-root` semantics exactly.

M9 must not introduce systemd socket activation, templated multi-instance services, or system-wide orchestration not in the current project.

## Part D — cron/watchdog

`deploy cron` renders/installs the O003 `ensure-running`/watchdog flow for systems without systemd.

Requirements:

- exact interval/default/user behavior;
- reviewed command/config path quoting;
- install/uninstall idempotence;
- no duplicate lines after repeated installation;
- no cron frequency faster than the current intended watchdog behavior merely to improve apparent uptime;
- cron entry invokes the cheap O003 command, not full server initialization on every tick.

## Part E — backup cron

This remains an optional OS-level schedule around the O006 manual backup command, separate from the in-process automatic-backup config/task.

Preserve personal/production/user/install/uninstall contract. Document that users should avoid enabling two independent backup schedules unintentionally; do not try to build cross-scheduler distributed locking. The O006 backup service itself must tolerate serialized/rejected overlap safely.

## Part F — logrotate

Port the exact current log path/rotation snippet behavior. Installation validates target path and mode, but does not invoke logrotate globally unless Python does.

No logging framework change is part of O009.

## Part G — Rust candidate install artifacts

After O003-O008 command behavior exists, prepare service/install assets that can point at the Rust binary:

- reviewed systemd/cron/logrotate templates generated by Rust;
- any helper script used specifically by deployment commands;
- candidate install documentation for building/copying the Rust binary in side-by-side testing.

Do **not** replace the public Python-focused `scripts/install.sh`/README quick-start with Rust-default behavior in O009. M11 will do the cutover after M10 qualification and release artifacts exist.

If a shared installer script can be safely made implementation-aware without changing the public default, that is allowed but must not make Rust canonical early.

## Part H — uninstall

Port `uninstall` with:

- `--yes`;
- `--keep-data`;
- `--keep-config`;
- `--keep-path`;
- `--deploy-artifacts`.

Requirements:

1. identify the current Rust executable/install method/path without broad filesystem scanning;
2. stop/disable owned service/process safely;
3. remove deployment artifacts only when requested/current contract permits;
4. preserve keep-flag paths exactly;
5. never follow symlinks outside reviewed EggPool roots;
6. refuse ambiguous/destructive roots;
7. remove only EggPool-owned known files/directories;
8. verify binary removal where applicable;
9. report leftovers/operator steps explicitly.

Do not `rm -rf` user HOME or parent XDG directories. Removing an EggPool subtree requires proving the target is the resolved EggPool-owned path.

## Tests

Use fake filesystem roots and fake `systemctl`, `useradd`, cron, chown/chmod command runners. Cover:

- exact personal/production unit snapshots/facts;
- path/user/group/env quoting including spaces;
- direct root vs sudo vs normal-user decisions;
- install cancel;
- missing binary/config invalid;
- every mandatory systemctl failure point;
- repeated install/idempotence;
- production user already exists/create failure;
- directory permission/ownership failure;
- cron install/reinstall/uninstall/custom interval/user;
- backup cron personal/production overlap behavior;
- logrotate print/install failure;
- deploy all ordering;
- uninstall each keep-flag combination;
- symlink/path traversal/refusal cases;
- service stop/disable failure and partial recovery reporting;
- no accidental deletion outside temp EggPool roots.

Add one bounded disposable Linux acceptance test for a non-production fake/temporary service root where feasible. Real host `/etc` mutation is not a normal CI prerequisite.

## Dependency posture

No packaging/deployment framework is expected. Use standard process/fs plus narrow safe Unix user/permission helpers already present or justified. Avoid libc/unsafe code in project source; prefer safe wrappers/commands where necessary.

## Documentation

Update migration/deployment docs so an implementer/operator can distinguish:

- current Python canonical install path until M11;
- Rust candidate build/run/deploy path for M9/M10 testing;
- systemd vs cron watchdog;
- in-process automatic backup vs optional backup cron;
- uninstall keep flags.

Do not rewrite public quick start as Rust-default yet.

## Non-goals

- GitHub release matrix;
- Homebrew/apt packages;
- Windows services;
- container/Kubernetes deployment;
- fleet management;
- M11 public cutover.

## Verification

Run fmt/Clippy, focused O009 tests, O003 lifecycle and O006 backup regressions, O008 update path tests, aggregate Rust, Python deploy/uninstall oracle tests, migration suite, shellcheck/current script tests where scripts changed, static checks, and `git diff --check`.

## Closure evidence

Write `migration-rs/closure/operations/009-status.md` with deployment artifact parity hashes/facts, fake-command failure matrix, uninstall deletion allowlist/keep-flag matrix, bounded Linux acceptance evidence, dependency/docs review, and unresolved M11 prerequisites.

## Acceptance criteria

O009 closes only when Rust can generate/install/remove the documented local deployment artifacts safely and idempotently, uninstall cannot escape reviewed EggPool paths, systemd/cron failures are recoverable/actionable, and no M10/M11 scope is prematurely claimed.

Accepted O009 promotes only O010.
