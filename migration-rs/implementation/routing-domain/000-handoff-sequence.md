# M5 Routing Domain Handoff Sequence

Status: closed after D009 corrective pass; M6 implementation handoff unblocked

Historical execution/closure order:

1. D001 — contract and deterministic fixture freeze (closed);
2. D002 — account registry and catalog cache/hydration (closed);
3. D003 — catalog refresh, normalization, and persistence (closed; see [closure](../../closure/routing-domain/003-status.md));
4. D004 — quota, pending/reserved claims, and fair-share scoring (closed; see [closure](../../closure/routing-domain/004-status.md));
5. D005 — health, bounded backoff, circuit breaker, and quarantine (closed; see [closure](../../closure/routing-domain/005-status.md));
6. D006 — routing eligibility, priority tiers, fairness, and local selection claims (historically closed; see [closure](../../closure/routing-domain/006-status.md));
7. D007 — compiled model-router registry and bounded affinity (closed; see [closure](../../closure/routing-domain/007-status.md));
8. D008 — integrated differential qualification and initial M5 closure (historically closed; see [closure](../../closure/routing-domain/008-status.md));
9. **D009 — selection fairness and frozen routing-trace correction (closed; see [closure](../../closure/routing-domain/009-status.md)).**

Independent post-D008 review found two mandatory D006 contract gaps that D008 did not exercise: configured random fairness does not currently alter the actual Rust `select_and_claim` choice, and accepted routing trace data can be rebuilt after active/pending claim publication changes routing scores. D009 corrects both without reopening unrelated M5 architecture.

D004 and D005 shared D003 as a hard predecessor. D009 depends on the complete D001-D008 implementation because its regression suite must prove the correction against the integrated M5 state machine.

D009 is closed and the stable routing-domain interface is restored. M6 implementation handoff is unblocked, but no M6 implementation plan is registered yet; its own planning review must establish the next dependency-ready handoff. M7 remains blocked on M6.

M5 local claim ownership still stops before durable inference dispatch. D009 may change the in-memory fairness decision and freeze a bounded selection snapshot on the claim; it must not add request/reservation/attempt persistence, provider submission, retry/failover, or terminal cleanup.

M5 model-router work likewise remains limited to deterministic compiled policy and affinity state. `ModelRouterSelector`-style internal inference calls remain M7 because the Python implementation delegates them to `RequestCoordinator`.

Historical D006/D008 closure records remain append-only. D009 has its own closure record and re-closes M5; M6 implementation handoff is now unblocked. M7 remains blocked until M6 closes.
