---
name: architecture
description: Architecture principles and design decisions for the EggPool project. Use when understanding the codebase structure, making design decisions, or reviewing architectural changes. Covers package boundaries, request lifecycle, and core invariants.
---

# Architecture Principles

See `architecture/README.md` for the full design overview.

## Core Principles

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations

## Request Lifecycle

- All data-plane requests flow through `RequestCoordinator`
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- Streaming cancellation finalization is wrapped in `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task cancellation cannot kill the finalizer while it holds the DB lock; the outer `Stale request finalizer` background task (`app._finalize_stale_requests`) is the safety net for anything that escapes this path
- `_crash_recovery` runs at every startup and recovers ALL pending requests and active reservations (no time threshold); a process restart is a definitive boundary, so leaked state from the previous process is unconditionally cleaned up
- **Structured observability persistence (migrations 0026-0029)**: every `request_attempts` row carries `provider_id/model_id/protocol/retry_category/release_reason/bytes_received/latency_ms/streamed/is_retry_outcome`; every routing decision is persisted as a `routing_decisions` row inside the same transaction as the `request_attempts` INSERT so the audit trail cannot diverge from durable state; the safety-net tasks (`_crash_recovery`, `_finalize_stale_requests_once`, `reconcile_expired_reservations`) record `operational_events` rows in the same transaction as the durable state mutation; latency is decomposed into `upstream_connect_ms / upstream_read_ms / coordinator_overhead_ms` so the dashboard can tell whether slowness is network, upstream, or eggpool-side
- **Phase 1 cache observability is reporting-only**: every finalized request is tagged with `cache_counter_status ∈ {reported, not_reported, unknown_format}` plus supporting cache-token columns (`cached_input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_write_input_tokens`, etc.) populated by `src/eggpool/proxy/normalized_usage.py`. The `QuotaFairScorer` does NOT consume cache fields (pinned by `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`); only request count + token count + cost (audit) + active count + health feed routing. Dashboard renders coverage under Runtime → Cache observability and `GET /api/stats/cache-observability` exposes the JSON breakdown. See `plans/cache_compression_phase_01_cache_token_observability.md`.
- **Phase 3 transcoder cache stability is reporting-only**: every cross-protocol request is observed by `src/eggpool/transcoder/cache_stability.py`. The `CacheBoundaryTracker` (carried on `TranscodeContext.cache_boundary_tracker`, capped at 64 annotations) records every `cache_control` boundary as `preserved`, `preserved_relocated`, `dropped_unsupported_target`, `dropped_feature_disabled`, `dropped_invalid_shape`, or (reserved) `synthesized`. `extract_cache_boundaries`, `extract_provider_visible_prefix`, `stable_dumps`, and `stable_hash` give operators deterministic cache-key introspection. Both transcoders emit `cache_control_unsupported_by_target_protocol`, `cache_control_feature_disabled`, `cache_control_invalid_shape`, `provider_extension_not_preserved`, `stable_prefix_preserved`, and `stable_prefix_reordered_canonically` as structured loss warnings. The OpenAI→Anthropic path preserves `tools[].cache_control` annotations and records a `preserved` boundary; Anthropic→OpenAI drops every `cache_control` (OpenAI has no equivalent field) and records a `dropped_unsupported_target` boundary. Routing still does NOT consume cache fields; cache stability is reporting-only. See `plans/cache_compression_phase_03_transcoder_cache_stability.md`. **Loss-policy enforcement**: when `loss_policy = "reject"` is set on `[transcoder]`, the body transcoder raises `eggpool.transcoder.errors.TranscodeLossError` (rendered as HTTP 400 with `invalid_request_error`) before upstream dispatch if any of the five protected cache-control loss kinds is recorded. The `warn` default preserves v1 behaviour. The preflight in `proxy_request.py` also enforces `loss_policy = "reject"` more broadly (any loss warning) and runs the transcoder in `warn` mode internally so it can collect the full warning list.
- **Runtime metrics**: `eggpool runtime-status` (CLI) and `GET /api/stats/runtime` (API) expose live operational health — process topology, memory usage, active thread count, background task status, DB health, OS load average (`load` section: 1m/5m/15m + normalized per-core), a bounded rolling-window upstream dispatch overhead (`dispatch_overhead` section: avg/min/max/p50/p95 over the last 100 attempts), and in-flight request counts. The `/runtime` dashboard page renders these metrics for operator visibility. Runtime metrics are always auth-gated regardless of dashboard public/private setting
- **Protocol transcoding**: when a client sends a request in one protocol but the routed provider only supports another, the transcoder module translates the request/response body. Phase 2 body translation, Phase 3 streaming SSE translation, Phase 4 routing eligibility widening, Phase 5 operator controls and docs, and Phase 6.1 tool-use/tool_calls body and streaming translation (including `pause_turn` sentinel handling and `stream_options.include_usage` lifting) are implemented in `src/eggpool/transcoder/`; `select_transcoder()` in `protocol.py` is the dispatch source of truth. Controlled by `[transcoder]` config; on by default.
- **Cache compression**: Phase 4 observe-mode accounting lives in `src/eggpool/transcoder/compression/` (`analyze_compression` returns a `CompressionObservation` per request; persisted via migration 0042); Phase 5 adds a safe-mode mutating applier (`apply_safe_compression` returns a `CompressionResult`, persists via migration 0043, fails-closed on stable_prefix_hash mismatch). Controlled by `[compression]` config; observe mode default.

## Runtime Observability

- `DispatchOverheadRecorder` (`src/eggpool/runtime_dispatch.py`) is a bounded rolling-window recorder (default `deque(maxlen=100)`) for EggPool-local pre-dispatch overhead. Stores only integer nanoseconds; no request body, model ID, account name, auth header, or client IP ever enters the buffer. Thread-safe via a tiny `threading.Lock`
- The recorder is built during lifespan setup in `app.py` before `RequestCoordinator` and `RuntimeMetricsService`, stored on `app.state.dispatch_overhead_recorder`, and passed (duck-typed) into both. The coordinator stores it as `self._dispatch_overhead_recorder` and records `time.perf_counter_ns() - context.started_monotonic_ns` immediately before `client.send(...)` in both `_execute_non_streaming` and `_execute_streaming`. One sample per upstream attempt (so retries each contribute)
- `ProxyRequestContext.started_monotonic_ns` is set via `time.perf_counter_ns` default factory so wall-clock NTP jumps never contaminate the metric. The older `started_monotonic: float` (wall-clock-shaped) field is preserved for compatibility
- The Runtime dashboard renders two new cards in the resource row: `Load average` (1m primary metric, with `load/core` subtitle and CPU count when available) and `Dispatch overhead` (avg as primary, p95/p99/max + sample count in subtitle). The static `Threads` (configured-server-threads) and the normal `Processes` cards were removed to make room; `processes.process_count_warning = True` surfaces a warning-only panel instead
- `RuntimeMetricsService.snapshot()` adds two new top-level sections: `load` (with `available` flag — `false` on platforms without `os.getloadavg`) and `dispatch_overhead` (empty stub when no recorder is wired). Both follow the existing probe-error best-effort pattern — failures append a bounded string to `probe_errors` and return `{"error": str(exc)}`
- No SQLite migration. No new dependency. Stdlib only (`collections.deque`, `threading`, `time.perf_counter_ns`, `os.getloadavg`, `os.cpu_count`). Operator can disable the recorder by simply not passing it; the snapshot still returns an empty stub
- **Thinking/reasoning observability**: in-memory counters (`ThinkingMetricsCounter`) track thinking decision outcomes. Per-request trace stored as `thinking_trace_json`. Exposed via `/api/stats/thinking` and dashboard overview. See `architecture/README.md` § Thinking/Reasoning Observability.

## Process Model

- `eggpool serve` is a single supervisor process that invokes `Granian` with `workers=1`; Granian spawns one worker, so exactly two processes run under the canonical name
- The Granian worker is launched with `process_name="eggpool"`, so both supervisor and worker appear as `eggpool` in `ps` / `top` / `pgrep` (not as a generic `python` entry)
- The supervisor owns the PID file. Path resolution lives in `eggpool.runtime_paths.default_pid_file()` and follows this precedence: `$EGGPOOL_PID_FILE` → `$XDG_RUNTIME_DIR/eggpool.pid` → `~/.local/state/eggpool/eggpool.pid` → `/tmp/eggpool-<UID>.pid`. The supervisor writes `os.getpid()` before `Granian.serve()` and clears it in a `finally` block; the FastAPI lifespan does not touch the PID file. This prevents the "kill worker leaves supervisor orphaned" failure mode
- `eggpool serve` refuses to start a second instance: first checks `runtime.read_pid()` + `runtime.is_process_running()`; if no live PID, probes `GET /v1/healthz` via stdlib `urllib.request` (bind `0.0.0.0` / `::` is rewritten to `127.0.0.1`). A live PID or a 200 from the probe exits `1`. Stale PID files (PID not running) are cleared before starting
- `[server].threads` (int, default `1`, min `1`, max `64`) controls Granian `runtime_threads` (the number of worker event-loop threads). Default `1` keeps process and thread counts minimal for SBC / Raspberry Pi; raise on capable hardware
- `eggpool restart` no longer has inline subprocess logic; it delegates to `runtime.restart_server` which calls `runtime.send_sigterm` and `runtime.start_server` (which `subprocess.Popen`s a new supervisor)
- `eggpool ensure-running` is the canonical cron watchdog — it atomically checks-and-starts without ever spawning a duplicate instance. Use it from `@reboot` and `*/5 * * * *` crontab lines instead of `croncheck || eggpool serve &`

### Daemon Mode

- `eggpool serve --daemon` is the operator-facing detach helper for personal / SBC deployments. It validates the config, refuses to start a second instance, then spawns a detached child and returns promptly with a short success message pointing at the log file
- The detached child runs the normal foreground `serve` command (Granian supervisor + worker). `--daemon` is **never** forwarded to the child; detachment is purely a parent-side concern
- stdin/stdout/stderr are detached from the calling terminal: `stdin=subprocess.DEVNULL`, `stdout`/`stderr` → log file (or `/dev/null` when `--quiet` is set without `--log-file`). The child is launched with `start_new_session=True` so it survives shell exit and signals to the parent CLI do not propagate
- Default log destination is `~/.local/state/eggpool/eggpool.log`, resolvable via `eggpool.runtime_paths.default_log_file()`. Override with `--log-file PATH` or `$EGGPOOL_LOG_FILE`. A log file beats `/dev/null` by default because a silent background failure is hard to diagnose
- The detached child is the supervisor; it owns its own PID file lifecycle via `runtime.write_pid_file()` / `runtime.clear_pid_file()`. The `Popen` handle from `start_server()` is intentionally not awaited; the parent returns as soon as the child is spawned
- `serve --daemon` refuses to run as root unless `--as-root` is passed (prevents accidental root personal deployment)
- Systemd should **not** use `--daemon`. The systemd unit already owns the process lifecycle; run foreground `serve` and let systemd manage the PID, journal logs, and restart policy
- `runtime.start_server()` signature: `start_server(config_path, *, cwd=None, daemon=True, log_path=None, quiet=True, verify=False, verify_timeout_s=3.0)`. `runtime.restart_server()` accepts the same `daemon`, `log_path`, and `quiet` options. The CLI flags `eggpool serve --daemon`, `--log-file PATH`, `--quiet`, and `--as-root` map directly to these parameters
## Installation and Deployment

- `eggpool.deploy_user` — `DeployUser`, `resolve_deploy_user()` (handles normal, `SUDO_USER`, and direct-root cases), `resolve_config_path()` (single source of truth for `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`), `resolve_env_path()`, and XDG default helpers (`default_config_dir()` / `default_data_dir()` / `default_state_dir()` / `default_config_path()` / `default_env_path()`)
- `eggpool.deploy` — bundled constants (`SYSTEMD_UNIT`, `LOGROTATE_CONF`, `CRON_BACKUP_FILE`, `CRON_BACKUP_SCRIPT`) + personal builders (`build_personal_systemd_unit`, `build_personal_watchdog_cron`, `build_personal_backup_block`, `build_personal_logrotate`) + cron block management (`install_cron_block`, `remove_cron_block`, `strip_managed_cron_blocks`). Every cron block is bracketed by `# BEGIN EggPool ...` / `# END EggPool ...` markers so uninstall only strips eggpool-owned lines
- `eggpool.cli_full.deploy_*` — Click commands: `deploy systemd [--install|--production|--as-root]`, `deploy cron [--install|--uninstall|--interval N]` (the **watchdog**, not the backup), `deploy backup-cron` (the actual backup), `deploy logrotate [--install]` (validates via `logrotate -d`), `deploy all`
- `eggpool.cli_full.uninstall [--deploy-artifacts]` — detects the install method, previews PATH edits via `preview_eggpool_path_changes()` + `RcFileChange` before writing, and removes the binary, config, data, and shell-rc entries. `--deploy-artifacts` extends this to systemd / logrotate / cron / backup-script cleanup
- Production systemd unit (`SYSTEMD_UNIT` constant) is the source of truth; `deploy/eggpool.service` is kept byte-for-byte identical
- `eggpool.deploy_user.resolve_config_path()` is the single source of truth for every CLI command's config-path resolution
- `eggpool deploy cron` is the **watchdog**; `eggpool deploy backup-cron` is the **backup**. The two are intentionally separate commands so a missing backup never blocks the watchdog and vice versa

