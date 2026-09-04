# M4 Provider Transport Handoff Sequence

Status: active sequencing note

Execute and close these plans in order:

1. T001 — contract and fixture freeze;
2. T002 — direct Hyper/Rustls provider HTTP core;
3. T003 — Eggress connector and proxy parity;
4. T004 — provider/account client pool and lifecycle boundary;
5. T005 — differential qualification and M4 closure.

Do not batch T002-T004 into one implementation commit merely because all plans are written. The separation exists so direct HTTP/TLS behavior can be qualified before Eggress, proxy behavior can be qualified before pool topology, and the pool can be qualified before M4 closure.

A later plan becomes dependency-ready only after the prior hard dependency has an accepted closure record. If closure fails, insert a bounded corrective plan before advancing.