# EggPool Architecture Overview

EggPool is a native Rust, LAN-hosted proxy that aggregates multiple LLM
provider accounts behind OpenAI Chat Completions (`POST /v1/chat/completions`),
OpenAI Responses (`POST /v1/responses`), and Anthropic Messages
(`POST /v1/messages`) compatible paths. The shipped runtime lives under
`rust/`; the native wheel is defined by `packaging/pypi/pyproject.toml` and
contains the executable plus metadata/assets only. The repository-root
`pyproject.toml` is tooling-only (`project_role = "repository-tooling-only"`).
Historical Python source is recoverable from Git history; see
[migration history](../docs/migration-history.md).

This document is the birds-eye view and review index. Each section below gives
a discrete module overview and links to its deep dive. The subsystem index in
[README.md](README.md) remains the design authority; this file does not
duplicate deep-dive detail.

## How everything fits together

```text
CLI adapter (rust/src/runtime.rs)
  -> operations services (rust/src/operations/)
  -> config + reload policy (rust/src/config.rs, config_reload_policy.rs, reload.rs)
  -> runtime generations (rust/src/runtime_lifecycle/)
       -> Axum server (rust/src/server/) -> coordinator (rust/src/coordinator/)
            -> admission (rust/src/request/) -> canonical IR (rust/src/wire/ir.rs)
            -> model/account routing (routing/, quota/, health/, accounts/, catalog/, model_router.rs)
            -> wire codecs (rust/src/wire/) -> provider pool (rust/src/providers/)
            -> durable publication/finalization (coordinator/publication.rs, finalization.rs, rust/src/db/)
       -> supervised background tasks (rust/src/task_supervisor.rs)
```

Request lifecycle (finite and streaming share the same budget/ownership rules):

1. Axum admits and bounds the request (`server/middleware.rs`,
   `server/inference.rs`). Inference handlers make exactly one coordinator
   call per request; `server/*` holds no retry/finalization logic.
2. `request/admission.rs` produces a bounded `CanonicalRequest` plus, for
   Responses, a source-native preservation envelope. Stateless policy is
   enforced here: `store` omitted/false is accepted; `store: true`,
   `previous_response_id`, conversation references, and background execution
   are rejected.
3. Routing selects an eligible provider account using quota, health,
   quarantine, and fairness (`routing/`, `quota/`, `health/`, `accounts/`,
   `catalog/`). Semantic aliases resolve first via the neutral
   `eggpool-model-routing` policy crate plus the app-owned affinity cache in
   `model_router.rs`.
4. The coordinator persists request/attempt identity before upstream dispatch
   (`coordinator/publication.rs`), submits one attempt at a time
   (`coordinator/attempt.rs`), and classifies failures centrally
   (`coordinator/failure.rs`).
5. The canonical wire boundary (`wire/ir.rs`) encodes the selected upstream
   surface. Codecs never chain translated payloads; cross-surface Responses
   preparation rejects native-only semantics or emits bounded adaptation
   notices.
6. The provider pool sends the request and observes the response/stream
   (`providers/`). Streams require wire terminal evidence; transport EOF is
   never synthesized into success.
7. Finalization records usage, releases reservations/claims, and applies health
   effects (`coordinator/finalization.rs`, `coordinator/streaming/terminal.rs`).

Runtime lifecycle: `RuntimeManager` publishes immutable `RuntimeGeneration`
candidates atomically. Requests hold a `GenerationLease` on the generation
they acquired; in-flight work finishes on the retiring generation while the
new active generation serves new work. Retries and alternate-wire negotiation
consume one shared upstream-submission budget and are structurally unavailable
after the streaming handoff (`StreamingExecution` returned). All transitions
fail closed on validation, commit, or ownership ambiguity.

## Discrete modules

### 1. Entry, CLI adapter, errors

