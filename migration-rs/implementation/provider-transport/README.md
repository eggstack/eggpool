# Provider Transport Implementation Handoffs

Status: closed; M4 provider transport complete

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md`

The M4 Provider HTTP + Eggress workstream is intentionally sequenced so later implementation cannot hide transport mismatches behind routing, provider codecs, or retry behavior.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| T001 | [Contract and fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; [closure record](../../closure/provider-transport/001-status.md) |
| T002 | [Direct Hyper/Rustls provider HTTP core](002-direct-hyper-rustls-core.md) | infrastructure | closed; [closure record](../../closure/provider-transport/002-status.md) |
| T003 | [Eggress connector and proxy parity](003-eggress-connector-and-proxy-parity.md) | infrastructure/capability | closed; [closure record](../../closure/provider-transport/003-status.md) |
| T004 | [Provider/account client pool and lifecycle](004-provider-account-client-pool.md) | capability/invariant | closed; [closure record](../../closure/provider-transport/004-status.md) |
| T005 | [Differential qualification and M4 closure](005-differential-qualification-and-closure.md) | invariant | closed; see [closure record](../../closure/provider-transport/005-status.md) |

T001-T005 are closed with individual closure records. M5 planning is unblocked
by the stable transport handoff; no M5 implementation plan is registered in
this workstream yet.
