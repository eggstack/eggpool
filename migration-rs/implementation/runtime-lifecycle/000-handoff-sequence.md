# M8 Runtime Lifecycle Handoff Sequence

Status: closed after R013 corrective pass; M9 eligible for separate planning/implementation review

Execute and accept in this order:

1. R001 — freeze runtime/reload/task/shutdown oracle and ownership matrix (**closed**).
2. R002 — process-owned shared state, generation factory, candidate ownership (**closed**).
3. R003 — `ArcSwap` active publication and linearizable generation leases (**closed**).
4. R004 — retirement, M7 retained-finalization drain, close ordering and bounds (**closed**).
5. R005 — exhaustive fail-closed reload classification and redaction (**closed**).
6. R006 — singleton process task supervisor and staged task-spec diffs (**closed**).
7. R007 — serialized transactional rehash across SQLite/runtime/task authority (**closed**).
8. R008 — generation-leased maintenance/background work and startup recovery (**closed**).
9. R009 — startup/signals/graceful and forced shutdown (**closed**).
10. R010 — active-generation authority audit and bounded diagnostics (**closed**).
11. R011 — integrated M8 qualification (**historical aggregate closure**).
12. R012 — process wire-policy authority and retained reload diagnostics (**historical corrective closure; post-close audit found remaining acceptance/validation/evidence gaps**).
13. R013 — exact wire-policy bounds, coherent acceptance/rollback, real inference qualification and boundary requalification (**closed**).

## Rules that apply to every handoff

- One finite request or stream remains on one generation for its accepted lifetime.
- M7 response-start/no-replay and retained-finalization invariants cannot be weakened.
- Live rehash never force-closes accepted old-generation work merely to retire faster.
- Unknown/unclassified config changes are restart-required; mixed live/restart-required diffs fail closed.
- Candidate state cannot become externally authoritative before the coherent acceptance boundary.
- No provider/network operation belongs inside the publication admission gate except deterministic local test fixtures exercising already-built request paths.
- Background callbacks requiring generation services acquire the active generation per tick.
- Startup/static server state is allowed only for fields classified restart-required.
- The process owns exactly one shared wire resolver. Its accepted policy must match validated config and rejected reloads must never be request-visible.
- Wire-policy rollback restores old policy and bounds immediately.
- Reload diagnostic `in_progress` ownership follows the retained reload transaction, not the calling future.
- M9 owns user-facing reload/control/daemon/update/deploy surfaces and remains outside the closed M8 scope.

Only `migration-rs/registry.md` authorizes implementation. R013 is accepted and re-closes M8. M9 is eligible for a separate planning/implementation review; it must not be auto-promoted from this closure.
