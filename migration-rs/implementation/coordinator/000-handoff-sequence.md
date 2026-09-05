# M7 Coordinator Handoff Sequence

Status: active

Execute and close in this order:

1. C001 — freeze coordinator/failure/retry/finalization contract and fixtures (**closed**; see [closure](../../closure/coordinator/001-status.md)).
2. C002 — durable dispatch publication and lifecycle identity (**closed**; see [closure](../../closure/coordinator/002-status.md)).
3. C003 — runtime wire resolution, rejection cache, learning, and negotiation single-flight (**closed**; see [closure](../../closure/coordinator/003-status.md)).
4. C004 — provider-bound attempt construction, auth/header/path assembly, and M4 submission (**closed**; see [closure](../../closure/coordinator/004-status.md)).
5. C005 — canonical failure effects, retry budget, account/wire failover, exhaustion (**closed**; see [closure](../../closure/coordinator/005-status.md)).
6. C006 — attempt/request finalizers, claim compensation, retained terminal ownership (**closed**; see [closure](../../closure/coordinator/006-status.md)).
7. C007 — finite provider response classification, downstream handoff, completion (**ready**).
8. C008 — streaming handoff, header/first-byte/idle timeouts, cancellation, EOF/terminal policy.
9. C009 — Axum public inference endpoints and D007 semantic-router internal coordinator dispatch.
10. C010 — restart reconciliation and deterministic fault injection across durable/runtime boundaries.
11. C011 — integrated Python/Rust differential qualification and M7 closure.

Do not batch C002-C006 into a monolithic coordinator implementation. Each establishes an ownership boundary used by the next plan.

Response-start is a monotonic point of no return. No transparent retry is permitted after downstream handoff.

Attempt cleanup and terminal finalization must be retained independently of the client task. `Drop` is not sufficient for async durable cleanup.

M8 owns runtime-generation publication, rehash, shutdown/signal orchestration, and recurring background scheduling. C010 may call reconciliation explicitly; it must not pull M8 forward.

M8 cannot become implementation-ready until C011 closes M7 and a separate M8 planning review accepts its handoffs.
