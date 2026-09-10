# M10 Qualification Implementation Plans

Status: corrective pass active; Q012 dependency-ready; M11 blocked

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

These plans implement M10 only. They qualify the closed Rust implementation from M4-M9 and do not authorize M11 public cutover or M12 Python retirement.

## Sequence

1. [Q001 — Qualification contract, target matrix, and evidence schema freeze](001-qualification-contract-target-matrix-and-evidence-freeze.md) — **complete**; [closure](../../closure/qualification/001-status.md).
2. [Q002 — Migration-wide deterministic differential qualification runner](002-migration-wide-differential-qualification-runner.md) — **complete**; [closure](../../closure/qualification/002-status.md).
3. [Q003 — Database upgrade, rollback, backup, and recovery compatibility](003-database-upgrade-rollback-backup-recovery-compatibility.md) — **complete**; [closure](../../closure/qualification/003-status.md).
4. [Q004 — Dashboard SSR, DOM, static asset, and visual parity review](004-dashboard-dom-static-and-visual-parity.md) — **historical accepted closure; superseded for dashboard-state/content findings by Q012**.
5. [Q005 — Supported-target build and non-root runtime portability](005-supported-target-build-and-runtime-portability.md) — **accepted**; [closure](../../closure/qualification/005-status.md).
6. [Q006 — Disposable rootful Linux operational acceptance](006-rootful-linux-operational-acceptance.md) — **accepted**; [closure](../../closure/qualification/006-status.md).
7. [Q007 — Bounded live-provider interoperability smoke](007-live-provider-interoperability-smoke.md) — **original attempt blocked; corrective closure accepted by Q011**.
8. [Q008 — ARM64 SBC functional and resource characterization](008-arm64-sbc-functional-and-resource-characterization.md) — **accepted by append-only re-acceptance**.
9. [Q009 — Sustained failure, reload, streaming, and resource-stability qualification](009-sustained-failure-reload-stream-resource-stability.md) — **accepted by append-only re-acceptance**.
10. [Q010 — Aggregate M10 closure and M11 readiness report](010-aggregate-m10-closure-and-m11-readiness.md) — **historical aggregate closure; superseded for current M10 closure authority by Q012**.
11. [Q011 — Q007 live-provider corrective closure](011-q007-live-provider-corrective-closure.md) — **accepted**; [closure](../../closure/qualification/011-status.md).
12. [Q012 — Dashboard state, semantic content, and visual requalification](012-dashboard-state-semantic-content-and-visual-requalification.md) — **ready for handoff**.

Only `migration-rs/registry.md` authorizes implementation. Q012 is the sole dependency-ready M10 plan. M11 is re-blocked until accepted Q012 closure re-closes M10.

## Hard boundaries

- Python remains the behavioral oracle through M10.
- No Rust-default public installation/release/update cutover occurs in M10.
- No Python production/runtime removal occurs in M10.
- Normal CI remains intentionally small unless a narrow deterministic regression is justified.
- Live-provider qualification remains opt-in and bounded; credentials never enter repository evidence.
- Rootful Linux qualification remains disposable-only.
- The accepted physical ARM64 SBC and live-provider evidence remains valid unless Q012 changes a surface that invalidates freshness.
- Dashboard qualification preserves the existing product design; Q012 may correct parity defects but may not redesign pages or introduce a frontend framework.
- Q012 must compare populated/error semantic content and actual screenshot artifacts, not merely route shells or planned screenshot filenames.
- Any failed closure creates a new corrective Q-plan rather than rewriting history.

## Closure discipline

Each accepted plan writes `migration-rs/closure/qualification/<NNN>-status.md` with exact commands, candidate/environment identity, findings, evidence hashes or bounded summaries, and registry transition.

Q004, Q010, and Q011 historical closure records remain append-only. Q012 is the current M10 corrective closure authority. Only accepted Q012 may mark M10 closed again and restore M11 eligibility for its separate planning review.