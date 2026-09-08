# M8 Runtime Lifecycle Handoff Sequence

Status: closed after R011; M9 eligible for separate planning/implementation review

Execute and accept in this order:

1. R001 — freeze the runtime/reload/task/shutdown oracle and ownership matrix (**closed**).
2. R002 — create process-owned shared state, one generation factory, and explicit candidate ownership (**closed**).
3. R003 — add `ArcSwap` active publication, linearizable generation leases, and hold leases for full finite/stream lifetimes (**closed**).
4. R004 — add retirement state, M7 retained-finalization drain, close ordering, and retirement-backlog bounds (**closed**).
5. R005 — port exhaustive fail-closed config reload classification, typed diffs, and secret redaction (**closed**).
6. R006 — build one process task supervisor and staged authoritative task-spec diffs (**closed**).
7. R007 — implement serialized transactional rehash across candidate, SQLite config-derived state, task specs, and active publication (**closed**).
8. R008 — wire generation-dependent maintenance/background ticks through active-generation leases and schedule C010 startup recovery at the correct process boundary (**closed**).
9. R009 — own server startup/shutdown, signal handling, graceful drain, forced close, and reload/shutdown exclusion (**closed**).
10. R010 — eliminate stale startup-generation authority from handlers and expose bounded secret-free runtime/reload diagnostics (**closed**).
11. R011 — run integrated Python/Rust differential, concurrency, fault, leak, reload, and shutdown qualification; close M8 (**closed**).

## Rules that apply to every handoff

- One finite request or stream remains on one generation for its accepted lifetime.
- The M7 response-start/no-replay and retained-finalization invariants cannot be weakened.
- No live rehash may force-close an old generation merely to finish retirement.
- Unknown or unclassified config changes are restart-required.
- Mixed live/restart-required diffs fail before candidate publication.
- No provider/network work occurs while the publication admission gate is closed.
- Background callbacks that need generation services acquire the active generation for the tick; they do not capture a stale `InferenceState`.
- Startup/static server state is allowed only for fields explicitly classified restart-required.
- M9 owns the user-facing reload/control/daemon CLI. M8 exposes the typed runtime/reload interfaces it will invoke.

R011 must audit the complete repository for direct startup `Config`/`InferenceState` authority before it can close M8.