`rust/src/main.rs` is a minimal Tokio `current_thread` entry point mapping
`AppError` to `ExitCode`. `rust/src/lib.rs` declares the module tree and
re-exports the CLI/config/error boundary. `rust/src/cli.rs` owns the Clap
command tree (`serve`, `connect`, `logout`, `check-config`, `edit`, `getkey`,
`newkey`, `configsetup`, `deploy`, `accounts`, `dashboard`, `db`, `models`,
`modelinfo`, `stats`, `onboard`, `croncheck`, `ensure-running`, `migrate`,
`stop`, `restart`, `init-config`, `help`, `recover`, `uninstall`, `update`,
`install-provenance`, `set`, `rehash`, `runtime-status`, `backup`, `version`).
`rust/src/runtime.rs` adapts each command to `operations/*`, `config`, and
`db` without touching coordinator/server internals. `rust/src/error.rs`
(`AppError`/`BootstrapError`) owns the typed error hierarchy and stable
exit-code mapping; HTTP/status mappings for server surfaces live with their
adapters and retain context without secrets or raw bodies. `rust/src/version.rs`
exposes `CARGO_PKG_VERSION` (currently `0.8.0`).

Deep dive: [Core](deep-dive-core.md).

### 2. Configuration, reload policy, reload service

`rust/src/config.rs` is the typed TOML contract (`Config`, `ServerConfig`,
`UpstreamConfig`, `DatabaseConfig`, `RoutingConfig`, `ModelsConfig`,
`ProviderConfig`, `DashboardConfig`, `SecurityConfig`, `MetricsConfig`,
`BackupConfig`, `ModelRouterConfig`, transcoder/model-info policies) with
`from_toml`/`validate` and proxy/model-router compilation. Resolution order is
explicit `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` >
`./config.toml`; API keys come from the environment or the adjacent `.env`
(`$EGGPOOL_ENV` override supported), never committed. Canonical examples are
the repository-root `config.example.toml` and `config.sbc.example.toml`;
`rust/build.rs` embeds the default example for `eggpool init-config` and embeds
the `rust/assets/db/migrations/` chain with checksums (via
`rust/build_support.rs`).

`rust/src/config_reload_policy.rs::classify_transition` is the only
reload-vs-restart authority. It returns one redacted typed result for
unchanged, live-reloadable, and restart-required candidates; mixed changes are
wholly restart-required. `rust/src/reload.rs` (`ReloadService`) revalidates and
reclassifies on the server before building a complete candidate generation,
staging task/wire deltas, and committing the DB transaction plus atomic
pointer swap. `eggpool rehash` serializes reloads over the Unix control socket.

Deep dives: [Core](deep-dive-core.md), [Control plane and rehash](deep-dive-control.md),
[Runtime](deep-dive-runtime.md).

### 3. HTTP server adapters

`rust/src/server/mod.rs` builds the Axum router, owns `AppState` (API key,
`Database`, `RuntimeManager`, body-task tracker), and owns foreground
lifespan, signal handling, and quiesce/drain/close shutdown. Siblings stay
thin: `middleware.rs` (constant-time Bearer/`x-api-key` auth, generation-lease
admission, `max_request_body_bytes` bounding, loopback exemption),
`health.rs` (`GET /v1/healthz`, `GET /v1/readyz`, `GET /v1/models`,
`GET /api/stats/runtime`, `GET /api/stats/update`), `inference.rs`
(`chat_completions`, `messages`, `responses`, `responses_compact`; finite vs. stream dispatch on the
`stream` flag; exactly one `coordinator::execute_finite`/`execute_stream` call
per request, compact via finite-only `execute_compact_finite`), `dashboard.rs` (server-rendered pages plus static assets).
Route topology: inference at `/v1/chat/completions`, `/v1/messages`,
`/v1/responses`, `/v1/responses/compact` (bounded distinct compaction
operation, not a Responses alias); dashboard at `/`, `/accounts`, `/models`,
`/models/{*model_id}`, `/latency`, `/events`, `/timeseries`, `/bandwidth`,
`/pings`, `/reliability`, `/routing`, `/traces`, `/runtime`, `/cache`,
`/api/stats/summary`; statics under `/static/`.

