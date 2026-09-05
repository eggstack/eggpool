# M7 Coordinator Implementation Handoffs

Status: corrective pass active; C012 dependency-ready, C013 queued, C007 re-blocked

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| C001 | [Coordinator contract and deterministic failure corpus](001-contract-and-failure-corpus-freeze.md) | invariant/infrastructure | closed; see [closure](../../closure/coordinator/001-status.md) |
| C002 | [Durable dispatch publication and lifecycle identity](002-durable-dispatch-publication-and-lifecycle-identity.md) | invariant/capability | closed; see [closure](../../closure/coordinator/002-status.md) |
| C003 | [Runtime wire resolution and negotiation ownership](003-runtime-wire-resolution-and-negotiation.md) | capability/invariant | historical closure; C012/C013 correct and requalify post-closure findings |
| C004 | [Provider-bound attempt construction and upstream submission](004-provider-attempt-construction-and-submission.md) | capability/invariant | historical closure; C012/C013 correct and requalify post-closure findings |
| C005 | [Failure effects, retry budget, and failover](005-failure-effects-retry-and-failover.md) | invariant/capability | historical closure; C012/C013 correct and requalify post-closure findings |
| C006 | [Durable finalization and retained terminal ownership](006-durable-finalization-and-retained-ownership.md) | invariant | historical closure; C012/C013 correct and requalify post-closure findings |
| C012 | [Coordinator core contract correction](012-coordinator-core-contract-correction.md) | invariant/corrective | **ready for handoff** |
| C013 | [Coordinator core differential requalification](013-coordinator-core-differential-requalification.md) | invariant/corrective | queued behind C012 |
| C007 | [Finite response handoff and completion](007-finite-response-handoff-and-completion.md) | capability/invariant | re-blocked behind accepted C013 closure |
| C008 | [Streaming handoff, timeouts, cancellation, and terminal policy](008-streaming-handoff-timeouts-and-cancellation.md) | capability/invariant | queued behind C007 |
| C009 | [Public inference endpoints and semantic-router internal dispatch](009-inference-endpoints-and-semantic-router-dispatch.md) | capability/invariant | queued behind C008 |
| C010 | [Crash/restart reconciliation and fault injection](010-crash-restart-reconciliation-and-fault-injection.md) | invariant | queued behind C009 |
| C011 | [Differential qualification and M7 closure](011-differential-qualification-and-m7-closure.md) | invariant | queued behind C010 |

Only `migration-rs/registry.md` authorizes an implementation handoff. C003-C006 closure records remain append-only historical evidence; they are not rewritten to conceal the post-C006 findings.

The corrective pass is deliberately narrow. C012 repairs wire-resolution bounds/inputs, provider-native model/header construction, complete failure/effect semantics, bounded effect ownership, and durable finalization truth/command compatibility. C013 independently proves those repairs against the C001 Python oracle and fault/concurrency matrix. C007 does not resume until C013 closes.

M7 must not become a Rust copy of Python's large coordinator module. New Rust code should remain organized around explicit request/attempt state, durable publication, retry policy, terminal ownership, and small composable interfaces to M4/M5/M6.

M8 remains responsible for generation publication/rehash/process background lifecycle. M7 may implement a bounded retained-finalization supervisor and explicit reconciliation methods, but not a generation manager or perpetual scheduling framework.