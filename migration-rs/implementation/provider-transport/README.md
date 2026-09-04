# Provider Transport Implementation Handoffs

Status: active workstream

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md`

The M4 Provider HTTP + Eggress workstream is intentionally sequenced so later implementation cannot hide transport mismatches behind routing, provider codecs, or retry behavior.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| T001 | [Contract and fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; [closure record](../../closure/provider-transport/001-status.md) |
| T002 | [Direct Hyper/Rustls provider HTTP core](002-direct-hyper-rustls-core.md) | infrastructure | closed; [closure record](../../closure/provider-transport/002-status.md) |
| T003 | [Eggress connector and proxy parity](003-eggress-connector-and-proxy-parity.md) | infrastructure/capability | ready; T001 feature decision and T002 closed |
| T004 | [Provider/account client pool and lifecycle](004-provider-account-client-pool.md) | capability/invariant | queued behind T003 |
| T005 | [Differential qualification and M4 closure](005-differential-qualification-and-closure.md) | invariant | queued behind T004 |

Only the first plan whose hard dependencies are closed should be treated as dependency-ready. Each implementation milestone requires its own closure record before the next plan advances.
