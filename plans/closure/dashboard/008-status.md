# Dashboard Milestone 008 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/008-post-merge-strict-ci-and-planning-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-008--post-merge-strict-ci-and-planning-reconciliation-corrective`

Repository baseline reviewed: `299a0b3657667af509742a184e658c14df22d406`

Implementation commits or pull requests:

- `3c40e34f` — collapse the two Clippy-rejected nested conditionals in `load_average_summary`.
- `30b8282a` — ensure hosted CI has the pinned oracle commit required by the unchanged tooling test (M011).

## 1. Executive finding

M008 is closed. The Linux dashboard load summary now uses a stable let-chain with the same read, parse, CPU-count, formatting, and fallback behavior. Local Rust and tooling gates pass, and hosted CI run `37049147155` completed every gate successfully after M011 corrected the runner's shallow history.

The initial implementation run `37047638579` passed all Rust gates but failed tooling pytest because `actions/checkout` had not fetched oracle commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`. M011 resolved that qualification environment defect without changing the oracle or test. The subsequent complete run is green.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Remove both stable Clippy findings without suppression | `rust/src/server/dashboard.rs::load_average_summary`; default strict Clippy | pass | Nested read/parse and parse/CPU-count conditionals are let-chained. |
| Preserve load summary success and fallback | Focused dashboard suite; full serial Rust suite | pass | Helper still reads `/proc/loadavg` once, parses the first field as `f64`, uses `available_parallelism`, and returns the same unavailable text on failure. |
| Default and no-default strict gates | Local default Clippy; no-default check and strict Clippy; hosted run `37049147155` | pass | All hosted Rust gates passed. |
| Focused and full Rust tests | Dashboard tests: 17 passed; workspace suite: 829 passed across 65 suites | pass | Serial execution used. |
| Tooling gates | uv sync; Ruff format/check; Pyright; pytest | pass | Pyright: zero errors; full tooling: 152 passed, 1 skipped. Focused projection: 26 passed. |
| Hosted CI completes every gate | GitHub Actions run `37049147155` | pass | Run concluded success; no step skipped. |
| Dashboard behavior and accepted parity are unchanged | Exact conditional-only Rust diff; M009 pre-refactor strict report | pass | M009 baseline reports the same nine accepted cells across 14 pages and 54 static/theme assets. |
| Planning registry is reconciled and blocked work audited | `plans/registry.md`; dashboard roadmap | pass | M003 deployment status is closed; M008 closes; M009 is promoted ready; M010 remains blocked on M009. |

## 3. Production implementation evidence

The Rust diff changes only the conditional structure in `load_average_summary`. It retains the Linux-only guard, reads `/proc/loadavg` once, takes the first whitespace-delimited field, parses it as `f64`, obtains CPU count from `std::thread::available_parallelism`, formats the same success string, and returns the same unavailable fallback on any failed observation. No lint allowance or behavior change was added.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
rtk uv sync --frozen
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short --maxfail=1
rtk env EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle-m002 EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle-m002/.venv/bin/python EGGPOOL_DASHBOARD_CANDIDATE_SHA=3c40e34f uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build --output /tmp/dashboard-m009-baseline.json --markdown /tmp/dashboard-m009-baseline.md
rtk git diff --check
```

Hosted run `37049147155` executed the complete `.github/workflows/ci.yml` gate sequence on implementation commit `30b8282a` and concluded success.

### Results

- Formatting, default strict Clippy, no-default check and strict Clippy: pass.
- Full Rust workspace suite: 829 passed across 65 suites; focused dashboard suite: 17 passed.
- Ruff format/check: pass; Pyright: zero errors; full tooling pytest: 152 passed, 1 skipped; focused dashboard projection: 26 passed.
- M009 strict baseline: 14 pages and 54 static/theme assets; nine mismatch cells, with group counts shared shell/API 2, overview/account/model 4, telemetry/routing/trace 0, runtime/cache 3. These are the already accepted M006 differences.
- Baseline hosted run `37040025250` reported two `clippy::collapsible_if` errors at the nested branches in `rust/src/server/dashboard.rs` around lines 3420 and 3421; all subsequent gates were skipped.
- Hosted run `37047638579` passed Rust gates but failed tooling pytest because the depth-one checkout lacked the pinned oracle commit. M011 added `fetch-depth: 0`; full hosted run `37049147155` passed without skipped gates.
- The browser screenshot/oracle capture matrix was not rerun for M008's formatting-only source edit. Full strict oracle qualification was run immediately before M009 and provides the production-refactor baseline.

## 5. Invariant review

- Linux load-average observation remains best effort; unavailable `/proc/loadavg`, malformed content, or CPU-count errors preserve the same fallback.
- Non-Linux behavior remains unchanged.
- Dashboard remains observational; no route, API, DOM, auth, theme, asset, persistence, or runtime ownership behavior changed.
- The accepted nine M006 parity differences remain unchanged; no oracle fixture or comparator was edited.
- Strict Clippy remains enabled without lint suppression; both default and no-default configurations pass.

## 6. Failure and recovery review

No runtime state, async boundary, cancellation, restart, persistence, or contention behavior changed. The dashboard field continues to degrade locally when host observation fails. CI now fetches full Git history so the frozen historical-blob test can execute; the test still fails closed if the object or blob is missing.

## 7. Migration and compatibility review

No API, DOM, config, schema, storage, protocol, or data migration. Dashboard load-summary output semantics are unchanged. CI checkout history is qualification-only.

## 8. Security review

The code reads only the existing public host load-average file and CPU parallelism metadata. It adds no logging, persistence, credential handling, or request-content exposure. CI's additional history fetch does not introduce secrets or modify privilege scope.

## 9. Documentation and operations

The dashboard roadmap and registry now show M008 closed and M009 ready. Registry reconciliation removes the stale Deployment/Packaging M003-ready statement; Deployment/Packaging M003 remains closed. M011 records why CI needs the pinned oracle history. No operator documentation changed because runtime behavior is unchanged.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M008 finding | None | None |

## 11. Roadmap disposition

Milestone closed; M009 is dependency-ready and may proceed. M010 remains hard-blocked on M009 closure. Provider Transport M002 remains dependency-ready; Persistence M007 remains evidence-gated on physical Pi/MMC qualification; Routing Selection M002 remains evidence-gated because no affinity workload measurement was available. No other blocker became eligible through this dashboard closure.

## 12. Registry updates

`plans/registry.md` marks M008 closed, promotes M009 to ready, leaves M010 blocked on M009, retains the M011 closure, and confirms Deployment/Packaging M003 is closed. `plans/subsystems/dashboard-roadmap.md` records the same status and links this closure record. Applied in the same commit.