## Fast-Path CLI

- The entry point `eggpool.cli:main` tries `fastcli.maybe_run_fast_command()` before importing Click
- The fast path imports `eggpool.runtime_paths` and `eggpool.fastcli` only — both modules are stdlib-only
- Recognized fast commands: `croncheck` (pure status probe) and `ensure-running` (check-and-spawn watchdog)
- Everything else falls through to `eggpool.cli_full` (the heavy Click CLI)
- Public symbol forwarding via PEP 562 `__getattr__` keeps `from eggpool.cli import cli` working for tests without forcing the heavy CLI graph to load at `eggpool.cli` import time

## Database Invariants

- SQLite is the durable source of truth for quota windows (5h/7d/30d)
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- All SQL operations on the shared connection are serialized; no task can execute SQL inside another task's transaction
- Every DML write must run inside `async with db.transaction():`; write helpers refuse to operate outside an owned transaction
- `Database.vacuum()` is the only sanctioned path for `VACUUM` in production code

## Metrics Buffering

- `MetricsWriteCoalescer` buffers lossy analytics events in memory and flushes to `usage_rollups` periodically
- Correctness-critical writes (request state, reservations, routing) remain immediate and are never buffered
- Three `write_mode` values: `immediate` (direct write), `balanced` (30s flush), `low_wear` (120s flush, coarser buckets)
- The coalescer emits one `UsageMetricEvent` per terminal request transition from `RequestFinalizer.finalize()`
- Shutdown flush has a 5-second timeout; lossy analytics are best-effort
- `usage_rollups` table uses additive upserts (`INSERT ... ON CONFLICT DO UPDATE SET col = col + excluded.col`)
- Rollup retention is configurable via `metrics.rollup_retain_days` (default 90)
- Runtime diagnostics expose buffer health via `/api/stats/runtime` (`metrics_buffer` section)

## Concurrency

- Readiness probes use `probe_writable()` with owned transactions, never interfere with request lifecycle work
- Child tasks cannot inherit transaction ownership (both task identity and ContextVar depth must match)
- Reservation and active-count in-memory cleanup occur only when the database reservation actually transitions
- Exhausted retries cannot corrupt another request's in-memory state

## Quota and Routing

