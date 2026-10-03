# Dashboard Milestone 012 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/012-lifecycle-closure-and-documentation-polish.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-012--lifecycle-closure-and-documentation-polish`

Repository baseline reviewed: `9eb46571a2e65aabe531197945f4656fe22caa79`

Merged terminal baseline recorded: `9eb46571a2e65aabe531197945f4656fe22caa79`
(PR #4, `codex/dashboard-m010-persistence-m007-provider-m002`)

Implementation and documentation commits:

- `cc2d3b96` — register M012 in the dashboard roadmap and registry.
- `46b2bc1` — activate M012 (plan, roadmap milestone row, registry active row).
- `bdd22aef` — correct three stale dashboard ownership paths in current
  documentation.

## 1. Executive finding

M012 is closed. The dashboard workstream had no technical defect: M001–M011
were already closed, the merged `main` head was green in hosted CI and the
dependency audit, and the production/tooling decompositions from M009/M010 were
present. The only real gap was planning-control drift plus three
current-authority documents still naming the retired
`rust/src/server/dashboard.rs` module. Both are reconciled here. The whole M012
range touches Markdown only — no Rust source, Cargo metadata, embedded
dashboard asset, provider template, persistence schema, config, API, DOM,
theme, auth behavior, oracle, or fixture changed.

## 2. Terminal technical evidence (work package A)

| Item | Evidence | Result |
|---|---|---|
| M008 closed the post-merge strict-Clippy/CI corrective | `plans/closure/dashboard/008-status.md`, hosted run `37049147155` | closed |
| M009 decomposed the production dashboard | `plans/closure/dashboard/009-status.md`, implementation `caa0b042`, hosted run `37053752173` | closed |
| M010 decomposed the qualification harness | `plans/closure/dashboard/010-status.md`, implementation `06c950f`, hosted run `37057371175` | closed |
| M011 closed the hosted oracle-history prerequisite | `plans/closure/dashboard/011-status.md`, implementation `30b8282a` | closed |
| Merged `main` contains both decompositions | `rust/src/server/dashboard/` (`mod.rs`, `routes.rs`, `api.rs`, `assets.rs`, `theme.rs`, `response.rs`, `format.rs`, `tests.rs`, `render/`); `scripts/qualification_dashboard_parity.py` is a 38-line facade over `scripts/dashboard_parity/` | present |
| Merge-head hosted CI | run [`37090882257`](https://github.com/eggstack/eggpool/actions/runs/37090882257), head SHA `9eb46571a2e65aabe531197945f4656fe22caa79`, event `push`, conclusion `success`, job `check`: `success` | pass |
| Merge-head dependency audit | run [`37090882237`](https://github.com/eggstack/eggpool/actions/runs/37090882237), head SHA `9eb46571a2e65aabe531197945f4656fe22caa79`, event `push`, conclusion `success` | pass |
| No ready/active dashboard successor outside M012 | `plans/subsystems/dashboard-roadmap.md` milestone table before this closure; `plans/registry.md` ready/active tables | confirmed |

## 3. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| M001-M012 recorded closed with valid closure links | `plans/subsystems/dashboard-roadmap.md` §12 rows 001-012, each with a `plans/closure/dashboard/NNN-status.md` link | pass | M001–M011 links unchanged; M012 links this record. |
| Dashboard roadmap top-level status is `closed` | `plans/subsystems/dashboard-roadmap.md` header `Status: closed` | pass | Was `active` with an M001–M011-closed body. |
| Registry Dashboard row is `closed`, names M012 as terminal cleanup, states no successor | `plans/registry.md` active-roadmap row | pass | Terminal reconciliation stated; no registered successor. |
| No Dashboard item in dependency-ready, active, or blocked sections | `plans/registry.md` sections 2-4 | pass | M012 removed from ready/active; no blocked row existed. |
| Current-facing docs use decomposed production/tooling ownership paths | See §4 audit | pass | Three corrections; the plan's four enumerated documents were already correct. |
| Historical M001-M011 plans/closures unchanged | M012 diff contains no `plans/closure/dashboard/00[1-9]-*`, `010-*`, or `011-*` path and no `plans/implementation/dashboard/00[1-9]-*`, `010-*`, or `011-*` path | pass | Append-only preserved. |
| No Rust/Cargo/asset/oracle behavior or fixture change | `git diff --name-only 9eb46571..HEAD` — seven Markdown paths only | pass | See §5. |
| M006 accepted differences and all parity evidence untouched | No `tests/fixtures/dashboard-python-oracle/` or `scripts/` path in the M012 diff | pass | Oracle commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`, accepted difference set, comparator normalization, and browser evidence unchanged. |
| Merge-head CI/dependency-audit success recorded as terminal integration evidence | §2 table with run IDs, head SHA, and conclusions | pass | — |
| Planning unblock audit finds no newly ready Dashboard successor | §10 unblock audit | pass | Nothing promoted; nothing unblocked. |
| Roadmap, registry, M012 plan, and M012 closure agree on state | Plan `Status: closed`, roadmap `Status: closed` + row `closed`, registry row `closed`, this record `Status: closed` | pass | No `active`/no-successor contradiction remains. |

## 4. Current-document ownership/path audit (work package B)

Search performed for retired single-file ownership claims
(`server/dashboard.rs`, `{...,dashboard}.rs` brace forms) and for any claim that
the qualification harness is a monolithic script.

| Document | Authority role | Finding |
|---|---|---|
| `architecture/deep-dive-dashboard.md` | current contributor deep dive | correct — full decomposed module map, facade role, pure `render/` modules, `server::dashboard::tests` path |
| `architecture/overview.md` | current review index | correct — `server/dashboard/` directory notation in both the request-lifecycle and subsystem-ownership maps |
| `.opencode/skills/development/SKILL.md` | current agent workflow | correct — names `scripts/qualification_dashboard_parity.py` as the stable facade and enumerates `scripts/dashboard_parity/` owners and `rust/src/server/dashboard/` production ownership |
| `tests/fixtures/dashboard-python-oracle/README.md` | current tooling/fixture authority | correct — describes the facade plus the eight `scripts/dashboard_parity/` owners |
| `architecture/README.md` | current architecture index | **stale** — listed `rust/src/server/{middleware,health,inference,dashboard}.rs`; corrected to `{middleware,health,inference}.rs` plus `rust/src/server/dashboard/` |
| `.opencode/skills/architecture/SKILL.md` | current agent workflow | **stale** — listed `rust/src/server/dashboard.rs` in the HTTP-adapter verification pointer; corrected to `rust/src/server/dashboard/` with its module list |
| `docs/thinking.md` | current operator guide | **stale path only** — `Source:` line named `rust/src/server/dashboard.rs`; corrected to `rust/src/server/dashboard/`. The surrounding thinking-observability prose is separately stale and is recorded as finding M-1 in §8. |
| `docs/rust-dashboard-qualification.md` | current operator guide | current for its scope; does not mention the frozen Python oracle runner — recorded as finding L-1 in §8 |

Remaining `server/dashboard.rs` occurrences exist only in immutable historical
records: `plans/closure/dashboard/00[2-9]-status.md`,
`plans/closure/dashboard/005-follow-up-006.md`, and the M002/M008/M009
implementation plans. They are not edited. `plans/subsystems/dashboard-roadmap.md`
§4 keeps its pre-restoration `rust/src/server/dashboard.rs` narrative under its
declared research baseline `17e298f64fa21589f558c43592a24fa91b952ff7` and already
carries the M001–M008 progression paragraph; M009's closure record is the
authoritative statement of the split. Rewriting that baseline narrative would
contradict the historical evidence M009 preserved, so it was left unchanged.

## 5. Changed-file list proving no product/oracle change (work package D)

Final M012 range, baseline `9eb46571` through the closure commit:

```text
$ git diff --name-only 9eb46571a2e65aabe531197945f4656fe22caa79 HEAD
.opencode/skills/architecture/SKILL.md
architecture/README.md
docs/thinking.md
plans/closure/dashboard/012-status.md
plans/implementation/dashboard/012-lifecycle-closure-and-documentation-polish.md
plans/registry.md
plans/subsystems/dashboard-roadmap.md
```

Every path is Markdown. There is no diff under `rust/`, `rust/assets/`,
`scripts/`, `tests/`, `packaging/`, `.github/`, or `uv.lock`. The three
documentation edits are single-token path corrections
(`dashboard.rs` → `dashboard/`); the planning edits are lifecycle state only.
`uv.lock` is deliberately excluded: running the tooling gate mutates it
working-tree-only (finding L-2), and that incidental change is not part of this
milestone.

## 6. Verification executed

### Commands run

```bash
rtk git diff --check
rtk git diff --name-only 9eb46571a2e65aabe531197945f4656fe22caa79 HEAD
rtk git diff --stat 9eb46571a2e65aabe531197945f4656fe22caa79 HEAD
rtk rg -n "server/dashboard\.rs" --glob '!plans/**' .
rtk uv run --frozen python scripts/validate_release_docs.py
rtk uv run --frozen python scripts/validate_runtime_package_boundary.py
rtk uv run --frozen ruff format --check scripts/ tests/tooling/
rtk uv run --frozen ruff check scripts/ tests/tooling/
rtk uv run --frozen pyright scripts/
rtk uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
rtk gh run view 37090882257 --json jobs,conclusion,headSha
rtk gh run view 37090882237 --json conclusion,headSha
```

### Results

- `git diff --check`: clean, no whitespace errors, in every M012 commit.
- `git diff --name-only`: exactly the seven Markdown paths in §5, including
  this closure record.
- Repository-wide search for `server/dashboard.rs` outside `plans/`: no
  matches after the three corrections.
- `validate_release_docs.py`: `status: pass`, 7 docs checked,
  `production_release: published 0.8.1`.
- `validate_runtime_package_boundary.py`: `status: pass`,
  `current_runtime: rust`, `historical_python_version: 0.7.4`.
- Ruff format: 58 files already formatted. Ruff check: all checks passed.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Tooling pytest: 151 passed, 4 skipped (locally; the skips are the
  environment-gated hardware qualification suites and are unrelated to M012).
  Running this suite has a working-tree side effect recorded as finding L-2; no
  lockfile change is committed by this closure.
- Hosted CI `37090882257` conclusion `success` on `9eb46571`; job `check`
  conclusion `success`.
- Hosted dependency audit `37090882237` conclusion `success` on `9eb46571`.

Rust default/no-default builds, the serial Rust suite, the strict oracle run,
and browser/shutdown-restart qualification were **not** re-run locally. They are
not required for a Markdown-only pass: no Rust, asset, oracle, or fixture file
changed, and the merge-head hosted run already executed the full matrix on the
exact baseline. Local results above are labelled local; hosted CI is the CI
truth for the product gate.

### Optional generic planning guard — declined

No guard was added. `tests/tooling/` contains no generic planning-validation
seam to extend; the nearest precedent, `tests/tooling/test_persistence_m004_evidence.py`,
is milestone-specific rather than generic. Expressing a
roadmap-versus-registry status-contradiction check would require a new
Markdown plan/roadmap/registry parser — the "bespoke parser / broad planning
framework" the plan forbids — and it would immediately fail on unrelated
subsystems whose rows still read `active` with no successor registered
(provider-profile metadata corrective), which is explicitly out of scope. The
plan's stop condition "a generic planning guard would require broad governance
changes or unrelated subsystem reconciliation" applies, so the guard was
declined by design rather than deferred.

## 7. Invariant review

- M001–M011 closure records are byte-unchanged; they remain the historical
  evidence of parity, oracle, browser, and hosted qualification.
- The frozen Python oracle commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`,
  fixture manifests/captures, comparator normalization, and browser evidence
  are untouched.
- No Rust source, Cargo metadata, embedded dashboard asset, provider template,
  persistence schema, config, API, DOM, theme, or auth behavior changed.
- M006's nine accepted source-backed differences remain accepted historical
  dispositions; M012 neither reopens nor reinterprets them.
- Current source truth is unchanged: production dashboard under
  `rust/src/server/dashboard/`; qualification facade at
  `scripts/qualification_dashboard_parity.py` with internals under
  `scripts/dashboard_parity/`; server route/auth assembly in
  `rust/src/server/mod.rs`.
- Historical sequencing remains traceable; M011's introduction as an M008
  hosted-CI corrective ahead of M009/M010 closure is preserved in both roadmap
  and registry.
- The registry stayed a compact control surface: one row plus one
  recently-closed row and one unblock-audit paragraph were added, with no
  milestone requirement duplicated.

## 8. Failure and recovery review

No runtime behavior changed, so there is no new product failure, cancellation,
restart, or contention semantics. The only sequencing requirement from the plan
— that the roadmap and registry not advertise contradictory dashboard states —
was honored by committing activation separately from closure, and by making the
lifecycle and closure-record changes within one commit.

## 9. Migration and compatibility review

No runtime, config, schema, storage, protocol, API, asset, oracle, or tooling
compatibility migration. Documentation-only corrections change no executable
behavior. No archival move was performed; dashboard plans and closure records
remain at their current paths per the plan's out-of-scope list.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium (out of scope) | `docs/thinking.md` §10–§13 documents thinking observability that has no Rust owner: `ThinkingMetricsCounter`, `GET /api/stats/thinking`, a `thinking_trace_json` request projection, `RequestCoordinator._recompute_thinking_budget_for_selected_provider()`, and `HealthManager.is_account_healthy()`. Verified absent from `rust/src/operations/metrics.rs`, `rust/src/server/`, and `rust/src/db/repositories.rs`; only migration `0039_thinking_observability.sql` and the `usage_rollups` `thinking_characters`/`reasoning_tokens` columns exist. This is Python-era prose drift predating the dashboard work, not a dashboard or runtime defect. | An operator following that guide would expect endpoints and counters that do not exist. | Register a new bounded thinking-documentation plan; M012 corrected only the dashboard path token inside it to keep §4's acceptance criterion true. |
| low (out of scope) | `docs/rust-dashboard-qualification.md` documents only the `operations_o008` qualification and browser review procedure; it does not mention the frozen Python oracle runner or `scripts/dashboard_parity/`. | Operator dashboard-qualification guidance is incomplete relative to the shipped tooling surface. | Fold into a bounded dashboard documentation follow-up. |
| low (out of scope) | `tests/tooling/test_release_docs.py:29` shells out to `uv run python scripts/validate_release_docs.py` without `--frozen`. Because the root `pyproject.toml` intentionally declares no `requires-python` (with `requires_python_semantics = "package-manager-compatibility-only"`, `[tool.pyright] pythonVersion = "3.11"`, ruff `target-version = "py311"`), re-resolution rewrites the committed `uv.lock` `requires-python` from `>=3.11` to the resolver default `>=3.13` on every tooling-gate run. Reproduced locally; the committed lockfile was restored and no lockfile change is included here. | Every run of the documented tooling gate leaves an unintended `uv.lock` diff that contradicts the pinned tool targets. | Register a narrow tooling fix (pass `--frozen`, or invoke the in-process `validate_release_docs` already imported at line 15). Out of M012 scope because it is a tooling behavior change. |
| None | No dashboard finding remains. Production, tooling, oracle, and asset behavior are unchanged from the merged green baseline. | — | — |

## 11. Roadmap and registry disposition

**Before:** dashboard roadmap `Status: active`; the M012 milestone row was
`ready` at registration (`cc2d3b96`) and `active` during execution
(`46b2bc1`), with no closure record; registry Dashboard row `active` while its
current milestone was `ready` at registration and its blocker text said no
successor existed; M012 present in the dependency-ready table and the active
roadmap row.

**After:** dashboard roadmap `Status: closed`; M012 row `closed` with a link to
this record; the roadmap terminal note states M001–M012 are closed, no successor
is registered, and future dashboard work requires a new bounded plan; registry
Dashboard row `closed` with M012 as terminal lifecycle reconciliation and no
registered successor; M012 removed from the dependency-ready and active tables;
M012 added to Recently closed; the header's "Most recently closed" pointer
advanced to M012; an unblock-audit paragraph appended.

The prior "active with no successor" contradiction is resolved. Roadmap,
registry, plan, and this record agree.

## 12. Unblock audit

Per planning governance, closing a milestone requires auditing blocked and ready
work in the same commit.

- **Persistence M007** (the only blocked row) stays blocked for an unchanged,
  unrelated reason: paired physical aarch64 Raspberry Pi-class MMC control and
  candidate evidence plus a 300-request convergence corpus is an operational
  requirement that no planning or documentation change can satisfy.
- **Routing-selection M002** stays evidence-gated: no affinity workload has
  been measured.
- **Provider Transport M002** is closed; its upstream blocker is historical.
- **Provider-profile-metadata planning/documentation reconciliation C001** is
  the sole remaining ready row, is Markdown-only, and is unaffected by M012.
- **Dashboard**: nothing was promoted and nothing was unblocked. No newly
  eligible Dashboard successor exists.
- The three findings in §10 are out of scope for this subsystem and are named here
  as the required future bounded plans; they are not blockers on any registered
  plan.

## 13. Registry updates

Applied in this same commit: dashboard roadmap top-level status `closed`; M012
milestone row `closed` with this closure link and a terminal note; registry
Dashboard row `closed`; M012 removed from the dependency-ready and active
implementation-plan tables; M012 added to Recently closed; "Most recently
closed" advanced to M012; an unblock-audit paragraph appended recording that
this closure promotes nothing and unblocks nothing.

## 14. Disposition

`closed`.
