# Dashboard M005 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/dashboard/005-runtime-cache-observability-parity.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-005--runtime-cache-observability-parity`

Repository baseline reviewed: `17e298f64fa21589f558c43592a24fa91b952ff7`

Implementation commits or pull requests:

- `b068315a` — restore runtime/cache dashboard projections and five stats APIs.
- `3510590e` — advance runtime/cache projections and generate strict gap evidence.
- `31e1a87` — add runtime process facts, safe load observations, and focused tests.
- `3cf7671` — restore cache segmentation and routing-guardrail presentation details.

## 1. Executive finding

The Runtime and Cache capability is implemented against current Rust-owned
runtime, task, metrics, and repository snapshots. The five dashboard stats
routes return bounded read-only projections. The empty Cache advanced panel
now matches the frozen segmentation/guardrail structure. M005 is conditionally
closed because strict qualification still identifies documented differences
where the frozen Python oracle either errors, undercounts distinct unknown
rows, or represents host/task ownership unavailable in the current Rust
runtime. M006 may proceed and must carry these dispositions through the final
full-contract audit.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Runtime/Cache values use existing owners and remain bounded | `rust/src/server/dashboard.rs`, `rust/src/db/repositories.rs`, `rust/src/runtime_lifecycle/diagnostics.rs` | pass | Dashboard remains a projection; no collector, migration, or second database pool was added. |
| Restore five dashboard stats APIs | routes and projections in `rust/src/server/dashboard.rs`; shared bounded SQL projection in `rust/src/db/repositories.rs` | pass | `/api/stats/transcoding`, `/api/stats/cache-observability`, `/api/stats/canonical-request-segmentation`, `/api/stats/cache-stability`, `/api/stats/request-shaping`. |
| Cache data distinguishes counters, segmentation, and routing inputs | strict report candidate `3cf7671`; `render_cache_page` projection | partial | Empty advanced segmentation and routing-guardrail structure matches. Populated raw unknown-status count intentionally differs from the frozen grouped undercount. |
| Runtime process facts avoid fabricated zero/success state | Runtime cards and focused unit coverage in `rust/src/server/dashboard.rs` | partial | Process age/PPID/daemon/platform are projected. Host load is collected from Linux `/proc/loadavg`; macOS reports unavailable because no safe bounded in-process source exists. Rust exposes all registered supervisor tasks. |
| Strict Python-oracle Runtime/Cache parity | `tests/fixtures/dashboard-python-oracle/current-gap-report.json` | partial | Three Runtime/Cache DOM cells remain, plus two populated API cells; details and decisions are recorded below. |
| Privacy and failure boundaries | dashboard render/API code; repository projection reads aggregate columns only | pass | No credentials, prompts, request bodies, cache keys, or raw provider bodies are added to output. |
| Lifecycle/content-boundary qualification | existing runtime lifecycle and dashboard test suites; no lifecycle owner changed | partial | M005 adds no new lifecycle or persistence behavior. Matched browser and concurrent shutdown evidence is carried into M006. |

## 3. Production implementation evidence

