# Deep Dive: Observability and Routing Traces

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md) (§12): §12 is the birds-eye summary, this file
is the observability authority.

Observability in the native runtime is bounded, deterministic, and
metadata-only. Every surface below reports scalar facts, counters, labels,
and truncated text — never credentials, prompts, raw request/response
bodies, cache keys, token values, or raw upstream error prose.

## Source boundaries

| Concern | Owner |
|---|---|
| Request/usage/latency/failure/reasoning/routing telemetry | `rust/src/operations/metrics.rs` (`MetricsWriteCoalescer`, `UsageMetricEvent`, `MetricsSnapshot`) |
| Compact proxy/provider health + shared readiness | `rust/src/operations/status.rs` (`ProxyStatusSnapshot`, `evaluate_readiness`, `aggregate_proxy`, `aggregate_provider`) |
| Process/runtime/generation projections | `rust/src/runtime_lifecycle/diagnostics.rs` (`RuntimeDiagnosticsSnapshot`) |
| Streaming-outcome counters | `rust/src/coordinator/streaming/diagnostics.rs` (`StreamDiagnostics`, `StreamDiagnosticEvent`) |
| Operator rollups, explain plans, dashboard queries | `rust/src/operations/operator.rs` over `rust/src/db/` repositories |

See [Metrics](deep-dive-metrics.md) for the coalescer contract (including
the M002 ownership paragraph, which is authoritative) and
[Dashboard](deep-dive-dashboard.md) for the HTTP/CLI presentation layer.

## Metrics pipeline (summary)

The request path hands the metrics boundary scalar, already-redacted
`UsageMetricEvent` facts keyed by bounded
(bucket, provider, model, account, protocol, streamed, status) tuples.
`MetricsConfig` defaults to `write_mode = "low_wear"` with
`max_buffered_events = 250` and a 300 s rollup bucket; `"immediate"`
flushes through the same coalescer/transaction boundary before returning.
Flush writes one ordered batch to `usage_rollups` (request/error/retry
counts, input/output/cache/reasoning tokens, `thinking_characters`,
`cost_microdollars` capped against overflow, bytes in/out, latency
sum/min/max, first-byte sum/count) and reports only counters
(`buffered_events`, `buffered_rows`, `total_received`, `total_flushed`,
`total_dropped`, `flush_failures`, `last_flush_rows`). Pricing feeds
accounting and observability only; it never influences routing.

## Status and shared readiness

`operations/status.rs` is the single health-aggregation boundary. It
combines active-generation account identity, live routing health, cached
catalog ping evidence, and runtime diagnostics into one bounded,
secret-free `ProxyStatusSnapshot` (`STATUS_SCHEMA_VERSION = 1`, at most
`MAX_STATUS_PROVIDERS = 256` provider rows, provider IDs truncated to
`MAX_PROVIDER_ID_CHARS = 96` chars, reason codes to
`MAX_REASON_CODE_CHARS = 64`).

- `ProxyStatus` is `Ready`/`Degraded`/`Unready`, plus CLI-only
  `Unavailable` (never emitted by the server endpoint).
- `ProviderStatus` is `Ready`/`Degraded`/`Unavailable`/`Disabled`/
  `Unknown`; `Unknown` means routable-by-gating but never observed, so
  status never claims verified readiness without evidence.
- `ProviderObservation` is `Verified`/`Failed`/`Stale`/`Never`, with
  staleness falling back to `DEFAULT_OBSERVATION_STALE_AFTER_SECS = 7200`
  when `models.stale_after_s` is unset.
- `evaluate_readiness()` is the one decision tree shared by
  `GET /v1/readyz` and status: runtime available, database writable,
  accounts configured/enabled with loaded credentials, and a usable model
  catalog. Disabled or `Unknown` providers never degrade the proxy by
  themselves; any degraded/unavailable enabled provider does, as do
  degraded background tasks, an active reload, or an abnormally retiring
  generation.
- The module performs no outbound provider requests, mutates no
  circuit-breaker state, and consumes raw ping errors only as a
  failure bit. Timestamps use `observed_at_now()` (RFC 3339 UTC, no
  monotonic clock leakage).

## Runtime diagnostics

`runtime_lifecycle/diagnostics.rs` projects process state without
retaining generation graphs: `active_generation` (leases, provider/
account/model counts, finalization jobs), `publication` (epoch,
admission/reload gates), up to 4 `retiring_generations`, `reload`
(in-progress flag, phase, last typed result), per-task `tasks`
(name, ownership, tick counts, last outcome), `startup_recovery`,
`shutdown`, monotonic `counters`, and the live `MetricsSnapshot`.
Text is capped (`MAX_DIAGNOSTIC_TEXT_BYTES = 96`,
`MAX_DIAGNOSTIC_PATHS = 32` paths) and digests render as 12-char
prefixes. Surfaced via `GET /api/stats/runtime` (authenticated) and
`eggpool runtime-status --json`.

## Streaming diagnostics

`coordinator/streaming/diagnostics.rs` keeps per-coordinator outcome
counters over 13 known outcomes (`response_header_timeout`,
`first_byte_timeout`, `stream_idle_timeout`, `stream_completed_canonical`,
`stream_completed_compatibility`, `empty_eof`,
`premature_eof_before_body/midstream`, `malformed_eof`,
`stream_responses_terminal_failure/terminal_incomplete`,
`upstream_midstream_error`, `client_cancelled`) plus an `unknown` bucket.
Each terminal path records one `StreamDiagnosticEvent` (outcome label,
attempt, bytes emitted, elapsed ms) — counts, durations, and labels
only. Variants carry no bodies, secrets, or provider prose, and SSE
transport EOF is never synthesized into success without terminal
evidence.

## Routing traces

Routing decisions are recorded as facts, not prompts: eligibility
outcomes and exclusion reason codes are queryable through
`eggpool accounts explain` (per-account eligible/reason, optional gates
and scores), the dashboard routing/reliability pages, and the bounded
`routing_decision_retain_days` rows. `[routing.trace]` (default
`mode = "off"`, `sample_rate = 0.0`) tunes live trace sampling separately
from `MetricsConfig::trace_sample_rate` (default `0.05`,
`aggregate_only = true`); traces describe which account was selected and why
others were excluded.

## Presentation

- `eggpool status` (one row per provider) and authenticated
  `GET /api/status` project the compact snapshot; `eggpool
  runtime-status` and `GET /api/stats/runtime` carry the deep
  process/runtime diagnostics.
- The dashboard is observational only: it renders bounded, redacted
  repository snapshots with escaped operator/provider values and never
  alters routing, quota, health, or provider state.

Persistence follows the low-wear policy: analytics rollups may be
coalesced and flushed on the deadline-bounded shutdown path, while
correctness-critical request/accounting lifecycle rows stay durable.
Shutdown performs one deadline-bounded metrics flush before the database
closes.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
```
