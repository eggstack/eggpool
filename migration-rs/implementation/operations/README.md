# M9 Operational CLI Implementation Plans

Status: active; O009 dependency-ready

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

These plans implement M9 only. They compose the closed M4-M8 Rust services into operational CLI and local lifecycle workflows. They do not authorize M10 broad qualification, M11 Rust-default cutover, or M12 Python retirement.

## Sequence

1. [O001 — Operational CLI contract and deterministic oracle freeze](001-operational-cli-contract-and-oracle-freeze.md) — **closed**.
2. [O002 — Local control, runtime paths, and process-state boundary](002-local-control-runtime-paths-and-process-state.md) — **closed**.
3. [O003 — Serve/daemon, lifecycle control, rehash, status, and watchdog commands](003-process-lifecycle-control-and-watchdog-commands.md) — **closed**.
4. [O004 — Config/key/provider onboarding and live-apply mutations](004-config-key-provider-onboarding-and-live-apply.md) — **closed**.
5. [O005 — Agent integration and `configsetup` generation](005-agent-integration-config-generation.md) — **closed**.
6. [O006 — Migrations, DB maintenance, backup/recover, and automatic backup](006-database-backup-recovery-and-automatic-backup.md) — **closed; 6af52fb**.
7. [O007 — Operator inspection, model/stats maintenance, and metrics flush](007-operator-inspection-maintenance-and-metrics-flush.md) — **closed**.
8. [O008 — Update/version resolution and update-checker background task](008-update-version-and-update-checker.md) — **closed**.
9. [O009 — Deployment artifacts, systemd/cron/logrotate, and uninstall](009-deployment-install-artifacts-and-uninstall.md) — **dependency-ready; O008 closure accepted**.
10. [O010 — Differential qualification and M9 closure](010-differential-qualification-and-m9-closure.md) — queued behind O009.

Only `migration-rs/registry.md` authorizes implementation. O009 is the current dependency-ready plan; O010 remains queued behind its direct predecessor.

## Hard boundaries

- F003 owns the command/option shape; M9 implements handlers behind it.
- M8 owns runtime generation/reload/shutdown/task machinery; M9 adapts it to operators.
- No Python fallback from Rust commands.
- No second scheduler, second provider HTTP stack, public control port, ORM, workflow engine, or DI framework.
- No Rust-only schema fork.
- M10 owns broad OS/SBC/live-provider characterization.
- M11 owns switching public install/releases to Rust by default.
- M12 owns removing Python production packaging/reference machinery.

## Closure discipline

Each accepted plan writes `migration-rs/closure/operations/<NNN>-status.md` with implementation commit(s), exact verification evidence, unresolved findings, and the registry transition it authorizes. Later defects get new corrective plans; historical closure records are append-only.
