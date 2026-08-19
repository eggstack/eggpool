# API Reference

EggPool exposes OpenAI Chat Completions- and Anthropic Messages-compatible paths, plus internal diagnostic endpoints.

## Chat Completions & Anthropic Messages

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions-compatible requests |
| `POST` | `/v1/responses` | Stateless OpenAI Responses-compatible requests (passthrough only) |
| `POST` | `/v1/messages` | Anthropic Messages-compatible requests |

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

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats/cache-observability` | Cache counter status coverage |
| `GET` | `/api/stats/canonical-request-segmentation` | Segmentation status, counts, and token estimates |
| `GET` | `/api/stats/cache-stability` | Transcoder cache boundary tracker counters |
| `GET` | `/api/stats/compression-observability` | Observe-mode opportunity, per-policy roll-ups |
| `GET` | `/api/stats/compression-runtime` | Safe-mode applied/fallback counts and latency |
| `GET` | `/api/stats/compression-policies` | Per-policy roll-up table |
| `GET` | `/api/stats/request-shaping` | Operator-facing request-shaping summary |
| `GET` | `/api/stats/runtime` | Runtime metrics, routing guardrails, background task summaries, stream diagnostics, and `finalization_supervisor` snapshot |
| `GET` | `/api/stats/summary` | Aggregate request stats (counts, tokens, cost, latency) |
| `GET` | `/api/stats/thinking` | Thinking/reasoning decision counter snapshot |
| `GET` | `/api/stats/update` | PyPI update check status |
| `GET` | `/api/stats/routing/eligibility` | Per-account routing eligibility diagnostics |
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