Deep dives: [Request lifecycle](deep-dive-request-lifecycle.md),
[Dashboard](deep-dive-dashboard.md), [Runtime](deep-dive-runtime.md).

### 4. Request admission

`rust/src/request/` stops before routing or provider submission. `admission.rs`
bounds JSON parsing (depth/collection caps) and emits `AdmittedRequest`
(canonical `CanonicalRequest` + native Responses preservation envelope +
token estimates + routing facts). `body.rs` owns deterministic compact JSON
encoding; `limits.rs` owns overflow-safe media/token/reservation estimators
and surface-aware output-token resolution.

Deep dives: [Request lifecycle](deep-dive-request-lifecycle.md),
[Transcoding](deep-dive-transcoder.md).

### 5. Coordinator (request lifecycle)

`rust/src/coordinator/` owns endpoint detection, bounded preparation,
model/account routing, durable request/attempt state, provider dispatch,
response adaptation, retry classification, and terminal finalization.
`finite.rs` loops non-streaming attempts; `streaming/coordinator.rs` owns the
pre-handoff streaming loop (header wait, status decode, first-byte prefetch,
retry/alternate-wire decisions); `streaming/execution.rs` owns the single
post-handoff body/cancellation owner (`StreamingExecution`,
`PendingStreamFinalization`); `streaming/terminal.rs` classifies EOF/terminal
outcomes without reparsing wire events; `streaming/timeout.rs`, `types.rs`,
`diagnostics.rs` hold timeout policy, contracts, and secret-free counters.
Supporting services: `publication.rs` (durable request/attempt/reservation
publication), `finalization.rs` (`FinalizationSupervisor`, durable terminal
convergence), `attempt.rs` (single prepared/submitted attempt), `failure.rs`
(`FailureDecisionEngine`, retry legality), `wire_resolver.rs` (bounded
process-owned wire candidate ordering), `endpoints.rs`/`semantic.rs`/
`reconciliation.rs` (surface detection, semantic helpers, crash repair hooks).

Deep dives: [Request lifecycle](deep-dive-request-lifecycle.md),
[Retry](deep-dive-retry.md).

### 6. Wire codecs and transcoding

`rust/src/wire/` is the closed, provider-independent codec boundary.
`ir.rs` captures canonical request/reasoning/usage/tool/response/stream-event
semantics before adaptation. `codec.rs`/`codecs.rs`/`additional_codecs.rs`
implement per-surface codecs (OpenAI Chat, Anthropic Messages, OpenAI
Responses, Gemini variants); `registry.rs` accepts only compiled codec IDs;
`runtime.rs`/`stream.rs` own `WireRuntime`/`WireStream`, `PreparedRequest`,
`FiniteResponse`, SSE decode/encode, and terminal summaries; `adaptation.rs`
owns reasoning/tool/capability and loss policy. Two bounded Responses paths:
canonical semantic adaptation and source-native same-surface preservation
(ordered items, encrypted reasoning, native tools preserved; alias targets
rewrite only EggPool-owned `model`). `custom` tools map to
`CanonicalToolKind::Freeform` with per-request-declaration unwrapping.
Streaming has native observe-and-forward (unknown valid events preserved) and
bounded cross-surface synthesis (indexed completed items, stable response ID,
`call_id`-paired tool outputs, `response.completed` required).

Deep dive: [Transcoding](deep-dive-transcoder.md).

### 7. Providers and outbound transport

