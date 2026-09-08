# M9 Operational CLI, Lifecycle, Update, and Deployment Roadmap

Status: active planning; O002 dependency-ready

Repository baseline: `e3edd5bc61b0718bc4559b85d30c27819e708350` (accepted R013 / M8 re-closure).

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, accepted ADR-0001 through ADR-0003, F003 CLI/config closure, closed M4-M8 subsystem roadmaps, and accepted R013 closure.

## Purpose

M9 turns the already-frozen Rust command parser into a complete operator-facing EggPool binary. It composes the closed Rust provider/routing/wire/coordinator/runtime capabilities into CLI, local process-control, backup/update, maintenance, deployment, and onboarding workflows without reopening M4-M8 architecture.

Python remains the behavioral oracle until M11 cutover. M9 preserves command names/options/exit classes, filesystem locations, backup semantics, deployment safety checks, secret handling, and observable operational behavior. It does not mechanically port the 167k-line Python CLI implementation; command handlers should be thin adapters over narrow reusable Rust services.

## Current baseline

F003 already represents the complete Python Click command tree in `rust/src/cli.rs`, but the runtime dispatcher implements only `version`, `check-config`, and foreground `serve --verbose`; nearly every other command returns migration-stage `NotImplemented`.

M8 exposes stable server-side authority for startup, reload, active-generation snapshots, task supervision, diagnostics, and shutdown. R008 intentionally left exactly three background capabilities unregistered for M9:

- `metrics_flush`;
- `update_checker`;
- `automatic_backup`.

M9 owns those business capabilities and their task registration. It must reuse the single M8 task supervisor rather than creating another scheduler.

## M9 invariants

1. **Frozen command shape.** F003/`contract-inventory.md` remains the command/option authority unless current Python intentionally changed and O001 records the delta.
2. **Thin CLI adapters.** Parsing/presentation live at the edge; config, control, backup, update, deployment, and inspection logic is reusable without Clap.
3. **No Python fallback.** A Rust command must execute Rust behavior or fail explicitly; it may not shell out to `python -m eggpool` as a compatibility crutch.
4. **No lifecycle fork.** `serve`, rehash, shutdown, runtime status, and background work consume M8 APIs; M9 does not build a second runtime manager/scheduler.
5. **One local control authority.** The Unix local-control channel is bounded, versioned, local-only, permissioned, one-request/one-response, and does not expose secrets or public TCP control endpoints.
6. **PID/process safety.** PID files are advisory lifecycle state, validated against live process/health observations, atomically written where practical, and stale files are recoverable without manual DB reset.
7. **No replay on restart.** Stop/restart/update/deploy never replay unknown in-flight provider requests; they use M8 graceful shutdown and C010 startup reconciliation.
8. **Fail-closed mutation.** Config/provider/key/restore/deploy/update mutations validate before destructive steps and either converge or report explicit partial-state recovery instructions.
9. **Secret discipline.** API/proxy credentials are never logged or included in diagnostics/backups unless the existing backup contract explicitly requires a protected local file; display requires the existing explicit command semantics such as `getkey`, `newkey`, or `--print-secret`.
10. **Backup is local and bounded.** Backup/restore operate on reviewed EggPool-owned paths and archive members; no arbitrary path traversal, symlink escape, or unbounded archive extraction.
11. **Update is atomic.** Download/verification/staging cannot replace the running executable until a compatible target is validated; failed replacement leaves the old executable/config/database usable.
12. **Deployment is local/SBC scale.** Systemd, cron, logrotate, path, ownership, and uninstall helpers stay direct and auditable; no fleet/orchestration framework.
13. **Background singleton ownership.** `metrics_flush`, `update_checker`, and `automatic_backup` register only after real callbacks exist and remain singleton/non-overlapping through the M8 supervisor.
14. **No schema fork.** Migrations and maintenance consume the existing numbered SQL/checksum set and repositories.
15. **M10 remains M10.** M9 uses deterministic local Linux/Unix qualification. Broad OS/architecture matrices, live-provider smoke, dashboard visual review, and SBC resource characterization remain M10.
16. **M11 remains cutover.** M9 may prepare Rust-capable install/update/deployment behavior, but does not make Rust the canonical public install/release path or remove Python production packaging.

## Full command ownership map

### Process lifecycle/control

`serve`, `stop`, `restart`, `rehash`, `runtime-status`, `croncheck`, `ensure-running`, `version`, `help`.

