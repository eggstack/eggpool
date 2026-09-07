# M7 Coordinator Implementation Handoffs

Status: implementation active; C011 dependency-ready

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| C001 | [Coordinator contract and deterministic failure corpus](001-contract-and-failure-corpus-freeze.md) | invariant/infrastructure | closed; see [closure](../../closure/coordinator/001-status.md) |
| C002 | [Durable dispatch publication and lifecycle identity](002-durable-dispatch-publication-and-lifecycle-identity.md) | invariant/capability | closed; see [closure](../../closure/coordinator/002-status.md) |
| C003 | [Runtime wire resolution and negotiation ownership](003-runtime-wire-resolution-and-negotiation.md) | capability/invariant | historical closure; corrected by C012/C013 |
| C004 | [Provider-bound attempt construction and upstream submission](004-provider-attempt-construction-and-submission.md) | capability/invariant | historical closure; corrected by C012/C013 |
| C005 | [Failure effects, retry budget, and failover](005-failure-effects-retry-and-failover.md) | invariant/capability | historical closure; corrected by C012/C013; C014 applies residual Retry-After fix |
| C006 | [Durable finalization and retained terminal ownership](006-durable-finalization-and-retained-ownership.md) | invariant | historical closure; corrected by C012/C013; C014 applies residual finalization fix |
| C012 | [Coordinator core contract correction](012-coordinator-core-contract-correction.md) | invariant/corrective | closed; see [closure](../../closure/coordinator/012-status.md) |
| C013 | [Coordinator core differential requalification](013-coordinator-core-differential-requalification.md) | invariant/corrective | closed; see [closure](../../closure/coordinator/013-status.md) |
| C014 | [Finalization idempotency and Retry-After closure](014-finalization-idempotency-and-retry-after-closure.md) | invariant/corrective | closed; see [closure](../../closure/coordinator/014-status.md) |
| C007 | [Finite response handoff and completion](007-finite-response-handoff-and-completion.md) | capability/invariant | closed; see [closure](../../closure/coordinator/007-status.md) |
| C008 | [Streaming handoff, timeouts, cancellation, and terminal policy](008-streaming-handoff-timeouts-and-cancellation.md) | capability/invariant | closed; see [closure](../../closure/coordinator/008-status.md) |
| C009 | [Public inference endpoints and semantic-router internal dispatch](009-inference-endpoints-and-semantic-router-dispatch.md) | capability/invariant | closed; see [closure](../../closure/coordinator/009-status.md) |
| C010 | [Crash/restart reconciliation and fault injection](010-crash-restart-reconciliation-and-fault-injection.md) | invariant | closed; see [closure](../../closure/coordinator/010-status.md) |
| C011 | [Differential qualification and M7 closure](011-differential-qualification-and-m7-closure.md) | invariant | **dependency-ready** |

Only `migration-rs/registry.md` authorizes an implementation handoff. C011 is now the sole ready coordinator plan after accepted C010 closure.

M7 must not become a Rust copy of Python's large coordinator module. New Rust code should stay organized around explicit request/attempt state, durable publication, retry policy, terminal ownership, and small composable interfaces to M4/M5/M6.

M8 remains responsible for generation publication/rehash/process background lifecycle. C014 is limited to finalization idempotency and retry-delay policy; it must not introduce generation management, perpetual scheduling, or endpoint behavior.
