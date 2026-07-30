# EggPool Architecture Overview

EggPool is a lightweight, LAN-hosted proxy that aggregates multiple LLM provider accounts behind one OpenAI/Anthropic-compatible endpoint. Designed for Raspberry Pi and SBC deployments, it provides protocol transcoding, quota-aware routing, and a self-updating dashboard.

## System Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Client (OpenAI/Anthropic SDK)                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP
┌───────────────────────────────▼─────────────────────────────────────┐
│                        FastAPI Application                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐    │
│  │  API Handlers │  │  Dashboard   │  │  Control Plane         │    │
│  │  (chat, msgs) │  │  (HTML/JSON) │  │  (Unix socket rehash)  │    │
│  └──────┬───────┘  └──────────────┘  └────────────────────────┘    │
│         │                                                          │
│  ┌──────▼───────────────────────────────────────────────────────┐  │
│  │              RequestCoordinator                               │  │
│  │  endpoint → routing → persistence → contract → transcode     │  │
│  │              → proxy → streaming → finalization               │  │
│  └──────┬───────────────────────────────────────────────────────┘  │
│         │                                                          │
│  ┌──────▼──────┐  ┌──────────────┐  ┌────────────────────────┐   │
│  │   Router     │  │  Transcoder  │  │  Provider Client Pool  │   │
│  │  (quota-aware│  │  (OpenAI↔    │  │  (httpx per-provider)  │   │
│  │   scoring)   │  │   Anthropic) │  │                        │   │
│  └──────────────┘  └──────────────┘  └────────────┬───────────┘   │
│                                                     │               │
│  ┌─────────────────────────────────────────────────▼────────────┐  │
│  │                     SQLite (WAL mode)                         │  │
│  │  requests | attempts | routing_decisions | models | accounts  │  │
│  │  quotas | pings | backoffs | model_info_* | compression_*    │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Upstream Providers   │
                    │   (27+ providers)      │
                    └───────────────────────┘
```

## Request Lifecycle (Simplified)

1. **Endpoint** — Client hits `/v1/chat/completions` (OpenAI) or `/v1/messages` (Anthropic)
2. **Routing** — `Router` selects best account via priority tiers + `QuotaFairScorer`
3. **Persistence** — Attempt + routing decision written to SQLite before dispatch
4. **Provider Contract** — `compose_provider_url()` + `build_upstream_headers()` from provider config
5. **Transcoding** — Body translated if client protocol ≠ upstream protocol
6. **Proxy** — Request sent via `ProviderClientPool` httpx client
7. **Streaming** — SSE frames parsed by `IncrementalSSEObserver`; transcoded if needed
8. **Finalization** — Usage recorded, reservations released, health updated

## Component Index

Each component has a dedicated deep-dive document:

| # | Component | Overview | Deep Dive |
|---|-----------|----------|-----------|
| 1 | **Core Application** | CLI entry point, config, errors, constants, JSON backend | [deep-dive-core.md](deep-dive-core.md) |
| 2 | **Request Lifecycle** | Coordinator, API endpoints, proxy, finalization | [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) |
| 3 | **Protocol Transcoding** | OpenAI↔Anthropic body + streaming translation | [deep-dive-transcoder.md](deep-dive-transcoder.md) |
| 4 | **Routing & Quota** | Priority tiers, fairness rotor, quota estimation | [deep-dive-routing.md](deep-dive-routing.md) |
| 5 | **Provider Architecture** | Client pool, contracts, auth, 27+ providers | [deep-dive-providers.md](deep-dive-providers.md) |
| 6 | **Database Layer** | SQLite WAL, migrations, repositories | [deep-dive-database.md](deep-dive-database.md) |
| 7 | **Runtime & Process Management** | Generations, supervisor, Granian worker | [deep-dive-runtime.md](deep-dive-runtime.md) |
| 8 | **Dashboard & Stats** | Server-rendered HTML, JSON API, stats service | [deep-dive-dashboard.md](deep-dive-dashboard.md) |
| 9 | **Background Tasks** | TaskSupervisor, cleanup, backups, update checker | [deep-dive-background.md](deep-dive-background.md) |
| 10 | **Health Management** | Circuit breaker, cooldown, backoff tracking | [deep-dive-health.md](deep-dive-health.md) |
| 11 | **Model Info Sidecar** | Multi-source metadata enrichment, tiered matching | [deep-dive-model-info.md](deep-dive-model-info.md) |
| 12 | **External Integrations** | OpenCode, Claude Code, Aider, Codex, 8+ tools | [deep-dive-integrations.md](deep-dive-integrations.md) |
| 13 | **Security** | Header redaction, API key auth, constant-time compare | [deep-dive-security.md](deep-dive-security.md) |
| 14 | **Cache & Compression** | Observability, safe compression, synthetic cache, tuning | [deep-dive-cache-compression.md](deep-dive-cache-compression.md) |
| 15 | **Failure Effects & Quarantine** | Typed failure effects, bounded model quarantine, signal extraction | [deep-dive-health.md](deep-dive-health.md) |
| 16 | **Deployment & Operations** | Systemd, scripts, install, operational tools | [deep-dive-deployment.md](deep-dive-deployment.md) |

## Key Architecture Patterns

### Runtime Generations
Immutable frozen-dataclass snapshots swapped atomically via `RuntimeManager`. Request-path code obtains a `GenerationLease` — a generation swap never interrupts an in-flight request.

### Process Model
Supervisor + 1 Granian worker (`workers=1`). PID file owned by supervisor. Default `runtime_threads=1` (single event-loop thread is canonical).

### Database Invariants
SQLite WAL with single-connection serialization. All DML runs inside `async with db.transaction():`. 50+ schema migrations tracked by checksums.

### JSON Backend
`eggpool.jsonx` abstracts over `orjson` (preferred) and stdlib `json`. Hot-path serialization, SSE frame helpers, and request body parsing all route through this layer.

### Error Hierarchy
`AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning mismatches. `TranscodeLossError` for loss-policy rejects.

