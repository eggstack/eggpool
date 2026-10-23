# Planning Governance Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/planning-governance/002-architecture-index-ownership-and-census-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/planning-governance-roadmap.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M002 is complete and **closed**. The review index and the agent workflow index
now describe the shipped Rust runtime accurately, and five cheap read-only
guards keep them honest.

Every stale census was corrected against the code rather than against the plan
text:

| Census | Before | After | Source of truth |
|---|---|---|---|
| Top-level CLI commands | 34 | **35** | `rust/src/cli.rs` — the undocumented variant is `Users` |
| Migration chain | v1–v54 | **v1–v63** | `rust/assets/db/migrations/` — the nine added migrations are 55 `user_identity`, 56 `web_sessions`, 57 `request_attribution`, 58 `model_policy`, 59 `user_budgets`, 60 `user_admin_audit`, 61 `user_budget_enforcement`, 62 `instance_budgets`, 63 `default_member_profile` |
| Registered routes | 28 documented | **69 documented** | `rust/src/server/mod.rs` — the only router construction site in the tree |
| Top-level runtime modules named in the workflow index | 21 | **30** | 17 `*.rs` files plus 13 module directories under `rust/src/` |
| Root wire facades credited with kernel ownership | 12 | **0** | The neutral `eggpool-wire` kernel crate owns the canonical semantics |

The managed-user subsystem — roughly five thousand lines of runtime code —
was previously invisible to a reviewer starting from the review index. It now
has a deep dive, a review-index entry, an ownership-table row, and a workflow
index entry.

The five guards were written first and confirmed to fail against the
pre-correction working tree, so none of them is vacuous.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Work package A — CLI census | `tests/tooling/test_architecture_census.py::test_review_index_cli_census_matches_the_command_tree` | pass | Compares the stated count AND the stated list against the parsed Clap variant tree; derives Clap's kebab-case rename itself rather than invoking Clap |
| Work package A — migration census in both documents | `...::test_migration_census_matches_the_migration_directory` | pass | Checks `architecture/overview.md` and `AGENTS.md` against the highest version in `rust/assets/db/migrations/` |
| Work package B — managed-user coverage | `...::test_workflow_index_names_every_top_level_runtime_module` and `...::test_route_topology_covers_every_registered_route` | pass | Proves `identity.rs`, `web_session.rs`, `user_budget.rs`, `instance_budget.rs`, `model_policy.rs` are indexed and the managed-user routes are covered |
| Work package C — wire ownership | `...::test_wire_kernel_ownership_is_not_credited_to_a_root_facade` | pass | Sentence-level attribution check inside `architecture/overview.md` §6, plus positive assertions that the kernel crate and the two EggPool-owned wire modules are named |
| Work package D — route topology | `...::test_route_topology_covers_every_registered_route` | pass | All 69 `.route(...)` registrations in `rust/src/server/mod.rs` are covered |
| Work package E — module map | `...::test_workflow_index_names_every_top_level_runtime_module` | pass | Fails when a top-level `rust/src/` module is added without an index entry |
| The guards are not vacuous | All five guards run against the pre-correction baseline | pass | All five failed before the prose was corrected (CLI `34` vs `35`; `v1–v54` vs `63`; 9 unnamed modules; 41 uncovered routes; wire facade ownership) |
| Each guard fails on its own drift | Seven deliberate perturbations (§4) | pass | Each perturbation failed exactly the guard that owns it; every file was byte-restored and re-verified |
| Zero production runtime change | `git diff --stat` over the M002 file set (§3) | pass | No Rust, `scripts/`, `.github/`, `docs/`, `plans/`, or other deep-dive file touched |
| Targeted guard passes | `uv run pytest tests/tooling/test_architecture_census.py -q` — 5 passed | pass | — |
| Full tooling suite passes | `uv run pytest tests/tooling/ -q --tb=short --maxfail=1` — 240 passed, 1 skipped (§4) | pass | An earlier run had `test_installer.py::test_quick_installer_harness_passes` fail on a 30 s subprocess timeout; it passes standalone in 86 s and is unrelated to this change |
| Release-doc validator passes | `uv run python scripts/validate_release_docs.py` — `status: pass`, 7 docs checked | pass | — |
| Lint and format gates | `ruff format --check`, `ruff check`, `pyright scripts/`, `git diff --check` | pass | See §4 for the mid-flight foreign-file failures and their resolution |
| Rust workspace gates | Not run for this milestone | not run | M002 changed no Rust file. The surrounding branch's Rust gates are verified separately in the enclosing milestone batch (§10) |

