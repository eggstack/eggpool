# F002 Contract Inventory

Status: reviewed seed inventory

Oracle: the checked-out Python implementation under `src/eggpool/`, invoked
through `tests/migration_rs/harness.py`.  The Rust candidate is the explicit
`rust/target/debug/eggpool` executable from F001.  This inventory records
observable contracts, not Python class structure.  It was refreshed against
commit `5f2c8b83` on 2026-09-04.

Parity classes use `exact` for bytes/status/exit/field names and `semantic` for
meaning that may cross a framework boundary (for example HTML DOM facts).

## Authority and parity map

| Contract family | Python oracle | Observation | Parity |
|---|---|---|---|
| CLI | `src/eggpool/cli.py`, `cli_full.py`, `cli_exit_codes.py` | exit code, stdout, stderr, command/option arguments | exact |
| Configuration | `src/eggpool/models/config.py`, `config.py`, `config_validation.py`, `config_utils.py`, `deploy_user.py` | valid/invalid result, error category, resolved path, safe diagnostics | exact semantics; path spelling normalized only in isolated roots |
| HTTP/API/SSE | `src/eggpool/app.py`, `src/eggpool/api/`, `src/eggpool/proxy/sse.py` | method, path, status, reviewed headers, body, ordered SSE frames | exact wire contract |
| SQLite | `src/eggpool/db/schema/*.sql`, `migrations.py`, `connection.py`, repositories | migration names/checksums, tables/columns/indexes, selected durable row effects | exact schema and durable effects |
| Dashboard SSR | `src/eggpool/dashboard/routes.py`, `render.py`, `escape.py`, `theme.py` | raw HTML plus element attributes/text facts | semantic DOM plus escaping-visible bytes |
| Static/theme resources | `src/eggpool/dashboard/static/`, `themes/`, `_resources.py` | path, content type, size, SHA-256 | exact bytes |
| Provider/account proxy forms | `src/eggpool/models/config.py`, `providers/contract.py`, `providers/pproxy_transport.py` | accepted mutually-exclusive fields and outbound structural stub observations | exact config semantics; no live providers |

## CLI command and option inventory

The root `--config PATH` option precedes every command.  The entries below are
the complete Click command tree; an empty option cell means the command has no
command-local options or arguments.

| Command | Options/arguments |
|---|---|
| `accounts` | — |
| `accounts list` | — |
| `accounts status` | — |
| `accounts explain` | `--model`, `--provider`, `--protocol`, `--scores`, `--gates` |
| `backup` | `--output-dir` |
| `check-config` | — |
| `configsetup` | — |
| `configsetup aider`, `cline`, `codex`, `continue`, `goose`, `kilo`, `openhands`, `qwen-code`, `roo-code` | `--print-secret`, `--no-clipboard`, `--force`, `--output`, `--write`, `--model`, `--base-url`, `--host` |
| `configsetup opencode` | — |
| `connect` | `--providers` |
| `connect list` | — |
| `croncheck` | — |
| `dashboard` | — |
| `dashboard public` | `--on` |
| `db` | — |
| `db vacuum` | — |
| `deploy` | — |
| `deploy all` | `--install` |
| `deploy backup-cron` | `--install`, `--uninstall`, `--production`, `--user` |
| `deploy cron` | `--install`, `--uninstall`, `--interval`, `--user` |
| `deploy logrotate` | `--install` |
| `deploy systemd` | `--install`, `--production`, `--as-root` |
| `edit` | — |
| `ensure-running` | — |
| `getkey` | — |
| `help` | — |
| `init-config` | `target`, `--force` |
| `logout` | `target` |
| `migrate` | — |
| `modelinfo` | — |
| `modelinfo aliases` | `model_id`, `--source` |
| `modelinfo list` | `--status` |
| `modelinfo refresh` | `--provider-catalog-only` |
| `modelinfo repair` | `--limit` |
| `modelinfo show` | `model_id` |
| `models` | — |
| `models refresh` | — |
| `newkey` | `--show-old` |
| `onboard` | `--providers` |
| `recover` | `source` |
| `rehash` | `--json` |
| `restart` | `--timeout` |
| `runtime-status` | `--json` |
| `serve` | `--verbose`, `--log-file`, `--quiet`, `--as-root` |
| `set` | `key`, `value` |
| `stats` | — |
| `stats explain-dashboard` | `--period`, `--bucket`, `--group-by`, `--json` |
| `stats recompute-costs` | `--dry-run`, `--limit` |
| `stats repair-costs` | `--provider`, `--since`, `--dry-run`, `--limit` |
| `stats transcoding` | `--period`, `--json` |
| `stop` | `--timeout` |
| `uninstall` | `--yes`, `--keep-data`, `--keep-config`, `--keep-path`, `--deploy-artifacts` |
| `update` | `requested_version`, `--check`, `--from-source` |
| `version` | — |

`--help` and Click's unknown-command/unknown-option/missing-argument responses
are captured as CLI observations.  Help wrapping is not normalized until a
terminal-width difference is proven incidental.

