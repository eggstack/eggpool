# Provider Transport Implementation Handoffs

Status: closed after corrective pass

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md`

The M4 Provider HTTP + Eggress workstream is sequenced so transport mismatches cannot be hidden behind routing, provider codecs, or retry behavior.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| T001 | [Contract and fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; [closure record](../../closure/provider-transport/001-status.md) |
| T002 | [Direct Hyper/Rustls provider HTTP core](002-direct-hyper-rustls-core.md) | infrastructure | closed; [closure record](../../closure/provider-transport/002-status.md) |
| T003 | [Eggress connector and proxy parity](003-eggress-connector-and-proxy-parity.md) | infrastructure/capability | historical closure; [closure record](../../closure/provider-transport/003-status.md) |
| T004 | [Provider/account client pool and lifecycle](004-provider-account-client-pool.md) | capability/invariant | closed; [closure record](../../closure/provider-transport/004-status.md) |
| T005 | [Differential qualification and initial M4 closure](005-differential-qualification-and-closure.md) | invariant | historical closure; [closure record](../../closure/provider-transport/005-status.md) |
| T006 | [Extended proxy runtime interoperability closure](006-extended-proxy-runtime-qualification.md) | invariant/corrective | closed; [closure record](../../closure/provider-transport/006-status.md) |

T006 was added after independent review found that mandatory Shadowsocks/SSR/Trojan/SSH rows were construction-qualified only even though the frozen T001/T003 contract requires runtime evidence or an explicit supported-difference decision.

The previous closure records remain traceable and valid for the behavior they actually tested. Aggregate M4 is now closed after T006. M5 planning and implementation handoff work is unblocked; no nonexistent M5 plan is marked dependency-ready.
