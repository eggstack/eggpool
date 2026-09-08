# M8 Runtime Lifecycle Implementation Plans

Status: active; R010 dependency-ready

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

These plans implement M8 only. They do not authorize M9 operational CLI/control/daemon work.

## Sequence

1. [R001 — Runtime/reload contract and deterministic oracle freeze](001-runtime-reload-contract-and-oracle-freeze.md)
2. [R002 — Process runtime, generation factory, and candidate ownership](002-process-runtime-generation-factory-and-candidate-ownership.md)
3. [R003 — Active generation manager, atomic publication, and request leases](003-active-generation-manager-publication-and-leases.md)
4. [R004 — Retirement, retained finalization drain, and resource close](004-generation-retirement-finalization-drain-and-close.md)
5. [R005 — Config diff, reload policy, and redacted change model](005-config-diff-reload-policy-and-redaction.md)
6. [R006 — Process task supervisor and authoritative task-spec staging](006-process-task-supervisor-and-task-spec-staging.md)
7. [R007 — Transactional live rehash and coherent acceptance](007-transactional-live-rehash-and-coherent-acceptance.md)
8. [R008 — Generation-leased maintenance, recovery, and background integration](008-generation-leased-maintenance-recovery-and-background.md)
9. [R009 — Server startup, signals, graceful drain, and forced shutdown](009-server-startup-signals-and-shutdown.md)
10. [R010 — Active-generation authority audit and runtime/reload diagnostics](010-active-generation-authority-and-diagnostics.md)
11. [R011 — Differential qualification and M8 closure](011-differential-qualification-and-m8-closure.md)

Only `migration-rs/registry.md` authorizes implementation. R001-R007 are
closed, R010 is the sole current dependency-ready plan, and R011 remains
queued behind R010.

## Hard boundaries

- M7 coordinator semantics are closed and are composed, not redesigned.
- `InferenceState` remains the generation request-service graph.
- No Rust-only DB schema is introduced.
- No daemon/control socket/`eggpool rehash` CLI is implemented here; M9 consumes M8's reload API.
- No broad platform/release CI matrix is added; M10 owns that work.
- Candidate construction, publication, retirement, tasks, and shutdown must remain bounded and secret-free.

## Closure discipline

Each accepted plan writes `migration-rs/closure/runtime-lifecycle/<NNN>-status.md`. A later defect gets a new corrective plan; historical closure records are never rewritten.

R011 is the only plan allowed to mark M8 closed or make M9 eligible for its separate planning/implementation review.