`rust/src/providers/` stops at neutral HTTP transport: `transport.rs`
(`ProviderHttpClient`, `ProviderBody`, `ProviderResponse`, `TransportError`),
`client_pool.rs` (generation-owned per-account pool). No auth/wire/retry here;
credentials render only at dispatch-header construction. Proxy construction
crosses the stable `eggress-embed` boundary (`OutboundConnector::from_pproxy_uri`
for single-hop and `__`-separated multi-hop; explicit `direct://` validated
through Eggress but using the direct Hyper connector). Default
`eggress-ssh-fallback` retains the 1.0.6 native SSH chain/session-cache
compat path; `--no-default-features` keeps direct/non-SSH proxy paths and
rejects SSH proxy config as `TransportError::ProxyConfiguration` before
dialing. Underlying HTTP is Hyper/Rustls (HTTP/1.1, `ring`, TLS 1.2, webpki
roots, bounded pooling). `rust/Cargo.toml` plus `cargo tree -e features` is the
dependency authority; `deny.toml` + `cargo deny check` gates
licenses/advisories/sources/duplicates. `unsafe_code = "forbid"` is a repo
invariant.

Deep dive: [Providers](deep-dive-providers.md).

### 8. Routing, quota, health, accounts, catalog, semantic model routing

`rust/src/routing/` (`router.rs`, `eligibility.rs`, `fairness.rs`, `claim.rs`)
does deterministic, load-based (never cost-based) selection and claim
transactions over `AccountRegistry`, `ModelCatalogCache`, `QuotaEstimator`,
`HealthManager`, quarantine, and fairness rotor. `rust/src/quota/`
(`state.rs`, `estimator.rs`, `scorer.rs`) owns windowed quota state and
fair-share scoring. `rust/src/health/` (`health_manager.rs`, `backoff.rs`,
`circuit_breaker.rs`, `effects.rs`, `quarantine.rs`, `repository.rs`) applies
the narrowest safe effect (pair quarantine vs. account breaker) and persists
only restart-safety state. `rust/src/accounts/` (`registry.rs`) owns static
identity/credential boundaries. `rust/src/catalog/` (`cache.rs` in-memory read
authority, `refresh.rs` fetch/normalize/persist) owns provider-scoped
discovery, capability/pricing/limits/withdrawal, and bounded model-info
enrichment attached to the generation-leased refresh (no separate scheduler).
`rust/crates/eggpool-model-routing/` (neutral, Rust 1.81-compatible:
validation/compilation, route IDs, fingerprints, hashed session identities) vs.
`rust/src/model_router.rs` (app-owned Tokio TTL/LRU/single-flight affinity
cache, TOML adaptation). Selectors choose a model before provider routing and
cannot pin accounts, bypass health/quota, or reselect after submission.

Deep dives: [Routing and quota](deep-dive-routing.md),
[Health](deep-dive-health.md), [Catalog](deep-dive-catalog.md),
[Model info](deep-dive-model-info.md), [Data models](deep-dive-models.md).

### 9. Persistence (SQLite)

`rust/src/db/` (`connection.rs` serialized `tokio-rusqlite` gate with
`bundled`/`backup` features, explicit caller-owned transactions;
`migrations.rs` checksum-validated runner; `repositories.rs` typed
account/catalog/model/request/ping/dashboard/usage plus health persistence)
is the only persistence boundary. Migrations (`rust/assets/db/migrations/`,
currently v1–v54 with `checksums.json`) are embedded and immutable; commit or
ownership ambiguity fails closed; startup reconciliation repairs only covered
states. Repositories accept no unbounded diagnostic content and never store
credentials, prompts, raw bodies, or cache keys.

Deep dive: [Database](deep-dive-database.md).

### 10. Runtime generations, background work, shutdown

