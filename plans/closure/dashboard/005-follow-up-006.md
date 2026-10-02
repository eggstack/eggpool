# Dashboard M005 — Follow-up Closure from M006

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/005-runtime-cache-observability-parity.md`

Original closure record:

- `plans/closure/dashboard/005-status.md` (conditionally closed; retained unchanged)

Qualifying milestone and evidence:

- `plans/implementation/dashboard/006-full-parity-qualification-and-closure.md`
- `plans/closure/dashboard/006-browser-manifest.json`

Repository baseline reviewed: `5bb3aba1df84077df3367bf315011d671fe46dbe`

Implementation commits:

- `b068315a`, `3510590e`, `31e1a87`, `3cf7671` — M005 runtime/cache restoration.
- `6bfa1781` — restore the full translated theme contract, correct mobile panel
  sizing, and add matched browser and process lifecycle qualification.
- `5bb3aba1` — include the model-detail theme in every browser evidence row.

## 1. Executive finding

M005's runtime/cache capability and five bounded stats routes are complete.
M006 reproduced and audited each M005 exception against the frozen Python
oracle and current Rust owners. The remaining differences are intentional
source-truth dispositions: a broken frozen endpoint, a historical undercount,
additional live Rust supervisor tasks, and platform-specific absence of a safe
load-average source. They do not require further M005 production work. This
additive record resolves the conditional disposition in the original closure;
that historical record remains unchanged.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Runtime/Cache pages use bounded authoritative current-owner data | `rust/src/server/dashboard.rs`; `rust/src/db/repositories.rs`; `rust/src/runtime_lifecycle/diagnostics.rs` | pass | Current task, process, repository, and metrics owners remain authoritative. |
| Five stats endpoints return bounded, read-only projections | strict M001 report at candidate `5bb3aba1`; `plans/closure/dashboard/006-browser-manifest.json` | pass with accepted oracle defect | Rust `/api/stats/cache-observability` returns 200; the frozen Python route returns 500 for the populated fixture. |
| Unknown request-shaping statuses are not undercounted | strict M001 report and M005 request-shaping projection | pass with accepted semantic difference | Rust retains five distinct classified raw rows where Python collapses them to one known row. |
| Runtime task/load values do not imitate obsolete or unsafe sources | matched Runtime captures and bounded layout audits | pass | Rust exposes registered `checkpoint` and `metrics_flush` tasks. macOS load average remains unavailable; Linux reads `/proc/loadavg`. |
| Browser and process lifecycle behavior remains bounded | 144 matched browser captures; eight interaction checks; shutdown/restart record | pass | No Rust root-page overflow, duplicate IDs, invalid canvases, browser errors, or failed same-origin loads. SIGTERM and restart both completed within 15 seconds. |

## 3. Final source dispositions

- **Cache observability status:** the pinned Python oracle returns HTTP 500 on
  the populated route. Keep the Rust HTTP 200 bounded response. The M006 matrix
  confirms the divergence, and the existing auth/query/bounds checks remain in
  force.
- **Request shaping:** Python merges distinct raw unknown status values into a
  single known category. Rust reports the raw classified row count. Retain the
  Rust count; do not copy the frozen undercount.
- **Supervisor tasks:** Rust's `checkpoint` and `metrics_flush` are registered
  live tasks absent from the old Python supervisor. Retain the complete Rust
  task inventory.
- **Host load:** this macOS target has no safe bounded in-process load snapshot.
  Keep the value unavailable rather than spawning a host command. Linux reads
  `/proc/loadavg`.

The strict report contains nine accepted source-truth cells across M003/M005
and the two documented stats API differences. The complete list and visual
evidence are recorded in M006. No Rust-owned Runtime/Cache correctness defect
remains.

## 4. Verification executed

Local repository results (not CI results):

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check — passed
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings — passed
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features — passed
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings — passed
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1 — 815 passed across 65 suites
rtk cargo build --manifest-path rust/Cargo.toml --locked --release — passed
rtk uv sync --frozen — passed
rtk uv run ruff format --check scripts/ tests/tooling/ — passed
rtk uv run ruff check scripts/ tests/tooling/ — passed
rtk uv run pyright scripts/ — 0 errors, 0 warnings
rtk env EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle-m002 EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle-m002/.venv/bin/python uv run pytest tests/tooling/ -q --tb=short --maxfail=1 — 152 passed, 1 skipped
```

The final strict/browser qualification at candidate `5bb3aba1` wrote
`/tmp/dashboard-m006-final-pushed.json` and
`/tmp/dashboard-m006-final-pushed-captures`. Its committed compact evidence is
`plans/closure/dashboard/006-browser-manifest.json`.

## 5. Security and failure review

Runtime/cache observations remain read-only, bounded, escaped, and secret-free.
The qualification captures only dimensions, hashes, static layout facts, and
browser dispositions. It stores no response bodies, prompts, credentials, or
cache keys. Browser and server processes are cleaned up after failure. The
shutdown/restart probe kept dashboard summary reads and an interactive browser
active across SIGTERM; the initial server exited successfully in 19 ms, then
restarted on the same port/database, served HTTP 200, and exited successfully
in 8 ms. Both processes used a 15-second deadline.

## 6. Unresolved findings

None for M005. The four listed source dispositions are accepted compatibility
facts and are not future work items.

## 7. Disposition and registry audit

M005 is closed. M006 remains active for the final cross-milestone closure. No
additional dashboard corrective plan is required, and no newly eligible
dashboard plan remains after M006. The registry and dashboard roadmap record
M005 as closed in the commit that adds this follow-up record.
