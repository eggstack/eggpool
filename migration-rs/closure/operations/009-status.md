# O009 Closure — Deployment, Install Artifacts, and Uninstall

Status: closed

Recommendation: closed; O010 is dependency-ready. M10 remains blocked on
accepted O010 M9 closure and its separate planning/implementation review.

Implementation commit: [`27310ffdd96e1d9aa94887edd312a932be0ad091a`](https://github.com/eggstack/eggpool/commit/27310ffdd96e1d9aa94887edd312a932be0ad091a)

Plan: [O009 — deployment, install artifacts, and uninstall](../../implementation/operations/009-deployment-install-artifacts-and-uninstall.md)

## Outcome

O009 is implemented in the Rust candidate. All six owned command surfaces now
reach Rust behavior: `deploy systemd`, `deploy cron`, `deploy backup-cron`,
`deploy logrotate`, `deploy all`, and `uninstall`. The implementation keeps
the Python package and public installer canonical, adds no release matrix or
platform cutover, and exposes no Python fallback.

## Deployment artifact parity

The Rust renderers preserve the current operator contract while substituting
the resolved Rust executable and safely quoting dynamic paths.

| Artifact | Implemented facts | Parity classification |
|---|---|---|
| Personal systemd | `Type=simple`, resolved `User=`/`Group=`, absolute `ExecStart`, config/data/env paths, restart-on-failure, 30-second graceful stop | semantic parity; path quoting is an intentional safety tightening |
| Production systemd | dedicated `eggpool` user/group, `/etc/eggpool`, `/var/lib/eggpool`, `/var/log/eggpool`, `/var/backups/eggpool`, hardened `ProtectSystem=strict` unit, no socket activation | exact directive/fact parity with the Python production layout, dynamic executable path allowed by O009 |
| Watchdog cron | marked `@reboot` and `*/N` entries, default five-minute interval, bounded 1–59 interval validation, absolute quoted paths, `ensure-running` only | exact schedule semantics; safe quoting added for path-bearing fields |
| Backup cron | separate from the in-process automatic-backup task; production wrapper invokes Rust `backup` at 02:00, personal entries are marked and removable | semantic parity with the current schedule and O006 backup boundary |
| Logrotate | `/var/log/eggpool/*.log`, daily rotation, 14 retained, compression/delay, copytruncate, dateext, 100M maxsize | exact content facts |
| `deploy all` | systemd, logrotate, watchdog in that order; backup cron is explicitly excluded and noted | exact ordering/exclusion |

The checked-in Python artifact references remain unchanged; O009 does not
rewrite `scripts/install.sh`, the README quick start, Python package metadata,
or the public update authority.

## Install and failure behavior

- `CommandRunner` is the reusable argv-only process boundary. Tests use
  `RecordingCommandRunner`; no shell command strings are constructed for
  `systemctl`, `useradd`, `chown`, `crontab`, or `logrotate`.
- Systemd installation validates the config before the unit is published,
  prepares directories with explicit modes/owners, writes atomically, and
  requires `daemon-reload`, `enable`, and `start` in order.
- Production installation provisions the dedicated user when absent, seeds
  missing config/env files, and reports user, permission, validation, and
  systemd failures as non-zero actionable errors.
- Existing PID state is stopped through the O003 lifecycle path before
  personal service takeover. Optional logrotate absence leaves the reviewed
  file installed with a warning; a present logrotate syntax failure fails the
  command.
- Cron installation removes the prior managed block before appending, so
  repeated installation is idempotent and unrelated crontab lines survive.
  Crontab write failures propagate rather than being reported as success.

## Uninstall deletion allowlist

Rust resolves the current executable only; it does not scan broad filesystem
locations. The removal allowlist is the resolved executable, the selected
config and adjacent env file, the resolved EggPool data/state directories,
the exact systemd/logrotate/cron/backup artifact paths, and explicitly
identified shell rc files.

| Flags | Binary | Config/env | Data/state | Shell PATH | Deploy artifacts |
|---|---:|---:|---:|---:|---:|
| default | remove | remove | remove | scrub | keep |
| `--keep-data` | remove | remove | keep | scrub | keep |
| `--keep-config` | remove | keep | remove | scrub | keep |
| `--keep-path` | remove | remove | remove | keep | keep |
| `--deploy-artifacts` | remove | remove | remove | scrub | remove |
| all keep flags | remove | keep | keep | keep | keep unless explicitly requested |

Removal refuses root/top-level targets, symlinked config/artifact paths,
symlinked parents outside the reviewed OS prefix, and recursive deletion of
unresolved parent directories. Directory traversal uses `symlink_metadata`
and never follows child symlinks. Known leftovers are returned and printed
for operator follow-up.

## Verification evidence

Commands run:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings   PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1 PASS (2)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --all-targets --no-run                    PASS
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1                              PASS (100 passed, 3 skipped)
rtk uv run pytest tests/migration_rs/test_o001_operations.py tests/unit/test_deploy_user.py tests/unit/test_lifecycle_uninstall.py -q --tb=short --maxfail=1 PASS (104)
rtk git diff --check                                                                         PASS
```

The serialized `cargo test --all-targets -- --test-threads=1` aggregate was
also attempted. The local runner stopped exposing a child process or output
after several minutes, so it was interrupted and is not represented as a
pass. All affected Rust suites and all-target compilation passed
individually. This is the same runner limitation documented by the accepted
O008 closure, not a failing O009 assertion.

The disposable temporary-root acceptance tests cover install command order,
mandatory command failure, cron stdin/argv behavior, every keep-flag class,
unrelated shell rc preservation, and symlink-parent refusal without mutating
the host. The current host is Darwin, so a real `/etc/systemd` or Linux
rootful acceptance was not run; broad Linux/SBC characterization remains M10
scope and no host service was changed during verification.

## Dependency, schema, and security review

O009 adds no Cargo dependency, database migration, config schema, network
surface, or public install authority. It reuses O003 lifecycle control and
O006 `backup`, and uses the existing standard filesystem/process primitives.
Secrets are not rendered into units, cron entries, diagnostics, or wrapper
arguments beyond reviewed config/env paths. The Rust candidate docs clearly
separate this side-by-side path from the Python canonical installer.

## Unresolved M11 prerequisites

M11 still must publish signed or otherwise authoritative Rust release assets,
define the supported target matrix and artifact names, decide integrity/signing
policy for public installation, qualify upgrade/rollback on those targets,
and then change the public quick-start/install/update authority. O009 makes no
claim that those prerequisites are complete.

## Registry transition

O009 is removed from the dependency-ready queue and recorded as closed. O010
is promoted from queued behind O009 to `dependency-ready; O009 closure
accepted` in the plan, operations index, roadmap, and registry. No plan beyond
O010 is unblocked; M10 remains blocked on accepted O010 closure and its own
planning/implementation review.
