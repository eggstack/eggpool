# Persistence Milestone 011 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/persistence/011-post-m010-roadmap-documentation-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-011--post-m010-roadmap-and-documentation-reconciliation`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M011 is complete and **closed**. The persistence planning surface now states
one truth about every milestone it registers:

- the M008 detailed body carries exactly one status declaration instead of two
  adjacent declarations expressing the same rejected verdict;
- the M009 detailed body says `closed` rather than `active`;
- the M010 detailed body says `closed — rejected under the current
  history/outage contract` rather than `closing`;
- the M011 detailed body says `closed` and the milestone status table links its
  closure record;
- the dependency graph, the completion definition, the milestone status table,
  and the registry agree with those bodies.

The sweep of current-authority persistence surfaces also corrected two
present-tense schema claims that had drifted with the managed-user migration
chain: the roadmap's cross-cutting storage requirement and the M010
investigation record both pinned "schema 54" while the chain now ends at 63.
The investigation record's own census remains explicitly scoped to the schema
current when it ran, which is a historical fact and was not rewritten.

The milestone adds a narrow, cheap, read-only guard. Seven distinct
perturbations were each shown to fail the specific guard that owns them.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| M008 detailed body has exactly one status declaration | `tests/tooling/test_persistence_current_docs.py::test_m008_detailed_body_declares_exactly_one_status` | pass | The body previously declared `Status:` twice for the same verdict; the richer single declaration is retained |
| M008 still reads as closed and rejected | `...::test_persistence_roadmap_records_m008_as_closed_and_rejected` | pass | The pre-existing M007/M008 guard is retained unchanged and still passes |
| M009 detailed body says closed, not active/ready/closing | `...::test_m009_detailed_body_is_closed_not_in_flight` | pass | Exactly one status declaration; `Status: active` was the drift |
| M010 detailed body says closed/rejected, not ready/active/closing | `...::test_m010_detailed_body_is_closed_and_rejected` | pass | `Status: closing` was the drift |
| M011 body is closed and its closure record is linked | `...::test_m011_detailed_body_is_closed_and_links_its_closure_record` | pass | The milestone status table row links `plans/closure/persistence/011-status.md` |
| M009 and M010 closure links are present in the milestone table | `...::test_roadmap_milestone_table_links_m009_and_m010_closures` | pass | The table links the 009, 010, and 011 closure records |
| The registry registers no Persistence dependency-ready, active-implementation, or blocked row after M011 closure | `...::test_registry_registers_no_persistence_executable_work` | pass | Only the three executable-work tables are parsed; historical registry prose is deliberately not parsed |
| The persistence roadmap remains top-level active | `...::test_roadmap_remains_top_level_active` | pass | The storage tail is still unresolved, so the roadmap stays `active` |
| The current production WAL/NORMAL invariant remains present | `...::test_production_wal_normal_invariant_remains_present` | pass | Asserted in both the roadmap and the database deep dive |
| The current-authority schema census is not pinned to a superseded version | `...::test_roadmap_states_the_current_schema_census` and `...::test_investigation_record_does_not_pin_a_superseded_schema_version` | pass | The chain's highest version is read from `rust/assets/db/migrations/` at test time, so the guard tracks the directory rather than a literal |
| The database and background deep dives do not present M007 as pending | `...::test_database_deep_dive_records_final_m007_m008_disposition` and `...::test_background_deep_dive_does_not_present_m007_as_pending` | pass | The pre-existing guards are retained unchanged and still pass |
| Zero production Rust/Cargo/schema/migration/API/config/artifact change | `git diff --stat` over the M011 file set (§3) | pass | The change set is Markdown plus one Python tooling test file |
| No accepted closure record or physical artifact changed | `git status --short` shows no change under `plans/closure/persistence/001-…010-` and no change under `artifacts/` | pass | M011's own closure record is the only new closure file |
| No ADR-0002 exists or is implied | `plans/adrs/` contains only `ADR-0001` and `ADR-0003` | pass | M011 creates no ADR and no plan implies one |
| Targeted lifecycle guard passes | `uv run pytest tests/tooling/test_persistence_current_docs.py -q` — 13 passed | pass | — |
| Full tooling suite passes | `uv run pytest tests/tooling/ -q --tb=short --maxfail=1` — 210 passed, 1 skipped, 0 failed (§4) | pass | The single skip is the pre-existing environment-gated test |
| Release-doc validator passes | `uv run python scripts/validate_release_docs.py` (§4) | pass | — |
| Rust workspace gates | Not run | not run | M011 changed no Rust file. The surrounding branch's Rust gates are verified separately in the enclosing milestone batch (§10) |

## 3. Production implementation evidence

No production change. The complete M011 change set is:

| File | Change |
|---|---|
| `plans/subsystems/persistence-roadmap.md` | M008's duplicate status declaration collapsed to one; M009 `active` → `closed`; M010 `closing` → `closed — rejected under the current history/outage contract`; M011 `ready` → `closed`; the cross-cutting storage requirement no longer pins "schema 54"; the dependency graph, the completion definition, and the milestone status table updated to the closed state |
| `plans/registry.md` | The Persistence M011 row removed from "Dependency-ready implementation plans"; the Persistence roadmap row now reads `M011 closed`; a "Recently closed" row added for Persistence M011 |
| `plans/implementation/persistence/011-post-m010-roadmap-documentation-reconciliation.md` | `Status: ready` → `Status: implemented` |
| `architecture/persistence-control-projection-investigation.md` | The present-tense "Production remains the schema-54 monolithic SQLite database" claim corrected to "the monolithic SQLite database" |
| `architecture/deep-dive-database.md` | The schema-54 census reference is now explicitly scoped as the schema current when the investigation ran |
| `tests/tooling/test_persistence_current_docs.py` | Extended from 3 tests to 13, adding the M008/M009/M010/M011 lifecycle guards, the registry work-table guard, the schema-census guard, and the investigation-record guard |
| `plans/closure/persistence/011-status.md` | This record |

`git diff --stat` over that set is 163 insertions and 26 deletions across five
files plus this record. **No file under `rust/`, no `Cargo.toml`, no
`rust/assets/db/migrations/`, no `scripts/`, no `.github/`, and no
`artifacts/` file is touched.**

The current-authority sweep deliberately left two categories alone:

- **Historical M006/M007/M008 prose.** The M006 detailed body states that it
  preserved "schema 54" because that was the schema when M006 landed. That is
  a historical statement about a delivered milestone and rewriting it would
  falsify the record.
- **The investigation's own census.** `architecture/persistence-control-projection-investigation.md`
  describes an index and field census taken at schema 54, and says so
  explicitly at each occurrence ("generated from the ordered schema-54
  migration SQL", "The `requests` field list was inspected at schema 54",
  "Maintained indexes (schema 54)"). Those are accurate statements about the
  investigation's scope, not claims about the current schema.

## 4. Verification executed

### Commands run

