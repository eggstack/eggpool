# Dashboard Milestone 013 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/013-observation-projection-derivation-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-013--observation-projection-derivation-reconciliation`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M013 is complete and **closed**. Each of the five duplicated derivations is
now computed once and consumed by both the dashboard and the CLI:

- the transcoding summary has one owner, and the CLI command and the HTTP
  endpoint consume it;
- the cache read ratio has one definition for the page and the JSON;
- the two time-to-first-token endpoints read the same storage layer and
  average the same population;
- the routing-policy facts are derived from the routing module's own
  reported score surface and guarded against drift;
- the month-label lookup is total for every input.

No published field name or shape changed, and no configuration key, table,
column, index, migration, or dependency was added. Published field **values**
change only where the two prior derivations disagreed; §3 records each one
with the reason.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| The CLI and the HTTP endpoint report identical transcoding counts | `rust/tests/stats_transcoding.rs::cli_and_http_report_identical_transcoding_counts` | pass | The pre-change code reported different counts |
| The HTTP projection reports the real per-direction breakdown | `rust/tests/stats_transcoding.rs::the_per_direction_breakdown_reports_the_transcoded_direction` | pass | The pre-change code returned a hardcoded `{}` |
| The transcoding derivation reads the request protocols | `rust/tests/stats_transcoding.rs::the_transcoding_derivation_reads_the_request_protocols` | pass | The persisted `transcoded` column is a materialized copy and stays redundant, not authoritative |
| The HTML page and the JSON projection report the same cache read ratio | `rust/src/server/dashboard/tests.rs::overview_html_and_summary_json_report_the_same_cache_read_ratio` | pass | The in-module placement is required: `render_overview` and `summary_json` are `pub(in crate::server::dashboard)` and are unreachable from an integration test |
| The two timeseries endpoints agree on the first-byte average | `rust/tests/dashboard_ttft_qualification.rs::the_timeseries_endpoints_agree_on_the_first_byte_average` and `rust/src/server/dashboard/tests.rs::the_grouped_timeseries_reports_the_ungrouped_first_byte_average` | pass | Covered for the rollup-absent and the rollup-present case; the grouped projection is weighted by the first-byte observation count, not the request count |
| The reported routing-policy facts are derived and guarded | `rust/src/server/dashboard/tests.rs::reported_routing_policy_facts_are_derived_and_guarded` | pass | Derived from the routing module's own reported score surface; **no new configuration key was added**, so the source plan's stop condition was not hit |
| An out-of-range month value renders without panicking | `rust/src/server/dashboard/tests.rs::out_of_range_month_values_render_without_panicking` | pass | The lookup is total for every `i64`; the previous `debug_assert!` panicked in a debug build |
| No published field name or shape changes | `git diff --name-only` touches no contract definition; the internal `ttft_count` repository row field is never published | pass | Two published field **values** change where the prior derivations disagreed (§3) |
| No new configuration key, table, column, index, migration, or dependency | `git diff --name-only` shows none; `rust/Cargo.toml` and `rust/Cargo.lock` are untouched | pass | — |
| `server/*` remains a thin projection layer | The render modules call the repository owner; no state machine was added | pass | — |
| The rollup schedule and the persistence layout are unchanged | `rust/src/db/repositories.rs` changed only the read path; no rollup writer was touched | pass | — |
| Strict Clippy with `-D warnings` | `cargo clippy --workspace --all-targets -- -D warnings` | pass | Zero findings |
| The `--no-default-features` build compiles | `cargo check --workspace --all-targets --no-default-features` | pass | Verified by the implementation agent during this pass |
| Default and no-default workspace suites pass | The affected targets green (§4) | partial | The full serial workspace suite was **not** run in this pass, by instruction to keep verification targeted. Outstanding (§10) |

## 3. Production implementation evidence

Before-and-after table for every reconciled fact. This is the closure
evidence for the milestone's reconciliation requirement.