- Successful responses without terminal usage consume the reservation estimate
- Unknown model protocols are rejected before durable selection
- Quota-exhausted accounts recover after cooldown expiration via `_refresh_transient_state()`
- Pending active requests are excluded from expiry cleanup
- Cancelled nonzero-cost requests remain in usage windows
- Cache-only rate changes create snapshots; cache-only token usage invokes cost calculation
- Tier-based routing: eligible accounts are grouped by `routing_priority` (default `0`); the highest non-empty tier wins; the `QuotaFairScorer` load-balances within the chosen tier; lower tiers are reached only via `exclude_accounts` retry paths
- `routing_priority` orders tiers; `weight` orders accounts within a tier — the two compose
- **Routing uses request count + token count, NOT cost**: per-window utilization = `max(request_count/capacity_requests, token_count/capacity_tokens)`. `cost_microdollars` is unreliable across providers (zero reported, unit confusion) so cost fields are kept on `PersistedWindowSnapshot` for audit only. Soft default capacities (`DEFAULT_REQUEST_CAPACITY_*`, `DEFAULT_TOKEN_CAPACITY_*` in `src/eggpool/quota/estimation.py`) keep the rotor useful even when no explicit capacity is configured.
- **`prefer_native`**: when transcoding is enabled and `prefer_native = true` (default), native-protocol accounts outrank transcodable ones within the same tier via a secondary sort key (`requires_transcode: False` sorts before `True`). When `prefer_native = false`, transcodable accounts may outrank native ones if their `final_score` is lower.
- `collapse_models = false` (default) exposes provider-suffixed model IDs; `collapse_models = true` collapses to a single unsuffixed ID routed across all providers
- **Upstream-authoritative suppression** (default `local_quota_mode = "score_only"`): local cost estimates influence routing rank but never hard-exclude accounts. Only upstream-observed failures, explicit operator disablement, catalog/protocol incompatibility, or an explicit `local_quota_mode = "hard_cap"` may make an account ineligible.
- **`hard_cap` opt-in**: setting `local_quota_mode = "hard_cap"` restores legacy behavior where locally over-quota accounts are excluded. Subscription aggregators should normally leave the default unchanged; a warning is logged at startup when `hard_cap` is enabled.
- Reservation cleanup is gated on `reservation_released` alone — `health_already_applied` must not be a precondition for in-memory reservation teardown, otherwise single-account 429/402 paths leak in-memory reservation state.
- **Lock-scope and publish ordering**: in `RequestCoordinator._select_and_persist_attempt()`, the runtime publication step (`Router.increment_active_request_count` + `QuotaEstimator.add_reservation`) runs INSIDE `_select_lock` AFTER the durable transaction commits but BEFORE the lock releases. The two contexts are explicit nested `async with` blocks (outer `_select_lock`, inner `_db.transaction()`). Do NOT collapse them into a compound `async with self._select_lock, self._db.transaction():` because Python exits context managers right-to-left (the transaction would still commit before the lock released — that is not the bug). The actual bug was that the runtime publication block lived INSIDE the transaction body, so active-count and reserved-cost state were published before the transaction committed. The key invariant is block placement (publication must be outside the DB transaction body but still inside `_select_lock`), not context-exit order. The compensation chain (`decrement` → `finalize_failed_attempt(asyncio.shield(...))` → `health_manager.release_request` → set `client_metadata["post_commit_interrupted"]` → `raise`) wraps the publish step; the outer `except BaseException:` catches `CancelledError` / `SystemExit` / `KeyboardInterrupt` and re-raises them after compensation so they cannot be swallowed.
- **Score components on every routing decision**: `RoutingDecisionTrace.score_components` carries the per-account breakdown (`quota_score`, `inflight_penalty`, `health_penalty`, `final_score`, `weight`, `active_request_count`, `reserved_microdollars`, per-window `cost_*` / `capacity_*` microdollar values, `util_5h` / `util_7d` / `util_30d` utilization ratios (None when capacity is unconfigured), `tie_break` summary naming the decisive factor between the chosen account and the runner-up (`tier`, `quota`, `inflight`, `transcode`, `near_tie`, `exact_tie`, `no_runner_up`), `tier`, `requires_transcode`, plus the top 5 near-tie candidates) for the selected account. `to_score_components_json()` serializes it to TEXT; migration `0035` adds the `score_components_json` column to `routing_decisions` (default `'{}'` for backward compatibility). Consumed by the dashboard and `eggpool accounts explain`; do not re-score from quota tables when only the diagnostic breakdown is needed.
- **Eligibility explanation**: `Router.explain_account_eligibility(model_id, provider_id, protocol)` returns one row per registered account with `eligible: bool` plus a stable `reason_code` (`ok`, `disabled`, `auth_failed`, `quota_exhausted`, `cooldown`, `rate_limited`, `circuit_open`, `wrong_provider`, `no_protocol`, `protocol_mismatch`, `no_model`, `model_stale`). The `reason_detail` includes the account name, provider id, configured protocols, requested model id, and stale-window seconds so operators can act on the diagnosis. Exposed via `eggpool accounts explain --model <id> [--provider P] [--protocol P] [--gates]` (`click.echo` table; previously a Rich table) and `GET /api/stats/routing/eligibility` (JSON). The command opens the database, runs migrations on a fresh install, and calls `ModelCatalogCache.hydrate_from_db(db)` to populate the in-memory cache from the durable `models` / `provider_model_metadata` / `account_models` tables before classification (the previous version constructed an empty cache and never saw real catalog state). Re-evaluated on every call against the live registry + catalog so operators can diagnose routing skew without restarting the service.
- **Gate diagnostics on explain**: `Router.explain_account_eligibility(include_gates=True)` adds a per-account gate breakdown dict (config, credentials, health, circuit, provider id registry/catalog match, provider-supports-protocol, model support row/availability/freshness, provider-metadata-exists, protocol_match, local_quota_gate, final_eligible). Surfaced via `eggpool accounts explain --gates`. The dict is informational; the canonical decision still comes from `_classify_eligibility`.
- **Catalog refresh semantics — non-destructive by default**: `CatalogService._fetch_and_process_account` returns an `AccountCatalogOutcome` enum (`SUCCESS_AUTHORITATIVE`, `SUCCESS_PARTIAL`, `SUCCESS_EMPTY`, `FAILED`, `SKIPPED`). Per-account cache updates run through `ModelCatalogCache.update_from_account(authoritative, allow_withdrawals)`; both flags default to `False`. The destructive `mark_account_models_unavailable` step is gated on `authoritative=True AND allow_withdrawals=True`, so the cache layer itself enforces non-destructive behavior. `ModelsConfig.catalog_withdrawal_policy` (`preserve_until_health` default, `confirmed_once`, `confirmed_twice`) controls when the service flips `allow_withdrawals=True`. `SUCCESS_PARTIAL` always forces withdrawal off because a partial response is never a complete withdrawal confirmation. The per-cycle INFO summary at `_log_refresh_summary` reports per-outcome counts. Failed/empty/partial refreshes never de-pool a healthy account; health is the only de-pooling mechanism under the default policy. See `architecture/README.md` § Catalog Refresh Semantics.

## Multi-Provider

