# M9 Operations Contract (O001)

Status: frozen by O001; implementation begins at O002.

This document is the bounded Python oracle for the M9 operational surface. It
freezes user-visible facts and effects, not Python class names or implementation
structure. Exact fields/bytes are marked `exact`; filesystem paths are exact
after isolated-root substitution; presentation that may change across a Rust
CLI framework is marked `semantic`.

## Authority and audit boundary

The audit covered the current checkout of:

- `src/eggpool/cli.py`, `cli_full.py`, `cli_exit_codes.py`, `fastcli.py`,
  `runtime.py`, `runtime_paths.py`, and `deploy_user.py`;
- `src/eggpool/control/client.py` and `control/server.py`;
- `src/eggpool/lifecycle/backup.py` and `lifecycle/uninstall.py`;
- `src/eggpool/update_checker.py`, `providers/connect.py`, `onboard.py`,
  `integrations/`, `background/`, `runtime_task_inventory.py`, and
  `runtime_tasks.py`;
- `src/eggpool/deploy/__init__.py`, `scripts/install.sh`, and deployment
  assets under `deploy/`;
- Rust `cli.rs`, `runtime.rs`, `runtime_lifecycle.rs`, `reload.rs`,
  `server.rs`, and `task_supervisor.rs`;
- the F003 inventory/closure and R008/R013 closure records.

The plan's historical references to `src/eggpool/deploy.py` and a flat
`src/eggpool/lifecycle/` helper set are satisfied by the current package
modules `deploy/__init__.py` and `lifecycle/{backup,uninstall}.py`. No such
module deletion or move is inferred from the plan text.

F003 has two current-main parser deltas. Python now exposes `dashboard public
--on/--off`, whereas the F003 Rust parser only had `--on`; Python exposes the
paired `--dry-run/--apply` flags for `stats recompute-costs` and
`stats repair-costs`, whereas Rust only represented `--dry-run`. O001 records
these deltas here and the Rust parser now accepts both sides with mutual
exclusion. F003 remains append-only history.

## Command ownership

The following is the complete current Click command tree (63 paths), including
group paths. Group rows are included because they have help/output contracts;
their implementation owner is the plan that owns their children.

| Command path | Owner | Contract class |
|---|---|---|
| `accounts`, `accounts list`, `accounts status`, `accounts explain` | O007 | semantic projection; `explain` accepts `--model --provider --protocol --scores --gates` |
| `backup`, `recover` | O006 | exact archive/effect contract |
| `check-config`, `edit`, `getkey`, `newkey`, `set`, `init-config` | O004 | exact mutation/diagnostic contract |
| `connect`, `connect list`, `logout`, `onboard` | O004 | semantic interactive/provider mutation contract |
| `dashboard`, `dashboard public` | O004 | exact toggle effect; `--on` and `--off` are mutually exclusive |
| `configsetup` | O005 | semantic group presentation |
| `configsetup opencode`, `configsetup claude-code` | O005 | exact/semantic target renderer contract |
| `configsetup aider`, `cline`, `codex`, `continue`, `goose`, `kilo`, `openhands`, `qwen-code`, `roo-code` | O005 | shared options: `--print-secret --no-clipboard --force --output --write --model --base-url --host` |
| `db`, `db vacuum`, `migrate` | O006 | exact maintenance result/effect contract |
| `models`, `models refresh`, `modelinfo`, all five `modelinfo` subcommands | O007 | semantic inspection/refresh projections |
| `stats`, `stats explain-dashboard`, `stats transcoding`, `stats recompute-costs`, `stats repair-costs` | O007 | semantic projections; recompute/repair preserve paired `--dry-run/--apply` |
| `serve`, `stop`, `restart`, `rehash`, `runtime-status`, `croncheck`, `ensure-running` | O003 | exact lifecycle/control result classes |
| `help` | O003 | semantic help presentation |
| `version`, `update` | O008 | exact version/update user semantics |
| `deploy`, `deploy systemd`, `deploy cron`, `deploy backup-cron`, `deploy logrotate`, `deploy all`, `uninstall` | O009 | exact snippets/effects and safe mutation contract |

