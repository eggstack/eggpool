# M7 Coordinator, Retry, Failover, and Durable Finalization Roadmap

Status: implementation active; C007 dependency-ready

Repository baseline for original M7 planning: `04820555479dc3ab86622d9c658c44c45c2c07e7`

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, accepted ADR-0001 through ADR-0003, the closed M4/M5/M6 roadmaps, and the accepted C001 coordinator contract.

## Purpose

M7 ports EggPool's inference orchestration boundary without recreating the Python `request/coordinator.py` monolith. It composes the closed Rust transport, routing/claim, and wire-codec layers into an explicit attempt/request state machine with bounded retry, alternate-wire negotiation, downstream handoff, cancellation handling, durable finalization, and crash-safe reconciliation.

The Python coordinator is a behavioral oracle, not a structural template. Its responsibilities are already factored across request coordination, attempt finalization, claim lifecycle, retained finalization, provider-bound requests, response handoff, stream completion/diagnostics, retry classification, failure/effects, provider contracts/client pools, and `wire/resolver.py`.

## Ownership boundary

M7 owns:

- one request/attempt lifecycle state machine;
- atomic durable request/attempt/reservation/routing-decision publication after an M5 local selection claim;
- conversion or compensation of M5 pending/active/quota/circuit ownership;
- runtime wire candidate resolution, rejection suppression, single-flight negotiation, learned/fixed/hinted preference, and alternate-wire attempts;
- provider-bound canonical/upstream model identity, auth/static/forwarded headers, path substitution, and M4 HTTP submission;
- upstream finite-response classification and client response adaptation through M6;
- complete failure observations/effects, retry legality/budget, account failover, wire failover, and terminal exhaustion;
- request/attempt finalization and exact-once convergence of durable and process-local obligations;
- retained finalization jobs and a bounded supervisor/reconciliation interface independent of the client task;
- streaming response handoff, response-start point-of-no-return, timeout ownership, cancellation, upstream interruption, M6 terminal evidence, and stream finalization;
- public Rust inference endpoints after lifecycle qualification;
- D007 semantic model-router selector dispatch through a bounded internal coordinator path;
- deterministic restart/crash reconciliation routines for M7 durable state.

M7 does not own:

- Hyper/Rustls/Eggress connection establishment or pooling (M4);
- account eligibility/scoring/local claim acquisition, quota/health/circuit/catalog policy, virtual-router compilation, or affinity policy (M5);
- canonical request/wire transformations, SSE parsing/encoding, usage normalization, or native terminal evidence extraction (M6);
- immutable runtime generation publication, ArcSwap generation replacement, live rehash, process signal lifecycle, or recurring/background scheduling (M8);
- daemon/install/update/backup CLI completion (M9);
- broad release/SBC qualification (M10).

## Core state model

Rust should use explicit typed ownership/state rather than flags scattered across handlers. Internal names may differ, but observations must preserve at least:

```text
admitted
 -> locally_claimed
 -> durable_attempt_published
 -> wire_selected
 -> dispatching
 -> upstream_headers_received
 -> downstream_started
 -> streaming
 -> terminal_command_registered
 -> durable_terminal
 -> runtime_released
 -> completed
```

Retryable pre-handoff failures transition through an independently terminal attempt before a new local claim/attempt can take ownership. Once downstream response start is sent or attempted, transparent replay is forbidden. Terminal cleanup may outlive the client task.

## Invariants

1. **No orphaned selected claim.** Every acquired M5 component is converted or compensated exactly once.
2. **Durable publication before provider ownership.** A selected attempt has stable request/attempt/reservation identity before provider send can escape.
3. **Canonical and upstream model identity remain distinct.** Provider-native aliases/remaps survive through request encoding/path construction.
4. **No retry after downstream handoff.** Response-start is monotonic and disables transparent replay.
5. **Retry is centralized.** M4 transport and M6 codecs never autonomously retry.
6. **Failure effects are evidence-driven.** Ambiguous auth/model/wire evidence cannot trigger destructive account/model state changes.
7. **Wire fallback is evidence-driven and bounded.** Candidate rejection/learning/delay occurs only from authorized policy and all resolver state has hard lifecycle bounds.
8. **Attempts are independently terminal.** A failed attempt reaches the required durable/runtime cleanup boundary before replacement ownership.
9. **Request terminal state is idempotent.** Compatible duplicate finalization observes convergence; incompatible terminal commands fail closed.
10. **Zero-row updates are not convergence proof.** Conditional durable transitions are re-read and verified when they do not change a row.
11. **Async cleanup is retained.** RAII/`Drop` cannot substitute for async durable/runtime finalization.
12. **Cancellation is phase-aware.** Pre-publication, pre-handoff, post-handoff, streaming, and finalization cancellation have distinct consequences.
13. **Terminal evidence is not invented.** M6 terminal evidence feeds M7 policy; EOF alone is not universal success.
14. **Bounded work.** Attempts, effect bookkeeping, wire state, flights, finalization jobs, diagnostics, and recovery scans all have hard bounds/retirement.
15. **No secret persistence/logging.** Auth/API/proxy values and arbitrary request/provider bodies never enter default diagnostics or durable error detail.
16. **Completion reflects required ownership, not invocation shape.** A durable-only duplicate with no runtime claim may be complete; a command with unreleased required runtime ownership may not.
17. **Historical attempt identity is immutable.** Re-observing an old terminal attempt after a replacement changes mutable parent selection validates against the historical attempt/reservation identity without mutating later ownership.
18. **Retry-After is uniformly bounded.** Numeric and HTTP-date forms cannot exceed the configured maximum effective delay.

## Dependency sequence

