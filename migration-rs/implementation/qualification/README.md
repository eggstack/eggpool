# M10 Qualification Implementation Plans

Status: active; Q007 blocked on live-provider evidence

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

These plans implement M10 only. They qualify the closed Rust implementation from M4-M9 and do not authorize M11 public cutover or M12 Python retirement.

## Sequence

1. [Q001 — Qualification contract, target matrix, and evidence schema freeze](001-qualification-contract-target-matrix-and-evidence-freeze.md) — **complete**; [closure](../../closure/qualification/001-status.md).
2. [Q002 — Migration-wide deterministic differential qualification runner](002-migration-wide-differential-qualification-runner.md) — **complete**; [closure](../../closure/qualification/002-status.md).
3. [Q003 — Database upgrade, rollback, backup, and recovery compatibility](003-database-upgrade-rollback-backup-recovery-compatibility.md) — **complete**; [closure](../../closure/qualification/003-status.md).
4. [Q004 — Dashboard SSR, DOM, static asset, and visual parity review](004-dashboard-dom-static-and-visual-parity.md) — **accepted**; [closure](../../closure/qualification/004-status.md).
5. [Q005 — Supported-target build and non-root runtime portability](005-supported-target-build-and-runtime-portability.md) — **accepted**; [closure](../../closure/qualification/005-status.md).
6. [Q006 — Disposable rootful Linux operational acceptance](006-rootful-linux-operational-acceptance.md) — **accepted**; [closure](../../closure/qualification/006-status.md).
7. [Q007 — Bounded live-provider interoperability smoke](007-live-provider-interoperability-smoke.md) — **blocked; see closure**.
8. [Q008 — ARM64 SBC functional and resource characterization](008-arm64-sbc-functional-and-resource-characterization.md) — queued behind Q007.
9. [Q009 — Sustained failure, reload, streaming, and resource-stability qualification](009-sustained-failure-reload-stream-resource-stability.md) — queued behind Q008.
10. [Q010 — Aggregate M10 closure and M11 readiness report](010-aggregate-m10-closure-and-m11-readiness.md) — queued behind Q009.

Only `migration-rs/registry.md` authorizes implementation. Q007 remains the active plan but is blocked by missing live interoperability evidence; later plans are promoted only by accepted closure of their direct predecessor.

## Hard boundaries

- Python remains the behavioral oracle through M10.
- No Rust-default public installation/release/update cutover occurs in M10.
- No Python production/runtime removal occurs in M10.
- Normal CI remains intentionally small unless Q001/Q002 justify a narrowly selected addition.
- Live-provider qualification is opt-in and bounded; credentials never enter repository evidence.
- Rootful Linux qualification runs only on disposable systems.
- Physical ARM64 SBC evidence is mandatory for M10 closure.
- Performance/resource work characterizes the candidate and catches leaks/unbounded behavior; it does not invent unsupported SLAs.
- Dashboard qualification preserves the existing design.
- Any failed closure creates a new corrective Q-plan rather than rewriting history.

## Closure discipline

Each accepted plan writes `migration-rs/closure/qualification/<NNN>-status.md` with exact commands, candidate/environment identity, findings, evidence hashes or bounded summaries, and registry transition.

Q010 alone may close M10 and make M11 eligible for a separate planning review.
