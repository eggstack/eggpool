# Plan 077 — SBC Lifecycle Simplification and Runtime Correctness Roadmap

Date: 2026-08-05
Status: ready for implementation
Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

Implementation plans:

- `plans/078-runtime-invariant-and-request-boundary-corrections.md`
- `plans/079-quarantine-durability-and-generation-publication.md`
- `plans/080-generation-finalization-ownership-alignment.md`
- `plans/081-terminal-ownership-consolidation.md`
- `plans/082-database-fail-closed-simplification.md`
- `plans/083-lean-defaults-and-conditional-subsystem-construction.md`
- `plans/084-legacy-path-dependency-and-ci-pruning.md`
- `plans/085-sbc-runtime-measurement-and-roadmap-closure.md`

## Purpose

Preserve EggPool's intended product: a lightweight, LAN-hosted proxy that aggregates multiple AI provider accounts behind one OpenAI- and Anthropic-compatible endpoint, performs quota-aware routing, survives ordinary provider/client failures, and runs predictably on Raspberry Pi and similar SBC hardware.

The implementation through the planning baseline has closed the original cascade-failure class. Provider validation errors are request-local, retry eligibility is attempt-scoped, temporary suppressions are bounded to 30 minutes, stream handoff is explicit, startup reconciliation repairs prior-process state, and the CI surface is already one small smoke job.

The next line of work is deliberately reductive. EggPool now has more lifecycle and recovery machinery than its local single-worker deployment requires. The goal is to correct the remaining concrete defects, establish one truthful ownership model, remove duplicate terminal/recovery paths, make lightweight behavior the normal installation shape, and delete compatibility scaffolding that no longer protects a supported use case.

This roadmap is not a production-hardening expansion. It must reduce state space, background activity, optional subsystem construction, and verification burden while preserving routing correctness, protocol compatibility, live rehash for supported fields, crash recovery, and current client behavior.

## Design center

EggPool is designed for:

- one supervised process on a private host or SBC;
- one Granian event-loop thread;
- SQLite WAL persistence;
- a small number of provider accounts and upstream hosts;
- moderate concurrent coding-agent streams rather than public multi-tenant traffic;
- systemd restart as an acceptable response to an indeterminate local database state;
- bounded local diagnostics, not a production observability platform;
- focused smoke and fault-boundary tests, not exhaustive distributed-systems verification.

## Confirmed findings to address

### Correctness defects

1. `AttemptRuntimeLease.release_once()` can mark a lease released when an acquired component has no dependency object and therefore was never released.
2. `server.threads` accepts unsupported values greater than one despite known loop-bound lock hazards.
3. durable model-quarantine hydration can fail open by logging and publishing an empty in-memory quarantine.
4. authoritative model reappearance clears in-memory quarantine before the durable row, allowing stale suppression to return after restart.
5. finalization duplicate detection compares `repr()` rather than a bounded semantic identity.
6. runtime-generation abort diagnostics retain and log unredacted exception text despite claiming secret-safe output.
7. forwarded client-IP headers are trusted without a configured trusted-proxy boundary.
8. documentation still contains isolated wording that equates response handoff with first body bytes.

### Ownership and complexity findings

1. `RequestFinalizationSupervisor` is constructed per generation but described as process-owned.
2. generation retirement does not have one simple, explicit contract for retained terminal work that outlives a request waiter.
3. request finalization, failed-attempt cleanup, claim compensation, attempt finalization, and startup repair represent overlapping terminal ownership through separate progress models.
4. the database wrapper carries same-process recovery states and ambiguous-operation machinery beyond what a supervised local service needs.
5. optional observability, model-info, compression/cache analysis, and writer subsystems can still influence construction and background activity even when a lightweight profile disables them.
6. legacy test/embedder fallbacks and milestone scaffolding keep alternate production paths alive and force additional verification.

### Resource and verification findings

1. the explicit SBC example is lean, but ordinary defaults enable more writes, probes, external model enrichment, background diagnostics, and retention than the product description implies.
2. the runtime dependency set is already small; replacing HTTPX, Pydantic, aiosqlite, Click, or FastAPI would increase local maintenance for little measurable benefit.
3. CI is already appropriately small; only dependency-install and documentation-only trigger reductions are warranted.
4. further optimization should be driven by representative SBC measurements after architecture reduction, not by speculative micro-optimization.

## Governing constraints

The implementation must obey all of the following:

