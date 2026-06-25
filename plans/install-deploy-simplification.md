# Install and Deploy Simplification Plan

## Purpose

Make EggPool easy to install, configure, deploy, and uninstall on private home/LAN machines, especially Raspberry Pis, spare desktops, and other small SBC-style hosts.

The primary target is not a hardened public internet deployment. The default experience should optimize for private use: a lightweight server on a LAN, running as the invoking user, with simple startup persistence through systemd or cron watchdog.

This plan consolidates the installer, deploy, cron, systemd, and uninstall fixes discussed during review. It should be implemented after the fast CLI/watchdog and daemon/runtime primitives are available.

## Target User Journey

Primary systemd path:

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
eggpool onboard
eggpool deploy systemd --install
```

Fallback for systems without systemd:

```bash
eggpool deploy cron --install
```

Verification:

```bash
eggpool accounts status
eggpool croncheck
curl http://localhost:11300/v1/healthz
```

Cleanup:

```bash
eggpool backup
eggpool uninstall --yes
```

The above should work in most personal Linux environments without requiring the user to understand pipx internals, uv tool internals, sudo secure_path, systemd unit details, or cron PATH behavior.

## Design Principles

Default deployment is personal/private. Run as the invoking user by default, not root.

Root should only be used to write system-level files when necessary.

Do not rely on sudo preserving PATH or HOME.

Do not create config files in arbitrary current working directories during deploy/uninstall.

Use absolute paths in systemd and cron artifacts.

Make systemd and cron deployment mutually clear: systemd is preferred when available; cron is a watchdog fallback for systems without systemd.

Separate watchdog cron from backup cron.

Fail early and clearly when required commands or environments are unavailable.

## Phase 1: Stable User and Path Resolution

Add a shared resolver for the deployment user context.

When running normally, the deployment user is the current user.

When running under sudo, resolve the original user using:

```text
SUDO_USER
SUDO_UID
SUDO_GID
getpwnam/getpwuid for home and group lookup
```

If running as root without sudo context, require an explicit root/production mode or print a warning. Do not accidentally deploy a personal root service.

Use stable default personal paths:

```text
config:    ~/.config/eggpool/config.toml
env:       ~/.config/eggpool/env or ~/.config/eggpool/.env
data dir:  ~/.local/share/eggpool
database:  ~/.local/share/eggpool/usage.sqlite3
state dir: ~/.local/state/eggpool
pid:       ~/.local/state/eggpool/eggpool.pid, unless XDG_RUNTIME_DIR is set
logs:      ~/.local/state/eggpool/eggpool.log
```

Support `EGGPOOL_CONFIG` consistently across the CLI. The install script currently suggests exporting it, but the CLI should actually honor it. Precedence should be:

```text
--config PATH
EGGPOOL_CONFIG
~/.config/eggpool/config.toml
```

If compatibility requires keeping `config.toml` in cwd for source development, limit that behavior to source/dev contexts or document it explicitly. The smooth installed path should not depend on cwd.

## Phase 2: Fix `scripts/install.sh`

### Fix project directory handling

After cloning or entering `$INSTALL_DIR`, reset `PROJECT_DIR` to the actual install directory.

Current bug: in the curl-piped path, the script changes into `$INSTALL_DIR`, but later reports and uses `CONFIG_PATH="$PROJECT_DIR/config.toml"`, where `PROJECT_DIR` was computed before cloning. This can point at the wrong directory.

Required behavior:

```bash
cd "$INSTALL_DIR"
PROJECT_DIR="$(pwd)"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
```

### Make pipx and uv-tool paths symmetric

The pipx branch should not skip config setup. Whether installation uses pipx or uv-tool, the script should:

```text
ensure eggpool command is installed
ensure ~/.config/eggpool/config.toml exists or can be initialized
print the resolved config path
print next commands
optionally run onboarding prompt
```

### Do not silently reinstall over an existing command

If `eggpool` already exists on PATH, do not continue into `pipx install eggpool` or `uv tool install .` without a deliberate update/reinstall choice.

Noninteractive default should be safe:

```text
Existing eggpool install detected: /path/to/eggpool
Run `eggpool update` to upgrade, or rerun with --force to reinstall.
```

If adding flags to the script is feasible:

```bash
./scripts/install.sh --force
./scripts/install.sh --upgrade
```

### Avoid surprising PyPI installs from source checkouts

If the script is run from a cloned source checkout and pipx is available, it should install the current checkout, not silently install the latest PyPI artifact.

Acceptable behavior:

```bash
pipx install .
```

or:

```bash
pipx install --force .
```

For curl-piped stable install, PyPI is acceptable.

### Improve install output

After install, print:

```bash
eggpool onboard
eggpool check-config
eggpool deploy systemd --install
```

and:

```bash
eggpool deploy cron --install
```

for non-systemd environments.

Do not tell users to run `sudo eggpool ...` until deploy handles sudo PATH and sudo HOME correctly.

## Phase 3: Fix Personal Systemd Deployment

`eggpool deploy systemd --install` should install a personal service that runs as the invoking user.

### Sudo-safe invocation

Docs and command hints should prefer:

```bash
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