- Provider-suffixed model IDs: `model-id/provider-id` format
- `ProviderClientPool` manages per-provider `httpx.AsyncClient` instances for upstream LLM forwarding and catalog model-list fetches
- `OutboundClientManager` owns a shared `httpx.AsyncClient` for non-provider network paths: update checks (PyPI), external catalog fetches (OpenRouter), and future background/CLI network operations. Initialized once at startup, reused by all background tasks. The `build_count` property should stabilize at 1; growth with request volume indicates a hot-path client construction bug. Accepts `[network]` config for transport tuning (connect_timeout_s, read_timeout_s, max_connections, max_keepalive, keepalive_expiry_s). Integrates a `DnsNetworkBackend` that wraps the default httpcore transport and caches resolved DNS entries in memory, reducing latency for repeated connections to the same upstream hosts. The DNS cache is controlled by `[network.dns_cache]` (enabled by default, TTL 1800s, max 50 entries). Exposes `snapshot()` with build_count, request_count, error_count for runtime diagnostics. `inject_client()` is the test escape hatch for injecting mock transports
- Hot-path provider requests must **never** construct fresh HTTP clients. Background and CLI paths should use the shared outbound client from `OutboundClientManager` rather than calling `httpx.get()` or building ad-hoc clients. `warn_adhoc_clientConstruction()` emits a runtime warning after startup when fresh clients are constructed outside managed paths
- Per-provider upstream paths: `openai_path`, `anthropic_path`, `models_endpoint` (a `[providers.<id>.models_endpoint]` table with `method`, `path`, `query`, `body`, `required`; `method = "DISABLED"` skips live model listing). Legacy `models_path` / `models_method` scalars are auto-synthesized into a default `models_endpoint`.
- **`static_models`** — providers may declare `[[providers.<id>.static_models]]` rows (`ProviderStaticModelConfig`) that seed the catalog at refresh time. Required when `models_endpoint.method = "DISABLED"`. Static rows participate in the same protocol/limit machinery as live rows; static-source fields (`protocol`, `protocol_source == "static_config"`, `supports_tools`, `supports_vision`) are preserved by `ModelCatalogCache._preserve_static_fields` when live rows arrive without them.
- Legacy flat `[[accounts]]` auto-normalizes to default `opencode-go` provider
- `parse_model_provider()` in `routing/provider.py` handles suffix parsing;
  `catalog/cache.py` retains a compatibility alias
- **`routing_priority`** — `[providers.<id>]` accepts `routing_priority: int` with `Field(default=0, ge=0)`. Higher values are preferred. The field is per-provider; accounts inside a tier are still load-balanced by `QuotaFairScorer`.
- **`collapse_models`** — `[models]` accepts `collapse_models: bool` (default `false`). When `false`, the catalog exposes one provider-suffixed entry per `(model_id, provider_id)`. When `true`, the same base model collapses to a single unsuffixed `model_id` and is routed across every provider that supports it.
- `eggpool connect` writes `routing_priority = 0` on every newly created provider block and leaves existing blocks untouched, so operators can edit one number to rebalance.
- `eggpool configsetup opencode` honors `collapse_models`: suffixed model IDs when `false`, unsuffixed when `true`.
- `/v1/models` includes an `eggpool.routing_priority` extension field on each suffixed entry.
- See `docs/providers.md` for the worked example with three providers and three priorities.

### Protocol Transcoding

When a provider-suffixed model routes to a provider whose `protocols` list does not include the client's request protocol, the `RequestCoordinator` invokes the transcoder to convert between formats. The `upstream_protocol` field on `ProxyRequestContext` carries the provider-side protocol for upstream dispatch. Controlled by `[transcoder]` config; disabled by default.

**Phase 2 — Body translation**: text-only, non-streaming request/response body translation is implemented in `src/eggpool/transcoder/`. The `BodyTranscoder` Protocol (`protocol.py`) defines the interface; `OpenAIToAnthropic` and `AnthropicToOpenAI` are the concrete translators. `select_transcoder()` is the single source of truth for dispatch. The coordinator pre-translates the request body before dispatch, decodes the response body on success, and re-renders non-retryable errors in the client protocol. Loss-of-information warnings are accumulated on `TranscodeContext.loss_warnings` and logged at request completion.

**Phase 3 — Streaming SSE translation**: bidirectional streaming frame translation between OpenAI SSE and Anthropic SSE formats. `select_streaming_transcoder()` dispatches to `OpenAIToAnthropicStreaming` or `AnthropicToOpenAIStreaming`. Implemented in `src/eggpool/transcoder/streaming.py`.

**Phase 4 — Routing eligibility widening**: when `[transcoder] enabled = true`, the routing layer widens the candidate set to include accounts whose `provider.protocols` includes the model's native protocol even if it does not include the client protocol. `_validate_endpoint` checks for transcodable routes before raising `ProtocolMismatchError`. The `_resolve_upstream_protocol` method determines which protocol to use upstream based on the largest eligible-account set. `prefer_native = true` (default) keeps native-protocol accounts ranked above transcodable ones via a secondary sort key in `QuotaFairScorer`. The two-pass context-limit check in `api/proxy_request.py` validates both client-side and upstream limits when transcoding is active.

**Phase 5 — Operator controls and docs**: the default `[transcoder]` config block is documented in `config.example.toml`. `eggpool stats transcoding [--period 1d|7d|30d] [--json]` reports transcoded request counts per direction and loss-warning summaries. The dashboard `/runtime` page includes a "Transcoding" card with total transcoded requests, direction breakdown, and top loss warnings. Boot-time INFO line fires when `[transcoder] enabled = true`. Structured INFO log per transcoded request includes `request_id`, `client_protocol`, `upstream_protocol`, `account`, `provider`, `native_match`, and `loss_warnings` count. See `docs/transcoding.md`.

**Phase 6.1 — Tool-use transcoding**: bidirectional tool calling translation in both directions for non-streaming and streaming requests. `OpenAIToAnthropic.encode_request` / `decode_response` and `AnthropicToOpenAI.encode_request` / `decode_response` translate `tools`, `tool_choice`, `parallel_tool_calls`, assistant `tool_calls` history, `role: "tool"` history, and `tool_use` / `tool_result` content blocks. A per-request `ToolCallIdMap` (on `TranscodeContext.id_map`) mints `call_<24 hex>` and `toolu_<24 hex>` ids so the two namespaces never collide. `OpenAIToAnthropicStreaming` and `AnthropicToOpenAIStreaming` extend their state machines to track `content_block_start` / `input_json_delta` / `content_block_stop` triples and emit OpenAI `tool_calls` deltas in insertion order; the reverse direction buffers OpenAI `tool_calls[*].function.arguments` chunks and flushes Anthropic `tool_use` blocks on `finish_reason: "tool_calls"`. Anthropic's `pause_turn` `stop_reason` maps to `finish_reason: "tool_calls"` plus a synthetic `__eggpool_pause_turn__` tool_call entry on both streaming and non-streaming paths. `stream_options.include_usage` is lifted onto `TranscodeContext.request_include_usage` so the streaming transcoder can decide whether to forward upstream usage chunks. New loss-warning kinds (`tool_call_id_translated`, `tool_call_id_changed`, `parallel_tool_calls_collapsed`, `malformed_tool_arguments`, `invalid_tool_choice`, `unsupported_tool_type`, `empty_tool_use_block`, `tool_result_image_dropped`, `tool_result_error_passthrough`, `cache_control_feature_disabled`, `cache_control_unsupported_by_target_protocol`, `pause_turn`, `non_text_content_dropped`) are added to `LOSS_WARNING_KINDS`. See `docs/transcoding.md` § Tool-Use Transcoding and `plans/tooltranscoding.md` for the full design.

**Phase 6.2 — Vision / image input**: `image_url` parts (base64 and URL) translate to Anthropic `image` blocks and vice versa; data URIs are split so Anthropic receives raw base64 in `source.data`. `document` blocks (PDF only) translate to OpenAI `file` parts. Size limits are enforced by dropping images over 5 MB and PDFs over 32 MB with warnings. New kinds: `image_unsupported_format`, `image_too_large`, `pdf_too_large`, `document_url_dropped`, `document_unsupported_media`. Gated behind `[transcoder.features] vision = false`.

**Phase 6.3 — Extended thinking / reasoning**: `reasoning_effort` maps to Anthropic `thinking` with budget heuristic (low→1024, medium→4096, high→16384). `reasoning_content` in history maps to `thinking` blocks and vice versa. Streaming `thinking_delta` maps to OpenAI reasoning delta fields (configurable via `[transcoder.openai_reasoning_fields]`). Thinking signatures are dropped with a warning. Streaming thinking deltas are feature-gated consistently with non-streaming paths. New kinds: `thinking_signature_dropped`, `reasoning_content_dropped`. Gated behind `[transcoder.features] thinking = false`.