```text
M4 T001-T006 closed
M5 D001-D009 closed
M6 W001-W012 closed
        |
        v
C001 contract + deterministic failure corpus              [closed]
 -> C002 durable publication + lifecycle identity         [closed]
 -> C003 wire resolution/negotiation                      [historical closure]
 -> C004 provider-bound attempt/submission                [historical closure]
 -> C005 failure effects/retry/failover                   [historical closure]
 -> C006 durable finalization/retained ownership          [historical closure]
 -> C012 coordinator core contract correction             [closed]
 -> C013 coordinator core differential requalification    [closed]
 -> C014 finalization idempotency + Retry-After closure   [closed]
 -> C007 finite response/handoff completion
 -> C008 streaming/handoff/timeouts/cancellation
 -> C009 public inference endpoints + semantic-router dispatch
 -> C010 crash/restart reconciliation + fault injection
 -> C011 integrated differential qualification + M7 closure
        |
        v
M8 planning/implementation eligibility
```

The append-only C012-C014 numbering is intentional. Post-closure audits found material or bounded gaps in historically accepted coordinator slices; planning history is not rewritten. C014 is closed, and C007 is now the sole dependency-ready plan.

Only the dependency-ready table in `../registry.md` authorizes handoff.

## Post-C006 corrective findings

The historical `97a4846` implementation remains useful but did not fully satisfy the accepted C001/C003-C006 contracts. C012/C013 addressed these findings:

- failure observations/effects omitted policy-bearing identity/transport/protocol/signal/model-presence dimensions;
- ambiguous 401 and explicit invalid-credential evidence were conflated;
- attempt effect bookkeeping was process-lifetime and unbounded;
- wire state insertion/bounds and fixed/hint/rate-limit-delay semantics were incomplete;
- provider-native `upstream_model_id` was lost before C004 path/body construction;
- C004 forwarded-header/request-ID/evidence boundary was incomplete;
- C006 could claim convergence after zero-row attempt/reservation updates without durable re-read;
- retained finalization could coalesce incompatible commands by key alone;
- partial runtime/effect progress was not explicit enough to prove resumable convergence.

C012 repaired those semantics. C013 independently requalified the corrected path against the C001 Python oracle, deterministic M4 fixtures, concurrency, boundedness, and durable/runtime fault injection.

## Post-C013 residual findings — C014

Post-C013 audit found four narrower issues that must close before C007 resumes:

- durable-only duplicate/reconciliation finalization can report `progress.completed = false` despite compatible durable convergence and no runtime claim obligation;
- retained finalization compatibility omits authoritative persisted terminal facts such as byte counts, latency, and bounded upstream request ID;
- numeric Retry-After is capped while HTTP-date Retry-After can bypass `RetryPolicy.max_retry_after`;
- re-finalizing/re-observing an already-terminal earlier retry attempt after a later attempt updates mutable parent account/provider selection can fail a parent identity check despite valid historical attempt/reservation identity.

C014 corrected these without widening into C007 response handling, C008 streaming policy, C010 restart scanning, or M8 lifecycle work. Its closure records failing-before/passing-after regression evidence, including a two-attempt historical-idempotency case and uniform Retry-After bound cases.

## Dependency posture

M7 should add no second HTTP stack, actor framework, ORM, async runtime, workflow engine, distributed queue, or convenience schema fork. Tokio, Axum, M4 Hyper transport, M5 domain state, M6 wire runtime, and existing SQLite access are sufficient.

A small process-local finalization supervisor is justified because terminal cleanup must survive client-task cancellation. Its state must remain bounded and movable into an M8 generation later.

## Failure and requalification corpus

C001 remains the authoritative behavioral corpus. C013 proved the corrected Rust core across client/local failures, transport phases, credential/model/wire evidence, Retry-After, handoff boundaries, wire state, provider-native model submission, header precedence, effect retirement, durable truth, supervisor compatibility, and replacement ownership.

C014 added focused coverage for:

- durable-only completion progress;
- compatibility over every authoritative `FinalizationData` field persisted by the durable finalizer;
- numeric and HTTP-date Retry-After values below/above the configured cap;
- historical attempt re-observation after later retry publication;
- no cross-attempt runtime release or mutable parent selection rollback.

## M7/M8 boundary

M7 implements terminal command identity/progress, bounded retained jobs, explicit drain/reconcile interfaces, and later C010 one-shot restart reconciliation. M8 owns generation publication/replacement, shutdown ordering, signals, and recurring invocation of background/reconciliation work.

C014 did not introduce a perpetual scheduler merely to close finalization or delay semantics.

## Closure

M7 closes only after accepted C001-C014/C007-C011 closure evidence proves:

- C012-C014 corrective findings are resolved;
- no retry after client-visible handoff;
- no selected-claim, reservation, active-count, quota, circuit-probe, wire-flight, effect-registry, or retained-finalization leak;
- every retryable attempt reaches its required cleanup boundary before replacement ownership;
- durable request/attempt/reservation state converges under duplicate, cancellation, DB fault, and restart cases;
- wire learning/rejection/delay is bounded and evidence-driven;
- finite and streaming public endpoints match Python semantically;
- no unresolved high/medium M7 correctness/security issue remains.

C011 remains the aggregate M7 closure plan. C014 only closes the residual core invariants before C007 proceeds; it does not replace C011.

## Current closure state

C001, C002, and C012-C014 are accepted and remain closed. C003-C006 retain append-only closure records but are historical for the findings corrected by C012-C014. C007 is now the sole dependency-ready plan. C008-C011 retain their serial dependencies. M8 remains blocked on accepted C011 closure plus its own planning review.
