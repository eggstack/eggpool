# Planning Governance Milestone 002 — Review index, subsystem ownership, and census reconciliation

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/planning-governance-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for subsystem ownership
- `plans/003-planning-process.md` — §2.3 roadmap structure, §3 work classification, §7 registry requirements

Applicable ADRs:

- None required. The planning convention, the hierarchy, and the registry
  discipline are unchanged.
- Stop for architecture review if the reconciliation would require changing
  the canonical long-term documents, which this milestone must not touch.

Primary class: polish

## 1. Objective

Make the architecture review index and the agent workflow index describe the
shipped runtime accurately, and add the guards that stop the documented
censuses from drifting away from the code again.

## 2. Why this milestone is ready

No dependencies. Every correction is a documentation and guard change with no
production runtime component.

## 3. Current implementation evidence

**The documented CLI census is stale.** `architecture/overview.md` states
"34 commands" and then lists them. The command tree in `rust/src/cli.rs`
declares 35 top-level variants. The undocumented one is `Users`, the
managed-user administration command tree, which carries the bootstrap, add,
disable, enable, show, list, reset-password, token, revoke, and enable-managed
subcommands.

**The documented migration census is stale.** `architecture/overview.md` and
`AGENTS.md` both state that the migration chain is "v1–v54". The chain
under `rust/assets/db/migrations/` currently ends at 63. The nine migrations
past 54 are the managed-user identity, web session, request attribution,
model policy, user budget, admin audit, budget enforcement, instance budget,
and default member profile migrations.

**An entire runtime subsystem is missing from the review index.** The
managed-user stack — `rust/src/identity.rs`, `rust/src/web_session.rs`,
`rust/src/user_budget.rs`, `rust/src/instance_budget.rs`, and
`rust/src/model_policy.rs`, together with the admin and member routes under
`rust/src/server/dashboard/admin.rs` and `rust/src/server/auth.rs` — has no
entry in the review index, no entry in the subsystem ownership table in
`architecture/README.md`, and no deep dive. The index mentions identity only
in the sense of provider and account identity. The stack is also absent from
the `AGENTS.md` layout section, which names none of those five modules, so an
agent working from the workflow index does not know roughly five thousand
lines of runtime code exist. The only coverage is a partial mention inside
`architecture/deep-dive-database.md`.

**The review index credits facades with implementation ownership.**
`architecture/overview.md` describes `ir.rs`, `codec.rs`, `codecs.rs`,
`additional_codecs.rs`, `stream.rs`, `adaptation.rs`, `decode.rs`,
`fidelity.rs`, `provenance.rs`, `conformance.rs`, and `tool_calls.rs` as
owning the canonical request semantics, the per-surface codecs, the stream
join, the adaptation and loss policy, the structural decoder, the residue and
conformance facts, and the tool-call accumulator. Every one of those files
under `rust/src/wire/` is a two-to-nine-line compatibility facade that
re-exports the corresponding module from the `eggpool-wire` crate. The same
document states, a few lines earlier, that the root wire modules are narrow
facades, so the document contradicts itself.

**None of these drifts is checked.** The repository has strong guards for
configuration classification, the wire kernel boundary, the provider
profile drift, and the release workflow contract, but no guard relates the
documented censuses to the code they describe.

## 4. Invariants that must not regress

- The canonical long-term documents `plans/000-*` through `plans/003-*` are
  not edited by this milestone.
- `plans/` remains append-only. Closed roadmaps and closure records are not
  rewritten to reflect this work.
- The registry remains a links-only control surface with no duplicated
  requirements.
- The review index remains the map from a module to its authority paths and
  its deep dive.
- The agent workflow index remains a workflow map, not a duplicate of the
  review index.
- The architecture documents describe the shipped Rust runtime, not a
  historical application.
- No production runtime behavior changes.

## 5. Scope

### In scope

- The CLI command census in `architecture/overview.md`.
- The migration chain census in `architecture/overview.md` and `AGENTS.md`.
- The managed-user subsystem's absence from the review index, the subsystem
  ownership table, and the agent workflow index.
- The wire facade ownership claims in `architecture/overview.md`.
- The route topology coverage for the managed-user routes.
- Guards that keep the documented censuses and the module map aligned with
  the code.

### Explicitly out of scope

- Any change to the runtime behavior of the managed-user subsystem. Only its
  documentation coverage changes here.
- Any change to the wire kernel boundary or the facade structure. Only the
  description of the boundary changes here.
- The durable health wiring, the quarantine gating, and the readiness claim.
  Those are routing selection milestones 005 and 006.
- Any change to the canonical long-term documents.
- Any change to `plans/registry.md` structure. Only the entries this
  milestone's own registration adds.
- Any edit to a closed closure record or to a pre-251 legacy plan.
- Any change to the release documentation validation rules.

## 6. Required production changes

**Census correction.** The CLI command count and list in the review index
must match the command tree in `rust/src/cli.rs`. The migration chain census
in both the review index and the agent workflow index must match the chain
under `rust/assets/db/migrations/`.

**Managed-user subsystem coverage.** The review index must gain an entry for
the managed-user subsystem with its authority paths, the subsystem ownership
table must gain a row, and a deep dive must be written for it. The agent
workflow index's layout section must name the modules an agent needs to know
exist.

**Wire ownership correction.** The review index must describe the wire layer
as the facade-and-seam arrangement it actually is: the neutral kernel in the
workspace crate owns the canonical semantics, the root modules are
compatibility facades, and `rust/src/wire/runtime.rs` plus
`rust/src/wire/adapters.rs` are the EggPool-owned parts. The description must
not attribute kernel ownership to a facade.

**Route topology coverage.** The review index's route topology must include
the managed-user routes, or must state that it covers only the inference and
observability routes and point at the deep dive for the rest.

