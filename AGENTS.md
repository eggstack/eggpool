# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — architecture index and quick reference; see `architecture/README.md` for full design details
- `deployment` — production deployment, systemd, operational scripts, configuration changes
- `development` — linting, testing, pre-commit checks, code style
- `documentation` — doc map, accuracy verification against code, and pruning rules for README/docs/architecture/AGENTS.md changes

## Quick Start

- Package manager: **uv** (not pip) for the Python tooling environment. Install deps: `uv sync --dev`
- CI installs with `uv sync --frozen` (locks match `uv.lock` exactly) — dependency changes must update `uv.lock`
- Entry point: `rust/src/main.rs` / `rust/src/cli.rs` → native `eggpool` executable
- Config resolution: `--config` flag > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`. API keys come from environment/`.env`
- Optional extras: none for the native runtime; Python dependencies are tooling-only
- Cargo is the authority for native runtime dependencies and features. Use `cargo tree --manifest-path rust/Cargo.toml -e features` when reviewing dependency changes; keep direct crates only when Rust source, build scripts, tests, packaging, or a documented compatibility contract names them.
- **Do not** add Python runtime fallbacks — retained Python is tooling-only

## Local Development Loop

Fast focused iteration:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run ruff format <changed tooling paths>
uv run ruff check <changed tooling paths>
uv run pytest <affected tooling test paths> -q --tb=short --maxfail=1
```

## Before-Push Check

Run the same checks as the CI job:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

For dependency or feature changes, also run the locked release build and
inspect the resolved graph:

```bash
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

## CI

One GitHub Actions job (`check`, Rust plus Python tooling): Cargo formatting,
strict Clippy, serial Rust tests, plus ruff, pyright, and
`pytest tests/tooling/`. Reproduce locally for deterministic results. New
Clippy warnings are not an accepted baseline; fix them before merging.

CI ignores paths-only changes to `docs/`, `architecture/`, `plans/`, `.opencode/skills/`, `CHANGELOG.md`, and `AGENTS.md` — docs-only PRs will show no CI run.

## Focused Verification

```bash
# Rust test target
cargo test --manifest-path rust/Cargo.toml --test operations_o008 -v

# Single test by name
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -v

# Integration tests only
uv run pytest tests/tooling/ -v

# Network-dependent tests
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --nocapture

# Opt-in live provider verification (requires test-only credentials)
uv run pytest tests/live/ -m live_opencode_go -v