`rust/src/runtime_lifecycle/` splits by ownership: `process.rs`
(`ProcessRuntime`: DB, affinity, wire resolver, task supervisor, metrics
coalescer, update-checker state), `generation.rs` (`RuntimeGeneration`
immutable snapshot + `RuntimeGenerationFactory::prepare` single construction
path), `lease.rs` (slots, `GenerationLease`, finalization guards), `manager.rs`
(`RuntimeManager` `ArcSwap` active pointer, two-phase staged swap,
max 4 retiring generations), `recovery.rs` (bounded one-shot crash
reconciliation, max 1024 passes), `diagnostics.rs` (secret-free projections),
`mod.rs` (facade/re-exports only). `rust/src/task_supervisor.rs` supervises
fixed-delay tasks: process-owned (WAL checkpoint, metrics flush, update check,
auto backup) vs. generation-leased (catalog refresh, retention/cleanup);
`operations/metrics.rs` coalescing and `operations/update.rs` freshness probes
plug in here. Shutdown closes admission, retires the active generation, joins
supervised/finalization work, then disconnects the DB.

Deep dives: [Runtime](deep-dive-runtime.md), [Background tasks](deep-dive-background.md).

### 11. Operations and local lifecycle

`rust/src/operations/` holds small process-local services (no second
runtime/reload authority): `lifecycle.rs` (start/stop/restart/ensure-running/
watchdog composition), `process.rs` (PID files, health/control probes,
identity proof), `paths.rs` (canonical config/data/state/log/PID/socket
resolution; production `/etc/eggpool`, `/var/lib/eggpool`, `/var/log/eggpool`
vs. XDG personal layout; `$EGGPOOL_CONFIG`, `$EGGPOOL_ENV`,
`$EGGPOOL_RUNTIME_DIR` aware), `control.rs` (Unix-domain JSON control
protocol for `rehash`/`runtime-status`), `config_mutation.rs` (comment-preserving
atomic TOML edits + `classify_transition`-carrying apply), `deploy.rs`
(systemd/logrotate/cron rendering, install/uninstall), `backup.rs` (staged
atomic ZIP with `META` + config + optional `.env` + consistent SQLite
snapshot; restore requires stopped service), `update.rs` (GitHub release
discovery, SHA-256 verification, atomic transition; background probes are
conservative, exact-version resolution is explicit), `catalog.rs` (embedded
installable-releases catalog), `provenance.rs` (install-provenance detection),
`operator.rs` (account/explain/stats/catalog/transcoding/cost/model-info
operator services), `metrics.rs` (bounded scalar-only coalescer),
`integrations.rs` (`eggpool configsetup` renderers for opencode/claude-code/
aider/codex and others; Codex emits HTTP/SSE Responses TOML with
`env_key = "EGGPOOL_API_KEY"`, printable by default, no embedded secret;
OpenCode uses `{env:EGGPOOL_API_KEY}` with the Responses-capable
`@ai-sdk/openai` runtime; Codex/OpenCode share one conservative
provider-neutral projection plus managed `--apply`/`--sync`/`--check`/
`--remove`/`--dry-run` lifecycle with ownership manifests and drift refusal).

Deep dives: [Control plane](deep-dive-control.md),
[Deployment](deep-dive-deployment.md), [Backup/restore](deep-dive-lifecycle.md),
[Integrations](deep-dive-integrations.md), [Metrics](deep-dive-metrics.md).

### 12. Observability and security

Observability is bounded, deterministic, and metadata-only: request/usage/
latency/failure/reasoning/routing facts via `operations/metrics.rs` and
coordinator instrumentation; routing traces record decisions, not prompts;
`runtime-status --json` and `/api/stats/runtime` project active/retiring
topology without secrets. Security is request limits, API-key auth,
header filtering, credential redaction, owner-only socket/state permissions
(`0o700` runtime dir, `0o600` socket), and safe filesystem handling. Raw
bodies, prompts, cache keys, token values, and provider bodies stay out of
persistence/logs/diagnostics.

Deep dives: [Observability](deep-dive-observability.md),
[Metrics](deep-dive-metrics.md), [Dashboard](deep-dive-dashboard.md),
[Security](deep-dive-security.md).

### 13. Capabilities at a glance