However, the command itself should also work when `sudo eggpool ...` succeeds on systems where sudo PATH includes the binary.

### Generate a non-root personal unit

The personal unit installed into `/etc/systemd/system/eggpool.service` must include:

```ini
User=<invoking-user>
Group=<invoking-user-primary-group>
WorkingDirectory=<absolute-user-data-dir>
ExecStart=<absolute-eggpool-binary> --config <absolute-config-path> serve
Environment=EGGPOOL_CONFIG=<absolute-config-path>
EnvironmentFile=<absolute-env-path-if-present>
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
```

Do not omit `User=` and `Group=`. Omitting them causes the system service to run as root, which is not appropriate for default private deployment.

If the operator intentionally wants root, require an explicit flag such as:

```bash
eggpool deploy systemd --install --as-root
```

and print a warning.

### Prepare filesystem state

Before writing the unit:

```text
create ~/.config/eggpool if missing
create ~/.local/share/eggpool if missing
create ~/.local/state/eggpool if missing
initialize config if missing, without overwriting existing config
ensure database parent directory exists
ensure env path handling is explicit
```

When running under sudo, perform these actions for the invoking user and set ownership correctly.

### Validate before starting

Before enabling/starting the service:

```bash
eggpool check-config
eggpool migrate
```

Run these as the deployment user, not root, for personal mode.

If credentials are placeholders or missing, fail with a clear message:

```text
Configuration is not ready. Run `eggpool onboard` or `eggpool connect` first.
```

### Harden systemctl error handling

Every systemctl step should be checked:

```bash
systemctl daemon-reload
systemctl enable eggpool
systemctl start eggpool
systemctl status eggpool
```

If `daemon-reload`, `enable`, or `start` fails, exit nonzero and print stderr. Do not continue after failed setup steps.

Catch `FileNotFoundError` for missing `systemctl` and print:

```text
Systemd is not available in this environment. Use `eggpool deploy cron --install`.
```

Also detect non-systemd PID 1 where practical.

## Phase 4: Explicit Production Systemd Mode

Keep production separate from personal mode.

Add:

```bash
eggpool deploy systemd --install --production
```

Production mode should automate the current manual production docs:

```text
create system user eggpool if missing
create /etc/eggpool
create /var/lib/eggpool
create /var/log/eggpool
copy or initialize /etc/eggpool/config.toml if missing
copy or initialize /etc/eggpool/env if missing
set ownership and permissions
validate config as eggpool user
run migrations as eggpool user
write hardened systemd unit
systemctl daemon-reload
systemctl enable eggpool
systemctl start eggpool
systemctl status eggpool
```

Production mode is not the default because EggPool's near-term target is lightweight private deployment.

## Phase 5: Redesign Cron Deployment as Watchdog

The current `deploy cron --install` behavior installs a daily backup job. That conflicts with the intended cron fallback, which should keep the server running.

Redefine:

```bash
eggpool deploy cron --install
```

as watchdog/startup deployment.

Generated entries should include:

```cron
@reboot /absolute/path/to/eggpool --config /absolute/config.toml ensure-running >> /absolute/state/cron.log 2>&1
*/5 * * * * /absolute/path/to/eggpool --config /absolute/config.toml ensure-running >> /absolute/state/cron.log 2>&1
```

Add interval option:

```bash
eggpool deploy cron --install --interval 5
eggpool deploy cron --install --interval 10
```

Default to 5 minutes for SBC/private deployment.

Do not require root for personal cron installation. Install into the invoking user's crontab. If run under sudo, use `SUDO_USER` and install into that user's crontab, not root's, unless explicitly requested:

```bash
eggpool deploy cron --install --user root
```

Cron entries must use absolute binary, config, and log paths. Do not rely on cron PATH.

Add uninstall support:

```bash
eggpool deploy cron --uninstall
```

Remove only EggPool-managed watchdog blocks. Use clear markers, for example:

```cron
# BEGIN EggPool watchdog
...
# END EggPool watchdog
```

## Phase 6: Move Backup Cron to a Separate Command

Move the current backup cron behavior to:

```bash
eggpool deploy backup-cron --install
```