# Lint auto-fix
uv run ruff check --fix scripts/ tests/tooling/
```

Markers registered in `pyproject.toml`: `unit`, `integration`, `network`, `live`,
`live_provider`, `live_opencode_go`, `live_gemini`, `performance`, `slow`.

## Code Style

- Python 3.11+ for retained tooling, with `from __future__ import annotations` in Python files
- Type hints on all function signatures and return types
- Ruff: E, F, W, I, N, UP, B, A, SIM, TCH rules; line length 88
- Pyright strict mode — covers `scripts/`; ruff covers `scripts/` and `tests/tooling/`
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- Python tests cover release/validation tooling under `tests/tooling/`; the native runtime is covered by Cargo targets in `rust/tests/`.
- No real provider network is used by the tooling suite.
- Rust integration/operation tests are the runtime contract; manual live-provider checks remain opt-in.

## Release

The pinned `.github/workflows/release.yml` is the production release
authority. See `docs/releasing.md` for manual checks, staged rehearsal,
rollback, and publication verification.

## File Organization

- Source: `rust/`; Tests: `rust/tests/`; Tooling: `scripts/` and `tests/tooling/` (scripts type-checked by pyright); Deployment: `deploy/`
- Config references: `config.example.toml`, `config.sbc.example.toml`, `.env.example`
- DB migrations: numbered SQL files in `rust/assets/db/migrations/`
- Shared assets: `rust/assets/` (embedded runtime templates/assets)
- Architecture docs: `architecture/` (deep dive per subsystem); Plans: `plans/` (historical record — consult only when the change falls within an active plan)

## Architecture

Start subsystem work at `architecture/README.md`, then the matching deep dive:

| Subsystem | Deep dive |
|-----------|-----------|
| CLI, config, errors, JSON backend | `architecture/deep-dive-core.md` |
| Request lifecycle, coordinator, proxy, finalization | `architecture/deep-dive-request-lifecycle.md` |
| Protocol transcoding (OpenAI ↔ Anthropic) | `architecture/deep-dive-transcoder.md` |
| Routing & quota | `architecture/deep-dive-routing.md` |
| Providers, contracts, outbound clients | `architecture/deep-dive-providers.md` |
| SQLite, migrations, repositories | `architecture/deep-dive-database.md` |
| Runtime generations & process management | `architecture/deep-dive-runtime.md` |
| Health, circuit breaker, quarantine | `architecture/deep-dive-health.md` |
| Background tasks & backups | `architecture/deep-dive-background.md` |
| Dashboard & stats API | `architecture/deep-dive-dashboard.md` |
| Model catalog & pricing | `architecture/deep-dive-catalog.md` |
| Model-info sidecar | `architecture/deep-dive-model-info.md` |
| Control plane (rehash) | `architecture/deep-dive-control.md` |
| Pydantic data models | `architecture/deep-dive-models.md` |
| Agent integrations (configsetup) | `architecture/deep-dive-integrations.md` |
| Security & redaction | `architecture/deep-dive-security.md` |
| Observability & routing traces | `architecture/deep-dive-observability.md` |
| Retry classification & backoff | `architecture/deep-dive-retry.md` |
| Metrics & telemetry | `architecture/deep-dive-metrics.md` |
| Backup/restore/uninstall lifecycle | `architecture/deep-dive-lifecycle.md` |
| Deployment & operations tooling | `architecture/deep-dive-deployment.md` |

Non-obvious wiring:

- **Request lifecycle**: `RequestCoordinator` in `rust/src/coordinator/` orchestrates endpoint → routing → persistence → dispatch → finalization; HTTP layer is in `rust/src/server.rs`
- **Runtime generations**: the Rust runtime lifecycle module owns active/retiring generations and leases; server state mirrors are not authority
- **Model-router registry/selector**: neutral policy compilation, deterministic registry semantics, and session identities are owned by `rust/crates/eggpool-model-routing`; `rust/src/model_router.rs` retains the process-owned async affinity cache. The immutable registry is generation-owned and dispatches concrete child requests through the coordinator
- **Model-router request path**: exact virtual aliases resolve before concrete parsing and target-specific checks; the resolved target then follows the unchanged coordinator path. `/v1/models` exposes only compact capability-free virtual metadata, and `/api/stats/runtime` carries bounded semantic-routing counters
- **Protocol transcoding**: `rust/src/wire/` converts OpenAI ↔ Anthropic and other supported surfaces; operator guide `docs/transcoding.md`
- **Wire surfaces**: `/v1/responses` is stateless but may adapt through the canonical wire boundary to OpenAI Chat, Anthropic Messages, or native Gemini surfaces. Surface selection is the `request_surface` field (`"chat_completions"` | `"responses"`), while concrete upstream selection is `WireProfile.surface` → `architecture/deep-dive-request-lifecycle.md`
- **Control plane**: live config reload (rehash) over a Unix-domain socket, `rust/src/operations/control.rs` and `rust/src/reload.rs`
- **Routing**: load-based, never cost-based; tier-based via `routing_priority`; provider/account routing is split across `routing/`, `quota/`, `retry/`, `catalog/`, `health/`, while semantic virtual-model policy is compiled by `rust/crates/eggpool-model-routing`
- **Wire surfaces**: the closed registry in `rust/src/wire/` owns concrete surfaces; provider profiles are embedded from `rust/assets/providers/_wire_profiles.toml`
- **Process model**: the native Rust executable owns the server process and its bounded worker/runtime configuration; no Python application process is required

## Gotchas

- **Single runtime owner is canonical**: mutable runtime generations and lifecycle state are owned by the native process
- **Task-owned transactions**: SQLite access is scoped to the owning Rust task/transaction; process transitions execute atomically with rollback
- **`/readyz` never performs a write**: reads a cached probe snapshot
- **`eggpool rehash` serializes reloads**: one reload at a time; concurrent attempts use the stable reload-busy exit code. Disruptive changes (host, port, db path) require restart instead
- **Model routers**: `[model_routers.<id>]` is optional, structurally validated, compiled during candidate generation construction, and live-reloadable as one mapping. Exact virtual aliases dispatch through the selector, then normal concrete routing. Sticky routers use the process-owned bounded TTL/LRU affinity cache; `X-EggPool-Route-Session` is hashed, never logged/persisted/forwarded, and semantic fingerprint changes invalidate old decisions. Automatic Chat/Messages identities reserve bounded bytes for the first user turn even with large system/developer prefixes; Responses requires the explicit header. `sticky = false` bypasses affinity. Selector prompts/output stay out of persistence; a 2xx invalid selector result can get one repair with the same bounded semantic context, while non-2xx results fall back immediately as `unavailable`. Aliases never semantic-failover after submission, and affinity/metrics never enter quota/account scoring
- **`ReloadObserver` is inert in production**: observer protocol defaults to no-ops
- **`eggpool connect`/`logout` don't silently restart**: healthy server with missing control socket returns `(False, "control unavailable (server healthy)")`
- **`eggpool update` does a live PyPI lookup**: bare update uses freshness-aware latest check; explicit `VERSION` uses the exact release endpoint and permits deliberate downgrades
- **`static_models` is source of truth for provider-specific protocol**: providers serving non-default protocols must ship `[[providers.<id>.static_models]]` rows
- **No pre-commit hooks configured**: CI runs ruff, pyright, and pytest via GitHub Actions
- **Cargo dependency authority**: direct runtime crates and non-default features must have a current owner in `rust/src/`, `rust/build.rs`, `rust/tests/`, packaging, or a documented compatibility contract. Do not remove Eggress proxy features or TLS/SQLite features from a text search alone; qualify the reduced graph and the affected provider-transport paths.
- **Model-info enrichment lifecycle**: startup performs one bounded external pass
  when enabled; recurring due work runs from the generation-leased
  `catalog_refresh` tick. There is no standalone `model_info_refresh` task.
  `[models].refresh_interval_s` controls opportunities, while canonical
  `next_refresh_at`/status TTLs and source cooldowns control due work.
  `model_info.refresh_interval_s` is deprecated compatibility-only; with
  `models.refresh_interval_s = 0`, later enrichment requires manual refresh or
  restart.
- **When constructing coordinator fixtures in tests**: pass explicit policy/configuration or assert the desired default
- **`ProviderBoundRequest` dispatch-freeze**: `serialize_provider_payload()` freezes the body; `replace_provider_payload()` and `set_provider_payload(increment_generation=False)` reject when frozen. Only generation-incrementing methods (`set_provider_payload(increment_generation=True)`, `adopt_provider_payload(increment_generation=True)`) clear the freeze — the post-selection transcoder relies on this to replace a previously dispatched body on retry
- **Thinking rejection error class**: `CapabilityError` (400) only when the aggregated thinking status is genuinely `unknown` or `unsupported`. When all supporting accounts are quarantined but the provider entry reports `supported`/`mixed`, a transient 503/502 is raised instead. Aggregation iterates `cache.get_provider_model_entries()` (which applies overrides), not `cache.get_model()` (which does not). Reasoning support and caller controls are discovered per provider/model from explicit catalog or verified model-info metadata; EggPool does not infer effort levels from model names. Operator overrides remain the intentional escape hatch. See `RequestCoordinator._determine_thinking_rejection_status` and `architecture/deep-dive-request-lifecycle.md`
- **Compositional reasoning metadata**: provider-bound `ThinkingControlContract` is the canonical source for independent `toggle`, `effort`, and `budget` support. Routing and post-selection adaptation match the exact requested dimension and effort label; reasoning support does not imply universal caller control. Omitted `reasoning_options` remains unknown; a complete empty list means supported reasoning with no caller control. Legacy `mode`, `supported_efforts`, budget bounds, and effort maps are compatibility inputs/projections, and discovery never fabricates effort-to-budget values. See `architecture/deep-dive-catalog.md` and `architecture/deep-dive-transcoder.md`
- **Per-model quarantine suppresses account-wide circuit breaker**: when `effects.model_effect != "none"` (quarantine), `EffectsApplier._apply_account_effect` skips `HealthManager.record_failure()` for that account. The account-wide breaker advances only when the classifier sets `source="transport"` (genuine account-wide failure: DNS, TLS, persistent transport failure with no per-model cause). A per-model 5xx must quarantine the `(account, model)` pair, not the whole account. See `architecture/deep-dive-health.md`
- **Wire profile credentials stay out of metadata**: `resolve_provider_wire_profiles()` carries auth shape only; call `build_wire_profile_headers()` with the selected account key at dispatch time. Surface priorities and bundled hints are revocable preferences, not endpoint truth. See `architecture/deep-dive-providers.md`
- **Runtime wire learning is process-owned and bounded**: `ProcessRuntime.wire_profile_resolver` survives safe generation swaps, keys learned state by a structural candidate fingerprint, and learns only from completed ordinary success or an explicit failure-effects transition. It never probes in the background, stores secrets/raw bodies, or adds a retry budget. See `architecture/deep-dive-providers.md` and `architecture/deep-dive-runtime.md`
- **Canonical wire intent is source-owned**: `rust/src/wire/ir.rs` captures the original request, reasoning intent, normalized usage, response blocks, and bounded streaming events before provider adaptation. Alternate targets must encode from that canonical source; never chain a previously translated provider payload. `ReasoningIntent` keeps effort labels separate from numeric budgets and explicit disable. See `architecture/deep-dive-transcoder.md`
- **Default wire codecs are concrete and terminal-aware**: the closed registry owns executable codecs for `openai_chat_completions`, `openai_responses`, `anthropic_messages`, `gemini_interactions`, and `gemini_generate_content`. Streaming adapters forward native grammar and require native terminal evidence; transport EOF never synthesizes a client terminal event. See `architecture/deep-dive-transcoder.md` and `architecture/deep-dive-providers.md`
- **Negotiation-safe failure effects**: a bare/unknown 401 never disables credentials, advances health, or cascades; only explicit invalid/expired/revoked credential evidence disables the selected account. Typed wire auth/surface/schema/model-on-surface signals take precedence over generic `Unsupported*` error classes, but negotiation still requires a declared alternate and pre-handoff response-status evidence. Weak model/endpoint availability wording is wire-local only when the selected provider-scoped catalog knows the model; strong `model not found`/authoritative absence remains model quarantine/withdrawal and does not enumerate surfaces. Deterministic wire rejection may enter one provider/model single-flight before downstream handoff. Leaders alone submit discovery candidates under the provider-wide abnormal-dispatch gate; followers share only the wire decision. 429/rate pressure ends discovery without candidate suppression, and cancellation cannot release unowned capacity. All account and wire retries consume one shared `1 + max_retries_before_stream` upstream-submission budget. See `architecture/deep-dive-retry.md` and `architecture/deep-dive-request-lifecycle.md`
- **Live wire verification is opt-in**: `tests/live/test_opencode_go_wire_live.py` uses `EGGPOOL_E2E_OPENCODE_GO_API_KEY`, temporary state, bounded prompts, and sanitized outbound observations, including Muse Spark 1.2/1.3 Responses requests, MiniMax-M3 binary-toggle Chat-to-Messages adaptation, and local rejection of an invalid MiniMax effort. It is excluded from default pytest, smoke, and CI; deterministic wire-negotiation and failure-isolation coverage is mandatory locally.

## Error Handling

Read `rust/src/error.rs` before adding error variants and preserve the existing
HTTP/status mapping contracts. Keep error context explicit and retain the
original cause when wrapping an error.

## CLI

The native `eggpool` executable is implemented in `rust/src/main.rs` and
`rust/src/cli.rs`. Operational commands, installer/update provenance, and
control-plane behavior are Rust-owned; Python remains a release and validation
tool only.

## Git Workflow

- Branch: `main`; commit messages concise, imperative mood
- Never commit secrets, API keys, or `.env` files

## Planning Policy

Planning is proportional to risk. Use the development skill's "Planning proportionality" section for guidance.
