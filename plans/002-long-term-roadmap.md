# EggPool Long-Term Roadmap

Status: execution roadmap for `plans/000-long-term-specification.md`

Terminology: `plans/001-terminology-and-domain-model.md`

This roadmap orders the work needed to sustain the EggPool end state while
preserving a working proxy at every stage. Each phase MUST leave the
repository coherent and MUST include focused implementation plans, tests,
documentation, and closure evidence before dependents are treated as ready.

Dependency-ordered, not calendar-ordered. Parallel work only where noted.

## Cross-phase execution rules

Every phase MUST:

1. preserve generation-owned execution and fail-closed reload/restart semantics;
2. keep `server/*` thin (no coordinator retries/finalization);
3. keep credentials, prompts, raw bodies, cache keys out of persistence/logs/diagnostics;
4. maintain `--no-default-features` compile/test parity;
5. run the serial Rust suite (`--test-threads=1`) and the repo lint matrix before claiming closure;
6. update `architecture/` docs and static guards with code;
7. record explicit exit evidence in the implementation plan or closure record.

## Phase 0 — Planning-governance bootstrap (this transition)

Objective: land the CodeGG-style hierarchy without touching runtime.

- Deliverables: `plans/README.md`, `000–003`, `registry.md`, `adrs/` +
  `subsystems/` + `implementation/` + `closure/` + `archive/` READMEs,
  expanded `plan` skill, first `planning-governance` roadmap + M001 plan +
  closure.
- Exit criteria: legacy flat plans untouched; registry truthful; closure
  record with requirement→evidence matrix; `git diff --check` clean.
- Status: active (Plan 251).

## Phase 1 — Transport and admission hardening (sustaining)

Objective: preserve Plans 243/250 transport posture and Plan 249 admission
coverage across dependency bumps.

- Representative work: Eggress/Eggfetch/EggServe requalification passes,
  inference-route classification guards, body-admission limit enforcement.
- Dependencies: none beyond current baseline.
- Exit criteria: exact-pin + feature-graph + footprint + no-default build
  evidence per requalification.

## Phase 2 — Runtime-lifecycle parity (startup ≡ reload construction)

Objective: unify startup and reload construction behind one builder with
all-or-nothing commit semantics (legacy flat Plans 001–010 direction,
restated under the new convention; NOT reopened automatically — each slice
needs a fresh subsystem roadmap milestone with current baseline evidence).

- Dependencies: Phase 0 (planning only); each milestone needs current
  `runtime_lifecycle/` + `config_reload_policy.rs` evidence.
- Exit criteria per slice: atomic publication, failed-candidate resource
  closure, no stale-lease fallback, legacy mirror consistency.

## Phase 3 — Persistence and publication bounds

Objective: bound restart/reload/backup/restore/recovery for the single-gate
SQLite path before any checkpoint-policy change (Plan 240 handoff constraint).

- Dependencies: Phase 2 slices touching publication/finalization are
  interface dependencies (stable ownership contract required).
- Operational gates: SBC loopback evidence where the claim is about SBC
  behavior; hosted CI alone does not close SBC claims.

## Phase 4 — Operations, integrations, and deployment

Objective: keep `operations/` lifecycle/status/integrations, the
`eggpool-connect` helper, and release/packaging coherent with runtime
changes.

- Rules: reusable policy crates stay neutral; EggPool `Config`/catalog/DB
  IO stays in `operations/integrations.rs`; helper never implies Windows
  proxy support.

## Phase 5 — Optional on-prem small-team access and budgeting

Objective: add identity-bound use of a single locally administered EggPool
instance without regressing the single-owner SBC and LAN deployment.
Authorized clients may route to operator-configured hosted or self-hosted LLMs;
all provider secrets/topology remain under TOML/environment ownership.

- Authority: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md`
  and `plans/subsystems/on-prem-user-access-roadmap.md`.
- Order: secure administrator bootstrap and user credentials; browser-session
  and route authorization; principal propagation/attribution; model-policy
  profiles; durable user budgets; web administration/client projection; small-team
  physical hardware and migration qualification.
- Integration edges: preserve generation leases, publication/finalization
  ownership, provider quota eligibility, single SQLite gate/WAL/NORMAL, and
  compatible Chat/Responses/Messages client authentication.
- Non-goals: multi-organization/tenant hosted management, SaaS billing,
  mandatory cloud/SSO, native public internet access, and unqualified claims
  about 50 concurrent generations.
- Completion: legacy upgrade preserves data and access; fresh bootstrap is
  fail closed without defaults; user policy cannot be bypassed via virtual
  routing or alternate surfaces; per-user budgets recover across cancellation/
  restart; measured SBC and ordinary-hardware evidence scopes supported
  team size and concurrent workload honestly.

Phases 2–5 create NO automatic milestones. Subsystem roadmaps are written on
demand when a slice is ready to be reasoned about, with typed dependencies
(hard / interface / soft / operational) and explicit non-goals.
