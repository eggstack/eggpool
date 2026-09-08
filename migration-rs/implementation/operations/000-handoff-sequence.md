# M9 Operational CLI Handoff Sequence

Status: active; O001 ready

Execute and accept in this order:

1. O001 — freeze exact operational CLI/filesystem/process/archive/update/deploy contracts and deterministic oracle corpus (**ready**).
2. O002 — implement local Unix control protocol/client/server, runtime paths, PID/process-state primitives (**queued**).
3. O003 — implement daemon serve, stop/restart, rehash, runtime-status, croncheck and ensure-running on O002/M8 (**queued**).
4. O004 — implement config/key/provider/onboarding mutations with validation and live-apply/restart decisions (**queued**).
5. O005 — implement all `configsetup` target generators and safe output/write/clipboard behavior (**queued**).
6. O006 — implement migrate/vacuum/backup/recover and register the real automatic-backup M8 task (**queued**).
7. O007 — implement accounts/models/modelinfo/stats/dashboard operator commands and register the real metrics-flush M8 task (**queued**).
8. O008 — implement latest/exact update resolution, staged verified replacement, version behavior, and update-checker M8 task (**queued**).
9. O009 — implement deploy systemd/cron/backup-cron/logrotate/all, reviewed Rust candidate install artifacts, and uninstall (**queued**).
10. O010 — run full M9 differential/fault/security/data-loss/deployment qualification and close M9 (**queued**).

## Rules for every handoff

- Python is the behavioral oracle; Rust command bodies do not call Python as a fallback.
- Preserve F003 command names/options/global `--config` behavior and EggPool-owned exit categories.
- Mutating commands validate before irreversible effects and retain actionable recovery state on partial external failures.
- Server lifecycle commands use M8 startup/reload/shutdown/drain APIs; never create a second runtime manager.
- Background business callbacks use the one M8 process task supervisor; no private timer loops.
- Local control is Unix-local, bounded, permissioned, versioned, secret-free and single-request/single-response.
- PID/socket/log paths follow current XDG/deploy-user authority and tolerate stale local files safely.
- No command may require DB reset to recover from an ordinary malformed config, stale PID/socket, failed provider response, failed backup/update, or client disconnect.
- Backup restore protects against traversal/symlink/archive-bomb style extraction and never destroys the only validated copy first.
- Update replacement is staged and verified; config/database are not overwritten by update.
- Deployment helpers stay local and auditable. Fake command environments cover most tests; rootful system acceptance is bounded.
- Secrets may be printed only on explicit existing surfaces whose contract requires it.
- No M10 broad matrix, M11 default-cutover, or M12 retirement work is pulled forward.

Only `migration-rs/registry.md` authorizes implementation. O001 is the sole ready plan.