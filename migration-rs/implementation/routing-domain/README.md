# M5 Routing Domain Implementation Handoffs

Status: active; D001-D007 closed; D008 ready for handoff

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md`

M5 is sequenced so policy/state parity is established before inference dispatch exists. The plans deliberately keep transport, request codecs, coordinator retry/finalization, runtime generation publication, and background lifecycle outside this workstream.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| D001 | [Contract and deterministic fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; see [closure](../../closure/routing-domain/001-status.md) |
| D002 | [Account registry and catalog cache/hydration](002-account-registry-and-catalog-cache.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/002-status.md) |
| D003 | [Catalog refresh, normalization, and persistence](003-catalog-refresh-normalization-and-persistence.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/003-status.md) |
| D004 | [Quota, claims, and fair-share scoring](004-quota-claims-and-fair-scoring.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/004-status.md) |
| D005 | [Health, backoff, circuit, and quarantine](005-health-backoff-circuit-and-quarantine.md) | invariant/capability | closed; see [closure](../../closure/routing-domain/005-status.md) |
| D006 | [Routing eligibility, fairness, and local claims](006-routing-eligibility-fairness-and-claims.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/006-status.md) |
| D007 | [Model-router registry and affinity](007-model-router-registry-and-affinity.md) | capability/invariant | closed; see [closure](../../closure/routing-domain/007-status.md) |
| D008 | [Differential qualification and M5 closure](008-differential-qualification-and-closure.md) | invariant | dependency-ready; D001-D007 closed |

D008 is the current sole dependency-ready plan. D007 is closed with an accepted closure record.

Every plan receives an individual closure record under `migration-rs/closure/routing-domain/` before its hard successor becomes dependency-ready.