## Configuration sections, defaults, aliases, and proxy forms

`AppConfig` has the following top-level sections.  Defaults are created by the
named Pydantic model's default factory; scalar examples are the current Python
defaults.  Nested fields are part of the same section contract and are
enumerated by the model definitions, not discarded by the harness.

| Section | Model | Important defaults and compatibility aliases |
|---|---|---|
| `server` | `ServerConfig` | `host=127.0.0.1`, `port=11300`, `api_key_env=SERVER_API_KEY`, `threads=1`, body limit 10 MiB |
| `upstream` | `UpstreamConfig` | OpenCode Go base URL; connect/read/write/pool timeouts 5/300/30/30 seconds |
| `database` | `DatabaseConfig` | `path=usage.sqlite3`, busy timeout 5000 ms, WAL on, `synchronous=NORMAL`, one worker |
| `models` | `ModelsConfig` | refresh 300 s, `expose_mode=union`, startup refresh on, stale-after 7200 s |
| `model_routers` | `dict[str, ModelRouterConfig]` | optional mapping; aliases are exact and not catalog-resolved during validation |
| `routing` | `RoutingConfig` | `strategy=quota_fair`, retries 3, fairness `round_robin`, wire negotiation enabled |
| `limits` | `LimitsConfig` | five-hour/weekly/monthly limits 12/30/60 million microdollars |
| `pricing` | `PricingConfig` | fallback `generic_estimate`; OpenRouter and OpenCode Zen catalog entries |
| `dashboard` | `DashboardConfig` | enabled, private, theme `Cyber Red`, content persistence rejected |
| `security` | `SecurityConfig` | empty host/CORS/proxy lists; redact `authorization` and `x-api-key` |
| `metrics` | `MetricsConfig` | `write_mode=low_wear`, aggregate-only, 120 s flush |
| `maintenance` | `MaintenanceBudgetConfig` | bounded cleanup budgets from model defaults |
| `backup` | `BackupConfig` | model defaults; backup is opt-in operational behavior |
| `readiness_probe` | `ReadinessProbeConfig` | disabled, interval 10 s, freshness 30 s, initial probe on |
| `network` | `NetworkConfig` | model defaults; diagnostics remain auth-gated |
| `proxies` | `dict[str, ProxyConfig]` | each entry must set exactly one of `url` or `url_env` |
| `accounts` | legacy flat `list[AccountConfig]` | normalized into the default provider when `providers` is absent |
| `providers` | `dict[str, ProviderConfig]` | empty by default; provider key must equal declared `id` |
| `model_overrides` | mapping | empty by default |
| `model_capabilities` | mapping | empty by default |
| `transcoder` | `TranscoderPolicy` | enabled compatibility path, `loss_policy=warn`, native preference on |
| `model_info` | `ModelInfoConfig` | enabled/startup refresh on; legacy `refresh_interval_s=21600` retained |
| `update_checker` | `UpdateCheckerConfig` | model defaults |

Resolution precedence is `--config`, `$EGGPOOL_CONFIG`,
`~/.config/eggpool/config.toml`, then `./config.toml`.  Environment indirection
is used for server/account/provider credentials and proxy URLs; secret values
never enter a persisted observation.  Account proxy configuration accepts one
of `proxy` (named configured proxy), `proxy_url`, or `proxy_url_env`, never more
than one.  Provider auth additionally distinguishes bearer, API-key, raw
authorization, and no-auth forms, with validated static/additional headers.

## HTTP/API/SSE inventory

The app uses `src/eggpool/app.py` as the composition oracle.  `public` below
means no API key is required for the selected route; dashboard and diagnostics
routes are private unless the dashboard public setting explicitly changes the
dashboard group.  API docs are framework public routes.