**Phase 7 — Budget resolution**: `resolve_thinking_budget()` in `src/eggpool/transcoder/budget_resolver.py` is the single source of truth for effort-to-budget translation. Resolution order: explicit `thinking.budget_tokens` (Anthropic style) → `reasoning_effort` (OpenAI style) via `ThinkingCapability.effort_to_budget_tokens` → `[transcoder.thinking_budget_defaults]` → hard-coded fallback (low=1024, medium=4096, high=16384). Budgets are clamped to `budget_tokens_min`/`budget_tokens_max` when known. `budget_resolution_policy = "strict"` rejects unknown efforts and clamped budgets before dispatch. New loss-warning kinds: `budget_clamped`, `unknown_effort`, `budget_rejected`, `budget_resolution_no_input`. The `BodyTranscoder.encode_request` protocol accepts optional `thinking_capability`, `budget_defaults`, and `budget_resolution_policy` kwargs.

**Phase 8 — Response-field compatibility**: configurable OpenAI-compatible reasoning field names for both streaming and non-streaming responses. `[transcoder.openai_reasoning_fields]` controls `non_stream` (default `["reasoning_content"]`) and `stream_delta` (default `["reasoning"]`) field names. `emit_compat_aliases = false` (default) emits only the primary field; when true, additional aliases are emitted. Streaming thinking deltas are now feature-gated consistently with non-streaming paths — when `[transcoder.features].thinking = false`, streaming thinking deltas are dropped. The `build_reasoning_fields()` helper builds the field dict from config.

**Phase 6.4 — Structured outputs**: `response_format: json_object` and `response_format: json_schema` coerce to system-prompt instructions asking the model to respond in JSON. New kind: `response_format_to_system_prompt`. Gated behind `[transcoder.features] structured_outputs = false`.

**Phase 6.5 — Anthropic primitives**: `top_k` dropped with `top_k_dropped` warning. `cache_control`, `context_management`, `container`, `mcp_servers` dropped with specific warnings. `metadata.user_id` translates verbatim to OpenAI `user`. Gated behind `[transcoder.features] anthropic_primitives = false`. The `loss_policy = "reject"` setting is also implemented — when set, requests whose translation would lose information are rejected with HTTP 400 before dispatch. See `plans/phase-6-reject-policy.md`.

### Provider Contract Rendering

`src/eggpool/providers/contract.py` centralizes:
- `compose_provider_url()` — absolute URL composition
- `build_auth_headers()` — provider-aware auth header construction
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

The coordinator calls `_build_upstream_headers()` and `_get_upstream_url()` which use the provider
contract when available, falling back to legacy Bearer auth and bare paths respectively.

### URL Composition Consistency

`compose_provider_url()` is the single source of truth for upstream URL
construction. Catalog fetch, non-streaming chat, and streaming chat all
call it through the provider config so a provider cannot list models at
one host and dispatch requests to another. The coordinator's
`_get_upstream_url()` returns an absolute URL when a provider config is
present; only the no-config fallback returns bare paths.

### MiniMax Templates

- `minimax` — international host `https://api.minimax.io/anthropic` (default for `minimax.io` token-plan keys). Uses the Anthropic-compatible transport (`x-api-key` header, `anthropic-version: 2023-06-01` static header). Live model discovery via documented `/v1/models` endpoint; static seeds serve as fallback.
- `minimax-cn` — China host `https://api.minimaxi.com/v1`. Plain OpenAI-compatible. Live verification is required before production use because the China endpoint family has not been confirmed against the Anthropic-compatible transport.

API keys must be raw tokens; EggPool prepends the configured auth scheme automatically. An optional `[providers.<id>.verify]` block controls live verification probes.

## Dashboard

### Page Architecture

- Server-rendered HTML pages in `src/eggpool/dashboard/render.py`, all using the existing `_render_layout(title, body, active_nav, period, refresh_interval_s, theme_css, available_themes, current_theme, auto_refresh, include_chart_js)` wrapper — no Jinja, no template engine
- Routes registered through `register_dashboard_routes(app, require_auth=...)` in `src/eggpool/dashboard/routes.py`; the `require_auth` flag is computed from `config.dashboard.public` once at startup and shared across every dashboard page
- Backend handlers fan out independent `StatsService` calls through `asyncio.gather` so page loads are bounded by the slowest query, not the sum of sequential round trips (the shared connection lock serializes per-query execution regardless)
- Frontend helpers live in `src/eggpool/dashboard/static/dashboard.js` under the `window.EggPoolDashboard` namespace (`fetchStats`, `formatDurationMs`, `formatAgeSeconds`, `formatPercent`, `formatCount`, `formatBytes`, `formatMicrodollars`, `formatTokens`, `formatDollarsFromMicro`, `initGroupedTimeseriesCharts`, `reinitTimeseriesChart`) — small, opt-in, no framework
- Chart.js v4 (MIT, bundled) is served at `/static/chart.js` with `Cache-Control: public, max-age=86400`; pages opt in via `include_chart_js=True` in `_render_layout`. When `include_chart_js=True`, the layout also loads `/static/dashboard.js` (the chart lifecycle helpers) in document order, both via `defer`
- Static assets (CSS, JS, favicon) are served via `app.py` handlers with appropriate `Cache-Control` headers
- Every free-text field on every page goes through `escape()` or `escape_attr()` from `src/eggpool/dashboard/escape.py`; never interpolate raw upstream or model data
- Format helpers in `escape.py` (`format_duration_ms`, `format_age_seconds`, `format_percent100`, `format_percent01`, `format_int`, `format_count_or_dash`, `short_id`) are shared by every renderer; do not redefine per-page
- **Account breakdown toggle**: the overview's Account breakdown panel header carries a panel-header chip (`_account_count_chip()`) showing "X enabled · Y disabled" and an anchor-based toggle (`_render_account_breakdown_filter()`) that reads "Show N disabled" / "Hide disabled". The toggle is a plain `<a href>` (no form, no JS), so it works without JavaScript and survives bookmarking. `aria-pressed` reflects the current state for screen readers. Disabled count is defensively coerced via `_coerce_int()` so the page never blows up on bad input. `/accounts` keeps its own dropdown filter (`_render_account_filters()`) — that page pairs a period selector with the show-disabled toggle

### Chart lifecycle

