# Architecture

High-level design overview for the EggPool aggregator.

## Package Structure

```
src/eggpool/
├── accounts/          # Account registry and runtime state
├── api/               # API endpoint handlers (chat completions, messages, stats)
├── background/        # TaskSupervisor, retention cleanup, periodic tasks
├── catalog/           # Model catalog, pricing, protocols, fetcher, normalizer, limits
├── control/           # Control plane (Unix socket, live reload)
├── model_info/        # Model information sidecar: persistent metadata, observations, summaries, source adapters
├── dashboard/         # Self-updating server-rendered HTML dashboard
├── db/                # SQLite connection, migrations, repositories, schema
├── health/            # Circuit breaker and health tracking
├── integrations/      # External tool configuration generation (OpenCode, Claude Code, Aider, Codex, Qwen Code, Kilo, Continue, Cline, Roo Code, Goose, OpenHands)
├── lifecycle/         # Backup and uninstall orchestration
├── metrics/           # Metrics buffering and thinking observability
├── models/            # Pydantic config, domain, API, and database models
├── observability/     # Routing trace writer
├── providers/         # ProviderClientPool, pproxy transport, connect CLI
├── proxy/             # Transparent proxy, shared SSE framing, observer, usage
├── transcoder/        # Protocol transcoding (OpenAI ↔ Anthropic, body + streaming)
├── quota/             # Quota estimation, reservations, scoring
├── request/           # RequestCoordinator, finalizers, body reader, limit enforcement
├── retry/             # Error classification and failover
├── routing/           # Quota-aware routing, eligibility, provider parsing
├── security/          # Header redaction, security utilities
├── stats/             # Statistics queries and service
├── deploy/            # Bundled systemd/logrotate/cron snippets for CLI output
├── _share/            # Bundled config examples and assets for pipx installs
├── auth.py            # Local API key authentication (constant-time)
├── cli.py             # CLI bootstrap entry point (tiny, dispatches fast-path then Click)
├── cli_full.py        # Click CLI commands (heavy imports)
├── config.py          # Config file helpers
├── config_reload_policy.py # Typed configuration diff, reload policy, ReloadResult, ReloadStage
├── reload_diagnostics.py  # Phase 11: canonical reload result categories, counters, finalization
├── config_utils.py    # Configuration utility functions for CLI and integrations
├── config_validation.py # Reusable validation contract used by check-config and rehash
├── constants.py       # Project-wide constants
├── cost_recompute.py  # Cost recompute CLI command
├── deploy_user.py     # Deploy user and path resolution
├── errors.py          # Exception hierarchy
├── fastcli.py         # Fast-path CLI (stdlib-only, croncheck/ensure-running)
├── logging.py         # Structured logging setup
├── onboard.py         # Interactive onboarding script
├── runtime.py         # Process management (restart, stop, PID lifecycle)
├── runtime_dispatch.py # Bounded rolling-window recorder for EggPool-local upstream dispatch overhead
├── runtime_metrics.py # Runtime/ops metrics: process, memory, DB, background tasks, OS load average
├── runtime_paths.py   # PID file and log path resolution (stdlib-only)
├── toml_edit.py       # Small, formatting-preserving edits for scalar TOML section values
└── update_checker.py  # PyPI update checker (background + freshness-aware CLI)
```

`integrations/common.py` owns configsetup context construction, catalog-backed
default model resolution, and format-safe scalar/key rendering helpers. New
agent targets should reuse those helpers instead of hand-quoting JSON, TOML,
YAML, shell, or model ID values in target modules.

## Request Lifecycle

All data-plane requests flow through `RequestCoordinator`:

1. **Endpoint** (`api/chat_completions.py` or `api/messages.py`) extracts model ID, parses provider suffix
2. **Routing** selects an eligible account via quota-aware scoring (`routing/router.py`)
3. **Attempt** is persisted to SQLite before upstream dispatch
4. **Provider Contract** renders absolute URL (`compose_provider_url()`) and auth headers (`build_upstream_headers()`) from `providers/contract.py`
5. **Protocol Transcoding** (if enabled) translates the request body when the client protocol differs from the upstream protocol
6. **Proxy** sends the request via the provider's `httpx.AsyncClient` from `ProviderClientPool`
6. **Streaming** is handled by one `proxy/sse.py` decoder; the coordinator fans shared frames to `proxy/sse_observer.py` and the selected frame-level transcoder
7. **Finalization** records usage, releases reservations, updates health state

All outbound dispatch paths (non-streaming chat, streaming chat, catalog refresh) share the same `compose_provider_url()` rules so a provider cannot list models at one host and dispatch requests to another. The coordinator's `_get_upstream_url()` returns an absolute URL for provider-configured paths, falling back to bare paths only when no provider config is loaded.

Key invariants:
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- **Protocol-aware streaming EOF**: `SSEDecoder` (`proxy/sse.py`) owns bounded UTF-8/SSE framing and emits shared `DecodedSSEFrame` objects. `IncrementalSSEObserver` retains bounded completion evidence (`[DONE]` for OpenAI, `message_stop` for Anthropic), and `classify_stream_eof()` maps clean exhaustion to canonical completion, provider-policy compatibility, empty, premature, or malformed EOF. The coordinator drains the shared decoder and classifies before `StreamingTranscoder.finish()`. A premature/malformed stream is finalized through the canonical `MIDSTREAM_ERROR` owner; it cannot clear success backoff or emit a synthetic downstream terminal marker. Provider policy is explicit on `ProviderConfig.stream_completion_policy` and defaults to `strict`.
- Every retryable failed attempt must reach terminal state, confirm its reservation is terminal, and release all owned runtime components before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- **Process-owned request finalization (Plans 026/047/055/056/057)**: every selected request-terminal outcome is owned by one retained, attempt-keyed `RequestFinalizationJob`; streaming work registers no placeholder before its terminal data exists. The retained task completes durable and in-memory cleanup independently of request waiters. Retryable failed attempts and post-commit claim compensation use separate coordinator-retained commands with per-component progress, a hard 128-entry capacity by default, explicit rejoin, and bounded generation-shutdown drain. A retained command may return to its caller only after its progress record proves durable attempt transition, terminal reservation status, and each owned runtime release; a completed child task alone is not a convergence proof. A cancelled waiter submits one canonical `CLIENT_CANCELLED` request terminal only after the selected ownership converges, using an explicit last-converged attempt identity between retries. Duplicate submissions join, conflicting terminal payloads are diagnosed, and the bounded `RequestFinalizationSupervisor` provides diagnostics, startup reconciliation, and shutdown drain. The stale-request finalizer is a recovery safety net rather than a normal terminal path.
- **Stale runtime accounting**: the bounded stale sweep reconciles only rows transitioned by that pass, releases one active unit per accepted request with one aggregate router update per account, and removes quota reservations by ownership rather than cost truthiness. Zero-cost request/token reservations are therefore released; bulk active-count underflow is logged and clamped to zero.
- The same URL composition rules apply to catalog fetch and chat dispatch
- **Structured observability persistence (migrations 0026-0029)** every `request_attempts` row carries provider/model/protocol/retry_category/latency/bytes/streamed/is_retry_outcome; every routing decision is persisted to `routing_decisions` in the same transaction as the `request_attempts` INSERT; safety-net tasks (`_crash_recovery`, `_finalize_stale_requests_once`, `reconcile_expired_reservations`) record `operational_events` rows inside the same transaction as the durable state mutation; latency is decomposed into `upstream_connect_ms / upstream_read_ms / coordinator_overhead_ms` so the dashboard can distinguish network vs upstream vs eggpool-side bottlenecks
- **Runtime metrics are best-effort and process-local** — the `/api/stats/runtime` endpoint and `eggpool runtime-status` CLI command gather process topology, memory, background task state, database health, OS load average (`os.getloadavg` + normalized per-core), and a bounded rolling-window dispatch-overhead distribution via `DispatchOverheadRecorder` (`src/eggpool/runtime_dispatch.py`); failed probes return `null` rather than raising, `probe_errors` is capped to 16 truncated entries, and the endpoint is always auth-gated even with a public dashboard
- **Dispatch timing boundaries (Milestone A4)** — two distinct timing slices measure EggPool-side latency before upstream dispatch. `DispatchOverheadRecorder` (`src/eggpool/runtime_dispatch.py`) covers the coordinator-internal slice: from `ProxyRequestContext.started_monotonic_ns` (after context_build) to just before `httpx.AsyncClient.send()`. `LocalPreUpstreamRecorder` (`src/eggpool/runtime_dispatch.py`) covers the full EggPool-side window: from `request_received_monotonic_ns` (ASGI handler entry) to just before upstream dispatch. `request_received_monotonic_ns` is captured at the top of `handle_proxy_request` (`src/eggpool/api/proxy_request.py`); `local_pre_upstream_ms` is exposed via `runtime_metrics.local_pre_upstream`. Both use monotonic/performance clocks. The two metrics are additive: `local_pre_upstream` includes context_build, body parsing, validation, segmentation, compression, and coordinator dispatch overhead; `dispatch_overhead` covers only the coordinator-internal selection/persistence/dispatch slice.

### Thinking/Reasoning Observability

Phase 10 adds structured observability for thinking/reasoning decisions:

- **In-memory counters** (`src/eggpool/metrics/thinking.py`): `ThinkingMetricsCounter` tracks decision outcomes with low-cardinality labels (protocol, decision, capability_status, provider_id). Counters: `requested`, `transcoded`, `dropped`, `rejected`, `unknown_capability`, `unsupported_capability`, `budget_clamped`, `stream_delta`, `response_block`.

- **Request trace**: each request that involves thinking carries a `thinking_trace` dict on `ProxyRequestContext` with fields: `requested`, `client_protocol`, `request_fields`, `requested_effort`, `resolved_budget_tokens`, `budget_clamped`, `capability_status`, `capability_source`, `upstream_protocol`, `upstream_fields`, `decision`. This is serialized to `thinking_trace_json` on the `requests` table (migration `0039`) and exposed in `/api/stats/recent/{id}`.

- **API surfaces**: `GET /api/stats/thinking` returns the counter snapshot. `/api/stats/runtime` includes `thinking_metrics` in the snapshot.

- **Dashboard**: overview page shows a Thinking/Reasoning stat card with total count and per-decision breakdown.

See `plans/thinking_reasoning_phase_10_observability.md` for the full design.

### Thinking/Reasoning Test Matrix

Phase 11 adds a comprehensive regression test matrix (`tests/unit/test_thinking_reasoning_matrix.py`) covering all thinking/reasoning subsystems:

1. **Config and capability schema** — default values, merge semantics, aggregate status computation, policy defaults
2. **/v1/models serialization** — provider-scoped models, collapsed models, client control field flattening, budget bounds
3. **Request classification** — OpenAI reasoning_effort, Anthropic thinking, assistant reasoning_content history
4. **Routing eligibility** — supported/unsupported/unknown provider filtering, mixed collapsed filter policy
5. **OpenAI-to-Anthropic request transcoding** — reasoning_effort→thinking budget, reasoning_content→thinking blocks, budget resolution
6. **Anthropic-to-OpenAI request transcoding** — thinking field handling, thinking content in history
7. **Non-streaming response translation** — thinking→reasoning_content, redacted thinking drops
8. **Streaming response translation** — thinking_delta→reasoning_delta, ordering preservation
9. **App/coordinator integration** — TranscoderPolicy injection, feature flag gating
10. **Observability** — counter increments, event dispatch, request trace metadata

See `plans/thinking_reasoning_phase_11_test_matrix.md` for the full plan.

### Thinking/Reasoning Closing Pass

The closing pass (`plans/thinking_reasoning_closing_pass.md`) hardens the thinking/reasoning subsystem against silent semantic gaps:

- **Phase A — Missing metadata == `unknown`.** `extract_thinking_status_from_entry()` is the canonical helper used by `get_eligible_accounts()` and `Router._collect_gate_status()`. Catalog entries with no `capabilities.thinking` block now participate in the `[transcoder.capability_policy].unknown_thinking` policy evaluation rather than silently being treated as `supported`.
- **Phase B — `BudgetResolutionError` is a `CapabilityError`.** Strict-policy rejections now flow through the existing 400 renderer without manual mapping.
- **Phase C — Per-provider budget recompute.** `RequestCoordinator._recompute_thinking_budget_for_selected_provider()` re-runs `resolve_thinking_budget()` against the selected provider's capability at dispatch, so provider-scoped overrides take effect at the right phase.
- **Phase D — Trace decisions and rejection attribution.** `classify_thinking_warning_decision()` collapses the loss-warning list into a single `decision` field (`passthrough` / `transcoded` / `dropped` / `clamped` / `rejected`). `_determine_thinking_rejection_status()` attributes routing rejections to `unknown` vs `unsupported`.
- **Phase E — Top-level `reasoning_content` detection.** `classify_thinking_request()` now detects top-level `reasoning_content` strings/lists on assistant messages.
- **Phase F — `supports_tools` removed.** Tool support is owned by transcoder features, not `ModelCapabilities`.
- **Phase G — Explicit Anthropic top-level thinking drop kind.** `anthropic_top_level_thinking_dropped` is now an explicit warning kind rather than the generic `dropped_field` bucket.
- **Phase H — Final provider budget cleanup.** The selected-provider recompute now parses `context.original_body` (not the already-translated `context.upstream_body`) so the resolver sees the original `reasoning_effort` / `thinking.budget_tokens` intent. The helper `_extract_original_thinking_budget_inputs()` returns `(effort, None)` for OpenAI clients and `(None, budget)` for Anthropic clients; for OpenAI this is what lets the selected provider's `effort_to_budget_tokens` mapping win over global/default mappings when collapsed model ids route to that provider. Post-selection strict rejections (e.g. provider-specific clamp) now flow through `_finalize_selected_capability_rejection()` which finalizes the attempt row (`release_reason = "capability_rejected"`), releases the reservation durably and in-memory, decrements the active request count, releases the health-manager probe slot, and stamps `thinking_trace.decision = "rejected"` — without recording an upstream health penalty. Streaming and non-streaming dispatch paths share the same cleanup via `_apply_selected_provider_transcode_adjustments()`.
- **Phase I — Final polish (`plans/thinking_reasoning_final_polish.md`).** `_recompute_thinking_budget_for_selected_provider()` populates `thinking_trace.upstream_fields = ["thinking"]` whenever the recompute actually writes/validates Anthropic `thinking.budget_tokens` and the trace carries an empty list (the default shape). A pre-populated non-empty list is preserved verbatim so future paths can stack additional upstream fields without being clobbered. The strict-rejection cleanup tests additionally pin `HealthManager.is_account_healthy()` and the underlying `AccountHealth` dataclass fields (`consecutive_failures`, `disabled_models`, `disabled_until`, `disabled_reason`, `cooldown_until`) so a capability rejection cannot silently record an upstream health penalty.

## Multi-Provider Architecture

EggPool supports 27+ upstream providers (OpenCode Go, OpenAI, Anthropic, Groq, DeepInfra, Gemini, xAI, Mistral, SiliconFlow, DeepSeek, Together, Fireworks, OpenRouter, Alibaba, MiniMax, and more), each with its own base URL, account pool, supported protocols, and model catalog. See `docs/providers.md` for the full roster.

### MiniMax templates

- **`minimax`** — international host `https://api.minimax.io/anthropic`. Anthropic-compatible transport (key sent as `x-api-key` plus `anthropic-version: 2023-06-01`). Model listing is exclusively live via `/v1/models`; no static seeds are shipped because the provider already accepts the anthropic value produced by the family mapping. The Anthropic model-list normalizer auto-detects MiniMax's hybrid response shape. Default for keys from `minimax.io`.
- **`minimax-cn`** — China host `https://api.minimaxi.com/v1` with the same OpenAI paths as a standard provider. Live verification is required because the China endpoint family has not been confirmed against EggPool's Anthropic-compatible transport.

The stored key must be the raw token; EggPool prepends the configured auth scheme automatically. An optional `[providers.<id>.verify]` block lets the verifier know which model to probe when neither `--openai-model` nor `--anthropic-model` is passed on the CLI.

### Provider Configuration

Providers are configured under `[providers.<id>]` in `config.toml`:

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[[providers.opencode-go.accounts]]
name = "personal"
api_key_env = "OPENCODE_GO_KEY_1"
```

Legacy flat `[[accounts]]` configs auto-normalize to a default `opencode-go` provider.

### Client Pool

`ProviderClientPool` (`providers/client_pool.py`) manages per-provider `httpx.AsyncClient` instances with independent connection pools, timeouts, and optional per-account proxy support.

### Model ID Format

Models are exposed with provider-suffixed IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`). `parse_model_provider()` in `routing/provider.py` is the canonical suffix parser; `catalog/cache.py` retains a compatibility alias.

### Provider-Specific Paths

Each provider can configure custom upstream paths:
- `openai_path` (default: `/chat/completions`)
- `anthropic_path` (default: `/messages`)
- `models_endpoint` — `[providers.<id>.models_endpoint]` table with `method`, `path`, `query`, `body`, `required`. Use `method = "DISABLED"` for providers that do not expose a live model listing (catalog is then populated from `static_models`).
- `models_method` / `models_path` — legacy scalar fields still accepted; auto-synthesized into a default `models_endpoint` table on parse.

### Provider Contracts

Each provider declares an explicit contract for authentication, URL composition, and model listing via `ProviderAuthConfig`, `ProviderStaticHeaderConfig`, `ProviderModelsEndpointConfig`, and `ProviderVerifyConfig` in `config.toml`.

`src/eggpool/providers/contract.py` centralizes:
- `compose_provider_url()` — absolute URL composition (rejects duplicate `/v1` prefix)
- `build_auth_headers()` — provider-aware auth header construction (`bearer`, `api_key`, `raw_authorization`, `none`)
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

The coordinator calls `_build_upstream_headers()` and `_get_upstream_url()` which use the provider contract when available, falling back to legacy Bearer auth and bare paths respectively.

#### Bearer-prefix guard

`AppConfig.validate_account_credentials()` rejects API keys that begin with the `Bearer` scheme for providers configured with `auth.mode = "bearer"`. EggPool adds the scheme automatically, so a stored `Bearer <token>` would produce `Authorization: Bearer Bearer <token>` upstream and cause 401s. The same guard runs in `scripts/verify_upstream_auth.py` so the operator gets an explicit error before any upstream call. Providers using `auth.mode = "raw_authorization"` are unaffected because they pass the value verbatim.

## Protocol Transcoding

When a client sends a request in one protocol (e.g., Anthropic Messages API)
but the routed provider only supports another (e.g., OpenAI Chat Completions
API), the `transcoder` module translates the request body before dispatch and
the response body (including streaming chunks) after receipt.

**Phase 1 (foundation)** lands the data model, configuration surface, and
helper modules without changing runtime behaviour:
- `TranscoderPolicy` config model (`[transcoder]` section)
- `TranscodeContext` per-request state dataclass
- `upstream_protocol` field on `ProxyRequestContext`
- Mechanical refactor: upstream-side reads in the coordinator use
  `context.upstream_protocol` instead of `context.protocol`
- Routing eligibility accepts a `transcode_eligibility` parameter
- Helper modules: `ids.py` (tool-call ID map), `usage.py` (usage
  canonicalisation), `errors.py` (upstream error envelope parser)
- **Runtime wiring**: `RequestCoordinator` receives `config.transcoder`
  via the `transcoder_policy` constructor parameter so that
  `self._transcoder_policy.features` gates per-feature transcoding
  during actual dispatch. `app.state.transcoder_policy` remains set
  for preflight helpers and diagnostics in `proxy_request.py`.

**Phase 2 — Body translation**: text-only, non-streaming request/response
body translation is implemented in `src/eggpool/transcoder/`. The
`BodyTranscoder` Protocol (`protocol.py`) defines the interface;
`OpenAIToAnthropic` and `AnthropicToOpenAI` are the concrete translators.
`select_transcoder()` is the single source of truth for dispatch. The
coordinator pre-translates the request body before dispatch, decodes the
response body on success, and re-renders non-retryable errors in the client
protocol. Loss-of-information warnings are accumulated on
`TranscodeContext.loss_warnings` and logged at request completion.

**Phase 3 — Streaming translation**: SSE stream translation in both
directions for text-only streams. `StreamingTranscoder` implementations
(`OpenAIToAnthropicStreaming`, `AnthropicToOpenAIStreaming`) translate
upstream SSE frames into client-format bytes chunk-by-chunk.
`select_streaming_transcoder()` in `streaming.py` is the dispatch source
of truth. The coordinator's `_build_stream_generator` applies the transcoder
when the client and upstream protocols differ. Same-protocol requests pass
through unchanged. Tool calls, thinking, and routing widening are out of
scope (phases 4–6).

**Phase 9 — Streaming hot-path optimisation (transcoded-stream-dispatch-fixes)**:
the streaming transcoder path is tuned for sustained concurrent coding-agent
streaming loads. The coordinator's bounded `SSEDecoder` is the single
observer — the streaming transcoder no longer runs its own observer for
usage extraction, so a single parse/observe pass per upstream chunk covers
both translation and finalization. The transcoder's `usage` property
returns a default `StreamUsageResult()`. `StreamingTranscoder.feed()` and
`.flush()` are synchronous (the per-chunk work performs no async I/O), so
the coordinator calls them without `await`. Translated output per upstream
chunk is coalesced via `b"".join(out_chunks)` rather than yielding each
translated frame separately, reducing ASGI send calls while preserving
wire ordering and the `[DONE]` / `message_stop` terminal events.
`upstream_include_usage` is computed once in `_execute_streaming` (after
any injection of OpenAI `stream_options.include_usage`) and threaded into
`_build_stream_generator` via an explicit parameter, removing the second
JSON parse in the generator. Frame helpers use compact JSON separators
`(",", ":")` to reduce output bytes and serialization work. The
microbenchmark (`tests/perf/test_streaming_transcoder_perf.py`) and the
E2E concurrency regression test
(`tests/integration/test_streaming_transcode_concurrency.py`) pin the
new behaviour; replay fixtures under `tests/fixtures/streaming_transcode/`
let tests assert on decoded SSE event sequences rather than raw bytes so
JSON whitespace changes do not break tests.

**Phase 10 — JSON backend migration (`eggpool.jsonx`)**: hot-path JSON
serialization and parsing live behind a small helper at
`src/eggpool/jsonx.py`. The preferred backend is `orjson`; install with
`uv pip install 'eggpool[fast]'` (or `uv sync --extra fast`) to enable
it. Without the `fast` extra the helper falls back to the stdlib
implementation with identical compact-separator wire behaviour.
Override at runtime with `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`. The
active backend is logged at startup (`json_backend=orjson|stdlib` in the
Granian profile line). Wire bodies (`encode_json_body()`), SSE frame
helpers (`_anthropic_frame`, `_openai_frame`), the streaming transcoder
parse helpers (`_safe_json`, `IncrementalSSEObserver._flush_event`),
tool-argument stringification, and the request-path body parses all
route through `eggpool.jsonx`. The `requests/messages` error envelopes
in `app.py` and `coordinator.py` also use the helper. Tests under
`tests/unit/test_jsonx.py` are parametrised across both backends so the
stdlib and orjson branches stay semantically equivalent. The plan that
introduced this layer is `plans/transcoded-json-backend-orjson.md`.

**Phase 4 — Routing eligibility widening**: transcoding is **on by default**. The routing layer widens the candidate set to include accounts whose `provider.protocols` includes the model's native protocol even if it does not include the client protocol. `_validate_endpoint` checks for transcodable routes before raising `ProtocolMismatchError`. The `_resolve_upstream_protocol` method determines which protocol to use upstream based on the largest eligible-account set. `prefer_native = true` (default) keeps native-protocol accounts ranked above transcodable ones via a secondary sort key in `QuotaFairScorer`. The two-pass context-limit check in `api/proxy_request.py` validates both client-side and upstream limits when transcoding is active. The `[transcoder] enabled = false` flag is a deprecated escape hatch that disables all translation and reverts to the pre-default protocol-exact routing.

**Phase 5 — Operator controls and docs**: the default `[transcoder]` config block is documented in `config.example.toml`. `eggpool stats transcoding` reports transcoded request counts and loss-warning summaries. The dashboard `/runtime` page includes a "Transcoding" card showing real-time counters. Structured DEBUG logs are emitted for every transcoded request and a startup line announces transcoding state. Loss warnings remain at INFO. See `docs/transcoding.md` for the full operator guide.

**Phase 6.1 — Tool-use transcoding**: bidirectional tool calling translation in both directions for non-streaming and streaming requests. `OpenAIToAnthropic.encode_request` / `decode_response` and `AnthropicToOpenAI.encode_request` / `decode_response` translate `tools`, `tool_choice`, `parallel_tool_calls`, assistant `tool_calls` history, `role: "tool"` history, and `tool_use` / `tool_result` content blocks. A per-request `ToolCallIdMap` (on `TranscodeContext.id_map`) mints `call_<24 hex>` and `toolu_<24 hex>` ids so the two namespaces never collide. The streaming transcoders (`OpenAIToAnthropicStreaming`, `AnthropicToOpenAIStreaming`) extend their state machines to track `content_block_start` / `input_json_delta` / `content_block_stop` triples and emit OpenAI `tool_calls` deltas in insertion order; the reverse direction buffers OpenAI `tool_calls[*].function.arguments` chunks and flushes Anthropic `tool_use` blocks on `finish_reason: "tool_calls"`. Anthropic's `pause_turn` `stop_reason` maps to `finish_reason: "tool_calls"` plus a synthetic `__eggpool_pause_turn__` tool_call entry so OpenAI clients can detect pause-and-resume flows. `stream_options.include_usage` is lifted onto `TranscodeContext.request_include_usage` so the streaming transcoder can decide whether to forward upstream usage chunks. New loss-warning kinds (`tool_call_id_translated`, `tool_call_id_changed`, `parallel_tool_calls_collapsed`, `malformed_tool_arguments`, `invalid_tool_choice`, `unsupported_tool_type`, `empty_tool_use_block`, `tool_result_image_dropped`, `tool_result_error_passthrough`, `cache_control_feature_disabled`, `cache_control_unsupported_by_target_protocol`, `cache_control_invalid_shape`, `provider_extension_not_preserved`, `stable_prefix_preserved`, `stable_prefix_reordered_canonically`, `pause_turn`, `non_text_content_dropped`, `tool_result_inferred`) are added to `LOSS_WARNING_KINDS`. See `docs/transcoding.md` § Tool-Use Transcoding and `plans/tooltranscoding.md` for the full design.

**Phase 7 — Budget resolution**: `resolve_thinking_budget()` in `src/eggpool/transcoder/budget_resolver.py` is the single source of truth for effort-to-budget translation. Resolution order: explicit `thinking.budget_tokens` (Anthropic style) → `reasoning_effort` (OpenAI style) via `ThinkingCapability.effort_to_budget_tokens` → `[transcoder.thinking_budget_defaults]` → hard-coded fallback (low=1024, medium=4096, high=16384). Budgets are clamped to `budget_tokens_min`/`budget_tokens_max` when known. `budget_resolution_policy = "strict"` rejects unknown efforts and clamped budgets before dispatch. New loss-warning kinds: `budget_clamped`, `unknown_effort`, `budget_rejected`, `budget_resolution_no_input`. The `BodyTranscoder.encode_request` protocol accepts optional `thinking_capability`, `budget_defaults`, and `budget_resolution_policy` kwargs.

**Provider-bound payload lifecycle (Plan 050)**: `ProviderBoundRequest` is created from the single client parse and remains authoritative through provider selection and dispatch. A valid `PreparedTranscode` supplies its immutable decoded provider payload and matching bytes; invalid preparation is recomputed into the same object. Thinking normalization, post-route synthetic cache synthesis, and OpenAI streaming `stream_options.include_usage` are ordered transforms over that object. Each structural mutation advances a generation and invalidates bytes; the final serializer caches one encode for the dispatched generation and freezes later mutation. `ProxyRequestContext.upstream_body` is a compatibility mirror only. See `src/eggpool/request/provider_bound_request.py`, `src/eggpool/request/transform_pipeline.py`, and `tests/unit/test_provider_bound_request.py`.

**Phase 8 — Response-field compatibility**: configurable OpenAI-compatible reasoning field names for both streaming and non-streaming responses. `[transcoder.openai_reasoning_fields]` controls `non_stream` (default `["reasoning_content"]`) and `stream_delta` (default `["reasoning"]`) field names. `emit_compat_aliases = false` (default) emits only the primary field; when true, additional aliases are emitted. Streaming thinking deltas are now feature-gated consistently with non-streaming paths — when `[transcoder.features].thinking = false`, streaming thinking deltas are dropped. The `build_reasoning_fields()` helper in `src/eggpool/transcoder/policy.py` builds the field dict from config. `AnthropicToOpenAIStreaming` and `OpenAIToAnthropic.decode_response` accept optional `reasoning_field_names` and `emit_compat_aliases` parameters forwarded from the coordinator via `TranscoderPolicy.openai_reasoning_fields`.

Token counts are mapped between protocol-specific fields (e.g.,
`input_tokens` → `prompt_tokens`, `cache_creation_input_tokens` →
separate cache counters). Controlled by `[transcoder]` config.

## Request Shaping Overview

EggPool’s cache-preserving request-shaping stack is easier to reason
about when grouped by operator surface instead of implementation phase:

- **Cache reporting** — provider-reported cache counters and coverage.
- **Request segmentation** — stable-prefix, semi-stable, and volatile
  suffix structure without payload mutation.
- **Native cache preservation** — how transcoding preserved or dropped
  provider-native cache annotations.
- **Compression opportunities** — observe-mode analysis only.
- **Safe compression** — deterministic volatile-suffix mutation with
  fail-closed stable-prefix verification.
- **Policy overrides** — scoped `[[compression.policies]]` overlays for
  client/protocol/model/provider rollouts.
- **Synthetic cache controls** — optional provider-bound cache
  annotations, disabled by default and dry-run first.
- **Advisory tuning** — bounded recommendation-only threshold guidance.
- **Routing guardrails** — hardcoded guarantee that none of the above
  request-shaping signals enter `QuotaFairScorer`.

The detailed phase sections below remain the implementation history and
schema/audit reference. For current operator behaviour, prioritize the
surface labels above and the `/cache` request-shaping summary;
`/runtime` only carries a compact relocation panel.

## Cache Token Observability (Phase 1)

Every finalized request is annotated with a `cache_counter_status` enum
(reported / not_reported / unknown_format) plus the parsed cache-token
counts the upstream actually surfaced. The observer layer in
`src/eggpool/proxy/normalized_usage.py` is provider-neutral:

- `normalize_usage(decoded_payload, protocol=...)` extracts `usage`
  blocks from decoded JSON and classifies whether the upstream reported
  cache counters. OpenAI providers are classified by the presence of
  `prompt_tokens_details.cached_tokens`; Anthropic providers by the
  presence of `cache_read_input_tokens` / `cache_creation_input_tokens`.
- `normalize_from_stream_result(result, protocol=...)` adapts a
  `StreamUsageResult` so the same enum applies to streaming responses.
- The finalizer (`src/eggpool/request/finalizer.py`) persists the
  parsed usage object, the cache-write alias, and the JSON-serialised
  raw payload alongside the legacy counters.

Status semantics:

- **`reported`** — at least one recognized cache field was present and
  parsed; counts are recorded.
- **`not_reported`** — usage block parsed cleanly but no cache fields
  were present (canonical OpenAI shape, or providers that omit the
  breakdown).
- **`unknown_format`** — payload could not be parsed, or returned a
  shape EggPool does not recognize. The cache state is ambiguous.

`QuotaFairScorer` does NOT consume cache fields; it is asserted by
`tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`.
Dashboard coverage is rendered under `/cache` (Cache → Cache observability) and
the JSON API exposes the breakdown at `GET /api/stats/cache-observability`.

## Canonical Request Segmentation (Phase 2)

Phase 2 introduces a structural segmentation layer that annotates every
finalized request into `stable_prefix` / `semi_stable_context` /
`volatile_suffix` regions without mutating the request body. The
segmentation pass is observational — it never changes routing, never
rewrites payloads, and never raises on malformed input.

The segmenter lives in `src/eggpool/transcoder/segmentation.py` and is
invoked by `src/eggpool/api/proxy_request.py` after the body is
decoded. The result is attached to `ProxyRequestContext.segmentation`
and threaded through `RequestFinalizer` → `RequestRepository.finalize_if_pending`
where it is persisted alongside the legacy `cache_counter_status` and
usage columns.

### Segment Kinds

- **`stable_prefix`** — system / developer messages, top-level `tools`
  schemas, and provider-native `cache_control` blocks. Marked
  `protected=True`, `compressible_candidate=False`. Later compression
  phases must not mutate these regions or provider cache continuity
  breaks.
- **`semi_stable_context`** — assistant messages, prior user turns,
  short follow-up user messages, and anything the classifier is
  uncertain about. Conservative default: when classification is
  ambiguous, the segmenter defaults to this kind rather than
  `volatile_suffix` so the request body is preserved.
- **`volatile_suffix`** — tool results, command output, search
  results, log output, and the latest user turn when it carries
  log / command / search markers. Marked
  `compressible_candidate=True` so later phases can identify
  candidates without re-parsing the request.

### Determinism and Content Privacy

- `SegmentationResult.stable_prefix_hash` is a SHA-256 digest of the
  stable prefix **structural descriptor** (sources, content paths,
  message indices, byte totals, token totals) — never the raw prompt
  text.
- `SegmentationResult.request_shape_hash` is a SHA-256 digest of the
  request shape (provider, protocol, role sequence, block-type
  sequence, tool schema count, coarse token buckets). Same input →
  same hash; whitespace-equivalent stable prefixes → same
  `stable_prefix_hash` (modulo byte totals).
- Both hashes are content-private. Neither exposes prompt text
  directly, and both exclude request timestamp, request ID,
  selected account, and other unstable metadata.

### Schema and Storage

Migration `0041_canonical_request_segmentation.sql` adds seven
columns to the `requests` table and a `segmentation_status` index
mirroring the Phase 1 `cache_counter_status` index:

- `segmentation_status` (TEXT NOT NULL DEFAULT 'empty_request')
- `stable_prefix_estimated_tokens` / `semi_stable_estimated_tokens`
  / `volatile_estimated_tokens` (INTEGER, nullable)
- `stable_prefix_bytes` / `semi_stable_bytes` / `volatile_bytes`
  (INTEGER, default 0)
- `segmentation_summary_json` (TEXT) — compact JSON serialisation
  of the full `SegmentationResult` for audit and dashboard
  drill-down; raw request content is never persisted.

`EXPECTED_SCHEMA_VERSION` is bumped to 41 in `scripts/check_database.py`.
The migration is non-destructive: legacy callers that do not run the
segmenter continue to work, with default `segmentation_status =
'empty_request'`.

### Observability

The stats layer exposes
`fetch_canonical_request_segmentation(db, start, end)` and the
service-layer method
`StatsService.get_canonical_request_segmentation(period)`. The
`/cache` dashboard page (Cache → Segmentation) renders per-status
counts, per-segment-kind token and byte totals, and a per-model
breakdown. The JSON endpoint `GET /api/stats/canonical-request-segmentation`
returns the same data for tooling.

### Invariants

- Segmentation is observational: it does not affect request bodies,
  route scoring, or eligibility. Asserted by
  `tests/unit/test_canonical_request_segmentation.py`.
- `segment_request` never raises on malformed input. Empty requests
  yield `SegmentationStatus.EMPTY_REQUEST`; non-mapping payloads
  yield `SegmentationStatus.PARSE_FAILURE`; well-formed requests
  yield `SegmentationStatus.SEGMENTED`.
- Token and byte estimates are cheap (no tokenizer dependency) and
  never raise. Missing estimates remain `None` and never block
  request handling.
- The finalizer is duck-typed against
  `FinalizationData.segmentation: Any | None`, so the transcoder
  module does not appear in the import path of unrelated callers.

## Transcoder Cache Stability (Phase 3)

Phase 3 is the cache-stability layer of the cache-preserving
deterministic compression roadmap. It is observational only: it
records what the transcoder did to `cache_control` annotations
during protocol translation so operators can attribute cache hit-rate
loss to specific fields and rebalance accordingly.