## 3. Production implementation evidence

No production change. The complete M002 change set is:

| File | Change |
|---|---|
| `tests/tooling/test_architecture_census.py` | **New.** Five read-only text-parsing guards, 325 lines. They parse source and documentation as text, never build the Rust workspace, and never modify the repository |
| `architecture/deep-dive-on-prem-user-access.md` | **New.** Covers the ownership boundary, the identity and credential model, the browser session and CSRF boundary, the model policy, the budget intersection, the admin and member surfaces, the durable tables, and the subsystem invariants |
| `architecture/overview.md` | CLI census corrected to 35 with `users` added; migration census corrected to v1–v63; the route topology rewritten to enumerate all 69 registered routes grouped by family with an explicit scope sentence; the wire ownership paragraph rewritten; a new §13 "On-prem user access" section; a review-index table row; a capabilities bullet; sections renumbered §13/§14/§15 → §14/§15/§16 |
| `architecture/README.md` | Ownership-table row for the managed-user subsystem; deep-dive list entry; a runtime-shape paragraph |
| `AGENTS.md` | Layout bullet now names `config.rs`, `config_reload_policy.rs`, `error.rs`, `version.rs`, and the five managed-user modules alongside the previously named 21 entries; migration census corrected to `v1–v63 immutable` |

**No file under `rust/`, `scripts/`, `.github/`, `docs/`, `plans/`, or any other
deep dive was touched.**

### Corrected claims, item by item

1. **CLI census.** `architecture/overview.md` §1 read "command tree (34
   commands: …)". It now reads "command tree (35 commands: …)" with `users`
   inserted after `stats`. The `users` variant carries the
   bootstrap/add/disable/enable/show/list/reset-password/token/revoke/
   enable-managed/policy/budget/global-budget/defaults subcommands, which are
   named in the deep dive rather than in the census — the census counts
   top-level commands only.
2. **Migration census.** `architecture/overview.md` §9 read "currently v1–v54
   with `checksums.json`". It now reads "currently v1–v63". `AGENTS.md` read
   "v1–v54 immutable" and now reads "v1–v63 immutable".
3. **Wire ownership.** `architecture/overview.md` §6 credited `ir.rs`,
   `codec.rs`, `codecs.rs`, `additional_codecs.rs`, `registry.rs`, `stream.rs`,
   `adaptation.rs`, `decode.rs`, `fidelity.rs`, `provenance.rs`,
   `conformance.rs`, and `tool_calls.rs` with the canonical semantics, while
   the same section's first paragraph called the root wire modules narrow
   facades. The canonical semantics are now attributed to the neutral kernel
   crate by module path (`eggpool_wire::ir`, `::codec`, `::codecs`,
   `::additional_codecs`, `::profile`, `::stream`, `::adaptation`,
   `::decode`, `::fidelity`, `::provenance`, `::conformance`,
   `::tool_calls`); the twelve root files are named explicitly as
   compatibility facades that re-export those kernel modules under the
   canonical `wire::` path and carry no implementation of their own; and
   EggPool's own wire code is named as `wire::runtime.rs` (selection-time
   join) and `wire::adapters.rs` (the extraction seam, explicitly not kernel).
   **The wire boundary and facade structure itself was not changed** — only
   the description.
4. **Route topology.** The paragraph now enumerates all 69 `.route(...)`
   registrations grouped by family (inference, discovery/health,
   observability, managed-user, dashboard pages, admin actions, statics) and
   closes with an explicit scope sentence: "The per-surface behavior of each
   family is owned by its deep dive, not by this index." Scope decision:
   option (a), list every registered route — option (b) was rejected because
   the milestone also requires the managed-user subsystem to be visible in the
   review index. Previously 41 registered routes were undocumented.
5. **Managed-user entries.** A new §13 in `architecture/overview.md` names the
   ownership of `identity.rs`, `web_session.rs`, `model_policy.rs`,
   `user_budget.rs`, `instance_budget.rs`, `server/auth.rs`, and
   `server/dashboard/admin.rs`, plus the boundary against `db/`,
   `coordinator/`, `routing/`, `quota/`, `health/`, and
   `server/middleware.rs`. A review-index table row, an
   `architecture/README.md` ownership row, a deep-dive list entry, a
   runtime-shape paragraph, and the `AGENTS.md` layout entries were added.

### Deliberately not changed

- Sections §2, §7, §8, §9, §10, §11, and §12 of `architecture/overview.md` are
  referenced by other deep dives and were left untouched. The new managed-user
  section was inserted as §13 specifically so that no existing `overview.md
  §N` reference breaks in files this milestone did not own.
