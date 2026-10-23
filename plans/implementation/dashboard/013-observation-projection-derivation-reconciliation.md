# Dashboard Milestone 013 — Observation projection derivation reconciliation

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for observed facts and derived projections
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. Every change keeps the published dashboard contract stable
  and makes its derivation single-sourced.
- Stop for architecture review if the reconciliation would change a published
  dashboard field's value or shape, because the embedded dashboard assets and
  the parity tooling are compatibility consumers.

Primary class: invariant

## 1. Objective

Make every duplicated observation derive its numbers from one source, so the
same named fact cannot mean two different things depending on whether it is
read from the CLI, from the dashboard JSON, or from the dashboard HTML.

## 2. Why this milestone is ready

No dependencies. Each divergence is between two existing derivations that
already exist in the repository, and each can be reconciled with a
single-source change plus a regression test.

## 3. Current implementation evidence

**Transcoding counts diverge between the CLI and the HTTP surface.**
`rust/src/operations/operator.rs` derives the transcoding summary with two
SQL aggregates: a native count over rows where the request protocol equals
the coalesced upstream protocol, and a transcoded count over rows where the
upstream protocol is present and differs. The same function then derives a
per-direction breakdown from a third query. `rust/src/db/repositories.rs`
derives the dashboard's transcoding object differently: the native count is
the total minus the transcoded count, and the per-direction breakdown is a
hardcoded empty object. The same facts are therefore reported differently by
`eggpool stats transcoding` and by `/api/stats/transcoding`, and the HTTP
variant never reports a direction breakdown at all.

**Cache read ratio has two denominators.** The dashboard overview page in
`rust/src/server/dashboard/render/overview.rs` derives its displayed cache
hit rate as cache read tokens over cache read tokens plus input tokens. The
JSON projection in the same file derives `cache_read_ratio` as cache read
tokens over input tokens plus cache read tokens plus cache write tokens. The
page and the JSON present the same concept with different denominators, so
the same installation reports two different cache ratios.

**Time to first token is measured over two different populations.** The
ungrouped timeseries in `rust/src/db/repositories.rs` always reads the raw
`requests` table and derives its average first-byte value over streamed rows.
The grouped timeseries prefers the `usage_rollups` table when rollups exist
and falls back to the raw table when they do not, and its rollup path
divides a summed first-byte duration by a summed first-byte count. The two
endpoints therefore read different storage layers over different windows and
derive their averages over different denominators, and the grouped endpoint's
numbers change depending on whether a rollup has been written yet.

**Routing-policy facts are hardcoded literals in two places.** The cache page
in `rust/src/server/dashboard/render/cache.rs` renders the mode, cache
metrics, compression metrics, stable-prefix hash, and compression policy as
fixed strings. The dashboard JSON in `rust/src/db/repositories.rs` renders the
same facts as fixed literals too, including a guardrails object asserting
that routing does not use cache metrics and does not use a stable-prefix
hash. No configuration key or runtime fact backs either assertion; the
values are correct today only because the corresponding capability does not
exist.

**A dashboard render indexes a month array without a bounds check.** In
`rust/src/server/dashboard/render/telemetry.rs` the heatmap label rendering
indexes a twelve-element month-name array with a month value converted to
`usize`. A month value outside the one-to-twelve range indexes out of
bounds.

## 4. Invariants that must not regress

- `rust/src/server/dashboard/` remains a thin HTTP adaptation and rendering
  layer. It may project facts but must not duplicate the state machines that
  own them.
- `rust/src/operations/status.rs`, `rust/src/operations/metrics.rs`, and
  `rust/src/db/` remain authoritative for live observations.
- The published dashboard JSON field names and shapes do not change.
- The embedded dashboard assets and the parity tooling remain consumers of an
  unchanged contract.
- The dashboard's server-rendered HTML and its JSON endpoints remain
  consistent with each other for every fact they both present.
- No secret, prompt, raw body, or cache key enters a dashboard projection.
- No new dependency, and no change to the query or projection hot path
  behavior beyond removing duplicated derivation.

## 5. Scope

### In scope

- One derivation for the transcoding counts and the per-direction breakdown.
- One denominator definition for the cache read ratio.
- One definition of the population and storage layer behind time to first
  token for both timeseries endpoints.
- A source of truth for the routing-policy facts the dashboard reports, and a
  guard that fails when a reported fact diverges from its source.
- The bounds check on the month-name array in the telemetry renderer.
- Regression tests for each of the above.

### Explicitly out of scope

- Any change to the meaning of a published field. The reconciliation must
  make the values agree, not redefine them. If the two current definitions
  cannot both be preserved, that is a decision for a separate plan.
- Any change to the embedded dashboard assets, the themes, or the DOM
  contract.
- Any change to the parity qualification harness or its recorded baselines.
- Any change to the persistence layer's table layout or its rollup schedule.
- Any new dashboard route or field.
- Any performance work on the query path.

## 6. Required production changes

**Single transcoding derivation.** The transcoding counts and the
per-direction breakdown must be computed once and consumed by both the CLI
command and the HTTP endpoint. The empty per-direction object in the HTTP
projection must be replaced by the real breakdown, or the breakdown must be
removed from the contract in both places. The dashboard repository and the
operations module must not each carry their own SQL for the same fact.

**Single cache ratio denominator.** One definition of the cache read ratio
must be used by the HTML page and the JSON projection. Pick the definition
that matches the existing label semantics and apply it in both places.

**Single time-to-first-token population.** The two timeseries endpoints must
agree on the population they measure and on the storage layer they read.
Either both read the raw table, or both read rollups with a documented
fallback. Whichever is chosen, the resulting averages must be equal for the
same filters.