The implementation lives in `src/eggpool/transcoder/cache_stability.py`
and is consumed by both `OpenAIToAnthropic.encode_request` and
`AnthropicToOpenAI.encode_request`. It is also a public surface
exported from `eggpool.transcoder` for downstream test code.

### Cache Boundary Tracker

`CacheBoundaryTracker` is an append-only, bounded tracker (cap = 64
annotations per request) carried on
`TranscodeContext.cache_boundary_tracker`. Each entry is a frozen
`CacheBoundaryAnnotation` with `kind`, `source_protocol`,
`target_protocol`, `source_path`, `target_path`, and
`cache_control_type`. The tracker records these kinds:

| Kind | Meaning |
|---|---|
| `preserved` | Cache annotation carried across at the same path with the same `cache_control_type`. |
| `preserved_relocated` | Reserved for future phases that relocate the annotation onto a different target path. |
| `dropped_unsupported_target` | The target protocol cannot carry this annotation (e.g. OpenAI has no `cache_control`). |
| `dropped_feature_disabled` | The annotation was discarded by policy. |
| `dropped_invalid_shape` | The annotation failed shape validation (missing or non-string `type`). |
| `synthesized` | Reserved for future phases that synthesise cache hints on behalf of the caller. |

Over-cap events increment `dropped_count` so operators can detect
truncation. The tracker is **never** consumed by routing — it lives
alongside Phase 1 and Phase 2 cache observability as a reporting
surface only.

### Helper Surface

- `extract_cache_control_type(cache_control)` — returns the `type`
  field of a `cache_control` annotation, or `None` for any malformed
  shape. Both transcoders use this to validate cache-control shapes
  before propagation.
- `extract_cache_boundaries(body)` — structural walker that returns
  every `cache_control` annotation in document order as
  `(dot_path, cache_control_type)` pairs. The walk is intentionally
  structural: it inspects Anthropic-style system blocks, tool
  definitions, and message content blocks; OpenAI-style inputs
  (which never carry `cache_control` natively) yield an empty list
  unless a tool definition is annotated via the bridging extension
  that the Phase 3 contract recognises.
- `extract_provider_visible_prefix(body)` — returns the body minus
  the volatile suffix (last message, `stream` flag) so callers can
  compute cache keys that ignore the user turn.
- `stable_dumps(payload)` / `stable_hash(payload)` — deterministic
  JSON serialisation and SHA-256 of the cache prefix; key order is
  canonicalised so wire bytes match across processes.

### Transcoder Wiring

The two body transcoders emit a structured loss warning and a
boundary annotation for every `cache_control` event during
translation:

- `OpenAIToAnthropic.encode_request` preserves `tools[].cache_control`
  annotations (carried across as `preserved`), records
  `dropped_invalid_shape` for malformed shapes, and emits a
  `stable_prefix_preserved` / `stable_prefix_reordered_canonically`
  summary at the end of the pass.
- `AnthropicToOpenAI.encode_request` drops every `cache_control`
  annotation (OpenAI has no equivalent field) and records
  `dropped_unsupported_target` boundaries with
  `cache_control_unsupported_by_target_protocol` warnings. Top-level
  Anthropic `cache_control` is reported as
  `cache_control_feature_disabled` to distinguish it from
  protocol-level loss.

Both transcoders also record `provider_extension_not_preserved` for
non-portable vendor fields on Anthropic tools (e.g. `defer_loading`,
`input_examples`) so operators can see which extensions are dropped.

### Loss-Policy Enforcement

When the operator has configured `loss_policy = "reject"` on
`[transcoder]`, each body transcoder scans its own emitted warnings
after translation completes. If any of the five protected
cache-control loss kinds (`cache_control_unsupported_by_target_protocol`,
`cache_control_feature_disabled`, `cache_control_invalid_shape`,
`provider_extension_not_preserved`,
`stable_prefix_reordered_canonically`) appears, the transcoder raises
`eggpool.transcoder.errors.TranscodeLossError`. The proxy layer
catches it in `proxy_request.py::handle_proxy_request` and renders it
as HTTP 400 with `invalid_request_error`. The `warn` default
preserves the v1 behaviour: the request proceeds and the loss is
recorded on `TranscodeContext.loss_warnings` for audit.

The preflight path in `proxy_request.py::_prepare_transcode_preflight`
runs the transcoder in `warn` mode regardless of operator policy so it
can collect the full warning list before the proxy-level check fires
on any non-empty warning set. The proxy-level check is the broader
gate (any warning → reject); the transcoder-level check is the
narrower gate (only protected cache-control kinds → reject). Both
must be satisfied for a request to be dispatched in reject mode.

The transcoder never injects warning text into the translated body.
Regression-guarded by
`tests/unit/test_transcoder/test_phase3_cache_stability.py::TestWarningsNotInModelVisibleContent`.

### Invariants

- The cache-stability layer is observational: it does not affect
  request bodies, route scoring, or eligibility. Asserted by
  `tests/unit/test_transcoder/test_phase3_cache_stability.py` and
  `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`.
- `CacheBoundaryTracker.record` is append-only and never raises. The
  cap is enforced silently with `dropped_count` incremented.
- `extract_cache_boundaries` never raises on malformed input — it
  returns an empty list for non-dict bodies, dicts that lack
  recognisable containers, or unknown field shapes.
- The boundary tracker is read by reporting surfaces only; routing
  consumes request count + token count + cost (audit) + active count
  + health feed, never cache fields.

## Observe-Mode Compression Accounting (Phase 4)

Phase 4 is the observe-mode layer of the cache-preserving deterministic
compression roadmap. It runs the compression analyzer over every
request's Phase 2 segmentation and records a per-request roll-up of
candidate counts, savings estimates, latency, and reason codes — but
**never mutates the request body, never changes routing, and never
synthesises provider cache controls**.

The implementation lives in
`src/eggpool/transcoder/compression/` and is consumed by
`proxy_request.py` after the segmenter runs. The public surface is
`eggpool.transcoder.compression.analyze_compression`, which returns a
`CompressionObservation` or `None` when disabled. The default config
has `enabled = false` and `mode = "observe"`, so production behaviour
is unchanged unless an operator opts in.

### Configuration

`CompressionConfig` lives on `AppConfig.compression` and is wired
through `app.state.compression_policy`. Operators can set:

- `enabled` (default `false`) — master switch.
- `mode` (default `"observe"`) — only `"observe"` is implemented.
- `placement` (default `"suffix_only"`) — which segment kinds are
  eligible.
- `respect_cache_boundaries` (default `true`) — never compress
  protected segments.
- `compress_static_prefix` (default `false`) — rejected at config
  validation in observe mode.
- `min_candidate_tokens` / `min_savings_tokens` — eligibility
  thresholds; candidates below either are recorded as suppressed.
- `max_compression_latency_ms` — analyzer budget; over-budget runs
  record `latency_budget` warnings and stop cleanly.
- `transforms.{fold_repeated_lines,compact_logs,compact_search_results,elide_base64_blobs,minify_machine_json,compact_stack_traces}`
  — per-transform toggles, all `true` by default.

### Analyzer

`analyze_compression(segmentation, policy, text_hints=None)` is a total
function: it never mutates the segmentation, never raises on malformed
input, and is content-private (production never sets `text_hints`; only
test fixtures do, so the regex/JSON detection paths are exercised
without exposing raw prompts). The analyzer:

1. Walks every `RequestSegment` in document order.
2. For each segment, runs the enabled transforms (`fold_repeated_lines`,
   `compact_logs`, `compact_search_results`, `elide_base64_blobs`,
   `minify_machine_json`, `compact_stack_traces`).
3. Each transform produces a `CompressionCandidate` with deterministic
   savings estimates derived from segment metadata (`kind`, `source`,
   `byte_length`, `estimated_tokens`, `protected`) plus the optional
   `text_hints` preview.
4. Policy filtering bumps the relevant reason code for every
   suppression: `protected_cache_boundary`, `static_prefix`,
   `placement`, `below_min_candidate_tokens`,
   `below_min_savings_tokens`, `transform_disabled`, `empty_segment`.
5. Latency budget check before each segment; over-budget runs append
   `latency_budget` warnings and stop.

The result is a `CompressionObservation` carrying
`candidate_count`, `eligible_candidate_count`,
`suppressed_candidate_count`, `estimated_original_tokens`,
`estimated_compressed_tokens`, `estimated_savings_tokens`,
`analyzer_latency_ms`, `warnings`, `reason_code_counts`, per-candidate
records, and `transform_counts`. A `to_summary_json` helper produces a
compact JSON snapshot for persistence.

### Persistence (Migration 0042)

`compression_status`, `compression_mode`,
`compression_candidate_count`, `compression_eligible_candidate_count`,
`compression_suppressed_candidate_count`,
`compression_estimated_original_tokens`,
`compression_estimated_compressed_tokens`,
`compression_estimated_savings_tokens`,
`compression_analyzer_latency_ms`, `compression_warning_count`,
`compression_reason_code_counts_json`, `compression_summary_json` are
added to the `requests` table. `EXPECTED_SCHEMA_VERSION` in
`scripts/check_database.py` is bumped to **42**; the migration's
checksum is recorded in `src/eggpool/db/schema/checksums.json`.

`RequestRepository.finalize_if_pending` accepts the new fields and
persists them in the same transaction as the rest of the request
finalization. The finalizer (`src/eggpool/request/finalizer.py`) reads
the duck-typed `compression_observation` from `FinalizationData` and
extracts each field with safe `getattr` defaults. Legacy callers that
do not run the analyzer write `compression_status = "disabled"`.

### Stats Surface

`fetch_compression_observability(start, end)` in
`src/eggpool/stats/queries.py` aggregates the persisted fields over a
time window. It returns:

- `total_requests`, `requests_with_compression_observed`
- `candidate_count`, `eligible_candidate_count`, `suppressed_candidate_count`
- `total_estimated_savings_tokens`, `median_savings_tokens`,
  `p95_savings_tokens`
- `median_analyzer_latency_ms`, `p95_analyzer_latency_ms`
- `top_reason_codes` (top-10 by count)
- `per_provider_status`, `per_model_status`, `per_mode_status`

The handler is mounted at `/api/stats/compression-observability` in
`src/eggpool/api/stats.py` and surfaces the roll-up in the same
shape as the cache-observability endpoint.

### Invariants

- The analyzer is observational: it never affects request bodies,
  route scoring, or eligibility. Asserted by
  `tests/unit/test_compression_analyzer.py::test_analyzer_never_mutates_segmentation`
  and the Phase 2/3 regression suite.
- `analyze_compression` is total: it returns `None` for disabled
  policies and never raises on malformed input.
- The dashboard / API roll-up does not change routing; the
  `QuotaFairScorer` still does not consume compression fields.
- Migration 0042 is non-destructive: pre-Phase-4 rows render as
  `compression_status = "disabled"` with zero counts and no
  warnings.

## Safe-Mode Suffix Compression (Phase 5)

Phase 5 is the first request-mutating layer of the cache-preserving
deterministic compression roadmap. It applies deterministic
transforms only to eligible `volatile_suffix` segments identified
by Phase 2's segmenter, and re-verifies the exact stable-prefix
content hash on the post-mutation payload. The applier is fail-closed: any
unexpected change to the stable-prefix content falls back to
the original payload and records a high-severity warning.

### Configuration

- `mode = "safe"` activates the applier. The default is `"observe"`
  (Phase 4 — reporting only), so production behaviour is unchanged
  unless an operator explicitly opts in.
- `placement = "suffix_only"` (default) restricts candidates to
  volatile-suffix regions; the other placement values remain
  reserved for future phases.
- `respect_cache_boundaries = true` (default) suppresses every
  candidate that overlaps a protected stable-prefix segment.
- `compress_static_prefix` is rejected in safe mode unless the
  operator sets `allow_static_prefix_override = true` (default
  false). Static-prefix mutation is opt-in and explicitly dangerous.
- `min_candidate_tokens` (default 2048) and `min_savings_tokens`
  (default 1024) gate eligibility.
- `max_compression_latency_ms` (default 25) bounds the applier
  budget; over-budget runs append `latency_budget_exceeded` warnings.
- Per-request headers: `x-eggpool-compression: off|observe|safe`
  (when `header_override = true`) and `x-eggpool-cache-policy:
  preserve` to opt out for cache-equivalent flows.
- Six transforms live on `CompressionTransforms`: `fold_repeated_lines`,
  `compact_logs`, `compact_search_results`, `elide_base64_blobs`,
  `minify_machine_json`, `compact_stack_traces`. Each can be
  toggled individually.

### Applier

`apply_safe_compression(payload, segmentation, *, policy, text_hints=None)`
in `src/eggpool/transcoder/compression/apply.py` is a total function.
It returns a `CompressionResult` carrying:

- `applied: bool` — True if mutation happened
- `transform_count: int` — number of transforms that mutated
- `transforms_by_reason: Mapping[str, int]` — reason-code → count
  for the transforms APPLIED
- `original_tokens` / `compressed_tokens` / `savings_tokens`
- `pre_stable_prefix_hash` / `post_stable_prefix_hash` (SHA-256 hex)
- `stable_prefix_preserved: bool`
- `warnings`, `latency_ms`, `failed_fallback`, `summary_json`

The applier:

1. Guards: returns no-op when `policy.enabled` is False or
   `policy.mode != "safe"` or segmentation is empty.
2. Recomputes the pre-mutation `stable_prefix_content_hash` by
   re-extracting canonical stable-prefix content from the original
   payload via stable-prefix segment paths and hashing it.
3. Discovers planned replacements by walking every volatile-suffix
   segment, applying each enabled transform whose estimated savings
   clear the eligibility thresholds. Once a replacement is
   planned, the applier applies it through path-level copy-on-write:
   only the dict/list ancestors on each mutated path are copied,
   and unchanged subtrees are preserved by reference. If no
   replacement is planned, the original payload object is returned
   unchanged. This is *not* a full deep copy: it is a selective
   structural copy that preserves the "input is never mutated"
   invariant without paying for a full payload clone on no-op runs.
   Each segment's `content_path` is a
   concrete JSON path resolving to an actual string leaf of the
   request payload (e.g. `("messages", i, "content")` for OpenAI
   string content, `("messages", i, "content", j, "text")` for
   OpenAI list content parts, `("system",)` for Anthropic string
   system, `("system", j, "text")` for Anthropic system blocks,
   `("messages", i, "content", j, ...)` for Anthropic content
   blocks). Path-resolution helpers `resolve_path` and
   `resolve_text_path` are available for tests and debug assertions.
4. Inserts a deterministic marker `[EggPool compression: <transform>
   | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]`
   via `eggpool.transcoder.compression.markers.build_marker`. The
   marker format is unified across all six transforms.
5. Recomputes the post-mutation `stable_prefix_content_hash` by
   re-extracting canonical stable-prefix content from the
   TRANSFORMED payload via the same stable-prefix segment paths.
   If it diverges from the pre-mutation hash and the policy does
   not allow static-prefix mutation, the applier fails closed:
   `applied=False`, `transformed_payload` is the ORIGINAL payload,
   `failed_fallback=True`, `warnings` includes
   `stable_prefix_hash_mismatch`, and `summary_json` records the
   rollback. The fail-closed verification re-hashes the mutated
   payload content, not just immutable segment metadata, so it
   catches real path bugs that mutate stable-prefix content.
6. Never mutates the input payload or segmentation in place.
   No-op runs return the original payload object by identity;
   applied runs return a path-level copy-on-write payload that
   shares unchanged subtrees with the input.
7. Never raises on malformed input; all exceptions are caught
   and rendered as fail-closed results with `failed_fallback=True`.

### Path Semantics

Every emitted `RequestSegment.content_path` is a concrete JSON path
that resolves to an actual string leaf of the request payload — not a
semantic role label. Examples after the Phase 5 Anthropic closure:

- OpenAI string content: `("messages", i, "content")`
- OpenAI list content part: `("messages", i, "content", j, "text")`
- Anthropic string system: `("system",)`
- Anthropic system block: `("system", j, "text")`
- Anthropic message text block: `("messages", i, "content", j, "text")`
- Anthropic `tool_result` with string content:
  `("messages", i, "content", j, "content")`
- Anthropic `tool_result` with nested text part:
  `("messages", i, "content", j, "content", k, "text")`

`resolve_path(payload, content_path)` walks the path on the live
payload; `resolve_text_path` is the same plus a `str` leaf check.
The applier's `_collect_text` and `_replace_path` use the same walk
internally. The fail-closed verification re-hashes the mutated
payload's stable-prefix content via `stable_prefix_content_hash`,
so a path that does not resolve to a real leaf would either be
ignored by the applier (no compression, no fail-closed trip) or
trigger a stable-prefix hash mismatch. Production emits a tight
invariant: every compressible-candidate path must resolve to a
non-`None` string leaf; this is asserted by
`tests/unit/test_compression_path_resolution.py`.

A single block may emit multiple segments (Anthropic
`tool_result` with nested content lists emit one segment per text
leaf). The applier walks each segment independently and combines
markers into the targeted text leaf.

### Stable-Prefix Content Hash vs Structural Descriptor Hash

Two distinct hashes live on `SegmentationResult`:

- `stable_prefix_hash` — STRUCTURAL descriptor hash computed by
  `_stable_prefix_descriptor` (byte totals, token totals, source
  kinds, path signatures). This is the Phase 2 dashboard-grouping
  hash. It is NOT sufficient for exact cache equality across
  requests with identical structure but different content.
- `stable_prefix_content_hash(payload, segmentation)` — EXACT
  content hash computed by `stable_prefix_content_hash`. Re-extracts
  canonical stable-prefix values from the live payload via
  stable-prefix segment paths and hashes them. This is the hash
  the Phase 5 applier uses for the pre/post fail-closed check.

`apply_safe_compression` carries BOTH (`pre_content_hash` drives
fail-closed; `pre_shape_hash` is recorded for dashboard grouping).
Code paths must consume `stable_prefix_content_hash` for any
equality decision that matters for cache hits; `stable_prefix_hash`
is for grouping and rollout observability only.

### Context-Limit Precedence

Context-limit checks happen before compression. Compression does NOT
make otherwise over-limit requests fit within model limits. This is
by design — compression is a token-saving optimization, not a
context-fit rescue mechanism. A follow-up "context-pressure
compression preflight" phase is planned for the future if needed.

### Marker Format

`src/eggpool/transcoder/compression/markers.py` exposes
`build_marker`, `parse_marker`, and `is_marker_line`. The marker
is a single line, deterministic (no timestamps), and round-trips
through `parse_marker`. The digest is the lowercase hex SHA-256
of the original (pre-transform) text bytes. The format is unified
across all six transforms:

```text
[EggPool compression: <transform> | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]
```

### Wiring

The applier runs in `src/eggpool/api/proxy_request.py` AFTER the
Phase 4 analyzer and BEFORE model rewrite, so the transcoder and
provider dispatch always see the post-mutation body. The
`CompressionResult` is attached to `FinalizationData.compression_result`
and extracted by `src/eggpool/request/finalizer.py` with the same
duck-typed `getattr` pattern Phase 4 uses for the analyzer.

### Persistence (Migration 0043)

Thirteen new columns on `requests`:

- `compression_applied` (bool), `compression_transform_count` (int),
  `compression_transforms_by_reason_json` (text),
  `compression_original_tokens` / `compression_compressed_tokens` /
  `compression_savings_tokens` (ints),
  `compression_pre_stable_prefix_hash` /
  `compression_post_stable_prefix_hash` (text),
  `compression_stable_prefix_preserved` (bool),
  `compression_warnings_json` (text),
  `compression_latency_ms` (real),
  `compression_failed_fallback` (bool),
  `compression_applied_summary_json` (text)

Plus indexes `idx_requests_compression_applied` and
`idx_requests_compression_savings_tokens`. `EXPECTED_SCHEMA_VERSION`
in `scripts/check_database.py` is bumped to **43**.

`RequestRepository.finalize_if_pending` accepts the new fields
and persists them in the same transaction as the rest of the
request finalization. The finalizer extracts them with safe
`getattr` defaults; legacy callers that did not run the applier
write `compression_applied = 0` and `stable_prefix_preserved = 1`.

### Stats Surface

`fetch_compression_observability(start, end)` in
`src/eggpool/stats/queries.py` now also aggregates the applied-mode
columns and returns:

- `requests_with_compression_applied`,
  `applied_transform_count_total`,
  `applied_total_savings_tokens`,
  `applied_median_savings_tokens`,
  `applied_p95_savings_tokens`,
  `applied_median_latency_ms`,
  `applied_p95_latency_ms`,
  `applied_stable_prefix_preserved_count`,
  `applied_failed_fallback_count`,
  `top_applied_reason_codes`,
  `applied_per_provider_status`,
  `applied_per_model_status`,
  `applied_per_mode`

The existing Phase 4 fields are preserved unchanged. The handler
at `/api/stats/compression-observability` returns the union.

### Invariants

- The applier never mutates stable-prefix segments unless
  `compress_static_prefix=True AND allow_static_prefix_override=True`.
  Asserted by `tests/unit/test_compression_apply.py`.
- Pre/post `stable_prefix_content_hash` MUST match whenever
  `compress_static_prefix` is False. A mismatch triggers fail-closed.
  The content hash is computed by re-extracting canonical
  stable-prefix content from the payload via segment paths.
- `apply_safe_compression` is total: it never raises on malformed
  input. Failures surface as `failed_fallback=True`.
- The dashboard / API roll-up does not change routing; the
  `QuotaFairScorer` still does not consume compression fields
  (Phase 1–4 invariant preserved).
- Compression/cache metrics do NOT affect same-provider account
  routing.
- Context-limit checks happen before compression; compression
  cannot rescue over-limit requests.
- Migration 0043 is non-destructive: pre-Phase-5 rows render as
  `compression_applied = 0`, `compression_stable_prefix_preserved = 1`,
  zero transforms, no warnings.

## Compression Policy Overrides (Phase 6)

Phase 6 lets operators target specific clients, protocols, models, or
transcoding paths with `[[compression.policies]]` rows that overlay
the global `[compression]` config without changing it for everyone
else. The override layer is observational and reporting-only: the
underlying compression behavior is unchanged for requests that do not
match an override, and resolution never raises.

### Resolver

`resolve_compression_policy(base, ctx, *, overrides=None)` in
`src/eggpool/transcoder/compression/policy_resolver.py` picks and
merges the policy for one request. The algorithm:

1. Walk the override list in file order.
2. For each override whose match fields fire against the
   `CompressionPolicyContext`, overlay non-`None` fields onto the
   current config. Scalar fields use last-match-wins; `transforms`
   merge field-by-field (`None` inside an override keeps the base
   value, `True` / `False` wins).
3. Re-validate the merged config against the same safety rules as
   the global config (static-prefix guard, transform defaults).
4. On validation error, fall back to the previous config and append
   a structured warning. Resolution never raises.
5. Return a frozen `ResolvedCompressionPolicy(name, source, config,
   matched_policy_names, warnings)`. `name` is the matched override
   name (`"<global>"` when none matched); `source` is `"global"` or
   `"policy:<name>"`.

### Match Surface

The pre-route context carries `client_id`, `client_name`,
`source_protocol`, `target_protocol`, `requested_model`,
`resolved_model`, `provider_id`, `provider_kind`, and `transcoded`.
Provider-specific fields (`provider_id`, `provider_kind`,
`resolved_model`) are `None` pre-route; the corresponding
`match_provider_ids`, `match_provider_kinds`, `match_models` matchers
are silently skipped until post-route resolution exists. Effective
pre-route matchers are `match_clients`, `match_requested_models`,
`match_protocols`, and `match_transcoded`. Match fields union OR:
the override fires when **any** field fires. Glob support is `*foo`,
`foo*`, `*foo*`, and exact match (case-sensitive).

### Configuration

`CompressionPolicyOverride` accepts:

- `name` (required, non-empty, must not shadow `"<global>"`)
- `match_*` (any of the seven match fields, all optional)
- `mode`, `enabled`, `placement`, `respect_cache_boundaries`,
  `min_candidate_tokens`, `min_savings_tokens`,
  `max_compression_latency_ms`, `compress_static_prefix` (overlay
  knobs; `None` keeps the base value)
- `transforms` (optional `CompressionTransforms` overlay; merged
  field-by-field)

The parent `CompressionConfig` validator rejects catch-all overrides
(no match fields) unless the operator names the row `"default"`. It
also rejects duplicate names and rejects `compress_static_prefix =
true` in any non-default override unless global
`allow_static_prefix_override = true` is set (the same safety rail
as the global config).

### Wiring

`src/eggpool/api/proxy_request.py` constructs a
`CompressionPolicyContext` from request headers (x-eggpool-client,
User-Agent, source protocol) and the requested model, calls
`resolve_compression_policy(base, ctx)`, and stores the result on
`ProxyRequestContext.resolved_compression_policy`. The coordinator
propagates the resolved policy into every `FinalizationData`. The
finalizer extracts `name`, `source`, and `warnings` and persists
them via migration 0044. Resolver failures (import error, call
exception) fall back to a global sentinel policy and the request
proceeds with the safe default — request path never crashes.

### Persistence (Migration 0044)

Three columns + one index are added to `requests`:

- `compression_policy_name TEXT` — name of the matched override
  (`"<global>"` for no match)
- `compression_policy_source TEXT` — `"global"` or `"policy:<name>"`
- `compression_policy_warnings_json TEXT` — JSON array of structured
  warnings produced during resolution

An index on `compression_policy_name` supports the stats roll-up.

### Stats Surface

`fetch_compression_observability` in
`src/eggpool/stats/queries.py` extends the existing roll-up with:

- `by_policy: dict[str, int]` — request count per policy name
- `by_policy_source: dict[str, int]` — request count per source
  (`"global"`, `"policy:..."`, etc.)
- `policy_warning_count_total: int` — sum of warning counts across
  finalized requests

The endpoint `GET /api/stats/compression-observability` exposes the
combined roll-up.

### Invariants

- The resolver never mutates `base`. Every overlay produces a new
  `CompressionConfig`.
- Resolution never raises. Malformed overrides are skipped with a
  warning; the previous config is preserved.
- The `QuotaFairScorer` does NOT consume policy fields. Phase 6
  routing parity with Phases 1–5 is preserved.
- Provider-specific matchers (`match_provider_ids`,
  `match_provider_kinds`, `match_models`) are silently skipped
  pre-route; post-route resolution is reserved for a future phase.
- Migration 0044 is non-destructive: pre-Phase-6 rows render as
  `compression_policy_name = "<global>"`,
  `compression_policy_source = "global"`, empty warnings array.
- The static-prefix safety rail applies to overrides: a non-default
  override cannot enable `compress_static_prefix` unless global
  `allow_static_prefix_override` is true.

## Dashboard and Runtime Views (Phase 7)

The runtime/dashboard layer now presents these internals on `/cache`
as one operator-facing **Request shaping** surface plus detailed
drill-down cards. `/runtime` keeps the live telemetry and a compact
link back to Cache. It is view-only: no new migrations, no new
persisted columns, no changes to routing. The purpose is to let an
operator answer questions like:

- Are providers reporting cache counters, or are values unknown?
- Are stable prefixes being preserved across transcoding and
  compression?
- How often does observe mode find compression opportunities? How
  often does safe mode actually compress?
- Which transforms are doing useful work?
- Which policies are active? Are fallbacks occurring?
- Is compression latency bounded and SBC-safe?
- Are cache/compression metrics reporting-only and not influencing
  routing?

### API surface

Seven focused JSON endpoints live under `/api/stats/`:

| Endpoint | Purpose |
|----------|---------|
| `request-shaping` | Operator-facing summary of compression mode, cache-reporting coverage, synthetic cache state, advisory tuning, and routing guardrails. |
| `cache-observability` | Coverage of cache counters by status (`reported` / `not_reported` / `unknown_format`); provider cache hit rate (cache reads are hits; cache writes/creation are warmup, not hits); deprecated `cache_hit_ratio_known_only` alias; cached input tokens by provider/model. |
| `canonical-request-segmentation` | Segmentation status counts; avg stable / semi-stable / volatile token estimates; top request-shape hashes. |
| `cache-stability` | Narrow summary. Per-boundary preservation/drop detail lives on the in-memory `CacheBoundaryTracker`; this endpoint confirms the tracker is wired and reports durable counters. |
| `compression-observability` | Observe-mode opportunities (candidates, estimated savings, suppress reasons) plus policy/source rollups. |
| `compression-runtime` | Safe-mode outcomes: applied / failed_fallback counts, candidate counts, estimated + actual savings tokens, latency (avg/p50/p95/max), per-transform applied/tokens_saved, warnings rollup, `cache_safety` stable-prefix preserved/mismatch. |
| `compression-policies` | Per-policy rollup with `<global>` sentinel first: request count, mode distribution, applied, failed_fallback, candidates, warnings. |

Endpoints are added to `register_dashboard_routes` in
`src/eggpool/dashboard/routes.py` and grouped with the existing
dashboard auth gate. Empty-DB responses are stable zero shapes; bad
window parameters return HTTP 400, not 500.

### Cache page cards

`render_cache` in `src/eggpool/dashboard/render.py` renders the
request-shaping summary plus the detailed drill-down cards on
`/cache`:

1. **Request shaping** — operator summary of compression mode,
   provider cache counter coverage, EggPool cache annotation state,
   safety guardrail, tuning suggestions, and routing isolation. The
   summary reads `Clean` / `Isolated` on a quiet default install
   instead of raw `reporting_only` or `—`. Raw modes survive in
   subtext/details, not the primary metric.
2. **Provider cache counters** — provider-reported cache counters and
   coverage. Provider cache counters and EggPool cache annotations
   are reported on separate cards and never conflated.
3. **Request segmentation** — stable-prefix, semi-stable, and
   volatile suffix structure without payload mutation.
4. **Compression** — observe-mode analysis and safe-mode outcomes,
   latency, and `cache_safety` stable-prefix preservation. Advanced
   diagnostics are collapsed by default.
5. **Compression policy** — per-policy rollup table with `<global>`
   sentinel first.
6. **Cache stability** — transcoded count plus the Phase 3 boundary
   tracker note.
7. **EggPool cache annotations** — optional provider-bound cache
   annotations, disabled by default and dry-run first.
8. **Tuning suggestions** — bounded recommendation-only threshold
   guidance.

`render_runtime` keeps only a compact Cache & request shaping
relocation panel that links operators back to `/cache`.

#### Cache page summary cards

The top summary on `/cache` renders six operator-facing cards in this
order:

| Card | Source | Quiet default |
|------|--------|---------------|
| Request changes | `mode.compression` + `compression.*` | `no changes` |
| Provider cache counters | `cache.cache_counter_reported_rate` + `cache_counter_reported_rows` + `cache_counter_known_rows` | `—` / `N provider-reported rows · M classified rows` |
| EggPool cache annotations | `mode.synthetic_cache` + `synthetic_cache.*` | `Off` |
| Safety guardrail | `guardrails.failed_fallback_count` + `policy_warning_count` + `synthetic_cache.warning_count` | `Clean` |
| Tuning suggestions | `mode.tuning` + `tuning.recommendation_count` + `tuning.override_count` | `Off` |
| Routing isolation | `mode.routing` + `guardrails.routing_uses_*` | `Isolated` (raw mode survives in subtext) |

#### Cache page advanced diagnostics disclosure

The advanced diagnostics `<details>` disclosure on `/cache` is
server-decided. `CacheAdvancedState` (`src/eggpool/dashboard/render.py`)
is the single source of truth: it carries `open_by_default`, `warning`,
and a `reasons` tuple. The disclosure auto-opens whenever any of the
following is true:

- compression warnings (runtime window count or guardrails-derived) > 0
- compression failed fallback count > 0
- compression stable-prefix mismatch > 0
- compression policy warning count > 0
- EggPool annotation warnings > 0
- EggPool annotation applied count > 0 (mutates requests — must surface)
- segmentation parse failures > 0
- tuning recommendation count > 0
- tuning override count > 0
- routing isolation unhealthy (`routing_uses_*` flags non-empty)
- transcoding loss warnings > 0

Quiet installs (zero everywhere, healthy routing, no synthetic-cache
mutation, no tuning activity) keep the disclosure collapsed. The
disclosure summary text reads `Advanced diagnostics (N active)` for
non-warning triggers and `Advanced diagnostics (N needs review)` for
warning-class triggers.

A static **routing-separation notice** card always renders on the
/cache page with this exact text:

> Cache and compression metrics are reporting-only. The
> `QuotaFairScorer` routes on request count, token count, active
> count, and upstream health — it never consumes cache or compression
> fields.

### Safety guarantees

- No raw prompts, tool outputs, system messages, request bodies, or
  auth headers appear in any card or JSON response. Phase 4's
  content-private analyzer is unchanged; Phase 7's rollups operate on
  aggregated counters and JSON columns.
- The `QuotaFairScorer` does not consume any Phase 7 field. The new
  endpoints and cards are pure observers.
- Phase 7 never mutates a request, never changes routing, and never
  synthesizes provider cache-control.

## Routing Guardrails and Non-Interference (Phase 8)

Phases 1–7 add cache/compression observability and policy controls.
Phase 8 codifies the invariant that those metrics NEVER enter account
scoring, health removal, or route reselection.  This is what lets the
fairness rotor keep multiple same-provider subscriptions balanced: a
provider with high cache hit ratio or large compression savings does
not gain any selection advantage over its peers.

### Hardcoded runtime diagnostic

`RuntimeMetricsService._snapshot_routing_runtime` returns a `guardrails`
dict with constant flags:

```json
{
  "routing_cache_compression_mode": "reporting_only",
  "routing_uses_cache_metrics": false,
  "routing_uses_compression_metrics": false,
  "routing_uses_stable_prefix_hash": false,
  "routing_uses_compression_policy": false,
  "route_scorer_inputs": ["health", "quota", "active_requests", "model_eligibility"]
}
```

The flags are derived from how the router is built, not from the
current request stream.  They are exposed via `GET /api/stats/runtime`
and rendered as a **Routing guardrails (Phase 8)** card on the /cache
dashboard next to the routing-separation notice.

### Routing input boundary

The `QuotaFairScorer.score_accounts()` signature accepts only four
inputs:

- `account_names: list[str]`
- `model_name: str | None`
- `active_requests: dict[str, int] | None`
- `request_estimates: dict[str, int] | None`

`Router._score_eligible_accounts` is the only call site; it builds
`active_requests` from `AccountRuntimeState.active_request_count` and
forwards to the scorer.  No cache, compression, segmentation, or
policy field ever crosses this boundary.

`RoutingScore` itself carries no cache/compression fields.  The data
class audit is pinned by `tests/unit/test_routing_guardrails.py`.

### Compression health separation

`apply_safe_compression` returns `CompressionResult(failed_fallback=True)`
when the post-transform stable-prefix content hash doesn't match the
pre-transform hash.  This is observational; the fallback path does not
increment provider error counters, does not write an
`account_backoffs` row, and does not call `HealthManager.mark_*`.
Health remains driven solely by upstream-observed failures (429/402/5xx/
auth), operator disablement, and catalog/protocol incompatibility.

### Policy non-interference

`resolve_compression_policy` returns a `ResolvedCompressionPolicy`
value object.  It does not mutate the registry, the candidate set, or
the routing decision.  Provider-specific match fields
(`match_provider_ids`, `match_provider_kinds`, `match_models`) are
silently skipped pre-route; the resolver never reroutes to satisfy a
policy override.

### No post-compression reroute

The proxy request flow (`api/proxy_request.py`) runs compression
**before** routing:

1. parse + validate body
2. segmentation (observational)
3. policy resolution (observational)
4. compression analyzer (observational)
5. compression applier (mutates payload only)
6. `coordinator.execute(context)` — calls `Router.select_accounts_for_failover` once per attempt

The compression results travel on `ProxyRequestContext` for
finalization only.  Compression fallbacks, transforms, savings
estimates, and policy resolutions never re-enter the route selection
loop.  The coordinator's retry loop reroutes on upstream transport /
HTTP errors only.

### Same-provider fairness regression

Two same-provider accounts with identical load but wildly different
cache/compression profiles still rotate fairly.  The regression suite
in `tests/unit/test_routing_guardrails.py::TestSameProviderFairnessUnderAdversarialCacheAndCompression`
covers five adversarial scenarios:

