# M7 Coordinator, Retry, Failover, and Durable Finalization Roadmap

Status: active; C001 closed, C002 dependency-ready

Repository baseline: `04820555479dc3ab86622d9c658c44c45c2c07e7`

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, accepted ADR-0001 through ADR-0003, and the closed M4/M5/M6 roadmaps.

## Purpose

M7 ports EggPool's inference orchestration boundary without recreating the current Python `request/coordinator.py` monolith. It composes the already-closed Rust transport, routing/claim, and wire-codec layers into one explicit attempt/request state machine with bounded retry, alternate-wire negotiation, downstream handoff, cancellation handling, durable finalization, and crash-safe reconciliation.

The Python coordinator is intentionally treated as a behavioral oracle, not a structural template. Its current responsibilities are already factored across `request/coordinator.py`, `attempt_finalizer.py`, `claim_lifecycle.py`, `finalization_job.py`, `finalizer.py`, `provider_bound_request.py`, `response_handoff.py`, `stream_completion.py`, `stream_diagnostics.py`, `retry/classification.py`, failure/effects modules, provider contracts/client pools, and `wire/resolver.py`.

## Ownership boundary

M7 owns:

- one request/attempt lifecycle state machine;
- atomic durable request/attempt/reservation/routing-decision publication after an M5 local selection claim;
- conversion or compensation of M5 pending/active/quota/circuit ownership;
- runtime wire candidate resolution, rejection suppression, single-flight negotiation, learned preference, and alternate-wire attempts;
- provider-bound request identity, auth/static/forwarded header construction, path/model substitution, and M4 HTTP submission;
- upstream finite-response classification and client response adaptation through M6;
- failure observation/effects, retry legality, retry budget, account failover, wire failover, and terminal exhaustion;
- request/attempt finalization and exact-once convergence of durable and process-local obligations;
- retained finalization jobs and a bounded supervisor/reconciliation interface independent of the client request task;
- streaming response handoff, response-start point-of-no-return, first-byte/idle/header timeout ownership, client cancellation, upstream interruption, M6 terminal evidence, and stream finalization;
- public Rust inference endpoints for Chat Completions, Responses, and Messages once the lifecycle is qualified;
- D007 semantic model-router selector dispatch through a bounded internal coordinator path;
- deterministic restart/crash reconciliation routines for M7 durable state.

M7 does not own:

- Hyper/Rustls/Eggress connection establishment or pooling (M4);
- account eligibility/scoring/local claim acquisition, quota model, health model, circuit model, catalog state, virtual-router compilation, or affinity policy (M5);
- canonical request/wire transformations, SSE parsing/encoding, usage normalization, or terminal evidence extraction (M6);
- immutable runtime generation publication, ArcSwap generation replacement, live rehash, process signal lifecycle, or recurring/background scheduling (M8);
- daemon/install/update/backup CLI completion (M9);
- broad release/SBC qualification (M10).

## Core state model

Rust should use explicit typed states rather than flags scattered across handlers. Exact names may change, but the lifecycle must distinguish at least:

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

Retryable pre-handoff failures transition through an attempt-terminal state back to a new local claim/attempt. Once downstream response start is sent or attempted, the same client request may not be transparently replayed to another account or wire profile. Terminal cleanup may outlive the client task.

## Invariants

1. **No orphaned selected claim.** Every acquired M5 component is converted into durable ownership or compensated exactly once.
2. **Durable publication before irreversible dispatch ownership.** A selected attempt must have a stable request/attempt/reservation identity before the provider request can become externally meaningful, subject to the frozen Python contract in C001.
3. **No retry after downstream handoff.** Response-start is a monotonic point of no return for transparent replay.
4. **Retry is centralized.** M4 transport and M6 codecs never autonomously retry.
5. **Wire fallback is evidence-driven.** Candidate rejection/learning occurs only from authorized failure evidence; malformed/unsupported proxy or auth behavior never falls back to direct transport.
6. **Attempts are independently terminal.** A failed attempt is finalized/released before a replacement attempt claims another account/wire.
7. **Request terminal state is idempotent.** Duplicate finalization observes convergence; incompatible terminal outcomes fail closed as an invariant conflict.
8. **Async cleanup is retained.** RAII/`Drop` may release purely synchronous local memory, but it cannot substitute for async DB/runtime finalization.
9. **Cancellation is phase-aware.** Cancellation before publication, after publication/before handoff, and after downstream start have distinct cleanup/retry consequences.
10. **Terminal evidence is not invented.** M6 stream terminal evidence feeds M7 policy; EOF alone is not universal success.
11. **Bounded work.** Attempts, account/wire enumeration, negotiation flights, finalization jobs, retry state, diagnostics, and recovery scans all have hard bounds.
12. **No secret persistence/logging.** Auth headers, API keys, proxy credentials, arbitrary request bodies, provider response bodies, and session identity are never included in default diagnostics or error-detail persistence.