Every command retains the global `--config PATH` option before the command.
The canonical options and positional arguments are recorded in
`fixtures/operations/o001-fixture-matrix.json`; the Python observation helper
derives the same tree from Click and rejects missing, extra, or unowned paths.

## Exact operational observations

### Paths and process state

In the isolated oracle environment, path values are compared after replacing
the temporary root with `<isolated-root>`. Resolution is:

| Fact | Frozen behavior |
|---|---|
| Config | `--config` > `EGGPOOL_CONFIG` > existing `$XDG_CONFIG_HOME/eggpool/config.toml` > `./config.toml` |
| Config/data/state | XDG config/data/state homes, each with `eggpool` beneath it; home-relative XDG fallbacks when unset |
| Runtime directory | explicit `EGGPOOL_RUNTIME_DIR`; private `XDG_RUNTIME_DIR/eggpool`; private state `runtime`; UID-scoped `/tmp` fallback |
| PID | explicit `EGGPOOL_PID_FILE`; `XDG_RUNTIME_DIR/eggpool.pid`; state `eggpool.pid`; UID-scoped fallback; content is decimal PID |
| Log | explicit `EGGPOOL_LOG_FILE`; state `eggpool.log`; UID-scoped fallback |
| Control socket | `<runtime-dir>/eggpool.sock`, owner-only mode `0600`, bounded path length |

PID state is advisory. Duplicate detection requires a live PID probe and a
health probe (`GET /v1/healthz`, with wildcard binds probed via loopback).
Missing/dead PID files are cleared. `croncheck` is a cheap PID/health check;
`ensure-running` only starts a process when no live server is observed.
Foreground service operation is the same server process used by systemd;
detached mode spawns the foreground command and redirects output to the log.
Direct-root personal deployment is refused unless explicitly overridden;
production deployment is a separate root-gated path.

### Local control

The current Python wire contract is one newline-delimited JSON request and one
newline-delimited JSON response per Unix-socket connection:

- protocol version `1`, command set `{reload_config}`, request id echoed;
- request fields: `protocol_version`, `request_id`, `command`, optional
  64-lower-hex `validated_digest`, and optional object `params`;
- response fields: `protocol_version`, `request_id`, `ok`, `stage`,
  `generation`, `changed_sections`, `warnings`, `restart_required`,
  `retirement_pending`, and `message`, plus bounded optional diagnostics;
- request limit `65536` bytes, request-id limit `256`, command timeout `30s`,
  one request per connection, owner-only socket permissions;
- malformed JSON, wrong version, missing/invalid request id, unknown command,
  invalid digest, empty, oversized, multi-frame, and timeout cases fail at a
  typed parse/timeout boundary and never invoke reload;
- stale cleanup removes only an owner-owned stale socket after a positive
  `ECONNREFUSED` probe and inode check; regular files, symlinks, foreign-owner
  entries, live sockets, and ambiguous probe errors are preserved;
- a successful `reload_config` projects the M8 response without exposing
  config contents, API keys, request bodies, or proxy credentials.

### Config/provider mutation

Mutations preserve unrelated TOML text where the existing Python editor does,
write only the resolved destination, and keep environment-backed secrets out
of the config. `set` and `dashboard public` are atomic logical changes;
`newkey` writes an inline key only when the config is not env-backed; `getkey`
is the explicit full-secret surface. `newkey` redacts the old key unless
`--show-old` is explicit. Provider `connect`, `logout`, and `onboard` preserve
template/provider selection, support interactive cancellation, and return a
control-socket live-apply result when available. Control-unavailable behavior
is reported as such and does not claim a live reload. Invalid candidates do not
replace the prior config.

### Integration generation

All eleven current targets use a bounded context containing the resolved base
URL, model choice, server key/reference, and model capability limits. Secret
content is written only on explicit safe output surfaces or to an explicit
target path; normal terminal output states that the secret was omitted.
`--force` permits replacement and creates a timestamped `.eggpool.bak.*`
copy when existing content differs. Clipboard is best-effort and can be
disabled. `--write` requires a known default target or `--output`; a missing
model is an explicit target error where the target requires one. Context-driven
config/transcoder mutations request a restart, and renderer output itself does
not invoke live providers.

