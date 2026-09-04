# M5 Routing Domain Handoff Sequence

Status: closed after D008 qualification; D001-D008 closed

Execute and close these plans in dependency order:

1. D001 — contract and deterministic fixture freeze (closed);
2. D002 — account registry and catalog cache/hydration (closed);
3. D003 — catalog refresh, normalization, and persistence (closed; see [closure](../../closure/routing-domain/003-status.md));
4. D004 — quota, pending/reserved claims, and fair-share scoring (closed; see [closure](../../closure/routing-domain/004-status.md));
5. D005 — health, bounded backoff, circuit breaker, and quarantine (closed; see [closure](../../closure/routing-domain/005-status.md));
6. D006 — routing eligibility, priority tiers, fairness, and local selection claims (closed; see [closure](../../closure/routing-domain/006-status.md));
7. D007 — compiled model-router registry and bounded affinity (closed; see [closure](../../closure/routing-domain/007-status.md));
8. D008 — integrated differential qualification and M5 closure (closed; see [closure](../../closure/routing-domain/008-status.md)).

D004 and D005 shared D003 as a hard predecessor; D006-D008 are now closed. M6 planning/implementation handoff is unblocked. Default handoff remains serial to keep review and closure evidence small; M7 remains blocked on M6.

Do not batch D002-D006 merely because they all feed the router. Their separation exists to make incorrect catalog destruction, quota pressure, health suppression, and final selection independently observable.

M5 local claim ownership stops before durable inference dispatch. D006 may atomically select/revalidate an account, acquire a half-open circuit probe, increment active ownership, and publish provisional quota load. D007 does not change that. Request/reservation/attempt persistence, provider submission, retry/failover, and terminal cleanup remain M7.

M5 model-router work likewise stops at deterministic compiled policy and affinity state. `ModelRouterSelector`-style internal inference calls remain M7 because the Python implementation delegates them to `RequestCoordinator`.

D008 closed after confirming no predecessor has an unresolved mandatory parity gap, an unbounded learned-state map, or a concurrency path that can publish partial claim ownership.