## Dependency sequence

```text
M4 T001-T006 closed
M5 D001-D009 closed
M6 W001-W012 closed
        |
        v
C001 contract + deterministic failure corpus
 -> C002 durable dispatch publication + lifecycle identity
 -> C003 runtime wire resolution + negotiation ownership
 -> C004 provider-bound attempt construction + upstream submission
 -> C005 failure effects + retry/failover decision engine
 -> C006 durable finalization + retained terminal ownership
 -> C007 finite response/handoff completion path
 -> C008 streaming/handoff/timeouts/cancellation path
 -> C009 public inference endpoints + semantic-router internal dispatch
 -> C010 crash/restart reconciliation + fault injection
 -> C011 integrated differential qualification + M7 closure
        |
        v
M8 planning/implementation eligibility
```

Only the dependency-ready table in `../registry.md` authorizes handoff. The default is serial. A later review may approve limited parallel test-fixture work, but no successor closes without all hard predecessors.

## Dependency posture

M7 should add no second HTTP stack, actor framework, ORM, async runtime, generic workflow engine, or distributed task queue. Tokio, Axum, M4 Hyper transport, M5 domain state, M6 wire runtime, and existing SQLite access are sufficient.

A small internal task registry/supervisor is justified because terminal cleanup must survive cancellation of the client task. It must remain bounded and process-local. M8 later embeds that supervisor in immutable runtime generations and supplies generation lifetime/scheduling semantics.

## Failure corpus

C001 freezes a deterministic corpus covering at least:

- success with finite body;
- success with streaming terminal evidence;
- bad client request/adaptation loss before claim;
- selection exhaustion;
- DB failure before/inside/after dispatch transaction;
- cancellation while waiting for claim, persistence, negotiation, connect, headers, first byte, stream body, and finalization;
- connect/TLS/proxy/write/read/pool failures;
- 400/401/403/404/408/409/429/5xx and provider-specific response signals;
- Retry-After numeric/date/invalid forms;
- model-specific absence vs provider endpoint/path mismatch;
- alternate-wire deterministic rejection, rate-limited negotiation, leader/follower cancellation, and successful learning;
- malformed finite JSON and valid provider error envelopes;
- empty stream EOF, partial EOF, malformed SSE, terminal failure/incomplete, compatibility completion, and upstream midstream exception;
- downstream disconnect before and after response start;
- finalizer DB interruption, repeated/concurrent finalization, terminal conflicts, and incomplete runtime release;
- restart with active request/attempt/reservation combinations.

## M7/M8 boundary for retained cleanup

M7 implements the terminal command, identity, progress, retry/backoff, bounded job registry, and explicit `reconcile_once`/drain interfaces required for correctness. Tests must prove jobs continue independently of a cancelled request waiter while the M7 owner remains alive.

M8 owns which runtime generation retains the supervisor, publication/replacement of generations, process shutdown ordering, signal handling, and recurring invocation of reconciliation/background work. C010 may invoke recovery explicitly in tests; it must not add a perpetual background loop merely to close M7.

## Closure

M7 closes only after C001-C011 have accepted closure records under `../closure/coordinator/` and the integrated corpus proves:

- no retry after client-visible handoff;
- no selected-claim, reservation, active-count, quota, circuit-probe, negotiation-flight, or retained-finalization leak;
- every retryable attempt is independently durable-terminal before replacement ownership is accepted;
- request/attempt/reservation state converges under duplicate, cancellation, DB fault, and restart cases;
- wire learning/rejection is bounded and evidence-driven;
- finite and streaming public endpoints match the Python oracle semantically;
- no new high/medium M7 correctness or security issue remains.

If post-closure review finds a material defect, add a new C012+ corrective plan. Do not rewrite historical closure records.

## Current closure state

C001 is accepted and closed. Its contract, deterministic fixture inventory,
Python structural observation projection, and local provider/failure tests are
recorded in [`coordinator-contract.md`](../coordinator-contract.md) and
[`closure/coordinator/001-status.md`](../closure/coordinator/001-status.md).
C002 is the sole dependency-ready implementation plan. C003-C011 remain
serially blocked on their named predecessors, and M8 remains blocked on
accepted C011 closure plus its own planning review.
