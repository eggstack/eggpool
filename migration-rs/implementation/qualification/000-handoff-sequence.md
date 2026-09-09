# M10 Qualification Handoff Sequence

Status: active; Q002 ready

Execute and accept in this order:

1. Q001 — freeze qualification cells, target support, exact/semantic normalization, environment classes, and evidence schema (**accepted**).
2. Q002 — build and run the migration-wide deterministic Python/Rust qualification runner (**ready**).
3. Q003 — qualify Python/Rust DB upgrade/rollback plus backup/recovery fault safety.
4. Q004 — qualify dashboard DOM/static/escaping/navigation and representative visual parity.
5. Q005 — qualify supported-target build and ordinary non-root runtime portability.
6. Q006 — run real disposable rootful Linux/systemd/cron/logrotate/install/uninstall acceptance.
7. Q007 — run bounded opt-in live-provider finite/stream/cross-surface smoke.
8. Q008 — run real Linux ARM64 SBC functional and resource characterization.
9. Q009 — run sustained deterministic failure/reload/streaming/resource-stability qualification.
10. Q010 — aggregate all M10 evidence, close findings, and decide M11 planning readiness.

## Rules for every handoff

- Current repository evidence must be inspected before changing code/tests.
- A qualification mismatch is a finding; do not loosen normalization merely to make it pass.
- High/medium findings block predecessor closure and therefore block later expensive handoffs.
- Existing focused suites are reused wherever possible; M10 does not create a parallel replacement test architecture.
- Evidence is bounded and secret-free.
- No paid/live provider traffic runs without explicit opt-in and a frozen small request budget.
- No rootful acceptance runs on a production host.
- No physical-machine identifier is written to closure records.
- No broad always-on CI matrix is added without explicit Q001/Q002 justification.
- No M11 release asset/public installer/default-update cutover is pulled forward.
- No M12 Python retirement work is pulled forward.

Only `migration-rs/registry.md` authorizes implementation. Q002 is now the sole ready plan; Q003-Q010 remain queued behind their direct predecessors.
