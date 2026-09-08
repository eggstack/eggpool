# M9 Operational CLI Closure Records

This directory stores append-only closure evidence for M9 operational CLI, lifecycle, update, backup, deployment, and background capability work.

Expected records:

- `001-status.md` — O001 operational contract/oracle freeze
- `002-status.md` — O002 local control/runtime paths/process state
- `003-status.md` — O003 lifecycle/daemon/rehash/status/watchdog commands
- `004-status.md` — O004 config/key/provider onboarding/live apply
- `005-status.md` — O005 agent integration/configsetup generation
- `006-status.md` — O006 migrations/DB/backup/recover/automatic backup
- `007-status.md` — O007 operator inspection/maintenance/metrics flush
- `008-status.md` — O008 update/version/update checker
- `009-status.md` — O009 deployment/install artifacts/uninstall
- `010-status.md` — O010 differential qualification/M9 closure

Closure records must include implementation commit(s), exact verification commands/results, failing-before/passing-after evidence where applicable, dependency/schema/security review, unresolved findings, and the registry transition they authorize.

Only accepted O010 may close M9 and make M10 eligible for separate planning/implementation review. Later defects get new corrective O011+ plans; historical closure records are never rewritten.