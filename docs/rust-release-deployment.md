# Rust release deployment

This document covers standalone Rust binaries and disposable qualification
hosts. Normal users should install the native Rust wheel from PyPI using
`scripts/install.sh`, uv, or pipx; see [Upgrade and rollback](upgrading.md).
Standalone binaries use the verified GitHub raw-asset authority and are
distinct from package-managed installations.

## Build and run

Build the release from the checkout, then run the resulting binary directly
against an isolated configuration:

```bash
cargo build --manifest-path rust/Cargo.toml --release
./rust/target/release/eggpool --config /path/to/config.toml check-config
./rust/target/release/eggpool --config /path/to/config.toml serve --verbose
```

The deployment commands preserve the invoked manager-exposed command path.
For a personal uv/pipx install, invoke deployment through `command -v
eggpool`; the resulting unit keeps that stable exposed path while the manager
replaces the wheel behind it. Copying a checkout binary remains a separate
foreground/qualification flow and is not a package-manager deployment.

## systemd

Personal mode runs as the invoking user and creates a unit with the resolved
Rust binary, config, data, state, and optional `.env` paths:

```bash
sudo env "PATH=$PATH" "$(command -v eggpool)" \
  --config "$HOME/.config/eggpool/config.toml" deploy systemd --install
```

The install validates the config before writing the unit, prepares the XDG
directories, writes `/etc/systemd/system/eggpool.service` atomically, then
runs `systemctl daemon-reload`, `enable`, and `start` in that order. Direct
root personal installs are refused unless `--as-root` is explicit.

Production mode has one explicit package authority: a system-owned pipx root,
not a root user's private tool environment. Prepare the exact package under
the dedicated paths, then install the unit:

```bash
sudo env PIPX_HOME=/var/lib/eggpool/pipx PIPX_BIN_DIR=/usr/local/bin \
  pipx install --force eggpool==VERSION
sudo /usr/local/bin/eggpool --config /etc/eggpool/config.toml \
  deploy systemd --install --production
```

Production uses the dedicated `eggpool` user and `/etc/eggpool`,
`/var/lib/eggpool`, `/var/log/eggpool`, and `/var/backups/eggpool`. It does
not use systemd socket activation or a second scheduler. The unit always
executes `/usr/local/bin/eggpool`; `HOME`, `PATH`, `PIPX_HOME`, and
`PIPX_BIN_DIR` are explicit, so update/rollback does not depend on shell
initialization. Run production package transitions as the operator with those
same environment values; the service user never mutates a root-owned manager
directory.

## cron watchdog and backups

When systemd is unavailable, the watchdog installs a marked `@reboot` plus
five-minute `ensure-running` block. The interval can be set from 1–59 minutes;
the command uses absolute, shell-quoted paths and invokes the cheap watchdog
entrypoint rather than starting the full server on every tick:

```bash
"/path/to/eggpool" --config "$HOME/.config/eggpool/config.toml" \
  deploy cron --install --interval 5
"/path/to/eggpool" --config "$HOME/.config/eggpool/config.toml" \
  deploy cron --uninstall
```

`deploy all --install` orders systemd, logrotate, and the watchdog. It does
not install backup cron; that schedule is deliberately separate from the
in-process `[backup].enabled` task so operators do not accidentally enable two
independent schedules.

`deploy backup-cron --install` installs a small wrapper that invokes the Rust
`backup` command. Production mode places the wrapper at
`/usr/local/bin/eggpool-backup` and the root-owned schedule at
`/etc/cron.d/eggpool-backup`; personal mode uses the marked user crontab
block. The backup service retains its own archive and overlap semantics.

## logrotate and uninstall

`deploy logrotate --install` writes the reviewed `/var/log/eggpool/*.log`
policy to `/etc/logrotate.d/eggpool` and runs `logrotate -d` when the tool is
available. A missing logrotate executable leaves the file installed and emits
an actionable warning; a present tool that rejects the policy fails the
command.

The Rust uninstaller resolves only the current executable and known EggPool
paths. It can preserve data, config (including adjacent `.env`), shell PATH
entries, or deployment artifacts:

```bash
"/path/to/eggpool" --config "$HOME/.config/eggpool/config.toml" \
  uninstall --yes --keep-data --keep-config --keep-path
"/path/to/eggpool" --config "$HOME/.config/eggpool/config.toml" \
  uninstall --yes --deploy-artifacts
```

Known services are disabled/stopped before artifact removal. Atomic writes,
argv-only external commands, explicit keep flags, symlink refusal, and
leftover reporting make partial failures retryable. The command never scans a
home directory or recursively removes an unresolved parent/XDG directory.

## Disposable rootful Linux qualification

Rootful qualification exercises the deployment boundary on a disposable Linux host with
systemd as PID 1. Build or copy the Rust release binary, then run the guarded
qualification runner as root:

```bash
sudo -E uv run python scripts/qualification_rootful_linux.py \
  --binary rust/target/release/eggpool \
  --output artifacts/qualification/006-run.json \
  --i-understand-disposable-host
```

The runner refuses non-Linux, non-root, non-systemd hosts and refuses known
EggPool paths that already exist. It uses a loopback-only provider, creates a
temporary non-root user, runs personal and production service flows, records
bounded secret-free evidence, and cleans its managed paths in a `finally`
path. If the host is interrupted after the ownership marker is written, run
the same command with `--cleanup --i-understand-disposable-host`; cleanup
refuses to proceed without that marker. Never run this procedure against a
production host.

The service-transition qualification uses the same guard and stable path
with one stateful package environment. Supply the immutable Python and Rust wheels to
the dedicated runner:

```bash
sudo -E uv run python scripts/qualification_service_transition.py \
  --python-wheel /path/to/eggpool-python.whl \
  --rust-wheel /path/to/eggpool-rust.whl \
  --mode personal \
  --output artifacts/qualification/service-transition-personal.json \
  --i-understand-disposable-host
```

Use `--mode production` only on a disposable host after verifying the
system-owned pipx executable and paths. The report contains hashes, service
state, and bounded command results; it never records config or environment
contents.