The previous inline `<script>` chart pattern ran before Chart.js had loaded (because `<script defer>` on Chart.js hadn't executed yet) and broke on overview auto-refresh because `innerHTML` replacement does not re-execute scripts. The current contract:

- Page renderers never emit inline JavaScript that calls `new Chart(...)`. They emit a `<canvas class="grouped-timeseries-chart">` plus a sibling `<script type="application/json" class="grouped-timeseries-data">` data island
- `window.EggPoolDashboard.initGroupedTimeseriesCharts()` (called from `DOMContentLoaded` and after every auto-refresh swap) reads each data island, destroys any prior `canvas.__eggpoolChart` instance, builds Chart.js datasets, and renders
- `window.EggPoolDashboard.reinitTimeseriesChart()` rebuilds the legacy overview line chart by refetching `/api/timeseries` and rebinding the canvas after the auto-refresh `innerHTML` swap. The legacy overview line chart keeps its 60s in-page refresh interval for in-place updates
- `_render_auto_refresh_script()` invokes both helpers after `content.innerHTML = next.innerHTML;` so overview auto-refresh preserves both the legacy line chart and any grouped chart
- Pages that do not render a chart must not include `/static/chart.js` or `/static/dashboard.js` (`include_chart_js=False`)

### Grouped Timeseries

The `/timeseries` page replaces the old "table of bucket counts" with a stacked-bar grouped chart plus a grouped detail table:

- **Data contract**: `/api/timeseries/grouped` returns `{bucket, group_by, metric, limit, series, buckets, bucket_totals, points}`. Each `point` is one `(bucket, series_key)` row carrying per-bucket counters (request_count, error_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens, cost_microdollars, bytes_received, bytes_emitted, avg_latency_ms, avg_ttft_ms). Each `bucket_totals` entry aggregates across all series (including the folded `Other` bucket) so totals stay loss-less
- **Group dimensions**: `provider_model` (default — `provider_id:model_id` key, `provider_id / model_id` label), `provider`, `model` (uses `COALESCE(original_model_id, model_id)` so deprecated-model relinking still appears under the original id), `account`
- **Top-N + Other**: rows outside the top-N (`limit`, clamped 1..25) fold into a single `__other__` series with `is_other: true`. The fold is Python-side after the raw grouped SQL query (the SQL group-by keys are stable; folding by a Python `set` of top keys is easier to test and avoids SQL alias fragility). `bucket_totals` includes the `Other` rows so the bucket total stays equal to the sum of all points
- **Metric dimension**: in the first pass, the backend always ranks series by `request_count`. The frontend hides latency/TTFT from the metric dropdown and renders them in tooltips / detail rows only (averages are not additive and would mislead stacked bars). The `metric` field is preserved in the response for API stability
- **Caching**: `StatsService.get_grouped_timeseries(...)` participates in the 30s dashboard cache (TTL + 32-entry cap, same as `get_bandwidth_timeseries`). Cache key incorporates `bucket`, `group_by`, `limit`, `account_name`, `model_id`
- **Validation**: invalid `bucket` → `"hour"`, invalid `group_by` → `"provider_model"`, `limit` → `1..25`. Unknown `account_name` returns the empty stable payload (not `None`, not an exception) so the renderer never has to special-case it
- **Renderer**: `_render_grouped_timeseries_chart()` emits the canvas + JSON data island + `data-*` attributes; `_render_grouped_timeseries_table()` emits the 17-column grouped detail table (Account column conditionally shown); `_render_timeseries_controls()` emits the period/bucket/group_by/metric/limit/account/model form
- **Legacy compatibility**: `/api/timeseries` (the old aggregate endpoint) and the overview's `_render_timeseries_chart()` (line chart) remain unchanged for backward compatibility. The overview auto-refresh now correctly re-binds the legacy line chart via `reinitTimeseriesChart()` after every `innerHTML` swap
- **Empty state**: empty database / filtered-out time window renders `<p class="empty">No requests in this window.</p>` instead of a chart, without JavaScript errors

### Tooltip System

- Pure CSS only — declared at the bottom of `src/eggpool/dashboard/static/dashboard.css`. No JavaScript listeners, no per-site CSS, no new dependencies
- Generalizable `[data-tooltip]` rule at `src/eggpool/dashboard/static/dashboard.css:396`: any element with the attribute renders a themed bubble using existing CSS custom properties (`--card-bg`, `--card-border`, `--page-text`); new tooltip sites need no additional CSS
- `aria-label` is set on every tooltip target so screen readers announce the same text sighted users see
- Every interpolated value inside `data-tooltip="..."` and `aria-label="..."` is HTML-escaped via `_html_escape(..., quote=True)` — never interpolate raw upstream or model data
- Overview auto-refresh swaps regions via `innerHTML` every 15-60s; CSS-only tooltips survive because no JS listeners exist
- Reduced-motion friendly via `@media (prefers-reduced-motion: reduce)` at `src/eggpool/dashboard/static/dashboard.css:462` (transition: none)
- Optional `[data-tooltip-pos="bottom"]` modifier (`src/eggpool/dashboard/static/dashboard.css:450`) flips the bubble below the element — not used in the first pass
- Heatmap cells in `_render_bandwidth_heatmap()` at `src/eggpool/dashboard/render.py:615` still render the SVG `<rect>` grid with a `<title>` element (native fallback + the `tests/unit/test_dashboard.py:test_renders_tooltip` contract) but the rects carry `pointer-events="none"` via `.heatmap rect { pointer-events: none; }` at `src/eggpool/dashboard/static/dashboard.css:370` so hover never reaches the SVG title
- A sibling `<div class="heatmap-overlay">` (`src/eggpool/dashboard/render.py:789`, styled at `src/eggpool/dashboard/static/dashboard.css:375`) mirrors the cell grid as transparent hitboxes with `data-tooltip` and `aria-label` (date + metrics + request count). Cell color stays in the SVG `<rect>`; the overlay is `background: transparent`
- `_format_tooltip_date()` at `src/eggpool/dashboard/render.py:83` reformats `YYYY-MM-DD` into `Wed, Mar 5 2026`
- `_status_badge_tooltip()` at `src/eggpool/dashboard/render.py:61` maps status badge names (`cooldown_active`, `auth_failed`, `rate_limited`, `quota_exhausted`, `circuit_open`, ...) to human descriptions; status badges in event tables carry `data-tooltip` from the same mapping
- Topbar opt-ins: theme selector (`Switch dashboard theme`), period selector (`Select time range`), refresh `↻` button (`Reload this page`)

### Models Page Merge and Detail Links

- `_merge_models_with_catalog()` (`src/eggpool/dashboard/routes.py`) honors `[models].collapse_models`:
  - `collapse_models=false` dedupes by `(model_id, provider_id)` so an unused sibling provider for a used base model is **not** suppressed by an active sibling's stats row; the catalog-complete view shows every provider-scope row.
  - `collapse_models=true` dedupes by `model_id` only so the collapsed view stays one row per base model.
  - `_model_row_key()` is the single source of truth for the key computation; legacy stats rows that omit `provider_id` fall back to `catalog_by_id[model_id]` for diagnostic fields but do **not** suppress provider-scoped catalog rows.
- `render_models()` percent-encodes the detail-link path segment via `urllib.parse.quote(safe="")` (in `src/eggpool/dashboard/render.py`) so model ids containing provider suffixes (`/`) or query/HTML metacharacters (`?`, `#`, `<`, `>`, `"`) round-trip cleanly through `/models/{model_id:path}` + the detail handler's `unquote()`. The stats-row merge preserves the literal model id; only the emitted `<a href>` is percent-encoded.
- The dashboard's `?info_status=` filter accepts both canonical status names (`sparse_new`, `conflicting`, `source_unavailable`, `manual_override`) and the display aliases the compact `/api/model-info` summary exposes (`sparse`, `conflict`, `source-unavailable`, `manual`). `_normalize_info_status_filter()` in `src/eggpool/dashboard/routes.py` owns the mapping; new statuses only need to be added there and in `_STATUS_ALIASES`.

## Update Checker

- `UpdateChecker` is the single source of truth for "is there a newer eggpool release available?"
- Background task registered via `TaskSupervisor` under the exact name `update_checker`
- `_register_update_checker()` helper in `app.py` creates the checker, stores it on `app.state.update_checker`, and registers it with the supervisor
- Default check interval is 24h; PyPI request timeout is 15s
- `UpdateInfo` is a frozen dataclass that holds the snapshot; `snapshot()` returns an isolated copy via `dataclasses.replace` so callers cannot mutate the cached state
- `async_check_for_update()` is the shared one-shot helper used by both the background periodic task and the `eggpool update` CLI — both paths MUST go through this helper instead of inlining their own PyPI lookup
- `GET /api/stats/update` returns the JSON snapshot; always auth-gated regardless of `dashboard.public`
- Dashboard footer renders the update indicator only when `update_available=True`; renders nothing otherwise
- PyPI failures are non-fatal and reflected in `last_check_error`; the checker preserves the previous `latest_version` on failure so the indicator still surfaces a known-newer release during momentary outages
- The checker never auto-installs; it is passive notification only

## Model Capabilities

- Protocol-neutral capability schema in `src/eggpool/catalog/capabilities.py`
- `ThinkingCapability` — structured thinking/reasoning model with status, source, native protocols, per-protocol client controls, and budget bounds
- `ModelCapabilities` — top-level container; extensible to future capability families (vision, tools, structured outputs, prompt caching, logprobs)
- `CapabilityStatus` = `"supported" | "unsupported" | "unknown" | "mixed" | "conflicting"` — `"unknown"` means no data observed, not `"unsupported"`
- Merge order: defaults → provider catalog/model-info → global overrides → provider-scoped overrides; manual overrides win
- Aggregate semantics: `"supported"` only if all providers supported; `"mixed"` when states vary; `"conflicting"` when explicit metadata disagrees
- `serialize_model_capabilities()` produces compact dict for `/v1/models` under `eggpool.capabilities`; includes per-protocol client control field mappings (`openai_request_fields`, etc.) and per-provider status for collapsed entries
- `client_requests_thinking()` heuristic detects thinking-related keys in request body; `has_thinking_support()` checks status
- Protocol compatibility alone does not imply thinking support — the schema captures this explicitly
- See `docs/thinking.md` for the full operator guide (enabling, configuration, overrides, routing policy, budget mapping, troubleshooting)

### Model-Info Capability Enrichment

- `build_canonical_detail()` merges thinking capability metadata from provider catalogs and external model-info sources into the canonical detail's `capabilities.thinking` block
- Merge priority: provider_catalog (authoritative) > external model-info (advisory) > global config override > provider-scoped config override
- Provider catalog data always outranks external source data; when two external sources disagree, the status is set to `"conflicting"`
- Only explicit API-control evidence (e.g. OpenRouter `supported_parameters` listing "reasoning"/"thinking") produces `status = "supported"`; vague "reasoning model" descriptions stay `unknown`
- `_propagate_enriched_capabilities()` writes enriched thinking capability back to the catalog cache so `_copy_exposed_model` picks it up before config overrides are applied

### Config Overrides

`ThinkingCapabilityOverrideConfig` in `src/eggpool/models/config.py` provides operator-controlled model capability overrides that persist through catalog refresh cycles.

**Global overrides** — apply to all providers:
```toml
[model_capabilities."minimax-m3".thinking]
status = "supported"
native_protocols = ["anthropic"]
budget_tokens_min = 1024
budget_tokens_max = 16384
```

**Provider-scoped overrides** — take precedence over global:
```toml
[providers.minimax.model_capabilities."minimax-m3".thinking]
status = "supported"
native_protocols = ["anthropic"]
```

**Override fields**: `status`, `source` (defaults to `"manual_override"`), `native_protocols`, `budget_tokens_min`, `budget_tokens_max`, `effort_to_budget_tokens`, `notes`.

**Validation rules**:
- `status = None` makes the override a no-op (all other fields silently cleared)
- `budget_tokens_min` and `budget_tokens_max` must be > 0
- `budget_tokens_min` must not exceed `budget_tokens_max`
- `effort_to_budget_tokens` values must be > 0
- `native_protocols` must be one of `"openai"` or `"anthropic"`
- Extra fields are forbidden

**Precedence**: defaults → discovered (provider catalog / model-info) → global overrides → provider-scoped overrides. The `source` field tracks provenance; `"manual_override"` marks operator-supplied values. Capabilities are serialized in `/v1/models` responses under the `eggpool.capabilities` extension field.

### Capability-Aware Routing

- `classify_thinking_request()` in `src/eggpool/catalog/capabilities.py` inspects the request body for OpenAI `reasoning_effort` / `reasoning` and Anthropic `thinking` / `thinking_budget` indicators plus assistant history `reasoning_content` blocks
- Returns a `ThinkingRequestRequirement` dataclass (`required`, `client_protocol`, `fields`, `requested_effort`, `requested_budget_tokens`)
- Threaded through `get_eligible_accounts()` → `Router._selection_candidates()` → `select_account()` / `select_accounts_for_failover()`
- Each candidate's thinking capability status is checked via `check_candidate_thinking_eligibility()` against `[transcoder.capability_policy]` settings
- `CapabilityPolicy` (in `src/eggpool/transcoder/policy.py`) controls three policy axes: `unsupported_thinking` (`reject` | `warn_drop` | `route_best_effort`), `unknown_thinking` (`reject` | `allow_with_warning` | `route_best_effort`), `mixed_collapsed_thinking` (`filter` | `reject` | `allow`)
- Default policy is `reject` for all — a client explicitly asking for thinking gets either a compatible upstream or a clear `CapabilityError` (HTTP 400)
- `mixed_collapsed_thinking = "filter"` silently drops providers without thinking support when a model is served by multiple providers; if no supported providers remain, the original unfiltered list is returned (falling through to standard rejection). `"allow"` keeps all providers; `"reject"` rejects the entire request
- `conflicting` status is always rejected. An operator resolves conflicts via manual overrides (`[model_capabilities."<model>".thinking]`), which are merged before the eligibility check runs — the merged status already reflects the resolution
- Requests without thinking controls route exactly as before (no capability check)
- `CapabilityError` is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)
- The `explain_account_eligibility()` diagnostic includes a `thinking_support` gate and `thinking_unsupported` / `thinking_unknown` / `thinking_conflicting` reason codes