**Census guards.** Add tooling guards that fail when the documented census
drifts from the code:

- a guard that compares the CLI command count and list stated in the review
  index against the command tree;
- a guard that compares the migration chain's highest version stated in the
  review index and the agent workflow index against the migration directory;
- a guard that asserts every top-level module under `rust/src/` is named in
  the agent workflow index's layout section.

The guards must be cheap and must not require building the Rust workspace.
They may parse the source and the documentation as text.

## 7. Ordered work packages

### Work package A — Census correction and guards

Intent:

Correct the two stale censuses and make them self-checking.

Required changes:

- Correct the CLI command count and list in the review index.
- Correct the migration chain census in the review index and the agent
  workflow index.
- Add the CLI census guard and the migration census guard.

Acceptance evidence:

- The guards pass on the corrected documents.
- The guards fail when the census is deliberately perturbed.

### Work package B — Managed-user subsystem coverage

Intent:

Bring the managed-user subsystem into the review index and the agent
workflow index.

Required changes:

- Write the deep dive for the managed-user subsystem: ownership boundary,
  the identity and credential model, the browser session and CSRF boundary,
  the budget intersection, the model policy, the admin and member surfaces,
  and the durable tables.
- Add the review index entry, the subsystem ownership row, and the deep-dive
  link.
- Name the modules in the agent workflow index's layout section.

Acceptance evidence:

- A reviewer can find the managed-user subsystem's ownership and invariants
  from the review index alone.
- The agent workflow index names every module the layout section should
  cover.

### Work package C — Wire ownership correction

Intent:

Describe the wire layer as it actually is.

Required changes:

- Correct the wire ownership paragraph in the review index so it credits the
  kernel crate with the canonical semantics and the root modules with the
  facade and seam roles.
- Remove the internal contradiction between the facade statement and the
  per-module ownership statement.

Acceptance evidence:

- No document attributes kernel implementation ownership to a root wire
  facade.

### Work package D — Route topology coverage

Intent:

Make the route topology complete or explicitly scoped.

Required changes:

- Add the managed-user routes to the route topology, or state the topology's
  scope and point at the deep dive for the rest.

Acceptance evidence:

- Every registered route is either listed or covered by an explicit scope
  statement.

### Work package E — Module-map guard

Intent:

Stop a new top-level runtime module from appearing without a workflow-index
entry.

Required changes:

- Add the guard that compares the top-level modules under `rust/src/` against
  the agent workflow index's layout section.

Acceptance evidence:

- The guard fails when a module is added without an index entry.

## 8. Failure, cancellation, restart, contention semantics

- Not applicable. This milestone changes documentation and adds read-only
  tooling guards. It adds no runtime path, no lock, and no await.

## 9. Compatibility and migration

- No migration. No storage change.
- No configuration key is added, removed, or reclassified.
- No public HTTP or CLI contract change.
- No public Rust API change.
- The guards are read-only and must not modify the repository.

## 10. Required tests

- `tests/tooling/` — a guard test for the CLI census.
- `tests/tooling/` — a guard test for the migration census.
- `tests/tooling/` — a guard test for the module map.
- A new guard test file may be added under `tests/tooling/`. Name it for the
  behavior, not for a milestone number.
- Python tooling tests run under `uv run pytest tests/tooling/`.

## 11. Required verification commands

```bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

This milestone touches `docs/`, `architecture/`, `AGENTS.md`, and `plans/`,
which CI treats as documentation-only and skips for trigger purposes. Run the
tooling suites locally before handing off, because CI will not run for a
documentation-only change. The Rust gates are unaffected by this milestone and
must remain green on the surrounding branch:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

These are the milestone's deliverable:

- `architecture/overview.md` — CLI census, migration census, wire ownership
  paragraph, route topology scope, and the new managed-user entry.
- `architecture/README.md` — the subsystem ownership row, the deep-dive link,
  and the runtime shape paragraph.
- `architecture/deep-dive-on-prem-user-access.md` — a new deep dive.
- `AGENTS.md` — the layout section's module list and the migration census.

## 13. Acceptance criteria

- The CLI command count and list stated in the review index match the command
  tree.
- The migration chain census stated in the review index and the agent
  workflow index matches the migration directory.
- Every top-level module under `rust/src/` is named in the agent workflow
  index's layout section.
- The managed-user subsystem has a review index entry, an ownership row, and
  a deep dive.
- No document attributes wire kernel implementation ownership to a root
  facade.
- The route topology covers every registered route or states its scope
  explicitly.
- The guards fail when a census is deliberately perturbed.
- The Python tooling suites and the release documentation validation pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- The correction would require editing a canonical long-term document.
- The correction would require rewriting a closed closure record or a
  pre-251 legacy plan; add a new corrective record instead.
- A guard would need to build the Rust workspace to run, which would make it
  too slow for ordinary CI.
- The managed-user subsystem's behavior needs a change to be describable; that
  is a separate plan in the on-prem user access subsystem.
- Evidence contradicts an invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  guard that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that each guard fails on a deliberately perturbed input.
- The documentation diff, showing every corrected claim.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

The deliverable here is not the corrected prose. It is the guard that makes
the prose stay correct. Write the deep dive after the guards exist, so the
guards hold while the prose is written.

Do not fold the durable health documentation corrections in here. Those belong
to routing selection milestone 005, which owns the health subsystem's claims.

This milestone touches `AGENTS.md` and `architecture/`, which CI treats as
documentation-only for trigger purposes. Run the tooling suites locally
before handing off, because the CI job may not run for a documentation-only
change.

Python tooling work uses `uv sync --dev` locally and `uv sync --frozen` for
CI parity. Preserve unrelated user changes in the working tree.
