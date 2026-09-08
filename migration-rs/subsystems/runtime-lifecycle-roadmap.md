# M8 Runtime Generations, Rehash, Background Tasks, and Process Lifecycle Roadmap

Status: closed after R013 corrective pass; M9 eligible for separate planning/implementation review

Repository baseline for original M8 planning: `e2be716018c365030ab06e648af71ed7588d9ad3` (accepted C011 / M7 closure).

Current corrective baseline: `a4495488d9071efa22eb7eff6447e6298f85d73d` (historical R012 re-closure before post-close audit).

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, accepted ADR-0001 through ADR-0003, the closed M4-M7 roadmaps, and append-only M8 closure records.

## Purpose

M8 replaces Python/Granian generation and process-lifecycle machinery with a smaller Rust-native runtime without weakening safe live rehash. It owns immutable generation snapshots, linearizable request leases, atomic generation publication, candidate abort/retirement, live config classification and rehash, process/background task ownership, startup recovery scheduling, graceful/forced shutdown, process-owned live wire-policy authority, and runtime/reload diagnostics.

The Python runtime is a behavioral oracle, not a structural template. Rust preserves observable lifecycle and safety invariants using `Arc`, `ArcSwap`, Tokio tasks/signals, SQLite transactions, and narrow synchronization rather than porting Python's manager/reload modules line-for-line.

## Ownership boundary

### Process-owned

The process lifetime owns resources that survive generation swaps:

- primary SQLite `Database` and repositories;
- `RuntimeManager` and reload serialization state;
- `ModelRouterAffinity`;
- exactly one shared `WireResolver`, including bounded learned/rejected state and accepted live policy;
- process task supervisor and task-spec registry;
- C010 crash reconciler entry point/startup report;
- reload diagnostics/last-result metadata;
- listener/server constructor state whose fields are `RESTART_REQUIRED`;
- future M9 control/CLI adapters, not implemented by M8.

### Generation-owned

Each immutable generation owns:

- validated `Config` snapshot, content digest and generation id;
- M7 `InferenceState` and finite/streaming coordinators;
- provider/account client pool and generation transport configuration;
- account/catalog/routing/quota/health state embedded in M5/M7 graph;
- compiled model-router registry and immutable routing facts;
- generation-scoped finalization supervisor and closeable resources.

A generation may not mutate another generation's service graph. Process-owned caches may be shared only when their entries are revalidated against generation-specific structure/fingerprints.

## M8 invariants

1. **One generation per request lifetime.** Finite requests and streams retain one generation through their accepted lifetime and terminal-registration handoff.
2. **Linearizable acquisition/publication.** No lease enters the old generation after publication commit; no request observes half-published state.
3. **Immutable request authority.** Await-capable handlers use their acquired generation for live generation-owned values.
4. **Candidate isolation.** Candidate construction cannot change active runtime, durable config-derived rows, process task schedule, or process wire policy.
5. **Abort closes candidate ownership.** Failed/cancelled candidates close exactly once through explicit async cleanup.
6. **Reload is fail-closed.** Invalid, stale, mixed/restart-required, backlog or preflight failures leave old accepted authority unchanged.
7. **No old/new mixing.** Runtime generation, durable config-derived state, task specs and process wire policy must cross acceptance coherently.
8. **Retirement preserves accepted work.** Live rehash never force-closes active leases/retained finalization merely to retire faster.
9. **Retirement is bounded.** Unresolved retiring slots have a small cap and block further reload rather than grow without bound.
10. **M7 terminal work drains before transport close.** Retirement waits for request leases and retained finalization convergence.
11. **Process tasks are singleton and non-overlapping.** Generation-dependent ticks acquire one active generation per tick.
12. **Reload transactions are serialized.** At most one reload owns candidate/publication state at a time.
13. **Publication gate is narrow.** Parsing, validation, diff, candidate construction and preflight occur before admission closes.
14. **Restart-required fields never partially apply.** Mixed diffs reject before publication.
15. **Shutdown is monotonic.** Once shutdown begins, new publication is refused; accepted work gets bounded drain before forced close.
16. **Crash recovery never replays provider work.** M8 schedules C010 durability reconciliation only.
17. **Diagnostics are bounded and secret-free.** No credentials/raw bodies/full secret-bearing config.
18. **No Rust-only schema fork.** Existing SQLite schema remains Python-readable.
19. **Wire-policy validation matches Python.** Accepted Rust `WireNegotiationConfig` ranges equal Python's contract and conversion cannot panic on invalid programmatic input.
20. **Rejected wire policy is never externally authoritative.** Preparing/staging a candidate policy is reversible and non-visible until coherent durable acceptance.
21. **Wire-policy rollback restores bounds immediately.** Old policy and old bounded state are true when rollback returns.
22. **Retained reload diagnostics outlive callers.** Caller cancellation/`Busy` overlap cannot strand or falsely clear the real transaction's `reload_in_progress` state.