## Model Context Limits

- `ModelLimitOverrideConfig` provides reusable limit fields (context, input, output, enforcement)
- Global overrides via `[model_overrides.<model-id>]`, provider overrides via `[providers.<id>.model_overrides.<model-id>]`
- `ModelLimitResolver` resolves per-field with precedence: provider > global > upstream > unknown
- `conservative_limits()` merges provider limits for unsuffixed model exposure (minimum across providers)
- `eggpool configsetup opencode --json-only` generates OpenCode config with explicit model limits
- Models with `capabilities.thinking.status = "supported"` receive a `"thinking": "supported"` annotation; all other statuses (`unknown`, `unsupported`, `mixed`, `conflicting`) are omitted so the config never claims thinking support without confirmed upstream backing
- Effective limits are configuration-derived; no database migration needed for static overrides

## In-Memory Bounds

Long-running deployments — especially Raspberry Pi / SBC nodes — must keep steady-state RSS bounded by workload throughput, not workload cardinality. Every growth axis in the hot path is capped by a hardcoded module constant or a per-catalog config knob; see `plans/memory.md` for the full design and the regression test (`tests/integration/test_memory.py`, marked `pytest.mark.slow`).

| Structure | File | Cap | Eviction |
|-----------|------|-----|----------|
| `QuotaEstimator.account_model_ewma` | `src/eggpool/quota/estimation.py:285` | `EWMA_HARD_CAP = 4096` (hardcoded) | LRU; on miss recomputes from persisted `QuotaWindow` |
| `QuotaEstimator.global_model_ewma` | `src/eggpool/quota/estimation.py:286` | `GLOBAL_EWMA_HARD_CAP = 1024` (hardcoded) | LRU |
| `CatalogResolverPipeline.TTLCache._data` | `src/eggpool/catalog/catalog_resolvers.py:128` | `max_entries = 4096` per `[pricing.catalogs.<name>]` (configurable) | LRU on store; `entry.raw` stripped after parse |
| `ModelCatalogCache._account_support` | `src/eggpool/catalog/cache.py:114, 639` | `frozenset[str]` (no per-call `.copy()`); bounded by registered account × model cardinality | — |
| `ModelCatalogCache._models` / `_provider_models` | `src/eggpool/catalog/cache.py:109-111` | De-duplicated (per-provider override only when it differs from global) | — |
| `OutboundClientManager._per_host_*` | `src/eggpool/providers/outbound.py:85` | `MAX_TRACKED_HOSTS = 256` (hardcoded) | Coldest-total eviction; `evictions_total` in `snapshot()` |
| `AccountRuntimeState.model_availability` | `src/eggpool/accounts/state.py` | Pruned at every `AccountRegistry.sync_accounts` | — |
| `HealthManager.AccountHealth.disabled_models` | `src/eggpool/health/health_manager.py:111` | Pruned by `health_disabled_models_prune` task (60s cycle) | — |

## Pricing Resolution