| Fact | Prior derivation A | Prior derivation B | Unified derivation | Reason |
|---|---|---|---|---|
| Transcoding total | CLI: `COUNT(*)` over all period requests, pending included | HTTP: `COUNT(*)` over finalized rows | Finalized requests of the period | A pending request has no upstream protocol, so it is neither native nor transcoded, and the dashboard already reports finalized requests everywhere else. **The CLI total and native count change.** |
| Transcoded count | CLI: protocol comparison (`upstream_protocol IS NOT NULL AND protocol != upstream_protocol`) | HTTP: the persisted `transcoded` column | The protocol comparison | Migration 0034 documents that comparison as the way to identify a transcoded request; column 0040 is a materialized copy, so the copy stays redundant, not authoritative |
| Native count | CLI: `SUM(protocol = COALESCE(upstream_protocol, protocol))` | HTTP: `total − SUM(transcoded)` | `total − transcoded` from the same owner | One definition, internally consistent with the breakdown |
| Per-direction breakdown | CLI: a real `client→upstream` map | HTTP: a hardcoded `{}` | The real breakdown from the shared owner, using the same predicate as the counts | The source plan requires the HTTP projection to report the real breakdown; removing a published field is not allowed |
| Cache read ratio | HTML: `cache_read / (cache_read + input)` | JSON: `cache_read / (input + cache_read + cache_write)` | The three-term denominator | The dashboard counts "input tokens" **without** the cache counters, so "total input" is `input + cache_read + cache_write`; the published JSON field and the per-account SQL already used it, and the frozen Python oracle rendered the three-term ratio on this card. **The HTML value changes; the JSON value does not.** |
| TTFT storage layer | Ungrouped: the raw `requests` table | Grouped: `usage_rollups` when present, else raw | Both read the raw `requests` table | The rollup table is lossy and flushed on its own schedule, so preferring it made the grouped numbers change when a rollup appeared; the frozen oracle read raw. The rollup schedule and layout are unchanged. **The grouped endpoint's published `source` value changes from `rollup` to `raw` for installations that have rollups.** |
| TTFT population and weight | Grouped projection: weighted by `request_count` | Ungrouped: `AVG(first_byte_ms)` over streamed rows | Weighted by the first-byte observation count | The two endpoints must average the same population |
| Routing mode | HTML literal `"reporting_only"` | JSON literal `"reporting_only"` | Derived: `"reporting_only"` while no routing capability is consumed, otherwise `"routing_aware"` (unreachable today) | An implementation fact derived from the routing module's own reported score surface rather than a literal. **`mode.routing` gains a second possible value, reachable only if a routing capability appears in that surface.** |
| `routing_uses_cache_metrics`, `routing_uses_stable_prefix_hash` | literals `false` | — | Derived from the same surface | Same |
| "Compression metrics" and "Compression policy" rows | literals `"no"` | — | Derived from the same surface | Same |
| Month labels | `months[(month-1) as usize]`, out of bounds possible | `format::month_name`, which panicked in a debug build | One total lookup for every label | No input may index a month array out of bounds, and no lookup may panic |

Landed ownership, all in the working tree:

- `rust/src/db/repositories.rs`: `TranscodingSummary`, `query_transcoding_summary`,
  and `DashboardRepository::transcoding_summary` are the single owner;
  `observability_stats` consumes them and reports a real `per_direction`;
  the grouped timeseries reads raw rows with a first-byte observation count;
  `RoutingCapabilitySurface`, `routing_policy_facts`, and
  `routing_policy_guard` derive the routing-policy facts.
- `rust/src/operations/operator.rs`: the `stats transcoding` command consumes
  the shared owner instead of carrying its own SQL.
- `rust/src/server/dashboard/api.rs`: the grouped-timeseries projection
  weights the first-byte average by the first-byte observation count.
- `rust/src/server/dashboard/render/overview.rs`: one `cache_read_ratio`
  definition serves the page and `summary_json`.
- `rust/src/server/dashboard/render/cache.rs`: the routing-policy rows, cards,
  and prose come from the derived surface.
- `rust/src/server/dashboard/render/telemetry.rs` and `format.rs`: the month
  label resolves through one total lookup, and `month_name` no longer asserts.