### Configuration/provider onboarding

`check-config`, `edit`, `getkey`, `newkey`, `init-config`, `set`, `onboard`, `connect`, `connect list`, `logout`, `dashboard public`.

### Agent integration output

`configsetup opencode`, `claude-code`, `aider`, `codex`, `qwen-code`, `kilo`, `continue`, `cline`, `roo-code`, `goose`, `openhands`.

### Database/data safety

`migrate`, `db vacuum`, `backup`, `recover`.

### Operator inspection/maintenance

`accounts list`, `accounts status`, `accounts explain`, `models refresh`, all `modelinfo` subcommands, and all `stats` subcommands.

### Update/deployment/removal

`update [VERSION]`, `deploy systemd`, `deploy cron`, `deploy backup-cron`, `deploy logrotate`, `deploy all`, and `uninstall`.

All root commands retain global `--config` resolution semantics.

## Implementation sequence

```text
M8 R013 accepted
  |
  v
O001 operational contract + oracle freeze
 -> O002 local control/runtime-path/process-state boundary
 -> O003 serve/daemon/stop/restart/rehash/status/watchdog lifecycle
 -> O004 config/key/provider onboarding and live-apply mutations
 -> O005 agent integration/configsetup generation
 -> O006 migrations/DB maintenance/backup/recover + automatic backup task
 -> O007 operator inspection/stats/model maintenance + metrics flush task
 -> O008 update/version/release resolution + update-checker task
 -> O009 deploy/systemd/cron/logrotate/install artifacts/uninstall
 -> O010 integrated differential qualification + M9 closure
  |
  v
M10 eligibility
```

Only `../registry.md` authorizes implementation. O001 is the sole dependency-ready plan at initial registration.

## Structural design

The expected M9 modules are narrow, names adjustable:

- `operations::control`: local socket protocol/client/server and typed results;
- `operations::process`: runtime paths, PID ownership, health/start/stop primitives;
- `operations::config_mutation`: atomic TOML edits, key/provider mutations, live-apply decision;
- `operations::integrations`: target-neutral integration context plus small target renderers;
- `operations::backup`: reviewed manifest, archive creation/validation/restore;
- `operations::inspection`: command projections over existing DB/catalog/routing/stats services;
- `operations::update`: release/version resolution, artifact selection/verification/staging;
- `operations::deploy`: systemd/cron/logrotate snippets and bounded install/uninstall file operations;
- CLI rendering functions that map typed results to Python-compatible stdout/stderr/exit classes.

Do not create an “operations framework,” plugin system, generic workflow engine, or internal localhost HTTP service.

## Local control boundary

Python currently uses a bounded Unix-domain socket for `reload_config`. Rust should preserve the observable local-control contract needed by `rehash` and any lifecycle operation explicitly proven to use it, while keeping public HTTP diagnostics separate.

The control listener is process-owned and starts/stops with the server lifecycle. It must:

- bind only a reviewed local filesystem socket path;
- reject oversized/malformed/multi-command frames;
- use a small versioned request/response schema and request id;
- set restrictive filesystem permissions;
- remove stale socket files only after proving no live owner;
- never carry API keys, request bodies, provider credentials, or arbitrary config content;
- retain M8 reload semantics rather than reimplementing reload inside the socket handler.

Do not add a general RPC crate unless O002 proves the standard-library/Tokio Unix socket surface cannot meet the contract.

## Daemon/process lifecycle

Foreground `serve --verbose` already composes the M8 server. M9 adds the Python-observable detached supervisor workflow, PID/log paths, duplicate-instance checks, root gating, stop/restart/watchdog behavior, and lifecycle presentation.

Detached mode should spawn the same Rust executable in foreground mode rather than create a second server implementation. Systemd continues to own foreground process lifecycle when deployed as a service.

`croncheck` must stay a cheap health/proc-state check. `ensure-running` may start only when not already running and must not create restart loops. Neither command may initialize the full provider runtime merely to decide whether the process exists.

## Configuration and live apply

Config mutations write only the resolved config/provider-template destinations. Use temp-file + fsync/rename semantics where supported for operator-owned file replacement; preserve permissions reasonably and fail before rename on invalid output.

Provider/account/key/config changes follow the existing live-apply policy:

- if the server is running and the change is live-reloadable, validate and request rehash;
- if control is unavailable, report that state and use the Python-defined restart fallback only where the current command does so;
- restart-required changes never masquerade as live-applied;
- a failed mutation does not silently rewrite unrelated TOML sections.