```bash
uv run ruff format tests/tooling/test_persistence_current_docs.py
uv run ruff check tests/tooling/test_persistence_current_docs.py
uv run pytest tests/tooling/test_persistence_current_docs.py -q --tb=short
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

### Results

- `ruff format` — 1 file reformatted; `ruff check` — all checks passed.
- Focused persistence guard — **13 passed**, 0 failed.
- Full tooling suite — **210 passed, 1 skipped**, 0 failed (153.64 s). The
  single skip is the pre-existing environment-gated test.
- `scripts/validate_release_docs.py` — pass.
- `git diff --check` — clean.
- Batch integration sweep — `cargo fmt --all -- --check` clean; strict Clippy exit 0 in both feature configurations; `cargo check --workspace --all-targets --no-default-features` clean; 47 test targets plus the lib, **645 passed, 0 failed, 1 ignored** (the pre-existing manual gate), covering every target touched by this milestone and by the four milestones closed alongside it.

### Regression-guard evidence

Seven independent perturbations were applied in place, each reverting one
corrected claim, and each was shown to fail exactly the guard that owns it.
The baseline was restored after each perturbation and re-verified.

| # | Perturbation | Guard that failed |
|---|---|---|
| P1 | M008's duplicate `Status:` declaration restored | `test_m008_detailed_body_declares_exactly_one_status` |
| P2 | M009 body reverted to `Status: active` | `test_m009_detailed_body_is_closed_not_in_flight` |
| P3 | M010 body reverted to `Status: closing` | `test_m010_detailed_body_is_closed_and_rejected` |
| P4 | A Persistence row re-registered in the registry's dependency-ready table | `test_registry_registers_no_persistence_executable_work` |
| P5 | The roadmap's schema census reverted to "schema 54 remain authoritative" | `test_roadmap_states_the_current_schema_census` |
| P6 | M011 body reverted to `Status: ready` | `test_m011_detailed_body_is_closed_and_links_its_closure_record` |
| P7 | The investigation record's "schema-54 monolithic SQLite database" claim restored | `test_investigation_record_does_not_pin_a_superseded_schema_version` |

Every perturbation produced exactly one failure and twelve passes; the
restored baseline produced thirteen passes.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| Zero production Rust/Cargo/schema/migration/API/config change | The M011 change set is listed in §3; `git diff --stat` shows no file under `rust/`, `Cargo.toml`, `rust/assets/db/migrations/`, `scripts/`, `.github/`, or `artifacts/` |
| Zero physical qualification artifact change | No file under `artifacts/` is modified |
| Zero accepted closure-record rewrite | `plans/closure/persistence/001-status.md` through `010-status.md` are unmodified; M011's own record is the only new closure file |
| M010 remains rejected under the current history/outage contract | The M010 detailed body, the completion definition, the dependency graph, and the milestone status table all say closed/rejected. The investigation record's own `Status:` line is unchanged |
| No ADR-0002 is created or implied | `plans/adrs/` holds only `ADR-0001` and `ADR-0003`; M011 creates none |
| Production remains one authoritative SQLite database/connection/gate/worker on WAL/NORMAL | Asserted by `test_production_wal_normal_invariant_remains_present` in both the roadmap and the database deep dive; no runtime file changed |
| The persistence roadmap remains top-level `active` | Asserted by `test_roadmap_remains_top_level_active`; the foreground checkpoint/storage tail is still unresolved |
| No successor architecture or product policy is invented | M011 registers no new milestone, no ADR, and no implementation work. The roadmap's completion definition continues to state that future split work needs an explicit finite history-retention and prolonged analytics-outage admission contract first |
| `plans/` remains append-only | M011 adds one new file (`plans/closure/persistence/011-status.md`) and edits only current-authority status prose. No closed closure record and no pre-251 legacy plan was rewritten |
| The registry remains a links-only control surface | The registry edits are table rows and one closure-audit paragraph; no milestone requirement is duplicated into the registry |

## 6. Failure and recovery review

Not applicable in the runtime sense. M011 adds no runtime path, no lock, no
await, no task, and no storage access. The only failure behavior added is a
static tooling assertion that rejects future current-document lifecycle drift.

The guard's own failure mode is fail-closed: a drifted census produces a
non-zero pytest exit, which fails the tooling suite and therefore the CI job
that runs it.

## 7. Migration and compatibility review

No migration. No storage change. No configuration key added, removed, or
reclassified. No public HTTP or CLI contract change. No public Rust API change.

The migration corpus under `rust/assets/db/migrations/` was not touched. The
guard reads that directory's file names to compute the current highest
migration version, so it tracks the real chain rather than a literal.

The one present-tense schema claim that was corrected — "schema 54" in the
roadmap's cross-cutting storage requirement — was a stale census, not a
behavioral claim. Correcting it does not change what production does.

## 8. Security review

- **Auth** — unchanged; no runtime path was touched.
- **Secret handling** — the guard reads fixed repository documentation paths
  only. No physical artifact payload, runtime identity, credential, prompt,
  raw body, or cache key is copied into the guard; closure links and lifecycle
  status text are sufficient.
- **Privilege bounds** — unchanged.
- **DoS bounds** — the guard parses three Markdown files and one directory
  listing. It does not build the Rust workspace and does not run a subprocess
  beyond the test runner.
- **Audit behavior** — unchanged.

## 9. Documentation and operations

- `plans/subsystems/persistence-roadmap.md`: the detailed milestone bodies,
  the dependency graph, the completion definition, the cross-cutting storage
  requirement, and the milestone status table now agree with the accepted
  closures.
- `plans/registry.md`: Persistence M011 moves from "Dependency-ready
  implementation plans" to "Recently closed"; the Persistence roadmap row
  records `M011 closed`.
- `architecture/persistence-control-projection-investigation.md`: the
  present-tense schema claim is corrected; the historical census scope is
  unchanged.
- `architecture/deep-dive-database.md`: the schema-54 census reference is
  explicitly scoped to the schema current when the investigation ran.
- `tests/tooling/test_persistence_current_docs.py`: the targeted guard is
  extended from 3 tests to 13 and remains a single file — M011 did not create
  a second overlapping test file.
- No operator command, diagnostic, or recovery instruction changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | The roadmap's detailed M006 body still says it preserved "schema 54" | A reader comparing M006's body against the roadmap's cross-cutting requirement sees two different schema numbers | Deliberate: M006's body is a historical statement about a delivered milestone. Rewriting it would falsify the record. No action |
| low | The M010 investigation record's index and field census remains scoped to schema 54 and is not refreshed | The census describes the schema current when the investigation ran; the managed-user migrations 55–63 are not covered by it | Deliberate: the census is the investigation's evidence, not a current-schema claim. A refresh is only warranted if a future split investigation is reopened, which requires an explicit retention/outage product contract first |
| low | The full serial Rust workspace suite was not run for this milestone | None for M011 specifically: no Rust file changed | The enclosing milestone batch runs the Rust gates on the surrounding branch. See the batch record |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Persistence M011 was the
only registered Persistence successor, so no milestone is promoted and the
blocked-work table gains no Persistence entry.

The Persistence roadmap itself remains **top-level `active`**: the foreground
SQLite checkpoint/storage tail is still unresolved, and M010's split remains
rejected under the current history/outage contract. No technical successor is
registered or promoted. Future split work remains unready until a product
decision on finite history retention and request admission during prolonged
analytics outages is explicitly defined, followed by a separately registered
plan.

## 12. Registry updates

- `plans/registry.md`: the Persistence M011 row is removed from "Dependency-ready
  implementation plans" and added to "Recently closed" with its disposition;
  the Persistence row in "Active subsystem roadmaps" now records `M011 closed`.
- `plans/subsystems/persistence-roadmap.md`: the milestone 011 row in the
  milestone status table moves to `closed` with its closure record linked; the
  M008/M009/M010/M011 detailed bodies, the dependency graph, the completion
  definition, and the cross-cutting storage requirement are reconciled.
- `plans/implementation/persistence/011-post-m010-roadmap-documentation-reconciliation.md`:
  the `Status` line moves from `ready` to `implemented`.
