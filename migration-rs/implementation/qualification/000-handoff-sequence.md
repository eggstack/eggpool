# M10 Qualification Handoff Sequence

Status: corrective pass active; Q012 ready; M11 blocked

Execute and accept in this order:

1. Q001 — freeze qualification cells, target support, exact/semantic normalization, environment classes, and evidence schema (**accepted**).
2. Q002 — build and run the migration-wide deterministic Python/Rust qualification runner (**accepted**).
3. Q003 — qualify Python/Rust DB upgrade/rollback plus backup/recovery fault safety (**accepted**).
4. Q004 — qualify dashboard DOM/static/escaping/navigation and representative visual parity (**historical accepted closure; dashboard-state/content gaps corrected by Q012**).
5. Q005 — qualify supported-target build and ordinary non-root runtime portability (**accepted**).
6. Q006 — run real disposable rootful Linux/systemd/cron/logrotate/install/uninstall acceptance (**accepted**).
7. Q007 — run bounded opt-in live-provider finite/stream/cross-surface smoke (**original attempt blocked; corrective closure accepted by Q011**).
8. Q008 — run real Linux ARM64 SBC functional/resource characterization (**accepted by append-only re-acceptance**).
9. Q009 — run sustained deterministic failure/reload/streaming/resource-stability qualification (**accepted by append-only re-acceptance**).
10. Q010 — aggregate all M10 evidence and decide M11 planning readiness (**historical aggregate closure; superseded after dashboard audit**).
11. Q011 — close the Q007 live-provider blocker (**accepted**).
12. Q012 — execute populated/error dashboard semantic and visual requalification, refresh aggregate evidence, and re-close M10 if clean (**ready**).

## Rules for every handoff

- Current repository evidence must be inspected before changing code/tests.
- A qualification mismatch is a finding; do not loosen normalization merely to make it pass.
- High/medium findings block M10 closure and therefore block M11.
- Existing focused suites are reused wherever possible; M10 does not create a parallel replacement test architecture.
- Evidence is bounded and secret-free.
- No paid/live provider traffic runs without explicit opt-in and a frozen small request budget.
- No rootful acceptance runs on a production host.
- No physical-machine identifier is written to closure records.
- No broad always-on CI matrix is added without explicit justification.
- Dashboard fixtures must execute real populated/error/private states; `reserved` metadata is not closure evidence.
- Dashboard semantic comparison must preserve meaningful content and ordering.
- Screenshot coverage metadata does not count as an actual screenshot; Q012 must prove captures existed and were reviewed.
- No M11 release asset/public installer/default-update cutover is pulled forward.
- No M12 Python retirement work is pulled forward.

Only `migration-rs/registry.md` authorizes implementation. Q012 is the sole dependency-ready plan. Q004/Q010 remain historical closure evidence, Q011 remains accepted live-provider evidence, and M11 is blocked until Q012 is accepted.