- Inference surfaces: `POST /v1/chat/completions`, `POST /v1/messages`,
  `POST /v1/responses` (finite + SSE streaming; Responses stateless contract
  as above), `POST /v1/responses/compact` (finite-only bounded distinct
  compaction operation; native compact-capable upstreams only).
- Discovery/health: `GET /v1/models`, `GET /v1/healthz`, `GET /v1/readyz`,
  `GET /api/stats/runtime`, `GET /api/stats/update`.
- Dashboard: pages listed in §3 plus `/api/stats/summary`; observational only.
- CLI: full command tree in §1; `runtime.rs` keeps prompts/presentation/
  exit codes, `operations/lifecycle.rs` keeps reusable workflows,
  `server/*` stays thin.
- Config profiles: full `config.example.toml` plus low-wear
  `config.sbc.example.toml` (WAL cap, trace off, `low_wear` metrics,
  model-info/backup disabled; request/accounting durability preserved).
- Codex contract: `[model_providers.eggpool]` Responses shape with
  `wire_api = "responses"`, `supports_websockets = false`, generated
  `model_catalog_json` picker discovery, optional explicit model/alias,
  server `base_url`, and `EGGPOOL_API_KEY` env contract; OpenCode uses the
  Responses-capable `@ai-sdk/openai` runtime with `{env:EGGPOOL_API_KEY}`;
  see [Integrations](deep-dive-integrations.md).

### 14. Native dependencies and feature gates

`rust/Cargo.toml` authority: Tokio, Hyper/Hyper-util/Hyper-Rustls/Rustls,
Axum/Tower, Clap, Serde/TOML/JSON, SHA-2, `tokio-rusqlite` (bundled/backup),
Nix, Zip, Tracing, Eggress 1.0.6 (optional SSH compat), plus the path crate
`eggpool-model-routing`. Default `eggress-ssh-fallback` supports SSH upstreams;
`--no-default-features` still compiles/tests, keeps direct/non-SSH proxy, and
rejects SSH proxy config pre-dial. Test-only `test-support` adds deterministic
local TLS peers; protocol fixture crates stay dev-only.

Deep dives: [Providers](deep-dive-providers.md), [Core](deep-dive-core.md).

### 15. Tooling and tests (not a runtime fallback)

Release/validation tooling only: `scripts/` validators/builders/qualifiers
(`validate_release_docs.py`, `validate_runtime_package_boundary.py`,
`validate_release_artifacts.py`, `validate_release_workflow.py`,
`build_release_artifacts.py`, qualification harnesses), `tests/tooling/`
pytest suite, `packaging/` release manifests. No Python runtime fallback; never
 import the retired application. Native tests live in `rust/tests/` (serial
 `--test-threads=1`): `cli_contract`, `coordinator_c007`–`c011`, `c013`–`c014`
 (there is no `c012`) +
 `coordinator_boundaries`/`finalization`/`publication`, `wire_*`
(`codecs`, `stream`, `runtime`, `qualification`, `adaptation`, `profiles`,
`multimodal`), `operations_o002`–`o010`, `runtime_lifecycle_r002`–`r013`,
`routing_*`, `quota`, `health`, `catalog_refresh`, `model_router`,
`database_compatibility`, `provider_transport`, `canonical_request`,
`codex_responses_compat`, `codex_compaction_compat`.

Deep dives: [Core](deep-dive-core.md), [Deployment](deep-dive-deployment.md).

## Review index