### Database and backup

Migrations consume the numbered Python schema/checksum authority. Fresh/current
DB, migration failure, checksum mismatch, and vacuum failure are non-zero,
secret-free outcomes; no reset or schema fork is allowed. Backups are
uncompressed ZIP archives named `eggpool-backup-YYYYMMDD-HHMMSS[-N].zip` and
contain `META`, `config.toml`, optional `.env`, and `usage.sqlite3` (plus sidecar
files only when explicitly present in the source-format helper). Runtime
backups use a consistent SQLite snapshot and record the live DB path in META.
The archive uses stored entries, atomic publication, count-based retention,
and private config/env restore modes. Restore accepts only the reviewed member
basenames; traversal-looking names are classified to the fixed target basename
and therefore cannot escape the destination, while unknown basenames fail.
Existing targets are snapshotted first, and no archive member is extracted as
an arbitrary filesystem path.

### Inspection and background ownership

Inspection commands project facts from existing repositories/services. Timestamps,
terminal widths, and temporary root paths are normalized only as incidental
presentation. JSON modes retain stable field names and types.

The only R008 deferred capabilities at current main are exactly:

| Task | Enable predicate | Schedule/ownership | Failure/reload semantics |
|---|---|---|---|
| `metrics_flush` | `metrics.write_mode != immediate` | process-owned, configured flush interval, 5s initial delay | one supervisor callback; flush failure is isolated; schedule is reloadable |
| `update_checker` | `update_checker.enabled` and process startup capability | process-owned, immediate first check then 86400s | lookup errors are recorded and swallowed; startup-only registration survives generation swaps |
| `automatic_backup` | `backup.enabled` and `backup.interval_s > 0` | process-owned, configured interval and startup delay | tick errors are logged/isolated; schedule is reloadable; no parallel scheduler |

All three use the one M8 supervisor and are shutdown-owned by it. No fourth
deferred capability is present. `catalog_refresh`, retention, checkpoint, and
other tasks are already registered M8/R008 capabilities, not M9 deferrals.

### Update

`version` prints the installed version. `update` compares the current version
with latest metadata, accepts exact versions with or without `v`, treats an
invalid or missing exact release as an explicit error, and supports `--check`
without replacement. Current Python distribution command construction is
historical evidence: source refuses exact targeting; pip/pipx/uv-tool select
their respective package-manager command. Rust may use a verified staged
artifact backend under M9; the user-visible guarantees are no unsafe partial
replacement, no config/database overwrite, and restart only when a running
server was present before an applied update.

### Deployment and uninstall

Snippet generation is deterministic for fixed inputs. Personal systemd units
use the resolved user/config/data/env paths; production units use the dedicated
`eggpool` user and `/etc/eggpool`, `/var/lib/eggpool`, `/var/log/eggpool`, and
`/var/backups/eggpool`. Watchdog cron is marked and idempotent; backup cron is
separate; logrotate is rendered without secrets. Fake systemctl/cron/logrotate
observations are the only O001 installation evidence. Root, confirmation,
ownership, and mode checks are explicit; no host service manager is touched.
Uninstall keep flags preserve the selected data/config/path/artifacts and
remove only EggPool-managed entries.

## Boundaries for later plans

O002 owns the reusable runtime-path/PID/control substrate. O003 owns lifecycle
handlers and presentation. O004/O005 own mutations and integrations. O006
owns DB/backup/recovery and automatic backup. O007 owns inspection/stats and
metrics flush. O008 owns update/version and update checker. O009 owns deploy
and uninstall. O010 owns integrated M9 qualification. M10 remains broad
platform/SBC/live-provider qualification, and M11 remains public Rust
distribution cutover.

The complete structured source for this contract is:

- `fixtures/operations/o001-fixture-matrix.json` — 63-command ownership and
  exact option matrix plus safety case inventory;
- `fixtures/operations/o001-python-observations.json` — deterministic scalar
  Python observations and the three-task R008 projection;
- `tests/migration_rs/operations_fixtures.py` and
  `tests/migration_rs/test_o001_operations.py` — executable guards.