1. Do not add a workflow engine, durable work queue, generic command bus, plugin framework, or new recovery subsystem.
2. Do not broaden EggPool into a public internet-facing, multi-tenant, or multi-worker service.
3. Do not add a CI matrix, coverage threshold, long-running soak gate, live-provider gate, benchmark gate, retained evidence bundle, or automated release workflow.
4. Keep one supported Granian runtime thread. Do not attempt generalized cross-loop compatibility.
5. Keep SQLite and aiosqlite. Do not introduce PostgreSQL, Redis, an ORM, or a second persistence service.
6. Keep FastAPI/Starlette, HTTPX/httpcore, Pydantic, Click, and aiosqlite unless a later measured result proves a replacement materially reduces installed/runtime cost without feature loss.
7. Preserve OpenAI and Anthropic request/response compatibility, streaming, provider suffix routing, quota-aware account selection, bounded retry, model capability validation, exact-version update, and supported live rehash behavior.
8. Preserve fail-closed startup integrity and restart-safe reconciliation for work left by a prior process.
9. Prefer deleting compatibility paths over maintaining two execution models when production startup always installs the canonical component.
10. Add only focused tests for changed invariants; extend existing test files where practical.
11. Each plan should land as one reviewable implementation commit plus a documentation/status reconciliation commit only when needed.
12. No plan may opportunistically refactor unrelated dashboard, provider, transcoder, pricing, or CLI behavior.

## GPT-5.6 Luna execution protocol

Each implementation plan is written for bounded execution by GPT-5.6 Luna. The implementing agent must:

1. read the parent roadmap, the assigned plan, `AGENTS.md`, and the directly named architecture documents before editing;
2. inspect every named production call site before changing a shared type or lifecycle contract;
3. implement only the required phase and its acceptance criteria;
4. avoid renaming public configuration or API fields unless the plan explicitly requires it;
5. avoid speculative abstractions and use the smallest local type/helper that proves the invariant;
6. run focused tests first and stop on the first unexplained failure;
7. run the existing smoke gate after focused verification;
8. update plan status only after recording the exact commands and outcomes;
9. leave uncertain or unproven checklist items open rather than inferring completion;
10. report a blocker instead of weakening a fail-closed invariant or adding a parallel fallback path.

## Roadmap phases

### Plan 078 — Runtime Invariant and Request-Boundary Corrections

Correct lease convergence, reject multi-loop runtime configuration, replace representation-based terminal conflict checks, redact generation-abort diagnostics, constrain forwarded client-IP trust, and reconcile stale handoff wording. This plan is deliberately small and precedes ownership refactoring.

### Plan 079 — Quarantine Durability and Generation Publication

Make quarantine hydration a publication prerequisite, order durable clears before in-memory clears, preserve the active generation when candidate hydration fails, and add narrow restart/rehash regressions.

### Plan 080 — Generation Finalization Ownership Alignment

Declare one truthful ownership model for the existing finalization supervisor and make generation retirement retain the resources needed by outstanding terminal work. Do not consolidate command types yet.

### Plan 081 — Terminal Ownership Consolidation

After lifetime ownership is safe, migrate failed-attempt cleanup and claim compensation into one bounded terminal owner, remove parallel coordinator registries and compatibility finalization paths, and retain startup repair only for work abandoned by a dead process.

### Plan 082 — Database Fail-Closed Simplification

Reduce same-process SQLite recovery to the supported local deployment contract: bounded ordinary lock handling, fail-closed exit for indeterminate/fatal states, and deterministic startup reconciliation. Remove unreachable recovery states and compatibility mechanisms only after call-site proof.

### Plan 083 — Lean Defaults and Conditional Subsystem Construction

Make onboarding and shipped defaults reflect lightweight local deployment, and ensure disabled optional planes do not construct clients, queues, callbacks, repositories, or background tasks. Preserve explicit opt-in full diagnostics.

### Plan 084 — Legacy Path, Dependency, and CI Pruning

Delete obsolete production fallbacks and milestone scaffolding, split CI dependencies from local developer extras, investigate removal of unused Granian extras, and skip the CI job for plan/document-only changes. Do not replace core runtime libraries.

### Plan 085 — SBC Runtime Measurement and Roadmap Closure

Measure the reduced runtime on representative hardware using small manual commands, compare against the planning baseline or a preserved pre-change release, correct only demonstrated regressions, and close the roadmap without adding permanent performance infrastructure.

## Dependency order

```text
078 invariant corrections -----+------------------------------+
                               |                              |
079 quarantine durability -----+--> 080 ownership alignment --> 081 terminal consolidation
                                                              |
                                                              +--> 082 DB simplification
                                                              |
                                                              +--> 083 lean construction

081 + 082 + 083 --> 084 pruning --> 085 measurement and closure
```