No general-purpose TOML document editor dependency is assumed; first evaluate the existing Rust config/template utilities and a small line/section-preserving editor for the currently supported mutations.

## Data safety and backup

M9 reuses F004 migrations and the current SQLite file. Backup/restore must freeze the Python archive/member/metadata contract in O001 before implementation. SQLite must be captured consistently (checkpoint/backup API as appropriate), not by racing a live WAL copy.

Restore validates archive paths, versions, manifest, destination ownership, and DB compatibility before replacing live state. A running server is stopped or otherwise put into a safe restore state according to the oracle. Recovery never deletes the only usable copy before the replacement is validated.

`automatic_backup` uses the same backup service and the M8 task supervisor; no separate cron-like Tokio loop is created.

## Metrics and operator inspection

Read/maintenance commands should call existing Rust repositories/services rather than reconstruct Python routing/catalog logic in CLI code. Network refresh commands may use existing provider/catalog clients and must preserve their bounded failure isolation.

`metrics_flush` must implement the existing configured write/coalescing semantics with bounded memory and one supervisor callback. Do not add OpenTelemetry or an external metrics datastore as part of parity work.

## Update boundary

M9 implements the Rust-side update mechanism and exact-version normalization, but M11 owns public Rust-default release cutover. The update core must be testable against a local fake release service/manifest and use the existing Hyper/Rustls stack or another already-present HTTP primitive rather than Reqwest.

The current Python semantics that matter remain: latest vs exact target, optional `v` normalization, explicit missing-version error, `--check`, no config/DB overwrite, and restart only if the service was running. The Rust artifact backend should stage a compatible artifact and verify release-provided integrity metadata before atomic replacement. If no compatible Rust asset exists during side-by-side migration, fail explicitly; do not fall back to modifying the Python environment from the Rust binary.

## Deployment/cutover boundary

M9 ports `deploy` and uninstall behavior for the Rust executable and prepares Rust-capable service snippets/install helpers. It does not flip `README`/`install.sh` to Rust-default distribution until M11.

M9 may update deployment docs to describe the Rust candidate and keep shared service paths stable. Broad binary release matrices and performance qualification remain M10/M11.

## Dependency posture

Expected implementation should fit the current dependency set. Before adding anything, prefer:

- Tokio Unix/process/fs primitives;
- Hyper/Rustls already present;
- rusqlite/tokio-rusqlite already present;
- serde/serde_json/toml already present;
- Clap already present;
- standard-library archive/checksum/process primitives where practical.

Potential archive implementation is the one area that may justify a small focused dependency if the Python backup format cannot be implemented safely with current crates. O006 must justify it explicitly rather than pulling a broad utility framework.

## Verification posture

Each plan adds focused deterministic tests plus targeted Python differential fixtures. External provider calls, root systemd mutation, and public release replacement are not normal unit-test prerequisites; use temporary directories, loopback servers, fake executables, fake `systemctl`/cron environments, and local release fixtures.

Rootful deployment should have a small disposable Linux acceptance check before M9 closure, but M10 owns the broader OS/SBC matrix.

## M9 closure

O010 may close M9 only when:

- every command in the F003 inventory has a real Rust implementation or an explicitly approved non-applicable platform outcome;
- migration-stage `NotImplemented` is unreachable for supported documented commands;
- help/parser/exit/output and mutation effects meet the frozen O001 corpus;
- daemon/stop/restart/rehash/runtime-status/watchdog flows compose M8 without stale authority or restart/replay defects;
- local control framing/permissions/cancellation/failure behavior is bounded and secret-free;
- config/provider/key mutations converge safely and live-apply/restart behavior matches the oracle;
- backup/recover/migrations/vacuum are fault-tested and preserve the existing schema/data contract;
- all three R008 deferred task capabilities are real, registered, singleton, non-overlapping, and reload-safe;
- operator inspection/model/stats/configsetup commands are functionally present and differential-qualified;
- update latest/exact/check/staging/failure behavior is complete without unsafe self-replacement;
- deploy/uninstall workflows are deterministic and safe on supported local Linux/Unix targets;
- no unresolved high/medium correctness, security, resource, compatibility, lifecycle, data-loss, or packaging finding remains;
- M10 is promoted only after accepted O010 closure.

M9 does not require Python retirement or Rust-default public distribution; those remain M11/M12.