- identical `cache_counter_status`
- skewed `cached_input_tokens` (high vs zero)
- skewed `cache_read_tokens` (cache-friendly provider)
- skewed `compression_status` / `applied` / `savings`
- skewed `stable_prefix_hash` (heavy cache hits vs none)

Each scenario runs 40 selections and asserts both accounts get
selected at least once.

### Test pin surface

`tests/unit/test_routing_guardrails.py` is the central regression
file.  It contains:

- signature audit (`inspect.signature` of `score_accounts` and
  `dataclasses.fields(RoutingScore)`) — no cache/compression/policy
  parameter or field is allowed
- identical-load, adversarial-cache behavioural pin
- same-provider fairness rotor pin across five adversarial scenarios
- compression fallback isolation from `HealthManager`
- policy resolver non-interference pin
- runtime diagnostic shape pin

If a future change adds a cache/compression field to the scorer's
input set, the parameter-name audit fails.  If a future change
restructures the `RoutingScore` to carry a cache field, the
`dataclasses.fields` audit fails.  If a future change introduces a
post-compression reroute, the compression-flow pin surfaces.

### Future cache-aware routing mode

A future opt-in **cache-aware routing** mode would require:

- explicit `routing.cache_aware = true` config flag
- same-provider fairness controls (preserve rotor behaviour)
- per-provider support detection
- cost model using cached-read / cached-write token prices
- backtesting metrics on representative traffic
- per-client opt-in
- dashboard warnings that surface the mode change

Phase 8 deliberately does not implement any of these.  The existence
of this note prevents accidental partial implementation.  See
`plans/cache_compression_phase_08_routing_guardrails.md` for the
full design.

## Synthetic Cache Controls (Phase 9)

Phase 9 layers opt-in synthetic `cache_control` annotations onto
the provider-bound body for providers that support explicit cache
boundary hints (initially Anthropic-style).  The selector and
mutator run inside `RequestCoordinator._apply_synthetic_cache_controls`
AFTER account selection and provider-bound transcoding, so the
feature sees `context.upstream_protocol` (not `endpoint.protocol`).
OpenAI clients routed to Anthropic providers are supported because
the selector sees the actual upstream protocol.

When enabled (and not in dry-run), the mutator annotates supported
stable-prefix containers so the upstream cache can reuse them across
requests.  Dry-run is the default when enabled so operators can
observe the plan without changing wire bodies.

### Key invariants

- **Post-route, provider-bound**: the selector operates on
  provider-bound segmentation computed after account selection and
  transcoding.  Only protected `stable_prefix` segments from
  `SYSTEM`, `DEVELOPER`, or `TOOL_SCHEMA` sources are eligible.
  Volatile suffix and compressed content are never annotated.
- **Native preservation**: existing native `cache_control` annotations
  are preserved byte-for-byte and never duplicated.  Path
  representation is normalized internally to `tuple[str | int, ...]`
  so candidates and native-preservation checks use the same form.
  Display strings are generated only in summary JSON.
- **TTL is explicit**: only `ttl = "ephemeral"` is currently accepted.
  `5m` and `1h` are reserved and rejected at config load.
- **Structural-diff safety**: apply mode validates the mutated payload
  only differs by added `cache_control` keys at candidate containers.
  Any unexpected change triggers `failed_fallback` and preserves the
  original payload.

### Routing non-interference

The `QuotaFairScorer` does NOT consume synthetic cache fields.
Routing stays load-based: request count + token count + active
count + health.  The synthetic cache selector is content-private --
it never reads prompt text, tool outputs, or system messages.
Per-policy overrides ride on the Phase 6 `[[compression.policies]]`
via `synthetic_cache_*` overlay fields resolved by
`resolve_compression_policy`.  `_overlay_config()` skips
synthetic-cache fields so a policy row containing only synthetic-cache
overrides does not trigger compression config validation warnings.

### Structural-diff tightening (Phase 10/11 review pass)

The coordinator's structural-diff safety check uses `_validate_synthetic_cache_diff` (in `src/eggpool/transcoder/cache_synthesis.py`) which validates added `cache_control` paths against the candidate container paths rather than merely checking that the path's last component is `cache_control`. This prevents the mutator from annotating non-candidate containers. `resolve_selected_provider_kind` falls back from `catalog.providers` to `config.providers[provider_id].kind` for config-backed providers when catalog metadata is missing.

### Code references

- `src/eggpool/transcoder/cache_synthesis.py` -- `run_synthetic_cache_synthesis`, `_validate_synthetic_cache_diff`
- `src/eggpool/transcoder/cache_synthesis_policy.py` -- `CacheConfig`
- `src/eggpool/transcoder/compression/policy_resolver.py` -- `synthetic_cache_overrides`, `_overlay_config()`
- `src/eggpool/request/coordinator.py` -- `_apply_synthetic_cache_controls`
- `src/eggpool/models/config.py` -- `ProviderConfig.kind`
- `src/eggpool/db/schema/0045_synthetic_cache_controls.sql` -- migration

## Closed-Loop Threshold Tuning (Phase 10)

Phase 10 adds a side-effect-free recommendation engine that observes
Phase 4-6 compression metrics and emits bounded suggestions for the
three tunable thresholds:

- `min_candidate_tokens`
- `min_savings_tokens`
- `max_compression_latency_ms`

Phase 10 is currently **recommendation-only**.  `mode = "apply"` is
accepted at config time for forward compatibility but is dormant: no
production code path registers runtime overrides.  A future
supervised background task must call `build_runtime_override()` then
`registry.register()` before apply mode takes effect.  Until then,
`compute_recommendation` always tags recommendations as
`recommendation_only`.

The engine is disabled by default.  When `[compression.tuning] mode`
is `recommend` (the default), the engine writes advisory
recommendations to the `compression_tuning_recommendations` table and
the dashboard; request behaviour never changes.  The in-memory
`RuntimeCompressionPolicyOverrideRegistry` and `apply_runtime_override`
helper exist for forward compatibility but no production code path
registers entries yet.

### Safety rails

- The algorithm is **content-private**: it never inspects raw
  prompts, tool outputs, system messages, or request bodies.  The
  only inputs are per-policy aggregates of Phase 4-6 columns.
- The algorithm is **bounded**: every suggested value is clamped to
  `[field_min, field_max]` from `[compression.tuning.bounds]`.  No
  override can escape these ranges even in `apply` mode.
- The algorithm is **rate-limited**: per-step deltas are capped by
  `max_adjustment_pct`, and a second recommendation within
  `cooldown_seconds` of the previous one is suppressed with
  `REASON_COOLDOWN_ACTIVE`.
- The algorithm never enables `compress_static_prefix`, never toggles
  `mode`, never adds or removes transforms, and never modifies the
  synthetic cache knobs.  Only the three tunable thresholds move.
- The runtime override is fail-closed: a malformed entry (unknown
  field, invalid value, validation error) is dropped and recorded in
  `runtime_override_metadata.dropped_fields` without affecting the
  previous config.

### Routing non-interference

The `QuotaFairScorer` does NOT consume advisory tuning state.  Routing
stays load-based: request count + token count + active count +
health.  Advisory tuning only mutates compression thresholds; it never
inspects the request body, never picks an account, never alters the
health feed.  The `QuotaFairScorer.score_accounts` signature is
unchanged.  `ResolvedCompressionPolicy.runtime_override_metadata`
is operator-visible only; it does not flow into scorer inputs.

### Code references

- `src/eggpool/transcoder/compression/tuning.py` -- `compute_recommendation`,
  `apply_runtime_override`, `RuntimeCompressionPolicyOverrideRegistry`
- `src/eggpool/transcoder/compression/policy.py` -- `CompressionTuningConfig`,
  `CompressionTuningTargetsConfig`, `CompressionTuningBoundsConfig`
- `src/eggpool/transcoder/compression/policy_resolver.py` --
  `resolve_compression_policy(runtime_override_registry=...)`
- `src/eggpool/db/schema/0046_closed_loop_threshold_tuning.sql` -- migration
- `src/eggpool/app.py` -- `app.state.compression_tuning_registry`
- `src/eggpool/api/proxy_request.py` -- resolver call site
- `src/eggpool/stats/queries.py` -- `fetch_compression_tuning_window_metrics`
  and persistence helpers
- `src/eggpool/stats/service.py` -- `get_compression_tuning_window_metrics`
- `src/eggpool/api/stats.py` -- `/api/stats/compression-tuning` endpoint
- `src/eggpool/dashboard/routes.py` and `src/eggpool/dashboard/render.py`
  -- runtime card and JSON handler

## Replay Fixtures and Regression Harness (Phase 11)

Phase 11 is the test-only layer that pins down the high-risk Phase 2/3/5/9 behaviour without ever shipping a real prompt to disk.  The harness lives entirely under `tests/` and never enters the production code path.

### What ships

- **Fixture tree** -- `tests/fixtures/cache_compression/` with five subdirectories:
  - `openai/` (6 fixtures): `simple_stable_prefix`, `repeated_tool_output`, `large_search_results`, `base64_blob`, `stack_trace`, `mixed_native_cache_like_fields`.
  - `anthropic/` (6 fixtures): `system_blocks_native_cache`, `tool_schema_native_cache`, `tool_result_string_large`, `tool_result_nested_text_large`, `thinking_block_protected`, `synthetic_cache_candidates`.
  - `transcode/` (2 fixtures): `openai_client_to_anthropic_provider` (preserves `cache_control` on `tools[0]`), `anthropic_client_to_openai_provider` (drops unsupported nested `cache_control`).
  - `routing/` (2 fixtures): `same_provider_two_accounts_equal_load` (two same-provider accounts with identical load but adversarial cache/compression metrics for routing-guardrails testing), `adversarial_cache_metrics` (per-account skew that pins scoring isolation).
  - `stats/` (1 fixture): `request_rows_phase_1_to_10` (synthetic rows covering Phases 1-10; verifies stats never leak raw prompts).
- **Sentinel strings** -- every fixture uses one of seven sentinels (`SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS`, `TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS`, `VOLATILE_LOG_LINE`, `STACK_TRACE_SENTINEL`, `SYNTHETIC_BASE64_BLOB`, `LONG_USER_INSTRUCTION`, `LATEST_USER_SENTINEL`) so the sanitization linter can prove no real prompt text leaked in.
- **Compact repeat spec** -- `expand_repeats()` lets fixtures declare repeating blocks (e.g., 32 volatile log lines) without writing thousands of lines of JSON.
- **Deterministic policies** -- `safe_policy`/`observe_policy`/`disabled_policy` mirror the canonical Phase 4-6 configs without the operator config surface.
- **`ReplayBundle` dataclass** -- summarises the structural outcome (segmentation status, stable-prefix hash pre/post, compression transforms by reason code, synthetic cache status, transcoder cache boundary tracker snapshot, and the replay shape used) without leaking raw payloads.  Failure paths emit fixture name + status delta, never raw prompt text.
- **Replay shape semantics** -- `run_full_replay()` records which replay shape was used via `ReplayBundle.synthetic_cache_shape` (`disabled` / `client_bound` / `provider_bound` / `provider_bound_unavailable`). When `client_protocol != target_protocol` and a `synthetic_cache` config is supplied, the helper runs transcode first and synthetic cache against the **provider-bound** body -- mirroring production Phase 9 (`_apply_synthetic_cache_controls`) which runs post-route. Provider-bound observability fields (`provider_bound_segmentation_status`, `provider_bound_synthetic_cache_status`, `provider_bound_synthetic_cache_candidate_count`) live alongside the client-shape fields on the bundle.
- **Helper API** -- `load_fixture`, `expand_repeats`, `run_full_replay`, `run_provider_bound_synthetic_replay` (explicit provider-bound lifecycle for transcode fixtures), `run_segmentation`, `run_compression`, `run_transcode`, `run_synthetic`, `path_keys`, `collect_segment_strings`.

### Regression suite

`tests/unit/test_replay_fixtures_regression.py` ships with 13 test classes covering:

1. **TestFixtureTreeStructuralInvariants** -- every fixture loads, segment+compress+transcode+synthetic pipeline runs end-to-end, stable-prefix hash is recomputed and matches.
2. **TestSegmentationInvariants** -- canonical-segmenter invariants per fixture (paths resolve, stable prefix protected, Anthropic tool_result segments resolve). Runs outside the full mark so it ships in default pytest.
3. **TestSafeCompressionReplay** -- safe compression never mutates `stable_prefix` content (hash unchanged before/after); OpenAI repeated tool output applies transforms; Anthropic nested tool result compresses; disabled/observe modes never mutate.
4. **TestSyntheticCacheReplay** -- synthetic cache candidates come from provider-bound segmentation (post-transcode), not client-bound segmentation; native `cache_control` is preserved verbatim; apply mode never duplicates cache_control on the same container.
5. **TestTranscoderCacheStability** -- openai -> anthropic preserves `cache_control` on tool definitions, anthropic -> openai drops unsupported nested `cache_control`, and transcoder never mutates unintended fields.
6. **TestReplayStructureInvariants** -- cross-cutting hash invariants and a paths-only contract that bundle fields never expose raw prompt text.
7. **TestSyntheticCacheCoexistWithCompression** -- synthetic apply mode does not corrupt the post-compression payload.
8. **TestFailClosedFallback** -- structural-diff mismatch triggers `failed_fallback=True` and preserves the original payload.
9. **TestRoutingGuardrailsReplay** -- routing fixture has required adversarial fields; `inspect.getsource(QuotaFairScorer.score_accounts)` and `inspect.getsource(RuntimeMetricsService._snapshot_routing_runtime)` prove the scorer signature is the canonical 4-tuple and the guardrails dict still pins `routing_uses_*` to `false`.
10. **TestStatsReplay** -- compaction summaries exclude prompt text, `CompressionResult.summary_json` is content-private across every fixture, and the stats fixture rows are content-private.
11. **TestSyntheticCacheFailurePaths** -- apply mode does not bleed `cache_control` into Anthropic message content blocks.
12. **TestFixtureSuppliedExpectations** -- per-fixture `expectations` block drives assertions against the bundle (segmentation status, stable_prefix_contains, volatile_suffix_contains, compression transforms).
13. **TestHarnessSurfaceSanity** -- default fixture root, bare-name load, `expand_repeats` immutability, `synthetic_cache_config` defaults.

Phase 12 polish pass adds two more test classes (`TestProviderBoundSyntheticReplay` and `TestReplaySmoke`); see § Phase 12 polish pass (replay-shape and default smoke coverage) below.

### Sanitization linter

`tests/unit/test_replay_fixtures_sanitization.py` enforces content privacy:

- No bearer tokens (`Bearer `), `sk-...` keys, or `Authorization:` lines.
- No oversized strings (>5 KB leaves), no real prompt text (sentinel coverage).
- Synthetic IDs follow the `synthetic_(call|id|use)_[A-Za-z0-9_]+` pattern.
- Unique fixture names across all subdirectories.

### Routing non-interference

Phase 11 is reporting-only.  The harness is invoked from pytest fixtures and never touches the routing layer, the database, or the dashboard.  `QuotaFairScorer.score_accounts` signature is unchanged from Phase 8.  No Phase 11 columns are added to the database and no migrations are required.  Same-provider account fairness (e.g., multiple OpenAI subscriptions) is preserved because cache hit ratios, compression savings, stable-prefix hashes, synthetic-cache status, and tuning state never enter the scorer inputs.

### Code references

- `tests/fixtures/cache_compression/` -- 17 sanitized JSON fixtures + `README.md`
- `tests/helpers/cache_compression_replay.py` -- harness (load_fixture, expand_repeats, ReplayBundle, run_* helpers, safe_policy/observe_policy/disabled_policy, synthetic_cache_config, path_keys, collect_segment_strings, run_provider_bound_synthetic_replay)
- `tests/unit/test_replay_fixtures_regression.py` -- 13 regression test classes + standalone function
- `tests/unit/test_replay_fixtures_sanitization.py` -- 8 sanitization linter tests
- `src/eggpool/transcoder/__init__.py` -- public exports used by the harness
- `plans/cache_compression_phase_11_replay_fixtures_regression_tests.md` -- design plan

### Phase 12 polish pass (replay-shape and default smoke coverage)

Phase 12 polish pass addresses two quality gaps discovered in the post-Phase-12 review of the harness:

1. **`run_full_replay()` previously ran synthetic cache against the client-shape payload even for transcode fixtures.** Production Phase 9 (`_apply_synthetic_cache_controls`) runs post-route against the provider-bound body using `context.upstream_protocol`. The polish pass aligns the harness:
   - When `client_protocol != target_protocol`, `run_full_replay()` runs transcode first and synthetic cache against the provider-bound body.
   - A dedicated `run_provider_bound_synthetic_replay()` helper is exposed for callers that need an explicit provider-bound lifecycle.
   - `ReplayBundle.synthetic_cache_shape` records the shape used (`disabled` / `client_bound` / `provider_bound` / `provider_bound_unavailable`), and `provider_bound_*` observability fields are populated on the bundle for the transcode path.
2. **High-value replay invariants were gated behind the `cache_compression_replay_full` mark.** A `TestReplaySmoke` class promotes six cheap invariants outside the marker so default `pytest` exercises the most important cache/compression guarantees on every PR: OpenAI prefix preservation, Anthropic nested-tool-result compression, provider-bound synthetic dry-run, native-cache preserve-apply, scoring guardrails, and a sentinel-linter smoke pass.

A `TestProviderBoundSyntheticReplay` class pins the provider-bound contract: dry-run must not mutate the client or provider body, apply mode only mutates the provider body, native `cache_control` survives apply, and bundle fields never expose the provider-bound payload.

No production request-shaping behavior changes. The `QuotaFairScorer` does not consume any Phase 12 polish pass fields. Routing stays load-based.

### Code references (Phase 12 polish pass)

- `tests/helpers/cache_compression_replay.py` -- `run_full_replay` now exercises provider-bound synthetic cache for transcode fixtures; `run_provider_bound_synthetic_replay` exposes the explicit provider-bound lifecycle; `ReplayBundle` carries `synthetic_cache_shape` + `provider_bound_*` fields
- `tests/unit/test_replay_fixtures_regression.py` -- `TestProviderBoundSyntheticReplay` and `TestReplaySmoke` test classes
- `tests/fixtures/cache_compression/README.md` -- Replay shape semantics section
- `plans/cache_compression_phase_12_polish_pass.md` -- design plan

## Operator Documentation, Profiles, and Rollout (Phase 12)

Phase 12 closes the gap between the cache-preserving deterministic compression primitives and operator usability. The runtime surface is unchanged; this phase ships documentation, six copy-pasteable config profiles, a dashboard interpretation reference, a symptom-to-cause troubleshooting guide, and a conservative rollout plan. Phase 6 (UI copy pass) standardized dashboard labels: "Provider cache counters", "Compression", "Compression — safe-mode details", "EggPool cache annotations", "Tuning suggestions", and "Routing isolation". Config keys and API field names are unchanged.

### Documentation surface

- `docs/cache-compression.md` -- ten-step operator model, what is safe by default, what is experimental, what never affects routing, privacy invariants, config validation notes, rollout summary, rollback.
- `docs/cache-compression-profiles.md` -- six profiles (baseline / observe-only / safe suffix / synthetic cache dry-run / synthetic cache apply / tuning recommendation-only) with the dashboard fields to watch and the JSON endpoint that surfaces them.
- `docs/cache-compression-troubleshooting.md` -- dashboard interpretation reference (every counter, status, and warning code on every Phase 1-11 endpoint) plus a symptom-to-cause guide for the common no-op and fallback cases.

### Profiles (config-only)

Each profile is a self-contained TOML snippet:

1. **Baseline / disabled** -- `[compression] enabled = false`, synthetic cache `enabled = false`, tuning `enabled = false`. No mutation. Phase 1-4 observability continues.
2. **Observe-only diagnostics** -- `[compression] enabled = true, mode = "observe"`. Analyzer runs on every request, no mutation. Phase 4 counters persist.
3. **Safe suffix compression for coding agents** -- `[compression] enabled = true, mode = "safe"`. Six transforms fire on eligible volatile-suffix segments. Stable-prefix content hash is recomputed; mismatch triggers fail-closed fallback.
4. **Anthropic synthetic cache dry-run** -- `[cache.synthetic_cache_controls] enabled = true, dry_run = true` plus a matching `[[compression.policies]]` row. Post-route selector computes a plan but does not mutate.
5. **Anthropic synthetic cache apply mode** -- same as Profile 4 with `dry_run = false`. Structural-diff safety (`_validate_synthetic_cache_diff`) gates every mutation.
6. **Threshold tuning recommendation-only** -- `[compression.tuning] enabled = true, mode = "recommend"`. Recommendations are advisory; `mode = "apply"` is accepted but currently dormant.

### What is safe by default

With shipped defaults the entire stack is observability-only. No request body, header, or route is altered:

- Phase 1 cache counters recorded, never affects quota scoring.
- Phase 2 segmentation annotates durable columns without inspecting prompts.
- Phase 3 cache stability records boundary events on the in-memory `CacheBoundaryTracker`.
- Phase 4 observe mode runs the analyzer on every request but never mutates.
- Phase 5 safe compression defaults to `mode = "observe"`.
- Phase 6 policy overrides default to `policies = []`.
- Phase 9 synthetic cache defaults to `enabled = false`.
- Phase 10 threshold tuning defaults to `enabled = false`.

### What is experimental

These ship behind explicit operator opt-in:

- Phase 5 `mode = "safe"` -- actually mutates eligible volatile-suffix segments. Even then, the applier fails closed on any stable-prefix mismatch.
- Phase 9 synthetic cache `apply` mode -- adds `cache_control` annotations to provider-bound Anthropic requests. Dry-run is the default when enabled. Apply mode requires a matching policy row by default (`require_policy = true`).
- Phase 10 `mode = "apply"` -- accepted at config time but currently dormant. No production code path registers runtime overrides today.

### What never affects routing

The invariant from Phase 8 holds. `QuotaFairScorer.score_accounts` accepts only `account_names`, `model_name`, `active_requests`, `request_estimates`. `GET /api/stats/runtime` exposes a `guardrails` dict with hardcoded constants (`routing_cache_compression_mode: "reporting_only"`, `routing_uses_cache_metrics: false`, `routing_uses_compression_metrics: false`, `routing_uses_stable_prefix_hash: false`, `routing_uses_compression_policy: false`, `routing_uses_synthetic_cache: false`, `routing_uses_compression_tuning: false`). Same-provider account fairness is preserved because cache hit ratios, compression savings, synthetic-cache status, and tuning state never enter the scorer inputs.

### Privacy invariants

No raw prompt, tool output, system message, request body, auth header, or provider API key is ever shown or persisted in any cache, compression, or synthetic-cache surface. The replay harness uses seven sentinel strings (`SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS`, `TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS`, `VOLATILE_LOG_LINE`, `STACK_TRACE_SENTINEL`, `SYNTHETIC_BASE64_BLOB`, `LONG_USER_INSTRUCTION`, `LATEST_USER_SENTINEL`) so `tests/unit/test_replay_fixtures_sanitization.py` can prove no real prompt text leaked in. Phase 12 documentation re-states this invariant verbatim on every page so operators do not have to reconstruct it from plan files.

### Config validation notes (documented for operators)

