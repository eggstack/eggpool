# O007 — Operator Inspection, Maintenance, and Metrics Flush

Status: queued behind O006

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted O006.

## Objective

Implement the remaining database/catalog/routing/model/stats operator commands over existing Rust services and close the R008 `metrics_flush` deferred background capability without duplicating domain logic in CLI code.

Owned commands:

- `accounts list`;
- `accounts status`;
- `accounts explain`;
- `models refresh`;
- `modelinfo aliases`;
- `modelinfo list`;
- `modelinfo refresh`;
- `modelinfo repair`;
- `modelinfo show`;
- `stats transcoding`;
- `stats recompute-costs`;
- `stats repair-costs`;
- `stats explain-dashboard`;
- `dashboard public` only for any read/projection portion not already closed in O004.

## Core rule — reuse domain services

Do not port Python business logic into CLI formatting functions. Each command must call the already-closed Rust repository/service that owns the fact:

- account registry/catalog/routing/health/quota from M5;
- model catalog refresh from M5/R008;
- model-info repositories/services already ported for HTTP/dashboard behavior where available;
- dashboard/stats queries from M3/M5/M7 database repositories;
- cost recompute/repair algorithms from the current Rust equivalent or a narrow service port if not yet present.

If a Python command exposes behavior whose underlying Rust domain capability genuinely does not exist, implement that domain service in a reusable module first; do not bury it inside `runtime.rs` or Clap handlers.

## Accounts commands

### `accounts list`

Preserve account/provider/name/enabled/config summary projection from O001, including deterministic ordering and secret omission.

### `accounts status`

Project provider, routing priority, weight/enabled/health facts from current config/DB state. Avoid provider network calls for a status listing unless explicitly required by the oracle.

### `accounts explain`

Reuse the M5 routing eligibility/explain path and optional score/gate projections. Preserve flags:

- `--model`;
- `--provider`;
- `--protocol`;
- `--scores`;
- `--gates`.

The explanation must not acquire a real request claim/reservation, mutate fairness counters, or launch a semantic router selector. It is observational.

## Models and model-info

### `models refresh`

Invoke the existing bounded catalog refresh service with the same provider/account failure isolation as R008. One provider failure cannot crash the CLI/runtime or corrupt the last good catalog.

### `modelinfo`

Port each current projection/mutation over the existing schema. Preserve status/source filters, alias ordering, source provenance, manual refresh bounds, provider-catalog-only option, repair limit, and no-result behavior.

`modelinfo refresh` may perform bounded external source work but must use existing HTTP stack/rate/failure controls and must not introduce another recurring scheduler. Automatic model-info scheduling remains tied to catalog refresh per current architecture.

`modelinfo repair` must be idempotent and bounded by `--limit`; failed row repair cannot corrupt unrelated rows.

## Stats commands

### `stats transcoding`

Preserve period validation, JSON schema, totals/native/transcoded/per-direction output and deterministic sorting.

### `stats recompute-costs`

Preserve dry-run/apply semantics and `--limit`. Reuse pricing snapshots/resolver semantics. Apply mode must use bounded transactions and record only the current schema's intended cost changes.

### `stats repair-costs`

Preserve provider/since/limit filters, provider-reported-cost exclusion, suspicious-row criteria, dry-run/apply behavior, breakdown output, and idempotence.

### `stats explain-dashboard`

Use SQLite `EXPLAIN QUERY PLAN` against the actual dashboard queries. Validate `period`, `bucket`, and `group-by`; JSON/human projections follow O001. It must not mutate the database.

## Metrics flush business capability

R008 intentionally left `metrics_flush` deferred because no Rust coalescer/writer existed. O007 implements the missing business boundary and registers it through the M8 process task supervisor.

Requirements:

- honor `[metrics].write_mode` and current flush interval semantics;
- bounded in-memory aggregation only;
- no raw request/provider bodies or secrets in metric buffers;
- deterministic flush of supported aggregate/event data to existing tables/repositories;
- bounded batch/transaction duration;
- failed flush retains or safely drops/reports data according to the Python contract, never duplicates silently across retries;
- singleton/non-overlap through M8 supervisor;
- reload task-spec reconfiguration through R007 staging;
- shutdown performs the frozen final-flush policy without delaying forced shutdown indefinitely;
- diagnostic counters/history remain bounded.

Do not add OpenTelemetry, Prometheus server, external time-series DB, or a second metrics datastore.

If current request paths already write all required data immediately, O007 must still implement the configured coalescing behavior rather than registering a no-op placeholder solely to clear the deferred inventory.

## Command execution model

Read-only commands can open a short-lived DB/config service graph when the server is stopped. Commands that need active-generation/live process state should use O003's runtime/API boundary rather than constructing a competing in-process server runtime.

Explicitly classify each command as:

- offline-safe;
- requires local DB only;
- may use external network;
- requires running server.

Record this table in closure and keep startup cost appropriate to the class.

## Tests

Use seeded SQLite fixtures and deterministic local/fake source servers. Cover:

- all account command order/empty/disabled/health cases;
- explain eligibility gates/scores without state mutation;
- catalog refresh partial provider failure and last-good preservation;
- every modelinfo subcommand including missing/repair/idempotence/source filter;
- stats valid/invalid period/filter/grouping/JSON;
- dry-run produces no DB mutation;
- apply is idempotent/bounded and preserves provider-reported cost rows;
- EXPLAIN uses current queries and never writes;
- metrics write modes and interval reload;
- metrics buffer capacity/flush threshold;
- DB busy/failure/cancellation at flush boundaries;
- supervisor non-overlap and task enable/disable/reload/shutdown;
- repeated failures do not grow in-memory state unboundedly;
- no secrets/raw bodies in task diagnostics or CLI output.

Add a coverage test asserting every `AccountsCommand`, `ModelsCommand`, `ModelInfoCommand`, and `StatsCommand` variant dispatches to real behavior.

## Non-goals

- dashboard visual redesign;
- new analytics schema;
- external metrics backend;
- live-provider qualification as a closure prerequisite;
- new model-info scheduler.

## Verification

Run fmt/Clippy, focused O007 tests, M5 routing/catalog regressions, R006-R008 task regressions, dashboard/stats DB tests, aggregate Rust, targeted Python accounts/modelinfo/stats/metrics tests, migration oracle, and static checks.

## Closure evidence

Write `migration-rs/closure/operations/007-status.md` with command/domain ownership map, offline/network/runtime classification, differential output matrix, metrics boundedness/failure tests, task registration proof, dependency/schema review, and unresolved findings.

## Acceptance criteria

O007 closes only when every owned inspection/maintenance command has real Rust behavior over the canonical domain services, dry-run/apply semantics are safe, and `metrics_flush` is a real bounded singleton M8 task rather than a placeholder.

Accepted O007 promotes only O008.