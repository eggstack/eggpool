# API Reference

EggPool exposes OpenAI Chat Completions- and Anthropic Messages-compatible paths, plus internal diagnostic endpoints.

## Chat Completions & Anthropic Messages

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions-compatible requests |
| `POST` | `/v1/responses` | Stateless OpenAI Responses-compatible requests; canonical adaptation is allowed to eligible upstream surfaces |
| `POST` | `/v1/messages` | Anthropic Messages-compatible requests |

Configured virtual model routers are included in `/v1/models` as compact,
capability-free entries with `owned_by = "eggpool"` and
`eggpool.virtual = true`. They do not expose selector prompts, route
descriptions, affinity state, prices, or concrete target capabilities. A
virtual request is resolved to a concrete model before normal context,
capability, transcoding, provider, account, and retry handling; only the
resolved concrete model is sent upstream.

`X-EggPool-Route-Session: <opaque-stable-id>` is an optional EggPool-local
header. On sticky virtual routers it provides the strongest cross-request
affinity signal, especially for stateless Responses calls. It is hashed for
the bounded process-local cache and is never persisted, logged, used as a
metric label, or forwarded upstream. See [Model routing](model-routing.md).

## Health & Readiness

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/healthz` | Liveness check |
| `GET` | `/v1/readyz` | Readiness check |

## Upstream Diagnostics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/backoffs` | Active upstream-derived account backoffs (`?now=<epoch>` for reproducible snapshots) |

## Model Info

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/model-info` | Enriched model metadata summaries |
| `GET` | `/api/model-info/{model_id}` | Enriched metadata detail for one model |
| `GET` | `/api/model-info/{model_id}/aliases` | Source-keyed alias rows for one model |
| `GET` | `/api/model-info/{model_id}/matches` | Match evidence diagnostics for one model |
| `GET` | `/api/model-info/sources` | Model-info source health and diagnostics per source |
| `POST` | `/api/model-info/refresh` | Trigger model-info refresh (auth-gated; supports `?model_id=&source=&force=1`) |

## Stats & Observability

Most `/api/stats/*` endpoints are public when the dashboard is public;
per-request traces stay auth-gated regardless.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats/summary` | Aggregate request stats (counts, tokens, cost, latency) |
| `GET` | `/api/stats/accounts` | Per-account usage roll-up |
| `GET` | `/api/stats/models` | Per-model usage roll-up |
| `GET` | `/api/stats/timeseries` | Time-bucketed usage series (`?period=`) |
| `GET` | `/api/stats/bandwidth` | Request/response byte totals |
| `GET` | `/api/stats/errors` | Error-class distribution |
| `GET` | `/api/stats/latency` | Latency percentiles |
| `GET` | `/api/stats/pings` | Provider ping history |
| `GET` | `/api/stats/ips` | Client IP aggregates |
| `GET` | `/api/stats/attempts` | Per-attempt outcome aggregates |
| `GET` | `/api/stats/retries` | Retry distribution |
| `GET` | `/api/stats/routing` | Routing decision distribution |
| `GET` | `/api/stats/routing-selections` | Selection breakdown by model/account |
| `GET` | `/api/stats/routing-exclusions` | Exclusion reasons breakdown |
| `GET` | `/api/stats/routing-skew` | Selection skew summary |
| `GET` | `/api/stats/routing/eligibility` | Per-account routing eligibility diagnostics |
| `GET` | `/api/stats/operational` | Operational health roll-up |
| `GET` | `/api/stats/pending-health` | Pending-health probe state |
| `GET` | `/api/stats/pricing-provenance` | Pricing data provenance |
| `GET` | `/api/stats/thinking` | Thinking/reasoning decision counter snapshot |
| `GET` | `/api/stats/cache-observability` | Cache counter status coverage |
| `GET` | `/api/stats/canonical-request-segmentation` | Segmentation status, counts, and token estimates |
| `GET` | `/api/stats/cache-stability` | Transcoder cache boundary tracker counters |
| `GET` | `/api/stats/request-shaping` | Operator-facing request-shaping summary |
| `GET` | `/api/stats/transcoding` | Protocol transcoding statistics (JSON) |
| `GET` | `/api/stats/runtime` | Runtime metrics, routing guardrails, background task summaries, stream diagnostics, and `finalization_supervisor` snapshot |
| `GET` | `/api/stats/update` | PyPI update check status |
| `GET` | `/api/stats/recent-requests` | Bounded recent-requests list (auth-gated) |
| `GET` | `/api/stats/recent/{request_id}` | Per-request trace detail (auth-gated) |
| `GET` | `/api/network/diagnostics` | Network and outbound-client diagnostics |

## Dashboard

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/` with request stats, latency metrics, provider health, model-info detail pages, and more. Stats API available under `/api/stats/*`.

## Events

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/events` | Operational event log |

## Request Body Limits

Request ingestion is bounded by `[server].max_request_body_bytes` (default 10 MiB). Provider document and image limits remain additional constraints; they never raise the whole-request ceiling. Oversized bodies are rejected before JSON parsing or transcoding. The field is live-reloadable with `eggpool rehash`.

## Authentication

All endpoints require the server API key unless `[server].api_key` is unset (loopback-only development). The key is sent as `Authorization: Bearer <key>` for OpenAI-compatible endpoints or `x-api-key: <key>` for Anthropic-compatible endpoints.
