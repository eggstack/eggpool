# Runtime Efficiency Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/runtime-efficiency/003-dashboard-ttft-percentile-query-qualification.md`

Source subsystem roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-003--dashboard-ttft-percentile-query-qualification-and-bounded-rewrite`

Repository baseline reviewed: `5f028625` (M002 closure; original query baseline `6489aa75aa276a12ff14c35a07b46f2eba01e2b8`)

Implementation and closure commits:

- `e0dbe67b` — add TTFT semantic and file-backed qualification harness
- `6adf9690` — correct qualification fixture to use in-window rows
- `6401ad84` — qualify and implement adaptive TTFT percentile query
- `2cad7eba` — begin formal closure

## 1. Executive finding

M003 is complete. Repeated full-row percentile sorts are retained for normal
histories and replaced with a grouped-frequency/window query once the eligible
TTFT population reaches 50,000 rows. The existing count query selects the
branch and remains a covering-index range scan. This measured threshold keeps
the 10k workload near baseline while lowering 100k summary and serialized
writer latency by about 37% on this host. Percentile ranks, source population,
period bounds, and empty behavior match the previous offset implementation.

The change is query-only. It adds no index, migration, database connection,
cache, task, checkpoint behavior, public API, or target-class performance
claim. Persistence M007 remains independently blocked on physical Pi/MMC
evidence.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Preserve TTFT population and percentile ranks | `dashboard_ttft_percentiles_preserve_population_and_rank_definitions`; large fixture compares exact p50/p99 to the previous count/offset implementation | pass | Empty, one, odd/even, duplicates, null, non-streamed, and out-of-window rows covered. |
| Characterize existing query plans | File-backed schema-54 10k/100k runs; SQLite 3.53.2; existing index inventory and `EXPLAIN QUERY PLAN` recorded below | pass | Old ordered-offset plan builds a temporary B-tree for each of the three percentile lookups. |
| Apply only an evidence-backed query change | Grouped-only and window candidates measured first; final adaptive path measured on both fixture sizes | pass | Grouping is selected at 50,000 eligible TTFT rows. |
| Reduce large-history summary and writer tenure | Final 100k p50 472.781 ms versus old baseline runs 754.329/755.840 ms; writer p50 471.905 ms | pass | Host-local descriptive result; no SBC claim. |
| Avoid small-history regression | Final 10k p50 54.719 ms versus old baseline 52.959/53.078 ms | pass | Reuses the required count; no distinct-count selector or extra scan. |
| Preserve serialized database ownership and API shape | Source review; database compatibility, dashboard route, coordinator publication/finalization, and complete default/no-default serial suites | pass | Existing DB worker/gate and summary response shape remain unchanged. |
| Tooling and release qualification | 27 dashboard parity tests; fmt, strict Clippy default/no-default, no-default check, locked release build | pass | No dependency or migration delta. |

## 3. Production implementation evidence

`UsageRollupRepository::dashboard_summary_basic` reuses its TTFT count to
select the existing three ordered-offset lookups below 50,000 eligible rows.
At or above the threshold, a SQL histogram groups `first_byte_ms` values and
uses cumulative frequency windows to select the two median positions and the
existing p99 position. Only three scalar values return to Rust; the full
population is never materialized in application memory. The exact same
streamed/non-null TTFT predicate and time bounds are used.

The count remains a covering range scan on
`idx_requests_streamed_started_ttft`. The grouped candidate plan scans the
same index, uses a temporary B-tree for `GROUP BY`, and uses a temporary
B-tree for window ordering. The previous implementation's ordered-offset
plan uses a temporary B-tree for each of the lower median, upper median, and
p99 statements. The small-history query plan remains on that existing
ordered-offset strategy.

The existing relevant indexes are:

- `idx_requests_streamed_started_ttft` on `(started_at, first_byte_ms)` for
  streamed rows with non-null TTFT;
- `idx_requests_streamed_provider_model_started_ttft` on
  `(provider_id, model_id, started_at, first_byte_ms)` for the same partial
  population.

## 4. Measurement evidence

The opt-in file-backed harness creates independent schema-54 databases with
10,000 and 100,000 deterministic request rows. 80% are streamed, about 68.6%
have TTFT, 90% are 12 hours inside the 24-hour query window, and 10% are older
than 30 days. SQLite was 3.53.2. The main database file sizes were about 9.05
MB and 87.05 MB; WAL sidecars are excluded. Each displayed distribution uses
20 complete summary calls and 10 paired serialized writer inserts.

Baseline `dashboard_summary_basic("24h")` results from two corrected runs:

| Rows | p50 ms | p95 ms | max ms | Writer p50 ms | Writer max ms |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 52.959 / 53.078 | 53.133 / 53.893 | 56.819 / 57.152 | 53.756 / 54.036 | 58.867 / 69.346 |
| 100,000 | 755.840 / 754.329 | 759.517 / 756.802 | 761.531 / 757.766 | 756.729 / 756.218 | 792.494 / 757.354 |

Final adaptive implementation:

| Rows | p50 ms | p95 ms | max ms | Writer p50 ms | Writer max ms | Decision |
|---:|---:|---:|---:|---:|---:|---|
| 10,000 | 54.719 | 54.870 | 54.961 | 55.605 | 115.087 | Existing offset path |
| 100,000 | 472.781 | 474.539 | 475.366 | 471.905 | 479.838 | Grouped-frequency path |

The final 100k summary p50 is about 37% below both baseline samples. Writer
latency tracks the serialized summary and fell by a similar amount. The final
10k p50 is within about 3% of the baseline. A grouped-only candidate regressed
the 10k fixture; an initial distinct-count selector added its own temporary
B-tree and did not help. Both were removed from the final decision path. An
initial fixture placed rows exactly 24 hours before seeding; those samples
were discarded after the fixture was corrected to 12 hours in-window.

These are host-local measurements. No Raspberry Pi, MMC, or production
latency result is claimed. Database read/write ordering remains on the
existing serialized worker; each concurrent writer fixture insert completed.

## 5. Verification executed

Commands run against the final query shape:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
CARGO_BUILD_JOBS=1 cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked --release
umask 077; EGGPOOL_RUNTIME_DIR=/home/sugarwookie/projects/eggpool/rust/target/runtime-qualification EGGPOOL_PID_FILE=/home/sugarwookie/projects/eggpool/rust/target/runtime-qualification/eggpool.pid CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
umask 077; EGGPOOL_RUNTIME_DIR=/home/sugarwookie/projects/eggpool/rust/target/runtime-qualification EGGPOOL_PID_FILE=/home/sugarwookie/projects/eggpool/rust/target/runtime-qualification/eggpool-no-default.pid CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv run --frozen pytest tests/tooling/test_dashboard_parity_projection.py tests/tooling/test_dashboard_parity_modules.py -q
CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --test dashboard_ttft_qualification qualify_dashboard_ttft_query_plans_and_serialized_write_delay -- --ignored --nocapture --test-threads=1
git diff --check
```

