# Dashboard Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/001-python-oracle-and-parity-substrate.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-001--python-oracle-freeze-and-strict-parity-substrate`

Repository baseline reviewed: `454b60c008cc6234d9c3db3b637402cfe8d2f1d8`

Implementation commits:

- `8b08bd5c8fdb7acc970e5788cc1cb118a69ece85` — activate M001.
- `3f3d5de87f98d0bdccb8556075fcdf764f779043` — add strict DOM/API tooling, sanitized capture corpus, gap report, and integrity tests.
- `286b70ab3aef6b3979a033f8ee5d8f72006ffa56` — record source hashes for all theme files.

## 1. Executive finding

M001 is closed as infrastructure only. The final Python dashboard at
`c23a70961f4b7858fdb0264cfb27b7ea26a8a334` is pinned in a machine-readable
manifest with sanitized empty/populated page and API projections, private-auth
results, a full ordered DOM comparator, JavaScript hook producers, and an
offline current-gap report. No Rust production renderer, runtime, route, or
asset changed. The current candidate still has expected parity gaps; this
closure makes those gaps testable and does not claim the dashboard is restored.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Fixed oracle and source inventory | `tests/fixtures/dashboard-python-oracle/manifest.json`; pinned commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334` | pass | Fourteen pages and eight dashboard JSON routes. |
| Static asset and theme identity | Manifest static Git blobs/SHA-256; 50 theme Git blobs/SHA-256; current gap run | pass | All 54 static/theme assets matched the historical Python copy. |
| Full DOM structural projection | `DomNode` projection in `scripts/qualification_dashboard_parity.py`; 14 empty and 14 populated captures | pass | Preserves element hierarchy/type, ordered text and children, all attributes, forms, controls, links, and bootstrap script data. |
| API projection and auth classification | Eight API captures in each state; `private-auth.json`; `compare_api_response` | pass | Object keys canonicalized, array order and values retained; all private page/API requests returned 401. Populated cache-observability HTTP 500 is captured as historical behavior. |
| JavaScript selector/API producer map | `manifest.json` `javascript_hooks` | pass | All observed selector producers map to pages; update-copy selectors are explicitly marked conditional on the update-available footer. Both JavaScript API fetches map to inventoried routes. |
| Deliberate mismatch detection | `tests/tooling/test_dashboard_parity_projection.py` | pass | Covers element type, class/id/data attributes, form/button controls, table/content order, route/link, script payload, API fields/order/status/auth, duplicate IDs, and unsafe links. |
| Durable sanitized fixtures and regeneration | `captures/capture-manifest.json`, fixture README, capture script | pass | 45 hashed capture artifacts, less than 2 MiB, fixed synthetic SQL hash, no committed credentials or live data. Two independent pinned-source captures produced identical projections. |
| Current candidate gap characterization | `current-gap-report.json` and `.md` | pass | Baseline Rust candidate: 50 mismatching matrix cells; payloads and fixture values excluded from the report. |
| No production parity change | Implementation diff and source review | pass | No Rust, CSS, JavaScript, schema, or runtime change. |

## 3. Production implementation evidence

There are no production behavior changes in M001. The tooling runs only when
explicitly invoked. Ordinary tooling tests read checked-in captures and do not
import or execute the retired Python application. The explicit capture command
requires a detached worktree at the fixed oracle commit and writes to a new
staging directory before publishing its output.

The strict page comparison rejects any canonical DOM tree difference. Only
insignificant HTML whitespace, class-token order, JSON object-key order,
standalone local timestamps, and the labeled process/host/memory/load/database
path/countdown values on Runtime are normalized. The exact list is recorded in
the manifest.

## 4. Verification executed

### Commands run

```text
rtk uv sync --frozen
rtk proxy .venv/bin/ruff format --check scripts/ tests/tooling/
rtk proxy .venv/bin/ruff check scripts/ tests/tooling/
rtk proxy .venv/bin/pyright scripts/
rtk proxy .venv/bin/pytest tests/tooling/ -q --tb=short --maxfail=1
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk git diff --check
```

Historical capture and gap qualification were local-only, against isolated
synthetic state:

```text
rtk git worktree add --detach /tmp/eggpool-dashboard-oracle c23a70961f4b7858fdb0264cfb27b7ea26a8a334
rtk uv sync --frozen                                      # from the historical worktree
rtk proxy cp scripts/qualification_dashboard_parity.py /tmp/eggpool-dashboard-oracle/scripts/qualification_dashboard_parity.py
rtk proxy /tmp/eggpool-dashboard-oracle/.venv/bin/python /tmp/eggpool-dashboard-oracle/scripts/qualification_dashboard_parity.py --capture-oracle /tmp/dashboard-oracle-captures-v7
rtk proxy /tmp/eggpool-dashboard-oracle/.venv/bin/python /tmp/eggpool-dashboard-oracle/scripts/qualification_dashboard_parity.py --capture-oracle /tmp/dashboard-oracle-captures-v8
rtk mkdir -p /tmp/eggpool-dashboard-oracle/rust/target/debug
rtk proxy ln -sf /Users/davidbowman/.codex/worktrees/9199/gorouter/rust/target/debug/eggpool /tmp/eggpool-dashboard-oracle/rust/target/debug/eggpool
EGGPOOL_DASHBOARD_CANDIDATE_SHA=454b60c008cc6234d9c3db3b637402cfe8d2f1d8 rtk proxy /tmp/eggpool-dashboard-oracle/.venv/bin/python /tmp/eggpool-dashboard-oracle/scripts/qualification_dashboard_parity.py --skip-build --output /tmp/dashboard-current-gap-final.json --markdown /tmp/dashboard-current-gap-final.md
```

### Results

- `uv sync --frozen`: pass; nine locked packages checked.
- Ruff format/check: pass; 47 files formatted and no lint findings.
- Pyright: pass; zero errors, warnings, or information diagnostics.
- Tooling suite: `145 passed, 1 skipped` in 41.37 seconds.
- Rust formatting and default Clippy: pass; no Clippy issues.
- No-default workspace check and Clippy: pass.
- Serial Rust workspace suite: `801 passed (65 suites)`.
- `git diff --check`: pass.
- Two historical captures had zero differing normalized projections. Current-gap qualification completed in 6.2 seconds and recorded 50 expected parity mismatches. Static/theme assets matched. This was local qualification, not CI evidence.
- Browser screenshots/interactions were not required by M001 and were not run; M002/M006 own those checks.

## 5. Invariant review

- Historical Python remains evidence/tooling only; no runtime/package fallback was added.
- Oracle commit is fixed; manifest regeneration records the same commit and capture refuses any other `HEAD`.
- Fixtures use the synthetic historical qualification SQL only. Tests reject credential sentinels and bound capture sizes.
- Static assets were not edited. Hashes are recorded for all static and theme source files.
- Full DOM comparison does not discard classes, data attributes, controls, order, panels, rows, or bootstrap content.

## 6. Failure and recovery review

M001 adds no production concurrency or persistence behavior. Capture starts only
the isolated historical server, terminates it in `finally`, removes temporary
runtime state, writes captures to a staging directory, enforces per-file and
aggregate bounds, and renames the completed output only after success. An
interrupted capture leaves no published partial corpus. The seeded
cache-observability HTTP 500 is preserved and reported rather than converted to
a success or hidden.

## 7. Migration and compatibility review

No database migration, config change, wire protocol change, or production
compatibility change was made. The source SQL fixture hash and source commit are
recorded. The historical application is not invoked by routine CI tests.

## 8. Security review

The fixture contains synthetic provider/account/model names and synthetic
operational events. Private dashboard pages and JSON routes returned 401 in the
capture run. Tests scan for the configured API-key sentinel and `sk-proj-`
credential marker. No API key, prompt, provider body, live database, machine
identity, or private endpoint was included in captures or the gap report. The
gap report stores only route/state, mismatch category/location, and status.

## 9. Documentation and operations

`tests/fixtures/dashboard-python-oracle/README.md` records the fixed oracle,
projection/normalization rules, capture provenance, and isolated regeneration
steps. The implementation plan, roadmap, and registry were updated for closure
and the dependency audit.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | Candidate dashboard parity remains incomplete: 50 matrix cells differ in the M001 report. | Expected Rust migration gaps remain user-visible. M001 supplies evidence only. | M002 restores the shared shell and timeseries APIs; M003-M005 handle their registered page groups. |
| Low | Matched browser evidence has not been collected. | Visual/interaction parity is not established by the oracle substrate. | M002 and M006 own browser interaction and matched-pair qualification. |

No M001 infrastructure or security defect remains open.

## 11. Roadmap disposition

Milestone closed as infrastructure. M002 has no remaining hard dependency and
is promoted to `ready`. M003/M004 remain blocked on M002; M005 remains blocked
on M002 and the M003/M004 interfaces; M006 remains blocked on M003-M005. No
unregistered successor was found.

## 12. Registry updates

In the closure commit, mark M001 `closed`, link this record, promote M002 to
`ready`, and keep M003-M006 blocked with their existing dependencies.
