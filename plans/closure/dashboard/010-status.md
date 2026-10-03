# Dashboard Milestone 010 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/010-parity-harness-decomposition.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-010--parity-qualification-harness-decomposition`

Repository baseline reviewed: `817282324feb0e838fb3177e62169e61bd73e7ff`

Implementation commits or pull requests:

- `13e9d41` — activate Dashboard M010.
- `06c950f` — decompose dashboard parity qualification tooling and update docs/tests.
- `0953540` — record local gates and begin closure.
- Hosted CI run [`37057371175`](https://github.com/eggstack/eggpool/actions/runs/37057371175) passed against implementation commit `06c950f`.

## 1. Executive finding

M010 is complete as a tooling-only decomposition. The existing command path
remains the stable facade, and the frozen oracle, comparator, normalization,
report, browser, and process-cleanup contracts remain unchanged. No production
Rust source, assets, Cargo dependency, or release behavior changed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Stable command and CLI | `scripts/qualification_dashboard_parity.py --help` before/after byte comparison | pass | Exact output and options match. |
| Explicit internal ownership | `scripts/dashboard_parity/` plus `tests/tooling/test_dashboard_parity_modules.py` | pass | Projection, oracle, process, fixtures, browser, report, runner, and CLI have direct owners; façade test imports remain compatible. |
| Strict projection and accepted-gap behavior | Focused projection suite and complete strict frozen-oracle report comparison | pass | Before/after JSON reports have identical keys and values except expected `duration_ms`; accepted mismatch groups and comparison inventories match. |
| Oracle authority unchanged | Frozen manifest and capture manifest SHA-256 before/after | pass | `manifest.json`: `ff9fffac8a3c3106487151e2607c85de2da23e4fa3ee38811e802e242b2879c1`; capture manifest: `c0e5a1686118e3e843a6e1b0f09bd10c303b619c379ebd00302c158351e51db8`. |
| Browser and interaction report behavior | `--screenshots` strict qualification | pass | Screenshot/interaction qualification completed with the existing report schema. |
| Operational-event barrier and shutdown/restart | `--shutdown-restart` qualification | pass | Startup recovery barrier passed; two concurrent summary responses remained available during SIGTERM, listener-close behavior was observed, and restart returned HTTP 200. |
| Tooling quality | Ruff, Pyright, tooling pytest | pass | 153 passed, 1 skipped; Pyright 0 errors/warnings/informationals. |
| Rust non-regression gates | Local format, default/no-default strict Clippy/check, full serial suite | pass | 830 Rust tests passed across 65 suites. |
| Hosted CI | Run `37057371175` | pass | Format, default Clippy, no-default check/Clippy, serial Rust workspace tests, uv sync, Ruff, Pyright, and tooling tests succeeded. |
| No runtime/tooling boundary leak | Diff and package-boundary review | pass | Changes are limited to tooling, tests, development/oracle docs, and planning records. |

## 3. Implementation evidence

The former single qualification script is now a thin stable facade backed by:

```text
scripts/dashboard_parity/
  _shared.py  projection.py  oracle.py  process.py  fixtures.py
  browser.py  report.py  runner.py  cli.py
```

The original projection, oracle, fixture, lifecycle, browser, and report
functions were moved without changing their bodies or qualification policy.
Internal tests continue to import through the facade, and a module-ownership
test pins representative responsibilities to their internal modules.

## 4. Verification executed

### Commands run

```bash
rtk proxy python3 scripts/qualification_dashboard_parity.py --help
rtk proxy env EGGPOOL_DASHBOARD_ORACLE_ROOT=/private/tmp/eggpool-dashboard-oracle EGGPOOL_DASHBOARD_ORACLE_PYTHON=/private/tmp/eggpool-dashboard-oracle/.venv/bin/python python3 scripts/qualification_dashboard_parity.py --skip-build --output /tmp/dashboard-parity-post.json --markdown /tmp/dashboard-parity-post.md
rtk proxy env EGGPOOL_DASHBOARD_ORACLE_ROOT=/private/tmp/eggpool-dashboard-oracle EGGPOOL_DASHBOARD_ORACLE_PYTHON=/private/tmp/eggpool-dashboard-oracle/.venv/bin/python python3 scripts/qualification_dashboard_parity.py --skip-build --screenshots --output /tmp/dashboard-parity-screenshots.json --markdown /tmp/dashboard-parity-screenshots.md
rtk proxy env EGGPOOL_DASHBOARD_ORACLE_ROOT=/private/tmp/eggpool-dashboard-oracle EGGPOOL_DASHBOARD_ORACLE_PYTHON=/private/tmp/eggpool-dashboard-oracle/.venv/bin/python python3 scripts/qualification_dashboard_parity.py --skip-build --shutdown-restart --output /tmp/dashboard-parity-restart.json --markdown /tmp/dashboard-parity-restart.md
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk git diff --check
```

### Results

- Focused parity projection: 26 passed.
- Full tooling suite: 153 passed, 1 skipped.
- Full Rust serial workspace suite: 830 passed across 65 suites.
- Local strict oracle before/after JSON report: same schema and all comparison,
  route, fixture, accepted-gap, asset, and theme fields; only measured
  `duration_ms` differs.
- Browser screenshot/interaction and shutdown/restart modes passed.
- Hosted run `37057371175` passed all CI gates on `06c950f`.

## 5. Invariant review

- The strict comparator still retains tag hierarchy, attributes, controls,
  links, ordered content, API semantics, normalization rules, and accepted-gap
  disposition; it has not been loosened or reclassified.
- The pinned oracle commit, manifests, captures, and fixture inputs are
  unchanged and their recorded hashes match the baseline.
- Browser and process cleanup remain in tooling-only modules and are exercised
  by screenshot/interactions and shutdown/restart qualification.
- No Python package/runtime fallback, Rust dependency, server task, process
  owner, or release capability was added.
- Evidence and docs contain no credentials, prompts, raw requests, or browser
  secrets.

## 6. Failure and recovery review

The operational startup-event barrier remains in `process.py` and passed the
shutdown/restart scenario. Candidate shutdown completed under the existing
bounded SIGTERM deadline with two concurrent summary responses; the restarted
candidate became ready and served the page. No cleanup or recovery deviation
was found.

## 7. Migration and compatibility review

No production migration or dependency change. The existing script invocation,
flags, environment variables, report schema, exit behavior, and tested
facade imports remain compatible. The `--help` output is byte-identical.

## 8. Security review

The change remains repository tooling outside the runtime dependency graph.
Existing bounded report and fixture rules remain in place. No sensitive
content was added to qualification output or documentation.

## 9. Documentation and operations

Updated the frozen-oracle README with module ownership and regeneration
guidance, and the development skill with the module map. The supported command
remains `uv run python scripts/qualification_dashboard_parity.py ...`.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved implementation or qualification findings. | — | — |

## 11. Roadmap disposition

Dashboard M010 is closed. M011 is already closed, and no registered dashboard
successor became newly dependency-ready in this closure pass. New qualification
maintenance requires a future bounded plan. Existing accepted dashboard
differences and historical closures remain unchanged.

## 12. Registry updates

`plans/registry.md` now records M010 closed and its closure evidence. The
dashboard roadmap points M010 to this closure record and notes that no
successor is currently ready.
