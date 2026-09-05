# M7 Coordinator, Retry, Failover, and Durable Finalization Roadmap

Status: corrective pass active; C012 dependency-ready, C013 queued, C007 re-blocked

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
 -> C012 coordinator core contract correction             [READY]
 -> C013 coordinator core differential requalification
 -> C007 finite response/handoff completion
 -> C008 streaming/handoff/timeouts/cancellation
 -> C009 public inference endpoints + semantic-router dispatch
 -> C010 crash/restart reconciliation + fault injection
 -> C011 integrated differential qualification + M7 closure
        |
        v
M8 planning/implementation eligibility
```

The append-only C012/C013 numbering is intentional. Post-C006 audit found material gaps in the historically closed C003-C006 slice; planning history is not rewritten. C007 was previously dependency-ready and is now re-blocked until accepted C013 closure.

Only the dependency-ready table in `../registry.md` authorizes handoff.

## Post-C006 corrective findings

The historical `97a4846` implementation remains useful but did not fully satisfy the accepted C001/C003-C006 contracts. C012/C013 are limited to these findings:

- failure observations/effects omit policy-bearing identity/transport/protocol/signal/model-presence dimensions;
- ambiguous 401 and explicit invalid-credential evidence are currently conflated;
- attempt effect bookkeeping is process-lifetime and unbounded;
- wire state insertion/bounds and fixed/hint/rate-limit-delay semantics are incomplete;
- provider-native `upstream_model_id` is lost before C004 path/body construction;
- C004 forwarded-header/request-ID/evidence boundary is incomplete;
- C006 can claim convergence after zero-row attempt/reservation updates without durable re-read;
- retained finalization can coalesce incompatible commands by key alone;
- partial runtime/effect progress is not explicit enough to prove resumable convergence.

C012 repairs those semantics. C013 independently requalifies the corrected path against the C001 Python oracle, deterministic M4 fixtures, concurrency, boundedness, and durable/runtime fault injection. Neither plan pulls C007/C008 behavior forward.

## Dependency posture

M7 should add no second HTTP stack, actor framework, ORM, async runtime, workflow engine, distributed queue, or convenience schema fork. Tokio, Axum, M4 Hyper transport, M5 domain state, M6 wire runtime, and existing SQLite access are sufficient.

A small process-local finalization supervisor is justified because terminal cleanup must survive client-task cancellation. Its state must remain bounded and movable into an M8 generation later.

## Failure and requalification corpus

C001 remains the authoritative behavioral corpus. C013 must prove the corrected Rust core across:

- client/local preparation failures;
- connect/proxy/TLS/write/read/pool transport phases;
- ambiguous and explicit 401/403 credential evidence;
- generic 404/path mismatch vs strong model absence;
- deterministic wire rejection and alternate-wire legality;
- 408/429/5xx including bounded Retry-After;
- response-start false/true no-replay boundary;
- fixed/hinted/learned/configured wire ordering, TTLs, rejection cooldown, rate-limit negotiation delay, fingerprint changes, eviction, leader/follower cancellation;
- canonical alias vs provider-native upstream model submission;
- auth/static/surface/forwarded-header precedence and redaction;
- effect idempotency/retirement/capacity;
- missing/already-terminal request/attempt/reservation durable states;
- compatible/incompatible supervisor registration;
- partial runtime release and resumable convergence;
- two-attempt replacement ownership ordering.

## M7/M8 boundary

M7 implements terminal command identity/progress, bounded retained jobs, explicit drain/reconcile interfaces, and later C010 one-shot restart reconciliation. M8 owns generation publication/replacement, shutdown ordering, signals, and recurring invocation of background/reconciliation work.

C012/C013 must not introduce a perpetual scheduler merely to make finalization/effect state bounded.

## Closure

M7 closes only after accepted C001-C013/C007-C011 closure evidence proves:

- C012/C013 corrective findings are resolved;
- no retry after client-visible handoff;
- no selected-claim, reservation, active-count, quota, circuit-probe, wire-flight, effect-registry, or retained-finalization leak;
- every retryable attempt reaches its required cleanup boundary before replacement ownership;
- durable request/attempt/reservation state converges under duplicate, cancellation, DB fault, and restart cases;
- wire learning/rejection/delay is bounded and evidence-driven;
- finite and streaming public endpoints match Python semantically;
- no unresolved high/medium M7 correctness/security issue remains.

C011 remains the aggregate M7 closure plan. C012/C013 correct and requalify the core before C007 resumes; they do not replace C011.

## Current closure state

C001 and C002 are accepted and remain closed. C003-C006 retain append-only closure records but are historical for the post-C006 findings enumerated above. C012 is the sole dependency-ready plan. C013 is queued behind C012. C007 has been re-blocked behind C013; C008-C011 retain their serial dependencies. M8 remains blocked on accepted C011 closure plus its own planning review.