- Resolution order: TOML override (`[pricing] model_overrides`) → upstream `/v1/models` metadata → external catalog (OpenRouter, OpenCode Zen) via the alias registry. Implemented in `src/eggpool/catalog/pricing_resolver.py` as `resolve_pricing_from_metadata()` and the `CatalogResolverPipeline` in `src/eggpool/catalog/catalog_resolvers.py`
- **`ResolvedPricing` provenance**: every resolution records `source` (`config` / `upstream` / `mixed`), `source_detail` (`operator_override` / `provider_metadata` / `openrouter` / `opencode_zen`), `source_confidence` (`exact_external_id` / `curated_alias` / `provider_metadata`), `source_model_id` (external catalog model ID), `source_provider_id` (catalog name)
- **Cost exactness values** stored on every finalized request: `provider_reported` (upstream supplied an authoritative `usage.cost`/`usage.cost_usd`/`usage.cost_microdollars`/`usage.billing.cost_usd` value via `eggpool.proxy.cost_reporting.extract_provider_reported_cost`), `exact` (catalog has rates for every nonzero billable category), `derived` (every nonzero billable category priced from a trusted rate), `partial` (at least one nonzero category filled by per-category fallback so the cost stays positive), `estimated` (no trusted rates; the local heuristic priced the request), `unknown` (no token usage). `provider_reported` is the most-trusted tier — it precedes every other category in the cost precedence ladder
- **Per-category fallback** in `CostCalculator._fallback_microdollars_for_category()` fills missing cache rates with conservative constants ($0.30/1M cache read, $3.75/1M cache write) so partial rows over-report rather than under-report. Opt out via `[pricing] fallback = "off"`
- **Alias registry** (`src/eggpool/catalog/pricing_aliases.py`): maps upstream model IDs (`mimo-v2.5`) onto external catalog IDs (`xiaomi/mimo-v2.5`) with a `confidence` enum (`exact` / `curated_alias` / `ambiguous_skip`). Seeded idempotently at startup via `seed_default_aliases()` and consulted by `CatalogResolverPipeline` before fetching
- **External catalog configuration** lives under `[pricing.catalogs.<name>]` in `config.toml`: `enabled`, `priority`, `ttl_seconds`, `base_url`, `api_key`, `options`. Built-in implementations: `openrouter` (enabled by default), `opencode_zen` (disabled by default)
- **`eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`** is the operator escape hatch after upgrading the resolver. Implemented in `src/eggpool/cost_recompute.py`; reuses the live `CostCalculator` so the new values match what the finalizer would write today. Default `--dry-run` reports deltas only
- **Dashboard wiring**: per-row cost-exactness badge (`<span class="exactness-badge derived|partial-mix|est-major">u:N,e:N,d:N,p:N,~:N,?:N</span>` where `u` = provider-reported count) on the Accounts and Models tables; high-spend estimated warning banner on the Accounts page when any row exceeds $10 in estimated cost. Overview Total-cost card surfaces the provider-reported count when present. Migration `0033` adds `provider_cost_microdollars`, `provider_cost_source`, `local_cost_microdollars`, `local_cost_exactness` to the `requests` table; `RequestRepository.update_after_completion` and `update_streaming_final` accept those audit columns alongside `cost_microdollars` so the canonical cost remains stable

## Health and Failure Classification

- Health systems use a normalized `FailureCategory` vocabulary shared by `HealthManager` and `AccountRuntimeState`
- `models.resolution_status` is set to `'resolved'` for all persisted models with resolved protocols
- **`BackoffPolicy` (in `health/backoff.py`)** maps each `FailureCategory` to a bounded exponential schedule (base, multiplier, cap, jitter, scope). Authentication failure is terminal — handled via `disable_account`. Context-limit failures produce no backoff. Rate-limit and quota-exhausted reasons honor upstream `Retry-After` when present.
- **`account_backoffs` table** persists upstream-derived backoffs across restarts. `AccountBackoffRepository` exposes upsert, clear-on-success, list_active, and expire_old. `HealthManager` state is rehydrated from this table at startup (best-effort, never blocks boot).
- **Successful requests clear transient backoff** for the relevant `(account_id, model_id, reason)` scope via `AccountBackoffRepository.clear_success`. Local cost overruns are never persisted as backoff rows.
- **Error classification (`retry/classification.py`)**: 408→TRANSIENT, 409/422→BAD_REQUEST (do not blindly suppress accounts), 429/402→QUOTA_EXCEEDED, 5xx→TEMPORARY/TRANSIENT. Provider error bodies are inspected for quota/rate-limit terms when status codes are ambiguous, with a denylist for false positives like "too many requests in queue".
- **`UpstreamExhaustedError` vs `ModelUnavailableError`**: 503 is reserved for genuine pre-dispatch unavailability (no enabled accounts, missing credentials, all explicitly disabled, model unknown). 502 (`UpstreamExhaustedError`) is raised when every candidate account was attempted and exhausted mid-request.
- **`/api/backoffs` endpoint** exposes active backoff rows from `AccountBackoffRepository.list_active(now)` for operator visibility during incidents.

## Error Hierarchy

- `AggregatorError` — base for all aggregator errors
- `ConfigError` — invalid or missing configuration
- `DatabaseError` — database-related failures
- `UpstreamError` — base for upstream API errors (`status_code` attribute)
  - `TemporaryUpstreamError` — temporary upstream errors (502, 503, 504)
  - `TransientUpstreamError` — transient upstream errors (retries may succeed)
  - `AuthenticationError` — upstream rejects credentials
  - `QuotaExhaustedError` — upstream account quota exhausted
  - `RateLimitError` — upstream rate-limited (`retry_after` attribute)
  - `ModelUnavailableError` — model not available upstream
- `ProxyError` — general proxy/transport errors
- `ModelNotFoundError` — requested model does not exist (`model_id` attribute)
- `NoEligibleAccountError` — no account can serve the request (503)
- `CatalogUnavailableError` — model catalog not available (503)
- `AuthenticationUnavailableError` — upstream credentials cannot be loaded (503)
- `UpstreamExhaustedError` — all upstream attempts exhausted (502)
- `AccountSuspendedError` — account suspended (503)
- `RequestTooLargeError` — request body exceeds configured limit
- `ContextLimitExceededError` — estimated request context exceeds configured model limit
- Chain exceptions with `raise ... from err` or `raise ... from None`

## Security

- Local client credentials (`Authorization`, `X-Api-Key`, `Proxy-Authorization`) are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- Persisted `error_detail` is fail-closed by default; the strengthened redactor (regex + JSON sanitization) only runs when `security.persist_redacted_error_detail = true`
- Optional persisted `error_detail` uses a strict diagnostic allowlist (`SAFE_JSON_KEYS`); arbitrary provider payload keys are dropped
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification

## Deployment

- The systemd unit intentionally omits `ExecReload`; all configuration changes require `sudo systemctl restart eggpool`
- The `scripts/check_database.py` checker opens the database read-only via `file:...?mode=ro` and refuses to mutate anything
- The `scripts/check_database.py` checker is fail-closed: it treats missing `_migrations`, empty `_migrations`, missing required tables/columns, and query errors as exit code 2 (configuration/schema error), not zero violations
- The `scripts/smoke_test.py` stream diagnostics use a rolling tail buffer to recognize SSE markers split across arbitrary transport chunks
- `scripts/verify_upstream_auth.py` is operator-only: it bypasses EggPool to confirm the configured key works directly upstream
- Pyright in CI covers `src/` AND `scripts/`; narrow type annotations with `cast` or `Any` rather than excluding a file

## CLI Commands

- `models refresh` synchronizes configured accounts via `AccountRepository.sync_from_config` before refreshing the catalog, so cached account/model relationships match normal application startup
- The CLI has a two-tier entry point: `eggpool.cli:main` is a tiny bootstrap that dispatches `croncheck` and `ensure-running` through the stdlib-only `eggpool.fastcli` fast path, then falls through to the heavy Click CLI in `eggpool.cli_full` for everything else. See **Fast-Path CLI** above