- Synthetic cache `ttl = "ephemeral"` is the only currently accepted value. `5m` and `1h` are reserved and rejected at config load.
- `compress_static_prefix = true` in any non-default policy override is rejected unless `allow_static_prefix_override = true` is set globally.
- Tuning `mode = "apply"` is accepted at config time but currently behaves like `recommend` (no production code path registers runtime overrides today).
- `compress_static_prefix = false` is the normal setting.
- Context-limit checks happen before compression. Compression cannot rescue over-limit requests.
- Default `max_breakpoints = 4` (Anthropic's documented limit).

### Rollout guide (conservative staging)

1. **Baseline disabled** -- confirm only Phase 1-4 observability is recorded.
2. **Observe-only compression** for 24-48 hours. Inspect candidate rate, estimated savings, analyzer latency, suppression reasons.
3. **Safe suffix compression** for one client/policy. Inspect stable-prefix preserved count and failed fallback count.
4. **Expand safe compression** to additional clients if stable.
5. **Synthetic cache dry-run** for Anthropic providers only. Inspect candidate/applied dry-run counts and native-preserved warnings.
6. **Synthetic apply mode** for one Anthropic provider/client only if dry-run is clean.
7. **Tuning recommendation-only** mode if operator wants threshold advice.

### Rollback

Operator rollback is a documented config-only change. No schema rollback is required; added columns and audit tables are additive:

```toml
[compression]
enabled = false
mode = "observe"

[cache.synthetic_cache_controls]
enabled = false
dry_run = true

[compression.tuning]
enabled = false
mode = "recommend"
```

After editing, run `eggpool rehash` to validate the new config. Then run `eggpool restart` to apply changes.

### Routing non-interference

The routing non-interference rollout is documentation-only. No new
columns are added to the database and no migrations are required.
`QuotaFairScorer.score_accounts` is unchanged. The
`RuntimeCompressionPolicyOverrideRegistry` is still dormant; no
production code path registers entries. Same-provider account
fairness (e.g., multiple OpenAI subscriptions) is preserved because
the docs explicitly state cache hit ratios, compression savings,
synthetic-cache status, and tuning state never enter the scorer
inputs.

### Code references

- `docs/cache-compression.md` -- main operator guide
- `docs/cache-compression-profiles.md` -- six copy-pasteable profiles
- `docs/cache-compression-troubleshooting.md` -- dashboard interpretation and symptom-to-cause
- `config.example.toml` § synthetic cache and advisory tuning commented blocks -- equivalent verbose config
- `src/eggpool/_share/config.example.toml` -- pipx-install copy
- `src/eggpool/api/stats.py` -- `/api/stats/cache-observability`, `/api/stats/canonical-request-segmentation`, `/api/stats/cache-stability`, `/api/stats/compression-observability`, `/api/stats/compression-runtime`, `/api/stats/compression-policies`, `/api/stats/synthetic-cache-observability`, `/api/stats/compression-tuning`
- `src/eggpool/dashboard/render.py` -- request-shaping cards on `/cache` page (`compression`, `compression_runtime`, `compression_policy`, `cache_stability`) plus synthetic cache and tuning cards
- `plans/cache_compression_phase_12_operator_docs_profiles.md` -- design plan

## Update Checker — freshness and CLI install-decision isolation

`src/eggpool/update_checker.py` exposes two paths:

- **`UpdateChecker`** — the background/periodic probe registered via `TaskSupervisor.register_periodic("update_checker", ...)`. It caches the latest `UpdateInfo` so the dashboard footer indicator and `/api/stats/update` reflect a recent state without hitting PyPI on every request. The background probe reuses the shared outbound `httpx.AsyncClient` and is intentionally conservative (no freshness bypass) to keep PyPI traffic minimal.
- **`async_check_for_update()`** — the CLI one-shot helper used by `eggpool update`. It performs its own live PyPI lookup and must NEVER read `UpdateChecker.snapshot()` — a stale dashboard cache could mask a real newer release and cause the operator to skip the update.
- **`check_exact_release()`** and **`normalize_requested_version()`** — the explicit-version path used by `eggpool update VERSION`. It validates one supported version, checks the exact PyPI release endpoint, and returns the canonical release without consulting the dashboard cache.

The CLI accepts `eggpool update` for the existing latest-release behavior and
`eggpool update VERSION` (including `vVERSION`) for an exact published release.
Exact package installs pin `eggpool==VERSION` and may downgrade; exact
`--from-source` installs pin the Git tag. A source checkout refuses exact
targeting because changing local checkout state safely requires operator
decisions. After an exact install, the distribution version must match the
canonical PyPI target before the server can restart.

### Freshness-aware CLI lookup

The CLI helper guards against stale CDN/PyPI responses by:

1. Sending a fresh request with `Cache-Control: no-cache, max-age=0`, `Pragma: no-cache`, `Accept: application/json`, and a distinct `User-Agent: eggpool/update-check`.
2. Appending a cache-bust query parameter (`_cb=<monotonic_ns>`) to the PyPI URL via `httpx` parameter handling.
3. If the first fresh response reports `latest <= current`, performing **one additional** fresh PyPI request with a different cache-bust token before concluding `Already up to date.`. The second fetch is bounded so a stale CDN response can no longer cause the operator to miss a real newer release; the failure path (second fetch errors out) falls back to the first response so transient failures are not escalated.
4. Comparing versions through `is_newer_version(current, latest)` (module-level public helper backed by `_pep440_key`) so tags like `0.5.10`, `0.5.9.post1`, and `0.5.9rc1` order correctly. Both the CLI and the background checker go through this helper — raw string equality was a footgun because `0.5.10` and `0.5.9` compare lexicographically the wrong way for the dashboard's `update_available` decision.

`_fetch_pypi_response_sync()` accepts `fresh: bool = False` and `cache_bust_token: str | None = None`; the background checker passes neither, keeping its traffic profile unchanged.

### Dashboard footer centering

The footer indicator markup is wrapped in `<span class="footer-update-indicator">` (added 0.5.10) so CSS can center the pill without disturbing the surrounding period/refresh/ready controls. The centering rule (`display: flex; justify-content: center; align-items: center; flex-wrap: wrap; width: 100%`) scopes to the new wrapper only — the rest of the footer keeps its existing left-aligned layout. The contract "render nothing when `update_available` is false" is preserved.

### Test pin surface

- `tests/unit/test_update_checker.py::TestAsyncCheckForUpdateFreshness` — covers fresh headers, cache-bust tokens, stale-first/fresh-second, single-fetch-when-newer, error passthrough, and the public `is_newer_version` helper.
- `tests/unit/test_update.py::TestUpdateFreshness` — covers the CLI integration: stale-first/fresh-second `--check`, both-current `--check`, subprocess-once behavior, and the invariant that the CLI does not consult `UpdateChecker.snapshot()`.
- `tests/unit/test_dashboard.py::TestFooterUpdateIndicatorCentering` — covers the wrapper span, the CSS centering rule, and the "do not center the whole footer" guard.

## Database

SQLite via aiosqlite with WAL mode. Single-connection serialization via a lock + ContextVar.

### Key Invariants

- Every DML write must run inside `async with db.transaction():`
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Child tasks cannot inherit transaction ownership

### Schema Migrations

Ordered SQL migrations in `db/schema/` (0001 through 0051). Checksums tracked in `checksums.json`.

### Repositories

| Repository | Purpose |
|------------|---------|
| `AccountRepository` | Account CRUD, config sync |
| `RequestRepository` | Request lifecycle (pending → selected → completed) |
| `ReservationRepository` | Quota reservations with release/reconciliation |
| `AttemptRepository` | Per-request attempt tracking |
| `UsageWindowRepository` | Aggregated cost queries (5h/7d/30d) |
| `PriceSnapshotRepository` | Model price snapshots |
| `ProviderRepository` | Provider CRUD and config sync |
| `PingRepository` | Provider health ping results |
| `AccountBackoffRepository` | Upstream-derived backoff persistence |
| `AccountEventRepository` | Account event logging |
| `OperationalEventRepository` | Safety-net task event logging |
| `RoutingDecisionRepository` | Routing decision persistence |

## Quota and Routing

Routing happens in two stages: a *priority grouping* step picks the highest
non-empty tier of providers, then a `QuotaFairScorer` load-balances inside
that tier.

The grouping step partitions eligible `AccountRuntimeState` records by their
provider's `routing_priority` (default `0`, must be `>= 0`). The router
selects the highest-priority tier that contains at least one eligible account;
if every account in that tier becomes unhealthy, exhausted, or fails pre-body,
the request falls through to the next tier. The `QuotaFairScorer` runs
unchanged against the accounts of the chosen tier, balancing across:

- Quota utilization across 5h/7d/30d windows
- In-flight request penalty
- Health penalty for degraded accounts
- Random tie-breaking for near-equal scores

The `weight` field continues to bias scoring inside a single tier. `weight`
orders accounts within a tier; `routing_priority` orders tiers.

**Cache/compression metrics NEVER enter routing.** See
[Routing Guardrails and Non-Interference (Phase 8)](#routing-guardrails-and-non-interference-phase-8)
for the runtime diagnostic, the four-input scorer contract, and the
regression suite that pins the invariant.  Same-provider account
fairness (e.g., multiple OpenAI subscriptions) is preserved because
cache hit ratios and compression savings cannot influence selection.

Accounts are excluded from routing when:
- Upstream-observed failure (`quota_exhausted`, `rate_limited`, auth, 5xx) is still inside its bounded backoff window (recovers after cooldown)
- Account is explicitly disabled or suspended by the operator
- Model is not supported by the account (catalog/protocol incompatibility)
- Health circuit breaker is open
- `local_quota_mode = "hard_cap"` is enabled AND local estimate exceeds capacity (opt-in legacy behavior; default is `score_only` advisory)

In the default `score_only` mode, local cost and quota estimates influence
routing **priority** only — above-capacity accounts stay eligible. Only
upstream-observed failures, explicit operator disablement, and catalog/
protocol incompatibility can suppress routing.

Upstream-derived backoffs (429, 402, model-unavailable) persist across
restarts in the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`)
and are rehydrated into the in-memory `HealthManager` at startup.
Local-estimate overage never produces a backoff row.

A single request still picks one upstream account. Failover across priority
tiers happens only through the existing `exclude_accounts` retry path.
When every candidate account has been attempted and exhausted mid-request,
the coordinator raises `UpstreamExhaustedError` (502) — synthetic 503 is
reserved for genuine pre-dispatch unavailability (no enabled accounts,
missing credentials, all explicitly disabled, model unknown).

### Same-Tier Fairness

EggPool is not purely lowest-score-wins for same-tier peer accounts. When
accounts are effectively tied by priority, weight, health, protocol, and
utilization score, same-tier fairness rotates candidates to avoid stable
config-order bias and subscription starvation.

When multiple accounts share the same `routing_priority`, weight, transcode
status, and have scores within `fairness_epsilon` of the best (default:
`near_tie_epsilon`), they are considered *same-tier peers*. Without fairness
intervention, stable config order or minor score noise can cause severe
routing skew (one account receiving nearly all traffic).

EggPool applies a deterministic round-robin rotor
(``FairnessRotor`` in ``src/eggpool/routing/fairness.py``) to the
*fairness band* — the set of tied peers within a single priority tier.
The rotor maintains an in-memory position counter per fairness key
(provider × model × protocol × priority × client_protocol) and rotates
the candidate list so the first-selected account advances on each
routing decision.

The rotor's position map is capped at 4096 entries
(``_ROTOR_HARD_CAP``).  When the cap is reached the entire map is
cleared and rotation restarts from position 0 for all keys.  This is a
blunt eviction strategy — there is no LRU or partial eviction.  The cap
prevents unbounded memory growth when model IDs or fairness keys vary
heavily.

Fairness is controlled by three ``[routing]`` config fields, all honored
by the server runtime:

- ``fairness_mode``: ``"round_robin"`` (default), ``"random"``, or ``"off"``.
- ``fairness_epsilon``: score proximity threshold; defaults to ``near_tie_epsilon``
  when omitted.
- ``fairness_scope``: rotation group granularity — ``"provider_model_protocol"``
  (default), ``"provider_model"``, or ``"priority_model_protocol"``.

Scope semantics for the fairness key:

- ``provider_model_protocol``: key includes provider, model, routed protocol,
  priority tier, and client protocol. Separate rotor per provider/model/protocol
  group. This is the default and recommended scope for subscription aggregation.
- ``provider_model``: key includes provider, model, priority tier, and client
  protocol; protocol is intentionally excluded so OpenAI and Anthropic traffic
  for the same model collapses into one rotation group.
- ``priority_model_protocol``: key excludes provider but includes model, routed
  protocol, priority tier, and client protocol. Co-balances accounts from
  different providers serving the same model in the same priority tier.

The fairness band is extracted *after* quota scoring and *before* the
coordinator selects the first circuit-breaker-accepted candidate. Priority
tier boundaries remain strict: lower-priority accounts never advance ahead
of higher-priority eligible accounts. Different-weight accounts opt out of
equal-peer rotation; the band requires identical weights within floating-point
tolerance.

Fairness decisions are recorded in ``routing_decisions.score_components_json``
under the ``fairness`` key for operator diagnostics:

```json
{
  "fairness": {
    "mode": "round_robin",
    "applied": true,
    "scope": "provider_model_protocol",
    "key": "provider=opencode-go|model=gpt-4|protocol=openai|tier=0|client_protocol=openai",
    "candidate_count": 3,
    "selected_index": 0,
    "selected_account_name": "0002",
    "reason": "ok"
  }
}
```

The ``top_candidates`` array in the same payload carries per-candidate
fairness annotations:

- ``rank_before_fairness``: candidate's position in the score-ordered list
  before the fairness rotor reordered the band.
- ``rank_after_fairness``: candidate's position in the final list.
- ``fairness_band_member``: ``true`` when the candidate was part of the
  fairness band eligible for rotation.

#### Diagnosing routing skew

When skew persists after deploying the fairness patch, run:

```bash
eggpool accounts explain --model '<hot-model>' --protocol openai --scores
```

Then inspect recent routing decisions:

```sql
SELECT
  selected_account_name,
  eligible_count,
  scored_count,
  top_score_account_name,
  selected_score,
  json_extract(score_components_json, '$.fairness.mode') AS fairness_mode,
  json_extract(score_components_json, '$.fairness.applied') AS fairness_applied,
  json_extract(score_components_json, '$.fairness.scope') AS fairness_scope,
  json_extract(score_components_json, '$.fairness.reason') AS fairness_reason,
  json_extract(score_components_json, '$.fairness.candidate_count') AS fairness_candidates
FROM routing_decisions
ORDER BY id DESC
LIMIT 50;
```

Interpretation:

- ``fairness_applied = true`` with ``fairness_candidates = 3``: fairness is
  working; check that the distribution is approximately balanced.
- ``fairness_applied = false`` with ``reason = not_tied``: scores diverge
  beyond ``fairness_epsilon``; the skew is driven by score policy, not
  config order.
- ``fairness_applied = false`` with ``reason = different_weights``: accounts
  have unequal weights; adjust weights to match if equal peer rotation is
  desired.
- ``eligible_count = 1`` or ``scored_count = 1``: this is not a fairness
  problem; accounts are excluded by catalog, health, or quota policy.
- ``fairness_applied = false`` with ``reason = disabled``: ``fairness_mode``
  is set to ``"off"``; switch to ``"round_robin"`` to enable rotation.

### Lock scope and publish ordering

#### Milestone B: selection-claim lock deconvoying

`RequestCoordinator._select_and_persist_attempt()` is split into three
phases so database I/O can never convoy other selectors through the
selection critical section. The narrow
`RequestCoordinator._selection_claim_lock` (added in dispatch-stability
milestone B) replaces the previous broad `_select_lock` with two
acquisitions per attempt:

1. **Phase A** — first acquisition of `_selection_claim_lock`. The
   coordinator probes the circuit breaker (`SPAN_CIRCUIT_PROBE`) and
   resolves the per-attempt identity
   (`SPAN_ACCOUNT_LOOKUP`): API key, account id, provider id,
   reservation cost. Account IDs/provider IDs come from the immutable,
   generation-hydrated identity map; a cache miss never creates a
   repository or awaits SQLite under this lock. The result is captured
   into a frozen `_ClaimIdentity` dataclass and the lock releases.
2. **Phase B** — durable commit, OUTSIDE the lock.
   `_persist_dispatch_bundle` opens its own
   `async with self._db.transaction():` and inserts the request,
   reservation, and attempt rows. Because the coordinator lock is
   not held here, a SQLite waiter can no longer convoy other
   selectors; the broader `_select_lock` (if any concurrent reader
   uses it) stays free. The helper reports
   `SPAN_DISPATCH_PERSISTENCE_WAIT` /
   `SPAN_DISPATCH_PERSISTENCE_TRANSACTION` /
   `SPAN_DISPATCH_PERSISTENCE_COMMIT`.
3. **Phase C** — second acquisition of `_selection_claim_lock`. The
   coordinator publishes runtime state
   (`_publish_runtime_state` → `Router.increment_active_request_count`
   + `QuotaEstimator.add_reservation`, wrapping
   `SPAN_RUNTIME_PUBLICATION` and the new
   `SPAN_POST_COMMIT_PUBLICATION`) and the lock releases. The
   `attempted_accounts` set is recorded while the lock is held so a
   concurrent selector entering Phase A next observes this attempt's
   runtime state and the freshly-stamped attempted-account history.

`SPAN_SELECTION_CLAIM_HELD` is recorded once per acquisition via the
`_maybe_span` placeholder. The legacy `SPAN_SELECTION_LOCK_WAIT` /
`SPAN_SELECTION_LOCKED` spans continue to fire (recorded once at the
end of the call) so historical dashboards stay comparable, but the
new selection-claim spans are the authoritative timing source for
operators looking to spot a contended lock on a hot path.

The compensation chain (`_compensate_or_rollback_claim` →
`decrement` → finalize-as-cancelled → release health slot →
set `client_metadata["post_commit_interrupted"]` → re-raise) wraps
Phase C and catches `BaseException` (including `CancelledError` /
`SystemExit` / `KeyboardInterrupt`, re-raised without swallowing).

Process-local diagnostics live on `SelectionClaimDiagnostics`
(`/api/stats/runtime` `selection_claims`) and track
`claims_created`, `claims_committed`, `claims_published`,
`claims_rolled_back_before_persistence`,
`ambiguous_commit_reconciliations`,
`post_commit_publication_failures`,
`compensation_successes` / `compensation_failures`,
`max_concurrent_claims`, and
`claim_lock_wait_overflows` / `claim_lock_wait_recent`
(`{sample_count, p50_ms, p95_ms, p99_ms, max_ms}`).

#### Pre-milestone-B ordering (Phase 5 reference)

The pre-milestone-B lock held `_select_lock` across BOTH the durable
transaction (`request_attempts` + `routing_decisions` INSERT inside
`async with self._db.transaction():`) AND the runtime publication
step (`Router.increment_active_request_count` +
`QuotaEstimator.add_reservation`). The publication ran AFTER the
transaction committed but BEFORE the lock released, so a concurrent
selector that entered the lock next observed this attempt's runtime
state. The publish was fast (in-process counter + cache mutation),
so the lock-hold stayed tight while still closing the burst-skew race
previously caused by publishing inside the transaction body.

The two contexts were written as explicit nested `async with` blocks
(outer `_select_lock`, inner `_db.transaction()`) — NOT as a compound
`async with self._select_lock, self._db.transaction():`. A compound
form would still exit right-to-left (transaction commits before the
lock releases), so context-exit order alone was not the invariant.
The actual bug was that the runtime publication block lived INSIDE
the transaction body; active-count and reserved-cost state were
therefore published before the transaction committed. The explicit
nested form made it hard to accidentally place publication inside
the transaction while still keeping publication under `_select_lock`.
The key invariant was block placement (publication must be outside
the DB transaction body but still inside `_select_lock`), not
context-exit order. Milestone B replaces this with two narrow
acquisitions of `_selection_claim_lock` so DB I/O never holds the
coordinator lock at all.

#### Milestone C: durable dispatch write pipeline

Milestone C replaces per-request correctness-critical dispatch
transactions with a process-owned, bounded in-process persistence
pipeline.  A `DispatchPersistenceWriter` (attached to
`ProcessRuntime`, survives generation swaps) collects immutable
`DispatchIntent` objects from concurrent coordinators and persists
them in microbatches.

Core flow:

1. Coordinator builds a `DispatchIntent` after routing and selection.
2. Coordinator enqueues the intent to the process-owned writer via
   `submit_intent()`, which returns a `Future[PersistedDispatchResult]`.
3. The writer's single drain task collects intents, forming batches
   up to `max_batch_size` with a bounded `max_batch_wait_ms` wait.
4. Batch persistence runs in a single `db.transaction()` via
   `persist_dispatch_bundles()`.  On failure, the entire batch
   rolls back.
5. Each successful intent's future resolves with a
   `PersistedDispatchResult` carrying durable IDs.
6. The coordinator receives the result, publishes runtime state,
   and proceeds with upstream dispatch.

The repository contract is binary: it returns a same-order list of fully
validated durable results or raises. It never returns placeholder IDs after a
rollback. `PersistedDispatchResult` requires non-empty request/reservation IDs
and a positive attempt ID; the coordinator validates the result again before
publishing runtime ownership. The writer fans one batch exception to every
waiter and leaves failed intents out of persisted counters. It is bound to the
event loop captured by `start()` and rejects cross-loop submission.

Key invariants:
- No upstream request is sent before its own dispatch bundle commit
  is acknowledged.
- Every accepted intent receives exactly one success or failure
  outcome.
- Queue saturation fails closed before upstream dispatch and is
  visible in diagnostics.
- Isolated requests do not incur an unconditional batching sleep.
- Caller cancellation cannot cancel unrelated batch members.

The writer is process-owned (on `ProcessRuntime`, not
`RuntimeGeneration`) and is not duplicated by live rehash.
Configuration lives under `[database.dispatch_writer]` with all
fields restart-required.  Runtime diagnostics are exposed via
`/api/stats/runtime` `dispatch_writer` (queue depth, batch sizes,
timing, error counts).  All sample storage uses bounded
`deque(maxlen=sample_window)`; snapshot p95 remains stable after
1M synthetic batches (Plan 029).

New modules:
- `src/eggpool/request/dispatch_intent.py` — immutable
  `DispatchIntent`, `PersistedDispatchResult`, and error classes.
- `src/eggpool/db/dispatch_repository.py` — repository-level
  bundle persistence (`persist_dispatch_bundles`).
- `src/eggpool/request/dispatch_writer.py` — process-owned
  `DispatchPersistenceWriter` with adaptive microbatching.

### Score components and eligibility diagnostics

Every persisted `routing_decisions` row carries the per-account score
breakdown captured by `QuotaFairScorer` at the moment the coordinator
chose the selected account. Migration `0035` adds the
`score_components_json` column; `RoutingDecisionTrace.to_score_components_json()`
serializes the diagnostic payload (TEXT JSON, defaults to `'{}'` on rows
written by code paths that pre-date the migration). The payload now also
includes per-window `util_5h` / `util_7d` / `util_30d` utilization ratios
(None when the scorer's capacity is unconfigured) and a `tie_break`
summary naming the decisive factor between the chosen account and its
runner-up (`tier`, `quota`, `inflight`, `transcode`, `near_tie`,
`exact_tie`, `no_runner_up`). The same data flows through
`eggpool accounts explain --model <id> [--provider P] [--protocol P]`
and `GET /api/stats/routing/eligibility` for live operator diagnostics.

`Router.explain_account_eligibility(model_id, provider_id, protocol)`
returns one row per registered account with `eligible: bool`, a stable
`reason_code` (`ok`, `disabled`, `auth_failed`, `quota_exhausted`,
`cooldown`, `rate_limited`, `circuit_open`, `wrong_provider`,
`no_protocol`, `protocol_mismatch`, `no_model`, `model_stale`), and a
short `reason_detail` that names the account, its provider, its
configured protocols, the requested model id, and the stale-window
seconds (so the operator can act directly on the diagnosis). The
classification mirrors the live filter chain in
`eggpool.routing.eligibility.get_eligible_accounts` so explanations
match the routing path exactly.

`eggpool accounts explain` opens the database, runs migrations on a
fresh install, and calls `ModelCatalogCache.hydrate_from_db(db)` to
populate the in-memory model / provider / account-support tables from
the durable `models`, `provider_model_metadata`, and `account_models`
rows. The cache is wrapped in a tiny `_CatalogShim` so `Router` can
consume it without booting a full `CatalogService`; output is rendered
with `click.echo` (the previous `rich` table was removed because the
dependency was undeclared).

## Provider Routing Priority and Model Collapse

Two related configuration knobs let operators control how requests for the
same base model fan out across providers and how that model appears in the
catalog.

- **`routing_priority`** — `[providers.<id>].routing_priority` is a non-negative
  integer (default `0`). Higher values are preferred. The field is per-provider,
  not per-account: keys of the same provider share a tier and are
  load-balanced by `QuotaFairScorer`.
- **`collapse_models`** — `[models].collapse_models` is a boolean (default
  `false`). When `false`, the catalog exposes one provider-suffixed entry per
  `(model_id, provider_id)`. When `true`, the same base model collapses to a
  single unsuffixed `model_id` and is routed across every provider that
  supports it.

`collapse_models` and `routing_priority` are independent. Either can change
without re-deriving the other. Both require a service restart.

### Default behavior

With defaults (`collapse_models = false`, `routing_priority = 0`), three
providers that all expose `minimax-m2.7` (`opencode-go`, `minimax`,
`generalcompute`) are surfaced as three distinct suffixed model IDs:
`minimax-m2.7/opencode-go`, `minimax-m2.7/minimax`,
`minimax-m2.7/generalcompute`. Each suffixed ID routes only against its own
provider's accounts, load-balanced within the provider.

### Worked example

A `generalcompute`-first / `minimax`-second / `opencode-go`-last ordering
with three `opencode-go` keys load-balancing inside their tier:

```toml
[models]
# collapse_models = false  # default; emit suffixed IDs

[providers.opencode-go]
routing_priority = 0  # load balance within this tier

[providers.minimax]
routing_priority = 2

[providers.generalcompute]
routing_priority = 3  # tried first
```

A request for `minimax-m2.7/generalcompute` first hits the
`generalcompute` accounts (load balanced inside the tier). If every
`generalcompute` account fails pre-body, the coordinator retries the
`minimax` tier, then the `opencode-go` tier. A request for
`minimax-m2.7/opencode-go` only ever hits `opencode-go` accounts regardless
of priority — priority only orders the eligible account set inside one
suffixed (or unsuffixed) model ID.

### Catalog exposure and CLI surface

- `/v1/models` includes an `eggpool.routing_priority` extension field on
  each suffixed entry.
- `eggpool configsetup opencode` generates suffixed IDs when
  `collapse_models = false` and a single unsuffixed ID per base model when
  `collapse_models = true`.
- `eggpool connect` writes `routing_priority = 0` on every newly created
  provider block and leaves existing blocks untouched, so operators can edit
  one number to rebalance.

## Catalog Refresh Semantics

The catalog refresh path is **non-destructive by default**. Healthy
account/model support rows survive every refresh cycle so a transient
network blip, an empty upstream response, or a partially-normalized
response cannot silently de-pool a healthy account. The only de-pooling
mechanism in the catalog layer is `HealthManager`; configuration and
health state jointly own the eligibility decision.

### Per-account outcome classification

`CatalogService._fetch_and_process_account` categorizes every refresh
attempt and returns an `AccountCatalogOutcome` plus, on success, an
`AccountCatalogUpdateResult`:

| Outcome | When | Cache touched? |
| ------- | ---- | -------------- |
| `SUCCESS_AUTHORITATIVE` | HTTP 2xx, fully protocol-resolved, non-empty | Add/update only unless `catalog_withdrawal_policy` permits withdrawal |
| `SUCCESS_PARTIAL` | HTTP 2xx, but at least one model lacks a resolved protocol | Add/update only; withdrawal forced off |
| `SUCCESS_EMPTY` | HTTP 2xx with zero normalizable models | No-op (prior support preserved) |
| `FAILED` | Network/5xx/auth/quota/JSON-shape failure | **No** (cache untouched) |
| `SKIPPED` | Fetcher returned without contacting upstream | **No** (cache untouched) |

### Withdrawal policy

`ModelsConfig.catalog_withdrawal_policy` controls when withdrawal is
permitted:

- `preserve_until_health` (default) — withdrawal is **never**
  triggered by a refresh. Health state is the sole de-pooling
  mechanism.
- `confirmed_once` — a single authoritative refresh may withdraw
  support for models no longer advertised.
- `confirmed_twice` — two consecutive authoritative refreshes are
  required to withdraw support.

`SUCCESS_PARTIAL` overrides the policy for that cycle and forces
`allow_withdrawals = False` because a partial response is never a
complete withdrawal confirmation. The destructive
`mark_account_models_unavailable` step is also gated on
`authoritative=True, allow_withdrawals=True` so the cache layer
itself enforces the invariant; the service decides which flags to
flip based on outcome category and policy.

### Per-cycle operational logging

`CatalogService.refresh()` calls `_log_refresh_summary` after every
cycle. The INFO log enumerates per-outcome counts on one line so
operators can spot catalog uncertainty without enabling debug
logging:

```
Catalog refresh summary: policy=preserve_until_health total=3 authoritative=1 partial=0 empty=1 failed=1 skipped=0
```

A run with many `FAILED` or `PARTIAL` rows is signal that an upstream
or DNS path is unhealthy; a sudden shift in `AUTHORITATIVE` count
indicates a real catalog change that may need rebalancing.

### Gate diagnostics for `accounts explain`

`Router.explain_account_eligibility(include_gates=True)` returns a
per-account gate breakdown dict (config, credentials, health,
circuit, provider, protocol, model support, freshness, provider
metadata, protocol match, local quota, final eligible) so operators
can pinpoint exactly which gate is failing without running live
traffic. The dict is informational; the canonical decision still
comes from `_classify_eligibility`. The `eggpool accounts explain
--gates` CLI command renders the same breakdown as a compact
text table.

### Shared per-provider metadata and sibling-wins protocol guard

`_provider_models` is keyed by `(model_id, provider_id)` and is
**shared** by every account that lists that provider — e.g. all
`opencode-go-0001`/`-0002`/`-0003` accounts share one row per
model on the `opencode-go` provider. The previous
`update_from_account()` clobbered this shared dict unconditionally
even on partial responses, which produced the upstream-reported
"all traffic on `opencode-go-0001`, none on `0002`/`0003`" regression
when a single account's refresh resolved the protocol as `None`.
`_preserve_resolved_protocol()` now applies a sibling-wins guard
in the non-destructive path: when a per-provider row already has a
resolved protocol and the new entry arrives with `protocol=None`,
the prior protocol is preserved and the resulting per-provider row
is shared across all sibling accounts. The destructive path
(`authoritative=True AND allow_withdrawals=True`) intentionally
skips the guard so operator-initiated withdrawals remain effective.

## Error Hierarchy

```
AggregatorError (base)
├── ConfigError
├── DatabaseError
├── UpstreamError (status_code attribute)
│   ├── TemporaryUpstreamError
│   ├── TransientUpstreamError
│   ├── AuthenticationError
│   ├── QuotaExhaustedError
│   ├── RateLimitError (retry_after attribute)
│   └── ModelUnavailableError
├── ProxyError
├── ModelNotFoundError (model_id attribute)
├── NoEligibleAccountError
├── CatalogUnavailableError
├── AuthenticationUnavailableError
├── UpstreamExhaustedError
├── AccountSuspendedError
├── RequestTooLargeError
├── ModelInfoSourceFetchError
├── ContextLimitExceededError
└── CapabilityError (model_id, capability, requested_fields attributes)
```

## Model Information

- **Sidecar subsystem**: `model_info/` package provides persistent model metadata sidecar tables (`model_info_canonical`, `model_info_observations`, `model_info_aliases`, `model_info_source_health`) via migration `0036`, with phase 5 hardening and override storage added by migration `0038`
- **Source adapter pattern**: `ModelInfoSource` protocol in `sources/base.py` defines `name`, `priority`, `fetch_all()`, `fetch_one()`; concrete adapters implement this interface
- **Provider-native observations**: `ProviderCatalogSource` reads in-memory `ModelCatalogCache` entries and emits `SourceModelRecord`s; no network I/O
- **OpenRouter metadata source** (phase 3): `OpenRouterModelInfoSource` fetches the OpenRouter `/models` catalog and emits `SourceModelRecord` observations for each entry. TTL-cached per source; uses the shared outbound HTTP client from `OutboundClientManager`. Exact/curated alias matching only (no fuzzy matching). Failures are recorded in source health and never break startup, catalog refresh, or routing
- **Identity resolution**: `model_info/identity.py` (`resolve_openrouter_record()`) matches OpenRouter source model IDs to local model IDs via exact `model_info_aliases` rows, exact source_model_id equality, or exact pricing aliases. No substring or edit-distance matching. Ambiguous matches (multiple aliases) return no match
- **Status classification**: models classified as `sparse_new`, `partial`, `fresh`, etc. based on available metadata (display name, context limit, capabilities)
- **Deterministic summaries**: generated from fields only (no LLM); sparse models explicitly note metadata sparsity
- **Lifecycle wiring**: `ModelInfoService` initialized at startup after catalog load; accepts optional `outbound_client` for external sources. `CatalogService.refresh()` returns `CatalogRefreshResult` with diff information (new/withdrawn models, changed provider keys)
- **Background refresh**: supervised `model_info_refresh` task processes due models via `ModelInfoRefreshScheduler`; reconciliation also runs after successful catalog refreshes. External source catalogs are fetched once per cycle (bulk) and matched to due models via identity resolution
- **Refresh scheduling**: `ModelInfoRefreshScheduler` computes next refresh time based on status, first-seen age, and config TTLs; sparse-new models receive accelerated refresh within a configurable window
- **Source health**: per-source health tracking with cooldown backoff; `record_source_success`/`record_source_error` helpers
- **Write deduplication**: observations deduplicated by `(source, source_model_id, raw_hash)`; canonical rows compared before rewrite
- **Error hierarchy**: `ModelInfoSourceFetchError` (subclasses `AggregatorError`) raised by source adapters on network/HTTP/parse failures; caught by `ModelInfoService` and recorded as source-health errors
- **CLI**: `eggpool modelinfo show/list/refresh` commands for inspection and manual refresh
- **Config**: `[model_info]` section in `config.toml` with TTL controls, refresh intervals, and source enablement (`[model_info.sources.openrouter]` for OpenRouter)
- **JSON API endpoints** (phase 4): `src/eggpool/api/model_info.py` registers the following endpoints. The specific suffix routes (`/matches`, `/aliases`) are registered BEFORE the greedy `/{model_id:path}` detail route so FastAPI's path matcher cannot capture `<model_id>/aliases` or `<model_id>/matches` as the `model_id` parameter. The order is pinned by `tests/unit/test_model_info_route_registration.py`:
  - `GET /api/model-info` — summary list of all models (status, sparse, summary, sources, timestamps)
  - `GET /api/model-info/{model_id:path}/matches` — match evidence diagnostics for one model (capped at 50 entries)
  - `GET /api/model-info/{model_id:path}/aliases` — alias list + source-keyed alias rows for one model
  - `GET /api/model-info/{model_id:path}` — per-model detail (limits, modalities, external IDs, provenance, observations, conflicts)
  - `GET /api/model-info/sources` — source health snapshot (redacts secrets and raw error messages)
  - `POST /api/model-info/refresh` — manual refresh (always auth-gated; accepts `?model_id=<id>`, `?source=`, `?force=1`)
  - Registered in `create_app()` under dashboard auth policy when `config.model_info.enabled`
- **Presentation helpers**: `src/eggpool/model_info/presentation.py` is the single source of truth for public status labels (`sparse_new` → `sparse`, `conflicting` → `conflict`), dashboard filter aliases, ISO timestamp formatting, and compact raw-payload-free summaries. `/api/model-info` and `/v1/models` request display labels; dashboard filtering requests canonical labels so `?info_status=sparse` and `?info_status=sparse_new` match the same rows.
- **`/v1/models` enrichment** (phase 4): `serialize_openai_model()` accepts an optional `model_info` mapping; when present, compact fields are nested under `eggpool["model_info"]` (status, sparse, summary, sources, last_refreshed_at). Raw observations, benchmarks, provenance, and conflicts are never included. The `/v1/models` route reads `config.model_info.include_in_models_endpoint` and `app.state.model_info` to build a summary map once before the loop, resolving by `base_model_id` for provider-suffixed entries. Model-info errors are logged and silently omitted.
- **Dashboard integration** (phase 4): `handle_models()` in `dashboard/routes.py` fetches model-info summaries concurrently with stats via `asyncio.gather()`. `render_models()` renders an "Info" column with colored status pills (`pill-fresh`, `pill-partial`, `pill-sparse`, `pill-stale`, `pill-conflict`, `pill-unmatched`, `pill-source-unavailable`). CSS tooltips use escaped summary text, sources, and last-refreshed timestamp. All source-provided text is HTML-escaped via `escape()`. CSS lives in `dashboard/static/dashboard.css` using theme-compatible colors.
- **Dashboard detail page** (post-phase 5): `GET /models/{model_id:path}` renders a full model-info detail page with status cards, summary, provider/callability, metadata, benchmarks, Hugging Face metadata, conflicts, and provenance sections. `handle_model_detail()` in `dashboard/routes.py` fetches `model_info` service from `app.state`; `render_model_detail()` in `dashboard/render.py` renders all sections with HTML-escaped output. Models page links to detail via model ID hyperlinks.
- **Artificial Analysis source** (phase 5): `ArtificialAnalysisSource` fetches benchmark data (throughput, latency, pricing) from the Artificial Analysis API. Gated behind an API key (`[model_info.sources.artificial_analysis]`); disabled by default. Emits `SourceModelRecord` observations with benchmark fields (tokens_per_second, time_to_first_token, cost_per_1k_input, cost_per_1k_output)
- **Hugging Face source** (phase 5): `HuggingFaceSource` fetches model card metadata and pipeline tags from the Hugging Face Hub API. Exact alias matching only (no fuzzy matching). Disabled by default; enable via `[model_info.sources.huggingface]`
- **Manual overrides** (phase 5): field-level, config-driven overrides in `[model_info.overrides.<model-id>]`. Supports display_name, summary, and other canonical fields. Overrides are applied after all source merges and take precedence over any source-provided value
- **Alias expansion** (phase 5): configured aliases in `[model_info.aliases]` map canonical model IDs to alternative identifiers. Source-specific alias matching (e.g., OpenRouter model IDs) uses these aliases during identity resolution. Aliases are also persisted in the `model_info_aliases` table
- **Source health hardening** (phase 5): `model_info_source_health` tracks `rate_limited_until` (explicit backoff timestamp), `last_status_code`, `last_payload_count`, and `last_success_duration_ms`. Sources respect `rate_limited_until` to avoid hammering rate-limited APIs. Health data is exposed via `GET /api/model-info/sources`
- **Detail API enhancements** (phase 5): `GET /api/model-info/{model_id}` returns benchmark data (per-source throughput/latency/pricing), alias list, Hugging Face metadata (pipeline_tags, model_card_url, library_name), and manual override indicators
- **Richer summary generation** (phase 5): deterministic summaries now include sparse-data warnings, benchmark highlights (e.g., "74 tok/s on Artificial Analysis"), Hugging Face card availability, and conflict annotations when sources disagree on fields
- **`model_info_overrides` table** (phase 5): migration `0038` adds a `model_info_overrides` table for persisting operator-set overrides to canonical fields. Overridden fields are marked with an `overridden` flag on canonical rows to distinguish from source-provided values
- **Tiered identity matching** (migration `0049`): `model_info/matching.py` resolves local model IDs to source records through a 6-tier resolver:
    0. `configured_exact_alias` — operator-configured `[model_info.aliases]` rows
    1. `exact_source_id` — raw `model_id` or `split(model_id)[1]` or provider-catalog
       `<provider_id>/<model_id>` aliases indexed verbatim
    2. `normalized_exact` — `normalize_model_key()` of model_id, display_name, and
       provider-catalog aliases compared against the candidate index
    2b. `deployment_suffix_normalized_exact` — conservative stripping of
       `DEPLOYMENT_SUFFIX_TOKENS` (`highspeed`, `fast`, `turbo`, `speed`, `lowlatency`, `lowlat`). Only fires
       when the original identifier has a digit or family anchor AND the
       candidate set is unique. Refuses to strip when the original contains a
       `SEMANTIC_VARIANT_TOKENS` token (`pro`, `mini`, `flash`, `lite`, `max`, `plus`, `instruct`, `chat`, `reasoning`, `thinking`, `preview`, `code`, `coder`, `omni`). Opt-in via
       `ModelInfoMatchingConfig.deployment_suffix_normalized_exact = True`.
     3. `regex_rule` — conservative built-in patterns for `minimax`, `claude`,
        `gemini` family shapes; rejects candidates whose version tokens or
        family tokens differ from the local model (e.g. `flash` vs `pro`)
    4. `similarity_guarded` — `difflib.SequenceMatcher` with strict thresholds
       (disabled by default)
  Normalization uses NFKC + `.casefold()` + separator stripping + duplicate-vendor
  collapse, so `MiniMax-M3`, `minimax-m3`, `MiniMax M3`, and `MiniMax: MiniMax M3`
  all normalize to `minimaxm3`. Provider namespaces like `opencode-go/minimax-m3`
  are stripped via `strip_provider_namespace` — they are NOT vendor namespaces.
  Non-exact accepted matches persist evidence rows in `model_info_match_evidence`
  and stamp `match_method` / `discovered_by` / `diagnostics_json` on
  `model_info_aliases`. Periodic refresh logs WARNING on no-match cycles.
  The tiered matcher receives `known_provider_namespaces` from the catalog cache
  so aggregator provider IDs (e.g. `opencode-go`) are stripped before matching.
  The production supervisor refresh (`_model_info_refresh_once` in `app.py`)
  calls `log_refresh_result()` for consistent INFO/WARNING logging on every
  cycle, not just cycles with `refreshed > 0`. Match evidence is exposed via
  `GET /api/model-info/{id}/matches` and as a compact `match_evidence` field
  on the detail endpoint.

### Corrective Pass (Phases A–F)

The model-info corrective plan in `plans/model-info-corrective-catalog-models-and-cards.md` makes the sidecar **observation-first** instead of usage-first, ensures external sources reach every model they should, and surfaces catalog presence on the dashboard.

- **Configured-alias seeding (Phase A)**: `ModelInfoService.seed_configured_aliases()` runs at startup (inside `load_cache()`) and inserts every `[model_info.aliases]` entry into `model_info_aliases` before the first external source fetch. Skips empty source/source_model_id; tolerates duplicates; uses `_alias_confidence_to_float()` to map names like `exact`/`curated`/`high` to numeric confidences. Mandates Hugging Face exact-source matches, which are otherwise impossible to link.
- **Observation-driven canonical detail (Phase B)**: `build_canonical_detail(latest_observations, sources, *, summary=None, supports_vision=None, ...)` merges the freshest observation per source into a single `detail` dict, then layers manual overrides and conflict detection (`_detect_context_conflicts`, `_detect_benchmark_conflicts`). The merged detail exposes a nested `detail["limits"]` block with `effective_context`, `external_context`, `effective_output`, `external_output`; the API detail handler reads from this block via a legacy fallback that maps the pre-Phase-B flat keys (`context_tokens`→`effective_context`, `context_window_external`→`external_context`, etc.). `reconcile_catalog_snapshot()` and `refresh_due_models()` are non-destructive — observation `last_refreshed_at` is preserved across restarts.
- **Single-model refresh (Phase C)**: `ModelInfoService.refresh_model_info(model_id, *, provider_id=None, source=None, force=False)` runs an immediate refresh for one model: provider catalog observation is always written (the source of truth for callability), the requested external source is fetched and indexed by `source_model_id`, and alias rows are matched identity-first. When `provider_id` is supplied the provider-catalog branch only matches that provider's record, narrowing the per-account store. `POST /api/model-info/refresh?model_id=<id>&source=<provider_catalog|openrouter|artificial_analysis|huggingface>&force=1` exposes the entry point and returns a `scope=model` payload with counts (`refreshed`, `skipped`, `errors`, `sources_attempted`, `sources_matched`, `observations`) plus the canonical `model_id`, the original `requested_model_id`, and the resolved `provider_id`. The endpoint URL-decodes `model_id`, then calls `parse_model_provider()` so `?model_id=gpt-4o/openai&force=1` refreshes the canonical `gpt-4o` row with `provider_id="openai"`. `source=all` (or absent) means every enabled source; unknown `source` values return HTTP 400 before the service is touched.
- **Catalog-complete Models page (Phase D)**: `handle_models()` runs `_get_model_info_summary_map()`, `get_dashboard_models()`, and the new `_get_catalog_rows()` concurrently via `asyncio.gather`. `_get_catalog_rows()` builds sparse `models` rows for every catalog entry, honoring `[models].collapse_models`:
  - **`collapse_models = false` (default)** — emits one row per `(model_id, provider_id)` pair by iterating `catalog.cache.get_provider_model_entries()`.
  - **`collapse_models = true`** — emits one row per unsuffixed `model_id` by iterating `catalog.get_models_for_exposure()` and threading the `providers` list (sorted) onto each row. `provider_id` is set to the first sorted provider so stats rows keyed by `(model_id, provider_id)` still match. `routing_priority_max` reflects the max priority across contributing providers.
  Both paths apply the account filter, set the diagnostic fields (`base_model_id`, `providers`, `available`, `catalog_status`, `routing_priority`, `protocol`, `display_name`), and produce zero-activity placeholders so unused models still render. The account filter is provider-aware: model-level support is not enough for provider-scoped rows, because a single sibling provider's support must not make another provider's row appear in an account-specific view. `_merge_models_with_catalog(stats, catalog, *, collapse_models=...)` dedupes by `(model_id, provider_id)` in provider-scoped mode and by `model_id` only in collapsed mode (via `_model_row_key()`); legacy stats rows that omit `provider_id` fall back to `catalog_by_id[model_id]` for diagnostic fields but do **not** suppress provider-scoped catalog rows, so an unused sibling provider for the same base model still renders. The merged list is sorted by request count descending with model_id and provider_id as tie-breakers. `render_models()` URL-encodes the detail link path segment via `urllib.parse.quote(safe="")` so model ids with provider suffixes, query metacharacters (`?`, `#`), or HTML-special characters round-trip cleanly through `/models/{model_id:path}` + the detail handler's `unquote()`.
- **Detail API legacy schema (Phase E)**: `handle_model_info_detail()` reads `detail["limits"]` first and falls back to the legacy flat keys only when the nested block is absent. Combined with Phase B's normalization, every pre-Phase-B canonical row remains API-readable; new writes always use the nested schema.
- **Legacy detail backfill (Phase F)**: `ModelInfoService.backfill_legacy_detail_blocks(limit=200)` walks `repo.list_all_canonical(limit)` and lifts pre-Phase-B flat keys into `detail["limits"]` via the private `_legacy_flat_keys_to_limits()`. Observation-derived `external_*` keys can overwrite stale legacy seeds so fresh OpenRouter/Artificial Analysis data wins. The provenance `backfilled_limits=True` marker prevents double-lift. Wired into `app.py` startup immediately after `backfill_missing_canonical()`.

### Dashboard Display Fix Plan (Phase G — Visibility & Lookup Correctness)

The plan in `plans/model-info-dashboard-display-fix.md` makes the dashboard's model-info surface correct under every catalog shape (collapsed / provider-scoped / case differences) and observable to operators when the subsystem is degraded.

- **Fail-open with degraded-state visibility** (Phase 1): `_get_model_info_summary_state()` in `dashboard/routes.py` returns a `ModelInfoDashboardState` dataclass (`summaries`, `available`, `degraded_reason`, `error_class`, `summary_count`). Missing service -> `degraded_reason="service_unattached"` with a server warning. Exception during fetch -> `degraded_reason="fetch_error"` with full traceback logged under `eggpool.dashboard.routes` and `error_class` set. Both render a visible degraded-state panel above the table via `render_models(model_info_state=...)`. `handle_model_detail()` distinguishes "no canonical row" from "lookup failed" via an optional `model_info_error` kwarg on `render_model_detail()`. Traceback text is never embedded in HTML.
- **Case-insensitive batch canonical lookup** (Phase 2): `ModelInfoRepository.get_canonical_many()` now uses `lower(model_id) IN (...)` with `casefold` normalization to mirror `get_canonical()`'s `COLLATE NOCASE` semantics. When `model_ids` is supplied, the returned dict is keyed by the **requested** id (not the stored id); when `None`, all rows are returned keyed by stored id. Empty-list input short-circuits to `{}`.
- **Provider-scoped catalog coverage** (Phases 3+4): `handle_models()` derives `requested_ids` from `_get_catalog_rows()` (`base_model_id` with `model_id` fallback, deduped) and forwards them to `_get_model_info_summary_state(model_ids=...)`. `ModelInfoService.get_summary_map()`, `reconcile_catalog_snapshot()`, and `ensure_canonical()` all treat both `catalog._models` and `catalog._provider_models` keys as in-catalog. `_build_detail()` falls back to a provider-scoped entry when the global `_models` row is missing. Provider-scoped rows that never appear in the global model index still get canonical rows with provider-derived detail (display_name, protocol, capabilities, limits).
- **Runtime diagnostics surface** (Phase 5): `ModelInfoService.health_snapshot()` returns a best-effort snapshot with `enabled`, `canonical_count`, `catalog_model_count`, `provider_model_count`, `due_count`, and `source_health`. Backed by `ModelInfoRepository.count_canonical()` and `count_due()`. Each counter is wrapped in `except Exception` so failures surface as `*_error` keys, never as raised exceptions. `RuntimeMetricsService` accepts an optional `model_info=` parameter and exposes the snapshot under `/api/stats/runtime` as `result["model_info"]`. No raw source payloads are exposed.
- **Force-refresh operator paths** (Phase 6): Catalog refresh (`eggpool models refresh`) updates provider listings; model-info enrichment refresh is a separate operation: `POST /api/model-info/refresh?model_id=<id>&source=<provider_catalog|openrouter|artificial_analysis|huggingface>&force=1` (single model) or `?force=1` without `model_id` (bounded batch via `ModelInfoService.force_refresh_batch()`). Single-model responses include `requested`, `refreshed`, `skipped`, `errors`, `sources_attempted`, `sources_matched`, `observations`.

### OpenRouter Enrichment Corrective Plan

The plan in `plans/model_info_openrouter_enrichment_corrective_plan.md` makes OpenRouter enrichment reliable, observable, and accurately projected on the dashboard/API without requiring process restarts.

- **Source health reflects catalog availability** (Phase 1.1): `refresh_model_info()` records `record_source_success("openrouter", payload_count=N)` immediately after `_openrouter_source.fetch_all()`, independent of any per-model match. `refresh_due_models()` does the same after its bulk OpenRouter fetch. Source success therefore represents catalog availability, not local-model match success; an OpenRouter catalog with no matching alias still updates `model_info_source_health.last_success_at` and `last_payload_count`.
- **`source_diagnostics` in forced-refresh responses** (Phase 1.2): `refresh_model_info()` returns a `source_diagnostics` dict alongside `sources_attempted`/`sources_matched`. The OpenRouter entry carries `initialized`, `fetched`, `catalog_count`, `alias_candidates`, `matched_source_model_id`, `miss_reason`, and `cache_retry`. Miss reasons are stable strings (`source_not_initialized`, `fetch_error`, `empty_catalog`, `no_aliases`, `alias_not_in_catalog`, `ambiguous_aliases`, `matched`).
- **Case-insensitive alias lookup** (Phase 2.1): `ModelInfoRepository.get_aliases_for_model()` and `list_alias_rows_for_model()` use `WHERE lower(model_id) = lower(?)`. Stored `model_id` casing is preserved in returned rows. Case variants like `MiniMax-M3` and `minimax-m3` no longer silently hide configured aliases.
- **Case-insensitive observation lookup** (Phase 2.1 followup): `ModelInfoRepository.list_compact_observations_for_model()` uses the same `WHERE lower(model_id) = lower(?)` predicate so the API can serve per-case observations consistently.
- **Manual refresh reseeds configured aliases** (Phase 2.3): `refresh_model_info()` calls `seed_configured_aliases()` first so newly added `[model_info.aliases]` rows (via admin tooling, config reload) take effect on the next refresh without a process restart. Seeding is idempotent and falls through on per-row failures.
- **OpenRouter cache bypass on forced refresh** (Phase 2.4): if configured aliases exist but none match the cached catalog, `OpenRouterModelInfoSource.invalidate_cache()` runs and the forced fetch retries once. The retry is recorded as `cache_retry: true` in `source_diagnostics.openrouter` so operators can see the cache invalidation.
- **Canonical detail display-name promotion** (Phase 3.1): when `provider_detail.display_name` is empty or missing, `build_canonical_detail()` promotes the first per-source `display_name_<source>` (e.g. `display_name_openrouter = "MiniMax: MiniMax M3"`) into `detail.display_name` and records `detail.display_name_source` so the dashboard can show both the chosen name and the source it came from. Provider-native `display_name` still wins when populated.
- **Source-scoped advisory pricing** (Phase 3.1): OpenRouter $/Mtok pricing is captured under `detail.pricing.openrouter` so advisory OpenRouter pricing can never collide with provider-reported cost accounting. Existing `detail.pricing_observation` block is preserved.
- **DB-backed API observations** (Phase 4): `GET /api/model-info/{model_id}` reads per-source observation rows via `ModelInfoRepository.list_compact_observations_for_model()`. Rows carry real `source_model_id`, `provider_id`, `observed_at`, and `confidence` from `model_info_observations` (not synthesised from provenance). The dashboard detail page surfaces the same rows in an "Observations" panel so dashboard and API see identical per-source truth. Legacy synthesised projections are still produced when callers pass `observations=None` and are flagged `_synthetic: true` so test doubles and historical callers never produce silent fabrications.

### OpenRouter Polish Closeout Plan

The plan in `plans/model_info_openrouter_polish_closeout_plan.md` closes the remaining polish holes from the corrective pass: alias-candidate determinism, non-misleading observation fallback, and parity between manual and scheduled refresh.

- **Deterministic alias candidate selection** (Phase 1): `ModelInfoRepository.list_alias_rows_for_model()` already returns rows in deterministic order; `choose_alias_candidates()` (`src/eggpool/model_info/identity.py`) prefers rows whose stored `model_id == requested_model_id` exactly, then `dedupe_alias_strings()` collapses identical alias strings. `resolve_openrouter_record()` consumes these helpers so two case-variant rows pointing to the same OpenRouter id resolve to one match (no false ambiguity), exact-case rows win over case-folded conflicting rows, and folded-case rows with no exact-case match that disagree on the source id produce a clean no-match the caller can surface as `miss_reason = ambiguous_aliases`. When multiple distinct aliases remain but only one is in the OpenRouter index, that one wins and the others are ignored.
- **Richer `source_diagnostics` for OpenRouter** (Phase 1): `source_diagnostics.openrouter` now exposes `alias_rows` (one entry per alias candidate with `match_kind = "exact_case" | "case_folded"`) and `alias_selection` so operators can audit why the resolver chose a particular row. `alias_candidates` is the deduped list.
- **Non-misleading observation fallback** (Phase 2): `_detail_response()` accepts an `observations_error` kwarg. When the repository observation read fails, `handle_model_info_detail()` passes `observations=None, observations_error=<ExcClass>` and the response returns `{"observations": [], "observations_error": "<ExcClass>"}` instead of synthesising external-source rows. The legacy `_synthetic: true` path is retained only for direct test-double callers. The dashboard detail page mirrors the contract via `render_model_detail(observations=..., observations_error=...)` which renders an "Observation read failed" panel with the error class name when the read fails.
- **Scheduled refresh parity** (Phase 3): `refresh_due_models()` uses the same `resolve_openrouter_record()` helper as manual refresh and now reports aggregate `openrouter_attempted`, `openrouter_matched`, and `openrouter_missed` counters in its return value. Source success is recorded independently of per-model match (Phase 1.1 invariant). Cache bypass is intentionally limited to manual `force=1` paths so scheduled cycles do not flood the OpenRouter API.
- **Operator verification script** (Phase 4): `scripts/debug_model_info_openrouter.sh` issues the force refresh, the detail GET, and the source-health query in one shot. Documented in `docs/model-info-openrouter-debug.md`.
- **Dashboard Observations panel** (Phase 5.2): `render_model_detail()` renders a per-source observations table (Source / Source model id / Provider / Observed / Confidence) when `observations` are supplied. Raw payloads are never exposed.

### OpenRouter Enrichment Test Coverage

`tests/unit/test_model_info_openrouter_enrichment.py` pins the invariants this plan promised:

- `TestOpenRouterSourceHealthWithoutMatch` — successful OpenRouter fetch with no matched alias still records `model_info_source_health`; matched alias reports `sources_matched` correctly.
- `TestOpenRouterDiagnostics` — diagnostics surfaces `alias_not_in_catalog`, `no_aliases`, `matched`.
- `TestAliasCaseInsensitiveLookup` — alias repository methods use `lower(model_id) = lower(?)`.
- `TestRefreshResedsConfiguredAliases` — manual refresh calls `seed_configured_aliases()` so new aliases take effect mid-process.
- `TestOpenRouterCacheBypassOnForce` — forced refresh with aliases-but-no-match invokes `OpenRouterModelInfoSource.invalidate_cache()` and retries once.
- `TestCanonicalDetailDisplayNamePromotion` — external `display_name_<source>` is promoted into `detail.display_name` only when the provider has none.
- `TestCompactObservationsRepository` — DB rows return truthful `source_model_id`, `provider_id`, `confidence`; case-insensitive lookup; latest-per-source selection.
- `TestDetailEndpointObservations` — API endpoint returns DB rows; synthesised fallback only fires when caller passes `observations=None`.
- `TestParseEntryToRecord` — `OpenRouterModelInfoSource` parsing produces the canonical `MiniMax-M3` shape from the documented OpenRouter payload.

### Suffix Matching, Benchmark Enrichment, and Startup Task Plan

The plan in `plans/model_info_suffix_benchmarks_startup_tasks_plan.md` extends the model-info subsystem along three axes: deployment-suffix identity matching, benchmark source diagnostics + tiered Artificial Analysis (AA) matching, and operator-friendly background task first-run behavior. None of the changes alter routing or billing; they tighten the sidecar so live `/models` and `/api/model-info` panels surface accurate, auditable state without surprises.

- **Deployment-suffix tier 2b (Phase 1)**: `src/eggpool/model_info/normalization.py` adds `DEPLOYMENT_SUFFIX_TOKENS` (`highspeed`, `fast`, `turbo`, `speed`, `lowlatency`, `lowlat`) and `SEMANTIC_VARIANT_TOKENS` (`pro`, `mini`, `flash`, `lite`, `max`, `plus`, `instruct`, `chat`, `reasoning`, `thinking`, `preview`, `code`, `coder`, `omni`). `generate_deployment_suffix_variants()` enumerates conservative stripping candidates and `has_digit_or_family_anchor()` guards against stripping tokens when the original identifier contains a digit or family anchor. `matching.py` adds `_tier_deployment_suffix_normalized_exact` between tier 2 (`normalized_exact`) and tier 3 (`regex_rule`); the tier is opt-in via `ModelInfoMatchingConfig.deployment_suffix_normalized_exact: bool = True` and refuses to strip any candidate whose original contains a `SEMANTIC_VARIANT_TOKENS` token, so `MiniMax-M2.7-highspeed` resolves to `minimax/minimax-m2.7` while `claude-3-haiku-highspeed` keeps its semantic shape.
- **Highspeed fixtures (Phase 2)**: `tests/fixtures/model_info/openrouter_minimax_highspeed_sample.json` and `tests/fixtures/model_info/provider_catalog_sample_minimax_highspeed.json` cover the `M2.1`/`M2.5`/`M2.7` highspeed variants. `tests/unit/test_model_info_deployment_suffix.py` pins status advancement, ambiguity rejection, persistence, and the `deployment_suffix_normalized_exact = False` opt-out. The highspeed tier 2b only fires when the suffix-stripped candidate is unique — ambiguous candidates fall through to the lower-priority tiers rather than risking a wrong alias.
- **Artificial Analysis source diagnostics (Phase 3)**: `ModelInfoService.source_diagnostics()` enumerates every configured source (`provider_catalog`, `openrouter`, `artificial_analysis`, `huggingface`) with `configured` / `constructed` / `requires_api_key` / `api_key_present` / `reason` fields. The `/api/model-info/sources` handler merges the live `model_info_source_health` snapshot with the diagnostics so operators can see why a source has no `last_success_at` row (e.g. `requires_api_key`, `not_constructed`, `disabled`). The handler tolerates both sync and async health snapshots via `inspect.isawaitable` so legacy test doubles that pass `AsyncMock` keep working. `tests/unit/test_model_info_source_diagnostics.py` pins the contract.
- **Tiered Artificial Analysis matching (Phase 3)**: `ModelInfoService._resolve_aa_record()` now consumes the shared tiered resolver, sharing the candidate index and `model_info_match_evidence` audit trail with OpenRouter. AA matches surface `match_method`/`discovered_by`/`diagnostics_json` on `model_info_aliases` and contribute to the same `MATCH_EVIDENCE` observability the OpenRouter plan already exposed.
- **Preserved external IDs in provenance (Phase 4)**: `build_canonical_detail()` now credits `provenance.sources` for any `existing_detail.external_ids[*]` key that wasn't contributed this cycle, populating `provenance.source_states[<src>] = "preserved_external_id"`. The detail cycle therefore distinguishes three source states explicitly: `contributed` (newly fetched this cycle), `preserved_external_id` (carried from the prior canonical row because the external ID persists in `external_ids`), and `absent` (no observation and no external ID). `tests/unit/test_model_info_provenance_consistency.py` pins the new contract; the existing `test_model_info_reconciliation.py` was updated to expect preserved `openrouter` entries with the new `source_states["openrouter"] = "preserved_external_id"` flag.
- **Background task first-run behavior (Phase 5)**: `update_checker`, `checkpoint`, and `model_info_refresh` in `app.py:_lifespan_runtime` are now registered with `run_immediately=True`, so their first tick fires during startup instead of after the first interval. `_first_run_state()` in `background/__init__.py` returns one of `last_success` / `last_error` / `never_run_not_due` / `never_run_startup_deferred` / `never_run_overdue` based on the supervisor's heartbeat fields, the configured `run_immediately` / `initial_delay_s`, and a 25%-of-interval (capped at 60s, minimum 5s) grace band. `SupervisedTask.snapshot()` exposes the label under `first_run_state` so the runtime API and dashboard can render friendly statuses.
- **Source and task diagnostics (Phase 6)**: `RuntimeMetricsService._snapshot_background_task_summary` now counts `never_run_not_due` and `never_run_overdue` separately from `failed`, and `/api/stats/runtime` `background_task_summary` carries both new counters. The runtime dashboard renders a `startup deferred` / `not yet due` / `never ran (overdue)` / `failing` badge from `first_run_state` so a freshly started process never looks unhealthy just because the first 30- or 60-second tick has not yet fired.
- **Tests**: `tests/unit/test_model_info_deployment_suffix.py`, `tests/unit/test_model_info_source_diagnostics.py`, `tests/unit/test_model_info_provenance_consistency.py`, and `tests/unit/test_background_first_run.py` lock the contracts. The matching-safety and tiered-matching test modules were extended to cover the deployment-suffix guards.

### Dashboard Model-Info Join Corrective Plan

The plan in `plans/model_info_dashboard_join_corrective_plan.md` closes the gap where the API reports canonical model-info rows but the `/models` dashboard page renders unknown pills. The failure modes it addresses:

* Catalog row construction silently swallowed exceptions, dropping the entire table when `catalog.cache.get_provider_model_entries()` (or `catalog.get_models_for_exposure()`) raised.
* Provider-suffixed dashboard rows (`minimax-m3/opencode-go`) needed a deterministic normalization step before the join so the renderer looked up the canonical `minimax-m3` row.
* `ModelInfoDashboardState` only covered `service_unattached` and `fetch_error`; there was no diagnostic for "API has summaries but the dashboard rows did not match any of them".

What changed:

- **`CatalogRowsState` dataclass** (`src/eggpool/dashboard/routes.py`): mirrors `ModelInfoDashboardState` for the catalog row builder. `_get_catalog_rows()`, `_get_provider_scoped_catalog_rows()`, and `_get_collapsed_catalog_rows()` now return `CatalogRowsState(rows, available, degraded_reason, error_class, row_count)` instead of a raw list. The two previously-silent `except Exception: return []` blocks now log via `logger.exception(...)` (full traceback) and surface `degraded_reason="fetch_error"` so the route can render a diagnostic.
- **`_normalize_dashboard_model_row()`** (`src/eggpool/dashboard/routes.py`): splits provider-suffixed `model_id` values using `parse_model_provider(known_providers=...)` derived from `config.providers`. Every catalog and stats row gets `base_model_id`, `provider_id`, `_model_info_lookup_id`, and `_model_id_was_suffixed` populated before the join. Rows with an existing `base_model_id` that differs from the literal `model_id` are preserved unchanged.
- **`_compute_model_info_join_stats()`** (`src/eggpool/dashboard/routes.py`): walks the post-filter dashboard rows and counts how many matched the canonical summary map using `_model_info_lookup_id` → `base_model_id` → `model_id`. Carries a five-row unmatched sample so operators can see exactly which rows failed the join.
- **`ModelInfoDashboardState` extensions** (`src/eggpool/dashboard/routes.py`): now carries `matched_row_count`, `unmatched_row_count`, and `unmatched_sample`. `render_models()` emits a visible join-failure diagnostic when `summary_count > 0`, dashboard rows exist, and `matched_row_count == 0`. The diagnostic includes the unmatched sample so operators can grep the canonical keys live.
- **Renderer lookup fallback hardening** (`src/eggpool/dashboard/render.py`): `render_models()` now consults `_model_info_lookup_id` first, then `base_model_id`, then `model_id`. Each rendered row carries `data-model-id`, `data-model-info-key`, and `data-provider-id` attributes so operators can verify the join keys via `curl ... | grep`.
- **Catalog-attached-but-empty warning** (`src/eggpool/dashboard/routes.py`): when the catalog is reachable but produces zero rows, the route logs a `WARNING` under `eggpool.dashboard.routes` so operators see "API correct / dashboard empty" as a diagnostic, not a silent failure.
- **Tests** (`tests/unit/test_dashboard_model_info_join.py`): 13 tests pin the renderer join, route normalization, silent-failure removal, and join-diagnostics behavior. The `test_handle_models_normalizes_suffixed_stats_rows_*` cases reproduce the exact "API correct / dashboard empty" regression that prompted this plan.

### Provider-Scoped Catalog Entries Accessor (Targeted Fix)

The narrow follow-up in `plans/dashboard_provider_catalog_accessor_targeted_fix.md` tightens the cache accessor that `_get_provider_scoped_catalog_rows()` already depends on so `protocol=None` rows still render as unavailable and the deprecated placeholder never leaks onto `/models`.

- **`ModelCatalogCache.get_provider_model_entries()`** (`src/eggpool/catalog/cache.py`): returns a `dict[(model_id, provider_id), dict[str, Any]]` keyed by exact provider-scoped tuple. Iteration order is stable (`sorted(self._provider_models)` by `(model_id, provider_id)`), the deprecated `__deprecated__` placeholder is filtered out, and each value is a shallow copy so mutations cannot leak back into `_provider_models`. Configured capability overrides apply via `get_provider_model_entry()` whenever `cache._config` is attached — matching the single-row accessor contract. There is **no** global fallback: rows whose only representative entry is the `_models` global row do not appear, so the dashboard always renders exact provider availability.
- **Resolved and unresolved rows both flow through**: an entry with `protocol=None` is kept (not dropped) so the dashboard can render it as `available=False, catalog_status="unavailable"` instead of silently omitting it.
- **Tests** (`tests/unit/test_catalog.py`, "Provider-scoped catalog entries accessor" block): cover exact-row presence, `protocol=None` preservation, deprecation filter, copy isolation, no-global-fallback, capability-override application, and deterministic ordering. The existing `test_effective_limits_survive_update_from_account` and `test_two_providers_retain_different_limits` tests continue to pass because `dict(entry)` preserves every cached field exactly.
- **Mock fixtures** (`tests/unit/test_catalog_service_limits.py`): the `_make_config` helper now also seeds `provider.model_capabilities = {}` and `config.model_capabilities = {}` so `MagicMock(spec=ProviderConfig / AppConfig)` exposes the override hooks the new accessor traverses.

## Model Capabilities

Protocol-neutral capability schema in `src/eggpool/catalog/capabilities.py` provides a structured representation for model capabilities, currently focused on thinking/reasoning. The schema decouples capability knowledge from any specific transcoder implementation so catalog, routing, serialization, and config code can import it without circular dependencies. See `docs/thinking.md` for the full operator guide.

### Capability Model

- **`ThinkingCapability`** — structured thinking/reasoning capability with `status` (`CapabilityStatus`), `source` (`CapabilitySource`), `native_protocols`, `client_controls` (per-protocol field mappings), `budget_tokens_min`/`budget_tokens_max`, and `effort_to_budget_tokens`
- **`ModelCapabilities`** — top-level container holding a `ThinkingCapability` field; designed to grow future capability families (vision, tools, structured outputs, prompt caching, logprobs)
- **`ThinkingClientControls`** — per-protocol field mappings for request, response, and streaming delta fields
- **`CapabilityStatus`** — `Literal["supported", "unsupported", "unknown", "mixed", "conflicting"]` where `"unknown"` means no data observed (not `"unsupported"`)
- **`CapabilitySource`** — `Literal["provider_catalog", "model_info", "manual_override", "heuristic", "aggregate", "unknown"]`

### Merge Semantics

Capability merge order is deterministic (lowest to highest priority):

1. Built-in safe defaults (absent capability = `"unknown"`)
2. Provider catalog / model-info data
3. Global model overrides
4. Provider-scoped model overrides

`merge_thinking_capabilities()` and `merge_model_capabilities()` implement override-wins semantics: the higher-priority value wins; on tie, the override is preferred. Manual overrides win over discovered metadata.

### Aggregate Semantics

Collapsed model entries may represent multiple providers. `aggregate_thinking_status()` derives a single status:

- `"supported"` only if every backing provider is `"supported"`
- `"unsupported"` only if every backing provider is `"unsupported"`
- `"unknown"` if all are `"unknown"`
- `"conflicting"` if any entry is `"conflicting"`
- `"mixed"` otherwise

`aggregate_thinking_capabilities()` produces a conservative aggregate: union of native protocols, last-wins per-protocol client controls, conservative budget bounds (max of mins, min of maxes).

### Serialization

`serialize_model_capabilities()` and `serialize_thinking_for_models()` produce a compact dict for the `/v1/models` response under the `eggpool.capabilities` namespace. Unknown/empty values are omitted.

**Provider-scoped entries** emit the full thinking capability shape including per-protocol client control field mappings (`openai_request_fields`, `openai_response_fields`, `openai_stream_delta_fields`, `anthropic_request_fields`, `anthropic_response_block_types`) when available.

**Collapsed entries** (unsuffixed model IDs) aggregate capabilities across all visible providers. When the aggregate status is `"mixed"` or `"conflicting"`, a `providers` dict maps each provider ID to its individual thinking status so clients can see per-provider truth without overclaiming support.

Example provider-scoped shape:
```json
{
  "id": "minimax-m3/minimax",
  "eggpool": {
    "capabilities": {
      "thinking": {
        "status": "supported",
        "source": "provider_catalog",
        "native_protocols": ["anthropic"],
        "openai_request_fields": ["reasoning_effort"],
        "openai_response_fields": ["reasoning_content"],
        "openai_stream_delta_fields": ["reasoning"],
        "anthropic_request_fields": ["thinking"],
        "anthropic_response_block_types": ["thinking"],
        "effort_to_budget_tokens": {"low": 1024, "medium": 4096, "high": 16384}
      }
    }
  }
}
```

Example collapsed mixed shape:
```json
{
  "id": "minimax-m3",
  "eggpool": {
    "capabilities": {
      "thinking": {
        "status": "mixed",
        "providers": {"minimax": "supported", "openrouter": "unknown"}
      }
    }
  }
}
```

### Request-Level Helpers

- `client_requests_thinking()` — heuristic check for thinking-related keys in the request body; returns `False` for unsupported/unknown/conflicting statuses
- `has_thinking_support()` — `True` when status is `"supported"` or `"mixed"`
- `classify_thinking_request()` — classifies whether a request explicitly requires thinking support, returning a `ThinkingRequestRequirement` with `required`, `client_protocol`, `fields`, `requested_effort`, and `requested_budget_tokens`
- `check_candidate_thinking_eligibility()` — determines whether a candidate model/provider is eligible for a thinking request based on its capability status and the configured policy

### Capability-Aware Routing

When a client sends a request with explicit thinking/reasoning controls, EggPool routes to ensure the upstream model can honor those controls. The pipeline is:

1. **Request classification** (`classify_thinking_request`): inspects the body for OpenAI `reasoning_effort`/`reasoning` and Anthropic `thinking`/`thinking_budget` indicators, plus assistant history `reasoning_content` blocks
2. **Eligibility filtering** (`get_eligible_accounts`): each candidate's thinking capability status (from the catalog cache) is checked against `[transcoder.capability_policy]` settings
3. **Candidate selection** (`select_account`/`select_accounts_for_failover`): only thinking-eligible candidates are considered; `CapabilityError` is raised if none remain
4. **Error responses**: `CapabilityError` (HTTP 400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)

**Policy configuration** (`[transcoder.capability_policy]`):

```toml
[transcoder.capability_policy]
unsupported_thinking = "reject"       # reject | warn_drop | route_best_effort
unknown_thinking = "reject"           # reject | allow_with_warning | route_best_effort
mixed_collapsed_thinking = "filter"   # filter | reject | allow
```

Default policy is `reject` for all axes — a client explicitly asking for thinking gets either a compatible upstream or a clear error. The `route_best_effort` escape hatch ignores the status entirely. `mixed_collapsed_thinking = "filter"` silently drops non-thinking providers when a model is served by multiple providers; if no supported providers remain, the original unfiltered list is returned. `conflicting` status is always rejected — operators resolve conflicts via manual overrides (`[model_capabilities."<model>".thinking]`), which are merged before the eligibility check runs. `CapabilityError` carries `model_id`, `capability`, `requested_fields`, and a human-readable `message`.

### Design Principle

Protocol compatibility alone does not imply thinking support. An OpenAI-protocol model may or may not support reasoning controls; an Anthropic-protocol model may or may not support extended thinking. The capability schema captures this explicitly.

### Model-Info Capability Enrichment

`build_canonical_detail()` in `src/eggpool/model_info/service.py` merges thinking capability metadata from provider catalogs and external model-info sources into the canonical detail block under `capabilities.thinking`. The merge priority is:

1. **Provider catalog** data (highest — authoritative)
2. **External model-info** data (OpenRouter, etc. — advisory)
3. **Global config override** (`[model_capabilities."<model_id>".thinking]`)
4. **Provider-scoped config override** (`[providers.<id>.model_capabilities."<model_id>".thinking]`)

Provider catalog data always outranks external source data. When two external sources disagree, the merged status is set to `"conflicting"` with details preserved in the `notes` field.

Only explicit API-control documentation produces `status = "supported"`. For example, OpenRouter's `supported_parameters` listing "reasoning" or "thinking" is treated as explicit API-control evidence. Vague descriptions like "reasoning model" or "thinking model" do NOT produce `status = "supported"` — they remain `unknown`.

`_propagate_enriched_capabilities()` writes the enriched thinking capability back to the catalog cache during reconciliation, so `_copy_exposed_model` picks it up before config overrides are applied. Provider-native thinking capabilities (source == "provider_catalog") are never overwritten by model-info enrichment.

See `plans/thinking_reasoning_phase_04_model_info_enrichment.md` for the full design.

## Model Context Limits

EggPool supports configurable effective context limits per model per provider, allowing operators to advertise smaller context windows than the provider physically supports.

### Configuration

- **`ModelLimitOverrideConfig`** — reusable Pydantic model with `max_context_tokens`, `max_input_tokens`, `max_output_tokens`, `enforce_context_limit`
- **Global overrides** — `[model_overrides.<model-id>]` applies to all providers
- **Provider overrides** — `[providers.<id>.model_overrides.<model-id>]` per provider

### Resolution

`ModelLimitResolver` in `catalog/limits.py` resolves effective limits per field with precedence:
1. Provider-specific override
2. Global override
3. Upstream-reported metadata
4. Unknown (None)

### Exposure

- **Unsuffixed models** — `conservative_limits()` takes the minimum across all visible providers
- **Provider-suffixed models** — each provider's exact limits are preserved
- **`/v1/models`** — includes namespaced `eggpool.limits` extension for observability

### OpenCode Integration

`eggpool configsetup opencode` generates OpenCode provider config with explicit `limit.context`, `limit.input`, and `limit.output` per model. This drives OpenCode's native compaction machinery.

Models whose `capabilities.thinking.status` is `"supported"` receive a `"thinking": "supported"` annotation in the generated entry. All other thinking statuses (`"unknown"`, `"unsupported"`, `"mixed"`, `"conflicting"`) are omitted so the config never claims thinking support without confirmed upstream backing. Mixed collapsed models do not silently appear as uniformly thinking-capable.

## Daemon Mode

`eggpool serve` runs in daemon mode by default. It is a one-shot detach
helper for personal / SBC deployments. It validates the configuration,
refuses to start a second instance, spawns a detached child running the
normal foreground `serve` command, and returns promptly with a short
success message pointing at the log file.

The parent only validates the config and refuses to start a second
instance. The detached child runs the foreground supervisor (Granian +
worker) unchanged. No daemon flag is forwarded to the child; detachment
is purely a parent-side concern. The child owns its own PID file
lifecycle via `runtime.write_pid_file()` /
`runtime.clear_pid_file()`.

### Detach mechanics

- `start_new_session=True` so the child survives shell exit and signals to the parent CLI do not propagate
- `stdin=subprocess.DEVNULL` to detach from the calling terminal
- `stdout`/`stderr` redirected to a log file (or `/dev/null` when `--quiet` is set without `--log-file`)
- Default log file: `~/.local/state/eggpool/eggpool.log` (resolvable via `eggpool.runtime_paths.default_log_file()`); override with `--log-file PATH` or `$EGGPOOL_LOG_FILE`. A log file beats `/dev/null` by default because a silent background failure is hard to diagnose
- The `subprocess.Popen` handle is intentionally not awaited by the CLI parent; the parent returns as soon as the child has been spawned

### PID file resolution

PID file path resolution lives in `eggpool.runtime_paths.default_pid_file()` and is the single source of truth shared by `serve`, `croncheck`, `ensure-running`, `stop`, `restart`, systemd, and the cron watchdog. Precedence:

1. `$EGGPOOL_PID_FILE` (if set)
2. `$XDG_RUNTIME_DIR/eggpool.pid` (if `XDG_RUNTIME_DIR` is set)
3. `~/.local/state/eggpool/eggpool.pid` (state dir auto-created)
4. `/tmp/eggpool-<UID>.pid` (UID-scoped fallback)

The `eggpool.constants.PID_FILE` constant is now a `_PIDFileProxy` that
resolves through `default_pid_file()` on every read, so the constant
inherits the same resolver for backwards compatibility with code that
imports it directly.

### Root-user guard

`eggpool serve` refuses to run when the effective UID is 0 unless
`--as-root` is passed. This prevents accidentally starting a personal
deployment as root; the explicit flag exists for intentional system-wide
installs. systemd production deployments should run foreground `serve --verbose`
under the systemd unit (with `User=` set) and must not use daemon mode.

### `runtime.start_server()` signature

`runtime.start_server()` accepts:

```python
def start_server(
    config_path: str,
    *,
    cwd: str | None = None,
    daemon: bool = True,
    log_path: str | None = None,
    quiet: bool = True,
    verify: bool = False,
    verify_timeout_s: float = 3.0,
) -> subprocess.Popen[bytes]:
    ...
```

`runtime.restart_server()` accepts the same `daemon`, `log_path`, and
`quiet` options. The CLI flags `--log-file`,
`--quiet`, and `--as-root` map directly to these parameters.

### Installation and Deployment

The install / deploy / uninstall surface is split across two source
modules and one CLI module so the responsibility is explicit:

- **`eggpool.deploy_user`** — user and path resolution:
  - `DeployUser` dataclass (`user`, `uid`, `gid`, `home`, `is_root`, `is_sudo`)
  - `resolve_deploy_user()` — handles normal, sudo (`SUDO_USER`/`SUDO_UID`/`SUDO_GID`), and direct-root cases via `pwd.getpwnam` / `pwd.getpwuid`
  - `resolve_config_path()` — `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml` (single source of truth for every CLI command)
  - `resolve_env_path()` — `$EGGPOOL_ENV` > `<config-dir>/.env` > XDG default
  - `default_config_dir()` / `default_data_dir()` / `default_state_dir()` / `default_config_path()` / `default_env_path()` — XDG-aware default paths honoring `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME`, `$XDG_STATE_HOME`

- **`eggpool.deploy`** — bundled snippets + dynamic builders:
  - Bundled constants: `SYSTEMD_UNIT` (the hardened production layout, byte-for-byte identical to `deploy/eggpool.service`), `LOGROTATE_CONF`, `CRON_BACKUP_FILE`, `CRON_BACKUP_SCRIPT`
  - Personal builders: `build_personal_systemd_unit()` (renders `User=`/`Group=` from the resolved `DeployUser`), `build_personal_watchdog_cron()`, `build_personal_backup_block()`, `build_personal_logrotate()`
  - Cron block management: `install_cron_block()`, `remove_cron_block()`, `strip_managed_cron_blocks()` — every block is bracketed by `# BEGIN EggPool ...` / `# END EggPool ...` markers so uninstall only strips eggpool-owned lines

- **`eggpool.cli_full.deploy_*`** — Click commands that consume the modules above:
  - `deploy systemd [--install] [--production] [--as-root]` — personal mode (default) renders the unit with `User=`/`Group=` set to the invoking user; `--production` provisions `/etc/eggpool` + `/var/lib/eggpool` + dedicated `eggpool` system user
  - `deploy cron [--install|--uninstall] [--interval N]` — watchdog (`@reboot` + `*/N * * * *` `ensure-running`), bracketed by `BEGIN EggPool watchdog` markers
  - `deploy backup-cron [--install|--uninstall] [--production]` — daily backup (user cron for personal, `/etc/cron.d/eggpool-backup` for production)
  - `deploy logrotate [--install]` — writes `/etc/logrotate.d/eggpool` and validates via `logrotate -d` (no `systemctl restart logrotate`)
  - `deploy all [--install]` — systemd + logrotate + watchdog cron (backup-cron is separate)

- **`eggpool.cli_full.uninstall`** — orchestrates `eggpool.lifecycle.uninstall.uninstall()`. Pass `--deploy-artifacts` to also remove the systemd unit, logrotate config, watchdog + backup cron blocks, and backup script. PATH edits are previewed via `preview_eggpool_path_changes()` / `RcFileChange` before being written so the operator can confirm the diff.

The production systemd unit (`SYSTEMD_UNIT` constant) is the source
of truth for the production layout. The matching file at
`deploy/eggpool.service` is kept byte-for-byte identical so both
source-checkout operators and wheel-installed users see the same
content. To update either, edit `eggpool.deploy.SYSTEMD_UNIT` AND
`deploy/eggpool.service` together.

### Filesystem Layout

Personal use (XDG defaults — overridable via `$XDG_*`):

```
~/.config/eggpool/
├── config.toml          # Main configuration
└── .env                 # Environment variables (API keys)

~/.local/share/eggpool/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

~/.local/state/eggpool/
├── eggpool.pid          # Live PID (owner: supervisor)
├── eggpool.log          # Daemon log
└── cron.log             # Watchdog cron output
```

Production (`eggpool deploy systemd --install --production`):

```
/etc/eggpool/            # Configuration + env
/var/lib/eggpool/        # Database + working state
/var/log/eggpool/        # Daemon logs
/var/backups/eggpool/    # Daily backup archives
/opt/eggpool/            # Source checkout + venv
```

## Security

- Local client credentials are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- API keys stored as environment variable names, never in SQLite
- Constant-time comparison for API key verification
- Fail-closed error detail redaction (configurable)
- Optional CORS and trusted host middleware

## Background Tasks

`TaskSupervisor` (`background/__init__.py`) manages two flavors of background work with restart-on-failure and exponential backoff:

- **daemon tasks** registered via `TaskSupervisor.register(...)` — long-lived coroutines the supervisor owns end-to-end (e.g. update checker and automatic backup, when they are not yet converted). These report `mode = "daemon"` and `next_run_at = None`.
- **periodic tasks** registered via `TaskSupervisor.register_periodic(...)` — the supervisor owns the cadence and a one-shot tick factory, recording per-tick heartbeat fields (`last_tick_started_at`, `last_tick_completed_at`, `last_tick_duration_ms`, `next_run_at`, `overdue_seconds`, `success_count`, `failure_count`, `consecutive_failure_count`). The runtime dashboard consumes `next_run_at` / `overdue_seconds` directly instead of inferring from outer-coroutine lifecycle timestamps (which previously produced false ``overdue`` warnings for healthy periodic loops). First-tick semantics: `run_immediately=True` fires the first tick without delay; `initial_delay_s` overrides the first-tick delay; default is `interval_s`. `run_immediately` and `initial_delay_s` are mutually exclusive (`ValueError`). **Scheduler policy is fixed-delay**: the next interval begins after the previous tick completes, preventing overlapping ticks. `initial_delay_s` is consumed exactly once per task lifecycle (Milestone A1) — subsequent ticks use `interval_s`. A failing first tick does not reset the initial-delay state; only `stop()`/`start()` reapplies it.

Overdue detection uses a 25%-of-interval (capped at 60s, minimum 5s) grace band so transient scheduler jitter does not trip the alert.

`SupervisedTask.snapshot()` exposes a `first_run_state` field (one of `last_success` / `last_error` / `never_run_not_due` / `never_run_startup_deferred` / `never_run_overdue`) so the runtime API and dashboard can distinguish a freshly started task that is merely waiting for its first tick from a task that has actually missed its deadline. `_first_run_state()` in `background/__init__.py` resolves the label by combining the supervisor's heartbeat fields, the configured `run_immediately` / `initial_delay_s` knob, and the same 25%-of-interval grace band used for overdue detection. Cadence diagnostics (Milestone A3) expose `configured_interval_s`, `observed_last_interval_s` (time between last two tick starts), `last_tick_drift_s` (actual tick start minus scheduled tick start), `initial_delay_consumed` (True after the one-time initial delay has been waited out), and `tick_in_progress` in the snapshot. These fields are computed without locks or database I/O.

All tasks are registered in `app.py` during lifespan setup. Periodic registrations are summarised below:

| Task | Interval | Mode | First-tick | Description |
|------|----------|------|------------|-------------|
| `catalog_refresh` | `refresh_interval_s` | periodic | staggered | Upstream model catalog refresh, model-info reconciliation after refresh |
| `model_info_refresh` | `config.model_info.refresh_interval_s` | periodic | `run_immediately=True` | Refresh due model-info rows from configured sources |
| `model_info_canonical_backfill` | 60s | periodic | staggered | Backfill canonical rows for orphaned models |
| `usage_window_refresh` | 60s | periodic | staggered | Reload persisted quota windows into memory |
| `stale_request_finalizer` | 60s | periodic | staggered | Safety net for leaked streaming requests |
| `health_disabled_models_prune` | 60s | periodic | staggered | Drop stale `model_availability` and `disabled_models` rows |
| `metrics_flush` | `config.metrics.flush_interval_s` | periodic | staggered | Buffered analytics flush to `usage_rollups` (lifespan shutdown path stops the supervisor first, then issues a final `flush(reason="shutdown")` to drain without race) |
| `retention_cleanup` | 1h | periodic | staggered | Cleanup of old requests, events, pings, rollups, expired reservations |
| `checkpoint` | 4h | periodic | `run_immediately=True` | SQLite WAL checkpoint |
| `update_checker` | 24h | periodic | `run_immediately=True` | PyPI update check; per-tick probe reuses the shared outbound client |
| `automatic_backup` | `config.backup.interval_s` | periodic | `initial_delay_s = config.backup.startup_delay_s` | In-process SQLite backup with count-based retention (preserves the historical startup wait) |

Tasks that the operator depends on for live health signalling (`update_checker`, `checkpoint`, `model_info_refresh`) use `run_immediately=True` so a freshly started process reports real state on the first dashboard paint rather than appearing as `never_run` for the entire first interval. Tasks that intentionally stagger (`stale_request_finalizer`, `health_disabled_models_prune`, `usage_window_refresh`, `metrics_flush`, `retention_cleanup`, `model_info_canonical_backfill`) keep their deterministic `initial_delay_s` offsets so first ticks never cluster on the same wall-clock second.

### Bounded maintenance and SQLite hygiene (Milestone E)

EggPool uses one process-owned primary SQLite connection with `BEGIN IMMEDIATE` transactions for predictable write serialization. Several periodic maintenance tasks (retention cleanup, stale request finalization, expired reservation reconciliation, WAL checkpoint) previously operated on unbounded row sets in single transactions, which could monopolize the writer and block dispatch persistence under sustained load.

Milestone E converts all periodic database maintenance into bounded, resumable batches:

**Maintenance budget contract** (`src/eggpool/background/maintenance.py`):
- `MaintenanceBudget` — per-task row/batch/time limits (default: 500 rows/batch, 4 batches/tick, 500ms budget)
- `MaintenancePassResult` — frozen dataclass tracking `rows_changed`, `batches_completed`, `duration_ms`, `stopped_reason`, `contention_deferrals`, `remaining_estimate`
- `ContentionGuard` — consults `Database.contention_snapshot()` lock-wait p95; defers P1/P2 tasks when write pressure exceeds `maintenance.contention_defer_above_lock_wait_p95_ms`; enforces `max_deferral_age_s` starvation cap that forces execution after the configured delay
- `MaintenanceState` — process-wide aggregator for per-task results and contention guard snapshots, wired into `RuntimeMetricsService` for `/api/stats/runtime` exposure
- `run_maintenance_pass()` — bounded batch loop with `await asyncio.sleep(0)` yields between transactions

**Task priority classes**:
- P0 (correctness recovery): expired reservation reconciliation, stale request finalization — runs unconditionally, higher budgets (1000 rows/batch), task-level timeouts via `p0_max_tick_duration_ms`
- P1 (storage safety): request/event/routing-decisions/ping/rollup/price retention — may defer under contention
- P2 (metadata repair): model-info observation cleanup — may defer under contention

**Chunked cleanup functions** (`src/eggpool/background/cleanup.py`):
- `cleanup_old_requests()` — keyset pagination on `(started_at, id)`, deletes reservations+requests per batch
- `cleanup_old_events()` — LIMIT-based pagination on `(created_at, id)`
- `cleanup_old_pings()` — chunked DELETE on `provider_pings`
- `cleanup_old_operational_events()` — chunked DELETE on `operational_events`
- `cleanup_old_routing_decisions()` — LIMIT-based pagination on `(decision_made_at, id)` via dedicated index
- `cleanup_old_usage_rollups()` — chunked DELETE on `usage_rollups`
- `cleanup_old_price_snapshots()` — chunked DELETE on `model_price_snapshots`
- `cleanup_old_model_info_observations()` — chunked DELETE on `model_info_observations`
- `reconcile_expired_reservations()` — bounded UPDATE with `WHERE id IN (SELECT ... LIMIT ?)`
- `finalize_stale_requests_once()` — bounded by `batch_size` parameter (default 500), task-level timeout

All cleanup functions populate `remaining_estimate` (1 when budget exhausted and more rows exist, 0 when fully drained, None when completed within budget) so the dashboard can signal backlog status.

**WAL checkpoint telemetry** (`checkpoint_database()` returns `{"busy", "log", "checkpointed", "duration_ms", "mode"}`).

**Runtime diagnostics** exposed via `/api/stats/runtime`:
- `db` section: WAL page count, DB page count/page size/freelist count, WAL/DB/SHM file sizes
- `maintenance` section: per-task last result (rows changed/scanned, batches, duration, stopped reason, remaining estimate, contention deferrals), contention guard state (threshold, deferrals, forced-by-starvation, elapsed since last success)

**Configuration** (`[maintenance]` in `config.toml`):
```toml
[maintenance]
max_rows_per_batch = 500
max_batches_per_tick = 4
max_tick_duration_ms = 500.0
contention_defer_above_lock_wait_p95_ms = 200.0
max_deferral_age_s = 3600.0
p0_max_rows_per_batch = 1000
p0_max_batches_per_tick = 2
p0_max_tick_duration_ms = 1000.0
```

**Schema**: migration 0050 adds `idx_routing_decisions_retention` on `(decision_made_at, id)` for the batched routing_decisions cleanup query.

All cleanup functions yield to the event loop between batches (`await asyncio.sleep(0)`) so dispatch persistence is never blocked for longer than one batch's transaction duration. Committed progress survives cancellation and resumes on later ticks.

### Operational profile logging (Milestone A6)

`_log_operational_profile()` (`src/eggpool/app.py`) emits a single structured startup log with: workers, runtime_threads, database_worker_threads, stats_db_separate, WAL/synchronous/busy_timeout, routing_trace_mode/sample_rate, metrics_write_mode/flush_interval_s, transcoder/compression/cache enabled flags, and background task counts split by process ownership vs generation-leased ownership. The log must not include secrets, provider keys, or request content. Operators find this line at INFO level during startup in the log file (`~/.local/state/eggpool/eggpool.log`).

### Operator visibility

The runtime dashboard renders the periodic-task table from the supervisor-owned snapshot fields:

- **Status** — `running` (tick in flight), `tick slow` (tick running longer than 2×interval), `failing` (registered with failures and no successes), `cancelled`, `stopped`, or `daemon` (legacy mode without next-run projection). The status badge uses `first_run_state` to refine the rendering: a `never_run_not_due` task is labelled `startup deferred` / `not yet due`, a `never_run_overdue` task is labelled `never ran (overdue)`, and a task with `last_error` and no successes shows `failing` regardless of how recent the last attempt was.
- **Next run** — `in <delta>` when `next_run_at` is in the future, `overdue <age>` when the deadline plus grace band has elapsed, or `—` for daemon tasks and tasks with no projected deadline.
- **Success/Fail** — per-tick counters so a single transient failure does not look like a permanent regression.
- **Last error** — `last_error_class` (type name) when a tick has failed.

The runtime API (`/api/stats/runtime`) also exposes `background_task_summary` (`registered`, `running`, `failed`, `overdue`, `never_run_not_due`, `never_run_overdue`, `last_error_count`) so dashboards / alerts can consume a coherent at-a-glance count without iterating every task. The new `never_run_not_due` counter tracks tasks that simply have not reached their first tick yet, separating them from `never_run_overdue` so a freshly started process is never mistaken for a broken one.

## Performance Optimization (Phases 0–5)

Correctness-preserving performance pass that reduces redundant computation and DB write pressure on the hot path. All changes are backward-compatible; no defaults change behavior.

### Phase 1 — Transcode Preflight Reuse

`PreparedTranscode` (`src/eggpool/transcoder/prepared.py`) captures the transcoder, context, and features fingerprint from the preflight step while keeping the dispatch payload immutable. The coordinator checks `prepared.is_valid_for(transcoder, features)` before re-encoding — when valid, it reuses the already-encoded upstream body and preflight warnings, avoiding a redundant `encode_request()` call. Falls back to full recompute when thinking controls or feature mismatches are detected. Debug observability lives on `PreparedTranscodeDiagnostics` via `available`, `reused`, and `recompute_reason` so the coordinator can log why reuse succeeded or failed without mutating the prepared dispatch data.

### Phase 2 — Conditional Request Segmentation

`should_segment_request()` (`src/eggpool/transcoder/segmentation_guard.py`) skips `segment_request()` when compression is disabled, synthetic cache is off, and `force_segmentation` is false. The gating decision is driven by the **resolved effective compression policy**, not the raw global config — per-client/per-protocol overrides are already folded in at this point. The coordinator tracks `segmentation_not_collected: bool` on `ProxyRequestContext` and passes it through to `RequestFinalizer`, which records `segmentation_status = 'not_collected'` instead of computing stable-prefix/volatile breakdowns.

### Phase 3 — Single-Pass Routing Plan

`RoutingPlan` (`src/eggpool/routing/router.py`) is a frozen dataclass carrying `eligible_names`, `ranked_candidates`, `fairness_decision`, `fairness_band_names`, and structured `exclusions`. `Router.build_routing_plan()` computes eligibility, tier grouping, scoring, ranking, fairness rotation, and quarantine exclusions in one pass. The coordinator calls it once instead of the previous double-call pattern (`get_eligible_account_names()` + `select_accounts_for_failover()`), eliminating redundant `get_eligible_accounts()`, `_filter_mixed_collapsed_thinking()`, and `_maybe_trigger_missing_account_recovery()` invocations. This is the authoritative selection path — there is no fallback to the legacy `select_accounts()` path. Trace sampling is decided from the request ID before optional score/exclusion detail is built; off and unsampled requests create no trace event or score-component payload.

### Phase 4 — Configurable Routing Trace Write Pressure

`RoutingTraceConfig` (`src/eggpool/models/config.py`) under `[routing.trace]` controls `routing_decisions` row persistence:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `all` / `sampled` / `off` | `sampled` | When to write routing traces |
| `sample_rate` | `0.0–1.0` | `0.05` | Deterministic request-id sampling in `sampled` mode |
| `include_score_components` | `bool` | `False` | Whether to serialize the per-account scoring breakdown |

Default `mode = "sampled"` keeps write pressure low on default installs (Raspberry Pi / SBC) where every microSD write costs latency. `sampled` mode uses a deterministic request-id hash at trace-write time, before upstream outcome is known, so it samples selection attempts rather than forcing all errors. Operators who want full diagnostic visibility for debugging should set `mode = "all"` and `include_score_components = true`. Routing trace rows are purely diagnostic — they have no effect on billing, retry, crash recovery, or routing outcomes.

### Phase 5 — Hot-Path Cleanup

- **ASGI middleware**: `_BodyLimitMiddleware` and `_HeaderRedactionMiddleware` (`src/eggpool/app.py`) replaced `BaseHTTPMiddleware` wrappers with direct ASGI classes — avoids the per-request Starlette `request.receive()` / `call_next()` overhead of the deprecated `BaseHTTPMiddleware`.
- **Log level**: `transcoded_request` log moved from `logger.info` to `logger.debug` (fires on every transcoded request — routine diagnostic data). Loss warnings remain at `logger.info`.
- **JSON body encoding**: `encode_json_body()` (`src/eggpool/request/body.py`) is the single serialization point using compact separators.

### Phase 5a — Dispatch Span Recorder Hardening

`DispatchSpanRecorder` (`src/eggpool/runtime_dispatch.py`) snapshots now
copy sample lists under the lock so concurrent appends/evictions cannot
mutate the snapshot during percentile computation. The recorder accepts
a `detailed_span_sample_rate` parameter (default `0.05`; range
`0.0–1.0`) that uses **request-coherent sampling**: a deterministic
SHA-256 hash of the request ID produces a stable per-request decision
so that one sampled request records all spans (coherent trace), rather
than an independent decision per span (which produces partial traces).
The decision is propagated to the coordinator's shared recorder instance
via a `ContextVar` so coordinator-internal spans respect the same
decision without each caller passing the flag explicitly.

The field is configurable under `[metrics.dispatch_spans].sample_rate`
with `[metrics.dispatch_spans].window_size` for the rolling-window size.
The legacy `[metrics].detailed_span_sample_rate` is deprecated but
overrides `dispatch_spans.sample_rate` when set to a non-`1.0` value.
Both are marked `LIVE` so `eggpool rehash` can adjust them without a
restart. `sampled_count` and `unsampled_count` counters are exposed in
the snapshot so operators can interpret the sampling distribution.

Spans with no recorded samples appear in the snapshot with all numeric
fields `None` so callers can distinguish "span did not run" from "span
ran in zero nanoseconds". Coarse metrics (`DispatchOverheadRecorder`,
`LocalPreUpstreamRecorder`) remain always-on and bounded regardless of
the sampling rate.

### Phase 6 — Low-Power Dashboard Performance Optimization

Default installs target Raspberry Pi and other SBC hardware where dashboard responsiveness under request load is a real operator pain point. The optimization is deliberately constrained: process workers stay at exactly one (multi-worker mode would duplicate FastAPI app state, background task supervisors, catalog refresh, provider client pools, in-memory health/routing state, and model-info services). Only intra-worker knobs and write-pressure defaults change.

#### Stats Connection Isolation

`Database` serializes all SQL operations through a single connection lock. On file-backed SQLite, dashboard analytics that share the primary connection queue behind request-path writes. The fix:

- `DatabaseConfig.worker_threads` defaults to `2` (was `1`). When `worker_threads > 1` and the database path is not `:memory:`, `app.py:_lifespan_runtime` opens a separate read-only `stats_db` connection. WAL readers see a consistent snapshot, so dashboard queries tolerate sub-second isolation.
- `StatsService(db)` is no longer constructed inside dashboard handlers. All cache, request-shaping, compression, transcoding, segmentation, tuning, and runtime routes use the lifespan-wired `app.state.stats` instance, which also owns the long-lived 30s in-memory dashboard cache.
- The CLI command `eggpool stats transcoding` still constructs a fresh short-lived `StatsService(db)` because it runs out-of-process and is bounded to a single query.

Granular dashboard handlers touched: `handle_runtime`, `handle_cache`, `handle_transcoding_stats_json`, `handle_cache_observability_json`, `handle_canonical_request_segmentation_json`, `handle_compression_observability_json`, `handle_synthetic_cache_observability_json`, `handle_compression_tuning_json`, `handle_request_shaping_json`, `handle_compression_runtime_json`, `handle_compression_policy_stats_json`, `handle_cache_stability_json`. Each now passes `use_cache=True` so repeated renders within the 30s TTL stay on the cache.

`use_cache` was added to `StatsService.get_transcoding_stats`, `get_cache_observability`, `get_canonical_request_segmentation`, `get_compression_observability`, `get_compression_runtime`, `get_compression_policy_stats`, `get_cache_stability`, `get_synthetic_cache_summary`, `get_compression_tuning_window_metrics`. API endpoints that are documented as exact remain exact — they do not pass `use_cache=True`.

#### Granian Runtime Threads

`ServerConfig.threads` defaults to `1` (single event-loop thread is canonical; values > 1 emit a startup warning). The default config example exposes the knob:

```toml
[server]
threads = 1
```

Granian still passes `workers=1`. A single runtime thread keeps all `asyncio.Lock` objects on one event loop, avoiding cross-loop contention. Values > 1 enable Granian multi-thread mode but require the operator to verify all long-lived asyncio primitives tolerate multiple event loops; a startup warning is emitted. `1` remains the documented canonical default. Startup logs the effective profile:

```text
Granian profile: workers=1 runtime_threads=N database_worker_threads=M access_log=...
```

#### Background Task Staggering

Multiple periodic tasks run at 30s or 60s cadences (`metrics_flush`, `usage_window_refresh`, `stale_request_finalizer`, `health_disabled_models_prune`, `model_info_canonical_backfill`). Each registration in `app.py:_lifespan_runtime` supplies an explicit `initial_delay_s` (5s, 10s, 15s, 25s, 40s) so first ticks do not cluster on the same wall-clock second. `background/periodic_initial_offset(name, interval_s, *, max_fraction=0.5)` is the deterministic-from-name helper for future additions; tests remain stable because the offset is `sha256(name)`-derived, not random.

Startup crash recovery (`_crash_recovery`) and the initial catalog load are NOT staggered — those run unconditionally before periodic registration, and safety-critical recovery must not be delayed.

#### Routing Trace Write Pressure

`RoutingTraceConfig.mode` defaults to `"sampled"` with `sample_rate = 0.05` and `include_score_components = False`. The default install therefore writes routing decision rows for ~5% of selection attempts instead of every attempt — a ~20x reduction in routing-decision insert volume. The deterministic request-id hash means operators still get a representative sample of traces across all accounts and tiers.

The dashboard degrades gracefully when trace data is sampled: `routing_decisions` lookups return bounded results, and `eggpool accounts explain` is unaffected. `routing.trace.mode = "all"` plus `include_score_components = true` is the documented full-diagnostics profile for operators who want every trace.

#### Dashboard Render Telemetry

`DashboardTelemetry` (`src/eggpool/dashboard/telemetry.py`) is a fixed-size (100-sample) rolling buffer per route. `record_render(route, duration_ms)` appends in O(1); `snapshot()` returns `{recent_render_ms_p50, recent_render_ms_p95, slowest_recent_route}`. Wired into `handle_overview`, `handle_models`, `handle_runtime`, `handle_cache` via `time.perf_counter()` deltas around the existing `HTMLResponse(...)` construction. No new dependencies, no per-request persistence.

The runtime snapshot (`/api/stats/runtime` → `dashboard_telemetry`) also exposes:

- `separate_stats_db`: whether the stats connection is distinct from the data-plane connection
- `runtime_threads`: effective `[server].threads` value
- `database_worker_threads`: effective `[database].worker_threads` value
- `routing_trace_mode`: effective `[routing.trace].mode` value

Operators can tell at a glance whether the install is on the recommended profile.

#### Performance Profiles

`docs/deployment.md` documents three profiles (balanced, minimum-footprint, full-diagnostics) with a symptom-to-knob troubleshooting table. The balanced profile matches the new defaults and is recommended for Raspberry Pi 4/5.

#### Tests

- `tests/integration/test_application_startup.py::test_worker_threads_two_opens_separate_stats_connection` pins the stats connection separation invariant.
- `tests/unit/test_runtime_metrics.py::test_db_stats_connection_separate` and `test_db_stats_connection_separate_true` pin the runtime-snapshot shape.
- `tests/unit/test_background.py` pins `initial_delay_s` semantics including `run_immediately` mutual exclusion and the 25%-of-interval overdue grace band.
- `tests/unit/test_routing_trace_mode.py` pins the new sampled-default and `include_score_components = false` defaults.
- `tests/unit/test_config.py::test_database_worker_threads_two_allowed` and `test_database_worker_threads_above_two_rejected` pin the `[1, 2]` range.

See `plans/2026-07-05-dashboard-low-power-performance-optimization-plan.md` for the full design.

### Phase 7 — Dashboard Graph First-Paint Latency Fix

Dashboard charts (overview request timeseries, reliability, bandwidth, timeseries, cache) can remain blank for several seconds because the current code path blocks on a broad `asyncio.gather()` of independent stats calls before returning HTML. The page shell cannot render until the full dashboard response arrives, so a slow stats query gates the entire first paint. Rollup paths exist but fall back to raw `requests` table aggregation when rollups return empty, and the fixed 30-second cache is too short for expensive historical aggregates.

Seven phases addressed the data-first rendering problem:

- **Phase 1 — Per-stage instrumentation**: extended `DashboardTelemetry` (`src/eggpool/dashboard/telemetry.py`) with `record_stage(page, stage, elapsed_ms, cache_hit)` and `stage_snapshot()` (returns `{slow_stages: [...]}` ranked by p95). Added per-namespace cache hit/miss counters (`_dashboard_cache_hits` / `_dashboard_cache_misses`) on `StatsService` exposed via `cache_snapshot()`. Runtime metrics (`src/eggpool/runtime_metrics.py`) now merge stage snapshots and cache stats into the `/api/stats/runtime` output. Dashboard route handlers (`src/eggpool/dashboard/routes.py`) instrument each stats call with named timers so regressions identify the specific slow stage.
- **Phase 2 — Diagnostic command**: `eggpool stats explain-dashboard` (`src/eggpool/cli_full.py`) prints `EXPLAIN QUERY PLAN` timings for the exact dashboard queries against the configured database, covering flat timeseries, grouped timeseries, account stats, model stats, bandwidth timeseries, and rollup queries. No new migration was needed; existing indexes suffice. Diagnostic output lives in `src/eggpool/stats/dashboard_explain.py`.
- **Phase 3 — Rollup-first chart behavior**: `StatsService.get_timeseries()` and `get_grouped_timeseries()` (`src/eggpool/stats/service.py`) now prefer rollup-backed queries for 24h, 7d, and 30d windows when rollups exist. Raw fallback is suppressed for windows > 2h when rollups return empty (returns an empty stable payload instead of scanning the raw table). A bounded live-tail merge fills the current open bucket for live charts. The rollup query limit was reduced from a hardcoded 10,000 to `limit * 4 + 100`.
- **Phase 4 — Per-namespace cache TTLs**: replaced the single `_DASHBOARD_CACHE_TTL_S = 30.0` with a per-namespace/per-period TTL table (`_DASHBOARD_CACHE_TTL_BY_NAMESPACE` and `_DASHBOARD_CACHE_PERIOD_OVERRIDES`). Defaults: 1h charts = 15s, 24h = 30–60s, 7d = 120–240s, 30d = 240–300s, options/lists = 300s. Cache counters track hit/miss rates per namespace.
- **Phase 5 — Progressive graph hydration**: chart-heavy pages render a loading shell (`_render_chart_loading_shell` in `src/eggpool/dashboard/render.py`) with a `<noscript>` fallback for no-JS. Shell containers use `data-chart-endpoint="/api/timeseries?..."` so the browser fetches chart data after the page shell arrives. `initChartLoadingShells()` in `src/eggpool/dashboard/static/dashboard.js` discovers these containers, deduplicates in-flight fetches by URL, and manages per-canvas interval handles to prevent stacked intervals after auto-refresh. Blocking `stats.get_timeseries()` calls were removed from `handle_overview`, `handle_reliability`, and `handle_bandwidth`.
- **Phase 6 — Chart.js preload**: chart pages emit `<link rel="preload" href="/static/chart.js" as="script">` so the browser starts fetching Chart.js earlier in the network waterfall. CSS loading shell styles (`src/eggpool/dashboard/static/dashboard.css`) provide a fixed-height container with spinner animation.
- **Phase 7 — Acceptance benchmarks**: `tests/perf/test_dashboard_first_paint_benchmarks.py` ships 8 acceptance benchmarks (6 small + 2 medium/slow) seeded with representative data at small (1k), medium (100k), and large (1M) scales. Regression tests in `tests/unit/test_dashboard_first_paint.py` (22 tests), `tests/unit/test_dashboard_telemetry.py` (8 tests), `tests/unit/test_dashboard_indexes.py` (9 tests), `tests/unit/test_dashboard_rollups.py::TestRollupFirstPaintBehavior` (9 tests), and `tests/unit/test_stats.py::TestDashboardStatsCache` (6 tests) pin the new behavior.

See `plans/2026-07-08-dashboard-graph-first-paint-latency-fix.md` for the full design.

### Benchmark and Regression Harness

`tests/perf/` contains baseline benchmarks and regression guards:

- `test_perf_baseline.py` — manually invoked benchmarks for representative request and routing paths
- `test_perf_regression.py` — 3 regression guards: segmentation bounded overhead, routing eligibility determinism, transcode body equivalence

Run with: `pytest tests/perf/test_perf_baseline.py -m performance -v` when a
performance comparison is useful; it is not part of ordinary CI.

## In-Memory Bounds and Memory Footprint

Long-running deployments — especially Raspberry Pi / SBC nodes — must keep steady-state RSS bounded by workload throughput, not workload cardinality. Every growth axis in the hot path is capped by a hardcoded module constant or a per-catalog config knob; see `plans/memory.md` for the full design and the per-request regression test (`tests/integration/test_memory.py`, marked `pytest.mark.slow`).

| Structure | Location | Cap | Eviction |
|-----------|----------|-----|----------|
| `QuotaEstimator.account_model_ewma` | `src/eggpool/quota/estimation.py:285` | `EWMA_HARD_CAP = 4096` (hardcoded) | LRU; on miss recomputes from persisted `QuotaWindow` |
| `QuotaEstimator.global_model_ewma` | `src/eggpool/quota/estimation.py:286` | `GLOBAL_EWMA_HARD_CAP = 1024` (hardcoded) | LRU |
| `CatalogResolverPipeline.TTLCache._data` | `src/eggpool/catalog/catalog_resolvers.py:128` | `max_entries = 4096` per `[pricing.catalogs.<name>]` (configurable) | LRU on store; `entry.raw` stripped after parse |
| `ModelCatalogCache._models` / `_provider_models` | `src/eggpool/catalog/cache.py:109-111` | De-duplicated (per-provider override only when it differs from global) | — |
| `ModelCatalogCache._account_support` | `src/eggpool/catalog/cache.py:114` | `frozenset[str]` (no per-call `.copy()`); bounded by registered account × model cardinality | — |
| `OutboundClientManager._per_host_*` | `src/eggpool/providers/outbound.py:85` | `MAX_TRACKED_HOSTS = 256` (hardcoded) | Coldest-total eviction; `evictions_total` surfaced in `snapshot()` and the `outbound_client` runtime metric |
| `AccountRuntimeState.model_availability` | `src/eggpool/accounts/state.py` | Pruned at every `AccountRegistry.sync_accounts` against advertised model set | — |
| `HealthManager.AccountHealth.disabled_models` | `src/eggpool/health/health_manager.py:111` | Pruned by `health_disabled_models_prune` supervisor task (60s cycle) | — |

The `frozenset` switch on `_account_support` (`src/eggpool/catalog/cache.py:639`) eliminates one O(n) `set.copy()` per routing decision. Every caller of `get_supporting_accounts(...)` / `get_supporting_accounts_for_model(...)` is read-only (membership, intersection, iteration), so the immutability is a strict superset of caller needs.

## Pricing Resolution and Cost Exactness

- Resolution order: global/provider `model_overrides` → upstream `/v1/models` metadata → external pricing catalogs (OpenRouter, OpenCode Zen) through the alias registry. The metadata path is `resolve_pricing_from_metadata()` in `src/eggpool/catalog/pricing_resolver.py`; external fallbacks live in `src/eggpool/catalog/catalog_resolvers.py`.
- Ambiguous bare upstream prices fail toward underestimation: absent an explicit suffix (`/token`, `/1k`, `/1M`) or an unambiguous field-name hint, the resolver defaults bare values to dollars-per-million, not dollars-per-token. Nested `pricing.cache_read` / `pricing.cache_write` fields inherit the surrounding pricing-cluster unit regime instead of hardcoding per-token semantics.
- Every resolved category is normalized to microdollars-per-million before persistence. Implausible local rates are rejected by snapshot trust gates in `apply_snapshot_trust_gates()` so a bad upstream payload cannot become the latest trusted snapshot.
- Canonical request-cost precedence in `RequestFinalizer`: `provider_reported` upstream cost wins; otherwise only trusted local `derived` / `partial` / `exact` values may become canonical. Positive local `estimated` values are routed through `choose_bounded_estimated_cost()` so, when both values are plausible, the lower value between the local estimate and `selected.estimated_microdollars` wins — a generous reservation MUST NOT silently override a tighter local estimate, and nothing later in finalization floors that choice back to the reservation. The structured `cost.reservation_fallback_suppressed` event is emitted when the reservation would otherwise dominate.
- **Reservation-fallback canonicalization** (plans/2026-07-03-...): the reservation estimate is a preflight budget, not a bill. `_QUOTA_RESERVATION_COST_CEILING_MICRODOLLARS` ($2.50) caps every reservation estimate — well below `MAX_REQUEST_COST_MICRODOLLARS` ($250) which bounds canonical cost — so a regression cannot use the reservation as canonical billing. Shared helpers (`total_billable_tokens`, `is_plausible_request_cost`, `choose_bounded_estimated_cost`) live in `src/eggpool/catalog/pricing.py` and are reused by the finalizer, the repair tool, and the dashboard summary.
- `CostCalculator.calculate_cost()` validates the **raw, pre-clamp** cost-per-token against `_MAX_TRUSTED_COST_PER_TOKEN_MICRODOLLARS` in both partial and derived paths so a wildly inflated snapshot can no longer hide behind the per-request cap. Implausible rates fall back to `_estimate_cost()` with `estimated` exactness.
- `QuotaEstimator.record_usage()` refuses to seed the EWMA on a first observation whose per-token rate exceeds `_QUOTA_ESTIMATED_COST_PER_TOKEN_MICRODOLLARS` (a unit-misclassification sample cannot permanently poison future reservations). All five `estimate_cost` tiers route through `_finalize_estimate()` which enforces both per-token and absolute reservation ceilings.
- Dashboard visibility for reservation-fallback canonicalization: `stats/queries.py` exposes `reservation_fallback_rows` and `reservation_fallback_excess_microdollars` on the global summary; `_render_reservation_fallback_warning()` (`src/eggpool/dashboard/render.py`) renders a banner card when either metric is non-zero so operators can run `eggpool stats repair-costs --apply`.
- Exactness labels on `requests.cost_microdollars`: `provider_reported`, `exact`, `derived`, `partial`, `estimated`, `unknown`. `estimated` covers both “no trusted rate exists” and “a local rate/cost was guardrailed as implausible”.
- Historical cleanup uses `eggpool stats repair-costs` for suspicious rows (dry-run by default, provider-reported rows skipped, audit rows written to `request_cost_repairs`). `repair-costs` recognizes a new suspicion class `reservation_fallback_overrode_lower_local_estimate` — canonical `cost_microdollars == reserved_microdollars` while a non-null, smaller persisted `local_cost_microdollars` exists — and prefers the persisted local estimate via `choose_bounded_estimated_cost`. `eggpool stats recompute-costs` remains the broader whole-table recalculation command.
- **Canonical cost precedence** — persisted `requests.cost_microdollars` follows: provider-reported → trusted local exact/derived/partial → bounded-estimated (via `choose_bounded_estimated_cost()` in `src/eggpool/catalog/pricing.py`). Reservation estimates are routing budgets/audit fields; they do NOT floor canonical request cost, and nothing in the finalizer may raise a chosen estimate back to the reservation after the bounded selector. Regression: MiniMax `model_id="MiniMax-M3"`, local estimated `21_848` μ$ vs reservation `5_411_079` μ$ — canonical must be `21_848`. See `tests/unit/test_request_finalizer.py::test_estimated_local_cost_beats_higher_reservation_floor_regression`.

## High-Concurrency Stream Stability (OpenCode Hardening)

OpenCode-style coding-agent clients keep many long-lived SSE streams open
in parallel. A small upstream hiccup can cascade into hundreds of pending
requests with locked-out reservations, and the symptoms can match
OpenCode's own `Failed to execute statement` reports even though EggPool
is the upstream trigger (dropped downstream responses, slow reads, lock
contention). The slice below is the layered defense.

### Stream outcome diagnostics

`StreamDiagnostics` (`src/eggpool/request/stream_diagnostics.py`) is a
process-local counter service that records every terminal streaming
path under a fixed label set:

- `stream_completed`
- `client_cancelled`
- `upstream_midstream_error`
- `stream_finalizer_timeout`
- `stream_finalizer_failed`
- `upstream_pool_timeout`
- `upstream_read_timeout`
- `upstream_connect_timeout`
- `upstream_write_timeout`
- `upstream_protocol_error`
- `upstream_connect_error`
- `upstream_transport_error`
- `stream_completed_canonical`, `stream_completed_compatibility`
- `empty_eof`, `premature_eof_before_body`, `premature_eof_midstream`, `malformed_eof`
- `response_header_timeout`, `first_byte_timeout`, `stream_idle_timeout`,
  `stream_lifetime_timeout`

Bounded ring histograms (`completed_ms`, `client_cancel_ms`,
`finalizer_timeout_ms`) keep p50 / p95 / p99 of recent samples without
unbounded memory growth. HTTPX exceptions are recorded separately as
`httpx_exception_counts`; upstream midstream errors record the
exception class under `upstream_error_class_counts`. The snapshot is
exposed under `/api/stats/runtime` and is the surface operators read
when triaging OpenCode-visible stream drops.

Provider stream timing is configured under
`[providers.<id>.stream_timeouts]`:

- `first_byte_timeout_s` bounds the interval from response headers to the
  first non-empty payload chunk;
- `idle_timeout_s` bounds the gap between payload chunks after streaming
  begins; and
- `max_lifetime_s` is retained as a deprecated compatibility field and is
  parsed but ignored; no total-lifetime timer runs in the stream loop.

When omitted, existing providers retain the HTTPX `read_timeout_s` behavior.
When explicit stream values are present, the provider client uses the largest
configured first-byte/idle value as its lower-level HTTPX read guardrail, so a
long active stream is not cut off by the historical 300-second default. The
coordinator timers use monotonic time, exclude finalization/database work, and
close the response on every timeout path. First-byte timeouts remain pre-body
retryable; idle timeouts after downstream output are midstream failures and
are never retried. Clean premature EOF remains a separate protocol-completion
outcome and is terminal once the stream response has been handed off.

### Database contention surface

`Database.contention_snapshot()` records a rolling p50 / p95 / p99
lock-wait distribution plus cumulative counters. The runtime snapshot
keys are: `lock_wait_p50_ms`, `lock_wait_p95_ms`, `lock_wait_p99_ms`,
`lock_wait_max_ms`, `lock_wait_sample_count`, `lock_wait_count`,
`cumulative_lock_wait_s`, `max_lock_wait_s`. When the p95 exceeds the
configured threshold the routing-trace guardrail skips its diagnostic
writes — see below.

### Terminal finalization convergence

The shielded immediate finalizer inside
`_build_stream_generator` is capped at 10 seconds. When SQLite lock
contention delays the immediate finalization past that ceiling, the
cancellation path used to fall back to the broad 60-second
`_finalize_stale_requests_once` sweep. `RequestFinalizationSupervisor`
(`src/eggpool/request/finalization_job.py`) closes that gap:

**Plan 026 update**: when a `RequestFinalizationSupervisor` is
available (wired through `RuntimeGenerationFactory`), the streaming
cancellation path uses a process-owned `RequestFinalizationJob`
instead of the fragile `asyncio.wait_for(asyncio.shield(...),
timeout=10)` pattern. The job is registered before the inner stream
generator; on `CancelledError`, the retained task owns finalization
even when every request waiter is cancelled. The legacy shielded path
remains as a fallback when no supervisor is available.

- **Structured**. `FinalizationResult` distinguishes durable terminal state,
  whether this invocation transitioned it, reservation convergence, and
  runtime cleanup completion. Already-terminal durable state is converged,
  not a retry failure.
- **One owner**. Retryable failures are scheduled by one supervisor timer with
  capped exponential backoff and maximum retry age. Capacity rejects before
  ownership transfer; detached terminal work is never returned.
- **Explicit recovery identity**. Request and attempt ambiguity use distinct
  strategies and explicit request/attempt/reservation IDs. Recovery reads
  named columns directly and keeps unknown statuses or mismatched tuples
  unresolved.

`FinalizationRetryQueue` is retained only as a bounded one-shot compatibility
adapter. It does not maintain an independent retry budget or drop an
already-terminal operation as failed.

The queue does NOT substitute for `_crash_recovery`: durable rows
that never converge are still recovered at every startup.
`RuntimeMetricsService._snapshot_finalization_retry_queue()` is async
and correctly awaits the queue's `snapshot()` method so the
`/api/stats/runtime` endpoint serializes retry queue state without
coroutine leaks or fallback errors.

### Routing-trace pressure guard

`RoutingTraceGuard` (`src/eggpool/request/routing_trace_guard.py`) acts as
a pre-enqueue pressure signal: it consults `db.contention_snapshot()`
before a routing trace write and skips the write when the rolling p95
lock wait exceeds `routing.trace.skip_above_lock_wait_p95_ms` (default
`200.0` ms). Skips require `>= 8` samples to avoid tripping on cold-
start spikes. Skips are counted under `skipped_db_pressure`; written
rows under `written`. The guard never raises — trace rows are
diagnostic, so their absence must never affect dispatch.

### Routing-trace async writer

`RoutingTraceWriter` (`src/eggpool/observability/routing_trace_writer.py`)
is a process-owned, single-drain-task async writer that collects
immutable `RoutingTraceEvent` objects via a non-blocking `submit()`
and persists them in micro-batches using `RoutingDecisionRepository`.
The writer owns a bounded `deque(maxlen=queue_capacity)` queue (default
1000) and a single drain coroutine. Thread-safe submission uses a
`threading.Lock` so callers from any thread or event loop can safely
enqueue. The drain loop batches up to `max_batch_size` (default 50)
events per flush interval (`flush_interval_s`, default 1.0s). Queue
overflow drops the newest event and increments `dropped_queue_full`.
The writer is process-owned (on `ProcessRuntime`, not
`RuntimeGeneration`) and is not duplicated by live rehash. Runtime
diagnostics are exposed via `/api/stats/runtime` `routing_trace_writer`
(queue depth, accepted/written/dropped counters, oldest event age).
The `RoutingTraceGuard` pressure signal is consulted *before* the event
enters the writer's queue, so under DB contention the guard filters
trace writes at the coordinator level without incurring queue overhead.

### HTTPX error classification

The coordinator classifies HTTPX failures explicitly instead of
folding them into a generic `httpx.HTTPError`:

- `PoolTimeout` — connection pool exhausted (HTTPX pool slot)
- `ReadTimeout` — upstream read stalled past `read_timeout_s`
- `ConnectTimeout` — TCP / TLS connect stalled past `connect_timeout_s`
- `WriteTimeout` — upstream write stalled past `write_timeout_s`
- `RemoteProtocolError` — upstream closed the stream midstream
- `ReadError` / `WriteError` — lower-level transport errors
- `TimeoutException` — generic catch-all (logged only when nothing
  more specific is available)

Each classification is recorded on the `StreamOutcomeEvent` and
aggregated under `httpx_exception_counts` /
`upstream_error_class_counts`.

### Reproducer and operator surface

The shared test harness (`tests/helpers/stream_stability_harness.py`)
provides canonical scenario names (`slow-stream` as primary,
`slow-token-cadence` as alias), SSE helpers, cancellation logic, and the
scenario-to-response builder used by both the integration test and the
CLI reproducer.

- `tests/integration/test_high_concurrency_streaming.py` runs 50
  concurrent mock streams with a configurable cancel rate and asserts
  the closure validation matrix (no leaked pending rows, no active
  reservations, router active counts return to zero, the finalization
  retry queue drains to zero, HTTPX / upstream error class counts are
  empty for the no-failure path, client cancellation does not register
  as an upstream error, provider health remains `healthy`).
- `scripts/repro_high_concurrency_streams.py` is the CLI mirror for
  operators without a pytest harness — runs the same harness against a
  configurable `--concurrency`, `--cancel-rate`, `--cancel-offset`,
  `--chunks-per-stream`, `--chunk-delay-s` and prints a structured
  summary. The `--scenario` flag accepts canonical names from the shared
  harness (`slow-stream`, `happy-path`, etc.) plus aliases
  (`slow-token-cadence`).
- `docs/opencode-stream-stability.md` is the operator playbook:
  symptom checklist, root-cause matrix, recovery commands
  (`eggpool runtime show`, `eggpool admin drain-finalization-queue`,
  `eggpool stats repair-reservations`, `eggpool admin
  set-routing-trace-threshold`), and capacity planning for OpenCode.
- `docs/providers.md` § High-Concurrency HTTP Client Profiles exposes
  three copy-pasteable profiles: low-power default, high-concurrency
  coding-agent streaming (`max_connections=256`, `max_keepalive=128`,
  `read_timeout_s=900`, `pool_timeout_s=60`), and diagnostic
  low-noise (`routing.trace.mode = "off"`, `read_timeout_s=1800`).

## Dispatch Stability Milestone D — Off-Path Observability

Milestone D completes the routing-trace persistence path by moving
trace writes fully off the synchronous dispatch path. The
implementation spans `RoutingTraceEvent`, `RoutingTraceWriter`,
`RoutingTraceGuard`, and coordinator integration.

**RoutingTraceEvent** (`src/eggpool/observability/routing_trace_writer.py`):
frozen dataclass carrying the trace payload — request identity, selected
account, attempt number, outcome label, timing, and optional score
components. Content-free by design: no request body, no response body,
no auth headers, no secrets.

**RoutingTraceWriter** (`src/eggpool/observability/routing_trace_writer.py`):
process-owned, single-drain-task async writer. Uses a bounded
`deque(maxlen=queue_capacity)` (default 1000) and a `threading.Lock`
for thread-safe submission from any event loop. The drain loop batches
up to `max_batch_size` (default 50) events per `flush_interval_s`
(default 1.0 s) interval. Queue overflow drops the newest event
(`dropped_queue_full`). The writer is on `ProcessRuntime`, not
`RuntimeGeneration`, so it survives generation swaps without
duplication. Runtime diagnostics exposed via `/api/stats/runtime`
`routing_trace_writer` (queue depth, accepted/written/dropped counters,
oldest event age).

**RoutingTraceGuard** (`src/eggpool/request/routing_trace_guard.py`):
pre-enqueue pressure gate consulted *before* the event enters the
writer's queue. Skips trace submission when any of: DB lock-wait p95
exceeds `skip_above_lock_wait_p95_ms` (default 200 ms, ≥ 8 samples),
queue occupancy exceeds `guard_queue_occupancy_threshold` (default 0.8),
oldest queued event exceeds `guard_oldest_event_age_s` (default 30 s),
or the writer reports recent flush errors. A hysteresis
`guard_cooldown_s` (default 5.0 s) prevents flapping. All skip reasons
are classified (`db_pressure`, `queue_pressure`, `oldest_event_stale`,
`flush_failure`, `cooldown`) and surfaced in snapshot counters.

**Coordinator integration**: trace write is Step 10 in
`_select_and_persist_attempt`, executed *after* DB persistence and
*outside* all locks (`_selection_claim_lock` was released after
Phase C publication). The guard is consulted first; on skip, the
coordinator records the skip reason and moves on. On acceptance, the
event is submitted to the writer via non-blocking `submit()`. Trace
writes never delay dispatch, even under contention or queue pressure.

**Configuration** (`[routing.trace]` in `RoutingTraceConfig`):

| Field | Default | Reload |
|-------|---------|--------|
| `mode` | `sampled` | LIVE |
| `sample_rate` | `0.05` | LIVE |
| `include_score_components` | `False` | LIVE |
| `skip_above_lock_wait_p95_ms` | `200.0` | LIVE |
| `guard_queue_occupancy_threshold` | `0.8` | LIVE |
| `guard_oldest_event_age_s` | `30.0` | LIVE |
| `guard_cooldown_s` | `5.0` | LIVE |
| `queue_capacity` | `1000` | RESTART_REQUIRED |
| `flush_interval_s` | `1.0` | RESTART_REQUIRED |
| `max_batch_size` | `50` | RESTART_REQUIRED |
| `shutdown_flush_timeout_s` | `5.0` | RESTART_REQUIRED |

Tests: `tests/unit/test_routing_trace_writer.py`,
`tests/unit/test_routing_trace_guard.py`,
`tests/unit/test_routing_trace_mode.py`,
`tests/perf/test_trace_mode_perf.py`.

## Dispatch Stability Milestone F — Runtime Concurrency and Hot-Path Hardening

### Supported Runtime Model

EggPool uses **Model 1: Single runtime loop is canonical**. The `server.threads` default is `1`; values > 1 emit a startup warning. This decision is based on:

- All `asyncio.Lock` objects are loop-bound and would fail under multi-loop access
- SQLite serialization is the actual concurrency bottleneck, not Python GIL
- Making everything thread-safe would be disproportionate complexity for minimal benefit

The async primitive audit (`docs/async_primitive_audit.md`) documents every long-lived primitive's loop ownership and cross-loop safety strategy.

### Event-Loop Lag Monitor

`EventLoopLagMonitor` (`src/eggpool/event_loop_lag.py`) measures event-loop starvation via periodic callback drift. It uses `loop.call_later()` to schedule a callback at a configurable cadence (default 1.0s, suitable for SBCs). Each callback records the drift between expected and actual wake time. The monitor is process-owned, uses a bounded rolling window (200 samples), and exposes p50/p95/p99/max lag in milliseconds via `/api/stats/runtime`.

### Metrics Coalescer Thread Safety

`MetricsWriteCoalescer` (`src/eggpool/metrics/buffer.py`) uses dual locks:
- `threading.Lock` for buffer mutation (safe from any thread)
- `asyncio.Lock` for async flush serialization

`record_usage()` acquires only the thread lock (never blocks on I/O). `flush()` acquires the async lock first, then the thread lock to snapshot the buffer, then releases both before performing I/O. Cancellation-safe restore uses the thread lock.

### Hot-Path Optimizations

- **ParsedRequestPayload** (`src/eggpool/request/parsed_payload.py`): caches the original JSON parse and derived state (model, streaming, thinking requirement) to avoid repeated parsing per request
- **estimate_padded_size()** (`src/eggpool/request/payload_utils.py`): replaces synthetic `b"\x00"*padding` allocation with a length-based API
- **ImmutableRequestState** (`src/eggpool/runtime_manager.py`): precomputes provider/account/header frozensets per generation, invalidated naturally through generation swap
- **build_upstream_headers()** (`src/eggpool/proxy/client.py`): combines header sanitization + auth injection in a single pass

### Bounded Runtime Diagnostics

`/api/stats/runtime` exposes bounded resource diagnostics for operators:
- DNS cache: max entries, current size, utilisation %, evictions total
- Provider client pool: provider count, per-provider details
- Stream diagnostics: histogram capacities and sample counts

### Tests

- `tests/unit/test_granian_topology.py`: verifies single-process, single-loop model; task supervisor count; writer identity; generation identity; shutdown behaviour
- `tests/unit/test_header_forwarding.py`: exhaustive header pass/drop tests
- `tests/unit/test_resource_plateau.py`: DNS cache, client pool, and stream diagnostics boundedness
- `tests/unit/test_parsed_payload.py`: ParsedRequestPayload parse caching and derived state
- `tests/unit/test_payload_utils.py`: estimate_padded_size arithmetic-only API
- `tests/unit/test_immutable_request_state.py`: ImmutableRequestState frozen dataclass and generation swap
- `tests/unit/test_metrics_coalescer_invariants.py`: concurrency invariants (total accounting, no negative counters, no lost updates, cancellation restore)
- `tests/unit/test_telemetry_bounded_growth.py`: bounded deque/histogram growth for all telemetry recorders
- `tests/unit/test_hotpath_equivalence.py`: consolidated 10-scenario hot-path regression (OpenAI/Anthropic native, streaming, large tools, invalid JSON, header security, payload caching, allocation-free estimation)
- `tests/unit/test_synchronization_hardening.py`: RuntimeManager concurrency, DNS singleflight cancellation, telemetry shard bounds, metrics coalescer rapid cycling
- `tests/perf/test_hot_path_performance.py`: before/after measurements for parse, allocation, header, span, and lag monitor
- `tests/perf/test_concurrent_workload_matrix.py`: end-to-end performance matrix — serial, 10/25/50 concurrent, streaming, large body, mixed workloads with dispatch overhead and span recorder metrics

### Runtime stability and optional validation

Production exposes bounded runtime telemetry for diagnosis: resource
plateaus, stream outcomes, dispatch spans, and database health are available
through `/api/stats/runtime`. These signals are best-effort operational data;
they are not fixed CI percentile gates or retained release evidence. The
ordinary correctness floor is the focused tests plus `tests/smoke/`, which
covers request ownership, canonical streaming completion, premature EOF, and
request-local failure recovery. Target-device runs and
`scripts/repro_high_concurrency_streams.py` are optional tools for
stream-specific diagnosis.

## Live Configuration Rehash — Validation, Diffing, and Fail-Closed CLI

Milestone A ships a shared validation contract, a typed configuration
diff and reload-policy layer, and a fail-closed `rehash` CLI command.
The foundation is consumed by milestones B and C without API changes.

Milestone C (control plane and transactional reload) is complete: the
control socket at `~/.local/state/eggpool/eggpool.sock` accepts
validated digests, the server re-validates, computes a diff, rejects
restart-required changes, builds a candidate generation, reconciles
persistence, atomically publishes, and retires the old generation.

The **closure pass** (Phases 1-5) enables the first deliberately bounded
set of `LIVE` fields so `eggpool rehash` actually applies supported
configuration changes to a running process. The new ownership map:

- **Provider definitions and accounts** (`[providers.<id>]`,
  `[[providers.<id>.accounts]]`, model endpoints, static models,
  protocols, authentication, headers, weights, enabled flags) are
  `LIVE`. The diff algorithm inherits `LIVE` for expanded per-key
  paths (`providers.<id>`, `accounts.<provider>/<name>`,
  `model_overrides.<id>`, `model_capabilities.<id>`) so adding,
  removing, or editing a provider or account publishes a new
  generation without restarting.
- **Routing and scoring knobs** under `[routing]` (strategy,
  fairness, scoring penalties, retry limits, quota advisory mode,
  trace policy) are `LIVE`.
- **Background-task cadences and process-bound construction**
  remain `RESTART_REQUIRED` (server bind, Granian construction,
  database path, middleware, security headers, metrics topology,
  backup paths, transcoder/compression storage topology).

Mixed live + restart-required changes are rejected entirely (no
partial application); the CLI returns exit code `2` so scripts and
deployment tooling can detect the situation. See
`plans/2026-07-13-live-config-rehash-closure-plan.md` for the
complete closure criteria.

#### Closure pass D1 — request-policy expansion

The D1 milestone extends the `LIVE` inventory to the request-path
policy fields that are already generation-owned by the candidate
builder (Milestone B wired `transcoder_policy`, `compression_policy`,
`cache_config`, and `compression_tuning_registry` into
`_build_candidate_generation` in `control/reload_manager.py`):

- **Transcoder policy** (`[transcoder]`) is `LIVE` — `transcoder_loss_policy`,
  `protocol_safety_mode`, `http_status_overrides`, and the entire
  subtree. A change is hot-swapped by constructing a new
  `TranscoderPolicy` from the candidate config and wiring it into the
  new generation's `RequestCoordinator`; identity is preserved
  (`is`/`id()`) so existing coordinators keep the old policy until
  their `GenerationLease` releases.
- **Compression policy** (`[compression]`, including
  `[compression.synthetic_cache_controls]`) is `LIVE` —
  `enabled`, `observe_only`, `max_request_body_bytes`,
  `max_output_tokens`, `prefer_native_cache`, `prefer_native_min_tokens`,
  `provider_cache_equivalents`, `overrides`, and the entire subtree.
  `RuntimeCompressionPolicyOverrideRegistry` is rebuilt for each
  generation so per-provider override changes take effect without a
  restart.
- **Cache synthesis controls** (`[cache]`, including
  `[cache.synthetic_cache_controls]`) are `LIVE`. The cache config
  feeds `_apply_synthetic_cache_controls` on the new generation.
- **Models subset** (`[models]`) — `expose_mode`, `collapse_models`,
  `refresh_interval_s`, `stale_after_s`, `allow_stale_catalog` are
  `LIVE`. Catalog objects are generation-owned and re-read the
  candidate config via `self._config`. Startup-only fields
  (`startup_refresh`, `ping_retain_days`,
  `catalog_withdrawal_policy`) remain `RESTART_REQUIRED` because they
  bind to the Granian lifespan and the SQLite persistence layer.
- **Security error-detail persistence** (`security.persist_redacted_error_detail`)
  is `LIVE`. The flag is threaded into the candidate
  `RequestCoordinator` via the `persist_error_detail=` kwarg.

Fields that stay `RESTART_REQUIRED` (intentionally):

- `[upstream]` — vestigial; only consulted at runtime-task startup
  (`runtime_tasks.py:248`). A later milestone would need a full
  rewrite to migrate the registry into a generation-owned object.
- `[model_info]` — `ModelInfoService` is constructed in the FastAPI
  lifespan (`app.py:935`) and is not rebuilt by the candidate
  manager. A follow-up milestone must refactor the service to read
  from `self._config` before `model_info.*` can become `LIVE`.

Mixed live + restart-required changes still reject entirely with
exit code `2`. The expanded inventory is pinned by
`tests/unit/test_config_reload_policy.py::test_live_field_inventory_matches_expected`
and inheritance is pinned by
`tests/unit/test_config_reload_policy.py::test_request_policy_sub_paths_inherit_live`.
Identity separation between active and candidate policies is pinned
by `TestMilestoneD1CandidateBuild` in `tests/unit/test_reload_manager.py`,
and end-to-end behavioral reload is pinned by the `test_d1_*`
tests in `tests/integration/test_rehash_streaming_swap.py`. See
`plans/2026-07-14-live-config-rehash-final-milestone-d1-request-policy-expansion.md`
for the rollout plan.

#### Closure pass D2 — background and observability expansion

The D2 milestone extends the `LIVE` inventory to background-task
cadences and retention durations.  It introduces a dual-ownership
model via `TaskOwnership` (`src/eggpool/runtime_task_inventory.py`):

- **Process-owned** tasks (`checkpoint`, `metrics_flush`,
  `update_checker`, `automatic_backup`) register on
  `process.process_supervisor` and survive generation swaps.
  Only one instance exists; reconfiguration mutates the schedule
  in place via `apply_spec_diff()`.
- **Generation-leased** tasks (`catalog_refresh`,
  `model_info_refresh`, `model_info_canonical_backfill`,
  `retention_cleanup`, `usage_window_refresh`,
  `finalization_retry_drain`, `stale_request_finalizer`,
  `health_disabled_models_prune`) acquire a generation lease on
  every tick and are retired when their generation is retired;
  a new generation gets a fresh registration.

The `RUNTIME_TASK_INVENTORY` tuple (`src/eggpool/runtime_task_inventory.py`)
is the single reviewable inventory driving both startup and reload.
`inventory_for_config()` resolves enabled state from the live config.
`build_task_specs()` (`src/eggpool/runtime_tasks.py`) builds spec
tuples from the inventory; `compute_spec_diff()` and
`apply_spec_diff()` drive the live transition.

D2 LIVE families and their consumers:

- **Retention durations**: `dashboard.retain_request_stats_days`,
  `dashboard.retain_event_days`, `models.ping_retain_days` — read
  by `retention_cleanup` and `stale_request_finalizer` closures
  that re-read `gen.config` per tick.
- **Upstream timeout**: `upstream.read_timeout_s` — applied to
  outbound HTTPX clients via `OutboundClientManager`.
- **Metrics flush cadence**: `metrics.flush_interval_s` — mutates
  the process-owned `metrics_flush` schedule in place.
- **Backup scheduling**: `backup.enabled`, `backup.interval_s`,
  `backup.retain_count`, `backup.startup_delay_s` — mutates the
  process-owned `automatic_backup` schedule in place.  Toggling
  `enabled` adds/removes the task.
- **Model-info scheduling**: `model_info.enabled`,
  `model_info.refresh_interval_s` — mutates the generation-leased
  `model_info_refresh` and `model_info_canonical_backfill` tasks.
  Toggling `enabled` adds/removes the tasks; changing
  `refresh_interval_s` replaces the schedule with the new cadence.

The `_run_periodic_loop` in `src/eggpool/background/__init__.py`
re-reads `self._interval_s` and `self._initial_delay_s` each
iteration so live interval changes take effect at the next tick
boundary without requiring a task restart.

Process-owned task resources retain identity across reloads — no
duplicated schedules, no orphaned tasks.  The `update_checker` task
now lives on the process supervisor and survives reloads (was a
regression on reload before D2).

Process-bound storage/deployment fields remain `RESTART_REQUIRED`
(database path, backup destination paths that cross permission
boundaries, control socket).

`ProcessRuntime` (`src/eggpool/runtime_manager.py`) now carries
`process_supervisor`, `task_spec_version`, and
`last_task_transition` fields.  `task_spec_version` increments on
each `apply_spec_diff` call; `last_task_transition` records the
added/removed/changed/unchanged counts.  Diagnostics are exposed
under `/api/stats/runtime` via `_snapshot_runtime_manager`
(`src/eggpool/runtime_metrics.py`).

Fields that stay `RESTART_REQUIRED` (intentionally):

- `[model_info]` — `ModelInfoService` is constructed in the
  FastAPI lifespan and is not rebuilt by the candidate manager.
- Process-bound storage/deployment paths (database, backup
  destinations, control socket).

Tests: `tests/unit/test_runtime_task_inventory.py` (35 tests),
`tests/unit/test_d2_transitions.py` (15 tests covering interval
changes, enable/disable, retention policy, metrics/backup cadence,
rapid reloads, observability), and `tests/unit/test_runtime_tasks.py`
extended with `TestProcessSupervisorRouting` and
`TestProcessSupervisorSurvival`.  See
`plans/2026-07-14-live-config-rehash-final-milestone-d2-background-observability-expansion.md`
for the rollout plan.

#### Closure pass D3 — release validation and security

D3 is a release-hardening milestone with no new LIVE fields.  It
closes secret-redaction gaps: `_record_event`
(`src/eggpool/control/reload_manager.py:547`) passes error text
through `sanitize_text_for_audit()` (`src/eggpool/config_reload_policy.py:348`)
before persisting to operational events, and `_redact_message`
(`src/eggpool/cli_rehash_format.py:65`) applies the same helper
to CLI output.  Three test-only seams (`TEST_INJECT_BUILD_FAILURE`,
`TEST_INJECT_RECONCILE_FAILURE`, `TEST_INJECT_PUBLISH_FAILURE` on
`ReloadManager`) enable deterministic failure injection.  The
exhaustive inventory audit (`tests/unit/test_reload_inventory_audit.py`)
caught two gaps: `dns_cache.ttl_seconds` (actual path is
`network.dns_cache.positive_ttl_seconds`) and 14 missing
`pricing.catalogs.*` entries.  Performance baseline: reload p50
≈ 480 ms, p95 ≈ 750 ms; concurrent-traffic p95 < 750 ms.
See `plans/2026-07-14-live-config-rehash-final-milestone-d3-release-validation-and-closure.md`.

### Validation contract

`src/eggpool/config_validation.py` owns the reusable, Click-free
`validate_config_file()` function. It accepts a config path and returns
a `ConfigValidationResult` without raising `SystemExit`. Every failure
path is a typed subclass of `ConfigValidationError(ConfigError)`:

| Class | Meaning |
|-------|---------|
| `ConfigFileAccessError` | File missing, unreadable, or not a regular file |
| `ConfigParseError` | TOML syntax error |
| `ConfigSchemaError` | Pydantic validation failure |
| `ConfigStartupAuthError` | Config-level auth constraints violated |
| `ConfigAccountCredentialError` | Account credential validation failed |
| `ConfigInternalError` | Unexpected internal error |

Both `check-config` and `rehash` call the same helper. The result
carries two distinct hashes:

- **`content_digest`** — SHA-256 of the exact file bytes. Used for
  time-of-check / time-of-use drift detection: if the digest changes
  between validation and restart, the process must re-validate.
- **`runtime_fingerprint`** — deterministic, secret-safe canonical
  hash. Secret fields (API keys, tokens) are redacted to
  `"<redacted>"` before fingerprinting. Used for no-op detection
  ("is the running config identical to the new one?") and diagnostics.

### Reload policy

`src/eggpool/config_reload_policy.py` defines the typed diff and
reload-policy layer.

**Dispositions.** `ReloadDisposition` is an enum with three values:

- `LIVE` — the field can be hot-swapped without a restart.
- `RESTART_REQUIRED` — changing the field requires a service restart.
- `IGNORED` — the field is ignored for reload purposes (e.g.
  logging-only fields).

The `_FIELD_DISPOSITION` map is the single reviewable inventory of
every `AppConfig` field and its disposition. **In milestone A every
field defaults to `RESTART_REQUIRED`** (fail-closed). No field is
currently `LIVE`.

**Diff shape.** `ConfigChange` carries:

- `field_path` (dotted string, e.g. `"server.threads"`)
- `old_value` / `new_value` (the raw values from each config)
- `disposition` (from `_FIELD_DISPOSITION`)
- `secret` (bool — when `True`, the value renders as `<changed>` in
  repr and never appears in logs or diagnostic output)

Account and provider order is normalized before diffing so reordering
`[[providers.*.accounts]]` rows does not produce spurious changes.

`ConfigDiff` is the collection of `ConfigChange` entries plus a
`content_digest` and `runtime_fingerprint` for the new config.
`compute_diff(old, new)` produces a `ConfigDiff` from two parsed
configs; `diff_from_validation(result)` produces one from a validation
result when only one config is available.

### Wire types for milestone C

`ReloadStage` and `ReloadResult` are the protocol-neutral types that
milestone C's control socket speaks directly:

```python
class ReloadStage(Enum):
    VALIDATION = "validation"
    DIGEST_CHECK = "digest_check"
    DIFF = "diff"
    PREPARATION = "preparation"
    RECONCILIATION = "reconciliation"
    COMMIT = "commit"
    RETIREMENT = "retirement"

@dataclass(frozen=True)
class ReloadResult:
    ok: bool
    stage: ReloadStage
    generation: int | None
    changed_sections: tuple[str, ...]
    warnings: tuple[str, ...]
    restart_required: tuple[str, ...]
    retirement_pending: bool
    message: str
```

### CLI surface

Both `check-config` and `rehash` flow through `validate_config_file()`:

- **`eggpool check-config`** — validates the config, prints warnings,
  and exits. Output now includes "Content digest: <hex>".
- **`eggpool rehash`** — runs the same validation, connects to the
  running server's control socket, sends the validated content digest,
  and renders the structured result (changed sections, generation
  number, retirement status, warnings). Exits zero on success
  (including semantic no-op). On validation failure, the preflight
  exits nonzero with "Live configuration is unchanged. Refusing to
  apply an invalid config and never invoking restart." The closure
  pass enabled `LIVE` fields (provider/account/routing/model-override
  families), so supported config changes are applied without
  restarting. The `--json` output is standardized via
  `cli_rehash_format.format_rehash_json()` and always includes 9
  keys: `ok`, `stage`, `exit_code`, `generation`, `changed_sections`,
  `warnings`, `restart_required`, `retirement_pending`, `message`.

### Runtime generations and control plane

Milestone B introduces a `RuntimeManager` that can apply `LIVE`-
disposition fields without a restart. Milestone C is complete: the
control-plane socket accepts a validated `ConfigDiff` and applies
`LIVE` fields atomically. Both milestones consume the same
`ConfigDiff`, `ConfigChange`, `ReloadResult`, and `ReloadStage`
types defined here. The control socket is wired into the FastAPI
lifespan (`app.py:_lifespan_runtime`), and the `ReloadManager` is
the single transaction entry point for all reload operations.

### Control server protocol (Milestone C)

The control server (`src/eggpool/control/server.py`) exposes a
single-shot newline-delimited JSON protocol on a Unix-domain socket.
One request per connection, structured response, then close.

**Socket**: `~/.local/state/eggpool/eggpool.sock` with `0o600`
(owner-only). Stale sockets are cleaned up on start and stop.

**Protocol v1** wire format:

- Request: `{"protocol_version": 1, "request_id": "<uuid>",
  "command": "reload_config", "validated_digest": "<sha-256>"}`
- Response: `{"protocol_version": 1, "request_id": "<uuid>",
  "ok": true|false, "stage": "<stage>", "exit_code": N,
  "generation": N, "changed_sections": [...], "warnings": [...],
  "restart_required": [...], "retirement_pending": bool,
  "message": "..."}`

The `stage` field is one of the `ReloadStage` enum values
(`validation`, `digest_check`, `diff`, `preparation`,
`reconciliation`, `commit`, `retirement`) plus the control-plane
sentinel `reload_in_progress` (busy). The `exit_code` mirrors the
process exit code and is always present so programmatic consumers
do not need to map stages themselves.

**Security model**: socket mode `0o600` prevents unprivileged
clients on shared hosts from issuing reload commands.

### Reload manager transaction flow (Milestone C + Phase 6)

`src/eggpool/control/reload_manager.py` implements `ReloadManager`,
which orchestrates the complete reload transaction under a serializing
`asyncio.Lock`.  Phase 6 introduces an application-level transaction
(`ReloadTransaction` in `src/eggpool/reload_transaction.py`) with a
monotonic state machine, prepared deltas, and a narrow commit point.

1. **Atomic admission claim** — `_claim_mutex` + `_reload_claimed`
   eliminate the TOCTOU race on concurrent reload attempts. The claim
   is acquired and released under `_mutex`; concurrent callers
   see `_reload_claimed == True` and raise `ReloadInProgressError`.
   The claim state is exposed via `ReloadManager.snapshot()` diagnostics
   (`admitted`, `admitted_at`, `admitted_request_id`).
2. **Digest validation** — Verify the content digest matches the
   CLI-validated bytes (prevents TOCTOU races).
3. **Diff** — `compute_diff(old_config, new_config)` produces a
   `ConfigDiff` with per-field dispositions.
4. **Restart-required gate** — Any `RESTART_REQUIRED` change causes
   the entire operation to be rejected with the offending field paths.
5. **No-op detection** — Identical fingerprints return success without
   building a new generation.
6. **Candidate preparation** — `RuntimeGenerationFactory.prepare()` builds
   a new `RuntimeGeneration` off to the side (router, DB, app state)
   without touching the active generation.  Process-supervisor task
   reconfiguration (`apply_spec_diff`) is **not** called here — it is
   deferred to the commit phase (step 10) to avoid leaving the process
   supervisor in a partially-reconfigured state on failure.
7. **Prepare persistence delta** — Calculate providers/accounts to
   sync without applying them.  Returns an immutable `PersistenceDelta`.
8. **Prepare process transitions** — Calculate task specs and callback
   factories without applying them.  Returns a `ProcessTransitionPlan`.
9. **Pre-commit verification** — Revalidate shutdown state, active
   generation ID/digest, and candidate ownership before entering
   the commit guard.  A concurrent reload that advanced the
   generation causes commit rejection.
10. **Commit** (narrow commit guard):
    a. Apply persistence delta and publish candidate generation within a single SQLite transaction. The delta SQL is applied first (inside the transaction), then the runtime pointer swap occurs. If publication fails, the SQLite transaction rolls back automatically, leaving provider/account state identical to the pre-reload state.
    b. Transfer candidate ownership to the runtime manager.
    c. Apply process transitions (`apply_spec_diff`) — after the SQLite commit.
    d. Update observable state.
    e. Schedule old-generation retirement.
11. **Completion** — Mark transaction completed.

The `ReloadTransaction` state machine tracks every transition:
`created → validated → diffed → candidate_prepared →
persistence_prepared → process_transitions_prepared →
commit_started → runtime_published → process_transitions_applied →
persistence_committed → observable_state_updated →
retirement_scheduled → completed`.

Post-publication failures are compensated by accepting the new
generation and retrying process transitions (the persistence delta
is idempotent).  Compensation failure transitions through
`ABORTING → COMPENSATION_FAILED`.  Cancellation after publication
is shielded to prevent mixed state.

Shutdown coordination: `ReloadManager.wait_for_transaction_completion()`
allows shutdown to wait for an in-flight transaction before closing
process-owned dependencies.  The `_transaction_complete_event` is
signaled in the `finally` block of `reload()`.

Process-transition behavioral methods: `ProcessTransition` is a base
class with `preflight()`, `apply()`, and `rollback()` methods.
`TaskSpecTransition` implements the actual task-spec reconfiguration
via `apply_spec_diff()`.  Rollback stores old specs at preflight
time and re-applies them if needed.

The manager exposes `snapshot()` for runtime diagnostics, including
`reload_count`, `reload_error_count`, `last_reload_result`, `admitted`
(claim state), `admitted_at`, `admitted_request_id`,
`operation_state` (current stage, started_at, generation_id,
digest_prefix), and `active_transaction` (Phase 6 transaction state).

### Prepared-swap publication protocol (Milestone C / Plan 014)

The `ReloadManager._publish_generation()` method is decomposed into
three auditable phases so the publication fact recording, the
runtime pointer swap, and the post-publication housekeeping can be
reasoned about (and tested) individually.

1. `_prepare_swap(candidate)` — `src/eggpool/control/reload_manager.py:2408`
   captures the active generation identity and the candidate
   generation object in a frozen `_PreparedSwap` record. **No state
   mutation occurs** in this phase. The record's `active_generation_id`
   is consulted in `_commit_publication` to detect concurrent
   publication races.
2. `_commit_publication(swap)` — `:2431` invokes
   `RuntimeManager.install_candidate(...)` inside the same SQLite
   transaction that holds the persistence delta. A failure raises
   `ReloadCommitError` and triggers `ReloadTransaction.mark_aborting`
   so the transaction state machine records that publication was
   attempted but did not occur.
3. `_finalize_retirement_handling(swap)` — `:2455` calls the
   candidate's `transfer_to_runtime_manager()` so the candidate's
   registered closeables are not re-closed by abort, then mirrors the
   new generation onto `app.state` for dashboard, readyz, and other
   synchronous consumers.

The transaction state machine records explicit publication facts
(`publication_attempted`, `publication_occurred`,
`active_generation_before`, `active_generation_after`,
`persistence_committed`, `process_transitions_applied`,
`effective_state_updated`, `retirement_scheduled`) that are
populated as each phase completes. Cancellation before publication
is handled by `publication_occurred == False`; after publication the
remaining commit work is shielded from cancellation.

Programmatic invariants are pinned by
`tests/unit/test_published_swap_protocol.py` and the round-trip end
to end matrix in `tests/integration/reload/test_reload_fault_matrix.py`.

### Reload observer protocol

`ReloadObserver` (defined in `src/eggpool/control/reload_manager.py`) provides a no-op base class with async stage callbacks. Tests subclass it to intercept specific reload stages without modifying production code. Every method is a no-op by default, so attaching an observer has zero runtime cost when no overrides are provided.

Stage order: `on_admission_claimed` → `on_validation_complete` → `on_diff_computed` → `on_candidate_started` → `on_candidate_complete` → `on_reconcile_started` → `on_reconcile_prepared` → `on_publish_started` → `on_publish_complete` → `on_retirement_started`.

Concurrent reload tests no longer need `xfail` markers — the atomic admission fix (`_claim_mutex` + `_reload_claimed`) works correctly in single-process tests.

Test support modules in `tests/support/`:
- `reload_harness.py` — `ReloadHarness` with temporary dirs, in-memory DB, real managers
- `reload_faults.py` — `ReloadFaultInjector` observer for deterministic fault injection
- `runtime_snapshot.py` — `RuntimeSnapshot` for state comparison
- `closeable_resources.py` — `InstrumentedCloseable` for use-after-close detection

### Control client (Milestone C)

`src/eggpool/control/client.py` implements `ControlClient`, the async
client used by `eggpool rehash`. It connects to the UDS, sends one
`reload_config` request, reads one response, and disconnects. Error
classes: `ControlClientConnectionError`, `ControlClientTimeoutError`,
`ControlClientProtocolError`, `ControlClientError`.

### Connect/logout fallback policy

`src/eggpool/providers/connect.py` implements `resolve_apply_outcome()`,
the safe-fallback decision tree used by `eggpool connect` and
`eggpool logout`. The decision tree:

1. **Validate locally** — invalid config → return immediately, no
   restart attempted.
2. **Probe the control socket** — if the server accepts the reload,
   apply live.
3. **Server healthy but socket missing** — return
   `(False, "control unavailable (server healthy)")` **without
   restarting**. The operator must intervene explicitly.
4. **Server not running** — fall through to `restart_server()` so the
   change still applies.

The old `apply_or_restart()` is now a thin wrapper that delegates
to `resolve_apply_outcome()` when `prefer_live=True`. Tests in
`tests/integration/test_connect_logout_fallback.py` prove that
rehash exits with code 3 when the control socket is missing and a
healthy server is running, and that the server PID does not change.

### Closure pass D1 — behavioral verification pattern

Every `LIVE` field family added by D1 is pinned by a behavioral E2E
test in `tests/integration/test_rehash_streaming_swap.py`. The
canonical pattern is:

1. **Start the server with the original config** (port + mock upstream).
2. **Capture `original_pid = proc.pid`** and the
   `/api/stats/runtime` `runtime_manager.active.generation_id` via
   the public HTTP API.
3. **Rewrite the config** to flip the target `LIVE` field
   (e.g. `transcoder_loss_policy = "warn"` → `"reject"`,
   `compression.enabled = false` → `true`,
   `models.collapse_models = false` → `true`).
4. **`eggpool rehash`** and assert exit code `0`,
   `"Generation:"` in stdout, and `proc.pid == original_pid`.
5. **Re-read `/api/stats/runtime`** and assert the
   `runtime_manager.active.generation_id` advanced (i.e. the active
   generation is now the candidate, not the original).
6. **Trigger a request** that exercises the changed policy and
   assert observable behavior (e.g. a cache-eligible request now
   hits the new compression rules; a new provider matches the new
   `models.collapse_models` view).

This pattern proves that the policy object the new generation
publishes is the candidate policy — not the original — without
requiring private attribute access from outside the process.

### Polish pass additions (Milestone C follow-up)

The polish pass strengthens the control plane without changing the
wire protocol or the LIVE field inventory:

- **Standardized JSON contract**: `cli_rehash_format.format_rehash_json()`
  and `render_rehash_human()` ensure every `--json` response always
  contains 9 keys (`ok`, `stage`, `exit_code`, `generation`,
  `changed_sections`, `warnings`, `restart_required`,
  `retirement_pending`, `message`). Tests in
  `tests/unit/test_cli_rehash_format.py` pin the contract.
- **Busy stage exits code 4**: `STAGE_RELOAD_IN_PROGRESS` in
  `cli_exit_codes.py` is the single source of truth; the
  `_STAGE_TO_EXIT` table maps it to `EXIT_RELOAD_BUSY`.
- **Safe connect/logout fallback**: `resolve_apply_outcome()` never
  silently restarts a healthy server when the control socket is
  missing. The operator is prompted to intervene explicitly.
- **Deterministic concurrency test seam**: `ReloadManager` exposes
  `self.preparation_event: asyncio.Event | None = None`. When set,
  `_build_candidate_generation` awaits the event before continuing.
  Tests in `tests/unit/test_reload_manager.py` use this hook to
  deterministically hold a reload while a concurrent one is attempted.
- **Observation-strengthened E2E tests**:
  `tests/integration/test_rehash_streaming_swap.py` now asserts
  config digest changes, credential fingerprint changes, and routing
  state re-application. A new test `test_provider_removal_live_reload`
  proves provider removal end-to-end. Tests in
  `tests/integration/test_connect_logout_fallback.py` prove the
  safe-fallback exit code and PID stability.

### Background task first-run transition (Milestone C-related)

`src/eggpool/background/` manages `SupervisedTask` lifecycle. Tasks
with `run_immediately=True` fire their first tick without delay (used
by `update_checker`, `checkpoint`, `model_info_refresh`). Tasks with
`initial_delay_s` override the first-tick delay. `never_run_not_due`
and `never_run_overdue` labels distinguish freshly started tasks from
missed-deadline tasks. The `/api/stats/runtime` endpoint and
`eggpool runtime-status` CLI surface `background_task_summary` with
per-label counters.

### Phase 3 — Asynchronous runtime-generation retirement

`RuntimeManager` publication is now bounded and independent of
old-generation drainage.  A successful rehash installs the candidate
generation and returns promptly even when long-lived streams still hold
leases on the previous generation.

**Slot lifecycle states** (`SlotState` enum in `runtime_manager.py`):
`active` → `retiring` → `closing` → `closed` (or `failed_close` on
terminal close error).

**Publication behavior** (`install_candidate`):
1. Validate candidate and expected active generation.
2. Acquire the runtime-manager state lock.
3. Mark the old slot non-accepting (state → `retiring`).
4. Install the candidate as active (state → `active`).
5. Register the old slot in the retiring collection.
6. Create and register one retirement task for the old slot.
7. Release the state lock.
8. Return publication metadata immediately.

No network close, lease-drain wait, sleep, or long-running cleanup
occurs while holding the state lock.

**Retirement task ownership**: `RuntimeManager` maintains
`_retirement_tasks: dict[int, asyncio.Task[None]]` keyed by generation
ID.  Each task:
- waits for active lease count to reach zero (via `drain_event`) or
  the drain deadline;
- observes shutdown acceleration policy;
- transitions slot state under the manager lock;
- closes generation-owned resources exactly once;
- consumes and records all exceptions;
- removes itself from the registry in `finally`.

**Event-based drain signaling**: `GenerationLease.release()` signals
`slot.drain_event` when `active_leases` reaches zero.  The retirement
task uses `asyncio.wait_for(slot.drain_event.wait(), ...)` instead of
polling with `asyncio.sleep`.

**Deadline behavior**: when the drain deadline expires:
- stop waiting for leases;
- mark the generation `forced_close = True`;
- close resources using the existing bounded close policy;
- record active lease count at deadline;
- log a structured warning;
- late lease releases remain safe and idempotent.

**Shutdown semantics** (`shutdown()`):
1. mark the manager as shutting down and reject new leases/publications;
2. mark the active slot retiring;
3. schedule retirement for the active slot;
4. await all registered retirement tasks within a bounded shutdown
   deadline (10s);
5. force-cancel remaining tasks if necessary;
6. leave no EggPool-owned retirement tasks pending.

**Diagnostics** (`RuntimeDiagnostics`): exposes `active` generation
diagnostics, `retiring` tuple (per-generation state, active_leases,
forced_close, timing), `retirement_task_count`, and
`shutdown_in_progress`.  `GenerationDiagnostics` includes `state`
(SlotState value), `forced_close`, `retirement_start_time`,
`drain_deadline_s`, `close_start_time`, and `close_complete_time`.

**Tests**: `tests/unit/test_phase3_async_retirement.py` covers prompt
reload completion, natural drainage, deadline force close, multiple
concurrent generations, shutdown with various states, task hygiene,
slot state lifecycle, and `wait_for_retirement`.  Existing tests in
`tests/unit/test_runtime_manager.py` and
`tests/integration/reload/test_reload_retirement.py` are updated for
the non-blocking publication path.

### Diagnostics (Milestone C-related)

- **Stream diagnostics** (`src/eggpool/request/stream_diagnostics.py`):
  bounded ring histograms for `completed_ms`, `client_cancel_ms`,
  `finalizer_timeout_ms`. Exposed under `/api/stats/runtime`.
- **Finalization retry queue** (`src/eggpool/request/finalization_queue.py`):
  drains cancellation finalizations that escaped the 10s shielded
  path. Supervisor-owned periodic task (active 1.5s, idle 15s).
- **Routing trace guard** (`src/eggpool/request/routing_trace_guard.py`):
  skips diagnostic routing trace writes when SQLite lock-wait p95
  exceeds threshold (default 200ms).
- **DB contention snapshot**: `lock_wait_p50_ms`, `p95_ms`, `p99_ms`,
  `max_ms`, `sample_count` exposed via `/api/stats/runtime`.

### Reload diagnostics (Phase 11)

Phase 11 makes every reload outcome observable, internally consistent,
and stage-accurate. The implementation lives in
`src/eggpool/reload_diagnostics.py` and `src/eggpool/control/reload_manager.py`.

**Canonical result model** (`ReloadDiagnosticResult`):
every admitted reload reaches one terminal finalizer
(`_finalize_reload`) that produces a frozen dataclass carrying:
- `request_id`, `category` (`ReloadResultCategory` enum),
  `terminal_stage` (`ReloadTerminalStage` enum);
- timestamps (`admitted_at`, `started_at`, `completed_at`, `duration_s`);
- generation metadata (old, candidate, active IDs and digests);
- section tracking (changed, ignored, restart-required);
- operation flags (semantic_noop, publication_occurred, persistence_committed,
  process_transitions_applied);
- compensation and cleanup status;
- retirement status (derived from `RuntimeManager.diagnostics()`);
- error classification (stable code/class, bounded message);
- bounded warnings and precise counters (`ReloadCounters`).

**Result categories** (`ReloadResultCategory`):
`SUCCESS_COMMITTED`, `SUCCESS_NOOP`, `SUCCESS_IGNORED_ONLY`,
`REJECTED_BUSY`, `REJECTED_VALIDATION`, `REJECTED_RESTART_REQUIRED`,
`FAILED_CANDIDATE_PREPARE`, `FAILED_PERSISTENCE_PREPARE`,
`FAILED_COMMIT`, `ABORTED_CANCELLED`, `ABORTED_SHUTDOWN`,
`COMPENSATION_FAILED`, `INTERNAL_ERROR`.

**Counter semantics** (`ReloadCounters`):
`total_requests`, `admitted_operations`, `busy_rejections`,
`committed_reloads`, `noop_outcomes`, `ignored_only_outcomes`,
`validation_rejections`, `restart_required_rejections`,
`prepare_failures`, `commit_failures`, `cancellations`,
`compensation_failures`, `retirement_failures`.

**Stage accuracy**: the terminal stage comes from the Phase 6
transaction state via `_set_stage()`, not from error class mapping.
A `ReloadPreparationError` at the VALIDATION stage (digest mismatch)
reports `VALIDATION`; at the PREPARATION stage (build failure) reports
`PREPARATION`.

**Retirement status**: derived from `RuntimeManager.diagnostics()`
after commit finalization, reflecting actual tracked retirement
tasks rather than inferring pending status from result success.

**Control protocol**: `ControlResponse` includes optional
`result_category` and `duration_s` fields (backward-compatible).
CLI `--json` output includes `result_category` and `duration_s`.

**Tests**: `tests/unit/test_reload_diagnostics_matrix.py` exercises
every result category, counter, stage, and snapshot field.

### Critical rules

- Do not raise `server.threads` or `workers` to "fix" high-concurrency
  stream instability. Granian runs `workers=1` and `threads=1`; raising
  `threads` does not improve HTTPX concurrency and raising `workers`
  multiplies the SQLite connection budget.
- Keep `database.worker_threads = 2` (default) so the dashboard
  analytics connection does not queue behind request-path writes on
  the shared connection lock.
- Keep `routing.trace.mode = "sampled"` as the default. Full trace
  persistence (`mode = "all"`) is diagnostic-only and should never be
  the steady-state posture on a high-concurrency streaming workload.
- Client cancellation is downstream behavior; it MUST NOT register as
  an upstream error or apply a provider health penalty. The
  coordinator passes `CLIENT_CANCELLED` through the dedicated outcome
  label and skips `HealthManager.record_failure` for that path.
- HTTPX exception class names are stable operator-facing tokens — do
  not rename them without a coordinated update to the dashboard,
  the runtime JSON contract, and the playbook.

## Dispatch stability

Use `/api/stats/runtime` and `eggpool runtime-status` to distinguish local
dispatch overhead from upstream latency and to inspect bounded stream and
resource diagnostics. The optional runbook is
`docs/operations/dispatch-stability.md`; it does not define a mandatory soak,
benchmark, or evidence workflow.

## Error-Isolation Reproducer and Invariant Baseline (Plan 023)

Phase 1 of the upstream error isolation roadmap (`plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`).
Observational, test-infrastructure focused — does not change production routing, failure classification, health policy, finalization ownership, database recovery, or payload semantics.
Establishes the deterministic mock upstream, state-audit fixtures, cancellation/database fault seams, JSON operation counters, and performance baselines used by Plans 024–030.

### Mock upstream contract

`tests/helpers/mock_upstream.py` provides `MockUpstream` — a `respx`-backed mock upstream service supporting OpenAI `/chat/completions` and Anthropic `/messages` endpoints.
Declarative `MockResponseSpec` for configurable status, headers, JSON body, text body, SSE stream chunks, transport errors, delayed headers/body, and connection drop.
`MockUpstreamRule` matches by model, reasoning_effort, has_thinking, request sequence, and custom predicate.
`CapturedRequest` captures model, thinking/reasoning fields, body bytes, sequence number — tests assert on structured fields, never on application logs.
Nine MiniMax-M3 scenario presets via `minimax_thinking_rules()`: no thinking success, accepted thinking success, unsupported 400, unsupported 422, misleading 404, error-then-unrelated-success, error-then-minimax-success, streaming rejected, connection drop.

### Canonical request fixtures

`tests/helpers/request_fixtures.py` provides 18+ immutable request payloads covering every thinking-control variant: OpenAI `reasoning_effort` (low/medium/high/xhigh/unknown/null/omitted), nested `reasoning` forms, Anthropic `thinking` with `budget_tokens`, historical `reasoning_content`, provider-qualified model IDs, streaming variants, tool-use, and cache-control fixtures.
`copy_fixture()` returns deep copies so tests cannot mutate shared state.
Parametrize-ready lists: `OPENAI_REASONING_EFFORT_VARIANTS`, `NESTED_REASONING_VARIANTS`, `ANTHROPIC_THINKING_VARIANTS`.

### Database fault seams

`Database` (`src/eggpool/db/connection.py`) exposes class-level test injection hooks: `TEST_INJECT_BEFORE_COMMIT_CALL`, `set_test_inject_commit_call()`, `set_test_inject_rollback_call()`, `set_test_inject_in_transaction_before_rollback()`.
`tests/support/reload_faults.py` provides `ReloadFaultInjector` for reload-stage faults.
Each test forces exactly one outcome (no multiple-possible-outcome assertions).

### Performance and resource baselines

Performance and long-running soak baselines are intentionally not part of the
standard ownership model; use the focused request and smoke tests instead.

### Test files

- `tests/smoke/test_failure_recovery_smoke.py` — request-local failure isolation
- `tests/smoke/` — canonical request, stream, and failure-isolation smoke paths

## Provider-Bound Thinking-Control Normalization (Plan 024)

Phase 2 of the upstream error isolation roadmap. Adds an explicit provider-bound request-contract layer so thinking/reasoning controls are validated and normalized after provider/account selection, regardless of whether protocol transcoding is required.

### Workstream A — Capability schema (`ThinkingControlContract`)

`src/eggpool/catalog/capabilities.py` defines `ThinkingControlContract` — a structured contract that explicitly answers whether the model/provider produces reasoning, whether the client can control reasoning, which wire fields are accepted, which effort labels are accepted, whether an explicit token budget is accepted, and what aliases or mappings are safe.

Control modes: `unknown`, `none`, `fixed`, `effort`, `budget`, `effort_or_budget`.

`infer_control_contract()` derives a contract conservatively from legacy `ThinkingCapability` fields when no explicit contract is present. Existing capability records without a `control_contract` continue to deserialize without failure. Manual overrides deterministically outrank built-in and discovered metadata. Collapsed model IDs retain provider-specific contracts via URL-pattern matching on the selected provider.

### Workstream B — Adaptation result type (`ProviderRequestAdaptation`)

`src/eggpool/transcoder/provider_adaptation.py` defines the pure adaptation result and function:

- `ControlFieldAdaptation` — typed per-field result with `disposition` (`unchanged`/`mapped`/`dropped`/`rejected`/`not_present`), `payload`, `requested_field`, `emitted_field`, `warning`. Ensures `emitted_controls` and warnings are truthful.
- `ProviderRequestAdaptation` — typed result with `payload`, `changed`, `decision` (`passthrough`/`mapped`/`dropped`/`rejected`), `requested_controls`, `emitted_controls`, `warnings`.
- `adapt_thinking_controls()` — pure function that validates/normalizes controls against the provider contract. Does not touch runtime health, database state, routing, or logging.
- Rejection raises `CapabilityError` (HTTP 400) — no upstream attempt.

### Workstream C — Post-selection normalization stage

`_adapt_provider_thinking_controls()` in the coordinator (`src/eggpool/request/coordinator.py`) runs after provider selection and budget recompute, before upstream dispatch. Runs for both native and transcoded paths. When the client protocol matches the upstream protocol (native path), unknown contracts pass through — the upstream will reject if needed. Uses the original client intent (ThinkingRequestIntent) rather than re-reading already-translated fields.

### Workstream D — Original client intent (`ThinkingRequestIntent`)

`ThinkingRequestIntent` (frozen dataclass in `capabilities.py`) captures the original client's thinking intent before any translation or adaptation. Stored in `ProxyRequestContext.thinking_intent`, it prevents intermediate translations from becoming falsely authoritative. Fields: `requested_effort`, `requested_effort_original`, `requested_budget_tokens`, `request_fields`, `has_historical_reasoning_content`, `client_requests_new_reasoning`, `client_protocol`.

### Workstream E — Built-in contracts

`src/eggpool/transcoder/builtin_contracts.py` contains manually curated contracts for known provider deployments:

| Provider | ID Pattern | URL Pattern | Model Pattern | Protocol | Mode |
|----------|------------|-------------|---------------|----------|------|
| OpenCode Go MiniMax-M3 | `^opencode-go$` | — | `*minimax*m3*` | anthropic | fixed |
| MiniMax native | `^minimax$` | — | `*minimax*m3*` | anthropic | effort |
| OpenCode Go URL compat | — | `*opencode.ai*` | `*minimax*m3*` | anthropic | fixed |
| Anthropic native | — | `*api.anthropic.com*` | `*` | anthropic | effort_or_budget |
| OpenAI native | — | `*api.openai.com*` | `*` | openai | effort |

Contract precedence: operator overrides > built-in contracts > inferred from legacy fields. `resolve_control_contract()` implements the three-tier resolution. Built-in matching evaluates specificity first (provider ID > provider kind > base URL), then lowest priority within specificity.

### Workstream F — Adaptation policy config

`[transcoder.provider_control_policy]` config section (`ProviderControlPolicyConfig` in `src/eggpool/transcoder/policy.py`):

- `unsupported_control`: `reject` (default) | `warn_drop` | `map_if_known`
- `unknown_contract`: `reject` (default) | `allow_with_warning`
- `allow_compatibility_retry`: `false` (default)

Strict transcoding/loss policy takes precedence over any warn/drop setting. `map_if_known` uses only explicit contract aliases/mappings; unmappable controls are rejected (same as `reject`) rather than silently passed through.

### Workstream G — Compatibility retry (deferred)

Optional one-time compatibility retry. The `allow_compatibility_retry` config field exists but defaults to `False`. No retry logic is wired into the adaptation pipeline. When implemented, it must be one-shot, allowlisted, pre-body, and health-neutral.

### Workstream H — Observability

Thinking trace extended with `provider_control_decision` and `provider_control_warnings` fields. New counters in `ThinkingMetricsCounter`: `provider_mapped`, `provider_dropped`, `provider_rejected` (pipe-delimited label keys with protocol/provider/model). No prompt or reasoning content is persisted.

### Workstream I — Tests

- `tests/unit/test_plan_024_thinking_control_contract.py` — contract schema, inference, merge, serialize, round-trip
- `tests/unit/test_plan_024_provider_request_adaptation.py` — adaptation decisions (passthrough, reject, drop, map)
- `tests/unit/test_plan_024_builtin_contracts.py` — built-in contract lookup and resolution
- `tests/unit/test_plan_024_native_provider_normalization.py` — native-path adaptation, skip logic, contract resolution
- `tests/unit/test_plan_024_transcoded_provider_normalization.py` — transcoded-path adaptation, historical content preservation
- `tests/unit/test_plan_024_thinking_trace.py` — ThinkingRequestIntent construction
- `tests/unit/test_plan_024_thinking_metrics.py` — provider control counters
- `tests/integration/test_plan_024_opencode_minimax_contract.py` — end-to-end OpenCode Go MiniMax-M3 contract, distinct MiniMax native behavior, collapsed model contracts, no durable state changes
- `tests/integration/test_plan_024_compatibility_retry.py` — compatibility retry deferral verification

## Provider Thinking-Control Normalization Closure (Plan 046)

Closes six confirmed defects in the Plan 024 thinking-control adaptation layer, making field-level semantics total, typed, and deterministic.

### Typed field dispositions

`ControlFieldAdaptation` (`src/eggpool/transcoder/provider_adaptation.py`) replaces ambiguous `dict | None` returns from field-level adapters. Dispositions: `unchanged` (field accepted), `mapped` (alias/contract changed value), `dropped` (policy removed field), `rejected` (policy raises `CapabilityError`), `not_present` (field absent). This eliminates false "mapped" labels for unsupported efforts and ensures `emitted_controls` is a truthful subset of the final payload.

### Fixed contract completeness

`_handle_fixed_contract` now removes `thinking.type`, `thinking.effort`, and top-level `thinking_budget` in addition to `reasoning_effort` and `thinking.budget_tokens`. A type-only thinking block is observably dropped rather than silently passed through. `map_if_known` policy rejects unmappable controls (same as `reject` for fixed contracts).

### Specificity-before-priority resolution

`lookup_builtin_contract` now selects by highest specificity first, then lowest priority within that level. A provider ID match (specificity 3) always wins over a URL match (specificity 1) regardless of priority numbers.

### OpenCode Go URL compatibility

`_OPENCODE_GO_URL_COMPAT_CONTRACT` matches `.*opencode\.ai.*` for MiniMax-M3 models at specificity 1, providing the fixed contract for providers configured with an OpenCode Go upstream URL but a non-canonical provider ID. Exact provider ID remains more specific. Native MiniMax URLs (`api.minimax.io`) do not match.

### Tests

- `tests/unit/test_plan_046_thinking_control_normalization.py` — 42 tests covering all defects, control spellings, contract modes, policy dispositions, emitted-controls truthfulness, and non-dict safety
- `tests/integration/test_plan_046_request_path_body_capture.py` — 8 request-path tests capturing upstream body bytes to prove warn-drop sanitization, native MiniMax control preservation, and streaming/non-streaming adaptation parity

## Typed Failure Effects and Bounded Model Quarantine (Plan 025)

Phase 3 of the upstream error isolation roadmap. Centralizes the consequences of request and upstream failures into one typed, test-pinned decision. Replaces first-observation indefinite model withdrawal with bounded, provider/account/model/protocol-scoped quarantine that requires corroboration before becoming terminal and automatically clears on recovery.

### Key components

- `FailureObservation` (`src/eggpool/failure/observation.py`) — immutable input record with source, status_code, error_class, provider/account/model scope, response_signal, retry_after, and response_started.
- `FailureEffects` (`src/eggpool/failure/effects.py`) — immutable decision output with retry, retry_scope, client_outcome, account_effect, model_effect, circuit_penalty, persist_backoff, backoff_reason/until, release_probe_only, and evidence_class.
- `classify_failure_effects()` (`src/eggpool/failure/classifier.py`) — single pure classifier with table-driven decision logic covering every status/body/error-class matrix row.
- `ModelQuarantine` (`src/eggpool/failure/quarantine.py`) — state machine (`healthy → suspected → quarantined → terminal_withdrawn`) keyed by (provider_id, account_id, canonical_model_id, upstream_model_id, upstream_protocol).
- `EffectsApplier` (`src/eggpool/failure/applier.py`) — applies effects exactly once per attempt via idempotency key.
- `extract_failure_signal()` (`src/eggpool/failure/signal_extract.py`) — bounded conservative signal extraction from response bodies.

### Tests

- `tests/unit/test_plan_025_failure_effects_table.py` — pure classifier unit tests
- `tests/unit/test_plan_025_failure_signal_extraction.py` — signal extraction tests
- `tests/unit/test_plan_025_model_quarantine_state_machine.py` — state machine transitions
- `tests/unit/test_plan_025_effects_idempotency.py` — applier idempotency
- `tests/unit/test_plan_025_quarantine_hydration.py` — hydration from SQLite
- `tests/unit/test_plan_025_quarantine_cli.py` — operator CLI
- `tests/integration/test_plan_025_error_isolation.py` — error isolation matrix
- `tests/integration/test_plan_025_cross_provider_quarantine.py` — cross-provider quarantine
- `tests/integration/test_plan_025_closure_evidence.py` — end-to-end pipeline verification

## Process-Owned Request Finalization (Plan 026)

Makes selected-attempt cleanup independent of the client request task. Once EggPool has durably created a request, attempt, or reservation and claimed runtime ownership, one retained process-owned finalization job must own terminal reconciliation until every durable and in-memory obligation has either completed or entered a bounded, observable retry state.

### Design principle

`asyncio.shield()` alone is not ownership. A shielded coroutine may continue after the outer task is cancelled, while the outer task skips subsequent cleanup. Eggpool must retain the finalization task in process-owned state, observe its completion independently of request waiters, reconcile completion exactly once, and keep bounded retry ownership when durable finalization cannot complete immediately.

### Key components

- `FinalizationIdentity` (`src/eggpool/request/finalization_job.py`) — immutable frozen dataclass containing all data needed to finalize without querying mutable request context.
- `FinalizationProgress` — progress state machine (`created → durable_finalization_pending → durable_finalized → runtime_release_pending → runtime_released → analytics_pending → completed`); only `completed` is terminal.
- `AttemptRuntimeLease` — idempotent runtime ownership token tracking active-count, quota-reservation, and health-probe acquisition/release facts.
- `RequestFinalizationJob` — process-owned job with retained `asyncio.Task`, single-flight `run()` via `asyncio.shield`, concurrent-caller sharing, and completion callback.
- `RequestFinalizationSupervisor` — bounded, deduplicated registry of active jobs with process-owned completion reconciliation, bounded history deque (scalar-only records), startup stale-state reconciliation, and shutdown drain/adopt.

### Streaming cancellation integration

The coordinator submits stream completion, EOF, error, and cancellation through
the canonical retained helper once terminal data is known; it does not create
a mutable placeholder job before the inner stream generator. On
`CancelledError`, the retained task owns finalization even when every request
waiter is cancelled. The `RuntimeGeneration` dataclass carries
`finalization_supervisor` wired through `RuntimeGenerationFactory`.

### Tests

- `tests/unit/test_plan_026_runtime_ownership_token.py` — identity, lease, release semantics
- `tests/unit/test_plan_026_finalization_state_machine.py` — progress, concurrent callers, cancellation safety
- `tests/unit/test_plan_026_finalization_supervisor.py` — registry, drain, startup reconciliation, diagnostics

## Terminal Lifecycle and Cancellation Safety (Plan 047)

Plan 055 corrected the remaining stream-specific ownership defects described
below: the historical Plan 047 closure claim is superseded until that pass is
complete, and the current invariant is documented in the request-lifecycle
summary above.

Plan 047 closes the gap between the retained cancellation job and the other
request terminal paths. After selection, the coordinator submits completion,
client cancellation, capability rejection, upstream 4xx, exhausted upstream
failure, and midstream failure through the same `RequestFinalizationJob`.
Streaming 4xx handling only raises its typed non-retryable error; the
exhausted-outcome path performs the single terminal submission.

### Ownership and identity

- `FinalizationIdentity` is keyed by `(proxy_request_id, attempt_id)`; two
  attempts of one request are distinct jobs.
- `RequestFinalizationSupervisor.register_or_get()` joins duplicate identical
  submissions. A different outcome or terminal payload raises
  `TerminalConflictError` and increments the bounded conflict diagnostic.
- `FinalizationResult` reports durable transition, reservation release,
  in-memory cleanup, health/effect handling, retry, and conflict facts instead
  of making callers infer completion from request status alone.
- `AttemptRuntimeLease` records each runtime component independently. A failed
  component release remains retryable without repeating components that already
  succeeded.

Capability rejection is a request-local client error: it releases the selected
  attempt and runtime ownership through the canonical job and never applies a
provider health penalty. The job registry remains bounded, completed entries
are removed into scalar history, and shutdown drains retained tasks before
adopting unresolved work for recovery.

### Tests

- `tests/unit/test_request_finalization_state_machine.py` — retained execution,
  cancellation, attempt-keyed deduplication, and terminal conflict detection
- `tests/unit/test_runtime_ownership_token.py` — per-component release retry
- request-path tests cover streaming/non-streaming 4xx parity and capability
  rejection cleanup through the coordinator's canonical terminal helper

## Database Connection Recovery (Plan 027)

Allows EggPool to recover safely from an invalidated or indeterminate SQLite connection without requiring a process restart. The process detaches a suspect connection, opens an unadmitted replacement, verifies it, reconciles ambiguous operations, and restores readiness only after every correctness check succeeds.

### Design principle

Fail closed on uncertain transaction outcome, but recover the process automatically. Never reuse an indeterminate connection. Never blindly replay a transaction whose commit may have succeeded. Reconcile using durable identities and state predicates.

### Key components

- `DatabaseLifecycleState` enum (`src/eggpool/db/connection.py`) — explicit state machine (`disconnected → connecting → ready → invalidating → invalidated → recovering → reconciling → ready / failed_closed → shutting_down`).
- `connection_epoch` property — incremented on every successful `connect()` so long-lived components detect replacement.
- `DatabaseRecoveryController` (`src/eggpool/db/recovery.py`) — single-flight recovery with bounded retry/escalation, reason-class tracking, and `RecoverySnapshot` diagnostics.
- `AmbiguousDatabaseOperation` — frozen dataclass capturing indeterminate commit metadata for dispatch/finalization reconciliation.
- `Database.transaction(ambiguous_operation=...)` — transaction-owned ambiguity metadata installed after lock acquisition; waiting tasks cannot overwrite it.
- Ambiguity retention is bounded but lossless: convergence acknowledges one operation at a time, while unresolved results remain queued and overflow fails closed.
- `DatabaseRollbackError` — typed error when ROLLBACK itself fails after a body exception, distinct from `DatabaseCommitError`.
- `_safe_rollback()` helper with bounded diagnostics.
- `[database.recovery]` config section with `max_attempts`, `initial_backoff_ms`, `max_backoff_ms`, `reconciliation_timeout_s`, `fail_process_on_exhaustion`.
- Wired into `ProcessRuntime.recovery_controller`, app.py startup/shutdown, `/readyz` recovery-state degradation.
- `WritableProbe.force_probe_nowait()` for recovery-cycle refresh.

### Recovery flow

1. `Database._invalidate_connection()` transitions through `INVALIDATING → INVALIDATED` and notifies the recovery controller.
2. The controller starts a single-flight recovery task; concurrent callers join the same attempt.
3. The suspect connection is closed and an unadmitted replacement opened.
4. For in-memory DBs, migrations are re-run privately; for file-backed DBs, schema compatibility is verified privately.
5. A private writable probe confirms the replacement connection is usable while public transactions remain rejected.
6. Ambiguous operations are reconciled via built-in reconcilers (`dispatch`, `finalization`); unresolved operations remain queued.
7. On success, `writes_admitted` and `reads_admitted` are restored; readiness recovers.
8. On any failed attempt, the candidate is closed and discarded. On exhaustion, the database enters `failed_closed` state with precise diagnostics.

### Tests

- `tests/unit/test_plan_027_database_lifecycle.py` — state machine, epoch tracking, ambiguous ops, diagnostics
- `tests/unit/test_plan_027_recovery_singleflight.py` — concurrent waiters, retry, shutdown, snapshot

## Provider Payload Lifecycle and Hot-Path Consolidation (Plan 028)

Reduces request-path CPU, allocations, serialization work, and SQLite writer-lock duration without changing protocol behavior. Consolidates provider-bound request transformations around one decoded payload lifecycle, consolidates non-stream response processing around one decoded response lifecycle, and moves avoidable lookups and best-effort work outside correctness-critical transactions.

### Design principles

1. Parse once, transform in memory, encode once.
2. Preserve raw bytes for exact passthrough.
3. Do not mutate shared client payload objects.
4. Avoid work when no consumer needs it.
5. Keep provider-bound transforms ordered and explicit.
6. Shorten global SQLite writer critical sections.

### Key components

- `ProviderBoundRequest` (`src/eggpool/request/provider_bound_request.py`) — typed lifecycle object owned by one proxy request; carries `client_payload` (immutable), `provider_payload` (copy-on-write via `set_provider_payload`), `provider_bytes` (serialized once), `payload_generation` counter, and `SegmentationValidityKey` for cache reuse.
- `SegmentationValidityKey` / `PreparedTranscodeValidityKey` — frozen dataclass compound keys that make segmentation and transcode reuse deterministic.
- `TransformPipeline` (`src/eggpool/request/transform_pipeline.py`) — ordered post-selection transform pipeline with declarative `TransformMeta`, `TransformResult`, and `run_transform_pipeline()` orchestrator that short-circuits on rejection.
- `ParsedUpstreamResponse` (`src/eggpool/request/parsed_upstream_response.py`) — single-decode lifecycle for non-streaming upstream responses; lazily parses body once and provides `parsed_dict`, `parse_status`, and `header_value()` accessors.
- `ProxyRequestContext.provider_bound` field — attaches the `ProviderBoundRequest` to the request context so `body_for_upstream` delegates to `provider_bound.provider_bytes`.
- `RequestCoordinator._extract_non_stream_usage_from_parsed()` — reads from `ParsedUpstreamResponse.parsed_dict` instead of re-parsing bytes, eliminating duplicate JSON decode in the non-streaming success path.
- `RequestFinalizer._precompute_finalization_diagnostics()` — precomputes all diagnostic serialization outside the `BEGIN IMMEDIATE` transaction, and moves best-effort account event enrichment post-commit.

### Non-streaming response lifecycle

Before Plan 028, the non-streaming success path parsed the upstream response body independently for usage extraction, normalized usage construction, and response transcoding. After Plan 028, a single `ParsedUpstreamResponse` is created once and shared across all consumers — each consumer reads from the same decoded representation without re-parsing.

### Finalization transaction shortening

All diagnostic serialization (segmentation summary JSON, compression observation/result JSON, synthetic cache JSON, resolved policy JSON) is precomputed in `_precompute_finalization_diagnostics()` before the `BEGIN IMMEDIATE` transaction. The transaction only executes the DML statements. Best-effort account event enrichment runs after the correctness transaction commits.

### Tests

- `tests/unit/test_plan_028_provider_bound_request.py` — lifecycle object, validity keys, payload mutation, segmentation caching
- `tests/unit/test_plan_028_transform_pipeline.py` — pipeline ordering, passthrough, mutation, rejection, warning accumulation
- `tests/unit/test_plan_028_parsed_upstream_response.py` — lazy parsing, dict/list/invalid distinction, header lookup