| Methods | Paths | Auth/parity |
|---|---|---|
| `GET` | `/v1/healthz`, `/v1/readyz` | public; exact status/body |
| `GET` | `/v1/models` | API-key required in handler |
| `POST` | `/v1/chat/completions`, `/v1/messages`, `/v1/responses` | API-key request path; exact JSON/SSE grammar |
| `GET` | `/v1/openapi.json`; `GET,HEAD /v1/docs`; `GET,HEAD /docs/oauth2-redirect`; `GET,HEAD /redoc` | framework docs/public |
| `GET` | `/`, `/accounts`, `/models`, `/models/{model_id:path}`, `/latency`, `/events`, `/timeseries`, `/bandwidth`, `/pings`, `/reliability`, `/routing`, `/traces`, `/runtime`, `/cache` | dashboard auth policy; semantic DOM |
| `GET` | `/api/timeseries`, `/api/timeseries/grouped` | dashboard auth policy; JSON |
| `GET` | `/api/stats/accounts`, `/api/stats/attempts`, `/api/stats/bandwidth`, `/api/stats/errors`, `/api/stats/ips`, `/api/stats/latency`, `/api/stats/models`, `/api/stats/operational`, `/api/stats/pending-health`, `/api/stats/pings`, `/api/stats/pricing-provenance`, `/api/stats/recent-requests`, `/api/stats/recent/{request_id}`, `/api/stats/retries`, `/api/stats/routing`, `/api/stats/routing-exclusions`, `/api/stats/routing-selections`, `/api/stats/routing-skew`, `/api/stats/routing/eligibility`, `/api/stats/runtime`, `/api/stats/summary`, `/api/stats/thinking`, `/api/stats/timeseries`, `/api/stats/transcoding`, `/api/stats/update` | dashboard auth policy; JSON |
| `GET` | `/api/stats/cache-observability`, `/api/stats/cache-stability`, `/api/stats/canonical-request-segmentation`, `/api/stats/request-shaping` | dashboard auth policy; JSON |
| `GET` | `/api/backoffs`, `/api/events`, `/api/network/diagnostics` | auth required |
| `GET` | `/api/model-info`, `/api/model-info/sources`, `/api/model-info/{model_id:path}`, `/api/model-info/{model_id:path}/aliases`, `/api/model-info/{model_id:path}/matches` | dashboard auth policy; JSON |
| `POST` | `/api/model-info/refresh` | dashboard auth policy; bounded refresh mutation |
| `GET` | `/static/dashboard.css`, `/static/dashboard.js`, `/static/chart.js`, `/static/favicon.svg`, `/static/theme.css` | public; exact bytes/content type |

The inventory retains duplicate registrations if the Python composition exposes
them; comparisons must report a duplicate rather than silently deduplicate a
route contract.

## SQLite migration and table inventory

The canonical migration directory is `src/eggpool/db/schema/`; it contains
numbered SQL migrations `0001` through `0054` and the reviewed
`checksums.json`.  The migration table has 54 applied rows after a current
empty-database migration.  Rust must consume these SQL files/checksums rather
than create a second schema source.

| Migration range | Contract focus |
|---|---|
| `0001`–`0014` | initial accounts/models/requests/reservations/pricing, attempts, health, protocol, bandwidth |
| `0015`–`0026` | providers, metadata, pings, client IP, dashboard indexes, backoffs, attempt observability |
| `0027`–`0035` | routing decisions, operational events, latency, pricing provenance, rollups, transcoding, score components |
| `0036`–`0047` | model-info lifecycle, thinking/cache/canonical segmentation, compression controls, rollup normalization |
| `0048`–`0054` | cost repairs, model-info evidence, routing retention, quarantine, catalog refresh, final quarantine identity fix |

Current non-SQLite system tables observed after migration are:

```text
_migrations, account_backoffs, account_events, account_models, accounts,
catalog_refresh_state, compression_tuning_overrides,
compression_tuning_recommendations, health_probe, model_info_aliases,
model_info_canonical, model_info_match_evidence, model_info_observations,
model_info_overrides, model_info_source_health, model_price_snapshots,
model_pricing_aliases, model_quarantine, models, operational_events,
provider_model_metadata, provider_pings, providers, request_attempts,
request_cost_repairs, requests, reservations, routing_decisions,
transcoding_daily, usage_rollups
```

Schema observations include column order/type/nullability/primary-key/default,
index identity/uniqueness, row counts for selected fixture tables, and the
migration checksum map.  They do not include SQLite page counts, WAL residue,
timestamps, or arbitrary request content.

## Dashboard, static files, and themes

Static routes map to these exact source files:

| Route | Source | SHA-256 |
|---|---|---|
| `/static/dashboard.css` | `dashboard/static/dashboard.css` | `3f399629eb182de5689cb475b02f60dc468969d5e8fead059a6ebda82222ac6d` |
| `/static/dashboard.js` | `dashboard/static/dashboard.js` | `e08fcc2259cd25a0fbed62bafeb37520cddc2931c6600123734e0404f31d989f` |
| `/static/chart.js` | `dashboard/static/chart.umd.min.js` | `206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e` |
| `/static/favicon.svg` | `dashboard/static/favicon.svg` | `593a7d0f464160e832f62399cf224a04dda68a55f3a6ce847dffb62016d37653` |

`/static/theme.css` is generated from a selected TOML theme through
`dashboard/theme.py`; its response is compared by exact CSS for a fixed theme
fixture.  The source theme set currently includes 50 TOML files, including
`Cyber Red.toml`, `Cyberpunk.toml`, `Dracula.toml`, `Nord.toml`, the three
Catppuccin variants, the three Rose Pine variants, and the remaining files in
`src/eggpool/dashboard/themes/`.  Theme names and path escaping are exact
inputs; unknown theme behavior is a contract case, not a normalization rule.

## Seed corpus

The first small Python oracle capture descriptors are committed in
[`fixtures/foundation/f002-python-oracle-captures.json`](fixtures/foundation/f002-python-oracle-captures.json).
They cover CLI version, valid/invalid configuration, health JSON, a migrated
SQLite schema, and static resources.  The executable test corpus lives in
`tests/migration_rs/test_harness.py`; later milestones should add two-sided
cases beside these seeds rather than snapshotting the entire Python suite.
