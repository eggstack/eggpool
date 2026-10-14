# CLI Reference

Every command accepts `--config /path/to/config.toml`. Config resolution:
`--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`.
Command list verified against `eggpool help` (0.8.2).

## Lifecycle

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the proxy server (daemon mode; `--verbose` for foreground) |
| `eggpool stop` | Stop the running server |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool rehash` | Apply supported config changes live without restart (`--json` for structured output) |
| `eggpool onboard` | Interactive onboarding wizard; a fresh database prompts for an initial administrator |
| `eggpool ensure-running` | Ensure server is running (no full runtime snapshot) |
| `eggpool croncheck` | Fast-path cron watchdog check (no server connection) |

## Providers & models

| Command | Description |
|---------|-------------|
| `eggpool connect` | Add a provider account interactively |
| `eggpool connect list` | List supported providers |
| `eggpool logout` | Remove a configured provider account |
| `eggpool check-config` | Validate configuration |
| `eggpool migrate` | Run database migrations |
| `eggpool models refresh` | Refresh the model catalog |
| `eggpool accounts list` | List configured provider accounts |
| `eggpool accounts status` | Show account status (provider, priority, weight, enabled) |
| `eggpool accounts explain` | Show per-account routing eligibility for a model |
| `eggpool modelinfo show` | Show enriched model metadata |
| `eggpool modelinfo list` | List model-info entries |
| `eggpool modelinfo refresh` | Trigger model-info source refresh |
| `eggpool modelinfo aliases` | Show model aliases |
| `eggpool modelinfo repair` | Repair legacy canonical model-info detail blocks |

## Stats & insight

| Command | Description |
|---------|-------------|
| `eggpool status` | Concise proxy/provider health summary (one row per provider; `--json` for structured output) |
| `eggpool runtime-status` | Detailed process/runtime diagnostics (`--json` for the full snapshot) |
| `eggpool stats transcoding` | Show protocol transcoding statistics |
| `eggpool stats repair-costs` | Dry-run/apply repair for suspicious historical request costs |
| `eggpool stats recompute-costs` | Recompute `cost_microdollars` on historical requests |
| `eggpool stats explain-dashboard` | Show EXPLAIN QUERY PLAN for dashboard queries |

## Config & keys

| Command | Description |
|---------|-------------|
| `eggpool set` | Set a config value |
| `eggpool edit` | Edit config in $EDITOR |
| `eggpool getkey` | Print the server API key |
| `eggpool newkey` | Generate and write a new server API key (`--show-old`, `--show-secrets`) |
| `eggpool users bootstrap` | Create the initial local administrator (`--password-stdin` for unattended input) |
| `eggpool users list` | List local usernames, role and status |
| `eggpool users token USER` | Issue a personal inference/discovery token (`--label` optional; `--show-secret` allows non-TTY output) |
| `eggpool users revoke USER` | Revoke all personal API tokens for a user |
| `eggpool users reset-password USER` | Reset a local password and revoke that user's API tokens and browser sessions (`--password-stdin` supported) |
| `eggpool users enable-managed` | Retire the legacy key path after clearing `[server].api_key`/`api_key_env` and setting `dashboard.public = false` |
| `eggpool users policy create ID NAME allow\|deny` | Create an exact-rule model profile |
| `eggpool users policy rule PROFILE MODEL allow\|deny` | Add or replace one exact model or `model/provider` rule |
| `eggpool users policy assign USER PROFILE` | Assign a profile to a local user |
| `eggpool users policy prefer PROFILE [MODEL]` | Set or clear a profile's allowed client recommendation |
| `eggpool init-config` | Initialize config from template |
| `eggpool dashboard public --on\|--off` | Toggle dashboard key requirement (default public) |

Policy commands are a local operator setup path until the admin dashboard
offers profile editing. They write SQLite policy state directly; model IDs
are exact and wildcards are rejected. `--actor ADMIN` records the active
administrator responsible for each change. Restricted profiles should use
`deny` as their default and add explicit allowed models.

## Maintenance

| Command | Description |
|---------|-------------|
| `eggpool backup` | Create a timestamped backup (`--output-dir` override) |
| `eggpool recover [source]` | Restore from a backup archive (interactive picker when omitted) |
| `eggpool db vacuum` | Vacuum the SQLite database |
| `eggpool version` | Show installed version |

## Deploy

| Command | Description |
|---------|-------------|
| `eggpool deploy systemd` | Print/install systemd unit |
| `eggpool deploy cron` | Install watchdog cron (non-systemd) |
| `eggpool deploy backup-cron` | Install daily backup cron job |
| `eggpool deploy logrotate` | Print/install logrotate config |
| `eggpool deploy all` | Print every deployment snippet in sequence |

Full deploy commands reference: [deployment.md](deployment.md#deploy-commands-reference)

Personal tokens are stored as digests and can authenticate inference,
`/v1/models`, and the integration profile; they do not grant dashboard or
administrative access. The shared `[server].api_key` authenticates only legacy
`legacy_unmigrated` installations and stops working after local identity
initialization. Browser sessions use HttpOnly cookies and protect logout and
password changes with same-origin checks plus a session-bound CSRF token.
Passwords are never accepted as command-line arguments. Local CLI access is
the recovery authority; password reset revokes API credentials and sessions.

## Agents & updates

| Command | Description |
|---------|-------------|
| `eggpool configsetup` | Generate config snippets for coding agents: `opencode`, `codex`, `claude-code`, `aider`, `qwen-code`, `kilo`, `continue`, `cline`, `roo-code`, `goose`, `openhands` (see [Agent Configuration](agent-configuration.md)) |
| `eggpool configremote <target>` | Export a secret-free `epc1` remote profile for Codex/OpenCode on other machines (`--format command\|token\|json`, `--base-url` override) |
| `eggpool update [VERSION]` | Install the latest or one exact catalogued release (`v` prefix accepted; `--check` for dry-run, `--from-source` for local build) |
| `eggpool install-provenance` | Show the package manager or standalone update authority |
| `eggpool uninstall` | Uninstall EggPool from this machine |

For exact upgrades, supported rollback targets, standalone binaries, and
ownership failures, see [upgrading.md](upgrading.md).