- `rust/src/server/dashboard/tests.rs`: four new in-module guards.
- `rust/tests/stats_transcoding.rs` (new) and
  `rust/tests/dashboard_ttft_qualification.rs`: the cross-surface guards.
- `architecture/deep-dive-dashboard.md`,
  `architecture/deep-dive-metrics.md`, and `docs/api-reference.md`: the
  definitions and the endpoint contract notes.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --test stats_transcoding -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test dashboard_ttft_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
uv run python scripts/validate_release_docs.py
cargo test --manifest-path rust/Cargo.toml \
  --test routing_fail_closed --test quota --test routing_domain \
  --test routing_domain_d008 --test routing_claims \
  --test routing_selection_efficiency --test health \
  --test durable_health_effects --test terminal_detail_redaction \
  --test coordinator_failure --test server_auth \
  --test coordinator_finalization --test coordinator_c007 \
  --test coordinator_c008 --test coordinator_c009 --test coordinator_c010 \
  --test coordinator_c011 --test coordinator_publication \
  --test database_compatibility --test on_prem_budgets \
  --test on_prem_global_budgets --test status_projection \
  --test status_command --test dashboard_filters \
  --test dashboard_ttft_qualification --test stats_transcoding \
  -- --test-threads=1
```

### Results

- `cargo fmt --all -- --check` — clean, zero diffs.
- `cargo clippy --workspace --all-targets -- -D warnings` — **pass**.
- `cargo check --workspace --all-targets --no-default-features` — **pass**.
- `stats_transcoding` — 3 passed, 0 failed.
- `dashboard_ttft_qualification` — 2 passed, 0 failed, 1 pre-existing ignored
  manual gate.
- `dashboard_filters` — 2 passed, 0 failed.
- `server::dashboard::tests` — 26 passed, 0 failed.
- `operations_o007` — 7 passed, 0 failed.
- `validate_release_docs.py` — `status: pass`.
- The 25-target sweep — 25 targets, 0 failures, 1 pre-existing ignored test.

### Regression-guard evidence

Each pre-change behavior was applied in turn, the corresponding test was
run, and the change was restored. Every scenario failed against the
pre-change behavior:

| Pre-change scenario | Test | Result |
|---|---|---|
| Empty `per_direction` | `the_per_direction_breakdown_reports_the_transcoded_direction` | FAILED — "the per-direction breakdown is empty for a transcoded request" |
| The CLI keeps its own SQL | `cli_and_http_report_identical_transcoding_counts` | FAILED — "the CLI and the HTTP endpoint report different transcoding counts" |
| The HTML two-term denominator | `overview_html_and_summary_json_report_the_same_cache_read_ratio` | FAILED |
| TTFT weighted by the request count | `the_grouped_timeseries_reports_the_ungrouped_first_byte_average` | FAILED |
| The grouped endpoint prefers the rollups | `the_timeseries_endpoints_agree_on_the_first_byte_average` | FAILED |
| The routing capability is enabled but the projection is unchanged | `reported_routing_policy_facts_are_derived_and_guarded` | FAILED |
| The `debug_assert!` month bound is restored | `out_of_range_month_values_render_without_panicking` | FAILED |

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| Published dashboard JSON field names and shapes do not change | No contract definition was edited; the only published changes are field **values** where the two prior derivations disagreed (§3) |
| No published field's meaning is redefined | Each reconciled fact keeps the meaning of the derivation the dashboard and the per-account SQL already used |
| `server/*` stays a thin projection layer | The render modules call the repository owner; no state machine was added |
| No performance work on the query path | No second database round trip was added and no `await` was introduced into a database call; the grouped timeseries now reads the same raw rows the ungrouped endpoint already read |
| The rollup schedule and the persistence layout are unchanged | No rollup writer and no schema was touched |
| A degraded or empty data source keeps producing the degraded response | The render paths are unchanged apart from the reconciled facts |
| No secret, prompt, raw body, or cache key enters a dashboard projection | The reconciled facts are counts, ratios, and labels |
| Strict Clippy with `-D warnings` | Clean |

## 6. Failure and recovery review

- **Duplicate delivery** — the repository owner is the only writer of the
  derived facts; a repeated read converges.
- **Cancellation races** — not applicable; these are read paths.
- **Restart** — not applicable; no new persisted state.
- **Partial persistence failure** — not applicable.
- **Malformed input** — the month-label lookup is now total; an
  out-of-range month cannot panic and cannot index out of bounds.
- **Degraded data source** — an empty period still renders the degraded
  response; the transcoding summary degrades to zeroes and an empty
  breakdown.

## 7. Migration and compatibility review

No schema migration, no config change, no protocol change. Two published
field values change for installations that have usage rollups or a
transcoded request: the grouped timeseries endpoint's `source` becomes
`raw`, and the CLI's transcoding total and native count become the
finalized-request counts. Both are documented in the dashboard and metrics
deep dives and in `docs/api-reference.md`.

## 8. Security review

- **Secret handling** — no reconciled fact carries a credential, a prompt,
  a raw body, or a cache key.
- **Privilege bounds** — unchanged; no route, no query parameter, and no
  surface was added.
- **DoS bounds** — the repository reads keep their existing limits. The
  grouped timeseries now reads the raw `requests` table, which is the table
  the ungrouped endpoint already read, so no new unbounded read was
  introduced.
- **Audit behavior** — unchanged.

## 9. Documentation and operations

- `architecture/deep-dive-dashboard.md`: a new "Single-source derivations"
  section recording each definition and the reason it was chosen.
- `architecture/deep-dive-metrics.md`: a new "Time-to-first-token population
  and storage layer" section.
- `docs/api-reference.md`: the transcoding and timeseries endpoint contract
  notes, including the `source` value change.
- The cache page's rendered prose changed from "these flags are hardcoded" to
  a statement that the flags are derived. This is a visible text change on
  that page and is recorded in §10.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial workspace suite was not run in this pass, by instruction to keep verification targeted at this stage | The default serial workspace suite required by the source plan is unverified. The `--no-default-features` **check** and clippy did pass | Run `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` before the change is pushed |
| medium | The cache page still renders "Request changes: no changes / disabled by config" and "Safety guardrail: Clean / 0 fallbacks · 0 policy warnings" as literals | There is no request-compression configuration key and no compressor in the runtime, so "disabled by config" names a knob that does not exist | Outside the plan's five-fact list, so it was left alone. A follow-up milestone should derive these from the same capability surface and from the `compression_summary` counters |
| medium | The dashboard parity harness was not run | It needs an external pinned Python oracle worktree (`tests/fixtures/dashboard-python-oracle/README.md`), and the recorded M012 baseline already shows 200/401 mismatches on every route, so it is not a passing gate today. The cache-page copy change adds one more DOM-text difference for the oracle comparison | Re-run the parity qualification and re-record the capture when the oracle worktree is available |
| low | The grouped timeseries keeps its pre-existing bounds (the repository's `LIMIT 5000` grouped rows and the projection's series limit) | Rows dropped by that bound could still make the grouped bucket average differ from the ungrouped one on a very large window | Pre-existing bound, not introduced here. A separate milestone should reconcile the bound with the population |
| low | The routing-capability classification matches score-component name fragments (`cache`, `compression`, `compression_policy`, `stable_prefix_hash`/`prefix_hash`) | A future routing capability whose score component carries none of those fragments would not be detected | The guard test pins the current surface, so a new score component shows up in the diff |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. No dashboard milestone
depends on M013, and no successor is registered in this subsystem.

## 12. Registry updates

- `plans/registry.md`: the dashboard milestone 013 row moves from `ready` to
  `closed`, with the closure record linked.
- `plans/subsystems/dashboard-roadmap.md`: the milestone 013 row in the
  milestone status table moves to `closed`.
- `plans/implementation/dashboard/013-observation-projection-derivation-reconciliation.md`:
  the `Status` line moves from `ready` to `implemented`.
