# M4 Provider Transport Handoff Sequence

Status: complete; M4 provider transport closed

Execute and close these plans in order:

1. T001 — contract and fixture freeze (closed);
2. T002 — direct Hyper/Rustls provider HTTP core (closed);
3. T003 — Eggress connector and proxy parity (closed);
4. T004 — provider/account client pool and lifecycle boundary (closed);
5. T005 — differential qualification and M4 closure (closed; see the [closure record](../../closure/provider-transport/005-status.md)).

Do not batch T002-T004 into one implementation commit merely because all plans are written. The separation exists so direct HTTP/TLS behavior can be qualified before Eggress, proxy behavior can be qualified before pool topology, and the pool can be qualified before M4 closure.

M4's hard sequence is complete. M5 planning may now proceed against the
stable transport handoff. If a later qualification finds a material transport
regression, insert a bounded corrective plan rather than weakening the frozen
contract.