Both complete serial Rust suites passed with a private runtime/PID directory
and restrictive umask. The default and no-default dashboard qualification
targets passed; the large 10k/100k file-backed harness passed with exact
comparison to the legacy percentile oracle. Dashboard tooling/parity reported
27 passed. Strict fmt/Clippy, no-default check, and locked release build
passed. The final simplification removed the temporary distinct-count
selector; after that change the targeted large harness and strict static /
release gates were rerun. The complete suites had passed on the immediately
preceding adaptive implementation, which used the same grouped query and a
more expensive selector. The final count-only selector also passed the
focused no-default semantic target and the default 10k/100k parity/contention
qualification.

## 6. Compatibility, migration, and security review

- No schema or index migration, database topology, checkpoint, runtime task,
  cache, public type, endpoint, or JSON shape changed.
- Empty populations remain `(0.0, 0.0)`; even p50 remains `f64::midpoint` of
  the two center rows, and p99 remains `ceil(0.99*n)-1`.
- No TTFT vector, request identity, prompt, body, credential, or new
  diagnostic is persisted or logged.
- Read errors and dashboard degradation behavior are unchanged.
- Rollback is an ordinary code revert; no durable state transition exists.

## 7. Unresolved findings and unblock audit

| Severity | Finding | Disposition |
|---|---|---|
| none | No M003-scoped unresolved finding | — |

M003 has no successor or dependent milestone. Closing it closes the
Runtime Efficiency roadmap after M001/M002/M003. No independent plan is
unblocked by this closure. Provider profile metadata reconciliation C001
remains ready and independent. Persistence M007 remains operationally blocked
on paired physical Pi/MMC evidence and is unaffected. No future roadmap or
registry status was changed beyond the Runtime Efficiency closure.

## 8. Registry and roadmap disposition

`plans/registry.md` marks M003 closed, adds this record to Recently Closed,
closes the Runtime Efficiency roadmap, and retains unrelated ready/blocked
work unchanged. The roadmap milestone table and source plan both link this
closure record. The accepted outcome is the bounded query-only rewrite; the
normal single database gate and persistence M007 boundary remain intact.
