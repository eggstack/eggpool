# EggPool Terminology and Domain Model

Status: normative language reference for all interim planning

Companion documents: `plans/000-long-term-specification.md`,
`plans/002-long-term-roadmap.md`.

When older flat plans (`plans/001-*`…`plans/250-*`) use overlapping terms,
this document is normative.

## Identity and lifecycle terms

- **RuntimeGeneration** — an immutable, atomically published snapshot of all
  runtime-owned services for one configuration epoch
  (`rust/src/runtime_lifecycle/`). Published by `RuntimeManager`; retired
  asynchronously while in-flight leases drain.
- **GenerationLease** — a guard held by one request (or admission) on the
  generation it acquired. Retiring generations serve only drained in-flight
  work; new work always acquires the active generation.
- **Rehash** — live configuration reload path: classify (via
  `config_reload_policy.rs::classify_transition`), build a candidate
  generation, atomically publish or fail closed. Never mutates process-owned
  services or persisted provider/account state before publication.
- **Reload vs restart** — the classify_transition verdict. Mixed changes are
  wholly restart-required. `rehash` reclassifies before publication.
- **Startup construction** — initial generation build at process start. The
  long-term direction is full parity between startup and reload construction
  (single builder; see `plans/002-*` roadmap).
- **Admission** — `rust/src/request/admission.rs` producing a bounded
  `CanonicalRequest` (+ source-native preservation envelope for Responses).
  Stateless policy enforced here (e.g. `store`/`previous_response_id` rules).
- **Endpoint execution** — the single `coordinator/endpoints.rs` call per
  request that owns finite/streaming selection and `from_admitted`
  construction from one parsed body.
- **Attempt** — one upstream submission within the shared
  upstream-submission budget. Prepared synchronously
  (`PreparedUpstreamAttempt` fully owned before `submit_once` is awaited).
- **Publication** — durable persistence of request/attempt identity before
  upstream dispatch (`coordinator/publication.rs`).
- **Finalization** — post-dispatch recording of usage, reservation/claim
  release, and health effects
  (`coordinator/finalization.rs`, `coordinator/streaming/terminal.rs`).
- **Canonical IR** — `rust/src/wire/ir.rs`, the single wire boundary between
  routing/coordinator and provider transport.
- **Provider pool topology** — the immutable nested provider/account
  topology published by `ProviderClientPool`, closed atomically on
  replacement.

## Classification terms (planning only)

- **Invariant** — must remain true across releases/strategies (e.g.
  generation-owned execution, single SQLite gate, fail-closed reload).
- **Capability** — user/operator-visible behavior (e.g. compact admission,
  Messages surface, `readyz`).
- **Infrastructure** — internal machinery capabilities depend on (e.g.
  publication pipeline, pool topology publication).
- **Polish** — ergonomics, diagnostics, performance, cleanup, docs.

## Status terms (post-251 planning)

`proposed`, `ready`, `active`, `blocked`, `closing`, `closed`,
`conditionally closed`, `superseded`, `archived` — as defined in
`plans/003-planning-process.md` and tracked in `plans/registry.md`. Flat-plan
headers (`draft`, `ready for implementation`, `implementation handoff`,
`complete`, `closure`) are legacy vocabulary and MUST NOT be used for new
hierarchy documents.
