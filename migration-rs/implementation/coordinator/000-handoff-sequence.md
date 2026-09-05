# M7 Coordinator Handoff Sequence

Status: corrective pass active

Execute and close in this order:

1. C001 — freeze coordinator/failure/retry/finalization contract and fixtures (**closed**; see [closure](../../closure/coordinator/001-status.md)).
2. C002 — durable dispatch publication and lifecycle identity (**closed**; see [closure](../../closure/coordinator/002-status.md)).
3. C003 — runtime wire resolution, rejection cache, learning, and negotiation single-flight (**historical closure**; post-closure findings corrected by C012/C013).
4. C004 — provider-bound attempt construction, auth/header/path assembly, and M4 submission (**historical closure**; post-closure findings corrected by C012/C013).
5. C005 — canonical failure effects, retry budget, account/wire failover, exhaustion (**historical closure**; post-closure findings corrected by C012/C013).
6. C006 — attempt/request finalizers, claim compensation, retained terminal ownership (**historical closure**; post-closure findings corrected by C012/C013).
7. C012 — coordinator core contract correction for C003-C006 (**ready**).
8. C013 — independent differential/fault requalification of corrected C003-C006.
9. C007 — finite provider response classification, downstream handoff, completion (**re-blocked until C013 closes**).
10. C008 — streaming handoff, header/first-byte/idle timeouts, cancellation, EOF/terminal policy.
11. C009 — Axum public inference endpoints and D007 semantic-router internal coordinator dispatch.
12. C010 — restart reconciliation and deterministic fault injection across durable/runtime boundaries.
13. C011 — integrated Python/Rust differential qualification and M7 closure.

The append-only numbering is intentional: C012/C013 were discovered after historical C003-C006 closure and are inserted as corrective gates before C007 rather than rewriting old closure records.

Response-start is a monotonic point of no return. No transparent retry is permitted after downstream handoff.

Attempt cleanup and terminal finalization must be retained independently of the client task. `Drop` is not sufficient for async durable cleanup.

A replacement attempt may not take durable ownership until the previous attempt satisfies the corrected C005/C006 cleanup boundary.

M8 owns runtime-generation publication, rehash, shutdown/signal orchestration, and recurring background scheduling. C010 may call reconciliation explicitly; it must not pull M8 forward.

M8 cannot become implementation-ready until C011 closes M7 and a separate M8 planning review accepts its handoffs.