| Module / concern | Authority paths | Deep dive |
|---|---|---|
| Entry, CLI, errors | `rust/src/main.rs`, `lib.rs`, `cli.rs`, `runtime.rs`, `error.rs`, `version.rs` | [Core](deep-dive-core.md) |
| Config, reload policy, reload | `rust/src/config.rs`, `config_reload_policy.rs`, `reload.rs`, `rust/build.rs`, `rust/build_support.rs` | [Core](deep-dive-core.md), [Control](deep-dive-control.md), [Runtime](deep-dive-runtime.md) |
| HTTP adapters | `rust/src/server/` | [Request lifecycle](deep-dive-request-lifecycle.md), [Dashboard](deep-dive-dashboard.md), [Runtime](deep-dive-runtime.md) |
| Admission | `rust/src/request/` | [Request lifecycle](deep-dive-request-lifecycle.md), [Transcoder](deep-dive-transcoder.md) |
| Coordinator finite/streaming | `rust/src/coordinator/`, `streaming/` | [Request lifecycle](deep-dive-request-lifecycle.md), [Retry](deep-dive-retry.md) |
| Publication/finalization/failure | `coordinator/publication.rs`, `finalization.rs`, `failure.rs`, `attempt.rs`, `wire_resolver.rs` | [Request lifecycle](deep-dive-request-lifecycle.md), [Retry](deep-dive-retry.md) |
| Wire/transcoding | `rust/src/wire/` | [Transcoder](deep-dive-transcoder.md) |
| Providers/transport | `rust/src/providers/` | [Providers](deep-dive-providers.md) |
| Routing/quota/health/accounts | `rust/src/routing/`, `quota/`, `health/`, `accounts/` | [Routing](deep-dive-routing.md), [Health](deep-dive-health.md) |
| Catalog/model-info | `rust/src/catalog/` | [Catalog](deep-dive-catalog.md), [Model info](deep-dive-model-info.md) |
| Semantic model routing | `rust/crates/eggpool-model-routing/`, `rust/src/model_router.rs` | [Routing](deep-dive-routing.md), [Models](deep-dive-models.md) |
| Persistence | `rust/src/db/`, `rust/assets/db/migrations/` | [Database](deep-dive-database.md) |
| Generations/lifecycle | `rust/src/runtime_lifecycle/`, `rust/src/task_supervisor.rs` | [Runtime](deep-dive-runtime.md), [Background](deep-dive-background.md) |
| Operations control/lifecycle | `operations/control.rs`, `lifecycle.rs`, `process.rs`, `paths.rs`, `config_mutation.rs` | [Control](deep-dive-control.md), [Deployment](deep-dive-deployment.md) |
| Deploy/backup/update | `operations/deploy.rs`, `backup.rs`, `update.rs`, `catalog.rs`, `provenance.rs` | [Deployment](deep-dive-deployment.md), [Lifecycle](deep-dive-lifecycle.md) |
| Operator/metrics/integrations | `operations/operator.rs`, `metrics.rs`, `integrations.rs` | [Metrics](deep-dive-metrics.md), [Integrations](deep-dive-integrations.md) |
| Observability/security | `operations/metrics.rs`, `server/dashboard.rs`, `server/health.rs`, `runtime_lifecycle/diagnostics.rs` | [Observability](deep-dive-observability.md), [Metrics](deep-dive-metrics.md), [Dashboard](deep-dive-dashboard.md), [Security](deep-dive-security.md) |
| Tooling/tests | `scripts/`, `tests/tooling/`, `rust/tests/`, `packaging/` | [Core](deep-dive-core.md), [Deployment](deep-dive-deployment.md) |

## Source-development flow

Run from the repository root with an explicit manifest:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
rust/target/debug/eggpool --help
```

Reduced surface (SSH fallback off) must still compile/test and must keep
direct/non-SSH proxy while rejecting SSH proxy config pre-dial:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features
```

Dependency/feature changes add:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo build --manifest-path rust/Cargo.toml --locked --release
```

Tooling (from repo root, `uv sync --frozen` for CI parity):

```bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

`Requires-Python >=3.11` in the packaging manifests is package-manager
compatibility for explicit historical targets, not a production interpreter
dependency. Build output stays under `rust/target/`.

See also [README.md](README.md) for the design index and
[migration history](../docs/migration-history.md) for the archival pointer.