**Routing-policy fact source.** The routing-policy facts the dashboard
reports must be derived from an actual source rather than asserted. If the
source is a configuration key, the key must be added to the configuration
schema and classified in `rust/src/config_reload_policy.rs`. If the fact is
a statement about the routing implementation rather than about configuration,
it must be derived from the routing module's own reported capability so that
adding the capability later changes the reported value.

**Bounds check.** The month-name lookup must not index out of bounds for any
input.

## 7. Ordered work packages

### Work package A — Unified transcoding derivation

Intent:

Make the CLI and the HTTP surface report the same transcoding facts.

Required changes:

- Move the transcoding derivation into one owner that both the operations
  module and the dashboard repository consume.
- Populate the per-direction breakdown in the HTTP projection, or remove it
  from both surfaces consistently.

Acceptance evidence:

- A test asserts the CLI command and the HTTP endpoint return identical
  transcoding counts for the same seeded data.
- A test asserts the per-direction breakdown is non-empty when the seeded
  data contains a transcode.

### Work package B — Unified cache read ratio

Intent:

Give the cache read ratio one denominator.

Required changes:

- Define the denominator once and apply it in both the HTML rendering and the
  JSON projection.

Acceptance evidence:

- A test asserts the HTML rendering and the JSON projection report the same
  ratio for the same seeded data.
- A test asserts the chosen denominator is the one the existing label
  describes.

### Work package C — Unified time-to-first-token population

Intent:

Make the two timeseries endpoints measure the same thing.

Required changes:

- Align the storage layer and the population between the ungrouped and the
  grouped timeseries.
- Document the choice in the API reference.

Acceptance evidence:

- A test asserts both endpoints report an equal average first-byte value for
  the same seeded data and filters, whether or not rollups exist.

### Work package D — Routing-policy fact source and guard

Intent:

Replace asserted routing-policy literals with a derived value.

Required changes:

- Derive each reported routing-policy fact from a real source.
- Add a guard that fails when the reported value diverges from the source.

Acceptance evidence:

- The guard fails when the underlying capability is enabled without the
  dashboard projection changing.

### Work package E — Telemetry month bounds

Intent:

Remove the out-of-bounds month index.

Required changes:

- Bound the month-name lookup so an out-of-range month cannot index out of
  bounds.

Acceptance evidence:

- A test asserts an out-of-range month value renders without panicking.

## 8. Failure, cancellation, restart, contention semantics

- The reconciliation must not introduce an await into a database call, and
  must not add a second database round trip where one suffices.
- The shared derivation must remain safe to call concurrently. It reads
  durable state and holds no mutable state.
- A degraded or empty data source must keep producing the current degraded
  response rather than an error.
- No cancellation path is added or changed.
- No restart behavior changes.

## 9. Compatibility and migration

- No migration. No table, column, or index change.
- No configuration key is removed. If the routing-policy fact source requires
  a new key, the key must be added and classified in
  `rust/src/config_reload_policy.rs`, and the schema-coverage guard must
  continue to pass.
- The published dashboard JSON field names and shapes do not change.
- Field *values* must not change for an installation that was previously
  self-consistent. Where the current values disagree, one of them changes;
  the milestone must record which and why.
- No public Rust API break.

## 10. Required tests

- `rust/tests/dashboard_filters.rs`: transcoding, cache ratio, and timeseries
  agreement coverage.
- `rust/tests/dashboard_ttft_qualification.rs`: the time-to-first-token
  population agreement, and the rollup-present and rollup-absent cases.
- `rust/tests/stats_command.rs` or the closest existing statistics target: the
  CLI and HTTP agreement for transcoding.
- `rust/src/server/dashboard/tests.rs`: the in-module dashboard tests, for
  the HTML and JSON agreement and the telemetry bounds.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test dashboard_ttft_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-dashboard.md` — record the single-source rule for
  the reconciled facts and the chosen denominator and population definitions.
- `architecture/deep-dive-metrics.md` — record the time-to-first-token
  population and storage-layer definition.
- `docs/api-reference.md` — record the time-to-first-token population choice
  and the per-direction breakdown contract if it changes.

## 13. Acceptance criteria

- The CLI transcoding command and the HTTP transcoding endpoint report
  identical counts for the same data.
- The dashboard HTML and JSON report the same cache read ratio.
- The two timeseries endpoints report the same average first-byte value for
  the same data and filters.
- Every routing-policy fact the dashboard reports is derived from a named
  source and guarded against drift.
- An out-of-range month value cannot panic the telemetry renderer.
- No published dashboard field name or shape changes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- Reconciling a divergence would require redefining a published field's
  meaning; that is a contract decision for a separate plan.
- A new configuration key would be needed and the maintainers have not
  accepted the addition.
- Reconciling the timeseries population would require changing the rollup
  schedule or the persistence layer's layout.
- The reconciliation would make `server/*` own a state machine it currently
  only projects.
- Evidence contradicts a dashboard invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- A before-and-after table of every reconciled fact showing both prior
  derivations and the unified one, with the chosen definition and the reason.
- Evidence that the parity tooling still passes against the recorded
  baselines.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

These are five independent reconciliations that share one theme. They are in
one milestone because they are one reviewable question: "is this number
computed twice?" If a work package turns out to need a contract decision,
stop and split it out rather than deciding the contract here.

The per-direction breakdown question is the one most likely to need a
decision: either the HTTP endpoint starts reporting it, or the CLI stops.
Do not leave the contract ambiguous.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
