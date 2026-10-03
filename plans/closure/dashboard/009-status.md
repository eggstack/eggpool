# Dashboard Milestone 009 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/009-production-module-decomposition.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-009--production-module-decomposition-and-ownership-cleanup`

Repository baseline reviewed: `30b8282a`

Implementation commits or pull requests:

- `caa0b042` — decompose dashboard production modules and update ownership documentation.

## 1. Executive finding

M009 is complete as a pure production module decomposition. The external dashboard route/auth assembly remains in `rust/src/server/mod.rs`; the dashboard facade preserves its consumed entry points. The strict frozen-oracle report has exactly the same comparison fields and accepted mismatch groups as the pre-refactor baseline. No HTTP, DOM, API, theme, or asset behavior changed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Production split with explicit ownership | `rust/src/server/dashboard/` module tree | pass | Routes, APIs, assets, response/format/theme helpers, and page-family renderers have separate owners. |
| Same server route/auth surface | `server::dashboard` facade exports used by `server/mod.rs`; strict parity auth/API comparisons | pass | `server/mod.rs` has no behavior change. |
| Renderers have no runtime/database authority | `dashboard/tests.rs` structural guard; pure synchronous renderer signatures | pass | Guard rejects repository, `AppState`, and `.await` references in renderer sources. |
| No user-visible dashboard change | pre/post strict report comparison | pass | 14 pages, 119 comparisons, same 9 accepted differences and group counts. |
| Static/theme bytes unchanged | strict manifest comparison | pass | 54 assets; exact baseline manifest and digest, including the already accepted CSS oracle difference. |
| Focused regression gates | focused Cargo and pytest commands below | pass | Dashboard 18, server transport 13, status 13, parity projection 26. |
| Default/no-default and tooling gates | local full suite and hosted CI run `37053752173` | pass | CI conclusion: success on implementation commit `caa0b042`. |
| Architecture/development docs current | architecture dashboard deep dive, architecture overview, development skill | pass | Module map and focused test location documented. |

## 3. Production implementation evidence

The former 5,816-line `rust/src/server/dashboard.rs` was replaced by:

```text
dashboard/
  mod.rs, routes.rs, api.rs, assets.rs, response.rs, format.rs, theme.rs, tests.rs
  render/{mod.rs, layout.rs, overview.rs, accounts.rs, models.rs,
          telemetry.rs, diagnostics.rs, runtime.rs, cache.rs}
```

`mod.rs` is the compatibility facade. Route and API handlers retain bounded data gathering. Rendering is split by page family and receives gathered input. Static/theme delivery remains separate from runtime and DB reads. Tests remain available under `server::dashboard::tests`; no broad public API or mutable shared state was introduced.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --lib server::dashboard -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short --maxfail=1
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk env EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle-m002 EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle-m002/.venv/bin/python EGGPOOL_DASHBOARD_CANDIDATE_SHA=693ac902 uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build --output /tmp/dashboard-m009-final.json --markdown /tmp/dashboard-m009-final.md
rtk git diff --check
```

### Results

- Local Rust full suite: 830 passed across 65 suites; serial execution.
- Focused dashboard: 18 passed; server transport: 13 passed; status command: 13 passed.
- Strict oracle: 14 page routes, 54 static/theme assets, 119 DOM/API comparisons; exact equality with `/tmp/dashboard-m009-baseline.json` for comparison counts, parity status, mismatch groups, route/fixture/theme/static inventories, screenshot and shutdown/restart fields. The nine accepted differences remain grouped as shared-shell/API 2, overview/account/model 4, telemetry/routing/trace 0, runtime/cache 3.
- Asset inventory: 54 entries and aggregate SHA-256 `cdc7cc3a9d5f0c36a9e2f11efe82b5e0864619b8dec9cb493b1cfe7dbd8f32c4`; the sole oracle-side CSS hash difference remains the pre-existing `.panel { min-width: 0; }` correction.
- Ruff format/check passed; Pyright reported 0 errors, 0 warnings, 0 informations; tooling pytest: 152 passed, 1 skipped; parity projection: 26 passed.
- Hosted CI run [`37053752173`](https://github.com/eggstack/eggpool/actions/runs/37053752173) passed on `caa0b0428fcdea17b2512a24bc94c02da3c88eae`, including all default/no-default Rust and tooling gates.

## 5. Invariant review

- Dashboard remains observational and read-only; no persistence/runtime owner moved.
- `server/mod.rs` retains route assembly and auth/public exemption ownership; the facade exports the same endpoints.
- Render modules are synchronous projection code; the structural guard prevents direct runtime/database authority.
- No new database connection, pool, task, provider probe, or runtime cache was added.
- Escaping, result bounds, redaction, route/API/DOM/theme behavior, and asset bytes are qualified unchanged by tests and strict oracle comparison.
- Default and no-default strict builds and full serial tests pass.

## 6. Failure and recovery review

No concurrency, snapshot timing, DB await, cancellation, shutdown, restart, or degraded-response policy changed. Async reads remain in route/API orchestration. Renderers do not borrow live process state. No new failure or recovery path was introduced.

## 7. Migration and compatibility review

No config, schema, storage, endpoint, DOM, asset, or data migration. The same facade path is consumed by the server and external HTTP/browser behavior is unchanged. No rollback limitation was introduced beyond reverting the module-layout commit.

## 8. Security review

Authentication ownership remains in `server/mod.rs` and route wiring. The route/API boundary retains bounded reads and existing redaction; renderers cannot access runtime state or repositories. Strict private/public auth projections and secret-free qualification passed. No security finding was identified.

## 9. Documentation and operations

Updated `architecture/deep-dive-dashboard.md`, `architecture/overview.md`, and `.opencode/skills/development/SKILL.md` with the directory module map and focused test path.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| None | No unresolved M009 findings. | None known. | None. |

## 11. Roadmap disposition

Milestone closed; the next hard-dependent plan may proceed. Dashboard M010 is unblocked because M009 was its sole hard dependency. Its oracle/comparator remains unchanged by this work, so M010 is promoted to `ready`.

## 12. Registry updates

In the same closure commit, mark M009 closed in this plan, the dashboard roadmap, and `plans/registry.md`; promote M010 from blocked to ready and update its implementation-plan status. No other blocked plan gains a dependency from this closure.
