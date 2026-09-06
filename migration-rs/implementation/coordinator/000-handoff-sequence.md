# M7 Coordinator Handoff Sequence

Status: corrective closure active

Execute/accept in this order:

1. C001 — contract/failure corpus (**closed**).
2. C002 — durable dispatch publication (**closed**).
3. C003-C006 — original coordinator core slices (**historical closures** for the findings corrected by C012-C014).
4. C012 — coordinator core contract correction (**closed**).
5. C013 — coordinator core differential requalification (**closed**).
6. C014 — finalization idempotency and Retry-After closure (**ready**).
7. C007 — finite provider response classification, downstream handoff, completion (**re-blocked on C014**).
8. C008 — streaming handoff, header/first-byte/idle timeouts, cancellation, EOF/terminal policy.
9. C009 — Axum public inference endpoints and D007 semantic-router internal coordinator dispatch.
10. C010 — restart reconciliation and deterministic fault injection across durable/runtime boundaries.
11. C011 — integrated Python/Rust differential qualification and M7 closure.

C014 is deliberately narrow. It must fix durable-only completion progress, retained-command compatibility for every authoritative persisted terminal fact, uniform Retry-After bounding, and historical retry-attempt idempotency after later attempts update mutable parent selection.

Response-start remains a monotonic point of no return. No transparent retry is permitted after downstream handoff.

Attempt cleanup and terminal finalization must remain retained independently of the client task. `Drop` is not sufficient for async durable cleanup.

M8 owns runtime-generation publication, rehash, shutdown/signal orchestration, and recurring background scheduling. C014 must not pull those responsibilities forward.

M8 cannot become implementation-ready until C011 closes M7 and a separate M8 planning review accepts its handoffs.