Backup cron should not be mixed with watchdog cron.

Personal backup cron should install into the invoking user's crontab and write to that user's backup directory.

Production backup cron may install `/etc/cron.d/eggpool-backup` and write to `/var/backups/eggpool`.

The backup script should handle missing `sqlite3` clearly. If `sqlite3` is required, docs and error messages should say so.

## Phase 7: Fix Logrotate Deployment

`deploy logrotate --install` should not call `systemctl restart logrotate`. Many systems do not have a `logrotate.service`; logrotate is usually run by cron or a systemd timer.

Instead:

```bash
logrotate -d /etc/logrotate.d/eggpool
```

or print a warning if `logrotate` is unavailable.

Exit nonzero on invalid config if validation is possible.

## Phase 8: Fix Uninstall and Lifecycle Commands

### Do not force config creation for lifecycle cleanup

The top-level CLI currently ensures config for most commands. Exclude lifecycle commands that need to operate even when config is missing or corrupted:

```text
uninstall
recover
backup, if appropriate
```

Uninstall should not create a new config file just to remove EggPool.

### Fix install-method detection

Do not classify a generic venv as pipx just because `pipx` exists on PATH.

Detect pipx by inspecting executable paths and metadata associated with the running command.

Detect uv-tool by checking uv tool directory structure.

Detect source checkout by verified project root.

Fallback to manual only when none match.

### Fix default source checkout uninstall

The installer defaults to `~/eggpool`. The safety layer currently refuses top-level children of home. Keep that safety rule generally, but allow deletion of a verified EggPool checkout at `~/eggpool` after explicit confirmation.

### Remove stale option references

The manual uninstall error references `--keep-binary`, but the CLI does not expose that option. Either implement it or remove the reference.

### Make rc cleanup transactional

Do not write shell rc changes before final confirmation.

Compute the planned diff, show affected files, ask for confirmation, then write.

### Optional deploy artifact cleanup

Add:

```bash
eggpool uninstall --deploy-artifacts
```

This may remove:

```text
personal systemd service
watchdog cron block
backup cron block
logrotate config
```

Keep production artifact removal cautious and explicit.

## Phase 9: Documentation Rewrite

Update README and `docs/deployment.md` around the true target flow.

Primary private/LAN path:

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
eggpool onboard
eggpool deploy systemd --install
```

Cron fallback:

```bash
eggpool deploy cron --install
```

Manual direct run:

```bash
eggpool serve --daemon
```

Debug foreground run:

```bash
eggpool serve
```

Clarify that public internet exposure is not the default target. Keep production deployment as advanced/explicit.

Do not document backup cron as `deploy cron`; document it as `deploy backup-cron` after the command split.

## Phase 10: Test Matrix

### User/path resolution

```text
normal user
sudo with SUDO_USER
sudo without SUDO_USER
root direct
custom --config
EGGPOOL_CONFIG
XDG_CONFIG_HOME
XDG_DATA_HOME
XDG_RUNTIME_DIR
```

### Installer behavior

```text
fresh pipx install
fresh uv-tool install
existing eggpool command
source checkout install with pipx available
curl-piped install
missing Python 3.11+
missing uv
wrong/old PROJECT_DIR regression
```

### Systemd rendering

```text
personal unit includes User and Group
personal unit does not run as root by default
ExecStart uses absolute binary path
config path is absolute
WorkingDirectory is absolute and exists
EnvironmentFile appears only when appropriate
systemctl failures stop the install
missing systemctl recommends cron
```

### Cron rendering

```text
watchdog cron includes @reboot
watchdog cron includes */5 default interval
--interval 10 renders */10
absolute binary path
absolute config path
absolute log path
installs into invoking user's crontab under sudo
uninstall removes only EggPool-managed block
backup cron is separate from watchdog cron
```

### Lifecycle cleanup

```text
uninstall works when config is missing
uninstall does not create config
install-method detection distinguishes pipx/uv-tool/source/manual
verified ~/eggpool source checkout can be removed after confirmation
rc cleanup is transactional
```

## Acceptance Criteria

Fresh private install on a Raspberry Pi can be completed with a handful of commands.

`eggpool deploy systemd --install` starts a service as the intended user, not root.

`eggpool deploy cron --install` installs a watchdog that uses `ensure-running` and does not cause recurring CPU spikes.

The current backup cron behavior is moved out of `deploy cron`.

Uninstall is not blocked by missing config and does not create new config during cleanup.

Docs match actual behavior.

## Non-Goals

Do not make public internet deployment the default.

Do not remove production deployment support; make it explicit.

Do not force users to understand systemd internals for private deployment.

Do not merge watchdog and backup cron behavior.