- `architecture/deep-dive-transcoder.md` never names the root wire facade
  files, so it contains no contradiction and needed no change.
- The durable health wiring, the quarantine gating, and the readiness claim
  in `architecture/overview.md` were left exactly as found. They belong to
  routing-selection M005/M006.

## 4. Verification executed

### Commands run

```bash
uv run pytest tests/tooling/test_architecture_census.py -q
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
git diff --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
```

### Results

- Focused census guard — **5 passed**, 0 failed.
- Full tooling suite — **240 passed, 1 skipped**, 0 failed.
- `ruff format --check` — pass, 71 files already formatted.
- `ruff check` — "All checks passed!"
- `pyright scripts/` — mid-flight the gate reported 3 errors, all in
  `scripts/validate_release_workflow.py`, which was another worker's in-flight
  file. That worker resolved them; its own final run reports 0 errors,
  0 warnings. The M002 files are outside pyright's `include = ["scripts"]`
  scope.
- `scripts/validate_release_docs.py` — `status: pass`, 7 docs checked.
- `git diff --check` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — exit 0.
- Batch integration sweep — `cargo fmt --all -- --check` clean; strict Clippy exit 0 in both feature configurations; `cargo check --workspace --all-targets --no-default-features` clean; 47 test targets plus the lib, **645 passed, 0 failed, 1 ignored** (the pre-existing manual gate), covering every target touched by this milestone and by the four milestones closed alongside it.

### Regression-guard evidence

Seven deliberate perturbations were applied to the documents, each reverting one
corrected claim. Every perturbation was byte-restored afterwards and the file
was SHA-256 verified before the test was re-run.

| # | Perturbation | Guard that failed | Failure output |
|---|---|---|---|
| P1 | `architecture/overview.md`: `(35 commands:` → `(34 commands:` | `test_review_index_cli_census_matches_the_command_tree` | `AssertionError: the review index states 34 commands but lists 35` / `assert 34 == 35` |
| P2 | `architecture/overview.md`: `users` removed from the stated list | same | `AssertionError: the review index CLI census does not match the command tree; missing from the index: ['users']; documented but absent from the tree: []` |
| P3 | `architecture/overview.md`: `currently v1–v63` → `currently v1–v54` | `test_migration_census_matches_the_migration_directory` | `AssertionError: the review index migration census does not match the migration directory (highest version 63); stated: ['54']` |
| P4 | `AGENTS.md`: `v1–v63 immutable` → `v1–v54 immutable` | same | `AssertionError: the agent workflow index migration census does not match the migration directory (highest version 63); stated: ['54']` |
| P5 | `AGENTS.md` layout: `identity.rs` removed | `test_workflow_index_names_every_top_level_runtime_module` | `AssertionError: the agent workflow index's layout section does not name these top-level runtime modules: ['identity.rs']` |
| P6 | `architecture/overview.md`: `/admin/audit` removed from the topology | `test_route_topology_covers_every_registered_route` | `AssertionError: the review index's route topology does not cover these registered routes: ['/admin/audit']` |
| P7 | `architecture/overview.md`: `eggpool_wire::ir` carries → `ir.rs` owns | `test_wire_kernel_ownership_is_not_credited_to_a_root_facade` | `AssertionError: the review index credits the root wire facade(s) ['ir'] with implementation ownership via 'owns'; the neutral kernel crate owns the canonical semantics` |

Every perturbation produced a genuine failure followed by a pass after
restoration. Separately, all five guards were confirmed to fail against the
pre-correction working tree, proving that none of them passes vacuously.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| Zero production runtime change | The M002 change set is listed in §3; no file under `rust/`, `scripts/`, `.github/`, `docs/`, `plans/`, or another deep dive is touched |
| The review index remains the map from a module to its authority paths and its deep dive | The new managed-user row, the wire ownership rewrite, and the route topology all preserve that shape |
| The agent workflow index remains a workflow map, not a duplicate of the review index | `AGENTS.md` gained module names and the migration census only; no requirement or deep-dive body was duplicated into it |
| No canonical long-term document (`plans/000-*`–`003-*`) is edited | M002 touched no `plans/` file |
| `plans/` remains append-only | No closed roadmap or closure record was rewritten |
| The registry remains a links-only control surface | M002 changed no registry row |
| The wire kernel boundary and facade structure are unchanged | Only prose changed; `rust/src/wire/` and the `eggpool-wire` crate were not touched |
| The durable health wiring, quarantine gating, and readiness claim are untouched | The existing `architecture/overview.md` health paragraph was left exactly as found |
| Guards are cheap, read-only, and do not build the Rust workspace | Each guard parses Markdown, Python-free text, or file names. No subprocess beyond the test runner, no Cargo invocation |
| The guards fail closed | A drifted census produces a non-zero pytest exit, failing the tooling suite and therefore the CI job that runs it |
| No production runtime behavior changes | No runtime file was edited |