The Rust dashboard uses existing dashboard repository aggregates and the
process-owned `RuntimeDiagnosticsSnapshot`. It restores the five historical
stats routes and projects the Runtime/Cache panels from bounded JSON/repository
facts. Runtime process age, parent PID, daemon hint, and host platform are
projected without unsafe APIs. Linux load average reads `/proc/loadavg`; other
platforms render unavailable. Cache rendering includes provider counter
coverage, request segmentation categories and totals, cache stability, and the
current scorer's allowed inputs. No schema migration or frontend asset change
was required.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk uv sync --frozen
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk git diff --check
rtk env EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle-m002 EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle-m002/.venv/bin/python EGGPOOL_DASHBOARD_CANDIDATE_SHA=3cf7671 uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build --output tests/fixtures/dashboard-python-oracle/current-gap-report.json --markdown tests/fixtures/dashboard-python-oracle/current-gap-report.md
```

### Results

- Rust formatting, default Clippy, no-default check, and no-default Clippy passed.
- The serial Rust workspace passed: 814 tests across 65 suites.
- `uv sync --frozen` passed; its incidental `uv.lock` Python-version rewrite was reverted.
- Ruff format/check and Pyright passed. Tooling tests passed: 149 passed, 1 skipped.
- Strict local qualification completed with nine mismatch cells: four M003
  overview/account/model cells, three Runtime/Cache DOM cells, and two
  populated stats API cells. The tracked report is `3cf7671`.
- Focused Runtime age/platform/load-summary tests passed.
- Browser matched-pair captures and the M006 shutdown/restart run were not
  performed in this M005 pass; M006 owns those full-contract gates.

## 5. Invariant review

- Runtime, task, generation, reload, and client ownership stays with their
  current runtime modules; the dashboard only reads snapshots.
- Cache and request-shaping output contains aggregate counts only and does not
  expose keys, prompts, bodies, or tool arguments.
- Unknown or unsupported host facts remain distinct from measured zero.
- Existing auth classification for dashboard compatibility routes is retained;
  `/api/status` and other authenticated operational APIs are unchanged.
- No new background collector, blocking global metrics lock, or SQLite pool was
  introduced.

## 6. Failure and recovery review

M005 changes no inference, cache, routing, reload, generation, or task lifecycle
behavior. Dashboard projections use the established bounded repository and
snapshot failure handling. A stale observation may occur across concurrent
reload, as permitted by the snapshot contract; no generation lease is acquired
or retained by rendering.

## 7. Migration and compatibility review

No database migration, configuration change, or frontend asset change was
required. The five restored routes are read-only dashboard compatibility
surfaces. Current non-dashboard status, integration, runtime, and update APIs
remain unchanged.

## 8. Security review

No new secret-bearing fields are rendered or persisted. The restored responses
remain bounded, read-only, and escaped. The code uses safe standard-library
process metadata and Linux procfs reads; no unsafe host API or command execution
was introduced.

## 9. Documentation and operations

`architecture/deep-dive-dashboard.md` documents Runtime/Cache authority,
unavailable host load behavior, and privacy boundaries. The implementation plan
records source-truth decisions and links the strict gap report. M006 is promoted
to ready in the same registry update as this conditional closure.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Frozen `/api/stats/cache-observability` returns 500 while Rust returns 200. | Historical oracle cannot provide a populated value for direct API parity. | Preserve the bounded Rust success; M006 records this as an oracle defect and confirms status/auth/bounds behavior. |
| medium | Frozen request-shaping projection collapses five raw unknown statuses into one known row; Rust preserves five classified raw rows. | Populated API and Cache DOM differ by count. | Keep raw-row correctness. M006 records this as an accepted source-truth difference and guards against undercounting. |
| low | Python fixture has two supervisor tasks; Rust also has live `checkpoint` and `metrics_flush` tasks. | Runtime task table differs from frozen fixture. | Keep the full Rust task inventory; M006 carries the difference into the matched review. |
| low | Python reports host load on macOS; Rust has no safe in-process source and renders unavailable there. | Runtime load text differs by platform. | Preserve unavailable semantics; M006 verifies Linux/macOS behavior and documents the platform condition. |
| low | Four strict Overview/Accounts/Models cells remain from M003. | Full dashboard parity is not yet qualified. | M006 owns the final report and matched browser disposition. |
| low | Matched browser and concurrent shutdown/restart evidence are outstanding. | Visual and lifecycle interaction evidence is incomplete. | M006 performs matched desktop/mobile captures and bounded shutdown/restart qualification. |

## 11. Roadmap disposition

**Conditionally closed.** Current Runtime/Cache ownership and values are
implemented; the named residuals have explicit source-truth decisions and are
safe to carry into the full dashboard qualification. No unresolved high or
critical finding remains. M006 is unblocked to complete matched browser,
shutdown/restart, and final cross-milestone disposition evidence.

## 12. Registry updates

In the same commit as this record, update M005 to conditionally closed, promote
M006 from blocked to ready, and record the explicit unblock audit in
`plans/registry.md` and `plans/subsystems/dashboard-roadmap.md`.
