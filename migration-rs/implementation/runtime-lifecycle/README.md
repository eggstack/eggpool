# M8 Runtime Lifecycle Implementation Plans

Status: corrective pass active; R013 dependency-ready

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

These plans implement M8 only. They do not authorize M9 operational CLI/control/daemon work.

## Sequence

1. [R001 — Runtime/reload contract and deterministic oracle freeze](001-runtime-reload-contract-and-oracle-freeze.md) — closed.
2. [R002 — Process runtime, generation factory, and candidate ownership](002-process-runtime-generation-factory-and-candidate-ownership.md) — closed.
3. [R003 — Active generation manager, atomic publication, and request leases](003-active-generation-manager-publication-and-leases.md) — closed.
4. [R004 — Retirement, retained finalization drain, and resource close](004-generation-retirement-finalization-drain-and-close.md) — closed.
5. [R005 — Config diff, reload policy, and redacted change model](005-config-diff-reload-policy-and-redaction.md) — closed.
6. [R006 — Process task supervisor and authoritative task-spec staging](006-process-task-supervisor-and-task-spec-staging.md) — closed.
7. [R007 — Transactional live rehash and coherent acceptance](007-transactional-live-rehash-and-coherent-acceptance.md) — closed.
8. [R008 — Generation-leased maintenance, recovery, and background integration](008-generation-leased-maintenance-recovery-and-background.md) — closed.
9. [R009 — Server startup, signals, graceful drain, and forced shutdown](009-server-startup-signals-and-shutdown.md) — closed.
10. [R010 — Active-generation authority audit and runtime/reload diagnostics](010-active-generation-authority-and-diagnostics.md) — closed.
11. [R011 — Differential qualification and initial M8 closure](011-differential-qualification-and-m8-closure.md) — historical aggregate closure.
12. [R012 — Wire-negotiation runtime authority and reload-diagnostics re-closure](012-wire-negotiation-runtime-authority-and-reload-diagnostics-reclosure.md) — historical corrective closure after post-R012 audit.
13. [R013 — Wire-policy acceptance and boundary requalification](013-wire-policy-acceptance-and-boundary-requalification.md) — **ready for handoff**.

Only `migration-rs/registry.md` authorizes implementation. R013 is the sole dependency-ready M8 plan. M9 remains blocked until accepted R013 closure re-closes M8.

## Hard boundaries

- M7 coordinator semantics are closed and are composed, not redesigned.
- `InferenceState` remains the generation request-service graph.
- The process continues to own exactly one shared wire resolver.
- R013 may correct resolver-policy validation, staging/acceptance ordering, rollback bounds, and qualification; it must not create a second resolver or per-generation negotiation subsystem.
- No Rust-only DB schema is introduced.
- No daemon/control socket/`eggpool rehash` CLI is implemented here; M9 remains blocked.
- No broad platform/release CI matrix is added; M10 owns that work.
- Candidate construction, publication, retirement, tasks, shutdown, wire-policy reconfiguration, and reload diagnostics remain bounded and secret-free.

## Closure discipline

Each accepted plan writes `migration-rs/closure/runtime-lifecycle/<NNN>-status.md`. A later defect gets a new corrective plan; historical closure records are never rewritten.

R011 and R012 remain append-only historical evidence. Only accepted R013 closure may mark M8 closed again and restore M9 eligibility for its separate planning/implementation review; no M9 implementation plan is promoted automatically.