Plan 079 may execute in parallel with Plan 078, but Plans 080–085 should follow the order above. Plan 081 must not start until generation ownership is proven. Plan 082 must not delete recovery machinery until terminal ownership and startup-repair responsibilities are explicit. Plan 084 must not remove compatibility paths before the canonical replacements have shipped and passed smoke verification.

## Cross-phase invariants

- An acquired runtime component is incomplete until it is actually released or a bounded failure remains owned and observable.
- A missing runtime dependency never counts as successful convergence.
- One supported event loop owns all long-lived asyncio primitives.
- A failed candidate generation never changes the active generation.
- Durable quarantine state is never silently replaced by an empty in-memory state after a read/parse failure.
- Response handoff means ASGI `http.response.start`, never payload byte count.
- No provider/account health penalty is created by a local serialization, adaptation, database, finalization-capacity, or invariant failure.
- No retry or reroute occurs after response handoff.
- Every selected attempt has one authoritative terminal owner.
- Generation-owned resources remain alive until their terminal work converges or the process terminates and startup repair becomes authoritative.
- Indeterminate SQLite state closes admission and relies on supervisor restart rather than continuing on uncertain state.
- Disabled optional functionality has near-zero background and construction cost.
- Routing remains load/quota based, never cost based.
- CI remains one short job and manual target-device checks remain non-gating.

## Aggregate verification policy

Each plan defines focused tests. Across the roadmap:

- use existing unit/integration/smoke test files;
- do not create plan-numbered test suites;
- use deterministic fake dependencies and direct ASGI invocation rather than sleeps or live providers;
- keep the ordinary CI gate unchanged except for dependency/trigger reductions in Plan 084;
- use manual SBC measurements only in Plan 085;
- do not require coverage percentages or exhaustive fault permutations.

The standard repository gate remains:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## Roadmap acceptance criteria

- [ ] Acquired runtime components cannot be reported released when their dependency or release action is missing.
- [ ] Configured Granian runtime threads are restricted to the supported single-loop value.
- [ ] Duplicate terminal submissions use a bounded semantic identity, not `repr()`.
- [ ] Runtime-generation cleanup diagnostics are centrally redacted and bounded.
- [ ] Forwarded client-IP headers are honored only from configured trusted proxies.
- [ ] Quarantine hydration failure prevents candidate publication and keeps the active generation unchanged.
- [ ] Model reappearance clears durable state before in-memory state.
- [ ] Finalization supervisor ownership and generation-retirement behavior agree in code and documentation.
- [ ] One bounded terminal owner replaces overlapping coordinator and finalization retained-work paths.
- [ ] Live runtime no longer attempts to continue after an indeterminate SQLite state.
- [ ] Startup reconciliation remains sufficient to repair durable work left by a dead process.
- [ ] Lightweight onboarding/defaults disable nonessential writes, probes, enrichment, traces, and detailed diagnostics.
- [ ] Disabled optional subsystems do not allocate their clients, queues, callbacks, or background tasks.
- [ ] Obsolete production fallbacks and migration scaffolding are removed with their tests.
- [ ] Runtime dependencies are reduced only where no feature or maintenance regression is introduced.
- [ ] CI remains one short smoke-oriented job and skips documentation/plan-only changes.
- [ ] Representative SBC measurements show no correctness regression and document idle/runtime resource changes.
- [ ] No new generalized framework, deployment tier, or verification apparatus is introduced.

## Rejection conditions

Do not close this roadmap if any of the following remains true:

- a missing dependency can produce `runtime_cleanup_complete=True`;
- `threads > 1` remains a supported configuration despite loop-bound state;
- quarantine hydration logs an error and publishes an empty state;
- a failed rehash candidate can replace or mutate the active generation;
- generation retirement can close resources still referenced by retained terminal work;
- more than one production component can claim authoritative terminal ownership for the same obligation;
- live code attempts to recover and reopen admission after an indeterminate commit/rollback without process restart;
- lightweight mode still constructs disabled external-source clients or diagnostic writers;
- core libraries are replaced without measured benefit;
- CI or release infrastructure expands;
- performance claims are made without representative measurements.

## Definition of done

This roadmap is complete when Plans 078–085 are implemented in dependency order, all checklist items above are evidenced, the focused and smoke gates pass, the target SBC profile has fewer background writes/tasks and no higher steady-state resource use without explanation, the canonical request path has fewer ownership/recovery branches, and the repository is easier for a small model or human maintainer to reason about than at the planning baseline.