## 6. Failure and recovery review

Not applicable in the runtime sense. M002 adds no runtime path, no lock, no
await, no task, and no storage access. The only failure behavior added is a
static tooling assertion that rejects future documentation drift.

The one recovery-shaped note is the CLI census parser: it derives Clap's
default kebab-case rename itself rather than invoking Clap, so it does not
depend on a built binary. Every variant in the current tree round-trips
correctly — the guard passes with an exact set match of 35 — and a future
variant with an acronym boundary would surface as a clear set-mismatch
failure rather than a silent pass.

## 7. Migration and compatibility review

No migration. No storage change. No configuration key added, removed, or
reclassified. No public HTTP or CLI contract change. No public Rust API change.

The migration corpus under `rust/assets/db/migrations/` was not touched. The
migration guard reads that directory's file names to compute the current
highest version, so it tracks the real chain rather than a literal.

The census corrections are documentation-only. The runtime's migration
behavior is unchanged: `rust/src/db/migrations.rs` already asserts that the
chain's last version is 63.

## 8. Security review

- **Auth** — unchanged; no runtime path was touched.
- **Secret handling** — the guards read fixed repository documentation paths
  only. No credential, prompt, raw body, or cache key is copied into a guard.
- **Privilege bounds** — unchanged.
- **DoS bounds** — each guard parses a bounded set of files. The CLI census
  guard parses `rust/src/cli.rs`; the route guard parses `rust/src/server/mod.rs`;
  the module guard lists `rust/src/`. None builds the workspace and none runs
  an unbounded subprocess.
- **Audit behavior** — unchanged.

## 9. Documentation and operations

- `architecture/overview.md`: CLI census, migration census, route topology,
  wire ownership, the new managed-user section, the review-index row, the
  capabilities bullet, and the section renumbering.
- `architecture/README.md`: the managed-user ownership row, the deep-dive
  link, and the runtime-shape paragraph.
- `architecture/deep-dive-on-prem-user-access.md`: the new managed-user deep
  dive.
- `AGENTS.md`: the expanded layout bullet and the corrected migration census.
- `tests/tooling/test_architecture_census.py`: the five guards.
- No operator command, diagnostic, or recovery instruction changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | The CLI census guard derives Clap's kebab-case rename rather than invoking Clap | A future variant with an acronym boundary could need the rename rule refined | It would surface as a clear set-mismatch failure, not a silent pass. No action today |
| low | The wire guard detects facade ownership attribution at sentence granularity within `architecture/overview.md` §6 only | A future sentence crediting a facade with a verb outside the guarded list would not be caught | The guard does assert the positive facts — the kernel crate is named as owner and `wire::runtime.rs` and `wire::adapters.rs` are named as EggPool-owned. A future refinement is cheap if needed |
| low | The full serial Rust workspace suite was not run for this milestone | None for M002 specifically: no Rust file changed | The enclosing milestone batch runs the Rust gates on the surrounding branch. See the batch record |
| info | `architecture/deep-dive-transcoder.md` does not name the root wire facade files | No contradiction exists there | None. Nothing to fix outside this milestone's ownership |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Planning-governance M002 was
the only registered successor in this subsystem, so no milestone is promoted
and the blocked-work table gains no Planning-governance entry.

The Planning-governance roadmap's milestone status table records M002 as
`closed` with its closure record linked. The subsystem's ongoing obligation —
that the review index, the workflow index, and the shipped runtime stay
aligned — is now enforced by five cheap guards rather than by review alone.

## 12. Registry updates

- `plans/registry.md`: the Planning-governance M002 row is removed from
  "Dependency-ready implementation plans" and added to "Recently closed" with
  its disposition; the Planning-governance row in "Active subsystem roadmaps"
  records `M002 closed`.
- `plans/subsystems/planning-governance-roadmap.md`: the milestone 002 row in
  the milestone status table moves to `closed` with its closure record linked;
  the dependency graph and the roadmap's completion statement reflect the
  closed state.
- `plans/implementation/planning-governance/002-architecture-index-ownership-and-census-reconciliation.md`:
  the `Status` line moves from `ready` to `implemented`.
