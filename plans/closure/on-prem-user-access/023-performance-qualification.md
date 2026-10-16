# On-Prem User Access M023 — Ordinary Linux Performance Qualification

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/023-short-window-fairness-controls.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m023--short-window-team-fairness-controls`

Previous implementation closure:

- `plans/closure/on-prem-user-access/023-status.md` (conditionally closed; retained as the implementation and local-verification record)

Repository baseline reviewed: `83c0779ae4776a6b9d4d82571c7255bcb71246d6`.

Runtime candidate: release binary built from `f08a760e2cd995ac9b3416b571d092e3315b6524`; SHA-256 `d7bec9bf508e9b9ccfd1c9ac53ae25b1ef37a5af266f2d4b0cf048c2816f7b73`. The later `83c0779a` commit changes qualification tooling only; the runtime source and candidate binary are unchanged.

Implementation and qualification commits:

- `84482e91b84fe7424144dcf916441479c5b492d1` — bounded short-window admission fairness.
- `f08a760e2cd995ac9b3416b571d092e3315b6524` — avoid legacy shared-key creation after administrator initialization.
- `83c0779ae4776a6b9d4d82571c7255bcb71246d6` — add measured M023 throttle mode to the ordinary-server qualification runner.

## 1. Executive finding

The remaining M023 operational condition is satisfied. The modified release binary was measured on the same non-virtualized Ubuntu 24.04.5 / ext4 / Samsung SSD 980 NVMe ordinary-server target used for M002. The no-rule matrix completed all 27 requested runs. Three additional single-concurrency controls repeat the 1-, 5-, and 49-principal cases. Three active-policy runs each sent 1,000 requests at concurrency 16 with five fresh principals and a 1-request/minute rule: each admitted exactly five requests and returned the distinct short-window 429 for the other 995.

The no-rule p50 and p95 distributions remain close to the M002 comparison in the ordinary 5-principal concurrency cells. The controlled 49-principal case matches M002's p95/p99 (61/81 ms). Single-concurrency p99 varied more between repetitions; the raw reports and host I/O pressure are retained. No universal team capacity or latency guarantee is inferred. The medium finding recorded in the prior M023 closure is resolved by the named post-change evidence; no unresolved M023 finding remains.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Measure the modified binary with the rate limiter disabled | `artifacts/qualification/on-prem-user-access/m023-ordinary-server-no-limit/matrix.json`, `summary.json`, and `comparison.json` | pass | 9 principal/concurrency cells × 3 repetitions; 1,000 requests per run; all 27 final reports pass and share the candidate SHA above. The fixture omits `[admission_fairness]`. |
| Compare the no-rule behavior to the pre-M023 M002 reference | `comparison.json`; M002 `plans/closure/on-prem-concurrency-performance/002-status.md` and its ordinary-server corpus | pass | Same host class, storage, provider profile, request count, and cell definitions. M002 candidate SHA is recorded in the comparison source summary. |
| Measure active throttling, admitted/rejected counts, and response latency | `artifacts/qualification/on-prem-user-access/m023-ordinary-server-active-throttle/summary.json` plus three raw reports | pass | Three passing runs each record HTTP 200: 5, HTTP 429: 995, with `short_window_rate_limited`; median 1,091.728 requests/s, p50 13 ms, p95 29 ms, p99 113 ms. |
| Bound the request path and keep denials before upstream dispatch | Existing `server_transport::short_window_fairness_rejects_before_dispatch_and_reports_retry_scope`; active-throttle reports | pass | Integration test verifies no body collection/upstream dispatch for a denied request. Measured runs account for exactly five initially admitted requests per five fresh principal buckets. |
| Retain honest host and pressure evidence | Raw Q008 reports and qualification manifests | pass with environment note | Host: Intel i9-9900K, 16 logical CPUs, 15.5 GiB RAM, Ubuntu 24.04.5, non-virtualized, ext4 on Samsung SSD 980 NVMe. Two initial attempts were stopped by harness gates (one shutdown observation; one full I/O PSI avg10 at 28.33%); their cells were retried after the host settled. The blocked active attempt is retained as `run-3-blocked-attempt.json`. |

No new capacity claim is made. The 49 named-principal result is metadata/concurrency characterization at the declared request counts, not a 49-simultaneous-inference promise.

## 3. Production implementation evidence

The production boundary remains `rust/src/request/admission_fairness.rs` and `rust/src/server/middleware.rs`. An absent policy returns before limiter locking or state allocation. Configured policy uses a process-local bounded principal table, a monotonic token bucket, pre-body denial, and a distinct 429 with `Retry-After`. The active measurement changes the live policy with `rehash` immediately before the measured burst so setup and warm-up traffic do not consume the fresh principal buckets.

The onboarding corrective in `rust/src/runtime.rs` checks the database identity state before generating a legacy shared server key. Existing `admin_initialized` and `managed` installations no longer receive a new key during onboarding; uninitialized and legacy installations retain their compatibility path.

M023 adds no database migration. Schema migration 0063 belongs to M021 and remains an additive nullable default-member-profile field with its checksum and runtime-manifest entry.

## 4. Verification executed

### Commands run

```bash
rtk proxy cargo build --manifest-path rust/Cargo.toml --locked --release
rtk proxy cargo test --manifest-path rust/Cargo.toml --lib runtime::tests::onboarding_does_not_create_a_legacy_key_after_admin_initialization -- --test-threads=1
rtk proxy cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk proxy cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk proxy cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk proxy cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk proxy uv run ruff format --check scripts/ tests/tooling/
rtk proxy uv run ruff check scripts/ tests/tooling/
rtk proxy uv run pyright scripts/
rtk proxy uv run pytest tests/tooling/test_qualification_sbc.py -q --tb=short --maxfail=1
rtk proxy python scripts/qualification_ordinary_server.py --binary rust/target/release/eggpool --output-dir artifacts/qualification/on-prem-user-access/m023-ordinary-server-no-limit
rtk proxy python scripts/qualification_sbc.py --binary rust/target/release/eggpool --target-class ordinary-server --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 1 --concurrency-workers 16 --concurrency-requests 1000 --benchmark-principals 5 --benchmark-budget-mode user-global --provider-latency-profile mixed-0-10-50ms --candidate-origin on-device-release-build --admission-fairness-rate-per-minute-per-principal 1 --output artifacts/qualification/on-prem-user-access/m023-ordinary-server-active-throttle/run-1.json
```

The active-throttle command was repeated for `run-2.json` and `run-3.json` with the same arguments. The no-limit controlled runs used the same Q008 arguments with concurrency 1 and principal counts 1, 5, and 49; three reports per cell are under `m023-ordinary-server-no-limit/controls/`.

### Results

- Locked release build passed. Candidate SHA-256: `d7bec9bf508e9b9ccfd1c9ac53ae25b1ef37a5af266f2d4b0cf048c2816f7b73`.
- Onboarding regression test: 1 passed.
- Default strict Clippy, no-default `check`, and no-default strict Clippy passed.
- Ruff format/check passed; Pyright reported 0 errors, warnings, or informations; focused tooling suite: 44 passed.
- The local full default workspace suite was stopped after six minutes when system I/O PSI exceeded 90% and the filesystem reached 99% used (13 GiB free). The pushed exact runtime/tooling head `83c0779a` has green hosted `check`, Linux installer, and macOS installer workflows. The hosted check is the full-suite gate for this candidate.
- Ordinary-server no-limit corpus: 9 cells, 3 passing runs per cell, 1,000 requests per run; every final run completed and settled durable request/reservation state.
- Active throttling: 3 passing runs; each completed all 1,000 client observations with five 200 responses and 995 correctly coded 429 responses. No expected denial was classified as a provider or budget failure.

Selected same-host medians (M002 → modified binary, limiter disabled):

| Principals / concurrency | p50 ms | p95 ms | p99 ms | requests/s |
|---|---:|---:|---:|---:|
| 1 / 1, controlled repeat | 15 → 13 | 82 → 96 | 148 → 164 | 36.495 → 35.987 |
| 5 / 1, controlled repeat | 15 → 15 | 61 → 62 | 89 → 123 | 42.179 → 38.753 |
| 49 / 1, controlled repeat | 15 → 14 | 61 → 61 | 81 → 81 | 42.322 → 43.498 |
| 5 / 2 | 14 → 15 | 61 → 62 | 79 → 82 | 93.976 → 92.816 |
| 5 / 4 | 14 → 15 | 59 → 60 | 75 → 82 | 193.683 → 190.485 |
| 5 / 8 | 19 → 22 | 69 → 73 | 76 → 85 | 335.268 → 295.215 |
| 5 / 16 | 35 → 38 | 88 → 91 | 106 → 119 | 341.347 → 326.388 |

The p50/p95 shifts are small in absolute terms on these loopback synthetic-provider runs. Single-concurrency p99 is variable: the comparison artifact preserves each repetition and its pre/post I/O PSI; one 49-principal matrix run was blocked at preflight and retried after pressure fell. These descriptive measurements do not establish an SLA.

## 5. Invariant review

- No-rule requests allocate no bucket state and do not acquire the limiter mutex.
- Configured buckets remain principal-scoped, bounded, and process-local. User tokens, request bodies, prompts, IP addresses, and provider results are not retained.
- The throttle decision remains before body collection, durable budget admission, provider routing, and upstream submission.
- A denied request consumes neither another principal's token nor a durable request reservation. Existing daily/monthly user and instance budgets remain authoritative.
- The performance work changes no SQLite ownership, WAL/NORMAL settings, writer count, routing policy, generation lease, or streaming handoff.

## 6. Failure and recovery review

The existing functional tests cover simultaneous final-slot admission, retry timing, principal-table capacity, and pre-dispatch denial. The measured active batch confirms fail-closed throttling remains prompt under 16 concurrent clients. Ephemeral state still resets on restart; durable budget state is unaffected. Harness-blocked attempts collected no load observations and are not counted as passing runs.

## 7. Migration and compatibility review

M023 has no schema change. Missing `[admission_fairness]` retains disabled behavior. Rates are enabled through the existing live-reload policy; the active measurement installed the rule with a live `rehash`. Migration 0063 remains M021-owned and is checked by the existing schema inventory, checksum, and compatibility tests.

## 8. Security review

The limiter key remains the authenticated database-owned user ID, not a token, header, model, IP, or body value. The denial response reveals only its bounded scope and retry time. No new secret-bearing field or diagnostic was introduced. The onboarding correction does not mint a shared key for an installation whose administrator is already initialized.

## 9. Documentation and operations

Operator configuration and restart/reset semantics remain documented in the M023 implementation closure and deployment/model-limit docs. Raw run reports, comparison summary, and active-throttle summary are retained under `artifacts/qualification/on-prem-user-access/`. The qualification tooling describes the synthetic workload and rejects runs when Linux/SSD-or-NVMe or the predeclared I/O-pressure gate is not satisfied.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | No unresolved M023 performance or correctness finding | The post-change no-rule and active-throttle behavior is measured on the required ordinary Linux/NVMe class | None. Do not infer a universal latency or concurrency SLA from this host-specific synthetic workload. |

## 11. Roadmap disposition

M023 is closed. This follow-up resolves the sole medium finding in the earlier conditional implementation closure. All registered M017–M023 milestones are complete; the user-access roadmap has no remaining successor. Historical conditional findings are preserved in their original records and do not authorize broader capacity claims. No future plan was made ready by this closure because no registered plan depends on M023.

## 12. Registry updates

The roadmap and registry now point M023 at this performance-qualification closure and mark M023 complete. The earlier `023-status.md` remains unchanged as the implementation closure that recorded the missing operational evidence. The dependency audit found no M023 successor or downstream dependency; no blocked row is promoted and no other plan status changes.
