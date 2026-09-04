# F005 Closure — Axum SSR Shell and Static-Asset Parity Baseline

Status: closed

Recommendation: closed; proceed with subsystem-specific roadmaps for the
deferred provider, routing, transcoding, coordinator, runtime, and operations
workstreams.

Implementation commit: [`9d272b8`](https://github.com/eggstack/eggpool/commit/9d272b862cf8e7f55a8583ff51a020305922c431)

Plan: [F005 — Axum SSR shell and static-asset parity baseline](../../implementation/foundation/005-axum-ssr-shell-and-static-assets.md)

## Outcome

The Rust candidate now has a development-only Axum/Tower inbound server. It
opens and migrates the existing SQLite database, synchronizes configured
accounts, serves public health/readiness, applies the server API-key policy
and request-body limit, renders a representative dashboard overview and
summary read endpoint, and serves copied dashboard assets and themes. Python
remains the production implementation; no Python entry point, packaging, or
deployment behavior was replaced.

## Requirement matrix

| Requirement | Evidence | Result |
|---|---|---|
| A. Listener, health/readiness, graceful shutdown | `rust/src/server.rs`; migration HTTP tests cover health, readiness, bind conflict, and process cleanup | Pass |
| B. API-key auth and bounded request bodies | `authenticate`, fixed-size key comparison, `RequestBodyLimitLayer`; black-box tests cover missing key and 413 | Pass |
| C. Static/theme inventory parity | `rust/assets/dashboard/manifest.json`, copied resources, `copied_asset_manifest_matches_the_frozen_python_source` | Pass; 54/54 assets byte-identical |
| D. Escaping and theme primitives | `html_escape`, bounded theme selection, all registered themes embedded, unit tests for markup/quotes | Pass |
| E. Representative SSR page | `/` overview shell, period/theme controls, account breakdown, shared F004 repositories; DOM assertions in `test_f005_server.py` | Pass for the selected shell/read plane |
| F. Representative JSON read endpoint | `/api/stats/summary`, controlled empty-database shape and malformed-period response | Pass |
| G. Differential and operational evidence | Python/Rust health body equality, targeted Python dashboard/API suite, README run/parity instructions | Pass |

## Surface and parity record

| Surface | Rust behavior at closure | Auth |
|---|---|---|
| `GET /v1/healthz` | `200 {"status":"ok"}` | Public |
| `GET /v1/readyz` | `200` when the bounded readiness checks pass; otherwise `503` with a sanitized reason | Public |
| `GET /` | Representative Overview HTML shell when the dashboard is enabled | Dashboard policy |
| `GET /api/stats/summary` | Summary JSON for `1h`, `24h`, `7d`, or `30d`; invalid periods return `400` | Dashboard policy |
| `GET /static/dashboard.css`, `dashboard.js`, `chart.js`, `favicon.svg` | Copied Python resources with explicit content types and cache headers | Public |
| `GET /static/theme.css?theme=...` | Bounded CSS variables for registered TOML themes; default/unknown themes produce an empty stylesheet | Public |
| Inference paths | `/v1/chat/completions`, `/v1/messages`, and `/v1/responses` are explicit `501` placeholders | API-key policy |

Exact parity is claimed for the copied resource bytes and the health body.
The selected SSR page preserves the Python shell's route names, resource
paths, DOM anchors, text/escaping boundary, and theme/period controls. The
overview is intentionally a first read-plane subset, not a claim that every
Python dashboard page or every derived statistic has been ported.

## Verification evidence

Commands completed successfully:

- `rtk cargo fmt --manifest-path rust/Cargo.toml -- --check`
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`
- `rtk cargo test --manifest-path rust/Cargo.toml` — 17 Rust tests passed
- `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 23 tests passed
- `rtk uv run pytest tests/unit/test_dashboard.py tests/unit/test_dashboard_api.py tests/unit/test_dashboard_theme.py tests/integration/test_dashboard_routes.py -q --tb=short --maxfail=1` — 461 tests passed
- `rtk uv sync --frozen --extra ci`
- `rtk uv run ruff format --check src/ tests/ scripts/`
- `rtk uv run ruff check src/ tests/ scripts/`
- `rtk uv run pyright src/ scripts/`
- `rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 tests passed
- `rtk git diff --check`

The migration suite specifically verified Python/Rust health equality, the
private dashboard's exact unauthorized response, readiness degradation on an
empty configured-account set, summary empty-shape values, static CSS hashing,
oversized-body rejection, malformed-period rejection, bind failure, and
listener/process cleanup. The manifest unit test verifies every copied static
and theme resource against the Python source tree.

## Failure, security, and lifecycle evidence

- Non-loopback startup requires a configured API key with the bounded Python
  character/length contract; loopback development configs may omit it.
- Bearer and `x-api-key` values are compared in fixed 512-byte buffers, and
  invalid/missing credentials return only a generic detail. Keys, prompts,
  request bodies, and provider credentials are not persisted or rendered.
- Axum's body-limit layer rejects oversized inference requests before the
  placeholder handler receives them. DB read failures become sanitized 503
  responses rather than panics.
- Startup migration/account synchronization occurs before listener admission;
  bind failure exits without opening a second listener. SIGINT/SIGTERM use the
  Axum graceful-shutdown path, and the black-box test confirms process exit.
- F004's single-worker SQLite ownership and canonical migration/checksum
  machinery are reused. F005 adds no schema migration and does not create a
  second writer or renderer-global mutable state.
- A targeted Firefox load of the local overview reached the `Overview` page
  title at `127.0.0.1:11301`; the environment's screen-capture stream failed
  when a full visual screenshot/accessibility capture was requested. The
  fixed HTTP/DOM/static assertions therefore remain the reproducible visual
  evidence for this environment.

## Dependency and migration delta

F005 adds Axum, Hyper, Serde JSON, Tower, Tower HTTP's request-limit layer,
and Tokio networking/signal features. It does not add outbound HTTP/TLS,
provider dispatch, Eggress, WebSockets, or a new crate hierarchy. The Rust
server is invoked with an explicit config and remains side-by-side only.

## Limitations and unresolved findings

No unresolved findings remain for the F005 scope. Deferred work includes
provider dispatch, complete model/catalog synchronization, full dashboard
pages, provider-aware stats, inference streaming, rehash/control, daemon and
systemd integration, and production cutover. These are explicit later
milestones, not closure defects. Theme translation currently exposes the
foundation CSS-variable subset; the copied TOML files remain available for
future exact visual expansion.

## Planning follow-through

F005's F003/F004 dependencies are closed, so the plan is removed from the
dependency-ready queue and recorded as completed in `migration-rs/registry.md`.
The foundation roadmap is closed. No currently represented future plan was
blocked on F005: the registry deliberately has no provider/routing/
transcoding/coordinator/runtime/operations handoff plans yet. Their statuses
remain unrepresented/not applicable, rather than being marked complete; the
next planning action is to create repository-specific subsystem roadmaps and
handoff plans when those workstreams are scoped.
