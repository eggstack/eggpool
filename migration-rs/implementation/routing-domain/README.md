# M5 Routing Domain Implementation Handoffs

Status: active corrective pass after D008; D009 ready for handoff

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md`

M5 is sequenced so policy/state parity is established before inference dispatch exists. The plans deliberately keep transport, request codecs, coordinator retry/finalization, runtime generation publication, and background lifecycle outside this workstream.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| D001 | [Contract and deterministic fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; see [closure](../../closure/routing-domain/001-status.md) |
| D002 | [Account registry and catalog cache/hydration](002-account-registry-and-catalog-cache.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/002-status.md) |
| D003 | [Catalog refresh, normalization, and persistence](003-catalog-refresh-normalization-and-persistence.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/003-status.md) |
| D004 | [Quota, claims, and fair-share scoring](004-quota-claims-and-fair-scoring.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/004-status.md) |
| D005 | [Health, backoff, circuit, and quarantine](005-health-backoff-circuit-and-quarantine.md) | invariant/capability | closed; see [closure](../../closure/routing-domain/005-status.md) |
| D006 | [Routing eligibility, fairness, and local claims](006-routing-eligibility-fairness-and-claims.md) | capability/invariant | historical closure; see [closure](../../closure/routing-domain/006-status.md) |
| D007 | [Model-router registry and affinity](007-model-router-registry-and-affinity.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/007-status.md) |
| D008 | [Differential qualification and M5 closure](008-differential-qualification-and-closure.md) | invariant | historical aggregate closure; see [closure](../../closure/routing-domain/008-status.md) |
| D009 | [Selection fairness and frozen routing-trace correction](009-selection-fairness-and-trace-snapshot-correction.md) | invariant/corrective | **ready for handoff**; post-D008 independent review |

D009 is a bounded corrective pass. It does not invalidate unrelated D001-D008 evidence; it corrects two uncovered D006 selection-contract defects: random fairness is not exercised by the actual accepted claim path, and accepted routing traces can be rebuilt from post-claim state rather than frozen at selection.

Until accepted D009 closure, M5 is reopened and M6 implementation handoff is blocked. M6 research/planning may continue. M7 remains blocked on M6.

Every plan receives an individual closure record under `migration-rs/closure/routing-domain/` before its hard successor becomes dependency-ready. Historical closure records are append-only; D009 gets a new `009-status.md` rather than rewriting D006/D008 evidence.