### Fast-Path CLI
`src/eggpool/fastcli.py` handles `croncheck` and `ensure-running` without importing Click — stays lightweight for Raspberry Pi watchdog cron jobs.

## Directory Structure

```
src/eggpool/
├── accounts/          # Account registry and runtime state
├── api/               # API endpoint handlers
├── background/        # TaskSupervisor, cleanup, periodic tasks
├── catalog/           # Model catalog, pricing, protocols
├── control/           # Control plane (Unix socket, live reload)
├── dashboard/         # Server-rendered HTML dashboard (50+ themes)
├── db/                # SQLite connection, migrations, repositories
├── health/            # Circuit breaker and health tracking
├── integrations/      # External tool configuration generation
├── lifecycle/         # Backup and uninstall orchestration
├── metrics/           # Metrics buffering and thinking observability
├── model_info/        # Model metadata sidecar (multi-source enrichment)
├── models/            # Pydantic config, domain, API models
├── observability/     # Routing trace writer
├── providers/         # Provider client pool and contracts
├── proxy/             # Transparent proxy, SSE observer, usage
├── quota/             # Quota estimation, reservations, scoring
├── request/           # RequestCoordinator, finalizers, dispatch
├── retry/             # Error classification
├── routing/           # Quota-aware routing, eligibility, fairness
├── security/          # Header redaction, security utilities
├── stats/             # Statistics queries and service
├── transcoder/        # Protocol transcoding + compression stack
│   └── compression/   # Safe compression, policy, tuning
├── _share/            # Bundled config examples for pipx
├── auth.py            # Local API key auth (constant-time)
├── cli.py             # CLI bootstrap (tiny)
├── cli_full.py        # Click CLI commands
├── config.py          # Config file helpers
├── config_validation.py
├── config_reload_policy.py
├── constants.py       # Project-wide constants
├── errors.py          # Exception hierarchy
├── jsonx.py           # JSON backend abstraction
├── runtime.py         # Process management
├── runtime_manager.py # Runtime generation ownership
├── runtime_dispatch.py# Dispatch timing recorders
├── runtime_metrics.py # Runtime/ops metrics
├── runtime_paths.py   # PID/log path resolution (stdlib-only)
├── fastcli.py         # Fast-path CLI (stdlib-only)
└── update_checker.py  # PyPI update checker

tests/
├── unit/              # ~242 test files
├── integration/       # ~80 test files
├── contract/          # Provider and transcoder contracts
├── perf/              # Performance baselines
├── soak/              # Long-running stability validation
├── live/              # Opt-in live external-source tests
├── helpers/           # Shared test utilities
└── fixtures/          # Test fixtures (cache_compression, streaming, etc.)

scripts/               # Operational, diagnostic, installer scripts
deploy/                # Systemd, logrotate, cron artifacts
docs/                  # Operator documentation (20+ topics)
plans/                 # 90+ design/implementation plans
config-examples/       # Agent config examples
```

## Configuration

Runtime configuration lives in `config.toml` + `.env` (API keys). Key sections:

| Section | Purpose |
|---------|---------|
| `[server]` | Host, port, workers |
| `[upstream]` | Default upstream settings |
| `[database]` | SQLite path, WAL mode, dispatch writer |
| `[routing]` | Fairness mode/epsilon/scope |
| `[models]` | `collapse_models`, catalog withdrawal |
| `[providers.<id>]` | Per-provider config (URL, auth, protocols, accounts) |
| `[transcoder]` | Protocol transcoding features, thinking/reasoning |
| `[compression]` | Request shaping (observe/safe modes, transforms) |
| `[cache]` | Synthetic cache controls |
| `[model_info]` | Source enablement, TTLs, overrides |
| `[dashboard]` | Theme, auth policy |
| `[metrics]` | Buffering, flush modes |
| `[backup]` | Automatic backup schedule |
| `[security]` | Header redaction |

## Testing

- **Unit**: ~242 files covering every module
- **Integration**: ~80 files for end-to-end flows
- **Contract**: Provider and transcoder contract tests
- **Performance**: Baselines and regression guards
- **Soak**: Long-running stability validation
- **Markers**: `unit`, `integration`, `slow`, `performance`, `live`, `network`, `soak`

## Further Reading

- `architecture/README.md` — Detailed design (cache/compression phases, routing guardrails, etc.)
- `.opencode/skills/architecture/SKILL.md` — Architecture principles and invariants
- `.opencode/skills/deployment/SKILL.md` — Deployment and operations
- `.opencode/skills/development/SKILL.md` — Development workflow
- `AGENTS.md` — Agent instructions, pre-commit checks, gotchas
