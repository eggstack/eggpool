# EggPool Planning and Agent-Handoff Process

Status: normative planning governance

Adapted from CodeGG `plans/003-planning-process.md`
(https://github.com/dbowm91/codegg) to EggPool paths, commands, and
ownership. CodeGG product content is not applicable.

The keywords MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, and MAY are
normative.

## 1. Purpose

Two horizons, kept separate:

1. **Long-term planning** (`000`, `001`, `002`, this document): product
   identity, runtime boundaries, invariants, capability dependencies,
   non-goals, end-state acceptance criteria.
2. **Interim planning** (`subsystems/`, `implementation/`, `closure/`):
   bounded work against a repository baseline, handed to coding agents.

Interim plans may surface evidence warranting a long-term change, but MUST
NOT silently edit long-term direction to match the easiest implementation.

## 2. Document classes

### 2.1 Canonical long-term documents

`000-long-term-specification.md`, `001-terminology-and-domain-model.md`,
`002-long-term-roadmap.md`, and this governance document. Stable during
ordinary implementation; amended only for intentional direction change,
contradiction/omission, accepted ADR effect, or explicit user direction. A
corrective pass alone never justifies a long-term change.

### 2.2 Architecture decision records

One durable decision affecting several milestones, subsystems, or public
contracts. MUST state: context/forces, alternatives, decision, consequences,
affected long-term sections/subsystems, migration/compatibility, status
(`proposed`, `accepted`, `rejected`, `deprecated`, `superseded`). Accepted
ADRs MUST NOT be rewritten; supersede with a link.

EggPool ADR threshold (normally required): reload-vs-restart boundary
changes; new server/provider/storage protocol; durable dependency selection
(Eggfetch/Eggress/EggServe); auth semantics; generation/lease/fencing
semantics; public compatibility contract; material non-goal change. NOT
required for local refactors, naming, internal structures, or reversible
optimizations preserving contracts.

### 2.3 Subsystem roadmaps

`subsystems/<subsystem>-roadmap.md`. Longer-lived than plans, adaptable vs
the canonical roadmap. MUST define: purpose/ownership boundary; spec/term
references; invariants and non-goals; current-state summary; dependency
graph; ordered milestones; user-visible exit conditions; cross-cutting
(storage, protocol, security, concurrency, observability) concerns; risks and
deferred work. SHOULD avoid commit-specific file lists, exact line numbers,
mechanical sequences. MUST NOT mark capability complete on infrastructure
landing alone.

### 2.4 Milestone implementation plans

Primary handoff artifact. MUST be independently executable, bounded, tied to
a repository baseline, and include: source roadmap/milestone; ADRs and
long-term refs; objective + non-goals; current implementation evidence;
non-regressing invariants; expected production changes; storage/protocol/
migration effects; ordered work packages; focused + broad verification
commands; static guards + doc updates; acceptance + stop conditions; closure
evidence required. MAY change with repository reality; material deviations
MUST be recorded.

Sizing: one coherent agent pass (one ownership boundary, prod changes,
tests, verification, docs). Too large: several releasable capability
boundaries, unrelated migrations, several unresolved ADRs. Too small: bare
rename/isolated coverage, unless a corrective unblock. Prefer vertical
slices with a consumer over horizontal refactors with none.

### 2.5 Closure records

`closure/<subsystem>/NNN-status.md` determines completion. MUST include:
implementation commits; requirement-to-evidence matrix; tests/guards run
with outcomes; migration/compatibility evidence; security/contention
evidence where applicable; docs/ops evidence; limitations; severity-tagged
unresolved findings; recommendation (`closed`, `conditionally closed`,
`corrective pass required`, `blocked`). A commit message is not closure
evidence.

### 2.6 Archive records

Post-251 completed/superseded/abandoned interim plans SHOULD move under
`plans/archive/` preserving relative structure. Pre-251 flat files stay at
top level as the legacy archive. Canonical docs + accepted ADRs MUST NOT be
archived merely because implementation completed.

## 3. Work classification

Every planned item gets one primary class: **invariant** (always true;
needs guards/property tests), **capability** (visible behavior; needs
end-to-end acceptance), **infrastructure** (internal machinery; MUST NOT be
presented as capability until a consumer path exists), **polish**
(ergonomics/diagnostics/perf/cleanup/docs; normally after correctness
closure).

## 4. Dependency model

Each milestone declares dependencies as **hard** (cannot correctly begin
before close), **interface** (may proceed against agreed contract/test
double), **soft** (parallel possible, integration depends), or
**operational** (can land; deploy/release needs external evidence).
Dependency-ready = all hard closed + all interface contracts stable.
Registry MUST identify blocked milestones + blockers.

## 5. Agent handoff contract

Default authority order: (1) canonical spec/terminology, (2) accepted ADRs,
(3) subsystem roadmap, (4) milestone implementation plan, (5) current
repository evidence. On conflict: preserve long-term invariants, record the
discrepancy, smallest coherent adjustment; never invent architecture to
finish a checklist. Agents MUST inspect code before editing, preserve
unrelated user changes, use typed generation/request context, keep
`server/*` thin, update tests + `architecture/` docs with code, and report
residual risks + incompletes. Credentials/prompts/bodies/cache keys stay out
of plans.

## 6. Corrective passes

A corrective pass is a NEW implementation plan (same subsystem, new local
number), not an edit pretending success. MUST reference original plan +
closure, list unclosed requirements/defects, explain why verification
missed them, add regression tests/guards, avoid reopening closed scope
without evidence. Repeated correctives ⇒ revise roadmap/sizing.

## 7. Registry requirements

`plans/registry.md` is the compact control surface: active roadmaps,
dependency-ready plans, active/closing work, blocked work + blockers, recent
closures. Links only, no duplicated requirements. One commit = one status
change. Closing a milestone REQUIRES auditing blocked work and promoting
newly-ready plans (or recording why still blocked) in the same commit.

## 8. Required planning review (before handoff)

Correct long-term refs; unresolved ADRs or explicit out-of-scope; dependency
readiness; bounded scope + non-goals; ownership + invariants; migration/
compat effects; concurrency/cancellation/restart/failure semantics; security
effects; test/guard evidence; unambiguous closure criteria. If unanswerable,
not ready for handoff.

## 9. Planning anti-patterns (prohibited)

Transient TODOs in canonical docs; one roadmap mixing all subsystems at file
granularity; broad goals without bounded milestone contracts; compilation =
closure; per-subsystem terminology drift; plans silently overriding accepted
architecture; stale active plans after material repo change; duplicated
requirements without one authoritative source; success-only evidence;
polish before correctness closure.
