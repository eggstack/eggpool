# Rust candidate deployment

This is the side-by-side installation path for the Rust migration candidate.
It is intended for local Linux/Unix qualification during M9/M10. The public
`scripts/install.sh`, Python package, PyPI update path, and README quick-start
remain canonical until M11.

## Build and run

Build the candidate from the checkout, then run the resulting binary directly
against an isolated configuration:

```bash
cargo build --manifest-path rust/Cargo.toml --release
./rust/target/release/eggpool --config /path/to/config.toml check-config
./rust/target/release/eggpool --config /path/to/config.toml serve --verbose
```

The deployment commands resolve the actual running Rust executable. Copy the
binary to its reviewed destination before installing a service; they do not
install or replace the public `eggpool` command.

## systemd

Personal mode runs as the invoking user and creates a unit with the resolved
Rust binary, config, data, state, and optional `.env` paths:

```bash
sudo env "PATH=$PATH" "/path/to/eggpool" \
  --config "$HOME/.config/eggpool/config.toml" deploy systemd --install
```

The install validates the config before writing the unit, prepares the XDG
directories, writes `/etc/systemd/system/eggpool.service` atomically, then
runs `systemctl daemon-reload`, `enable`, and `start` in that order. Direct
root personal installs are refused unless `--as-root` is explicit. Production
mode is a separate root-owned layout:

```bash
sudo "/path/to/eggpool" --config /etc/eggpool/config.toml \
  deploy systemd --install --production
```

Production uses the dedicated `eggpool` user and `/etc/eggpool`,
`/var/lib/eggpool`, `/var/log/eggpool`, and `/var/backups/eggpool`. It does
not use systemd socket activation or a second scheduler.

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