## Implementation and corrective sequence

```text
M7 C011 closed
   |
   v
R001 runtime/reload contract + deterministic oracle freeze
 -> R002 process runtime + generation factory + candidate ownership
 -> R003 active manager + ArcSwap publication + request/stream leases
 -> R004 retirement + retained-finalization drain + resource close
 -> R005 config diff/reload policy + redacted change model
 -> R006 process task supervisor + authoritative task-spec staging
 -> R007 transactional live rehash + persistence/task/runtime commit
 -> R008 generation-leased maintenance/recovery/background integration
 -> R009 server startup, signals, graceful/forced shutdown
 -> R010 active-generation authority + diagnostics
 -> R011 integrated differential qualification / initial M8 closure
 -> R012 process wire-policy authority + retained reload diagnostics / historical re-closure
 -> R013 exact wire bounds + coherent policy acceptance + real inference requalification
   |
   v
M9 planning eligibility only after accepted R013 closure
```

Only the dependency-ready table in `../registry.md` authorizes implementation. No M8 plan is currently dependency-ready because R013 has closed the milestone.

## Structural design

The accepted runtime surface remains small:

- `ProcessRuntime`: DB, process-owned affinity/wire state, task supervisor, reload/diagnostic state.
- `RuntimeGeneration`: immutable generation snapshot and closeable resources.
- `PreparedGeneration`: candidate owner that can abort or transfer ownership.
- `GenerationSlot`: lifecycle/lease/retirement metadata.
- `GenerationLease`: synchronous local lease counter over an `Arc<RuntimeGeneration>`.
- `RuntimeManager`: `ArcSwap` active pointer plus narrow publication gate/lock, retiring slots and shutdown state.
- `ReloadPolicy`: exhaustive fail-closed config diff classification.
- `RuntimeTaskSupervisor`: one process-owned scheduler with staged spec diffs.
- `ReloadService`: serialized validate/diff/build/preflight/stage/commit/retire transaction.
- process `WireResolver`: one shared state machine with separately staged accepted policy.

`arc-swap` remains the only M8-specific runtime dependency. Do not add actor/lifecycle/workflow frameworks, a job queue, ORM, DI container, or second HTTP stack.

## Active-generation authority

Production handlers acquire live generation authority rather than capturing startup `InferenceState`/live config. Inference body limits are checked from the leased generation before buffering; readiness/account/catalog/routing reads use the active generation where behavior is live. Constructor-owned listener/auth/dashboard topology remains startup-owned only where R005 classifies it restart-required.

The process-owned wire resolver is intentionally different: it is shared across generations so compatible learned/rejected state can survive. Its **policy** is therefore part of the process acceptance transaction, not generation-local state.

## Reload acceptance boundary

The intended rehash transaction is:

1. serialize reload and snapshot expected active id/digest;
2. read/validate candidate config and expected digest;
3. compute redacted typed diff; no-op/restart/mixed/invalid fail before mutation;
4. build candidate generation and prepare durable/task/wire-policy deltas without changing accepted authority;
5. preflight staged task and wire-policy changes;
6. close the short admission gate and stage candidate generation against expected active id;
7. perform the bounded durable/runtime/task acceptance work;
8. only after the durable acceptance point, make the matching process wire policy authoritative while admission remains closed;
9. ensure any required post-commit fail-closed adoption keeps DB/runtime/task/wire policy coherent;
10. reopen admission, transfer candidate ownership, retire the old generation, and publish bounded result/diagnostics.

A reload that aborts before accepted publication may not be request-visible through the process wire resolver. No provider/network call belongs in the acceptance gate.

## Wire-policy contract

Rust follows Python's exact validated bounds:

- concurrency `1..=8`;
- negotiation interval `0..=1800s`;
- rejection cooldown `0..=1800s`;
- learned preference TTL `>0..=604800s` (the current Python source uses `gt=0`);
- cache entries `1..=65536`.

The conversion into runtime duration/capacity types must be non-panicking even for programmatically-constructed invalid values. Config parsing rejects invalid user values rather than clamping them silently.

An accepted live policy may affect subsequently executed process-shared wire resolution, including old-generation work that reaches the shared resolver after acceptance. An aborted policy may never affect accepted work.

## Background ownership

M8 keeps one process task supervisor. Generation-dependent callbacks acquire the active generation for each tick and release it before sleep. Startup crash reconciliation is one-shot. Deferred `metrics_flush`, `update_checker`, and `automatic_backup` capabilities remain explicit M9 work and do not run placeholders.

## M8/M9 boundary

M8 owns server-side live reload, shutdown, task/runtime status and diagnostic primitives. M9 owns user-facing operational transport and CLI: `rehash`, daemon/control socket, stop/restart/install/systemd/croncheck, backup/recover, update/version, packaging and related operator workflows.

M9 is eligible for its own separate planning/implementation review after accepted R013 closure. R013 does not implement M9 surfaces.

## Verification posture

Use deterministic local fixtures. No paid/live provider is a normal closure prerequisite. R013 includes a local deterministic provider and a real public inference request proving request-visible process wire-policy authority. `/healthz` or policy snapshots alone are not sufficient evidence.

M8 does not add broad OS/architecture CI; M10 owns system/SBC characterization.

## Historical closure notes

R011 remains historical aggregate evidence. Post-R011 audit found missing process wire-policy authority and caller-owned reload diagnostics, leading to R012.

R012 fixed those headline ownership mechanisms, but post-R012 audit found:

- Rust omitted Python's upper bounds and could panic on a huge finite duration;
- candidate process wire policy became authoritative before durable acceptance;
- rollback did not prove immediate restoration of old bounds;
- the claimed production request-path evidence used `/v1/healthz`, not M7 inference;
- the subsystem-roadmap file itself was accidentally replaced by registry content and is restored by the R013 planning pass.

These findings make R011/R012 historical closure evidence rather than current M8 closure authority.

## M8 closure

The accepted R013 closure re-closes M8 and records proof of:

- exact Python wire-policy bounds and non-panicking invalid conversion;
- no request-visible candidate policy before coherent durable acceptance;
- all rejected/aborted pre-accept reloads preserve old DB/runtime/task/wire authority throughout;
- post-commit fail-closed adoption cannot mix authorities;
- rollback immediately restores old policy and bounds;
- real Axum/M7 inference observes an accepted live wire-policy change and cannot observe a rejected one;
- retained reload diagnostics converge under cancellation, Busy overlap, failure and shutdown;
- R003/R005/R007/R009/R010/R011/R012 and applicable M7 wire-resolution regressions remain green;
- no schema fork, M9 scope creep, new architecture framework, secret leak, or unresolved high/medium M8 issue remains.

R013 is accepted and closes M8. M9 is eligible for separate planning/implementation review; no M9 plan is created or promoted automatically.
